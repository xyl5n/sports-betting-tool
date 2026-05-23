"""
train_props_models.py
=====================
Expanded training pipeline for MLB player-prop models (pitcher + batter).

Data sources
------------
* MLB Stats API (statsapi.mlb.com) -- per-game logs + season platoon splits
  (free, no key required)
* Hardcoded park-factor dicts for all 30 stadiums

Multi-season training
---------------------
Loops over TRAINING_SEASONS = [2023, 2024, 2025].  Each season's raw game
logs are cached separately so reruns skip the ~1000-call fetch:
    .cache/props_training_data_2023.json
    .cache/props_training_data_2024.json
    .cache/props_training_data_2025.json

Platoon splits (pitcher ERA/K vs LHB/RHB; batter OPS vs LHP/RHP) are
cached per season in:
    .cache/props_training_splits_2023.json
    .cache/props_training_splits_2024.json
    .cache/props_training_splits_2025.json

Feature sets
------------
Pitcher features (computed at training time — real data):
  season-to-date and 7/14-game rolling averages of K, BB, H, ER, IP,
  K/9, BB/9; days_since_last_start; ip_last_30d (fatigue);
  ballpark_factor_k (hardcoded per stadium); is_home;
  era_vs_lhb / k_rate_vs_lhb / era_vs_rhb / k_rate_vs_rhb (season splits)

Pitcher features (inference-time only — zero during training; model
  learns weights when real values are provided at prediction time):
  lineup_avg_k_rate, lineup_lhb_count, lineup_rhb_count,
  weather_temp, weather_wind_speed, weather_wind_dir_num,
  time_of_day, umpire_k_rate, implied_total,
  first_inning_k_pct, pitch_mix_fastball_pct, pitch_mix_breaking_pct,
  pitch_mix_offspeed_pct

Batter features (real):
  season-to-date and 7/14-game rolling averages of H, HR, RBI, R, BB,
  SO, TB, AB, PA, H_per_AB, TB_per_AB, HR_per_AB, BB_per_PA, SO_per_PA,
  k_pct (SO/PA); babip_7d / babip_14d (BABIP approximation);
  batting_order; is_home; ballpark_factor_hits; ballpark_factor_hr;
  ops_vs_lhp / ops_vs_rhp / obp_vs_lhp / slg_vs_lhp / obp_vs_rhp /
  slg_vs_rhp (season splits)

Batter features (inference-time only — zero during training):
  whiff_pct, chase_pct, hard_hit_rate, barrel_rate, sprint_speed,
  platoon_matchup_flag, weather_temp, weather_wind_speed, time_of_day,
  ba_vs_breaking, ba_vs_fastball, ba_vs_offspeed,
  h2h_career_ab, h2h_career_avg, h2h_career_k_rate, implied_total

SHAP analysis
-------------
After each model trains, SHAP TreeExplainer computes feature importances.
Top-15 features per model are logged and saved to:
    .cache/props_feature_importance.json

Supabase push
-------------
Reads SUPABASE_URL + SUPABASE_KEY from env.  When python-dotenv is
installed and .env exists the training script loads it automatically
so local runs without Railway env vars work too.

Usage
-----
    python app/scripts/train_props_models.py
    python app/scripts/train_props_models.py --seasons 2023 2024 2025
    python app/scripts/train_props_models.py --refresh-data
    python app/scripts/train_props_models.py --skip-pitcher --skip-batter --no-push

All progress is prefixed PROPS-TRAIN so it is easy to grep in Railway
logs or terminal output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_CACHE_DIR = Path(".cache")
_TRAIN_DIR = _CACHE_DIR / "props_train"
_STATS_BASE = "https://statsapi.mlb.com/api/v1"
_HTTP_SLEEP = 0.05   # 50 ms between calls → ~20 req/s, well under free-API limits
_HTTP_TIMEOUT = 15
_MIN_PITCHER_STARTS = 5
_MIN_BATTER_PA = 20
TRAINING_SEASONS = [2023, 2024, 2025]

# Regression target stats — one XGBRegressor per stat per bucket
_PITCHER_REG_STATS = ["K", "ER", "H", "BB", "outs"]
_BATTER_REG_STATS  = ["H", "TB", "HR", "RBI", "R", "BB"]


# ---------------------------------------------------------------------------
# Park factors (hardcoded for all 30 MLB stadiums, keyed by team abbreviation)
# Neutral = 1.00.  Source: multi-year FanGraphs park factors approximated.
# ---------------------------------------------------------------------------

# Strikeout park factor  (>1 = more Ks, <1 = fewer Ks)
PARK_FACTORS_K: dict[str, float] = {
    "ARI": 0.96,  # Chase Field (retractable roof, hitter-friendly)
    "ATL": 0.99,  # Truist Park
    "BAL": 1.00,  # Camden Yards
    "BOS": 0.96,  # Fenway Park (small, hitter-friendly)
    "CHC": 0.97,  # Wrigley Field
    "CIN": 0.96,  # Great American Ball Park
    "CLE": 1.01,  # Progressive Field
    "COL": 0.88,  # Coors Field (thin air — batters make contact)
    "CWS": 0.99,  # Guaranteed Rate Field
    "DET": 1.02,  # Comerica Park (large park)
    "HOU": 1.00,  # Minute Maid Park
    "KC":  1.02,  # Kauffman Stadium (large)
    "LAA": 1.01,  # Angel Stadium
    "LAD": 1.04,  # Dodger Stadium (pitcher-friendly)
    "MIA": 1.00,  # loanDepot Park (retractable roof)
    "MIL": 1.00,  # American Family Field (retractable roof)
    "MIN": 1.00,  # Target Field
    "NYM": 1.03,  # Citi Field (pitcher-friendly)
    "NYY": 0.94,  # Yankee Stadium (short porch, hitter-friendly)
    "OAK": 1.04,  # Oakland Coliseum (large, pitcher-friendly)
    "PHI": 0.95,  # Citizens Bank Park (hitter-friendly)
    "PIT": 1.04,  # PNC Park (pitcher-friendly)
    "SD":  1.05,  # Petco Park (large, pitcher-friendly)
    "SEA": 1.04,  # T-Mobile Park (pitcher-friendly)
    "SF":  1.05,  # Oracle Park (pitcher-friendly)
    "STL": 1.03,  # Busch Stadium
    "TB":  1.03,  # Tropicana Field (dome)
    "TEX": 0.97,  # Globe Life Field (dome, hitter-friendly)
    "TOR": 0.99,  # Rogers Centre (turf dome)
    "WSH": 1.00,  # Nationals Park
}

# Hit park factor (>1 = more hits allowed)
PARK_FACTORS_H: dict[str, float] = {
    "ARI": 0.97,
    "ATL": 0.99,
    "BAL": 1.01,
    "BOS": 1.08,  # Fenway — lots of balls off the wall
    "CHC": 1.05,
    "CIN": 1.08,
    "CLE": 0.99,
    "COL": 1.25,  # Coors — very high hit rates
    "CWS": 1.00,
    "DET": 0.97,
    "HOU": 0.99,
    "KC":  0.97,
    "LAA": 1.01,
    "LAD": 0.93,
    "MIA": 0.98,
    "MIL": 1.00,
    "MIN": 1.00,
    "NYM": 0.95,
    "NYY": 1.05,
    "OAK": 0.96,
    "PHI": 1.07,
    "PIT": 0.95,
    "SD":  0.90,
    "SEA": 0.93,
    "SF":  0.89,
    "STL": 0.95,
    "TB":  0.97,
    "TEX": 1.02,
    "TOR": 0.99,
    "WSH": 1.00,
}

# Home-run park factor (>1 = more HRs)
PARK_FACTORS_HR: dict[str, float] = {
    "ARI": 1.05,
    "ATL": 1.08,
    "BAL": 1.10,
    "BOS": 1.12,
    "CHC": 1.08,
    "CIN": 1.35,
    "CLE": 0.95,
    "COL": 1.26,
    "CWS": 1.15,
    "DET": 0.85,
    "HOU": 0.95,
    "KC":  0.88,
    "LAA": 1.05,
    "LAD": 0.87,
    "MIA": 0.85,
    "MIL": 1.05,
    "MIN": 1.10,
    "NYM": 0.93,
    "NYY": 1.40,
    "OAK": 0.80,
    "PHI": 1.30,
    "PIT": 0.82,
    "SD":  0.72,
    "SEA": 0.82,
    "SF":  0.60,
    "STL": 0.90,
    "TB":  0.90,
    "TEX": 1.10,
    "TOR": 1.05,
    "WSH": 0.95,
}

# Stadium geo-coordinates — used at inference time to fetch Open-Meteo weather.
# Included here so the inference layer has a single source of truth.
STADIUM_COORDS: dict[str, tuple[float, float]] = {
    "ARI": (33.4455, -112.0667),
    "ATL": (33.8908, -84.4678),
    "BAL": (39.2838, -76.6218),
    "BOS": (42.3467, -71.0972),
    "CHC": (41.9484, -87.6553),
    "CIN": (39.0975, -84.5065),
    "CLE": (41.4962, -81.6852),
    "COL": (39.7559, -104.9942),
    "CWS": (41.8299, -87.6338),
    "DET": (42.3390, -83.0485),
    "HOU": (29.7573, -95.3555),
    "KC":  (39.0517, -94.4803),
    "LAA": (33.8003, -117.8827),
    "LAD": (34.0739, -118.2400),
    "MIA": (25.7781, -80.2197),
    "MIL": (43.0280, -87.9712),
    "MIN": (44.9817, -93.2778),
    "NYM": (40.7571, -73.8458),
    "NYY": (40.8296, -73.9262),
    "OAK": (37.7516, -122.2005),
    "PHI": (39.9061, -75.1665),
    "PIT": (40.4469, -80.0057),
    "SD":  (32.7076, -117.1570),
    "SEA": (47.5914, -122.3325),
    "SF":  (37.7786, -122.3893),
    "STL": (38.6226, -90.1928),
    "TB":  (27.7682, -82.6534),
    "TEX": (32.7473, -97.0832),
    "TOR": (43.6414, -79.3894),
    "WSH": (38.8730, -77.0074),
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"PROPS-TRAIN: {msg}", flush=True, file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch_json(url: str, *, label: str = "", retries: int = 2) -> Optional[dict]:
    """GET *url* and return parsed JSON.  Returns None on permanent failure."""
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "sports-betting-ai/1.0"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                _log(f"  {label} HTTP {exc.code} -- retry in {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            _log(f"  {label} HTTP {exc.code} -- giving up")
            return None
        except urllib.error.URLError as exc:
            if attempt < retries:
                _log(f"  {label} network -- retry in {delay}s ({exc.reason})")
                time.sleep(delay)
                delay *= 2
                continue
            return None
        except Exception as exc:  # noqa: BLE001
            _log(f"  {label} {type(exc).__name__}: {exc}")
            return None
    return None


# ---------------------------------------------------------------------------
# Player list
# ---------------------------------------------------------------------------

def fetch_players_for_season(season: int) -> list[dict]:
    """Return [{id, name, position_code, team_abbrev}] for every active
    MLB player in *season*.  One HTTP call."""
    url = f"{_STATS_BASE}/sports/1/players?season={season}"
    _log(f"fetching player list for {season}")
    data = _fetch_json(url, label=f"players({season})")
    if not data:
        _log("  player list fetch FAILED -- aborting")
        return []
    out: list[dict] = []
    for p in (data.get("people") or []):
        try:
            pos  = ((p.get("primaryPosition") or {}).get("code") or "").strip()
            team = ((p.get("currentTeam") or {}).get("abbreviation") or "")
            out.append({
                "id":            int(p.get("id") or 0),
                "name":          p.get("fullName") or "",
                "position_code": pos,
                "team_abbrev":   team,
            })
        except (TypeError, ValueError):
            continue
    _log(f"  parsed {len(out)} players for {season}")
    return out


# ---------------------------------------------------------------------------
# Platoon splits (season-level, fetched once per player per season)
# ---------------------------------------------------------------------------

def fetch_pitcher_platoon_splits(pid: int, season: int) -> dict:
    """Return season ERA and K-rate vs LHB and RHB.

    Keys: era_vs_lhb, k_rate_vs_lhb, era_vs_rhb, k_rate_vs_rhb.
    Returns neutral defaults on failure."""
    neutral = {"era_vs_lhb": 4.50, "k_rate_vs_lhb": 0.215,
               "era_vs_rhb": 4.50, "k_rate_vs_rhb": 0.215}
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=statSplits&group=pitching&season={season}&sitCodes=vl,vr"
    )
    data = _fetch_json(url, label=f"pitcher splits pid={pid}")
    if not data:
        return neutral
    result = dict(neutral)
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
    return result


def fetch_batter_platoon_splits(pid: int, season: int) -> dict:
    """Return season OPS/OBP/SLG vs LHP and RHP.

    Keys: ops_vs_lhp, obp_vs_lhp, slg_vs_lhp,
          ops_vs_rhp, obp_vs_rhp, slg_vs_rhp.
    Returns neutral defaults on failure."""
    neutral = {
        "ops_vs_lhp": 0.720, "obp_vs_lhp": 0.315, "slg_vs_lhp": 0.405,
        "ops_vs_rhp": 0.720, "obp_vs_rhp": 0.315, "slg_vs_rhp": 0.405,
    }
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=statSplits&group=hitting&season={season}&sitCodes=vl,vr"
    )
    data = _fetch_json(url, label=f"batter splits pid={pid}")
    if not data:
        return neutral
    result = dict(neutral)
    for grp in (data.get("stats") or []):
        for split in (grp.get("splits") or []):
            code = (split.get("split") or {}).get("code", "")
            st   = split.get("stat") or {}
            try:
                ops = float(st.get("ops") or 0.720)
                obp = float(st.get("obp") or 0.315)
                slg = float(st.get("slg") or 0.405)
            except (TypeError, ValueError):
                continue
            if code == "vl":
                result["ops_vs_lhp"] = ops
                result["obp_vs_lhp"] = obp
                result["slg_vs_lhp"] = slg
            elif code == "vr":
                result["ops_vs_rhp"] = ops
                result["obp_vs_rhp"] = obp
                result["slg_vs_rhp"] = slg
    return result


def collect_platoon_splits(
    players: list[dict],
    season: int,
    *,
    is_pitcher: bool,
    refresh: bool = False,
) -> dict[int, dict]:
    """Fetch platoon splits for *players* and cache to disk.

    Returns {player_id: splits_dict}."""
    cache_path = _CACHE_DIR / f"props_training_splits_{season}.json"
    ptype      = "pitchers" if is_pitcher else "batters"

    # Load existing cache (may have only one of pitchers/batters)
    cached_all: dict = {}
    if cache_path.exists() and not refresh:
        try:
            cached_all = json.loads(cache_path.read_text(encoding="utf-8"))
            _log(f"splits cache found: {cache_path}")
        except Exception as exc:  # noqa: BLE001
            _log(f"splits cache read failed ({exc}) -- re-fetching")
            cached_all = {}

    cached_section: dict[str, dict] = cached_all.get(ptype) or {}
    out: dict[int, dict] = {}
    fetch_count = 0
    for i, p in enumerate(players, 1):
        pid = p["id"]
        key = str(pid)
        if key in cached_section and not refresh:
            out[pid] = cached_section[key]
            continue
        # Need to fetch
        if is_pitcher:
            splits = fetch_pitcher_platoon_splits(pid, season)
        else:
            splits = fetch_batter_platoon_splits(pid, season)
        time.sleep(_HTTP_SLEEP)
        out[pid]            = splits
        cached_section[key] = splits
        fetch_count        += 1
        if fetch_count % 50 == 0:
            _log(f"  splits progress ({ptype}): fetched {fetch_count} "
                 f"({i}/{len(players)} total)")

    # Persist updated cache
    cached_all[ptype] = cached_section
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cached_all, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"splits cache write failed: {exc}")

    _log(f"platoon splits ({ptype}): {len(out)} players "
         f"(fetched {fetch_count}, from cache {len(out) - fetch_count})")
    return out


# ---------------------------------------------------------------------------
# Per-pitcher game log
# ---------------------------------------------------------------------------

def _parse_ip(value) -> float:
    """Convert MLB Stats API innings like '5.2' (= 5⅔ IP) to a true float."""
    if value is None or value == "":
        return 0.0
    try:
        s = str(value)
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + (float(frac) / 3.0)
    except (TypeError, ValueError):
        return 0.0


def fetch_pitcher_game_log(pid: int, season: int) -> list[dict]:
    """Return chronological per-game pitcher stats for *pid* in *season*."""
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=gameLog&group=pitching&season={season}"
    )
    data = _fetch_json(url, label=f"pitcher gameLog pid={pid}")
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
                gs  = int(st.get("gamesStarted") or 0)
                k   = int(st.get("strikeOuts") or 0)
                bb  = int(st.get("baseOnBalls") or 0)
                h   = int(st.get("hits") or 0)
                er  = int(st.get("earnedRuns") or 0)
                # PR3: capture homeRuns allowed so career FIP can be computed
                # without a separate fetch.  Older cache files lack this field;
                # consumers default it to None / 0 and fall back to a BB/9
                # proxy in that case.
                hr  = int(st.get("homeRuns") or 0)
            except (TypeError, ValueError):
                continue
            ip = _parse_ip(st.get("inningsPitched"))
            # Derive the park team abbrev: home team when pitcher is at home,
            # opponent team when pitcher is away.
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
                "HR":            hr,
                "games_started": gs,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------------
# Per-batter game log
# ---------------------------------------------------------------------------

def fetch_batter_game_log(pid: int, season: int) -> list[dict]:
    """Return chronological per-game batter stats for *pid* in *season*."""
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=gameLog&group=hitting&season={season}"
    )
    data = _fetch_json(url, label=f"batter gameLog pid={pid}")
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
                ab  = int(st.get("atBats") or 0)
                h   = int(st.get("hits") or 0)
                hr  = int(st.get("homeRuns") or 0)
                rbi = int(st.get("rbi") or 0)
                r   = int(st.get("runs") or 0)
                bb  = int(st.get("baseOnBalls") or 0)
                so  = int(st.get("strikeOuts") or 0)
                tb  = int(st.get("totalBases") or 0)
                pa  = int(st.get("plateAppearances") or (ab + bb))
            except (TypeError, ValueError):
                continue
            order_raw = split.get("battingOrder") or st.get("battingOrder")
            try:
                batting_order = int(order_raw) // 100 if order_raw else 0
            except (TypeError, ValueError):
                batting_order = 0
            park_team = home_team if is_home else opp
            rows.append({
                "date":          game_date,
                "opp_team":      opp,
                "park_team":     park_team,
                "is_home":       is_home,
                "AB":            ab,
                "H":             h,
                "HR":            hr,
                "RBI":           rbi,
                "R":             r,
                "BB":            bb,
                "SO":            so,
                "TB":            tb,
                "PA":            pa,
                "batting_order": batting_order,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------------
# Collection driver (per-season, disk-cached)
# ---------------------------------------------------------------------------

def collect_training_data(season: int, *, refresh: bool = False) -> dict:
    """Fetch game logs for all active MLB players in *season*.

    Cached to .cache/props_training_data_<season>.json.
    Each cached player entry now includes a 'park_team' field per game
    so park factors can be looked up during feature engineering.
    """
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"props_training_data_{season}.json"
    if cache_path.exists() and not refresh:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            _log(
                f"game-log cache HIT {season}: "
                f"pitchers={len(data.get('pitchers') or [])}  "
                f"batters={len(data.get('batters') or [])}"
            )
            return data
        except Exception as exc:  # noqa: BLE001
            _log(f"cache read failed ({exc}) -- re-fetching {season}")

    started = time.monotonic()
    players = fetch_players_for_season(season)
    if not players:
        return {"season": season, "pitchers": [], "batters": []}

    pitchers_ids = [p for p in players if p["position_code"] == "1"]
    batters_ids  = [p for p in players if p["position_code"] not in ("1", "")]
    _log(f"{season}: {len(pitchers_ids)} pitchers, {len(batters_ids)} batters")

    pitcher_payload: list[dict] = []
    for i, p in enumerate(pitchers_ids, 1):
        if i % 25 == 0 or i == len(pitchers_ids):
            _log(f"  pitcher progress {season}: {i}/{len(pitchers_ids)}  "
                 f"kept={len(pitcher_payload)}")
        rows   = fetch_pitcher_game_log(p["id"], season)
        time.sleep(_HTTP_SLEEP)
        starts = sum(1 for r in rows if r.get("games_started"))
        if starts < _MIN_PITCHER_STARTS:
            continue
        pitcher_payload.append({
            "id":    p["id"],
            "name":  p["name"],
            "team":  p["team_abbrev"],
            "games": rows,
        })
    _log(f"pitcher dataset {season}: {len(pitcher_payload)} players kept")

    batter_payload: list[dict] = []
    for i, p in enumerate(batters_ids, 1):
        if i % 25 == 0 or i == len(batters_ids):
            _log(f"  batter progress {season}: {i}/{len(batters_ids)}  "
                 f"kept={len(batter_payload)}")
        rows = fetch_batter_game_log(p["id"], season)
        time.sleep(_HTTP_SLEEP)
        total_pa = sum(int(r.get("PA") or 0) for r in rows)
        if total_pa < _MIN_BATTER_PA:
            continue
        batter_payload.append({
            "id":    p["id"],
            "name":  p["name"],
            "team":  p["team_abbrev"],
            "games": rows,
        })
    _log(f"batter dataset {season}: {len(batter_payload)} players kept")

    payload = {
        "season":     season,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "pitchers":   pitcher_payload,
        "batters":    batter_payload,
    }
    try:
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"game-log cached: {cache_path}")
    except Exception as exc:  # noqa: BLE001
        _log(f"cache write failed: {exc}")
    _log(f"{season} collection done in {time.monotonic() - started:.1f}s")
    return payload


def collect_multi_season_data(
    seasons: list[int],
    *,
    refresh: bool = False,
) -> dict:
    """Collect and merge game-log data across multiple seasons.

    Each season is fetched/cached independently.  Returns a combined dict
    with all pitchers and batters tagged with their season.
    """
    _log(f"collecting data for seasons: {seasons}")
    all_pitchers: list[dict] = []
    all_batters:  list[dict] = []

    for season in seasons:
        data = collect_training_data(season, refresh=refresh)
        for p in (data.get("pitchers") or []):
            entry = dict(p)
            entry["season"] = season
            all_pitchers.append(entry)
        for b in (data.get("batters") or []):
            entry = dict(b)
            entry["season"] = season
            all_batters.append(entry)

    _log(
        f"multi-season merge: {len(all_pitchers)} pitcher-seasons, "
        f"{len(all_batters)} batter-seasons"
    )
    return {"seasons": seasons, "pitchers": all_pitchers, "batters": all_batters}


# ---------------------------------------------------------------------------
# Feature engineering — pitcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Opponent context + pitcher career baseline (PR #2)
# ---------------------------------------------------------------------------
# Three groups of derived features computed once per training run:
#   * Batter handedness map (player_id -> "L"/"R"/"S") fetched in batches from
#     MLB Stats API /people?personIds=... and cached on disk.
#   * Per-(season, team) baselines (k_rate, woba_proxy, lhb_pct) aggregated
#     from cached batter game logs + the handedness map.  These are looked up
#     at inference by opposing batting team so the pitcher model sees opponent
#     quality, not just pitcher form.
#   * Per-pitcher career baselines (k_per_9, bb_per_9, total_ip) aggregated
#     across the entire training horizon.  Stable signal that smooths over
#     small-sample season variance.

_HANDEDNESS_CACHE_PATH         = _CACHE_DIR / "batter_handedness.json"
_PITCHER_HANDEDNESS_CACHE_PATH = _CACHE_DIR / "pitcher_handedness.json"
_TEAM_BASELINES_PATH           = _CACHE_DIR / "team_baselines.json"
_BATTER_TEAMS_CACHE_PATH       = _CACHE_DIR / "batter_teams.json"
_PITCHER_SEASON_STATS_PATH     = _CACHE_DIR / "pitcher_season_stats.json"


# Full team-name → 3-letter abbreviation map (mirror of TEAM_NAME_TO_ABBREV
# in props_model.py).  The cached game logs store opp_team / park_team as
# *full names* ("Cincinnati Reds") because the MLB Stats API's opponent
# field falls back to `name` when `abbreviation` is missing.  Without
# normalizing on read, the PARK_FACTORS_K lookups always miss and the model
# trains on ballpark_factor=1.00 for every row — a silent latent bug
# separate from the inference-side park lookup fix.
_TEAM_NAME_TO_ABBREV: dict[str, str] = {
    "Arizona Diamondbacks":  "ARI",
    "Atlanta Braves":        "ATL",
    "Baltimore Orioles":     "BAL",
    "Boston Red Sox":        "BOS",
    "Chicago Cubs":          "CHC",
    "Cincinnati Reds":       "CIN",
    "Cleveland Guardians":   "CLE",
    "Colorado Rockies":      "COL",
    "Chicago White Sox":     "CWS",
    "Detroit Tigers":        "DET",
    "Houston Astros":        "HOU",
    "Kansas City Royals":    "KC",
    "Los Angeles Angels":    "LAA",
    "Los Angeles Dodgers":   "LAD",
    "Miami Marlins":         "MIA",
    "Milwaukee Brewers":     "MIL",
    "Minnesota Twins":       "MIN",
    "New York Mets":         "NYM",
    "New York Yankees":      "NYY",
    "Oakland Athletics":     "OAK",
    "Athletics":             "OAK",   # mid-cache rebrand; same franchise
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates":    "PIT",
    "San Diego Padres":      "SD",
    "Seattle Mariners":      "SEA",
    "San Francisco Giants":  "SF",
    "St. Louis Cardinals":   "STL",
    "Tampa Bay Rays":        "TB",
    "Texas Rangers":         "TEX",
    "Toronto Blue Jays":     "TOR",
    "Washington Nationals":  "WSH",
}


def _to_abbrev(team_str) -> str:
    """Normalize a team string to its 3-letter abbreviation.

    Accepts both full names ("New York Yankees" → "NYY") and already
    abbreviated inputs ("NYY"); empty / unknown → "".
    """
    if not team_str:
        return ""
    s = str(team_str).strip()
    if s in _TEAM_NAME_TO_ABBREV:
        return _TEAM_NAME_TO_ABBREV[s]
    upper = s.upper()
    if upper in PARK_FACTORS_K:
        return upper
    return ""


def collect_batter_teams(seasons: list[int], *, refresh: bool = False) -> dict[str, str]:
    """Fetch {"<season>:<player_id>" -> "<team_abbrev>"} via per-team rosters.

    The cached game logs don't store batter team per-row, and the player-
    level `team` field is empty for most batters (MLB API quirk).  One
    roster call per (team, season) gives us a definitive mapping stable
    enough to anchor team_baselines aggregation.  ~90 calls on first run
    (30 teams × 3 seasons); cached.
    """
    cache: dict[str, str] = {}
    if _BATTER_TEAMS_CACHE_PATH.exists() and not refresh:
        try:
            cache = json.loads(_BATTER_TEAMS_CACHE_PATH.read_text(encoding="utf-8"))
            _log(f"batter teams cache: {len(cache)} (season, player) entries")
        except Exception as exc:  # noqa: BLE001
            _log(f"batter teams cache read failed ({exc}) -- refetching")

    if cache and not refresh:
        return cache

    teams_data = _fetch_json(f"{_STATS_BASE}/teams?sportId=1", label="teams")
    if not teams_data:
        _log("batter teams: /teams fetch failed -- empty map")
        return cache
    team_ids: list[tuple[int, str]] = []
    for t in (teams_data.get("teams") or []):
        tid = t.get("id")
        abbrev = (t.get("abbreviation") or "").strip().upper()
        if tid and abbrev in PARK_FACTORS_K:
            team_ids.append((int(tid), abbrev))
    _log(f"batter teams: discovered {len(team_ids)} active MLB teams")

    for season in seasons:
        for tid, abbrev in team_ids:
            url = (f"{_STATS_BASE}/teams/{tid}/roster"
                   f"?rosterType=fullSeason&season={season}")
            data = _fetch_json(url, label=f"roster t={abbrev} s={season}")
            if not data:
                continue
            for entry in (data.get("roster") or []):
                pid = (entry.get("person") or {}).get("id")
                if pid:
                    cache[f"{season}:{int(pid)}"] = abbrev
            time.sleep(_HTTP_SLEEP)

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _BATTER_TEAMS_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8",
        )
        _log(f"batter teams saved: {_BATTER_TEAMS_CACHE_PATH} ({len(cache)} entries)")
    except Exception as exc:  # noqa: BLE001
        _log(f"batter teams cache write failed: {exc}")
    return cache


def collect_batter_handedness(payload: dict, *, refresh: bool = False) -> dict[int, str]:
    """Fetch L/R/S batting hand for every unique batter in *payload*.

    Returns {batter_id: "L" | "R" | "S"}.  Cached at .cache/batter_handedness.json
    so reruns skip the ~30 batched HTTP calls.

    Switch hitters (S) are treated as LHB in downstream lineup-LHB% math —
    they bat from the left vs the majority of starters (RHP), which is the
    distribution that drives our pitcher-vs-LHB feature interaction.
    """
    cache: dict[int, str] = {}
    if _HANDEDNESS_CACHE_PATH.exists() and not refresh:
        try:
            raw = json.loads(_HANDEDNESS_CACHE_PATH.read_text(encoding="utf-8"))
            cache = {int(k): str(v) for k, v in raw.items() if v}
            _log(f"batter handedness cache: {len(cache)} players")
        except Exception as exc:  # noqa: BLE001
            _log(f"handedness cache read failed ({exc}) -- refetching")

    # Build set of unique batter ids across all seasons in this payload.
    needed: set[int] = set()
    for b in (payload.get("batters") or []):
        bid = int(b.get("id") or 0)
        if bid and bid not in cache:
            needed.add(bid)

    if not needed:
        _log("batter handedness: all players already cached")
        return cache

    _log(f"batter handedness: fetching {len(needed)} new players in batches")
    ids = sorted(needed)
    BATCH = 50
    fetched = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        url = (f"{_STATS_BASE}/people"
               f"?personIds={','.join(str(x) for x in chunk)}&hydrate=batSide")
        data = _fetch_json(url, label=f"people-batch[{i}:{i+len(chunk)}]")
        if not data:
            continue
        for person in (data.get("people") or []):
            pid = int(person.get("id") or 0)
            if not pid:
                continue
            hand = ((person.get("batSide") or {}).get("code") or "").strip().upper()
            if hand in ("L", "R", "S"):
                cache[pid] = hand
                fetched += 1
        time.sleep(_HTTP_SLEEP)
        if (i // BATCH) % 10 == 0:
            _log(f"  handedness progress: {fetched}/{len(needed)}")

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _HANDEDNESS_CACHE_PATH.write_text(
            json.dumps({str(k): v for k, v in cache.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"batter handedness saved: {_HANDEDNESS_CACHE_PATH} "
             f"({len(cache)} players, +{fetched} new)")
    except Exception as exc:  # noqa: BLE001
        _log(f"handedness cache write failed: {exc}")
    return cache


def collect_pitcher_handedness(payload: dict, *, refresh: bool = False) -> dict[int, str]:
    """Fetch L/R throwing hand for every unique pitcher in *payload*.

    Mirrors collect_batter_handedness but hits the `pitchHand` field instead
    of `batSide`.  Returns {pitcher_id: "L" | "R"} cached at
    .cache/pitcher_handedness.json.

    Used by the batter model's PR3 opp_pitcher_throws_lhp feature: when a
    batter faces a pitcher, the model gets the LH/RH bit so it can interact
    with the platoon-split features (ops_vs_lhp / ops_vs_rhp).  Without
    this, those splits sat as static season-mean signals because the
    matchup-specific direction was unknown.
    """
    cache: dict[int, str] = {}
    if _PITCHER_HANDEDNESS_CACHE_PATH.exists() and not refresh:
        try:
            raw = json.loads(_PITCHER_HANDEDNESS_CACHE_PATH.read_text(encoding="utf-8"))
            cache = {int(k): str(v) for k, v in raw.items() if v}
            _log(f"pitcher handedness cache: {len(cache)} players")
        except Exception as exc:  # noqa: BLE001
            _log(f"pitcher handedness cache read failed ({exc}) -- refetching")

    needed: set[int] = set()
    for p in (payload.get("pitchers") or []):
        pid = int(p.get("id") or 0)
        if pid and pid not in cache:
            needed.add(pid)

    if not needed:
        _log("pitcher handedness: all players already cached")
        return cache

    _log(f"pitcher handedness: fetching {len(needed)} new players in batches")
    ids = sorted(needed)
    BATCH = 50
    fetched = 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        url = (f"{_STATS_BASE}/people"
               f"?personIds={','.join(str(x) for x in chunk)}&hydrate=pitchHand")
        data = _fetch_json(url, label=f"pitcher-people-batch[{i}:{i+len(chunk)}]")
        if not data:
            continue
        for person in (data.get("people") or []):
            pid = int(person.get("id") or 0)
            if not pid:
                continue
            hand = ((person.get("pitchHand") or {}).get("code") or "").strip().upper()
            if hand in ("L", "R"):
                cache[pid] = hand
                fetched += 1
        time.sleep(_HTTP_SLEEP)
        if (i // BATCH) % 10 == 0:
            _log(f"  pitcher handedness progress: {fetched}/{len(needed)}")

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _PITCHER_HANDEDNESS_CACHE_PATH.write_text(
            json.dumps({str(k): v for k, v in cache.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"pitcher handedness saved: {_PITCHER_HANDEDNESS_CACHE_PATH} "
             f"({len(cache)} players, +{fetched} new)")
    except Exception as exc:  # noqa: BLE001
        _log(f"pitcher handedness cache write failed: {exc}")
    return cache


def build_opp_pitcher_lookup(payload: dict) -> tuple[dict[str, int], dict[str, dict[str, float]]]:
    """Build two derived maps from cached pitcher game logs:

      1. opp_pitcher_by_game: "<date>:<batter_team>" -> pitcher_id
         The pitcher's `opp_team` field IS the batter's team, so each
         pitcher-game-log row with games_started > 0 contributes one entry.

      2. pitcher_season_stats: "<season>:<pid>" -> {k_per_9, era}
         Season-end aggregates per pitcher.  Used by the batter feature
         builder to fill opp_pitcher_szn_k_per_9 / opp_pitcher_szn_era
         when the opp pitcher is known.

    These are stored on disk too (pitcher_season_stats.json) so the
    inference path can read them at predict time without rebuilding from
    raw payloads.

    Caveats:
      * Multiple pitchers can have games on the same (date, opp_team) when
        starters are pulled early; we take the FIRST one we see (which is
        the actual starter for the cached game-log payload structure).
      * Season-end aggregates are biased optimistically for backtesting a
        bet from the middle of that season.  Acceptable for v1 — the bias
        is constant across PR iterations so deltas stay interpretable.
        A future PR can swap in per-game pitcher rolling stats if needed.
    """
    opp_lookup: dict[str, int] = {}
    season_stats: dict[str, dict[str, float]] = {}

    # Aggregate per-pitcher season totals
    totals: dict[str, dict[str, float]] = {}
    for p in (payload.get("pitchers") or []):
        pid    = int(p.get("id") or 0)
        season = int(p.get("season") or 0)
        if not pid or not season:
            continue
        season_key = f"{season}:{pid}"
        agg = totals.setdefault(season_key, {"IP": 0.0, "K": 0.0, "ER": 0.0})
        for g in (p.get("games") or []):
            try:
                ip = float(g.get("IP") or 0)
                k  = int(g.get("K") or 0)
                er = int(g.get("ER") or 0)
            except (TypeError, ValueError):
                continue
            agg["IP"] += ip
            agg["K"]  += k
            agg["ER"] += er
            # Record opp_pitcher only for starts
            if int(g.get("games_started") or 0) > 0:
                date = (g.get("date") or "")[:10]
                opp  = _to_abbrev(g.get("opp_team") or "")
                if date and opp:
                    key = f"{date}:{opp}"
                    # First writer wins (the actual starter for that team's game)
                    opp_lookup.setdefault(key, pid)

    # Convert totals to k_per_9 / era
    for key, agg in totals.items():
        ip = max(agg["IP"], 0.01)
        season_stats[key] = {
            "k_per_9": round(agg["K"] * 9.0 / ip, 4),
            "era":     round(agg["ER"] * 9.0 / ip, 4),
            "ip":      round(ip, 1),
        }

    _log(
        f"opp pitcher lookup: {len(opp_lookup)} (date,team)->pid entries, "
        f"{len(season_stats)} (season,pid)->stats entries"
    )

    # Persist season stats (the lookup is only used at training, so no
    # need to write it out — inference resolves opp_pitcher via the live
    # schedule, not historical dates).
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _PITCHER_SEASON_STATS_PATH.write_text(
            json.dumps({
                "generated_at":  datetime.utcnow().isoformat() + "Z",
                "season_stats":  season_stats,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"pitcher season stats saved: {_PITCHER_SEASON_STATS_PATH}")
    except Exception as exc:  # noqa: BLE001
        _log(f"pitcher season stats save failed: {exc}")

    return opp_lookup, season_stats


def compute_team_baselines(
    payload: dict,
    handedness: dict[int, str],
    batter_teams: Optional[dict[str, str]] = None,
) -> dict[str, dict[str, float]]:
    """Aggregate per-(season, team) batter stats from cached game logs.

    Returns {"<season>:<team_abbrev>": {team_k_rate, team_woba, team_lhb_pct}}.

    *team_k_rate* = sum(SO) / sum(PA) across all batters whose team_abbrev
        equals the key team in the given season.
    *team_woba* = OPS proxy = OBP + SLG = (H+BB)/(AB+BB) + TB/AB.  Not a true
        Fangraphs wOBA — that needs split hit totals (1B/2B/3B/HR) we don't
        cache — but OPS correlates highly with wOBA (~0.96) and uses fields
        we already have.
    *team_lhb_pct* = sum(PA) by LHB / sum(PA) total.  Switch hitters are
        counted as LHB (see collect_batter_handedness docstring).

    Batter→team mapping:
      1. *batter_teams* roster lookup keyed by (season, batter_id) (definitive)
      2. Player-level `team` field on the cache entry (often empty)
      3. The first non-empty opp_team in the player's game logs is *NOT* the
         batter's team; we can't reliably derive teams without the roster lookup
         for batters whose cache `team` is blank.

    Keyed as "<season>:<team>" so a single dict survives multi-season pooling.
    Inference looks up by the opposing batting team for the prop's game.
    """
    agg: dict[tuple[int, str], dict[str, float]] = {}
    skipped_no_team = 0
    for b in (payload.get("batters") or []):
        season = int(b.get("season") or 0)
        bid    = int(b.get("id") or 0)
        if not (season and bid):
            continue
        hand = handedness.get(bid, "R")  # default to RHB when handedness unknown
        is_lhb = hand in ("L", "S")
        # Resolve batter's team: roster map first, then cache field.
        team = ""
        if batter_teams:
            team = (batter_teams.get(f"{season}:{bid}") or "").strip().upper()
        if not team:
            team = _to_abbrev((b.get("team") or "").strip())
        if not team:
            skipped_no_team += 1
            continue
        key = (season, team)
        bucket = agg.setdefault(key, {
            "SO": 0.0, "PA": 0.0, "H": 0.0, "BB": 0.0,
            "AB": 0.0, "TB": 0.0, "PA_LHB": 0.0,
        })
        for g in (b.get("games") or []):
            try:
                pa = int(g.get("PA") or 0)
                ab = int(g.get("AB") or 0)
                so = int(g.get("SO") or 0)
                bb = int(g.get("BB") or 0)
                h  = int(g.get("H")  or 0)
                tb = int(g.get("TB") or 0)
            except (TypeError, ValueError):
                continue
            bucket["SO"] += so
            bucket["PA"] += pa
            bucket["H"]  += h
            bucket["BB"] += bb
            bucket["AB"] += ab
            bucket["TB"] += tb
            if is_lhb:
                bucket["PA_LHB"] += pa

    if skipped_no_team:
        _log(f"team baselines: skipped {skipped_no_team} batters with no derivable team")
    out: dict[str, dict[str, float]] = {}
    for (season, team), b in agg.items():
        pa = max(b["PA"], 1.0)
        ab = max(b["AB"], 1.0)
        obp = (b["H"] + b["BB"]) / max(b["AB"] + b["BB"], 1.0)
        slg = b["TB"] / ab
        out[f"{season}:{team}"] = {
            "team_k_rate":   round(b["SO"] / pa, 4),
            "team_woba":     round(obp + slg, 4),  # OPS, ~0.96 corr with true wOBA
            "team_lhb_pct":  round(b["PA_LHB"] / pa, 4),
        }
    _log(f"team baselines computed: {len(out)} (season, team) buckets")
    return out


def compute_career_baselines(payload: dict) -> dict[int, dict[str, float]]:
    """Aggregate per-pitcher career baseline stats across all training seasons.

    Returns {pid: {career_k_per_9, career_bb_per_9, career_fip, career_ip}}.

    career_fip uses the classic FIP formula:
        FIP = (13*HR + 3*BB - 2*K) / IP + 3.10
    The constant 3.10 is the modern-era league-FIP intercept.  When the
    pitcher's gameLog cache lacks HR (pre-PR3 cache files), we fall back
    to a BB/9-derived proxy on the same scale (3.10 + (career_bb_per_9 -
    3.30) * 0.6) so feature_fip stays sane without re-fetching.  Newly
    fetched cache rows after PR3 include HR and produce true FIP.
    """
    career: dict[int, dict[str, float]] = {}
    seen_hr: dict[int, bool] = {}
    for p in (payload.get("pitchers") or []):
        pid = int(p.get("id") or 0)
        if not pid:
            continue
        bucket = career.setdefault(
            pid, {"K": 0.0, "BB": 0.0, "HR": 0.0, "IP": 0.0},
        )
        for g in (p.get("games") or []):
            try:
                bucket["K"]  += int(g.get("K")  or 0)
                bucket["BB"] += int(g.get("BB") or 0)
                bucket["IP"] += float(g.get("IP") or 0.0)
            except (TypeError, ValueError):
                continue
            # HR is PR3-new in fetch_pitcher_game_log; only count when
            # explicitly present (None != 0) so we know whether to fall
            # back to the BB/9 proxy for this pitcher.
            hr_raw = g.get("HR")
            if hr_raw is not None:
                try:
                    bucket["HR"] += int(hr_raw)
                    seen_hr[pid] = True
                except (TypeError, ValueError):
                    pass

    out: dict[int, dict[str, float]] = {}
    fip_proxy_count = 0
    fip_true_count  = 0
    for pid, b in career.items():
        ip = max(b["IP"], 0.01)
        career_k_per_9  = round(b["K"]  * 9.0 / ip, 4)
        career_bb_per_9 = round(b["BB"] * 9.0 / ip, 4)
        if seen_hr.get(pid):
            # True FIP with the modern-era 3.10 league constant.
            career_fip = (13 * b["HR"] + 3 * b["BB"] - 2 * b["K"]) / ip + 3.10
            fip_true_count += 1
        else:
            # Proxy: shift around league-avg FIP using BB/9 deviation.
            career_fip = 3.10 + (career_bb_per_9 - 3.30) * 0.6
            fip_proxy_count += 1
        out[pid] = {
            "career_k_per_9":  career_k_per_9,
            "career_bb_per_9": career_bb_per_9,
            "career_fip":      round(float(career_fip), 4),
            "career_ip":       round(b["IP"], 2),
        }
    _log(
        f"career baselines computed: {len(out)} pitchers  "
        f"(true_fip={fip_true_count}, proxy_fip={fip_proxy_count})"
    )
    return out


def _build_pitcher_dataset(
    payload: dict,
    splits_by_season: dict[int, dict[int, dict]],
    team_baselines: Optional[dict[str, dict[str, float]]] = None,
    career_baselines: Optional[dict[int, dict[str, float]]] = None,
):
    """Build (X, y, reg_targets, feature_names, snapshots) for pitcher_strikeouts >= 6 label.

    Also returns a *snapshots* dict {str(player_id): {features..., name, team,
    as_of_date}} keyed by player ID; each entry holds that pitcher's
    most-recent engineered feature row across all training seasons.  The
    inference layer looks these up at predict time so szn_*/r7_*/r14_*
    features carry the pitcher's real recent form instead of the prop
    line as a noisy proxy (which is what the old line-as-proxy heuristic
    in props_model._build_reg_vector did for pitchers).

    Real features (computed from game logs + platoon splits):
        season-to-date and 7/14-game rolling K, BB, H, ER, IP, K/9, BB/9
        days_since_last_start, ip_last_30d (fatigue), ballpark_factor_k,
        is_home, era_vs_lhb, k_rate_vs_lhb, era_vs_rhb, k_rate_vs_rhb

    Opponent context features (per-row, looked up from team_baselines by
    (season, opp_team) — same values reused at inference time):
        opp_team_k_rate, opp_team_woba, opp_team_lhb_pct

    Pitcher career baseline features (per-pitcher, identical for every row
    of the same pid — looked up from career_baselines):
        career_k_per_9, career_bb_per_9, career_ip

    Inference-time placeholder features (zero during training; populated
    at prediction time by the inference layer):
        lineup_avg_k_rate, lineup_lhb_count, lineup_rhb_count,
        weather_temp, weather_wind_speed, weather_wind_dir_num,
        time_of_day, umpire_k_rate, implied_total,
        first_inning_k_pct, pitch_mix_fastball_pct,
        pitch_mix_breaking_pct, pitch_mix_offspeed_pct
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _log("pandas/numpy missing -- aborting pitcher build")
        return None, None, None, None, None, None

    # Inference-time features that are always zero during training
    INFER_FEATS = [
        "lineup_avg_k_rate",
        "lineup_lhb_count",
        "lineup_rhb_count",
        "weather_temp",
        "weather_wind_speed",
        "weather_wind_dir_num",
        "time_of_day",
        "umpire_k_rate",
        "implied_total",
        "first_inning_k_pct",
        "pitch_mix_fastball_pct",
        "pitch_mix_breaking_pct",
        "pitch_mix_offspeed_pct",
    ]

    rows: list[dict] = []
    reg_rows: list[dict] = []
    snapshots: dict[str, dict] = {}
    for p in (payload.get("pitchers") or []):
        games  = p.get("games") or []
        season = p.get("season", 2025)
        pid    = p["id"]
        pname  = p.get("name") or ""
        pteam  = p.get("team") or ""
        if not games:
            continue

        # Platoon splits for this player+season (season-level aggregates)
        season_splits = splits_by_season.get(season, {})
        splits = season_splits.get(pid, {
            "era_vs_lhb": 4.50, "k_rate_vs_lhb": 0.215,
            "era_vs_rhb": 4.50, "k_rate_vs_rhb": 0.215,
        })

        # Career baseline (same value for every row of this pid).  Defaults
        # match league-average modern era: K/9 ~ 8.5, BB/9 ~ 3.3, FIP ~ 4.0,
        # IP small so the model can learn "low IP = high variance" if it wants.
        career = (career_baselines or {}).get(pid, {
            "career_k_per_9":  8.50,
            "career_bb_per_9": 3.30,
            "career_fip":      4.00,
            "career_ip":       50.0,
        })

        df = pd.DataFrame(games).sort_values("date").reset_index(drop=True)
        df["date_dt"] = pd.to_datetime(df["date"], errors="coerce")

        # ── Defensive: ensure park_team exists ────────────────────────────
        # Old cache files (written before park_team was added) lack this column.
        # Derive it: home game → pitcher's own team is the park; away → opponent.
        if "park_team" not in df.columns:
            player_team = p.get("team", "")
            df["park_team"] = df.apply(
                lambda r: player_team if r.get("is_home") else (r.get("opp_team") or ""),
                axis=1,
            )

        # ── Defensive: ensure games_started exists ─────────────────────────
        # Should always be present, but guard against unexpected cache shapes.
        if "games_started" not in df.columns:
            df["games_started"] = 0

        # ── PR3: filter relief outings BEFORE computing rolling stats ──────
        # Pre-PR3 the pitcher dataset included every appearance, so a
        # starter's rolling K/9 was contaminated by 1-IP relief outings
        # from earlier in the season.  Keep only games where the pitcher
        # was the listed starter; the model trains on starter-only rows
        # which is what the props line is set against.
        before_filter = len(df)
        df = df[df["games_started"] > 0].reset_index(drop=True)
        if len(df) < before_filter:
            _log(
                f"  pid={pid} {pname}: dropped {before_filter - len(df)} relief "
                f"outing(s), kept {len(df)} starts"
            )
        if df.empty:
            continue

        # Per-row rate stats (needed for rolling averages)
        df["k_per_9"]  = (df["K"]  * 9.0 / df["IP"].clip(lower=0.01)).fillna(0.0)
        df["bb_per_9"] = (df["BB"] * 9.0 / df["IP"].clip(lower=0.01)).fillna(0.0)

        roll_stats = ["K", "BB", "H", "ER", "IP", "k_per_9", "bb_per_9"]

        # Season-to-date (expanding mean, shift(1) = strictly before this row)
        for c in roll_stats:
            df[f"szn_{c}"] = df[c].shift(1).expanding().mean()
            df[f"r7_{c}"]  = df[c].shift(1).rolling(window=7,  min_periods=2).mean()
            df[f"r14_{c}"] = df[c].shift(1).rolling(window=14, min_periods=3).mean()

        # days_since_last_start: calendar days between consecutive starts.
        # Initialize _prev_start to NaT first so the column always exists even
        # when start_mask is all-False (avoids KeyError on .ffill()).
        df["_prev_start"] = pd.NaT
        start_mask  = df["games_started"] > 0
        start_dates = df.loc[start_mask, "date_dt"]
        if not start_dates.empty:
            df.loc[start_mask, "_prev_start"] = start_dates.shift(1).values
        df["_prev_start"] = df["_prev_start"].ffill()
        df["days_since_last_start"] = (
            (df["date_dt"] - pd.to_datetime(df["_prev_start"]))
            .dt.days.fillna(5)   # neutral default ~5 days rest
            .clip(0, 30)
            .fillna(5)           # second pass in case clip introduced NaN
        )

        # ip_last_30d: total IP pitched in the 30 calendar days BEFORE this game.
        # Use a date-indexed rolling sum (shift so current game excluded).
        df_dated = df.set_index("date_dt").copy()
        df_dated["IP_shifted"] = df_dated["IP"].shift(1).fillna(0)
        # pandas rolling with offset requires sorted DatetimeIndex
        try:
            df["ip_last_30d"] = (
                df_dated["IP_shifted"]
                .rolling("30D", min_periods=0)
                .sum()
                .values
            )
        except Exception:  # noqa: BLE001
            # Fallback: simple 6-game rolling sum (~30 days at typical 5d cadence)
            df["ip_last_30d"] = df["IP"].shift(1).rolling(6, min_periods=0).sum().fillna(0)

        # Ballpark strikeout factor — park_team is guaranteed to exist above
        df["ballpark_factor_k"] = df["park_team"].map(
            lambda t: PARK_FACTORS_K.get(_to_abbrev(t), 1.0)
        )

        # Drop rows that lack rolling averages (first 2 appearances)
        df = df.dropna(subset=[f"szn_{roll_stats[0]}", f"r7_{roll_stats[0]}"])
        if df.empty:
            continue

        df["label"]      = (df["K"] >= 6).astype(int)
        df["is_home_i"]  = df["is_home"].astype(int)

        feat_cols = (
            [f"szn_{c}"  for c in roll_stats]
            + [f"r7_{c}" for c in roll_stats]
            + [f"r14_{c}" for c in roll_stats]
            + [
                "is_home_i",
                "days_since_last_start",
                "ip_last_30d",
                "ballpark_factor_k",
            ]
        )

        # Default opp-team baselines used when the opponent team has no row
        # in team_baselines (e.g. interleague + first appearance, or a team
        # abbrev mismatch).  Tuned to modern-era league averages.
        OPP_DEFAULTS = {
            "opp_team_k_rate":  0.225,
            "opp_team_woba":    0.720,   # league-avg OPS proxy
            "opp_team_lhb_pct": 0.420,
        }
        for _, row in df.iterrows():
            record = {c: float(row[c]) for c in feat_cols}
            # Static platoon splits (same value for all rows of this player+season)
            record["era_vs_lhb"]    = float(splits.get("era_vs_lhb", 4.50))
            record["k_rate_vs_lhb"] = float(splits.get("k_rate_vs_lhb", 0.215))
            record["era_vs_rhb"]    = float(splits.get("era_vs_rhb", 4.50))
            record["k_rate_vs_rhb"] = float(splits.get("k_rate_vs_rhb", 0.215))
            # ── Opponent context (per-row, varies with opp_team) ─────────────
            opp = _to_abbrev(row.get("opp_team") or "")
            opp_key = f"{int(season)}:{opp}"
            opp_stats = (team_baselines or {}).get(opp_key, OPP_DEFAULTS)
            record["opp_team_k_rate"]  = float(opp_stats.get("opp_team_k_rate",
                                              opp_stats.get("team_k_rate", OPP_DEFAULTS["opp_team_k_rate"])))
            record["opp_team_woba"]    = float(opp_stats.get("opp_team_woba",
                                              opp_stats.get("team_woba", OPP_DEFAULTS["opp_team_woba"])))
            record["opp_team_lhb_pct"] = float(opp_stats.get("opp_team_lhb_pct",
                                              opp_stats.get("team_lhb_pct", OPP_DEFAULTS["opp_team_lhb_pct"])))
            # ── Pitcher career baseline (static per pid) ─────────────────────
            record["career_k_per_9"]  = float(career["career_k_per_9"])
            record["career_bb_per_9"] = float(career["career_bb_per_9"])
            # PR3: career_fip computed in compute_career_baselines using
            # true HR when available, BB/9 proxy otherwise.  Same skill
            # axis as career_bb_per_9 but on the 4.00-centered FIP scale.
            record["career_fip"]      = float(career.get("career_fip", 4.00))
            record["career_ip"]       = float(career["career_ip"])
            # Inference-time features — zero during training
            for f in INFER_FEATS:
                record[f] = 0.0
            record["label"]      = int(row["label"])
            # PR3: stamp pid on every row so _train_and_save can pass
            # groups=pitcher_ids to GroupKFold and prevent player-identity
            # leak across folds.  Stripped before X is built (column not
            # in feat_cols).
            record["_player_id"] = int(pid)
            rows.append(record)
            reg_rows.append({
                "K":    float(row["K"]),
                "ER":   float(row["ER"]),
                "H":    float(row["H"]),
                "BB":   float(row["BB"]),
                "outs": float(round(row["IP"] * 3)),
            })

        # ── Snapshot: capture this pitcher's MOST RECENT engineered row ──────
        # The snapshot represents their feature state going into a hypothetical
        # next start.  Inference looks it up by name → MLB ID so szn_*/r7_*/r14_*
        # plus days_since_last_start / ip_last_30d / platoon splits all carry
        # real values instead of the line-as-proxy heuristic.  Last-season wins
        # because collect_multi_season_data appends seasons in order — most
        # recent training season for a given pitcher overwrites earlier ones.
        #
        # NOTE: opp_team_* features are *opponent*-dependent and live in
        # team_baselines.json instead of the per-pitcher snapshot.  Inference
        # looks them up by the current prop's opposing batting team.
        last = df.iloc[-1]
        snap_feats = {c: float(last[c]) for c in feat_cols}
        snap_feats["era_vs_lhb"]    = float(splits.get("era_vs_lhb", 4.50))
        snap_feats["k_rate_vs_lhb"] = float(splits.get("k_rate_vs_lhb", 0.215))
        snap_feats["era_vs_rhb"]    = float(splits.get("era_vs_rhb", 4.50))
        snap_feats["k_rate_vs_rhb"] = float(splits.get("k_rate_vs_rhb", 0.215))
        # Career baselines travel with the pitcher snapshot (stable across games).
        snap_feats["career_k_per_9"]  = float(career["career_k_per_9"])
        snap_feats["career_bb_per_9"] = float(career["career_bb_per_9"])
        snap_feats["career_fip"]      = float(career.get("career_fip", 4.00))
        snap_feats["career_ip"]       = float(career["career_ip"])
        snapshots[str(pid)] = {
            "name":        pname,
            "team":        pteam,
            "season":      int(season),
            "as_of_date":  str(last.get("date", "")),
            "features":    snap_feats,
        }

    if not rows:
        _log("pitcher dataset empty after feature build")
        return None, None, None, None, None, None

    df_all = pd.DataFrame(rows)
    # PR3: _player_id is the GroupKFold grouping key, not a model feature
    # -- exclude it from feat_cols before X is built.
    feature_names = [
        c for c in df_all.columns
        if c not in ("label", "_player_id")
    ]

    # ── Column diagnostic — confirm all expected features are present ──────
    _log(
        f"pitcher feature matrix columns ({len(feature_names)} total): "
        + ", ".join(sorted(feature_names))
    )
    missing_check = [c for c in feature_names if df_all[c].isna().all()]
    if missing_check:
        _log(f"  WARN: all-NaN columns (will be filled with 0): {missing_check}")

    X = df_all[feature_names].fillna(0).to_numpy(dtype=float)
    y = df_all["label"].to_numpy(dtype=int)
    groups = df_all["_player_id"].to_numpy(dtype=int)
    _log(
        f"pitcher features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}  unique_groups={len(set(groups.tolist()))}"
    )
    reg_targets: dict[str, "np.ndarray"] = {
        stat: pd.DataFrame(reg_rows)[stat].to_numpy(dtype=float)
        for stat in _PITCHER_REG_STATS
    }
    _log(f"pitcher snapshots captured: {len(snapshots)} unique pitchers")
    return X, y, reg_targets, feature_names, snapshots, groups


