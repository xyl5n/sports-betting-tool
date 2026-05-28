"""
Enriched MLB historical dataset — attaches real starting pitcher stats
to every Retrosheet game so the models train on genuine feature variation
instead of league-average neutral baselines.

Data sources
------------
1. Retrosheet game logs (.cache/retrosheet/gl{year}.joblib) — already cached
2. MLB Stats API (statsapi.mlb.com, free, no key) — historical schedule with
   probablePitcher hydration gives starters for every game date 2022-2024
3. pybaseball (optional) — team-level pitching ERA for bullpen proxy

Features newly enriched vs. basic historical_data.py
-----------------------------------------------------
* sp_era_diff      (index 10) — was always 0.0
* sp_whip_diff     (index 11) — was always 0.0
* sp_k_rate_diff   (index 12) — was always 0.0
* home_sp_rest     (index 13) — was always 4 days
* away_sp_rest     (index 14) — was always 4 days
* sp_hand_adv      (index 15) — was always 0
* bullpen_era_diff (index 19) — was neutral unless pybaseball installed

Still neutral (no free historical data source available)
---------------------------------------------------------
* errors_diff, home_implied_prob, run_line, wind_speed, wind_direction,
  bullpen_fatigue_diff, lineup_confirmed, line_movement

Cache paths
-----------
.cache/enriched_mlb_dataset.joblib   — full enriched (X, y, y_rl, totals)
.cache/mlb_hist_schedule_{date}.json — schedule per game date (1-yr TTL)
.cache/mlb_hist_sp_{pid}_{yr}.json   — per-pitcher season stats (1-yr TTL)
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import joblib
import numpy as np

log = logging.getLogger(__name__)

_CACHE_DIR    = Path(".cache")
_DATASET_PATH = Path(".cache/enriched_mlb_dataset.joblib")
_CACHE_DAYS   = 30
_MIN_GAMES    = 10
_SEASONS      = (2022, 2023, 2024, 2025)   # 2025 added for recency weighting
_BASE         = "https://statsapi.mlb.com/api/v1"

# Neutral pitcher values used as fallback
_NEUTRAL_SP = {
    "era": 4.50, "whip": 1.30, "k_rate": 0.215,
    "bb9": 3.30, "era_home": 4.50, "era_away": 4.50, "last3_era": 4.50,
    "hand": 0, "rest": 4,
}
_NEUTRAL_BP = 4.20

# League baselines (mirror mlb_features._LEAGUE so the historical and live
# composites use the same z-score reference).
_LG_K, _LG_K_STD = 0.215, 0.05
_LG_E, _LG_E_STD = 4.50,  1.5
_LG_W, _LG_W_STD = 1.30,  0.30

_RETRO_TO_MLB: dict[str, str] = {
    "ANA": "Los Angeles Angels",  "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",      "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",      "CHA": "Chicago White Sox",
    "CHN": "Chicago Cubs",        "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians", "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",      "HOU": "Houston Astros",
    "KCA": "Kansas City Royals",  "LAN": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",       "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",     "NYA": "New York Yankees",
    "NYN": "New York Mets",       "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies","PIT": "Pittsburgh Pirates",
    "SDN": "San Diego Padres",    "SEA": "Seattle Mariners",
    "SFN": "San Francisco Giants","SLN": "St. Louis Cardinals",
    "TBA": "Tampa Bay Rays",      "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",   "WAS": "Washington Nationals",
}


# ---------------------------------------------------------------------------
# Low-level HTTP helper
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 12) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sports-betting-ai/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def _cache_path(key: str) -> Path:
    safe = key.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "_")
    return _CACHE_DIR / f"{safe}.json"


def _cache_get(key: str, ttl_days: int = 365):
    p = _cache_path(key)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_days * 86400:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_set(key: str, value) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    try:
        _cache_path(key).write_text(json.dumps(value, default=str), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# ---------------------------------------------------------------------------
# Rolling-stats tracker (identical to historical_data.py)
# ---------------------------------------------------------------------------

class _TeamSeason:
    def __init__(self) -> None:
        self.g = self.w = 0
        self.hg = self.hw = 0
        self.ag = self.aw = 0
        self.rs = self.ra = 0
        self.last10: deque = deque(maxlen=10)
        self.last20: deque = deque(maxlen=20)   # for season-trend feature

    def stats(self) -> dict:
        def p(n, d): return n / d if d > 0 else 0.5
        win_pct  = p(self.w, self.g)
        last20p  = (sum(self.last20) / len(self.last20)) if self.last20 else 0.5
        return {
            "games":        self.g,
            "win_pct":      win_pct,
            "home_win_pct": p(self.hw, self.hg),
            "away_win_pct": p(self.aw, self.ag),
            "rpg":   self.rs / self.g if self.g > 0 else 4.5,
            "rapg":  self.ra / self.g if self.g > 0 else 4.5,
            "last10": sum(self.last10) / len(self.last10) if self.last10 else 0.5,
            "last20": last20p,
            # season_trend: positive = team improving vs. its season average
            "season_trend": last20p - win_pct,
        }

    def update(self, rs, ra, *, is_home: bool) -> None:
        won = rs > ra
        self.g += 1; self.rs += rs; self.ra += ra
        self.last10.append(1 if won else 0)
        self.last20.append(1 if won else 0)   # track last-20 window
        if won: self.w += 1
        if is_home:
            self.hg += 1
            if won: self.hw += 1
        else:
            self.ag += 1
            if won: self.aw += 1


# ---------------------------------------------------------------------------
# MLB Stats API: schedule + pitcher stats
# ---------------------------------------------------------------------------

def _team_tokens(name: str) -> set[str]:
    return set(name.lower().split())


def _fetch_date_schedule(date_str: str) -> list[dict]:
    """
    Return list of game entries for one date.
    Each entry: {game_pk, home_name, away_name, home_pitcher, away_pitcher}
    where *_pitcher is either a dict {id, note, pitchHand} or None.
    Cached for 1 year (historical data never changes).

    Waterfall:
      1. MLB Stats API (statsapi.mlb.com) -- full pitcher data
      2. ESPN scoreboard for date          -- team names only, pitcher=None
      3. BallDontLie for date              -- team names only, pitcher=None
    Fallback pitcher entries (None) resolve to _NEUTRAL_SP via _pitcher_from_entry().
    """
    key = f"mlb_hist_schedule_{date_str}"
    cached = _cache_get(key, ttl_days=365)
    if cached is not None:
        return cached

    # -- Primary: MLB Stats API --------------------------------------------------
    url  = (f"{_BASE}/schedule?sportId=1&date={date_str}"
            f"&hydrate=probablePitcher(note,pitchHand)")
    data = _fetch(url)

    # data.get("dates") returns:
    #   None  -> API unreachable / returned {} (failure -- try fallbacks)
    #   []    -> API responded, no games on this date (cache and return)
    #   [...]  -> games found (cache and return)
    dates_payload = data.get("dates")

    if dates_payload is not None:
        # Primary responded -- parse it (may be empty list for off-days)
        entries = []
        for day in dates_payload:
            for game in day.get("games", []):
                teams = game.get("teams", {})
                entries.append({
                    "game_pk":      game.get("gamePk"),
                    "home_name":    teams.get("home", {}).get("team", {}).get("name", ""),
                    "away_name":    teams.get("away", {}).get("team", {}).get("name", ""),
                    "home_pitcher": teams.get("home", {}).get("probablePitcher"),
                    "away_pitcher": teams.get("away", {}).get("probablePitcher"),
                })
        _cache_set(key, entries)
        return entries

    # -- Primary failed (data == {}) -- try fallbacks ---------------------------
    # Fallback 1: ESPN
    try:
        from .mlb_fallback_fetcher import fetch_espn_mlb_schedule_for_date
        espn_entries = fetch_espn_mlb_schedule_for_date(date_str)
        if espn_entries:
            _cache_set(key, espn_entries)
            return espn_entries
    except Exception as exc:
        log.debug("ESPN schedule fallback failed for %s: %s", date_str, exc)

    # Fallback 2: BallDontLie
    try:
        import os
        from .mlb_fallback_fetcher import fetch_bdl_mlb_schedule_for_date
        bdl_key = os.environ.get("BALLDONTLIE_API_KEY", "")
        bdl_entries = fetch_bdl_mlb_schedule_for_date(date_str, bdl_key)
        if bdl_entries:
            _cache_set(key, bdl_entries)
            return bdl_entries
    except Exception as exc:
        log.debug("BallDontLie schedule fallback failed for %s: %s", date_str, exc)

    # All sources failed -- return empty without caching so next run retries
    return []


def _ip_to_float(ip_str) -> float:
    """MLB Stats API reports innings pitched as '123.1' = 123 + 1/3 innings."""
    s = str(ip_str or "0")
    try:
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + (float(frac) / 3.0)
    except (ValueError, TypeError):
        return 0.0


def _fetch_pitcher_stats(pid: int, season: int) -> dict:
    """
    Return pitcher season stats for the given season year, enriched with
    BB/9 (computed from baseOnBalls + inningsPitched) and home/away ERA
    splits (separate API call). Cached per (pitcher_id, season) for 1 year.
    """
    if not pid:
        return dict(_NEUTRAL_SP)

    # Bump cache key suffix so old (era/whip/k_rate/hand/rest)-only caches
    # are invalidated and regenerated with the new BB/9 + split fields.
    key = f"mlb_hist_sp_{pid}_{season}_v2"
    cached = _cache_get(key, ttl_days=365)
    if cached is not None:
        return cached

    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=season&season={season}&group=pitching&sportId=1")
    data = _fetch(url)

    era = whip = k_rate = bb9 = None
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            st = split.get("stat", {})
            try:
                era_val  = float(st.get("era", ""))
                whip_val = float(st.get("whip", ""))
                k   = float(st.get("strikeOuts", 0) or 0)
                bf  = float(st.get("battersFaced", 1) or 1)
                bb  = float(st.get("baseOnBalls", 0) or 0)
                ip  = _ip_to_float(st.get("inningsPitched", "0"))
                era = era_val; whip = whip_val
                k_rate = k / bf if bf > 0 else 0.215
                bb9    = (bb * 9.0 / ip) if ip > 0 else None
            except (TypeError, ValueError):
                pass
            break
        if era is not None:
            break

    # Home/away splits — separate endpoint, cached implicitly by inclusion below
    splits = _fetch_pitcher_home_away_splits(pid, season)

    result = {
        "era":      era    if era    is not None else 4.50,
        "whip":     whip   if whip   is not None else 1.30,
        "k_rate":   k_rate if k_rate is not None else 0.215,
        "bb9":      bb9    if bb9    is not None else 3.30,
        "era_home": splits.get("home", era if era is not None else 4.50),
        "era_away": splits.get("away", era if era is not None else 4.50),
        "hand":     0,   # filled in from schedule data if available
        "rest":     4,   # computed separately from game log dates
    }
    _cache_set(key, result)
    return result


def _fetch_pitcher_home_away_splits(pid: int, season: int) -> dict:
    """
    Return {'home': ERA, 'away': ERA} for a pitcher's H/A splits in a season.
    One API call per (pid, season); cached for 1 year. Empty dict on failure.
    """
    if not pid:
        return {}
    key = f"mlb_hist_sp_splits_{pid}_{season}"
    cached = _cache_get(key, ttl_days=365)
    if cached is not None:
        return cached

    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=statSplits&season={season}&group=pitching"
           f"&sitCodes=h,a&sportId=1")
    data = _fetch(url)
    result: dict[str, float] = {}
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            code = (split.get("split") or {}).get("code", "").lower()
            try:
                era = float((split.get("stat") or {}).get("era", "")) if (
                    split.get("stat") or {}).get("era") not in (None, "", "-.--") else None
            except (TypeError, ValueError):
                era = None
            if era is None:
                continue
            if code == "h":
                result["home"] = era
            elif code == "a":
                result["away"] = era
    _cache_set(key, result)
    return result


def _fetch_pitcher_gamelog(pid: int, season: int) -> list[dict]:
    """
    Return the pitcher's gameLog for one season as a list of
    {'date': 'YYYY-MM-DD', 'ip': float, 'er': int} entries (chronological).
    One API call per (pid, season); cached for 1 year. Empty list on failure.
    """
    if not pid:
        return []
    key = f"mlb_hist_sp_gamelog_{pid}_{season}"
    cached = _cache_get(key, ttl_days=365)
    if cached is not None:
        return cached

    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=gameLog&season={season}&group=pitching&sportId=1")
    data = _fetch(url)
    out: list[dict] = []
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            d  = (split.get("date") or "")[:10]
            st = split.get("stat") or {}
            if not d:
                continue
            ip = _ip_to_float(st.get("inningsPitched", "0"))
            try:
                er = int(float(st.get("earnedRuns", 0) or 0))
            except (TypeError, ValueError):
                er = 0
            out.append({"date": d, "ip": ip, "er": er})
    out.sort(key=lambda r: r["date"])
    _cache_set(key, out)
    return out


def _last3_era_before(gamelog: list[dict], game_date: str) -> Optional[float]:
    """
    Compute ERA over the pitcher's last ≤3 starts strictly before game_date.
    Returns None if fewer than 1 prior start with non-zero IP.
    """
    if not gamelog or not game_date:
        return None
    prior = [g for g in gamelog if g["date"] < game_date]
    if not prior:
        return None
    window = prior[-3:]
    ip_total = sum(g["ip"] for g in window)
    er_total = sum(g["er"] for g in window)
    return (er_total * 9.0 / ip_total) if ip_total > 0 else None


def _hand_adv(h_hand: int, a_hand: int) -> float:
    if h_hand == 1 and a_hand == 0: return  1.0
    if h_hand == 0 and a_hand == 1: return -1.0
    return 0.0


# ---------------------------------------------------------------------------
# Per-game pitcher lookup
# ---------------------------------------------------------------------------

def _match_game(
    schedule_entries: list[dict],
    home_name: str,
    away_name: str,
) -> Optional[dict]:
    """
    Find the schedule entry matching home/away team names.
    Uses token overlap (handles minor name differences like "Guardians" vs "Indians").
    """
    h_tokens = _team_tokens(home_name)
    a_tokens = _team_tokens(away_name)
    best, best_score = None, 0
    for entry in schedule_entries:
        h_ov = len(_team_tokens(entry.get("home_name", "")) & h_tokens)
        a_ov = len(_team_tokens(entry.get("away_name", "")) & a_tokens)
        score = h_ov + a_ov
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= 2 else None


def _pitcher_from_entry(
    pitcher_info: Optional[dict],
    season: int,
    hand_override: Optional[int] = None,
) -> dict:
    """Extract pitcher stats from a schedule probablePitcher entry."""
    if not pitcher_info:
        return dict(_NEUTRAL_SP)

    pid = pitcher_info.get("id")
    stats = _fetch_pitcher_stats(pid or 0, season)

    # Handedness can come directly from schedule hydration
    if hand_override is not None:
        stats["hand"] = hand_override
    elif isinstance(pitcher_info.get("pitchHand"), dict):
        stats["hand"] = 1 if pitcher_info["pitchHand"].get("code") == "L" else 0

    return stats


# ---------------------------------------------------------------------------
# Pitcher rest-day tracker (per-season)
# ---------------------------------------------------------------------------

class _PitcherRestTracker:
    """Tracks last start date per pitcher ID to compute rest days."""

    def __init__(self) -> None:
        self._last: dict[int, str] = {}   # pid -> last start date (YYYY-MM-DD)

    def get_rest(self, pid: Optional[int], game_date: str) -> int:
        if not pid:
            return 4
        last = self._last.get(pid)
        if last is None:
            return 4   # first start of season
        try:
            delta = datetime.fromisoformat(game_date) - datetime.fromisoformat(last)
            return max(0, min(delta.days - 1, 10))
        except Exception:
            return 4

    def record(self, pid: Optional[int], game_date: str) -> None:
        if pid:
            self._last[pid] = game_date


# ---------------------------------------------------------------------------
# Build feature vector (identical layout to historical_data._build_vec)
# ---------------------------------------------------------------------------

def _pitcher_dominance(sp: dict) -> float:
    """Mirrors mlb_features._pitcher_dominance using the same league baselines."""
    z_k = (sp.get("k_rate", _LG_K) - _LG_K) / _LG_K_STD
    z_e = (sp.get("era",    _LG_E) - _LG_E) / _LG_E_STD
    z_w = (sp.get("whip",   _LG_W) - _LG_W) / _LG_W_STD
    return float(z_k - z_e - z_w)


def _blowout_prob_hist(
    sp_era_diff: float, bullpen_era_diff: float,
    net_run_diff: float, sp_recent_form_diff: float,
) -> float:
    """Mirrors mlb_features._blowout_probability (same weights / sigma)."""
    score = (0.40 * net_run_diff
             + 0.30 * sp_era_diff
             + 0.20 * bullpen_era_diff
             + 0.10 * sp_recent_form_diff)
    return float(np.clip(1.0 / (1.0 + np.exp(-score / 2.0)), 0.02, 0.98))


def _build_vec(
    hs: dict, as_: dict,
    h_bat: dict, a_bat: dict,
    h_era: float, a_era: float,    # bullpen ERA
    park_run: float,
    h_sp: dict, a_sp: dict,
    h_last3_era: Optional[float] = None,
    a_last3_era: Optional[float] = None,
) -> np.ndarray:
    """
    Assemble the 30-element MLB_FEATURES vector.
    Indices 10-15 + 19 + 24-27 + 29 are populated from REAL values where the
    historical pipeline has them. Index 28 (lineup_vuln_diff) stays neutral 0
    because batter-platoon splits were skipped from the backfill (user choice).
    """
    h_rpg  = hs["rpg"];  a_rpg  = as_["rpg"]
    h_rapg = hs["rapg"]; a_rapg = as_["rapg"]
    h_hpg  = h_bat.get("hpg", 8.5)
    a_hpg  = a_bat.get("hpg", 8.5)

    # Season trend: (home last20 win% − home season win%) − (away last20 win% − away season win%)
    h_trend = hs.get("season_trend", 0.0)
    a_trend = as_.get("season_trend", 0.0)

    net_diff   = (h_rpg - h_rapg) - (a_rpg - a_rapg)
    sp_era_d   = a_sp["era"]  - h_sp["era"]
    sp_whip_d  = a_sp["whip"] - h_sp["whip"]
    sp_k_d     = h_sp["k_rate"] - a_sp["k_rate"]
    bp_era_d   = a_era - h_era

    bb9_diff = a_sp.get("bb9", 3.30) - h_sp.get("bb9", 3.30)
    sp_split_era_diff = (
        a_sp.get("era_away", a_sp["era"])
        - h_sp.get("era_home", h_sp["era"])
    )
    # last-3-start ERA: fall back to season ERA when prior-3 unavailable so the
    # diff is 0 rather than misleading.
    h_l3 = h_last3_era if h_last3_era is not None else h_sp["era"]
    a_l3 = a_last3_era if a_last3_era is not None else a_sp["era"]
    sp_recent_form_diff = a_l3 - h_l3

    pitcher_dom_diff = _pitcher_dominance(h_sp) - _pitcher_dominance(a_sp)
    blowout = _blowout_prob_hist(
        sp_era_diff=sp_era_d, bullpen_era_diff=bp_era_d,
        net_run_diff=net_diff, sp_recent_form_diff=sp_recent_form_diff,
    )

    return np.array([
        # ── Team statistics (0-9) ────────────────────────────────────────────
        net_diff,                                   # 0  net_run_diff
        h_rpg  - a_rpg,                              # 1  rpg_diff
        h_rapg - a_rapg,                             # 2  rapg_diff
        hs["win_pct"]      - as_["win_pct"],         # 3  win_pct_diff
        hs["home_win_pct"] - as_["away_win_pct"],    # 4  home_away_split_diff
        hs["last10"]       - as_["last10"],          # 5  last10_diff
        h_hpg - a_hpg,                               # 6  hits_diff
        0.0,                                          # 7  errors_diff (neutral)
        0.54,                                         # 8  home_implied_prob (neutral)
        -1.5,                                         # 9  run_line (neutral)
        # ── Starting pitcher (10-15) — REAL values ───────────────────────────
        sp_era_d,                                     # 10 sp_era_diff
        sp_whip_d,                                    # 11 sp_whip_diff
        sp_k_d,                                       # 12 sp_k_rate_diff
        float(h_sp["rest"]),                          # 13 home_sp_rest
        float(a_sp["rest"]),                          # 14 away_sp_rest
        _hand_adv(h_sp["hand"], a_sp["hand"]),        # 15 sp_hand_adv
        # ── Ballpark (16-18) ─────────────────────────────────────────────────
        park_run,                                     # 16 park_run_factor (real)
        0.0,                                          # 17 wind_speed (neutral)
        0.0,                                          # 18 wind_direction (neutral)
        # ── Bullpen (19-20) ──────────────────────────────────────────────────
        bp_era_d,                                     # 19 bullpen_era_diff
        0.0,                                          # 20 bullpen_fatigue_diff (neutral)
        # ── Lineup / market (21-22) ──────────────────────────────────────────
        0.0,                                          # 21 lineup_confirmed (neutral)
        0.0,                                          # 22 line_movement (neutral)
        # ── Season trend (23) ────────────────────────────────────────────────
        h_trend - a_trend,                            # 23 trend_diff
        # ── Player-level pitcher (24-26) — REAL where backfilled ─────────────
        bb9_diff,                                     # 24 bb9_diff
        sp_split_era_diff,                            # 25 sp_split_era_diff
        sp_recent_form_diff,                          # 26 sp_recent_form_diff
        # ── Composites (27-29) ───────────────────────────────────────────────
        pitcher_dom_diff,                             # 27 pitcher_dominance_diff (REAL)
        0.0,                                          # 28 lineup_vuln_diff (NEUTRAL: batters not backfilled)
        blowout,                                      # 29 blowout_prob (REAL)
    ], dtype=np.float32)


def _build_totals_vec(
    hs: dict, as_: dict,
    h_sp: dict, a_sp: dict,
    h_bp_era: float, a_bp_era: float,
    park_run: float,
) -> np.ndarray:
    """9-feature totals vector with real SP ERA/k_rate values."""
    return np.array([
        hs["rpg"]  + as_["rpg"],                   # combined_rpg
        hs["rapg"] + as_["rapg"],                   # combined_rapg
        h_sp["era"] + a_sp["era"],                  # combined_sp_era  (REAL)
        h_sp["k_rate"],                             # home_sp_k_rate  (REAL)
        a_sp["k_rate"],                             # away_sp_k_rate  (REAL)
        park_run,                                   # park_run_factor (real)
        0.0,                                        # wind_speed (neutral)
        h_bp_era + a_bp_era,                        # combined_bullpen_era (real proxy)
        72.0,                                       # temperature (neutral)
    ], dtype=np.float32)


# ---------------------------------------------------------------------------
# pybaseball helpers
# ---------------------------------------------------------------------------

def _load_pybaseball_stats(seasons):
    from .retrosheet_client import FG_TO_RETRO
    batting_lu: dict = {}
    pitching_lu: dict = {}
    try:
        import pybaseball
        try:
            tb = pybaseball.team_batting(min(seasons), max(seasons))
            for _, row in tb.iterrows():
                fg = str(row.get("teamIDfg") or "")
                rc = FG_TO_RETRO.get(fg)
                if not rc: continue
                season = int(row.get("Season", 0))
                g = float(row.get("G", 0) or 0) or 162.0
                h = float(row.get("H", 0) or 0)
                batting_lu[(rc, season)] = {"hpg": h / g}
        except Exception as e:
            log.debug("pybaseball batting: %s", e)
        try:
            tp = pybaseball.team_pitching(min(seasons), max(seasons))
            for _, row in tp.iterrows():
                fg = str(row.get("teamIDfg") or "")
                rc = FG_TO_RETRO.get(fg)
                if not rc: continue
                season = int(row.get("Season", 0))
                era = float(row.get("ERA", 4.20) or 4.20)
                pitching_lu[(rc, season)] = {"era": era}
        except Exception as e:
            log.debug("pybaseball pitching: %s", e)
    except ImportError:
        pass
    return batting_lu, pitching_lu


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_enriched_dataset(
    seasons: tuple = _SEASONS,
    force_rebuild: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Build (X_24, y_ml, y_rl, totals_combined) from Retrosheet + MLB Stats API.

    X_24            : (n, 24) float32 — full MLB feature vector with real pitcher values
                      and season-trend feature (index 23)
    y_ml            : (n,)    int32   — 1 = home win
    y_rl            : (n,)    int32   — 1 = home covers -1.5 (wins by 2+)
    totals_combined : (n,)    float32 — actual combined runs scored

    Also saves a 'row_seasons' array (per-row season year) used for recency
    weighting when training the models.
    """
    _CACHE_DIR.mkdir(exist_ok=True)

    # Import locally to avoid circular import at module load
    from .sports_config import MLB_FEATURES
    expected_n_features = len(MLB_FEATURES)

    if not force_rebuild and _DATASET_PATH.exists():
        age = (datetime.now() - datetime.fromtimestamp(
            _DATASET_PATH.stat().st_mtime)).days
        if age < _CACHE_DAYS:
            saved = joblib.load(_DATASET_PATH)
            # Validate that cached data matches the current MLB_FEATURES count
            # and the seasons tuple we expect. Adding new player-level features
            # (n_features=30) invalidates older 24-column caches automatically.
            cached_seasons = tuple(saved.get("seasons_list", ()))
            n_features     = saved["X"].shape[1] if "X" in saved and len(saved.get("y_ml", [])) else 0
            if (
                "X" in saved and "y_ml" in saved and "y_rl" in saved
                and n_features == expected_n_features
                and cached_seasons == tuple(seasons)
            ):
                n = len(saved["y_ml"])
                sp_enriched = saved.get("sp_enriched", 0)
                print(f"  Enriched dataset from cache: {n} games "
                      f"({sp_enriched} with real pitcher data, "
                      f"seasons {list(seasons)})")
                return saved["X"], saved["y_ml"], saved["y_rl"], saved.get(
                    "totals_combined", np.zeros(n, dtype=np.float32))

    from .retrosheet_client import get_season_gamelogs
    from .park_factors import get_park_factors

    print("  Loading enriched MLB historical dataset…")
    batting_lu, pitching_lu = _load_pybaseball_stats(seasons)

    X_rows:       list[np.ndarray] = []
    X_tot_rows:   list[np.ndarray] = []
    y_ml_rows:    list[int]        = []
    y_rl_rows:    list[int]        = []
    totals_rows:  list[float]      = []
    season_labels: list[int]       = []   # which season each row belongs to
    total_skipped = 0
    sp_enriched   = 0

    for season in seasons:
        games = get_season_gamelogs(season)
        if not games:
            print(f"  Retrosheet {season}: not available — skipping")
            continue

        print(f"  Processing {season}: {len(games)} games…", flush=True)
        games.sort(key=lambda g: g["date"])

        trackers: dict[str, _TeamSeason] = {}
        rest_tracker = _PitcherRestTracker()

        # Per-pitcher game log cache for last-3-start ERA. Populated lazily
        # on first appearance of each pid in this season.
        pitcher_gamelogs: dict[int, list[dict]] = {}

        # Pre-fetch all unique game dates for this season (batched)
        unique_dates = sorted({g["date"] for g in games})
        date_schedules: dict[str, list[dict]] = {}

        print(f"    Fetching {len(unique_dates)} game dates from MLB Stats API…",
              flush=True)
        for i, d in enumerate(unique_dates, 1):
            date_schedules[d] = _fetch_date_schedule(d)
            if i % 30 == 0:
                print(f"    {i}/{len(unique_dates)} dates…", flush=True)
        print(f"    Done fetching schedule data.")

        season_rows = season_sp = 0

        for game in games:
            hc = game["home_code"]
            ac = game["away_code"]
            if hc not in trackers: trackers[hc] = _TeamSeason()
            if ac not in trackers: trackers[ac] = _TeamSeason()

            ht = trackers[hc]; at = trackers[ac]
            hs_pre = ht.stats(); as_pre = at.stats()
            home_r = game["home_runs"]; away_r = game["away_runs"]
            date_str = game["date"]

            # Lookup starters from MLB Stats API schedule
            sched = date_schedules.get(date_str, [])
            home_name = game.get("home_name", _RETRO_TO_MLB.get(hc, hc))
            away_name = game.get("away_name", _RETRO_TO_MLB.get(ac, ac))
            entry = _match_game(sched, home_name, away_name)

            h_sp_info = entry["home_pitcher"] if entry else None
            a_sp_info = entry["away_pitcher"] if entry else None

            h_pid = (h_sp_info or {}).get("id")
            a_pid = (a_sp_info or {}).get("id")

            # Compute rest BEFORE recording this start
            h_rest = rest_tracker.get_rest(h_pid, date_str)
            a_rest = rest_tracker.get_rest(a_pid, date_str)

            # Record starts
            rest_tracker.record(h_pid, date_str)
            rest_tracker.record(a_pid, date_str)

            # Update team trackers
            ht.update(home_r, away_r, is_home=True)
            at.update(away_r, home_r, is_home=False)

            if hs_pre["games"] < _MIN_GAMES or as_pre["games"] < _MIN_GAMES:
                total_skipped += 1
                continue

            # Fetch pitcher stats
            h_sp = _pitcher_from_entry(h_sp_info, season)
            a_sp = _pitcher_from_entry(a_sp_info, season)
            h_sp["rest"] = h_rest
            a_sp["rest"] = a_rest

            if h_pid or a_pid:
                season_sp += 1

            # Lookup pybaseball team stats
            h_bat  = batting_lu.get((hc, season), {})
            a_bat  = batting_lu.get((ac, season), {})
            h_bp_era = pitching_lu.get((hc, season), {}).get("era", _NEUTRAL_BP)
            a_bp_era = pitching_lu.get((ac, season), {}).get("era", _NEUTRAL_BP)

            # Park factor
            park_run, _ = get_park_factors(home_name)

            # Last-3-start ERA (one gameLog fetch per pid-season, then sliced)
            h_l3 = a_l3 = None
            if h_pid:
                if h_pid not in pitcher_gamelogs:
                    pitcher_gamelogs[h_pid] = _fetch_pitcher_gamelog(h_pid, season)
                h_l3 = _last3_era_before(pitcher_gamelogs[h_pid], date_str)
            if a_pid:
                if a_pid not in pitcher_gamelogs:
                    pitcher_gamelogs[a_pid] = _fetch_pitcher_gamelog(a_pid, season)
                a_l3 = _last3_era_before(pitcher_gamelogs[a_pid], date_str)

            vec = _build_vec(hs_pre, as_pre, h_bat, a_bat,
                             h_bp_era, a_bp_era, park_run, h_sp, a_sp,
                             h_last3_era=h_l3, a_last3_era=a_l3)
            tvec = _build_totals_vec(hs_pre, as_pre, h_sp, a_sp,
                                     h_bp_era, a_bp_era, park_run)

            X_rows.append(vec)
            X_tot_rows.append(tvec)
            y_ml_rows.append(1 if home_r > away_r else 0)
            y_rl_rows.append(1 if (home_r - away_r) >= 2 else 0)
            totals_rows.append(float(home_r + away_r))
            season_labels.append(season)   # tag with season year
            season_rows += 1

        sp_enriched += season_sp
        print(f"  {season}: {season_rows} rows, {season_sp} with real SP data")

    if not X_rows:
        print("  No enriched data available — returning empty arrays")
        empty = np.empty((0, expected_n_features), dtype=np.float32)
        return empty, np.array([], np.int32), np.array([], np.int32), np.array([], np.float32)

    X           = np.vstack(X_rows).astype(np.float32)
    X_tot       = np.vstack(X_tot_rows).astype(np.float32)
    y_ml        = np.array(y_ml_rows,    dtype=np.int32)
    y_rl        = np.array(y_rl_rows,    dtype=np.int32)
    totals      = np.array(totals_rows,  dtype=np.float32)
    row_seasons = np.array(season_labels, dtype=np.int32)

    # Season breakdown for logging
    for s in sorted(set(season_labels)):
        count = int((row_seasons == s).sum())
        print(f"    Season {s}: {count:,} rows")

    joblib.dump({
        "X": X, "X_totals": X_tot,
        "y_ml": y_ml, "y_rl": y_rl, "totals_combined": totals,
        "row_seasons":  row_seasons,   # per-row season labels for recency weighting
        "sp_enriched":  sp_enriched,
        "built_at":     datetime.now().isoformat(),
        "seasons_list": list(seasons),
    }, _DATASET_PATH)

    print(f"  Enriched dataset: {len(y_ml):,} games "
          f"({sp_enriched} with real SP data, {total_skipped} skipped)")
    return X, y_ml, y_rl, totals


