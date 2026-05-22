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
                gs = int(st.get("gamesStarted") or 0)
                k  = int(st.get("strikeOuts") or 0)
                bb = int(st.get("baseOnBalls") or 0)
                h  = int(st.get("hits") or 0)
                er = int(st.get("earnedRuns") or 0)
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

def _build_pitcher_dataset(payload: dict, splits_by_season: dict[int, dict[int, dict]]):
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
        return None, None, None, None, None

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
            lambda t: PARK_FACTORS_K.get(str(t) if t else "", 1.0)
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

        for _, row in df.iterrows():
            record = {c: float(row[c]) for c in feat_cols}
            # Static platoon splits (same value for all rows of this player+season)
            record["era_vs_lhb"]    = float(splits.get("era_vs_lhb", 4.50))
            record["k_rate_vs_lhb"] = float(splits.get("k_rate_vs_lhb", 0.215))
            record["era_vs_rhb"]    = float(splits.get("era_vs_rhb", 4.50))
            record["k_rate_vs_rhb"] = float(splits.get("k_rate_vs_rhb", 0.215))
            # Inference-time features — zero during training
            for f in INFER_FEATS:
                record[f] = 0.0
            record["label"] = int(row["label"])
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
        last = df.iloc[-1]
        snap_feats = {c: float(last[c]) for c in feat_cols}
        snap_feats["era_vs_lhb"]    = float(splits.get("era_vs_lhb", 4.50))
        snap_feats["k_rate_vs_lhb"] = float(splits.get("k_rate_vs_lhb", 0.215))
        snap_feats["era_vs_rhb"]    = float(splits.get("era_vs_rhb", 4.50))
        snap_feats["k_rate_vs_rhb"] = float(splits.get("k_rate_vs_rhb", 0.215))
        snapshots[str(pid)] = {
            "name":        pname,
            "team":        pteam,
            "season":      int(season),
            "as_of_date":  str(last.get("date", "")),
            "features":    snap_feats,
        }

    if not rows:
        _log("pitcher dataset empty after feature build")
        return None, None, None, None, None

    df_all = pd.DataFrame(rows)
    feature_names = [c for c in df_all.columns if c != "label"]

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
    _log(
        f"pitcher features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}"
    )
    reg_targets: dict[str, "np.ndarray"] = {
        stat: pd.DataFrame(reg_rows)[stat].to_numpy(dtype=float)
        for stat in _PITCHER_REG_STATS
    }
    _log(f"pitcher snapshots captured: {len(snapshots)} unique pitchers")
    return X, y, reg_targets, feature_names, snapshots


# ---------------------------------------------------------------------------
# Feature engineering — batter
# ---------------------------------------------------------------------------

