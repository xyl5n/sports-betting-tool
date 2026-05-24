"""
player_matchup.py
=================
Matchup-tab data for the player profile page (Tab 3 — MATCHUP).

Everything here is sourced from FREE APIs (MLB Stats API + Open-Meteo)
and the app's existing static tables (park factors, stadium coords).
All public functions are best-effort: they never raise and return a
``{"available": False, ...}`` shape on any failure so the page can show
a graceful "unavailable" message instead of crashing.

Sections covered (foundation):
  * get_matchup_grade   -- Section A: overall A/B/C/D letter grade
  * get_weather         -- Section B: Open-Meteo temp/conditions/wind
  * get_park            -- Section B: park name + run/HR factors
  * get_opposing_starter-- Section C: opposing probable pitcher + season
                           stats (batter view)

Caching: matchup grade + weather are cached in Supabase per the spec
(per player/game/date and per ballpark/date respectively).  pybaseball
pitch-mix / arsenal / bullpen land in a follow-up.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from typing import Optional

from . import db as _db
from .utils import _fetch_url as _fetch
from . import weather_client as _wx
from . import park_factors as _pf
from . import player_profile_client as _ppc


def _log(msg: str) -> None:
    print(f"MATCHUP: {msg}", file=sys.stderr, flush=True)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────────────
# Weather (Open-Meteo, free, no key) -- temp / conditions / wind
# ──────────────────────────────────────────────────────────────────────────

# WMO weather-code -> human label (Open-Meteo `weathercode`).
_WMO: dict[int, str] = {
    0: "Clear", 1: "Mostly Clear", 2: "Partly Cloudy", 3: "Cloudy",
    45: "Fog", 48: "Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Heavy Drizzle",
    56: "Freezing Drizzle", 57: "Freezing Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Freezing Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow",
    80: "Rain Showers", 81: "Rain Showers", 82: "Heavy Showers",
    85: "Snow Showers", 86: "Snow Showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(deg: float) -> str:
    try:
        return _COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]
    except (TypeError, ValueError):
        return "—"


def get_weather(home_team: str, commence_time_utc: Optional[str]) -> dict:
    """Return game-time weather for the ballpark.

    Shape: ``{"available", "dome", "temperature", "conditions",
    "wind_speed", "wind_dir", "note"}``.  Cached in Supabase per ballpark
    per ET day.  Falls back to neutral dome values for covered parks.
    """
    out = {"available": False, "dome": False, "temperature": None,
           "conditions": "—", "wind_speed": None, "wind_dir": "—",
           "note": "Weather unavailable."}
    if not home_team:
        return out

    if home_team in _wx._DOMES:
        return {"available": True, "dome": True, "temperature": 72.0,
                "conditions": "Dome (Climate Controlled)", "wind_speed": 0.0,
                "wind_dir": "—", "note": ""}

    coords = _wx._team_coords(home_team)
    if not coords:
        return out
    lat, lon = coords

    try:
        game_utc = datetime.fromisoformat(
            (commence_time_utc or "").replace("Z", "+00:00"))
    except Exception:                                                     # noqa: BLE001
        game_utc = datetime.now(timezone.utc)
    date_str = game_utc.date().isoformat()

    cache_key = f"weather_{lat:.2f}_{lon:.2f}_{date_str}"
    try:
        row = _db.cache_get(cache_key)
        if row and (row.get("data") or {}).get("available"):
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    url = (
        f"{_wx._BASE}?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,weathercode,windspeed_10m,winddirection_10m"
        f"&windspeed_unit=mph&temperature_unit=fahrenheit"
        f"&timezone=UTC&start_date={date_str}&end_date={date_str}"
    )
    try:
        data = _fetch(url) or {}
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return out
        # Closest hour to game time.
        best_i, best_d = 0, float("inf")
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
                d = abs((t - game_utc).total_seconds())
                if d < best_d:
                    best_d, best_i = d, i
            except Exception:                                             # noqa: BLE001
                continue

        def _at(key, default=None):
            arr = hourly.get(key) or []
            return arr[best_i] if best_i < len(arr) and arr[best_i] is not None else default

        temp = _at("temperature_2m", 72.0)
        code = int(_at("weathercode", 0) or 0)
        wind = _at("windspeed_10m", 0.0)
        wdir = _at("winddirection_10m", 0.0)
        out = {
            "available":   True,
            "dome":        False,
            "temperature": round(float(temp), 0) if temp is not None else None,
            "conditions":  _WMO.get(code, "—"),
            "wind_speed":  round(float(wind), 0) if wind is not None else None,
            "wind_dir":    _compass(wdir),
            "note":        "",
        }
        try:
            _db.cache_set(cache_key, "mlb", date_str, out)
        except Exception:                                                 # noqa: BLE001
            pass
        return out
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_weather error: {exc}")
        return out


# ──────────────────────────────────────────────────────────────────────────
# Park factors (existing static table)
# ──────────────────────────────────────────────────────────────────────────

def get_park(home_team: str) -> dict:
    """Return ``{"available", "park_name", "run_factor", "hr_factor"}``
    for the home ballpark using the app's existing park-factor table."""
    out = {"available": False, "park_name": "—",
           "run_factor": None, "hr_factor": None}
    if not home_team:
        return out
    try:
        run_f, hr_f = _pf.get_park_factors(home_team)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_park error: {exc}")
        return out
    park_name = (_pf._VENUE_NAME.get(home_team)
                 if hasattr(_pf, "_VENUE_NAME") else None) or home_team
    return {"available": True, "park_name": park_name,
            "run_factor": round(float(run_f), 3),
            "hr_factor": round(float(hr_f), 3)}