def get_enriched_X_y(seasons=_SEASONS, force_rebuild=False):
    """
    Convenience wrapper: returns (X, y_ml).
    Transparently provides enriched data to any caller that used basic historical data.
    """
    X, y_ml, _, _ = build_enriched_dataset(seasons, force_rebuild)
    return X, y_ml


def get_enriched_seasons(seasons=_SEASONS, force_rebuild=False) -> np.ndarray:
    """
    Return an int32 array of per-row season years (e.g. 2022, 2023, 2024, 2025).
    Used by model training to split data into old / previous / current buckets
    for recency weighting.

    Fallback: if 'row_seasons' is absent from the cached dataset (old format)
    OR the cached seasons tuple doesn't match *seasons*, we trigger a rebuild.
    If the rebuild also fails, we return an array of year 2024 (treats all rows
    as "old" data — safe degradation).
    """
    if not force_rebuild and _DATASET_PATH.exists():
        try:
            saved = joblib.load(_DATASET_PATH)
            cached_seasons = tuple(saved.get("seasons_list", ()))
            if "row_seasons" in saved and cached_seasons == tuple(seasons):
                return saved["row_seasons"]
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    # Need to rebuild to get accurate per-row season labels
    try:
        build_enriched_dataset(seasons, force_rebuild=True)
        saved = joblib.load(_DATASET_PATH)
        return saved.get("row_seasons", np.array([], dtype=np.int32))
    except Exception as exc:
        log.warning("get_enriched_seasons: rebuild failed (%s) — defaulting all to 2024", exc)
        # Fall back: treat everything as old data (year 2024)
        try:
            saved = joblib.load(_DATASET_PATH)
            n = len(saved.get("y_ml", []))
            return np.full(n, 2024, dtype=np.int32)
        except Exception:
            return np.array([], dtype=np.int32)


def get_enriched_totals_X_y(seasons=_SEASONS, force_rebuild=False):
    """Return (X_totals, y_combined_runs) for the totals model."""
    if not force_rebuild and _DATASET_PATH.exists():
        try:
            saved = joblib.load(_DATASET_PATH)
            cached_seasons = tuple(saved.get("seasons_list", ()))
            if "X_totals" in saved and cached_seasons == tuple(seasons):
                return saved["X_totals"], saved["totals_combined"]
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    # Build fresh
    build_enriched_dataset(seasons, force_rebuild=True)
    saved = joblib.load(_DATASET_PATH)
    return saved["X_totals"], saved["totals_combined"]
