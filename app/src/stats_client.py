"""
UNUSED — NFL-only API-Sports American Football client.
NFL support has been removed; active sports are MLB and WNBA only.
This file is kept for reference but is not imported anywhere in the app.
"""
import requests
from typing import Optional
from .cache import Cache

BASE_URL = "https://v1.american-football.api-sports.io"
NFL_LEAGUE = 1

# Position impact weights for injury scoring (higher = more impactful loss)
_POSITION_WEIGHTS: dict[str, float] = {
    "QB": 0.35,
    "WR": 0.10,
    "TE": 0.08,
    "RB": 0.07,
    "LT": 0.09, "LG": 0.06, "C": 0.06, "RG": 0.06, "RT": 0.09,
    "DE": 0.08, "DT": 0.06,
    "LB": 0.07, "MLB": 0.07, "OLB": 0.06,
    "CB": 0.07, "FS": 0.06, "SS": 0.06, "S": 0.06,
}

# API-Sports injury status strings that mean a player is OUT/doubtful
_OUT_STATUSES = {"Out", "Doubtful", "Injured Reserve", "IR"}


class StatsClient:
    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.cache = cache or Cache()
        self.session = requests.Session()
        self.session.headers.update({
            "x-apisports-key": api_key,
        })
        self._team_index: Optional[dict[str, dict]] = None  # name → team record

    def _get(self, path: str, params: dict) -> dict:
        cache_key = f"sports_{path}_{sorted(params.items())}"
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
        """Cache a name→team mapping for the given season."""
        data = self._get("/teams", {"league": NFL_LEAGUE, "season": season})
        self._team_index = {}
        for item in data.get("response", []):
            team = item.get("team", item)
            name = team.get("name", "")
            self._team_index[name.lower()] = team

    def find_team(self, name: str, season: int) -> Optional[dict]:
        """
        Return the API-Sports team dict matching *name* (fuzzy).
        Tries exact match, then token-based overlap.
        """
        if self._team_index is None:
            self._build_team_index(season)

        needle = name.lower()

        # Exact match
        if needle in self._team_index:
            return self._team_index[needle]

        # City/nickname token match — pick best overlap
        needle_tokens = set(needle.split())
        best, best_score = None, 0
        for key, team in self._team_index.items():
            key_tokens = set(key.split())
            score = len(needle_tokens & key_tokens)
            if score > best_score:
                best, best_score = team, score

        return best if best_score >= 1 else None

    # ------------------------------------------------------------------
    # Season stats
    # ------------------------------------------------------------------

    def get_team_stats(self, team_id: int, season: int) -> Optional[dict]:
        """Return the season-level statistics dict for one team, or None."""
        data = self._get("/teams/statistics", {
            "league": NFL_LEAGUE,
            "season": season,
            "team": team_id,
        })
        resp = data.get("response")
        return resp if resp else None

    # ------------------------------------------------------------------
    # Injuries
    # ------------------------------------------------------------------

    def get_injuries(self, team_id: int, season: int) -> list[dict]:
        """Return the current injury report for a team."""
        data = self._get("/injuries", {
            "league": NFL_LEAGUE,
            "season": season,
            "team": team_id,
        })
        return data.get("response", [])

    def injury_health_score(self, team_id: int, season: int) -> float:
        """
        Score in [0, 1] representing team health.
        1.0 = fully healthy, 0.0 = catastrophically injured.
        """
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

    # ------------------------------------------------------------------
    # Completed games (for training data)
    # ------------------------------------------------------------------

    def get_completed_games(self, season: int) -> list[dict]:
        """Return all completed regular-season NFL games for the given season."""
        data = self._get("/games", {
            "league": NFL_LEAGUE,
            "season": season,
        })
        games = data.get("response", [])
        completed = [
            g for g in games
            if g.get("game", {}).get("status", {}).get("short") in ("FT", "AET")
        ]
        return completed