# ---------------------------------------------------------------------------
# Feature engineering — batter
# ---------------------------------------------------------------------------

def _build_batter_dataset(
    payload: dict,
    splits_by_season: dict[int, dict[int, dict]],
    *,
    opp_pitcher_lookup:    Optional[dict[str, int]] = None,
    pitcher_season_stats:  Optional[dict[str, dict[str, float]]] = None,
    pitcher_handedness:    Optional[dict[int, str]] = None,
):
    """Build (X, y, feature_names, snapshots, groups) for batter_hits >= 1 label.

    Also returns a *snapshots* dict {str(player_id): {features..., name, team,
    as_of_date}} keyed by player ID; each entry holds that player's most-recent
    engineered feature row across all training seasons.  The inference layer
    looks up snapshots at predict time so we no longer fill rolling features
    with the prop line as a noisy proxy.

    Real features (computed from game logs + platoon splits):
        season-to-date and 7/14-game rolling H, HR, RBI, R, BB, SO, TB,
        AB, PA, H_per_AB, TB_per_AB, HR_per_AB, BB_per_PA, SO_per_PA,
        k_pct (SO/PA per row), babip_7d, babip_14d, batting_order,
        is_home, ballpark_factor_hits, ballpark_factor_hr,
        ops_vs_lhp, obp_vs_lhp, slg_vs_lhp,
        ops_vs_rhp, obp_vs_rhp, slg_vs_rhp,
        r30_HR, r30_BB (30-game windows for sparse counts)

    PR3 opposing-pitcher context (when *opp_pitcher_lookup* etc. provided):
        opp_pitcher_szn_k_per_9, opp_pitcher_szn_era, opp_pitcher_throws_lhp

    Inference-time placeholder features (zero during training):
        whiff_pct, chase_pct, hard_hit_rate, barrel_rate, sprint_speed,
        platoon_matchup_flag, weather_temp, weather_wind_speed,
        time_of_day, ba_vs_breaking, ba_vs_fastball, ba_vs_offspeed,
        h2h_career_ab, h2h_career_avg, h2h_career_k_rate, implied_total
    """
    # Default to empty dicts so the per-row lookups gracefully return the
    # league-average neutral when no opp pitcher context is wired in.
    opp_pitcher_lookup   = opp_pitcher_lookup   or {}
    pitcher_season_stats = pitcher_season_stats or {}
    pitcher_handedness   = pitcher_handedness   or {}
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _log("pandas/numpy missing -- aborting batter build")
        return None, None, None, None, None, None

    INFER_FEATS = [
        "whiff_pct",
        "chase_pct",
        "hard_hit_rate",
        "barrel_rate",
        "sprint_speed",
        "platoon_matchup_flag",
        "weather_temp",
        "weather_wind_speed",
        "time_of_day",
        "ba_vs_breaking",
        "ba_vs_fastball",
        "ba_vs_offspeed",
        "h2h_career_ab",
        "h2h_career_avg",
        "h2h_career_k_rate",
        "implied_total",
    ]

    rows: list[dict] = []
    reg_rows: list[dict] = []
    # Per-player snapshots: most-recent engineered row across all seasons.
    # Since collect_multi_season_data appends seasons in chronological order,
    # the last assignment wins (= most recent season the player appeared in).
    snapshots: dict[str, dict] = {}
    for p in (payload.get("batters") or []):
        games  = p.get("games") or []
        season = p.get("season", 2025)
        pid    = p["id"]
        pname  = p.get("name", "")
        pteam  = p.get("team", "")
        if not games:
            continue

        season_splits = splits_by_season.get(season, {})
        splits = season_splits.get(pid, {
            "ops_vs_lhp": 0.720, "obp_vs_lhp": 0.315, "slg_vs_lhp": 0.405,
            "ops_vs_rhp": 0.720, "obp_vs_rhp": 0.315, "slg_vs_rhp": 0.405,
        })

        df = pd.DataFrame(games).sort_values("date").reset_index(drop=True)

        # ── Defensive: ensure park_team exists ────────────────────────────
        # Old cache files lack this column; derive it from is_home + opp_team.
        if "park_team" not in df.columns:
            player_team = p.get("team", "")
            df["park_team"] = df.apply(
                lambda r: player_team if r.get("is_home") else (r.get("opp_team") or ""),
                axis=1,
            )

        # ── Defensive: ensure batting_order exists ─────────────────────────
        if "batting_order" not in df.columns:
            df["batting_order"] = 0

        # ── Defensive: ensure opp_team exists (should always, but guard) ───
        if "opp_team" not in df.columns:
            df["opp_team"] = ""

        # Per-row rate stats
        df["H_per_AB"]  = (df["H"]  / df["AB"].clip(lower=1)).fillna(0.0)
        df["TB_per_AB"] = (df["TB"] / df["AB"].clip(lower=1)).fillna(0.0)
        df["HR_per_AB"] = (df["HR"] / df["AB"].clip(lower=1)).fillna(0.0)
        df["BB_per_PA"] = (df["BB"] / df["PA"].clip(lower=1)).fillna(0.0)
        df["SO_per_PA"] = (df["SO"] / df["PA"].clip(lower=1)).fillna(0.0)
        # K% = strikeouts per plate appearance (this specific game, pre-lag)
        df["k_pct"] = df["SO_per_PA"]

        # BABIP approximation: (H - HR) / max(AB - SO - HR, 1)
        # Using shift(1) so we roll it leak-free below
        df["_babip_num"] = (df["H"] - df["HR"]).clip(lower=0)
        df["_babip_den"] = (df["AB"] - df["SO"] - df["HR"]).clip(lower=1)
        df["_babip"]     = df["_babip_num"] / df["_babip_den"]

        roll_stats = [
            "H", "HR", "RBI", "R", "BB", "SO", "TB", "AB", "PA",
            "H_per_AB", "TB_per_AB", "HR_per_AB", "BB_per_PA", "SO_per_PA",
        ]

        # Season-to-date + rolling windows (all leak-free via shift(1))
        for c in roll_stats:
            df[f"szn_{c}"] = df[c].shift(1).expanding().mean()
            df[f"r7_{c}"]  = df[c].shift(1).rolling(window=7,  min_periods=2).mean()
            df[f"r14_{c}"] = df[c].shift(1).rolling(window=14, min_periods=3).mean()

        # PR3: r30 (30-game) rolling windows for sparse count stats.  HR
        # and BB tail-events average ~0.1 / ~0.3 per game so 7- and 14-game
        # rolling means are jumpy on a per-row basis.  A 30-game window
        # smooths the signal toward the player's true level over a 5-6
        # week horizon — adds two features instead of replacing the
        # shorter windows so the model can blend them.
        for c in ("HR", "BB"):
            df[f"r30_{c}"] = df[c].shift(1).rolling(window=30, min_periods=5).mean()

        # BABIP rolling averages (7d and 14d)
        df["babip_7d"]  = df["_babip"].shift(1).rolling(window=7,  min_periods=2).mean()
        df["babip_14d"] = df["_babip"].shift(1).rolling(window=14, min_periods=3).mean()

        # K% rolling averages (7d and 14d) for trend feature
        df["k_pct_7d"]  = df["k_pct"].shift(1).rolling(window=7,  min_periods=2).mean()
        df["k_pct_14d"] = df["k_pct"].shift(1).rolling(window=14, min_periods=3).mean()

        # Ballpark factors — park_team is guaranteed to exist above
        df["ballpark_factor_hits"] = df["park_team"].map(
            lambda t: PARK_FACTORS_H.get(_to_abbrev(t), 1.0)
        )
        df["ballpark_factor_hr"] = df["park_team"].map(
            lambda t: PARK_FACTORS_HR.get(_to_abbrev(t), 1.0)
        )

        # ── PR3: per-row opposing-pitcher context ──────────────────────────
        # Lookup key is (game_date, batter's_own_team) because that's how
        # build_opp_pitcher_lookup indexed pitcher game logs (the pitcher's
        # opp_team IS the batter's team).  Falls back to league averages
        # when the lookup misses (e.g. opener / bullpen game where the
        # cached "starter" row is ambiguous).
        pteam_abbrev = _to_abbrev(pteam)

        def _opp_pid_for_row(d) -> int:
            if not pteam_abbrev or not d:
                return 0
            key = f"{str(d)[:10]}:{pteam_abbrev}"
            return int(opp_pitcher_lookup.get(key, 0))

        df["_opp_pitcher_id"] = df["date"].map(_opp_pid_for_row)

        def _opp_k9(pid_val) -> float:
            pid_int = int(pid_val) if pid_val else 0
            if pid_int <= 0:
                return 8.50  # league-average neutral
            stats = pitcher_season_stats.get(f"{season}:{pid_int}")
            if not stats:
                return 8.50
            return float(stats.get("k_per_9") or 8.50)

        def _opp_era(pid_val) -> float:
            pid_int = int(pid_val) if pid_val else 0
            if pid_int <= 0:
                return 4.30
            stats = pitcher_season_stats.get(f"{season}:{pid_int}")
            if not stats:
                return 4.30
            return float(stats.get("era") or 4.30)

        def _opp_throws_lhp(pid_val) -> float:
            pid_int = int(pid_val) if pid_val else 0
            if pid_int <= 0:
                return 0.30  # ~30% of MLB starters are LHP
            return 1.0 if pitcher_handedness.get(pid_int) == "L" else 0.0

        df["opp_pitcher_szn_k_per_9"] = df["_opp_pitcher_id"].map(_opp_k9)
        df["opp_pitcher_szn_era"]    = df["_opp_pitcher_id"].map(_opp_era)
        df["opp_pitcher_throws_lhp"] = df["_opp_pitcher_id"].map(_opp_throws_lhp)

        df = df.dropna(subset=[f"szn_{roll_stats[0]}", f"r7_{roll_stats[0]}"])
        if df.empty:
            continue

        df["label"]     = (df["H"] >= 1).astype(int)
        df["is_home_i"] = df["is_home"].astype(int)

        feat_cols = (
            [f"szn_{c}"  for c in roll_stats]
            + [f"r7_{c}" for c in roll_stats]
            + [f"r14_{c}" for c in roll_stats]
            # PR3: 30-game smoothing for the two sparse count stats.
            + ["r30_HR", "r30_BB"]
            + [
                "k_pct_7d", "k_pct_14d",
                "babip_7d", "babip_14d",
                "batting_order",
                "is_home_i",
                "ballpark_factor_hits",
                "ballpark_factor_hr",
                # PR3: opposing-pitcher context, looked up per (date, batter_team).
                "opp_pitcher_szn_k_per_9",
                "opp_pitcher_szn_era",
                "opp_pitcher_throws_lhp",
            ]
        )

        for _, row in df.iterrows():
            record = {c: float(row[c]) for c in feat_cols}
            # Static platoon splits
            record["ops_vs_lhp"] = float(splits.get("ops_vs_lhp", 0.720))
            record["obp_vs_lhp"] = float(splits.get("obp_vs_lhp", 0.315))
            record["slg_vs_lhp"] = float(splits.get("slg_vs_lhp", 0.405))
            record["ops_vs_rhp"] = float(splits.get("ops_vs_rhp", 0.720))
            record["obp_vs_rhp"] = float(splits.get("obp_vs_rhp", 0.315))
            record["slg_vs_rhp"] = float(splits.get("slg_vs_rhp", 0.405))
            # Inference-time features — zero during training
            for f in INFER_FEATS:
                record[f] = 0.0
            record["label"]      = int(row["label"])
            # PR3: stamp pid on every batter row so _train_and_save can pass
            # groups=batter_ids to GroupKFold.  Stripped from feat_cols below.
            record["_player_id"] = int(pid)
            rows.append(record)
            reg_rows.append({
                "H":   float(row["H"]),
                "TB":  float(row["TB"]),
                "HR":  float(row["HR"]),
                "RBI": float(row["RBI"]),
                "R":   float(row["R"]),
                "BB":  float(row["BB"]),
            })

        # ── Snapshot: capture this player's MOST RECENT engineered row ───────
        # The snapshot represents their feature state going into a hypothetical
        # next game and is what the inference layer needs.  Last-season wins
        # because collect_multi_season_data appends seasons in order.
        last = df.iloc[-1]
        snap_feats = {c: float(last[c]) for c in feat_cols}
        snap_feats["ops_vs_lhp"] = float(splits.get("ops_vs_lhp", 0.720))
        snap_feats["obp_vs_lhp"] = float(splits.get("obp_vs_lhp", 0.315))
        snap_feats["slg_vs_lhp"] = float(splits.get("slg_vs_lhp", 0.405))
        snap_feats["ops_vs_rhp"] = float(splits.get("ops_vs_rhp", 0.720))
        snap_feats["obp_vs_rhp"] = float(splits.get("obp_vs_rhp", 0.315))
        snap_feats["slg_vs_rhp"] = float(splits.get("slg_vs_rhp", 0.405))
        snapshots[str(pid)] = {
            "name":        pname,
            "team":        pteam,
            "season":      int(season),
            "as_of_date":  str(last.get("date", "")),
            "features":    snap_feats,
        }

    if not rows:
        _log("batter dataset empty after feature build")
        return None, None, None, None, None, None

    df_all = pd.DataFrame(rows)
    # PR3: _player_id is the GroupKFold grouping key, not a model feature.
    feature_names = [
        c for c in df_all.columns
        if c not in ("label", "_player_id")
    ]

    # ── Column diagnostic — confirm all expected features are present ──────
    _log(
        f"batter feature matrix columns ({len(feature_names)} total): "
        + ", ".join(sorted(feature_names))
    )
    missing_check = [c for c in feature_names if df_all[c].isna().all()]
    if missing_check:
        _log(f"  WARN: all-NaN columns (will be filled with 0): {missing_check}")

    X = df_all[feature_names].fillna(0).to_numpy(dtype=float)
    y = df_all["label"].to_numpy(dtype=int)
    groups = df_all["_player_id"].to_numpy(dtype=int)
    _log(
        f"batter features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}  unique_groups={len(set(groups.tolist()))}"
    )
    reg_targets: dict[str, "np.ndarray"] = {
        stat: pd.DataFrame(reg_rows)[stat].to_numpy(dtype=float)
        for stat in _BATTER_REG_STATS
    }
    _log(f"batter snapshots captured: {len(snapshots)} unique players")
    return X, y, reg_targets, feature_names, snapshots, groups