# ──────────────────────────────────────────────────────────────────────────
# Opposing starter (batter view) -- probable pitcher + season stats
# ──────────────────────────────────────────────────────────────────────────

def get_opposing_starter(prop: dict, player_name: str) -> dict:
    """Resolve today's opposing starter and pull their season line.

    Shape: ``{"available", "note", "id", "name", "hand", "wins",
    "losses", "era", "whip", "k9", "bb9"}``.  Cached per pitcher per day.
    """
    out = {"available": False, "note": "No opposing starter announced yet.",
           "id": None, "name": "", "hand": ""}
    try:
        pit = _ppc.get_today_opposing_pitcher(prop, player_name)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_opposing_starter resolve error: {exc}")
        return out
    if not pit or not pit.get("id"):
        return out

    pid = pit["id"]
    cache_key = f"oppstarter_{pid}"
    today = _today_str()
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today and (row.get("data") or {}).get("available"):
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    try:
        ss = _ppc.get_season_stats(pid, is_pitcher=True) or {}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_opposing_starter season error: {exc}")
        ss = {}

    out = {
        "available": True, "note": "",
        "id": pid, "name": pit.get("name", ""), "hand": pit.get("hand", ""),
        "wins": int(ss.get("wins") or 0), "losses": int(ss.get("losses") or 0),
        "era": float(ss.get("era") or 0.0), "whip": float(ss.get("whip") or 0.0),
        "k9": float(ss.get("k9") or 0.0), "bb9": float(ss.get("bb9") or 0.0),
    }
    try:
        _db.cache_set(cache_key, "mlb", today, out)
    except Exception:                                                     # noqa: BLE001
        pass
    return out


# ──────────────────────────────────────────────────────────────────────────
# Overall matchup grade (Section A)
# ──────────────────────────────────────────────────────────────────────────

# Letter cutoffs on a 0-100 score.  Higher = more favorable for the
# bet side implied by the prop (Over for batters' offensive props,
# pitcher dominance for pitchers).
_GRADE_BANDS = [
    (92, "A+"), (85, "A"), (80, "A-"),
    (75, "B+"), (68, "B"), (63, "B-"),
    (58, "C+"), (50, "C"), (45, "C-"),
    (40, "D+"), (33, "D"), (0,  "D-"),
]


def _score_to_letter(score: float) -> str:
    for cutoff, letter in _GRADE_BANDS:
        if score >= cutoff:
            return letter
    return "D-"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _player_is_home(prop: dict, opp_abbrev: Optional[str]) -> bool:
    """The home team's park is always the venue.  We can't always tell
    which side the player is on from the prop alone, so default to the
    home team being the venue (park factor uses home_team regardless)."""
    return True


def _recent_form_delta(games: list[dict], stat_key: str) -> Optional[float]:
    """Return (L5 avg - season avg) / season avg for *stat_key*, or None."""
    vals = [g.get(stat_key) for g in games if isinstance(g.get(stat_key), (int, float))]
    if len(vals) < 6:
        return None
    season = sum(vals) / len(vals)
    last5 = sum(vals[-5:]) / 5
    if season <= 0:
        return None
    return (last5 - season) / season


