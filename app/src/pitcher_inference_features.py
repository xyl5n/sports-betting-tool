"""
pitcher_inference_features.py
=============================
Build real-stats feature dicts for pitcher prop inference.

Why this exists
---------------
`train_props_models.py` computes per-pitcher rolling features
(szn_/r7_/r14_ of K, BB, H, ER, IP, k_per_9, bb_per_9; days_since_last_start;
ip_last_30d; season platoon splits) from real per-game logs.  Production
inference previously filled all of those columns with the prop's sportsbook
line as a proxy -- the model saw a 21-feature vector of "line, line, line,
..." plus park factor and league-average defaults, ignoring the pitcher's
actual recent form entirely.

This module closes the gap.  Given a prop dict
({player_name, home_team, away_team, commence_time, ...}) it:

  1. Resolves the player_name -> MLB Stats API pitcher id using the cached
     daily schedule (PitcherClient builds that cache already).
  2. Fetches the pitcher's gameLog for the prop's season (cached on disk
     so the same pitcher isn't re-fetched on every prop).
  3. Computes the exact same rolling features the training pipeline does,
     using only games strictly before the prop's commence date.
  4. Fetches the pitcher's season platoon splits vs LHB / RHB.
  5. Returns a {feature_name: float} dict the inference vector builder
     overlays onto the league-average baseline.

Anything the helper can't compute (pitcher unresolved, gameLog empty,
season-too-early with < 2 prior starts) falls through to the existing
neutral defaults so the predictor never crashes -- it just degrades to
the old behavior for that one prop.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# Reuse the platoon-split fetcher / IP parser from the training script
# so the inference path stays byte-identical to training.  The script is
# in app/scripts/ which isn't on the import path during normal runtime,
# so we copy the small helpers below rather than perform a fragile import.

_BASE = "https://statsapi.mlb.com/api/v1"
_CACHE_DIR = Path(".cache")
_RECENT_CACHE_PATH = _CACHE_DIR / "pitcher_recent_stats.json"
_RECENT_CACHE_TTL = 6 * 3600  # 6 hours -- mid-game updates are fine
_HTTP_TIMEOUT = 10
_NAME_ID_CACHE_PATH = _CACHE_DIR / "pitcher_name_to_id.json"

# Same constant list the training script uses for rolling stats.
_ROLL_STATS = ["K", "BB", "H", "ER", "IP", "k_per_9", "bb_per_9"]

# Neutral platoon defaults -- match training script.
_NEUTRAL_SPLITS = {
    "era_vs_lhb": 4.50, "k_rate_vs_lhb": 0.215,
    "era_vs_rhb": 4.50, "k_rate_vs_rhb": 0.215,
}


def _log(msg: str) -> None:
    print(f"[pitcher_inf_feat] {msg}", flush=True, file=sys.stderr)


def _fetch_json(url: str, label: str) -> Optional[dict]:
    """One-shot GET that returns None on any failure.  Inference is
    latency-sensitive so we don't retry -- if the call fails the caller
    falls back to neutral defaults for that prop."""
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "sports-betting-ai/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: HTTP 200 ({ms}ms)")
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: HTTP {exc.code} ({ms}ms)")
        return None
    except Exception as exc:  # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: {type(exc).__name__}: {exc} ({ms}ms)")
        return None


def _parse_ip(value) -> float:
    """MLB Stats API innings like '5.2' = 5 + 2/3 IP -> 5.667."""
    if value is None or value == "":
        return 0.0
    try:
        s = str(value)
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + (float(frac) / 3.0)
    except (TypeError, ValueError):
        return 0.0


# ── Disk cache (recent stats + name->id) ────────────────────────────────────

def _load_cache(path: Path) -> dict:
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _log(f"cache write failed for {path.name}: {exc}")


_recent_cache: dict = _load_cache(_RECENT_CACHE_PATH)
_name_id_cache: dict = _load_cache(_NAME_ID_CACHE_PATH)


# ── Name -> player id resolution ────────────────────────────────────────────

def _resolve_pid(player_name: str, date_str: str) -> Optional[int]:
    """Resolve a pitcher's player_name to MLB Stats API id.

    Strategy:
      1.  Persistent name->id cache (lifetime: until manually cleared).
      2.  PitcherClient's daily schedule cache -- has every probable
          starter's id keyed by full name.
      3.  /people/search?names=<name> as a last resort.

    Returns None when all three miss; caller falls back to defaults.
    """
    norm = (player_name or "").strip().lower()
    if not norm:
        return None

    cached = _name_id_cache.get(norm)
    if isinstance(cached, int) and cached > 0:
        return cached

    # Try the PitcherClient schedule cache -- already populated when the
    # day's slate has been viewed.  Look up by fullName.
    try:
        from .pitcher_client import get_pitcher_client
        client = get_pitcher_client()
        # Trigger the schedule fetch (cached on disk for 1 hour by client)
        schedule = client._get_schedule(date_str)  # noqa: SLF001
        for entry in schedule:
            for side in ("home_pitcher", "away_pitcher"):
                pp = entry.get(side) or {}
                full = (pp.get("fullName") or "").strip().lower()
                pid  = pp.get("id")
                if full and isinstance(pid, int) and pid > 0:
                    # Cache every starter we saw, not just the requested name
                    _name_id_cache[full] = pid
                if full == norm and isinstance(pid, int) and pid > 0:
                    _save_cache(_NAME_ID_CACHE_PATH, _name_id_cache)
                    return pid
    except Exception as exc:  # noqa: BLE001
        _log(f"pid lookup via schedule failed for {player_name!r}: {exc}")

    # Last resort: /people/search.  Pull the first match.
    try:
        from urllib.parse import quote
        url = f"{_BASE}/people/search?names={quote(player_name)}"
        data = _fetch_json(url, label=f"people/search {player_name!r}")
        if data:
            for p in (data.get("people") or []):
                full = (p.get("fullName") or "").strip().lower()
                pid  = p.get("id")
                if isinstance(pid, int) and pid > 0:
                    _name_id_cache[full or norm] = pid
                    _save_cache(_NAME_ID_CACHE_PATH, _name_id_cache)
                    return pid
    except Exception as exc:  # noqa: BLE001
        _log(f"pid lookup via /people/search failed for {player_name!r}: {exc}")

    return None


# ── Pitcher gameLog fetch (per pid per season, cached) ──────────────────────

def _fetch_gamelog(pid: int, season: int) -> list[dict]:
    """Return chronological per-game stats for *pid* in *season*.

    Returns one entry per appearance:
        {date, opp_team, is_home, IP, H, ER, BB, K, games_started, park_team}

    Matches the shape the training script builds from the same endpoint.
    Cached on disk under "gl_<pid>_<season>" with TTL.
    """
    cache_key = f"gl_{pid}_{season}"
    cached    = _recent_cache.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("_ts", 0)
        if time.time() - ts < _RECENT_CACHE_TTL:
            return cached.get("rows") or []

    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=gameLog&group=pitching&season={season}")
    data = _fetch_json(url, label=f"gameLog pid={pid} season={season}")
    if not data:
        return []

    rows: list[dict] = []
    for grp in (data.get("stats") or []):
        for split in (grp.get("splits") or []):
            st        = split.get("stat") or {}
            opp       = ((split.get("opponent") or {}).get("abbreviation")
                         or (split.get("opponent") or {}).get("name") or "")
            home_team = ((split.get("team") or {}).get("abbreviation") or "")
            game_date = split.get("date") or ""
            try:
                is_home = bool(split.get("isHome"))
            except Exception:  # noqa: BLE001
                is_home = False
            try:
                gs = int(st.get("gamesStarted") or 0)
                k  = int(st.get("strikeOuts") or 0)
                bb = int(st.get("baseOnBalls") or 0)
                h  = int(st.get("hits") or 0)
                er = int(st.get("earnedRuns") or 0)
            except (TypeError, ValueError):
                continue
            ip = _parse_ip(st.get("inningsPitched"))
            park_team = home_team if is_home else opp
            rows.append({
                "date":          game_date,
                "opp_team":      opp,
                "park_team":     park_team,
                "is_home":       is_home,
                "IP":            ip,
                "H":             h,
                "ER":            er,
                "BB":            bb,
                "K":             k,
                "games_started": gs,
            })
    rows.sort(key=lambda r: r["date"])

    _recent_cache[cache_key] = {"_ts": time.time(), "rows": rows}
    _save_cache(_RECENT_CACHE_PATH, _recent_cache)
    return rows


# ── Platoon splits fetch (per pid per season, cached) ──────────────────────

def _fetch_splits(pid: int, season: int) -> dict:
    """Return season ERA + K-rate vs LHB and RHB for *pid*.

    Matches fetch_pitcher_platoon_splits in train_props_models.py.
    Returns neutral defaults on any failure.
    """
    cache_key = f"sp_{pid}_{season}"
    cached    = _recent_cache.get(cache_key)
    if isinstance(cached, dict):
        ts = cached.get("_ts", 0)
        if time.time() - ts < _RECENT_CACHE_TTL:
            return cached.get("splits") or dict(_NEUTRAL_SPLITS)

    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=statSplits&group=pitching&season={season}&sitCodes=vl,vr")
    data = _fetch_json(url, label=f"splits pid={pid} season={season}")
    if not data:
        return dict(_NEUTRAL_SPLITS)

    result = dict(_NEUTRAL_SPLITS)
    for grp in (data.get("stats") or []):
        for split in (grp.get("splits") or []):
            code = (split.get("split") or {}).get("code", "")
            st   = split.get("stat") or {}
            try:
                era = float(st.get("era") or 4.50)
                ip  = _parse_ip(st.get("inningsPitched"))
                ks  = int(st.get("strikeOuts") or 0)
                bf  = int(st.get("battersFaced") or (int(ip * 3) + 1))
                k_rate = ks / max(bf, 1)
            except (TypeError, ValueError):
                continue
            if code == "vl":
                result["era_vs_lhb"]    = era
                result["k_rate_vs_lhb"] = round(k_rate, 4)
            elif code == "vr":
                result["era_vs_rhb"]    = era
                result["k_rate_vs_rhb"] = round(k_rate, 4)

    _recent_cache[cache_key] = {"_ts": time.time(), "splits": result}
    _save_cache(_RECENT_CACHE_PATH, _recent_cache)
    return result


# ── Rolling feature computation (matches training exactly) ──────────────────

def _compute_rolling_features(
    rows: list[dict],
    as_of_date: str,
) -> Optional[dict[str, float]]:
    """Compute szn_/r7_/r14_ rolling features as of *as_of_date*.

    Filters *rows* to games strictly before as_of_date (YYYY-MM-DD).
    Mirrors the training-time logic in _build_pitcher_dataset:
        szn_X = expanding mean (all prior games)
        r7_X  = last-7 mean (needs >= 2)
        r14_X = last-14 mean (needs >= 3)

    Also returns days_since_last_start and ip_last_30d.

    Returns None when there are fewer than 2 prior games (matches the
    training-time dropna() that excludes those rows).  Caller falls
    back to defaults / line-as-proxy for early-season starts.
    """
    prior = [r for r in rows if (r.get("date") or "") < as_of_date]
    if len(prior) < 2:
        return None

    # Per-row rate stats (training code clips IP at 0.01 to avoid div-by-zero)
    def _safe_div(num, den):
        return num * 9.0 / max(den, 0.01)

    enriched = []
    for r in prior:
        ip = float(r.get("IP") or 0.0)
        enriched.append({
            "date":          r["date"],
            "K":             float(r.get("K")  or 0),
            "BB":            float(r.get("BB") or 0),
            "H":             float(r.get("H")  or 0),
            "ER":            float(r.get("ER") or 0),
            "IP":            ip,
            "k_per_9":       _safe_div(float(r.get("K")  or 0), ip),
            "bb_per_9":      _safe_div(float(r.get("BB") or 0), ip),
            "games_started": int(r.get("games_started") or 0),
        })

    out: dict[str, float] = {}

    # szn_ = mean across all prior games.
    n = len(enriched)
    for c in _ROLL_STATS:
        out[f"szn_{c}"] = sum(g[c] for g in enriched) / n

    # r7_ = last 7 games (needs >= 2).  Training uses pandas.rolling which
    # produces NaN until min_periods is met; we already returned None for
    # n<2 so the threshold is satisfied.
    last7 = enriched[-7:]
    for c in _ROLL_STATS:
        out[f"r7_{c}"] = sum(g[c] for g in last7) / len(last7)

    # r14_ = last 14 games (needs >= 3 -- if only 2 prior games we still
    # compute it, matching training's behavior of accepting r7 with min=2
    # and r14 with min=3; rows with n=2 would be dropped at training too,
    # but the dataset dropna only checks szn_ + r7_, so r14 with 2 obs
    # is allowed in training.  Match that.)
    last14 = enriched[-14:]
    for c in _ROLL_STATS:
        out[f"r14_{c}"] = sum(g[c] for g in last14) / len(last14)

    # days_since_last_start -- calendar days between last START and as_of_date.
    starts = [g for g in enriched if g["games_started"] > 0]
    if starts:
        try:
            last_start = datetime.fromisoformat(starts[-1]["date"]).date()
            current    = datetime.fromisoformat(as_of_date).date()
            days = (current - last_start).days
            out["days_since_last_start"] = float(max(0, min(30, days)))
        except (TypeError, ValueError):
            out["days_since_last_start"] = 5.0
    else:
        out["days_since_last_start"] = 5.0

    # ip_last_30d -- sum of IP in the 30 days BEFORE as_of_date.
    try:
        cutoff = datetime.fromisoformat(as_of_date).date()
        total_ip = 0.0
        for g in enriched:
            try:
                d = datetime.fromisoformat(g["date"]).date()
                if 0 <= (cutoff - d).days <= 30:
                    total_ip += g["IP"]
            except (TypeError, ValueError):
                continue
        out["ip_last_30d"] = float(total_ip)
    except (TypeError, ValueError):
        out["ip_last_30d"] = 30.0

    return out


# ── Public entry point ──────────────────────────────────────────────────────

def enrich_pitcher_features(prop: dict) -> dict[str, float]:
    """Return real per-pitcher feature values for one prop.

    Output keys (when available):
        szn_K, szn_BB, szn_H, szn_ER, szn_IP, szn_k_per_9, szn_bb_per_9,
        r7_K, r7_BB, r7_H, r7_ER, r7_IP, r7_k_per_9, r7_bb_per_9,
        r14_K, r14_BB, r14_H, r14_ER, r14_IP, r14_k_per_9, r14_bb_per_9,
        days_since_last_start, ip_last_30d,
        era_vs_lhb, k_rate_vs_lhb, era_vs_rhb, k_rate_vs_rhb,
        is_home_i  (derived from PitcherClient schedule when present)

    Returns an empty dict if no enrichment was possible (caller keeps
    the league-average defaults that were already in place).
    """
    player_name = (prop.get("player_name") or "").strip()
    if not player_name:
        return {}

    # Parse the prop's game date from commence_time (ISO).
    commence = (prop.get("commence_time") or "").strip()
    if commence:
        date_str = commence[:10]
    else:
        date_str = date.today().isoformat()

    try:
        season = int(date_str[:4])
    except (TypeError, ValueError):
        season = date.today().year

    pid = _resolve_pid(player_name, date_str)
    if pid is None:
        _log(f"could not resolve pid for {player_name!r} -- defaults only")
        return {}

    rows     = _fetch_gamelog(pid, season)
    rolling  = _compute_rolling_features(rows, date_str) or {}
    splits   = _fetch_splits(pid, season)

    out: dict[str, float] = {}
    out.update(rolling)
    out.update({k: float(v) for k, v in splits.items()})

    # Derive is_home from the day's schedule when we know which side this
    # pitcher started on.  Avoids the silent is_home=False default.
    try:
        from .pitcher_client import get_pitcher_client
        client = get_pitcher_client()
        schedule = client._get_schedule(date_str)  # noqa: SLF001
        for entry in schedule:
            home_pp = entry.get("home_pitcher") or {}
            away_pp = entry.get("away_pitcher") or {}
            if home_pp.get("id") == pid:
                out["is_home_i"] = 1.0
                break
            if away_pp.get("id") == pid:
                out["is_home_i"] = 0.0
                break
    except Exception:  # noqa: BLE001
        pass

    _log(f"enriched {player_name!r} pid={pid} -> {len(out)} real features "
         f"(rolling={len(rolling)}, splits=4, is_home={'is_home_i' in out})")
    return out


def get_is_home_for_pitcher(player_name: str, date_str: str) -> bool | None:
    """Return True if the pitcher is scheduled as the home starter on *date_str*,
    False if away, or None if the pitcher cannot be resolved or found in today's
    schedule.

    Cheap — only resolves the player ID and checks the day's pitcher schedule;
    does NOT fetch game logs or platoon splits.  The pitcher schedule is cached
    by PitcherClient so repeated calls within the same process are free.

    Used by player_profile_client.get_today_prop() to inject the correct
    is_home context before calling predict() so the model sees today's
    home/away status rather than the stale training-snapshot value.
    """
    pid = _resolve_pid(player_name, date_str)
    if pid is None:
        return None
    try:
        from .pitcher_client import get_pitcher_client
        client   = get_pitcher_client()
        schedule = client._get_schedule(date_str)  # noqa: SLF001
        for entry in schedule:
            if (entry.get("home_pitcher") or {}).get("id") == pid:
                return True
            if (entry.get("away_pitcher") or {}).get("id") == pid:
                return False
    except Exception:  # noqa: BLE001
        pass
    return None