# ---------------------------------------------------------------------------
# SHAP feature importance
# ---------------------------------------------------------------------------

def _run_shap_analysis(
    model,
    X,
    feature_names: list[str],
    label: str,
) -> dict[str, float]:
    """Compute SHAP TreeExplainer importances, log top 15, return full dict.

    Returns {feature_name: mean_abs_shap_value}.
    """
    try:
        import shap
        import numpy as np
    except ImportError:
        _log(f"shap not installed -- skipping SHAP analysis for {label}")
        return {}

    _log(f"SHAP analysis: computing TreeExplainer for {label} model …")
    try:
        # Use a subsample for speed when dataset is large (>10k rows)
        X_sample = X
        if len(X) > 10_000:
            rng = np.random.default_rng(42)
            idx = rng.choice(len(X), size=10_000, replace=False)
            X_sample = X[idx]
            _log(f"  SHAP: subsampled {len(X)} → 10,000 rows for speed")

        explainer   = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(X_sample)

        # shap_values is 2-D (n_samples × n_features) for binary XGBoost.
        # Cast to Python float immediately — numpy float32 is not JSON-
        # serializable and would crash _save_feature_importance.
        mean_abs = {
            k: float(v)
            for k, v in zip(feature_names, abs(shap_values).mean(axis=0))
        }

        top15 = sorted(mean_abs.items(), key=lambda kv: -kv[1])[:15]
        _log(f"SHAP top-15 features [{label}]:")
        for rank, (feat, val) in enumerate(top15, 1):
            _log(f"  {rank:2d}. {feat:<40s} {val:.5f}")

        return mean_abs
    except Exception as exc:  # noqa: BLE001
        _log(f"SHAP analysis failed ({type(exc).__name__}: {exc}) -- skipping")
        return {}


