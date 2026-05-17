"""
The Odds API client — fetches baseball (MLB) and basketball (WNBA) markets only.
Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
import requests
from typing import Optional
from .cache import Cache

BASE_URL = "https://api.the-odds-api.com/v4"


def _american_to_prob(american: int) -> float:
    """Convert American moneyline to raw implied probability (0-1)."""
    if american > 0:
        return 100 / (american + 100)
    return abs(american) / (abs(american) + 100)


def _remove_vig(home_prob: float, away_prob: float) -> tuple[float, float]:
    """Strip bookmaker vig so probabilities sum to 1."""
    total = home_prob + away_prob
    return home_prob / total, away_prob / total


class OddsClient:
    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = requests.Session()

    def _get(self, path: str, params: dict) -> dict | list:
        params["apiKey"] = self.api_key
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=15)
        resp.raise_for_status()
        self._log_quota(resp)
        return resp.json()

    @staticmethod
    def _log_quota(resp: requests.Response) -> None:
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        if remaining != "?":
            print(f"  [Odds API] requests used={used}, remaining={remaining}")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_odds(
        self,
        sport_key: str,
        markets: str = "h2h,spreads,totals",
        regions: str = "us",
    ) -> list[dict]:
        """Return upcoming games for *sport_key* with implied probabilities.

        sport_key must be an active Odds API sport key, e.g.:
          "baseball_mlb"      — MLB moneylines / run lines / totals
          "basketball_wnba"   — WNBA moneylines / spreads / totals
        """
        cache_key = f"odds_{sport_key}_{markets}_{regions}"
        cached = self.cache.get(cache_key, ttl=900)  # 15-min TTL
        if cached is not None:
            return cached

        raw = self._get(f"/sports/{sport_key}/odds/", {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
        })

        games = [self._parse_game(g) for g in raw]
        games = [g for g in games if g is not None]
        self.cache.set(cache_key, games)
        return games

    def get_scores(self, sport_key: str, days_from: int = 3) -> list[dict]:
        """Return recently completed games (free tier = 3 days)."""
        cache_key = f"scores_{sport_key}_{days_from}"
        cached = self.cache.get(cache_key, ttl=3600)
        if cached is not None:
            return cached

        raw = self._get(f"/sports/{sport_key}/scores/", {"daysFrom": days_from})
        completed = [g for g in raw if g.get("completed")]
        self.cache.set(cache_key, completed)
        return completed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_game(self, game: dict) -> Optional[dict]:
        """Flatten a raw Odds API game into a simpler structure."""
        home = game["home_team"]
        away = game["away_team"]

        h2h_home_odds = h2h_away_odds = None
        spread = None
        rl_home_odds = rl_away_odds = rl_point = None
        over_odds = under_odds = total_line = None

        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market["key"] == "h2h" and h2h_home_odds is None:
                    outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
                    h2h_home_odds = outcomes.get(home)
                    h2h_away_odds = outcomes.get(away)
                elif market["key"] == "spreads" and spread is None:
                    for o in market["outcomes"]:
                        if o["name"] == home:
                            spread        = o.get("point")
                            rl_home_odds  = o.get("price")
                            rl_point      = o.get("point")
                        elif o["name"] == away:
                            rl_away_odds  = o.get("price")
                elif market["key"] == "totals" and total_line is None:
                    for o in market["outcomes"]:
                        if o["name"] == "Over":
                            over_odds  = o.get("price")
                            total_line = o.get("point")
                        elif o["name"] == "Under":
                            under_odds = o.get("price")

        if h2h_home_odds is None or h2h_away_odds is None:
            return None

        raw_home = _american_to_prob(h2h_home_odds)
        raw_away = _american_to_prob(h2h_away_odds)
        home_prob, away_prob = _remove_vig(raw_home, raw_away)

        return {
            "id": game["id"],
            "commence_time": game["commence_time"],
            "home_team": home,
            "away_team": away,
            "h2h_home_odds":    h2h_home_odds,
            "h2h_away_odds":    h2h_away_odds,
            "home_implied_prob": round(home_prob, 4),
            "away_implied_prob": round(away_prob, 4),
            "spread":            spread,           # moneyline spread point
            "run_line_home_odds": rl_home_odds,    # ATS home odds (usually -115)
            "run_line_away_odds": rl_away_odds,    # ATS away odds
            "run_line_point":    rl_point,         # -1.5 (home favored by 1.5)
            "over_odds":         over_odds,        # O/U over odds
            "under_odds":        under_odds,       # O/U under odds
            "total_line":        total_line,       # posted O/U number (e.g. 8.5)
        }
