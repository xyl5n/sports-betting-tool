"""
statcast_client.py
==================
pybaseball-backed Statcast data for the player profile page.

Powers the heavy follow-up sections:
  * get_batter_percentiles / get_pitcher_percentiles  (Tab 2 Overview)
  * get_pitch_mix                                      (Tab 3 Matchup — arsenal)
  * get_batter_vs_pitch_types                          (Tab 3 Matchup — bvp table)

Design rules (per spec):
  * pybaseball calls are slow -> always cache the COMPUTED result (small
    JSON, never the raw DataFrame) in Supabase.  Percentiles refresh
    weekly; pitch-mix / batter-vs-pitch refresh daily.
  * Percentiles are computed against built-in MLB league *reference
    distributions* per metric (mean / std), NOT by re-downloading the
    whole league every week — that would time out / OOM on Railway.
  * Every public function is best-effort: it never raises and returns
    ``{"available": False, "note": ...}`` on ImportError / timeout / empty
    data so the page shows a clean "Data unavailable" message.

All log lines are prefixed STATCAST so they're easy to grep in Railway.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from typing import Optional

from . import db as _db

try:
    from .player_profile_client import _CURRENT_SEASON
except Exception:                                                         # noqa: BLE001
    _CURRENT_SEASON = 2025


def _log(msg: str) -> None:
    print(f"STATCAST: {msg}", file=sys.stderr, flush=True)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _season_dates(season: int) -> tuple[str, str]:
    """Regular-season-ish span for *season* (spring through November)."""
    return f"{season}-03-01", f"{season}-11-30"


# ──────────────────────────────────────────────────────────────────────────
# League reference distributions  (mean, std, lower_is_better)
# Approximate qualified-MLB distributions; percentile = normal CDF vs these.
# ──────────────────────────────────────────────────────────────────────────

# Each entry: key -> (label, fmt, mean, std, lower_better)
#   fmt: "rate3" (.000), "pct" (NN%), "one" (NN.N), "two" (N.NN)
_BATTER_METRICS: list[tuple] = [
    ("xwoba",     "xwOBA",         "rate3", 0.320,  0.035, False),
    ("xba",       "xBA",           "rate3", 0.250,  0.025, False),
    ("slg",       "SLG",           "rate3", 0.415,  0.055, False),
    ("ev",        "Avg Exit Velo", "one",   89.0,   2.0,   False),
    ("maxev",     "Max Exit Velo", "one",   109.5,  2.5,   False),
    ("barrel",    "Barrel%",       "pct",   8.0,    3.5,   False),
    ("hardhit",   "Hard-Hit%",     "pct",   39.5,   6.0,   False),
    ("sweetspot", "Sweet-Spot%",   "pct",   33.0,   4.0,   False),
    ("la",        "Avg Launch°",   "one",   12.5,   4.0,   False),
    ("chase",     "Chase%",        "pct",   28.5,   4.5,   True),
    ("whiff",     "Whiff%",        "pct",   24.5,   5.0,   True),
    ("zcontact",  "Z-Contact%",    "pct",   85.0,   4.0,   False),
    ("k",         "K%",            "pct",   22.5,   5.0,   True),
    ("bb",        "BB%",           "pct",   8.5,    2.5,   False),
]

_PITCHER_METRICS: list[tuple] = [
    ("xera",            "xERA",            "two",   4.10,  0.70,  True),
    ("xwoba_against",   "xwOBA Against",   "rate3", 0.315, 0.030, True),
    ("ev_against",      "Avg EV Against",  "one",   89.0,  1.5,   True),
    ("barrel_against",  "Barrel% Against", "pct",   8.0,   2.5,   True),
    ("hardhit_against", "Hard-Hit% Agst",  "pct",   39.5,  5.0,   True),
    ("k",               "K%",              "pct",   22.5,  5.5,   False),
    ("bb",              "BB%",             "pct",   8.0,   2.0,   True),
    ("whiff",           "Whiff%",          "pct",   24.5,  5.0,   False),
    ("chase",           "Chase%",          "pct",   28.5,  4.5,   False),
    ("first_strike",    "First-Strike%",   "pct",   60.0,  4.0,   False),
]


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _percentile(value: Optional[float], mean: float, std: float,
                lower_better: bool) -> Optional[float]:
    if value is None or std <= 0:
        return None
    try:
        z = (float(value) - mean) / std
    except (TypeError, ValueError):
        return None
    pct = _norm_cdf(z) * 100.0
    if lower_better:
        pct = 100.0 - pct
    return round(max(1.0, min(99.0, pct)), 0)


def _fmt_value(value: Optional[float], fmt: str) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if fmt == "rate3":
        s = f"{v:.3f}"
        return s.lstrip("0") if v < 1 else s
    if fmt == "pct":
        return f"{v:.1f}%"
    if fmt == "one":
        return f"{v:.1f}"
    if fmt == "two":
        return f"{v:.2f}"
    return str(v)


def _build_rows(raw: dict, metrics: list[tuple]) -> list[dict]:
    """Turn a {key: raw_value} dict into display rows with percentiles."""
    rows: list[dict] = []
    for key, label, fmt, mean, std, lower_better in metrics:
        val = raw.get(key)
        pct = _percentile(val, mean, std, lower_better)
        rows.append({
            "key":        key,
            "label":      label,
            "value":      _fmt_value(val, fmt),
            "percentile": pct,
        })
    return rows


# ──────────────────────────────────────────────────────────────────────────
# Plate-discipline / batted-ball event sets
# ──────────────────────────────────────────────────────────────────────────

_SWING = {
    "hit_into_play", "foul", "foul_tip", "swinging_strike",
    "swinging_strike_blocked", "foul_bunt", "missed_bunt",
    "hit_into_play_score", "hit_into_play_no_out",
}
_WHIFF = {"swinging_strike", "swinging_strike_blocked", "missed_bunt"}
_K_EVENTS = {"strikeout", "strikeout_double_play"}
_WALK_EVENTS = {"walk"}
_HBP_EVENTS = {"hit_by_pitch"}
_SAC_EVENTS = {"sac_fly", "sac_bunt", "sac_fly_double_play", "sac_bunt_double_play"}
_TB = {"single": 1, "double": 2, "triple": 3, "home_run": 4}


def _safe_div(num, den) -> Optional[float]:
    try:
        den = float(den)
        return float(num) / den if den else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ──────────────────────────────────────────────────────────────────────────
# Metric computation from a Statcast DataFrame (pitch-level rows)
# ──────────────────────────────────────────────────────────────────────────

def _compute_batter_metrics(df) -> tuple[dict, int]:
    """Return ({metric_key: raw_value}, plate_appearances) for *df* (a
    batter's pitch-level Statcast frame).  Empty dict when no usable rows."""
    if df is None or len(df) == 0:
        return {}, 0
    cols = set(df.columns)
    needed = {"events", "description", "type", "zone"}
    if not needed.issubset(cols):
        return {}, 0

    ev_series = df["events"]
    pa = int(ev_series.notna().sum())
    if pa == 0:
        return {}, 0

    ev_counts = ev_series.value_counts()

    def _n(name) -> int:
        return int(ev_counts.get(name, 0))

    walks = sum(_n(e) for e in _WALK_EVENTS)
    hbp   = sum(_n(e) for e in _HBP_EVENTS)
    sac   = sum(_n(e) for e in _SAC_EVENTS)
    k     = sum(_n(e) for e in _K_EVENTS)
    ab    = max(0, pa - walks - hbp - sac)

    # Batted balls (in play, with measured exit velocity).
    bip = df[df["type"] == "X"]
    bbe = bip[bip["launch_speed"].notna()] if "launch_speed" in cols else bip.iloc[0:0]
    n_bbe = len(bbe)

    raw: dict = {}

    # xwOBA / xBA (estimated, from the speed-angle model).
    if "estimated_woba_using_speedangle" in cols:
        woba_num = float(bip["estimated_woba_using_speedangle"].fillna(0).sum())
        woba_num += 0.69 * walks + 0.72 * hbp
        raw["xwoba"] = _safe_div(woba_num, pa)
    if "estimated_ba_using_speedangle" in cols:
        raw["xba"] = _safe_div(
            float(bip["estimated_ba_using_speedangle"].fillna(0).sum()), ab)

    # Actual SLG (real total bases / AB).
    tb = sum(_TB[e] * _n(e) for e in _TB)
    raw["slg"] = _safe_div(tb, ab)

    # Batted-ball quality.
    if n_bbe > 0:
        ls = bbe["launch_speed"]
        raw["ev"] = float(ls.mean())
        raw["maxev"] = float(ls.max())
        raw["hardhit"] = float((ls >= 95).sum()) / n_bbe * 100.0
        if "launch_angle" in cols:
            la = bbe["launch_angle"]
            raw["la"] = float(la.mean())
            raw["sweetspot"] = float(la.between(8, 32).sum()) / n_bbe * 100.0
        if "launch_speed_angle" in cols:
            raw["barrel"] = float((bbe["launch_speed_angle"] == 6).sum()) / n_bbe * 100.0

    # Plate discipline (pitch-level).
    desc = df["description"]
    zone = df["zone"]
    swing = desc.isin(_SWING)
    whiff = desc.isin(_WHIFF)
    inzone = zone.between(1, 9)
    outzone = zone.between(11, 14)
    n_swing = int(swing.sum())
    raw["chase"] = _safe_div(int((swing & outzone).sum()), int(outzone.sum()))
    if raw.get("chase") is not None:
        raw["chase"] *= 100.0
    raw["whiff"] = _safe_div(int(whiff.sum()), n_swing)
    if raw.get("whiff") is not None:
        raw["whiff"] *= 100.0
    zsw = int((swing & inzone).sum())
    raw["zcontact"] = _safe_div(int((swing & inzone & ~whiff).sum()), zsw)
    if raw.get("zcontact") is not None:
        raw["zcontact"] *= 100.0
    raw["k"] = _safe_div(k, pa)
    if raw.get("k") is not None:
        raw["k"] *= 100.0
    raw["bb"] = _safe_div(walks, pa)
    if raw.get("bb") is not None:
        raw["bb"] *= 100.0

    return raw, pa


def _compute_pitcher_metrics(df) -> tuple[dict, int]:
    """Return ({metric_key: raw_value}, batters_faced) for *df* (a
    pitcher's pitch-level Statcast frame)."""
    if df is None or len(df) == 0:
        return {}, 0
    cols = set(df.columns)
    needed = {"events", "description", "type", "zone"}
    if not needed.issubset(cols):
        return {}, 0

    ev_series = df["events"]
    pa = int(ev_series.notna().sum())
    if pa == 0:
        return {}, 0
    ev_counts = ev_series.value_counts()

    def _n(name) -> int:
        return int(ev_counts.get(name, 0))

    walks = sum(_n(e) for e in _WALK_EVENTS)
    hbp   = sum(_n(e) for e in _HBP_EVENTS)
    k     = sum(_n(e) for e in _K_EVENTS)

    bip = df[df["type"] == "X"]
    bbe = bip[bip["launch_speed"].notna()] if "launch_speed" in cols else bip.iloc[0:0]
    n_bbe = len(bbe)

    raw: dict = {}

    # xwOBA against (estimated).
    if "estimated_woba_using_speedangle" in cols:
        woba_num = float(bip["estimated_woba_using_speedangle"].fillna(0).sum())
        woba_num += 0.69 * walks + 0.72 * hbp
        xwoba_against = _safe_div(woba_num, pa)
        raw["xwoba_against"] = xwoba_against
        # xERA approximated from xwOBA-against via a documented linear map
        # (≈ league-avg 4.10 at xwOBA .315).  Approximate; Statcast's true
        # xERA model isn't exposed in raw data.
        if xwoba_against is not None:
            raw["xera"] = max(0.0, (xwoba_against - 0.315) * 30.0 + 4.10)

    if n_bbe > 0:
        ls = bbe["launch_speed"]
        raw["ev_against"] = float(ls.mean())
        raw["hardhit_against"] = float((ls >= 95).sum()) / n_bbe * 100.0
        if "launch_speed_angle" in cols:
            raw["barrel_against"] = float((bbe["launch_speed_angle"] == 6).sum()) / n_bbe * 100.0

    desc = df["description"]
    zone = df["zone"]
    swing = desc.isin(_SWING)
    whiff = desc.isin(_WHIFF)
    outzone = zone.between(11, 14)
    n_swing = int(swing.sum())
    raw["k"] = _safe_div(k, pa)
    if raw.get("k") is not None:
        raw["k"] *= 100.0
    raw["bb"] = _safe_div(walks, pa)
    if raw.get("bb") is not None:
        raw["bb"] *= 100.0
    raw["whiff"] = _safe_div(int(whiff.sum()), n_swing)
    if raw.get("whiff") is not None:
        raw["whiff"] *= 100.0
    raw["chase"] = _safe_div(int((swing & outzone).sum()), int(outzone.sum()))
    if raw.get("chase") is not None:
        raw["chase"] *= 100.0

    # First-strike%: of all first pitches of a PA (balls==0 & strikes==0),
    # what share are a strike or in play.
    if {"balls", "strikes"}.issubset(cols):
        first = df[(df["balls"] == 0) & (df["strikes"] == 0)]
        if len(first) > 0:
            raw["first_strike"] = float(first["type"].isin(["S", "X"]).sum()) / len(first) * 100.0

    return raw, pa


# ──────────────────────────────────────────────────────────────────────────
# pybaseball fetch wrappers (best-effort; return None on any failure)
# ──────────────────────────────────────────────────────────────────────────

def _fetch_statcast(kind: str, player_id: int, season: int):
    """Return a Statcast DataFrame for *player_id* in *season*, or None.
    *kind* is 'batter' or 'pitcher'."""
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pybaseball as pb
    except Exception as exc:                                              # noqa: BLE001
        _log(f"pybaseball import failed: {exc}")
        return None
    start, end = _season_dates(season)
    try:
        fn = pb.statcast_batter if kind == "batter" else pb.statcast_pitcher
        df = fn(start, end, player_id)
        return df
    except Exception as exc:                                              # noqa: BLE001
        _log(f"statcast_{kind}({player_id}, {season}) failed: {exc}")
        return None


def _split_by_hand(df, hand_col: str, hand: str):
    """Filter *df* to rows where *hand_col* == 'R'/'L' for the split."""
    try:
        if hand_col in df.columns:
            return df[df[hand_col] == hand]
    except Exception:                                                     # noqa: BLE001
        pass
    return df.iloc[0:0]


# ──────────────────────────────────────────────────────────────────────────
# Public: percentile bars (Tab 2 Overview), cached WEEKLY
# ──────────────────────────────────────────────────────────────────────────

def _percentiles(player_id: int, is_pitcher: bool,
                 season: Optional[int] = None) -> dict:
    season = int(season or _CURRENT_SEASON)
    cache_key = f"statcast_{player_id}_{season}"
    role = "pitcher" if is_pitcher else "batter"

    # Weekly cache (survives within 7 days of the stored fetch time).
    try:
        row = _db.cache_get(cache_key)
        data = (row or {}).get("data") if row else None
        if isinstance(data, dict) and data.get("role") == role:
            fetched = data.get("_fetched_at")
            if fetched:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(fetched)).days
                if age <= 7 and data.get("available"):
                    return data
    except Exception:                                                     # noqa: BLE001
        pass

    df = _fetch_statcast(role, player_id, season)
    if df is None or len(df) == 0:
        return {"available": False, "role": role,
                "note": "Statcast data unavailable."}

    metrics = _PITCHER_METRICS if is_pitcher else _BATTER_METRICS
    compute = _compute_pitcher_metrics if is_pitcher else _compute_batter_metrics
    hand_col = "stand" if is_pitcher else "p_throws"   # split dimension

    splits: dict = {}
    min_pa = {"all": 25, "rhp": 12, "lhp": 12}
    for split, hand in (("all", None), ("rhp", "R"), ("lhp", "L")):
        sub = df if hand is None else _split_by_hand(df, hand_col, hand)
        raw, pa = compute(sub)
        if pa < min_pa[split] or not raw:
            splits[split] = {"available": False,
                             "note": "Not enough data for this split.", "pa": pa}
        else:
            splits[split] = {"available": True, "pa": pa,
                             "rows": _build_rows(raw, metrics)}

    available = any(s.get("available") for s in splits.values())
    out = {
        "available":   available,
        "role":        role,
        "season":      season,
        "splits":      splits,
        "note":        "" if available else "Statcast data unavailable.",
        "_fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if available:
        try:
            _db.cache_set(cache_key, "mlb", _today_str(), out)
        except Exception:                                                 # noqa: BLE001
            pass
    return out


def get_batter_percentiles(player_id: int, season: Optional[int] = None) -> dict:
    return _percentiles(player_id, is_pitcher=False, season=season)


def get_pitcher_percentiles(player_id: int, season: Optional[int] = None) -> dict:
    return _percentiles(player_id, is_pitcher=True, season=season)


# ──────────────────────────────────────────────────────────────────────────
# Public: pitch mix / arsenal (Tab 3), cached DAILY
# ──────────────────────────────────────────────────────────────────────────

_PITCH_NAMES = {
    "FF": "4-Seam FB", "SI": "Sinker", "FC": "Cutter", "SL": "Slider",
    "CU": "Curveball", "KC": "Knuckle Curve", "CH": "Changeup",
    "FS": "Splitter", "ST": "Sweeper", "SV": "Slurve", "FT": "2-Seam FB",
    "KN": "Knuckleball", "EP": "Eephus", "SC": "Screwball", "CS": "Slow Curve",
}


def get_pitch_mix(pitcher_id: int, season: Optional[int] = None) -> dict:
    """Pitch arsenal for *pitcher_id*: usage% + avg velocity per pitch type.
    Cached daily.  Shape: {available, total_types, pitches:[{type, name,
    usage, velocity}]}."""
    season = int(season or _CURRENT_SEASON)
    cache_key = f"pitchmix_{pitcher_id}_{season}"
    today = _today_str()
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today and (row.get("data") or {}).get("available"):
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    df = _fetch_statcast("pitcher", pitcher_id, season)
    if df is None or len(df) == 0 or "pitch_type" not in df.columns:
        return {"available": False, "note": "Pitch data unavailable.",
                "total_types": 0, "pitches": []}

    sub = df[df["pitch_type"].notna()]
    total = len(sub)
    if total == 0:
        return {"available": False, "note": "Pitch data unavailable.",
                "total_types": 0, "pitches": []}

    pitches: list[dict] = []
    for ptype, grp in sub.groupby("pitch_type"):
        n = len(grp)
        usage = round(n / total * 100.0, 1)
        if usage < 1.0:
            continue
        velo = None
        if "release_speed" in grp.columns:
            v = grp["release_speed"].dropna()
            velo = round(float(v.mean()), 1) if len(v) else None
        name = _PITCH_NAMES.get(str(ptype), str(ptype))
        if "pitch_name" in grp.columns:
            pn = grp["pitch_name"].dropna()
            if len(pn):
                name = str(pn.iloc[0])
        pitches.append({"type": str(ptype), "name": name,
                        "usage": usage, "velocity": velo})

    pitches.sort(key=lambda p: p["usage"], reverse=True)
    out = {"available": bool(pitches), "total_types": len(pitches),
           "pitches": pitches,
           "note": "" if pitches else "Pitch data unavailable."}
    if out["available"]:
        try:
            _db.cache_set(cache_key, "mlb", today, out)
        except Exception:                                                 # noqa: BLE001
            pass
    return out


# ──────────────────────────────────────────────────────────────────────────
# Public: batter vs pitch type (Tab 3), cached DAILY
# ──────────────────────────────────────────────────────────────────────────

def get_batter_vs_pitch_types(batter_id: int, pitcher_id: int,
                              season: Optional[int] = None) -> dict:
    """How *batter_id* performs against each pitch type *pitcher_id* throws.

    Returns {available, rows:[{pitch, faced, avg, slg, hr, k_pct}]}.  AVG /
    SLG / K% are computed on the plate appearances that ended on each pitch
    type.  Cached daily."""
    season = int(season or _CURRENT_SEASON)
    cache_key = f"bvp_pitch_{batter_id}_{pitcher_id}_{season}"
    today = _today_str()
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today and (row.get("data") or {}).get("available") is not None:
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    mix = get_pitch_mix(pitcher_id, season)
    if not mix.get("available"):
        return {"available": False, "note": "Pitch data unavailable.", "rows": []}
    pitcher_types = [p["type"] for p in mix["pitches"]]

    df = _fetch_statcast("batter", batter_id, season)
    if df is None or len(df) == 0 or "pitch_type" not in df.columns:
        return {"available": False, "note": "Statcast data unavailable.", "rows": []}

    rows: list[dict] = []
    for ptype in pitcher_types:
        grp = df[df["pitch_type"] == ptype]
        faced = len(grp)
        if faced == 0:
            rows.append({"pitch": _PITCH_NAMES.get(ptype, ptype), "type": ptype,
                         "faced": 0, "avg": "—", "slg": "—", "hr": 0, "k_pct": "—"})
            continue
        ev = grp["events"]
        ev_counts = ev.value_counts()

        def _n(name) -> int:
            return int(ev_counts.get(name, 0))

        pa = int(ev.notna().sum())
        walks = _n("walk")
        hbp = _n("hit_by_pitch")
        sac = sum(_n(e) for e in _SAC_EVENTS)
        ab = max(0, pa - walks - hbp - sac)
        h = sum(_n(e) for e in _TB)
        hr = _n("home_run")
        tb = sum(_TB[e] * _n(e) for e in _TB)
        k = sum(_n(e) for e in _K_EVENTS)
        avg = _safe_div(h, ab)
        slg = _safe_div(tb, ab)
        k_pct = _safe_div(k, pa)
        rows.append({
            "pitch":  mix_name(ptype, mix),
            "type":   ptype,
            "faced":  faced,
            "avg":    _fmt_value(avg, "rate3") if avg is not None else "—",
            "slg":    _fmt_value(slg, "rate3") if slg is not None else "—",
            "hr":     hr,
            "k_pct":  f"{k_pct * 100:.0f}%" if k_pct is not None else "—",
        })

    out = {"available": bool(rows), "rows": rows,
           "note": "" if rows else "No pitch-type data."}
    try:
        _db.cache_set(cache_key, "mlb", today, out)
    except Exception:                                                     # noqa: BLE001
        pass
    return out


def mix_name(ptype: str, mix: dict) -> str:
    for p in mix.get("pitches", []):
        if p.get("type") == ptype:
            return p.get("name", ptype)
    return _PITCH_NAMES.get(ptype, ptype)