def _save_feature_importance(
    pitcher_importance: dict[str, float],
    batter_importance:  dict[str, float],
) -> None:
    """Save feature importance dicts to .cache/props_feature_importance.json."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _CACHE_DIR / "props_feature_importance.json"

    def _top_n(d: dict, n: int = 30) -> list[dict]:
        return [
            {"feature": k, "mean_abs_shap": round(v, 6)}
            for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]
        ]

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pitcher": {
            "top_30": _top_n(pitcher_importance),
            "all":    {k: round(v, 6) for k, v in sorted(
                pitcher_importance.items(), key=lambda kv: -kv[1])},
        },
        "batter": {
            "top_30": _top_n(batter_importance),
            "all":    {k: round(v, 6) for k, v in sorted(
                batter_importance.items(), key=lambda kv: -kv[1])},
        },
    }
    try:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"feature importance saved: {out_path}")
    except Exception as exc:  # noqa: BLE001
        _log(f"feature importance save failed: {exc}")


def _save_reg_metadata(
    pitcher_feature_names: list[str],
    batter_feature_names: list[str],
) -> None:
    """Save feature name lists so the inference layer can build correct-length
    zero vectors without importing the training data at runtime."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _CACHE_DIR / "props_reg_metadata.json"
    payload = {
        "generated_at":          datetime.utcnow().isoformat() + "Z",
        "pitcher_feature_names": pitcher_feature_names,
        "batter_feature_names":  batter_feature_names,
    }
    try:
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"regression metadata saved: {out_path}")
    except Exception as exc:  # noqa: BLE001
        _log(f"regression metadata save failed: {exc}")


