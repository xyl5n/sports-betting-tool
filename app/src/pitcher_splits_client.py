"""
Extended pitcher stats (home/away ERA splits, BB/9, last-3-starts ERA trend)
fetched from the free MLB Stats API (statsapi.mlb.com).

Used for the Pitcher Dominance Score and Blowout Probability composite
features.  Daily-cached per (pitcher_id, season) so each pitcher is hit
at most once per day during live prediction.

Live cache:        .cache/pitcher_splits_live.json   (TTL 24h)
Historical cache:  .cache/mlb_hist_psplits_{pid}_{yr}.json   (TTL 1yr)
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .utils import _safe, _fetch_url_ua

_BASE = "https://statsapi.mlb.com/api/v1"
_LIVE_CACHE_FILE = Path(".cache/pitcher_splits_live.json")
_LIVE_CACHE_TTL  = 86400   # 24 hours
_HIST_CACHE_DIR  = Path(".cache")
_HIST_CACHE_TTL  = 365 * 86400

# Defaults when the API returns nothing for a pitcher.
_NEUTRAL_SPLITS = {
    "home_era":      4.50,
    "away_era":      4.50,
    "bb_per_9":      3.20,
    "last3_era":     4.50,
    "last3_era_change": 0.0,   # negative = improving
}


# ── Live cache (single JSON file, daily TTL) ─────────────────────────────────

def _load_live_cache() -> dict:
    try:
        if _LIVE_CACHE_FILE.exists():
            raw = json.loads(_LIVE_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _LIVE_CACHE_TTL:
                return raw
    except Exception:
        pass
    return {}


def _save_live_cache(data: dict) -> None:
    try:
        _LIVE_CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _LIVE_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


# ── Historical per-(pid, season) cache ───────────────────────────────────────

def _hist_cache_path(pid: int, season: int) -> Path:
    return _HIST_CACHE_DIR / f"mlb_hist_psplits_{pid}_{season}.json"


def _hist_cache_get(pid: int, season: int) -> Optional[dict]:
    p = _hist_cache_path(pid, season)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > _HIST_CACHE_TTL:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _hist_cache_set(pid: int, season: int, value: dict) -> None:
    try:
        _HIST_CACHE_DIR.mkdir(exist_ok=True)
        _hist_cache_path(pid, season).write_text(json.dumps(value), encoding="utf-8")
    except Exception:
        pass


# ── API fetchers ─────────────────────────────────────────────────────────────

def _fetch_home_away_splits(pid: int, season: int) -> tuple[Optional[float], Optional[float]]:
    """Return (home_era, away_era) for the pitcher's given season.  None on miss."""
    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=statSplits&group=pitching&season={season}&sportId=1"
           f"&sitCodes=h,a")
    data = _fetch_url_ua(url)
    home_era = away_era = None
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            code = split.get("split", {}).get("code", "")
            era  = _safe(split.get("stat", {}).get("era"), None)
            if era is None:
                continue
            if code == "h":
                home_era = era
            elif code == "a":
                away_era = era
    return home_era, away_era


def _fetch_season_bb_per_9(pid: int, season: int) -> Optional[float]:
    """Return walks per 9 IP for the pitcher's season."""
    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=season&season={season}&group=pitching&sportId=1")
    data = _fetch_url_ua(url)
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            st = split.get("stat", {})
            # Prefer pre-computed walksPer9Inn if present.
            bb9 = _safe(st.get("walksPer9Inn"), None)
            if bb9 is not None:
                return bb9
            # Compute from raw counts.
            walks  = _safe(st.get("baseOnBalls"), None)
            ip_str = st.get("inningsPitched")
            try:
                ip = float(ip_str) if ip_str is not None else None
            except (TypeError, ValueError):
                ip = None
            if walks is not None and ip is not None and ip > 0:
                return walks * 9.0 / ip
    return None


def _fetch_game_log_eras(pid: int, season: int) -> list[tuple[str, float, float]]:
    """
    Return [(date, earned_runs, innings_pitched), ...] across all starts in
    chronological order.  Used to compute last-3-starts ERA trend.
    """
    url = (f"{_BASE}/people/{pid}/stats"
           f"?stats=gameLog&group=pitching&season={season}&sportId=1")
    data = _fetch_url_ua(url)
    starts: list[tuple[str, float, float]] = []
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            st = split.get("stat", {})
            try:
                # Only count games started (skip relief).
                if not int(st.get("gamesStarted", 0) or 0):
                    continue
                er = _safe(st.get("earnedRuns"), 0.0)
                ip = float(st.get("inningsPitched") or 0)
                game_date = split.get("date", "")
                if ip > 0 and game_date:
                    starts.append((game_date, er, ip))
            except (TypeError, ValueError):
                continue
    starts.sort(key=lambda r: r[0])
    return starts