def get_matchup_grade(
    info: dict,
    prop: dict,
    games: list[dict],
    is_pitcher: bool,
    *,
    stat_key: Optional[str] = None,
) -> dict:
    """Compute a letter grade + one-sentence rationale from park factor,
    opposing-pitcher quality, and the player's recent form.

    Shape: ``{"available", "grade", "color", "summary"}``.  Cached per
    player per game per date.
    """
    out = {"available": False, "grade": "—", "color": "dim", "summary": ""}
    pid = info.get("id")
    home_team = (prop.get("home_team") or "").strip()
    date_str = (prop.get("commence_time") or "")[:10] or _today_str()
    game_pk = prop.get("game_id") or f"{home_team}_{date_str}"

    cache_key = f"matchup_grade_{pid}_{game_pk}_{date_str}"
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == date_str and (row.get("data") or {}).get("available"):
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    # ── Signals ──────────────────────────────────────────────────────────
    factors: list[tuple[str, float]] = []   # (reason, contribution -50..+50)
    score = 50.0

    # 1) Park factor (run environment).
    try:
        run_f, _ = _pf.get_park_factors(home_team)
        park_c = _clamp((float(run_f) - 1.0) * 100.0, -12.0, 12.0)
    except Exception:                                                     # noqa: BLE001
        run_f, park_c = 1.0, 0.0
    if not is_pitcher:
        score += park_c
        if park_c >= 4:
            factors.append(("a hitter-friendly park", park_c))
        elif park_c <= -4:
            factors.append(("a pitcher-friendly park", park_c))
    else:
        score -= park_c          # low run env favors the pitcher
        if park_c <= -4:
            factors.append(("a pitcher-friendly park", -park_c))
        elif park_c >= 4:
            factors.append(("a hitter-friendly park", -park_c))

    # 2) Opposing pitcher quality (batter view only).
    if not is_pitcher:
        opp = get_opposing_starter(prop, info.get("name", ""))
        if opp.get("available"):
            era = opp.get("era") or 4.0
            k9 = opp.get("k9") or 8.5
            era_c = _clamp((era - 4.0) * 6.0, -15.0, 15.0)     # high ERA -> good for batter
            k9_c = _clamp((8.5 - k9) * 2.5, -10.0, 10.0)       # low K/9 -> good for batter
            score += era_c + k9_c
            if era_c >= 5:
                factors.append((f"a weak opposing starter ({era:.2f} ERA)", era_c))
            elif era_c <= -5:
                factors.append((f"a tough opposing starter ({era:.2f} ERA)", era_c))

    # 3) Recent form vs season.
    sk = stat_key or ("K" if is_pitcher else "H")
    delta = _recent_form_delta(games, sk)
    if delta is not None:
        form_c = _clamp(delta * 40.0, -15.0, 15.0)
        score += form_c
        if form_c >= 5:
            factors.append(("hot recent form", form_c))
        elif form_c <= -5:
            factors.append(("cold recent form", form_c))

    score = _clamp(score, 5.0, 99.0)
    grade = _score_to_letter(score)

    color = "pos" if score >= 63 else ("warn" if score >= 45 else "neg")

    # One-sentence rationale from the two strongest signals.
    factors.sort(key=lambda x: abs(x[1]), reverse=True)
    if factors:
        reasons = " and ".join(r for r, _ in factors[:2])
        summary = f"Grade reflects {reasons}."
    else:
        summary = "Average matchup with no standout edges."

    out = {"available": True, "grade": grade, "color": color, "summary": summary,
           "score": round(score, 1)}
    try:
        _db.cache_set(cache_key, "mlb", date_str, out)
    except Exception:                                                     # noqa: BLE001
        pass
    return out


# ──────────────────────────────────────────────────────────────────────────
# Percentile colour helper (shared with the Overview tab)
# ──────────────────────────────────────────────────────────────────────────

def percentile_color(pct: Optional[float]) -> str:
    """Map a 0-100 percentile to a semantic colour name: 'neg' (<40),
    'warn' (40-60), 'pos' (>60).  None -> 'dim'."""
    if pct is None:
        return "dim"
    if pct >= 60:
        return "pos"
    if pct >= 40:
        return "warn"
    return "neg"


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# (mean, std, lower_is_better) reference distributions for qualified MLB
# starters -- used to turn a raw rate stat into an approximate percentile
# for the starter-card badges.  Approximate; refreshed when real
# league-wide computation lands in the follow-up.
_PITCHER_REF: dict[str, tuple[float, float, bool]] = {
    "era":  (4.00, 0.85, True),
    "whip": (1.28, 0.13, True),
    "k9":   (8.60, 1.70, False),
    "bb9":  (3.10, 0.80, True),
}


def pitcher_stat_percentile(stat: str, value: Optional[float]) -> Optional[float]:
    """Approximate 0-100 percentile for a starter rate stat vs the league
    reference distribution.  Higher percentile = better.  None on bad input."""
    ref = _PITCHER_REF.get(stat)
    if not ref or value is None:
        return None
    mean, std, lower_better = ref
    if std <= 0:
        return None
    try:
        z = (float(value) - mean) / std
    except (TypeError, ValueError):
        return None
    pct = _norm_cdf(z) * 100.0
    if lower_better:
        pct = 100.0 - pct
    return round(_clamp(pct, 1.0, 99.0), 0)