# ---------------------------------------------------------------------------
# Train + save
# ---------------------------------------------------------------------------

def _train_and_save(
    X,
    y,
    out_path: Path,
    *,
    label: str,
    groups=None,
) -> tuple[Optional[float], Optional[object]]:
    """5-fold CV XGBoost training + isotonic calibration on a 20% holdout.

    Returns (oof_accuracy, base_classifier).  The *saved* artifact is the
    CalibratedClassifierCV wrapper; the returned model is the underlying
    base XGB tree so SHAP TreeExplainer can introspect it (TreeExplainer
    can't traverse the wrapper).

    PR2 change: previously calibrated with cv='prefit' on the SAME X, y
    used to fit the base classifier (acknowledged in the source as
    "optimistic but directional").  Now uses an explicit 20% stratified
    holdout — base fits on 80%, isotonic regression fits on the unseen
    20% — so the post-calibration Brier score is an honest test-set
    number, not a self-graded one.

    PR3 change: when *groups* is provided, switch from StratifiedKFold
    (shuffle=True) to GroupKFold so the same player_id never lands in
    both train and test folds.  Previous shuffle-only splits leaked
    player identity across folds — a pitcher's first 8 starts could
    train the fold that scored their 9th, inflating OOF accuracy.
    The 80/20 calibration split also switches to GroupShuffleSplit so
    base + calibration cohorts stay disjoint at the player level.
    """
    if X is None or y is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) "
             "-- skipping train")
        return None, None
    try:
        from sklearn.model_selection import (
            StratifiedKFold, GroupKFold, GroupShuffleSplit, train_test_split,
        )
        from sklearn.metrics         import accuracy_score, log_loss
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        return None, None

    _xgb_kwargs = dict(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.75,
        min_child_weight=3, gamma=0.1,
        objective="binary:logistic", eval_metric="logloss",
        use_label_encoder=False, verbosity=0,
    )

    # ── OOF k-fold on full X, y — honest generalisation measure ───────────
    # When groups is provided we switch to GroupKFold so all rows from the
    # same player land in the same fold (no identity leak between train
    # and test).  Falls back to StratifiedKFold(shuffle=True) when groups
    # is None so callers that don't track player identity still work.
    groups_arr = None
    if groups is not None:
        try:
            groups_arr = np.asarray(groups)
            if groups_arr.shape[0] != len(y):
                _log(f"{label}: groups length mismatch "
                     f"({groups_arr.shape[0]} vs {len(y)}) -- ignoring")
                groups_arr = None
        except Exception:                                                  # noqa: BLE001
            groups_arr = None

    if groups_arr is not None:
        unique_groups = int(len(set(groups_arr.tolist())))
        _log(f"{label} using GroupKFold (n_splits=5)  unique_groups={unique_groups}")
        splitter = GroupKFold(n_splits=5)
        split_iter = splitter.split(X, y, groups=groups_arr)
    else:
        _log(f"{label} using StratifiedKFold (n_splits=5, shuffle=True) "
             "-- no groups provided")
        splitter = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        split_iter = splitter.split(X, y)

    oof = np.zeros(len(y))
    for fold, (tr, te) in enumerate(split_iter, 1):
        clf = xgb.XGBClassifier(**_xgb_kwargs)
        clf.fit(X[tr], y[tr])
        proba        = clf.predict_proba(X[te])[:, 1]
        oof[te]      = proba
        fold_acc     = accuracy_score(y[te], (proba >= 0.5).astype(int))
        fold_ll      = log_loss(y[te], proba, labels=[0, 1])
        _log(f"{label} fold {fold}: acc={fold_acc:.3f}  log_loss={fold_ll:.3f}")

    oof_acc = accuracy_score(y, (oof >= 0.5).astype(int))
    oof_ll  = log_loss(y, oof, labels=[0, 1])
    _log(f"{label} OOF: acc={oof_acc:.3f}  log_loss={oof_ll:.3f}")

    # ── 80/20 split: base trains on 80%, isotonic fits on 20%.
    # When groups is provided, GroupShuffleSplit keeps players whole so
    # the calibration cohort is fully unseen by the base classifier.
    if groups_arr is not None:
        gss = GroupShuffleSplit(n_splits=1, test_size=0.20, random_state=42)
        tr_idx, cal_idx = next(gss.split(X, y, groups=groups_arr))
        X_train, X_cal = X[tr_idx], X[cal_idx]
        y_train, y_cal = y[tr_idx], y[cal_idx]
        _log(
            f"{label} split: base={len(X_train)} rows ({len(set(groups_arr[tr_idx].tolist()))} players), "
            f"calibration={len(X_cal)} rows ({len(set(groups_arr[cal_idx].tolist()))} players)  "
            f"-- GroupShuffleSplit (no player overlap)"
        )
    else:
        X_train, X_cal, y_train, y_cal = train_test_split(
            X, y, test_size=0.20, random_state=42, stratify=y,
        )
        _log(
            f"{label} split: base={len(X_train)} rows, calibration={len(X_cal)} rows "
            f"(positive_rate train={y_train.mean():.3f}  cal={y_cal.mean():.3f})"
        )

    base = xgb.XGBClassifier(**_xgb_kwargs)
    base.fit(X_train, y_train)

    # ── Isotonic calibration on the held-out 20% ──────────────────────────
    # We wrap `base` in FrozenEstimator so CalibratedClassifierCV.fit() only
    # fits the isotonic map — the base classifier's weights aren't touched.
    # sklearn 1.6+ removed the legacy `cv='prefit'` argument in favour of
    # this pattern (the cv= arg is now strictly for k-fold ints / splitters).
    # Base and calibrator see disjoint data, so the Brier delta below reads
    # honestly as a test-set number.
    try:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.frozen      import FrozenEstimator
        from sklearn.metrics     import brier_score_loss

        brier_pre_holdout = brier_score_loss(
            y_cal, base.predict_proba(X_cal)[:, 1],
        )
        _log(f"{label} pre-calibration Brier (20% holdout): {brier_pre_holdout:.4f}")

        calibrated = CalibratedClassifierCV(
            FrozenEstimator(base), method="isotonic",
        )
        calibrated.fit(X_cal, y_cal)

        brier_post_holdout = brier_score_loss(
            y_cal, calibrated.predict_proba(X_cal)[:, 1],
        )
        _log(f"{label} post-calibration Brier (20% holdout): {brier_post_holdout:.4f}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(calibrated, out_path)
        _log(f"{label} calibrated model saved: {out_path}")
        # Return base for SHAP; calibrated is what landed on disk.
        return float(oof_acc), base
    except Exception as cal_exc:                                            # noqa: BLE001
        _log(f"{label} calibration failed ({cal_exc}) -- saving uncalibrated base")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(base, out_path)
        _log(f"{label} model saved (uncalibrated): {out_path}")
        return float(oof_acc), base


def _train_regressor(
    X,
    y_reg,
    out_path: Path,
    *,
    label: str,
    objective: str = "reg:squarederror",
    groups=None,
) -> tuple[Optional[float], Optional[object]]:
    """5-fold CV XGBoost regression training.  Returns (oof_rmse, final_model).

    *objective* defaults to ``reg:squarederror``.  For non-negative count
    targets (batter H/TB/HR/RBI/R/BB, pitcher K/ER/H/BB/outs) ``count:poisson``
    is principled and typically yields better-calibrated, lower-RMSE
    predictions on sparse counts like HR.

    *groups* (PR3): when provided, switches the CV splitter from KFold
    (shuffle=True) to GroupKFold so the same player_id never lands in both
    a fold's train and test partitions.  Without this, the regressor was
    memorising player skill — the OOF RMSE looked good because folds were
    full of the same names.  GroupKFold collapses that leak; OOF RMSE
    rises slightly but is now an honest generalisation estimate.
    """
    if X is None or y_reg is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) "
             "-- skipping regressor train")
        return None, None
    try:
        from sklearn.model_selection import KFold, GroupKFold
        from sklearn.metrics         import mean_squared_error
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        return None, None

    # PR3: GroupKFold when groups provided, else legacy shuffle KFold.
    groups_arr = None
    if groups is not None:
        try:
            groups_arr = np.asarray(groups)
            if groups_arr.shape[0] != len(y_reg):
                _log(f"{label}: groups length mismatch "
                     f"({groups_arr.shape[0]} vs {len(y_reg)}) -- ignoring")
                groups_arr = None
        except Exception:                                                   # noqa: BLE001
            groups_arr = None

    if groups_arr is not None:
        unique_groups = int(len(set(groups_arr.tolist())))
        _log(f"{label} using GroupKFold (n_splits=5)  unique_groups={unique_groups}")
        splitter = GroupKFold(n_splits=5)
        split_iter = splitter.split(X, y_reg, groups=groups_arr)
    else:
        _log(f"{label} using KFold (n_splits=5, shuffle=True) -- no groups provided")
        splitter = KFold(n_splits=5, shuffle=True, random_state=42)
        split_iter = splitter.split(X)

    oof  = np.zeros(len(y_reg))
    for fold, (tr, te) in enumerate(split_iter, 1):
        reg = xgb.XGBRegressor(
            n_estimators=400, max_depth=4, learning_rate=0.04,
            subsample=0.8, colsample_bytree=0.75,
            min_child_weight=3, gamma=0.1,
            objective=objective, eval_metric="rmse",
            verbosity=0,
        )
        reg.fit(X[tr], y_reg[tr])
        oof[te]   = reg.predict(X[te])
        fold_rmse = float(np.sqrt(mean_squared_error(y_reg[te], oof[te])))
        _log(f"{label} fold {fold}: rmse={fold_rmse:.3f}")

    oof_rmse = float(np.sqrt(mean_squared_error(y_reg, oof)))
    _log(f"{label} OOF: rmse={oof_rmse:.3f}  objective={objective}")

    final = xgb.XGBRegressor(
        n_estimators=400, max_depth=4, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.75,
        min_child_weight=3, gamma=0.1,
        objective=objective, eval_metric="rmse",
        verbosity=0,
    )
    final.fit(X, y_reg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, out_path)
    _log(f"{label} regressor saved: {out_path}")
    return oof_rmse, final