def _build_batter_dataset(payload: dict, splits_by_season: dict[int, dict[int, dict]]):
    """Build (X, y, feature_names, snapshots) for batter_hits >= 1 label.

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
        ops_vs_rhp, obp_vs_rhp, slg_vs_rhp

    Inference-time placeholder features (zero during training):
        whiff_pct, chase_pct, hard_hit_rate, barrel_rate, sprint_speed,
        platoon_matchup_flag, weather_temp, weather_wind_speed,
        time_of_day, ba_vs_breaking, ba_vs_fastball, ba_vs_offspeed,
        h2h_career_ab, h2h_career_avg, h2h_career_k_rate, implied_total
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _log("pandas/numpy missing -- aborting batter build")
        return None, None, None, None, None

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

        # BABIP rolling averages (7d and 14d)
        df["babip_7d"]  = df["_babip"].shift(1).rolling(window=7,  min_periods=2).mean()
        df["babip_14d"] = df["_babip"].shift(1).rolling(window=14, min_periods=3).mean()

        # K% rolling averages (7d and 14d) for trend feature
        df["k_pct_7d"]  = df["k_pct"].shift(1).rolling(window=7,  min_periods=2).mean()
        df["k_pct_14d"] = df["k_pct"].shift(1).rolling(window=14, min_periods=3).mean()

        # Ballpark factors — park_team is guaranteed to exist above
        df["ballpark_factor_hits"] = df["park_team"].map(
            lambda t: PARK_FACTORS_H.get(str(t) if t else "", 1.0)
        )
        df["ballpark_factor_hr"] = df["park_team"].map(
            lambda t: PARK_FACTORS_HR.get(str(t) if t else "", 1.0)
        )

        df = df.dropna(subset=[f"szn_{roll_stats[0]}", f"r7_{roll_stats[0]}"])
        if df.empty:
            continue

        df["label"]     = (df["H"] >= 1).astype(int)
        df["is_home_i"] = df["is_home"].astype(int)

        feat_cols = (
            [f"szn_{c}"  for c in roll_stats]
            + [f"r7_{c}" for c in roll_stats]
            + [f"r14_{c}" for c in roll_stats]
            + [
                "k_pct_7d", "k_pct_14d",
                "babip_7d", "babip_14d",
                "batting_order",
                "is_home_i",
                "ballpark_factor_hits",
                "ballpark_factor_hr",
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
            record["label"] = int(row["label"])
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
        return None, None, None, None, None

    df_all = pd.DataFrame(rows)
    feature_names = [c for c in df_all.columns if c != "label"]

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
    _log(
        f"batter features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}"
    )
    reg_targets: dict[str, "np.ndarray"] = {
        stat: pd.DataFrame(reg_rows)[stat].to_numpy(dtype=float)
        for stat in _BATTER_REG_STATS
    }
    _log(f"batter snapshots captured: {len(snapshots)} unique players")
    return X, y, reg_targets, feature_names, snapshots


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
    """
    if X is None or y is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) "
             "-- skipping train")
        return None, None
    try:
        from sklearn.model_selection import StratifiedKFold, train_test_split
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
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros(len(y))
    for fold, (tr, te) in enumerate(skf.split(X, y), 1):
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

    # ── 80/20 stratified split: base trains on 80%, isotonic fits on 20% ──
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
) -> tuple[Optional[float], Optional[object]]:
    """5-fold CV XGBoost regression training.  Returns (oof_rmse, final_model).

    *objective* defaults to ``reg:squarederror``.  For non-negative count
    targets (batter H/TB/HR/RBI/R/BB, pitcher K/ER/H/BB/outs) ``count:poisson``
    is principled and typically yields better-calibrated, lower-RMSE
    predictions on sparse counts like HR.
    """
    if X is None or y_reg is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) "
             "-- skipping regressor train")
        return None, None
    try:
        from sklearn.model_selection import KFold
        from sklearn.metrics         import mean_squared_error
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        return None, None

    kf   = KFold(n_splits=5, shuffle=True, random_state=42)
    oof  = np.zeros(len(y_reg))
    for fold, (tr, te) in enumerate(kf.split(X), 1):
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
    args = ap.parse_args()

    # Single-season shortcut
    seasons = [args.season] if args.season else args.seasons

    started = time.monotonic()
    _log(f"=== PROPS MODEL TRAINING ===  seasons={seasons}")
    summary: dict = {"seasons": seasons}

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

    # ── 3. Feature engineering + training ───────────────────────────────────
    pitcher_importance: dict[str, float] = {}
    batter_importance:  dict[str, float] = {}
    pitcher_feat_names: list[str] = []
    batter_feat_names:  list[str] = []

    pitcher_path = Path(".cache/props_model_pitcher.joblib")
    batter_path  = Path(".cache/props_model_batter.joblib")

    pitcher_snapshots: dict[str, dict] = {}
    if not args.skip_pitcher:
        _log("building pitcher feature matrix …")
        X, y, reg_targets_p, feat_names, pitcher_snapshots = _build_pitcher_dataset(
            payload, pitcher_splits_by_season,
        )
        acc, model = _train_and_save(X, y, pitcher_path, label="pitcher")
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
                rmse, _ = _train_regressor(
                    X, y_reg, reg_path,
                    label=f"pitcher_reg_{stat}",
                    objective=obj,
                )
                summary[f"pitcher_reg_{stat}_rmse"] = rmse

    batter_snapshots: dict[str, dict] = {}
    if not args.skip_batter:
        _log("building batter feature matrix …")
        X, y, reg_targets_b, feat_names, batter_snapshots = _build_batter_dataset(
            payload, batter_splits_by_season,
        )
        acc, model = _train_and_save(X, y, batter_path, label="batter")
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
                    rmse, _ = _train_regressor(
                        X, y_reg, reg_path,
                        label=f"batter_reg_{stat}",
                        objective="count:poisson",
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
        push_result = _push_to_supabase_direct(model_pairs)
        summary["supabase_push"] = push_result

    elapsed = time.monotonic() - started
    _log(f"=== DONE in {elapsed:.1f}s ===  summary={json.dumps(summary, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
