"""
Fetches probable MLB starting pitchers and their season stats via the
free MLB Stats API (statsapi.mlb.com — no key required).

Usage:
    client = PitcherClient()
    data = client.get_starters_for_game("New York Yankees", "Boston Red Sox", "2026-05-13")
    # data = {"home": {...}, "away": {...}}  or None on failure
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

_BASE = "https://statsapi.mlb.com/api/v1"
_CACHE_FILE = Path(".cache/pitcher_cache.json")
_CACHE_TTL = 3600  # 1 hour

_NEUTRAL_PITCHER = {
    "era": 4.50,
    "whip": 1.30,
    "k_rate": 0.215,
    "hand": 0,    # 0 = RHP, 1 = LHP
    "rest": 4,
}

# Shared helpers — imported from utils instead of defined locally
from .utils import _safe, _team_tokens, _fetch_url as _fetch  # noqa: E402


def _load_disk_cache() -> dict:  # noqa: E302
    try:
        if _CACHE_FILE.exists():
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _CACHE_TTL:
                return raw
    except Exception:
        pass
    return {}


def _save_disk_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _parse_rest(note: str) -> int:
    """Extract days of rest from MLB Stats note string like '3 Days Rest'."""
    m = re.search(r"(\d+)\s*[Dd]ay", note or "")
    return int(m.group(1)) if m else 4


class PitcherClient:
    """Caches pitcher data for today to avoid repeated API calls."""

    def __init__(self):
        self._cache = _load_disk_cache()
        self._dirty = False

    # ── Public API ────────────────────────────────────────────────────────────

    def get_starters_for_game(
        self,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
    ) -> dict:
        """
        Return pitcher feature dict for one game:
        {
            "home": {"era": float, "whip": float, "k_rate": float, "hand": int, "rest": int},
            "away": {...},
        }
        Returns neutral values for any unavailable fields.
        """
        date_str = game_date or date.today().isoformat()
        schedule = self._get_schedule(date_str)

        home_stats = away_stats = None

        for entry in schedule:
            h_name = entry.get("home_name", "")
            a_name = entry.get("away_name", "")
            # Match by token overlap (handles minor name differences)
            h_overlap = len(_team_tokens(h_name) & _team_tokens(home_team))
            a_overlap = len(_team_tokens(a_name) & _team_tokens(away_team))
            if h_overlap >= 1 and a_overlap >= 1:
                home_stats = self._pitcher_stats(entry.get("home_pitcher"))
                away_stats = self._pitcher_stats(entry.get("away_pitcher"))
                break

        return {
            "home": home_stats or dict(_NEUTRAL_PITCHER),
            "away": away_stats or dict(_NEUTRAL_PITCHER),
        }

    def save(self) -> None:
        if self._dirty:
            _save_disk_cache(self._cache)
            self._dirty = False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_schedule(self, date_str: str) -> list[dict]:
        cache_key = f"sched_{date_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = (
            f"{_BASE}/schedule?sportId=1&date={date_str}"
            f"&hydrate=probablePitcher(note,pitchHand)"
        )
        data = _fetch(url)
        entries = []

        for day in data.get("dates", []):
            for game in day.get("games", []):
                teams = game.get("teams", {})
                entries.append({
                    "game_pk": game.get("gamePk"),
                    "home_name": teams.get("home", {}).get("team", {}).get("name", ""),
                    "away_name": teams.get("away", {}).get("team", {}).get("name", ""),
                    "home_pitcher": teams.get("home", {}).get("probablePitcher"),
                    "away_pitcher": teams.get("away", {}).get("probablePitcher"),
                })

        self._cache[cache_key] = entries
        self._dirty = True
        return entries

    def _pitcher_stats(self, pitcher_info: Optional[dict]) -> Optional[dict]:
        if not pitcher_info:
            return None

        pid = pitcher_info.get("id")
        if not pid:
            return None

        cache_key = f"p_{pid}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{_BASE}/people/{pid}/stats?stats=season&group=pitching&sportId=1"
        data = _fetch(url)

        era = whip = k_rate = None
        for grp in data.get("stats", []):
            for split in grp.get("splits", []):
                st = split.get("stat", {})
                era   = _safe(st.get("era"), None)
                whip  = _safe(st.get("whip"), None)
                k     = _safe(st.get("strikeOuts"), 0)
                bf    = _safe(st.get("battersFaced"), 1)
                k_rate = k / bf if bf > 0 else None
                break
            if era is not None:
                break

        hand_code = (
            pitcher_info.get("pitchHand", {}).get("code", "R")
            if isinstance(pitcher_info.get("pitchHand"), dict)
            else "R"
        )

        result = {
            "era":    era    if era    is not None else 4.50,
            "whip":   whip   if whip   is not None else 1.30,
            "k_rate": k_rate if k_rate is not None else 0.215,
            "hand":   1 if hand_code == "L" else 0,
            "rest":   _parse_rest(pitcher_info.get("note", "")),
        }

        self._cache[cache_key] = result
        self._dirty = True
        return result


# Module-level singleton so we don't re-instantiate on every call
_client: Optional[PitcherClient] = None


def get_pitcher_client() -> PitcherClient:
    global _client
    if _client is None:
        _client = PitcherClient()
    return _client