# ---------------------------------------------------------------------------
# Supabase push (env-var driven, dotenv-aware)
# ---------------------------------------------------------------------------

def _sanitize_supabase_url(raw: str) -> str:
    """Strip path / query / fragment from a Supabase project URL.

    supabase-py builds REST paths by concatenating '/rest/v1' onto the URL
    we pass in.  A trailing slash (or any path component) produces a double-
    slash like 'https://x.supabase.co//rest/v1/app_cache', which PostgREST
    rejects with PGRST125.  This matches the sanitization in src/db.py.
    """
    from urllib.parse import urlparse
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw


def _push_to_supabase_direct(model_pairs: list[tuple[Path, str]]) -> str:
    """Push trained model files directly to Supabase app_cache table.

    Uses the same supabase-py create_client pattern as src/db.py.
    Reads SUPABASE_URL + SUPABASE_KEY from environment; loads .env via
    python-dotenv when present so local runs work without Railway env vars.

    Returns a short status string for the training summary log.
    """
    # ── Load .env if python-dotenv is available ──────────────────────────
    try:
        from dotenv import load_dotenv
        here = Path(__file__).resolve()
        for parent in [here.parent, here.parent.parent, here.parent.parent.parent]:
            env_file = parent / ".env"
            if env_file.exists():
                load_dotenv(env_file)
                _log(f"dotenv: loaded {env_file}")
                break
    except ImportError:
        pass  # python-dotenv is optional

    url_raw = os.environ.get("SUPABASE_URL", "").strip()
    key     = os.environ.get("SUPABASE_KEY", "").strip()

    if not url_raw or not key:
        _log("Supabase: SUPABASE_URL or SUPABASE_KEY not set -- skipping push")
        return "skipped (no env vars)"

    # Sanitize URL before passing to create_client (strips trailing slash /
    # path components that cause PostgREST double-slash errors).
    url = _sanitize_supabase_url(url_raw)
    if url != url_raw:
        _log(f"Supabase: URL sanitized: {url_raw!r} -> {url!r}")

    # ── Import supabase-py (same pattern as src/db.py) ───────────────────
    # postgrest-py is a required dependency of supabase>=2.0.0.  If this
    # import raises ModuleNotFoundError it means requirements.txt was not
    # fully installed — postgrest is now pinned explicitly to prevent this.
    try:
        import base64
        from supabase import create_client   # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        _log(
            f"Supabase: import failed ({exc}).  "
            "Ensure 'supabase' and 'postgrest' are in requirements.txt "
            "and the venv is up to date -- skipping push"
        )
        return f"skipped (import error: {exc})"

    # ── Connect ──────────────────────────────────────────────────────────
    try:
        sb = create_client(url, key)
    except Exception as exc:  # noqa: BLE001
        _log(f"Supabase: create_client failed ({exc}) -- skipping push")
        return f"error: create_client: {exc}"

    # ── Upsert each model file into app_cache ─────────────────────────────
    results = []
    for path, cache_key in model_pairs:
        if not path.exists():
            _log(f"Supabase: {path} not found -- skipping")
            results.append(f"{cache_key}: missing")
            continue
        try:
            encoded = base64.b64encode(path.read_bytes()).decode()
            # on_conflict="key" matches the app_cache table primary key,
            # consistent with how src/db.py writes to this table.
            sb.table("app_cache").upsert(
                {
                    "key":        cache_key,
                    "value":      encoded,
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                },
                on_conflict="key",
            ).execute()
            _log(f"Supabase: pushed {cache_key} ({len(encoded) // 1024} KB)")
            results.append(f"{cache_key}: ok")
        except Exception as exc:  # noqa: BLE001
            _log(f"Supabase: push {cache_key} failed: {exc}")
            results.append(f"{cache_key}: error({type(exc).__name__}: {exc})")

    status = "  ".join(results)
    all_ok = all(r.endswith(": ok") for r in results)
    _log(f"Supabase push complete: {status}")
    return status if not all_ok else "ok"


