"""
Checks whether confirmed starting lineups are available for MLB games via
the free MLB Stats API (statsapi.mlb.com — no key required).

Returns 1.0 when both the home and away lineups for a game are officially
confirmed, 0.0 when either is still projected or unavailable.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Optional

_BASE = "https://statsapi.mlb.com/api/v1"
_CACHE_FILE = Path(".cache/lineup_cache.json")
_CACHE_TTL = 900  # 15 minutes — lineups drop late in the day

# Shared helpers — imported from utils instead of defined locally
from .utils import _team_tokens, _fetch_url as _fetch  # noqa: E402


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _CACHE_TTL:
                return raw
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


class LineupClient:
    """Checks lineup confirmation status for today's games."""

    def __init__(self):
        self._cache = _load_cache()
        self._dirty = False

    # ── Public API ────────────────────────────────────────────────────────────

    def is_lineup_confirmed(
        self,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
    ) -> float:
        """
        Return 1.0 if both starting lineups are confirmed for this matchup,
        0.0 if either is still projected or unavailable.
        """
        date_str = game_date or date.today().isoformat()
        games = self._get_lineup_schedule(date_str)

        for entry in games:
            h_overlap = len(_team_tokens(entry["home_name"]) & _team_tokens(home_team))
            a_overlap = len(_team_tokens(entry["away_name"]) & _team_tokens(away_team))
            if h_overlap >= 1 and a_overlap >= 1:
                return 1.0 if (entry["home_confirmed"] and entry["away_confirmed"]) else 0.0

        return 0.0

    def save(self) -> None:
        if self._dirty:
            _save_cache(self._cache)
            self._dirty = False

    def get_lineups(
        self,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
    ) -> dict:
        """Return the actual batting orders for this matchup:

            {"confirmed": bool,
             "home": [{"id", "name", "order", "position", "bats"}],
             "away": [...]}

        ``confirmed`` mirrors is_lineup_confirmed (both sides posted).  The
        player lists carry whatever the schedule's hydrate=lineups feed
        provides (in batting order); empty lists when the lineup isn't out
        yet.  Callers fall back to a probable lineup when these are empty."""
        date_str = game_date or date.today().isoformat()
        games = self._get_lineup_schedule(date_str)
        for entry in games:
            h_overlap = len(_team_tokens(entry["home_name"]) & _team_tokens(home_team))
            a_overlap = len(_team_tokens(entry["away_name"]) & _team_tokens(away_team))
            if h_overlap >= 1 and a_overlap >= 1:
                return {
                    "confirmed": bool(entry["home_confirmed"] and entry["away_confirmed"]),
                    "home": entry.get("home_lineup") or [],
                    "away": entry.get("away_lineup") or [],
                }
        return {"confirmed": False, "home": [], "away": []}

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_lineup(players: list) -> list[dict]:
        """hydrate=lineups player objects -> ordered batting-order rows."""
        out: list[dict] = []
        for i, p in enumerate(players or []):
            if not isinstance(p, dict):
                continue
            pos = (p.get("primaryPosition") or {}).get("abbreviation", "")
            bats = (p.get("batSide") or {}).get("code", "")
            out.append({
                "id":       p.get("id"),
                "name":     p.get("fullName", ""),
                "order":    i + 1,
                "position": pos,
                "bats":     bats,
            })
        return out

    def _get_lineup_schedule(self, date_str: str) -> list[dict]:
        cache_key = f"lineups_{date_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = (
            f"{_BASE}/schedule?sportId=1&date={date_str}"
            f"&hydrate=lineups"
        )
        data = _fetch(url)
        entries: list[dict] = []

        for day in data.get("dates", []):
            for game in day.get("games", []):
                teams    = game.get("teams", {})
                lineups  = game.get("lineups", {})
                home_bat = lineups.get("homePlayers", [])
                away_bat = lineups.get("awayPlayers", [])
                entries.append({
                    "home_name":      teams.get("home", {}).get("team", {}).get("name", ""),
                    "away_name":      teams.get("away", {}).get("team", {}).get("name", ""),
                    "home_confirmed": len(home_bat) >= 8,
                    "away_confirmed": len(away_bat) >= 8,
                    "home_lineup":    self._parse_lineup(home_bat),
                    "away_lineup":    self._parse_lineup(away_bat),
                })

        self._cache[cache_key] = entries
        self._dirty = True
        return entries


# Module-level singleton
_client: Optional[LineupClient] = None


def get_lineup_client() -> LineupClient:
    global _client
    if _client is None:
        _client = LineupClient()
    return _client