def _compute_last3_metrics(
    starts: list[tuple[str, float, float]],
    cutoff_date: Optional[str] = None,
) -> tuple[float, float]:
    """
    From a chronological starts list, return (last3_era, last3_era_change).
    If cutoff_date is set, only consider starts strictly before it (so
    historical training rows don't peek at the same-day stat line).
    last3_era_change = last3_era - prev3_era  (negative = improving).
    """
    if cutoff_date:
        starts = [s for s in starts if s[0] < cutoff_date]
    if len(starts) < 3:
        return _NEUTRAL_SPLITS["last3_era"], 0.0

    def era_of(slc):
        er  = sum(r[1] for r in slc)
        ip  = sum(r[2] for r in slc)
        return (er * 9.0 / ip) if ip > 0 else _NEUTRAL_SPLITS["last3_era"]

    last3 = starts[-3:]
    last3_era = era_of(last3)

    if len(starts) >= 6:
        prev3 = starts[-6:-3]
        change = last3_era - era_of(prev3)
    else:
        change = 0.0

    return last3_era, change


# ── Combined fetch  ──────────────────────────────────────────────────────────

def _fetch_full_splits(
    pid: int,
    season: int,
    cutoff_date: Optional[str] = None,
) -> dict:
    """Hit all three endpoints and combine.  Falls back to neutral on miss."""
    home_era, away_era = _fetch_home_away_splits(pid, season)
    bb9 = _fetch_season_bb_per_9(pid, season)
    starts = _fetch_game_log_eras(pid, season)
    last3_era, last3_change = _compute_last3_metrics(starts, cutoff_date)

    return {
        "home_era":         home_era if home_era is not None else _NEUTRAL_SPLITS["home_era"],
        "away_era":         away_era if away_era is not None else _NEUTRAL_SPLITS["away_era"],
        "bb_per_9":         bb9      if bb9      is not None else _NEUTRAL_SPLITS["bb_per_9"],
        "last3_era":        last3_era,
        "last3_era_change": last3_change,
    }


# ── Public API ───────────────────────────────────────────────────────────────

class PitcherSplitsClient:
    """Daily-cached extended pitcher stats for live prediction."""

    def __init__(self):
        self._cache = _load_live_cache()
        self._dirty = False

    def get_splits(self, pitcher_id: Optional[int], season: int) -> dict:
        """Return the full splits dict; neutral defaults on any failure."""
        if not pitcher_id:
            return dict(_NEUTRAL_SPLITS)
        key = f"p_{pitcher_id}_{season}"
        if key in self._cache:
            return self._cache[key]
        try:
            result = _fetch_full_splits(pitcher_id, season)
        except Exception:
            result = dict(_NEUTRAL_SPLITS)
        self._cache[key] = result
        self._dirty = True
        return result

    def save(self) -> None:
        if self._dirty:
            _save_live_cache(self._cache)
            self._dirty = False


def get_historical_splits(
    pitcher_id: Optional[int],
    season: int,
    cutoff_date: Optional[str] = None,
) -> dict:
    """
    Historical fetch path used by enriched_historical_data.py.
    Caches per (pid, season) to disk; cutoff_date controls last-3 metric
    so a row dated 2024-06-15 only sees that pitcher's starts before it.
    """
    if not pitcher_id:
        return dict(_NEUTRAL_SPLITS)

    # Hist cache stores the per-season raw building blocks; last3 metrics
    # are re-derived with the row's cutoff_date from the cached starts.
    cached = _hist_cache_get(pitcher_id, season)
    if cached is not None and "starts" in cached:
        last3_era, last3_change = _compute_last3_metrics(
            [(s[0], s[1], s[2]) for s in cached["starts"]], cutoff_date
        )
        return {
            "home_era":         cached.get("home_era", _NEUTRAL_SPLITS["home_era"]),
            "away_era":         cached.get("away_era", _NEUTRAL_SPLITS["away_era"]),
            "bb_per_9":         cached.get("bb_per_9", _NEUTRAL_SPLITS["bb_per_9"]),
            "last3_era":        last3_era,
            "last3_era_change": last3_change,
        }

    try:
        home_era, away_era = _fetch_home_away_splits(pitcher_id, season)
        bb9    = _fetch_season_bb_per_9(pitcher_id, season)
        starts = _fetch_game_log_eras(pitcher_id, season)
    except Exception:
        # On any unexpected error, cache the miss as neutral and move on.
        _hist_cache_set(pitcher_id, season, {
            "home_era": _NEUTRAL_SPLITS["home_era"],
            "away_era": _NEUTRAL_SPLITS["away_era"],
            "bb_per_9": _NEUTRAL_SPLITS["bb_per_9"],
            "starts":   [],
        })
        return dict(_NEUTRAL_SPLITS)

    _hist_cache_set(pitcher_id, season, {
        "home_era": home_era if home_era is not None else _NEUTRAL_SPLITS["home_era"],
        "away_era": away_era if away_era is not None else _NEUTRAL_SPLITS["away_era"],
        "bb_per_9": bb9      if bb9      is not None else _NEUTRAL_SPLITS["bb_per_9"],
        "starts":   starts,
    })

    last3_era, last3_change = _compute_last3_metrics(starts, cutoff_date)
    return {
        "home_era":         home_era if home_era is not None else _NEUTRAL_SPLITS["home_era"],
        "away_era":         away_era if away_era is not None else _NEUTRAL_SPLITS["away_era"],
        "bb_per_9":         bb9      if bb9      is not None else _NEUTRAL_SPLITS["bb_per_9"],
        "last3_era":        last3_era,
        "last3_era_change": last3_change,
    }


_client: Optional[PitcherSplitsClient] = None


def get_pitcher_splits_client() -> PitcherSplitsClient:
    global _client
    if _client is None:
        _client = PitcherSplitsClient()
    return _client