# ---------------------------------------------------------------------------
# PR3: backtest harness invocation (pre/post comparison)
# ---------------------------------------------------------------------------

def _run_backtest_snapshot(*, label: str) -> dict:
    """Shell out to scripts/backtest_props_model.py and return the
    summary it writes to .cache/backtest_<timestamp>_<label>.json.

    Run as a subprocess so the backtest's imports of src.props_model
    pick up the joblibs currently on disk (the in-process module cache
    inside this script would still hold pre-overwrite state).

    Returns {} on any failure -- the comparison helper renders 'n/a'
    instead of crashing the train run.
    """
    import subprocess
    out_path = _CACHE_DIR / f"backtest_{label}.json"
    cmd = [
        sys.executable,
        str(Path(__file__).resolve().parent / "backtest_props_model.py"),
        "--label", label,
        "--no-baseline-write",
        "--output-json", str(out_path),
    ]
    _log(f"=== BACKTEST {label} ===  cmd={' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
        # Pipe the subprocess's stderr through ours so the PROPS-SETTLE /
        # backtest lines surface in the same Railway log stream.
        if proc.stderr:
            for line in proc.stderr.splitlines():
                print(line, flush=True, file=sys.stderr)
        if proc.returncode != 0:
            _log(f"backtest {label}: exited {proc.returncode} -- treating as empty")
            return {}
    except subprocess.TimeoutExpired:
        _log(f"backtest {label}: TIMEOUT after 600s -- treating as empty")
        return {}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"backtest {label}: subprocess crashed {type(exc).__name__}: {exc}")
        return {}

    if not out_path.exists():
        _log(f"backtest {label}: expected output {out_path} missing")
        return {}
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"backtest {label}: result parse failed: {exc}")
        return {}


