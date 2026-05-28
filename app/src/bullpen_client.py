"""
Fetches MLB bullpen ERA and recent-game fatigue via the free MLB Stats API
(statsapi.mlb.com — no key required).

bullpen ERA:  overall team pitching ERA for the season (starter + reliever
              aggregate is a reliable proxy for bullpen quality)
fatigue:      number of team games played in the 5 days before game_date
              (higher count = bullpen used more recently)
"""
from __future__ import annotations

import logging
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_BASE = "https://statsapi.mlb.com/api/v1"
_CACHE_FILE = Path(".cache/bullpen_cache.json")
_CACHE_TTL = 3600  # 1 hour

_NEUTRAL = {"era": 4.20, "fatigue": 2}

# Shared helpers — imported from utils instead of defined locally
from .utils import _safe, _team_tokens, _fetch_url as _fetch  # noqa: E402
from .sports_config import CURRENT_SEASON                       # noqa: E402


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _CACHE_TTL:
                return raw
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


class BullpenClient:
    """Caches bullpen data for today to avoid repeated API calls."""

    def __init__(self):
        self._cache = _load_cache()
        self._dirty = False
        self._mlb_teams: dict[str, int] = {}  # canonical name → mlb team id

    # ── Public API ────────────────────────────────────────────────────────────

    def get_bullpen_for_game(
        self,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
    ) -> dict:
        """
        Return:
          {
            "home": {"era": float, "fatigue": int},
            "away": {"era": float, "fatigue": int},
          }
        fatigue = games played in the 5 days before game_date (proxy for recent
                  bullpen workload — higher = more tired).
        """
        date_str = game_date or date.today().isoformat()

        home_id = self._resolve_team_id(home_team)
        away_id = self._resolve_team_id(away_team)

        home_data = self._stats_for_team(home_id, date_str) if home_id else dict(_NEUTRAL)
        away_data = self._stats_for_team(away_id, date_str) if away_id else dict(_NEUTRAL)

        if self._dirty:
            _save_cache(self._cache)
            self._dirty = False

        return {"home": home_data, "away": away_data}

    def save(self) -> None:
        if self._dirty:
            _save_cache(self._cache)
            self._dirty = False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_team_id(self, team_name: str) -> Optional[int]:
        cache_key = f"tid_{team_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if not self._mlb_teams:
            self._load_all_teams()

        tokens = _team_tokens(team_name)
        best_id, best_n = None, 0
        for name, tid in self._mlb_teams.items():
            n = len(tokens & _team_tokens(name))
            if n > best_n:
                best_id, best_n = tid, n

        if best_id is not None:
            self._cache[cache_key] = best_id
            self._dirty = True
        return best_id

    def _load_all_teams(self) -> None:
        data = _fetch(f"{_BASE}/teams?sportId=1&season={CURRENT_SEASON}")
        for team in data.get("teams", []):
            name = team.get("name") or team.get("teamName") or ""
            tid  = team.get("id")
            if name and tid:
                self._mlb_teams[name] = tid
                self._mlb_teams[team.get("teamName", name)] = tid

    def _stats_for_team(self, team_id: int, date_str: str) -> dict:
        year = date_str[:4]
        era  = self._get_team_era(team_id, year)
        fat  = self._get_recent_game_count(team_id, date_str)
        return {"era": era, "fatigue": fat}

    def _get_team_era(self, team_id: int, year: str) -> float:
        cache_key = f"era_{team_id}_{year}"
        if cache_key in self._cache:
            return float(self._cache[cache_key])

        url = (
            f"{_BASE}/teams/{team_id}/stats"
            f"?stats=season&group=pitching&season={year}&sportId=1"
        )
        data = _fetch(url)
        era: Optional[float] = None
        for grp in data.get("stats", []):
            for split in grp.get("splits", []):
                raw = split.get("stat", {}).get("era")
                if raw is not None:
                    try:
                        era = float(raw)
                    except (TypeError, ValueError):
                        pass
                    break
            if era is not None:
                break

        era = era if era is not None else _NEUTRAL["era"]
        self._cache[cache_key] = era
        self._dirty = True
        return era

    def _get_recent_game_count(self, team_id: int, date_str: str) -> int:
        """Count completed games in the 5 days ending the day before game_date."""
        try:
            end   = date.fromisoformat(date_str) - timedelta(days=1)
            start = end - timedelta(days=4)
        except Exception:
            return _NEUTRAL["fatigue"]

        cache_key = f"fat_{team_id}_{date_str}"
        if cache_key in self._cache:
            return int(self._cache[cache_key])

        url = (
            f"{_BASE}/schedule?sportId=1&teamId={team_id}"
            f"&startDate={start.isoformat()}&endDate={end.isoformat()}"
            f"&gameType=R"
        )
        data  = _fetch(url)
        count = sum(len(d.get("games", [])) for d in data.get("dates", []))

        self._cache[cache_key] = count
        self._dirty = True
        return count


# Module-level singleton
_client: Optional[BullpenClient] = None


def get_bullpen_client() -> BullpenClient:
    global _client
    if _client is None:
        _client = BullpenClient()
    return _client
