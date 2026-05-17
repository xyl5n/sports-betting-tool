"""
API-Sports Baseball client.
Docs: https://api-sports.io/documentation/baseball/v1
Base URL: https://v1.baseball.api-sports.io
"""
import requests
from typing import Optional
from .cache import Cache

BASE_URL = "https://v1.baseball.api-sports.io"

MLB_LEAGUE = 1       # MLB
MLB_SEASON = 2025    # current / most recent season

# Injury position weights for baseball (scaled to baseball roster importance)
_POSITION_WEIGHTS: dict[str, float] = {
    "SP": 0.30,   # starting pitcher — huge impact
    "RP": 0.08,   # relief pitcher
    "CL": 0.12,   # closer
    "C":  0.10,   # catcher
    "1B": 0.06, "2B": 0.06, "SS": 0.08, "3B": 0.07,
    "LF": 0.06, "CF": 0.08, "RF": 0.06,
    "DH": 0.06,
}

_OUT_STATUSES = {"Day-To-Day", "10-Day IL", "15-Day IL", "60-Day IL", "Out"}


class MLBStatsClient:
    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.cache = cache or Cache()
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": api_key})
        self._team_index: Optional[dict[str, dict]] = None

    def _get(self, path: str, params: dict) -> dict:
        cache_key = f"mlb_{path}_{sorted(params.items())}"
        cached = self.cache.get(cache_key, ttl=3600 * 6)
        if cached is not None:
            return cached
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self.cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    # Team lookup
    # ------------------------------------------------------------------

    def _build_team_index(self, season: int) -> None:
        data = self._get("/teams", {"league": MLB_LEAGUE, "season": season})
        self._team_index = {}
        for item in data.get("response", []):
            team = item.get("team", item)
            name = (team.get("name") or "").lower()
            if name:
                self._team_index[name] = team

    def find_team(self, name: str, season: int) -> Optional[dict]:
        if self._team_index is None:
            self._build_team_index(season)

        needle = name.lower()
        if needle in self._team_index:
            return self._team_index[needle]

        needle_tokens = set(needle.split())
        best, best_score = None, 0
        for key, team in self._team_index.items():
            score = len(needle_tokens & set(key.split()))
            if score > best_score:
                best, best_score = team, score

        return best if best_score >= 1 else None

    # ------------------------------------------------------------------
    # Season stats
    # ------------------------------------------------------------------

    def get_team_stats(self, team_id: int, season: int) -> Optional[dict]:
        """Return season statistics for one MLB team."""
        data = self._get("/teams/statistics", {
            "league": MLB_LEAGUE,
            "season": season,
            "team": team_id,
        })
        return data.get("response") or None

    def get_standings(self, season: int) -> list[dict]:
        """Return full standings (includes win%, last-10 record)."""
        data = self._get("/standings", {
            "league": MLB_LEAGUE,
            "season": season,
        })
        return data.get("response", [])

    # ------------------------------------------------------------------
    # Game / pitcher data
    # ------------------------------------------------------------------

    def get_today_games(self, date: str, season: int) -> list[dict]:
        """
        Return today's scheduled games.
        date format: YYYY-MM-DD
        """
        data = self._get("/games", {
            "league": MLB_LEAGUE,
            "season": season,
            "date": date,
        })
        return data.get("response", [])

    def get_game_players(self, game_id: int) -> list[dict]:
        """Get starting lineup + pitchers for a game."""
        data = self._get("/games/players", {"id": game_id})
        return data.get("response", [])

    def get_completed_games(self, season: int) -> list[dict]:
        """All completed regular-season MLB games for the given season."""
        data = self._get("/games", {
            "league": MLB_LEAGUE,
            "season": season,
        })
        return [
            g for g in data.get("response", [])
            if g.get("status", {}).get("short") in ("FT", "AOT")
        ]

    # ------------------------------------------------------------------
    # Injuries
    # ------------------------------------------------------------------

    def get_injuries(self, team_id: int, season: int) -> list[dict]:
        data = self._get("/injuries", {
            "league": MLB_LEAGUE,
            "season": season,
            "team": team_id,
        })
        return data.get("response", [])

    def injury_health_score(self, team_id: int, season: int) -> float:
        injuries = self.get_injuries(team_id, season)
        total_impact = 0.0
        for record in injuries:
            player = record.get("player", {})
            status = record.get("injury", {}).get("status", "")
            if status not in _OUT_STATUSES:
                continue
            pos = player.get("position", "").upper().strip()
            weight = _POSITION_WEIGHTS.get(pos, 0.04)
            total_impact += weight
        return max(0.0, 1.0 - total_impact)