def _print_backtest_delta(pre: Optional[dict], post: Optional[dict]) -> None:
    """Render a per-market table comparing pre/post hit rate + MAE + RMSE.

    Both inputs are the JSON shape backtest_props_model.py emits:
        {per_market: {<market>: {n, hit_rate, mae, rmse, ...}}, ...}
    Missing fields render as 'n/a' so a partial snapshot doesn't crash
    the report.
    """
    if not pre and not post:
        _log("=== BACKTEST DELTA ===  both snapshots empty -- nothing to compare")
        return
    pre  = pre  or {}
    post = post or {}
    pre_pm  = (pre.get("per_market")  or {}) if isinstance(pre,  dict) else {}
    post_pm = (post.get("per_market") or {}) if isinstance(post, dict) else {}
    all_markets = sorted(set(pre_pm.keys()) | set(post_pm.keys()))

    _log("=== BACKTEST DELTA (PR2 baseline -> PR3) ===")
    _log(f"  {'market':<28}  "
         f"{'n_pre':>6}  {'n_post':>6}  "
         f"{'hit_pre':>8}  {'hit_post':>8}  {'Δ_hit':>7}  "
         f"{'mae_pre':>8}  {'mae_post':>8}  {'Δ_mae':>7}  "
         f"{'rmse_pre':>9}  {'rmse_post':>9}  {'Δ_rmse':>8}")

    def _f(value, spec="0.3f") -> str:
        if value is None:
            return "n/a"
        try:
            return format(float(value), spec)
        except (TypeError, ValueError):
            return "n/a"

    def _delta(a, b, spec="+0.3f") -> str:
        try:
            return format(float(b) - float(a), spec)
        except (TypeError, ValueError):
            return "n/a"

    for m in all_markets:
        a = pre_pm.get(m, {}) or {}
        b = post_pm.get(m, {}) or {}
        _log(
            f"  {m:<28}  "
            f"{str(a.get('n_bets_scored', a.get('n', 'n/a'))):>6}  "
            f"{str(b.get('n_bets_scored', b.get('n', 'n/a'))):>6}  "
            f"{_f(a.get('hit_rate')):>8}  {_f(b.get('hit_rate')):>8}  "
            f"{_delta(a.get('hit_rate'), b.get('hit_rate')):>7}  "
            f"{_f(a.get('mae')):>8}  {_f(b.get('mae')):>8}  "
            f"{_delta(a.get('mae'), b.get('mae'), '+0.3f'):>7}  "
            f"{_f(a.get('rmse'), '0.4f'):>9}  {_f(b.get('rmse'), '0.4f'):>9}  "
            f"{_delta(a.get('rmse'), b.get('rmse'), '+0.4f'):>8}"
        )


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Train MLB player-prop models (pitcher + batter) "
                    "across multiple seasons.",
    )
    ap.add_argument(
        "--seasons", nargs="+", type=int, default=TRAINING_SEASONS,
        help="Seasons to train on (default: 2023 2024 2025)",
    )
    ap.add_argument(
        "--season", type=int, default=None,
        help="Single-season shortcut (overrides --seasons)",
    )
    ap.add_argument(
        "--refresh-data", action="store_true",
        help="Ignore all cached training data and re-fetch from MLB Stats API",
    )
    ap.add_argument("--skip-pitcher", action="store_true")
    ap.add_argument("--skip-batter",  action="store_true")
    ap.add_argument(
        "--no-push", action="store_true",
        help="Skip Supabase upload",
    )
    ap.add_argument(
        "--no-shap", action="store_true",
        help="Skip SHAP feature importance analysis (saves ~30s)",
    )
    ap.add_argument(
        "--run-backtest", action="store_true",
        help="PR3: run the backtest harness BEFORE retraining (captures the "
             "existing-joblib baseline) AND AFTER retraining (captures the "
             "new joblibs).  Writes both snapshots into .cache/ and prints a "
             "per-stat / per-market delta report to stderr.",
    )
    args = ap.parse_args()

    # Single-season shortcut
    seasons = [args.season] if args.season else args.seasons

    started = time.monotonic()
    _log(f"=== PROPS MODEL TRAINING ===  seasons={seasons}")
    summary: dict = {"seasons": seasons}

    # PR3: pre-training backtest snapshot (captures the existing-joblib
    # PR2 baseline before this run overwrites the .cache/*.joblib files).
    if args.run_backtest:
        summary["backtest_pre"] = _run_backtest_snapshot(label="PR3-pre")

    # ── 1. Collect game logs (multi-season, cached per season) ──────────────
    payload = collect_multi_season_data(seasons, refresh=args.refresh_data)

    # ── 2. Collect platoon splits per season (separate cache) ───────────────
    # We need splits keyed by season then by player_id for lookup during
    # feature engineering.
    _log("collecting platoon splits for pitchers …")
    pitcher_splits_by_season: dict[int, dict[int, dict]] = {}
    batter_splits_by_season:  dict[int, dict[int, dict]] = {}

    for season in seasons:
        # Gather player lists for this season
        season_pitchers = [
            {"id": p["id"]} for p in payload["pitchers"] if p.get("season") == season
        ]
        season_batters = [
            {"id": p["id"]} for p in payload["batters"] if p.get("season") == season
        ]
        if not args.skip_pitcher:
            pitcher_splits_by_season[season] = collect_platoon_splits(
                season_pitchers, season,
                is_pitcher=True,
                refresh=args.refresh_data,
            )
        if not args.skip_batter:
            batter_splits_by_season[season] = collect_platoon_splits(
                season_batters, season,
                is_pitcher=False,
                refresh=args.refresh_data,
            )

    # ── 2b. Opponent context + career baselines (PR #2) ─────────────────────
    # Built once across the full payload and shared by every pitcher row.
    # team_baselines is also persisted to disk so inference can look up the
    # opposing batting team's k_rate / woba / lhb_pct at predict time.
    team_baselines:   dict[str, dict[str, float]] = {}
    career_baselines: dict[int, dict[str, float]] = {}
    if not args.skip_pitcher:
        handedness    = collect_batter_handedness(payload, refresh=args.refresh_data)
        batter_teams  = collect_batter_teams(seasons, refresh=args.refresh_data)
        team_baselines   = compute_team_baselines(payload, handedness, batter_teams=batter_teams)
        career_baselines = compute_career_baselines(payload)
        try:
            _TEAM_BASELINES_PATH.write_text(
                json.dumps({
                    "generated_at":   datetime.utcnow().isoformat() + "Z",
                    "team_baselines": team_baselines,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _log(f"team baselines saved: {_TEAM_BASELINES_PATH} "
                 f"({len(team_baselines)} buckets)")
            summary["team_baselines_saved"] = True
        except Exception as exc:  # noqa: BLE001
            _log(f"team baselines save failed: {exc}")

    # ── 3. Feature engineering + training ───────────────────────────────────
    pitcher_importance: dict[str, float] = {}
    batter_importance:  dict[str, float] = {}
    pitcher_feat_names: list[str] = []
    batter_feat_names:  list[str] = []

    pitcher_path = Path(".cache/props_model_pitcher.joblib")
    batter_path  = Path(".cache/props_model_batter.joblib")

    pitcher_snapshots: dict[str, dict] = {}
    pitcher_groups = None
    if not args.skip_pitcher:
        _log("building pitcher feature matrix …")
        X, y, reg_targets_p, feat_names, pitcher_snapshots, pitcher_groups = (
            _build_pitcher_dataset(
                payload, pitcher_splits_by_season,
                team_baselines=team_baselines,
                career_baselines=career_baselines,
            )
        )
        # PR3: pass groups=pitcher_ids so _train_and_save uses GroupKFold,
        # keeping each pitcher's starts in a single fold.
        acc, model = _train_and_save(
            X, y, pitcher_path, label="pitcher", groups=pitcher_groups,
        )
        summary["pitcher_oof_acc"] = acc
        summary["pitcher_features"] = len(feat_names) if feat_names else 0
        summary["pitcher_snapshots"] = len(pitcher_snapshots or {})
        if model is not None and not args.no_shap and feat_names is not None:
            pitcher_importance = _run_shap_analysis(model, X, feat_names, "pitcher")
        if feat_names:
            pitcher_feat_names = feat_names
        if X is not None and reg_targets_p:
            # Count-valued targets (K, BB, ER, H) use Poisson objective —
            # principled for non-negative counts and typically yields better-
            # calibrated, lower-RMSE predictions on sparse counts than the
            # default squared-error objective.  `outs` is left on squared-error:
            # its distribution is multimodal (clustered around 15, 18, 21 outs
            # = 5, 6, 7 IP) rather than Poisson-shaped, and Poisson fit makes
            # it worse in our holdout tests.
            for stat in _PITCHER_REG_STATS:
                y_reg = reg_targets_p.get(stat)
                if y_reg is None:
                    continue
                reg_path = _CACHE_DIR / f"props_model_pitcher_reg_{stat}.joblib"
                obj = "count:poisson" if stat in ("K", "BB", "ER", "H") else "reg:squarederror"
                # PR3: pass groups so the regressor CV also uses GroupKFold.
                # Without this, the regressor was the leakier of the two
                # learners — same player's starts scattered across folds let
                # XGB memorise per-pitcher means and inflated OOF RMSE.
                rmse, _ = _train_regressor(
                    X, y_reg, reg_path,
                    label=f"pitcher_reg_{stat}",
                    objective=obj,
                    groups=pitcher_groups,
                )
                summary[f"pitcher_reg_{stat}_rmse"] = rmse

    batter_snapshots: dict[str, dict] = {}
    batter_groups = None
    if not args.skip_batter:
        # PR3: collect opposing-pitcher context once, then thread through the
        # batter dataset builder so each row has opp_pitcher_szn_k_per_9 /
        # _era / _throws_lhp.  Without these the model had no signal about
        # who's pitching that day; the ops_vs_lhp / ops_vs_rhp splits were
        # static season means that couldn't interact with the actual matchup.
        _log("collecting opposing-pitcher context for batter side …")
        opp_pitcher_lookup, pitcher_season_stats = build_opp_pitcher_lookup(payload)
        pitcher_handedness = collect_pitcher_handedness(payload, refresh=args.refresh_data)

        _log("building batter feature matrix …")
        X, y, reg_targets_b, feat_names, batter_snapshots, batter_groups = (
            _build_batter_dataset(
                payload, batter_splits_by_season,
                opp_pitcher_lookup=opp_pitcher_lookup,
                pitcher_season_stats=pitcher_season_stats,
                pitcher_handedness=pitcher_handedness,
            )
        )
        # PR3: same GroupKFold treatment for batters -- player_id is the
        # grouping key so no batter's games span train + test folds.
        acc, model = _train_and_save(
            X, y, batter_path, label="batter", groups=batter_groups,
        )
        summary["batter_oof_acc"] = acc
        summary["batter_features"] = len(feat_names) if feat_names else 0
        summary["batter_snapshots"] = len(batter_snapshots or {})
        if model is not None and not args.no_shap and feat_names is not None:
            batter_importance = _run_shap_analysis(model, X, feat_names, "batter")
        if feat_names:
            batter_feat_names = feat_names
        if X is not None and reg_targets_b:
            # Batter count stats (H, TB, HR, RBI, R, BB) use Poisson objective —
            # principled for non-negative counts and meaningfully better calibrated
            # for sparse targets like HR.
            for stat in _BATTER_REG_STATS:
                y_reg = reg_targets_b.get(stat)
                if y_reg is not None:
                    reg_path = _CACHE_DIR / f"props_model_batter_reg_{stat}.joblib"
                    # PR3: pass groups so the regressor CV also uses
                    # GroupKFold.  Closes the same player-skill leak the
                    # classifier path already plugged.
                    rmse, _ = _train_regressor(
                        X, y_reg, reg_path,
                        label=f"batter_reg_{stat}",
                        objective="count:poisson",
                        groups=batter_groups,
                    )
                    summary[f"batter_reg_{stat}_rmse"] = rmse

    # ── 4. Save SHAP feature importance JSON ────────────────────────────────
    if pitcher_importance or batter_importance:
        _save_feature_importance(pitcher_importance, batter_importance)
        summary["shap_saved"] = str(_CACHE_DIR / "props_feature_importance.json")

    # ── 4b. Save regression feature-name metadata ───────────────────────────
    if pitcher_feat_names or batter_feat_names:
        _save_reg_metadata(pitcher_feat_names, batter_feat_names)
        summary["reg_metadata_saved"] = True

    # ── 4c. Save pitcher rolling-stat snapshots ─────────────────────────────
    # Mirror of the batter snapshot block below.  One row per pitcher capturing
    # their most-recent engineered features.  Inference looks these up so the
    # pitcher rolling features carry real values instead of the prop line.
    pitcher_snapshot_path = _CACHE_DIR / "pitcher_rolling_snapshots.json"
    if pitcher_snapshots:
        try:
            import statistics
            feat_keys: set[str] = set()
            for snap in pitcher_snapshots.values():
                feat_keys.update((snap.get("features") or {}).keys())
            league_medians_p: dict[str, float] = {}
            for fk in feat_keys:
                vals = [
                    float(snap["features"].get(fk, 0.0))
                    for snap in pitcher_snapshots.values()
                    if isinstance(snap.get("features"), dict)
                ]
                if vals:
                    league_medians_p[fk] = float(statistics.median(vals))

            payload_psnap = {
                "generated_at":   datetime.utcnow().isoformat() + "Z",
                "league_medians": league_medians_p,
                "players":        pitcher_snapshots,
            }
            pitcher_snapshot_path.write_text(
                json.dumps(payload_psnap, ensure_ascii=False),
                encoding="utf-8",
            )
            _log(
                f"pitcher snapshots saved: {pitcher_snapshot_path} "
                f"({len(pitcher_snapshots)} players, "
                f"{pitcher_snapshot_path.stat().st_size // 1024} KB)"
            )
            summary["pitcher_snapshots_saved"] = True
        except Exception as exc:  # noqa: BLE001
            _log(f"pitcher snapshot save failed: {exc}")

    # ── 4d. Save batter rolling-stat snapshots ──────────────────────────────
    # One snapshot per player capturing their most-recent engineered row.
    # Inference looks these up by player_name → MLB ID so szn_*/r7_*/r14_*
    # features get real values instead of the prop line as a noisy proxy.
    snapshot_path = _CACHE_DIR / "batter_rolling_snapshots.json"
    if batter_snapshots:
        try:
            import statistics
            # Compute league-median fallback values across all snapshots —
            # used by inference when a specific player has no snapshot
            # (rookies, mid-season call-ups).
            feat_keys: set[str] = set()
            for snap in batter_snapshots.values():
                feat_keys.update((snap.get("features") or {}).keys())
            league_medians: dict[str, float] = {}
            for fk in feat_keys:
                vals = [
                    float(snap["features"].get(fk, 0.0))
                    for snap in batter_snapshots.values()
                    if isinstance(snap.get("features"), dict)
                ]
                if vals:
                    league_medians[fk] = float(statistics.median(vals))

            payload_snap = {
                "generated_at":    datetime.utcnow().isoformat() + "Z",
                "league_medians":  league_medians,
                "players":         batter_snapshots,
            }
            snapshot_path.write_text(
                json.dumps(payload_snap, ensure_ascii=False),
                encoding="utf-8",
            )
            _log(
                f"batter snapshots saved: {snapshot_path} "
                f"({len(batter_snapshots)} players, "
                f"{snapshot_path.stat().st_size // 1024} KB)"
            )
            summary["batter_snapshots_saved"] = True
        except Exception as exc:  # noqa: BLE001
            _log(f"batter snapshot save failed: {exc}")

    # ── 5. Supabase push ────────────────────────────────────────────────────
    if not args.no_push:
        _log("pushing models to Supabase …")
        model_pairs: list[tuple[Path, str]] = [
            (pitcher_path, "props_model_pitcher"),
            (batter_path,  "props_model_batter"),
        ]
        for stat in _PITCHER_REG_STATS:
            reg_path = _CACHE_DIR / f"props_model_pitcher_reg_{stat}.joblib"
            model_pairs.append((reg_path, f"props_model_pitcher_reg_{stat}"))
        for stat in _BATTER_REG_STATS:
            reg_path = _CACHE_DIR / f"props_model_batter_reg_{stat}.joblib"
            model_pairs.append((reg_path, f"props_model_batter_reg_{stat}"))
        # Snapshot JSON travels alongside the joblibs so cold-boot inference
        # gets it via the same restore path.
        if pitcher_snapshot_path.exists():
            model_pairs.append((pitcher_snapshot_path, "pitcher_rolling_snapshots"))
        if snapshot_path.exists():
            model_pairs.append((snapshot_path, "batter_rolling_snapshots"))
        if _TEAM_BASELINES_PATH.exists():
            model_pairs.append((_TEAM_BASELINES_PATH, "team_baselines"))
        push_result = _push_to_supabase_direct(model_pairs)
        summary["supabase_push"] = push_result

    # PR3: post-training backtest snapshot (captures the freshly trained
    # joblibs on the same settled-ledger bets the pre-snapshot scored).
    # Prints a side-by-side delta so the PR can quote the per-market
    # accuracy / hit-rate movement directly.
    if args.run_backtest:
        post_snapshot = _run_backtest_snapshot(label="PR3-post")
        summary["backtest_post"] = post_snapshot
        _print_backtest_delta(summary.get("backtest_pre"), post_snapshot)

    elapsed = time.monotonic() - started
    _log(f"=== DONE in {elapsed:.1f}s ===  summary={json.dumps(summary, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
