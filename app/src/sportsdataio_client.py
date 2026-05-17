"""
sportsdataio_client.py
=======================
SportsDataIO REST API client for MLB and WNBA supplemental data.

Provides: team stats, standings, rosters, and player stats for both MLB and WNBA.

DATA VALIDATION
---------------
SportsDataIO's free trial may return scrambled/demo data.  Every numeric value
fetched is compared against the corresponding value from the primary source
(API-Sports / ESPN / stats.wnba.com).  If any value differs by more than 50%
from the primary source baseline it is replaced with the primary value.
Use merge_with_validation() to apply this safely.

ENVIRONMENT
-----------
SPORTSDATAIO_API_KEY -- get a free trial at https://sportsdata.io/cart/free-trial

If the key is absent all methods return empty collections immediately.

ENDPOINT REFERENCE
------------------
MLB:  https://api.sportsdata.io/v3/mlb/scores/json/Teams
      https://api.sportsdata.io/v3/mlb/scores/json/Standings/{year}
      https://api.sportsdata.io/v3/mlb/scores/json/Players
      https://api.sportsdata.io/v3/mlb/stats/json/PlayerSeasonStats/{year}
      https://api.sportsdata.io/v3/mlb/stats/json/TeamSeasonStats/{year}
WNBA: https://api.sportsdata.io/v3/wnba/scores/json/Teams
      https://api.sportsdata.io/v3/wnba/scores/json/Standings/{year}
      https://api.sportsdata.io/v3/wnba/scores/json/Players
      https://api.sportsdata.io/v3/wnba/stats/json/PlayerSeasonStats/{year}
      https://api.sportsdata.io/v3/wnba/stats/json/TeamSeasonStats/{year}
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

from .cache import Cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MLB_BASE  = "https://api.sportsdata.io/v3/mlb"
_WNBA_BASE = "https://api.sportsdata.io/v3/wnba"

# Maximum fractional deviation allowed from primary source before a value is
# considered scrambled and replaced with the primary baseline.
_MAX_DEVIATION = 0.50   # 50 %


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_numeric(sdio_val: Any, primary_val: Any) -> Any:
    """
    Return *sdio_val* if it is within _MAX_DEVIATION of *primary_val*,
    otherwise return *primary_val*.  Non-numeric or None values pass through.
    """
    if primary_val is None or sdio_val is None:
        return sdio_val
    try:
        sv = float(sdio_val)
        pv = float(primary_val)
        if pv == 0:
            # Avoid division-by-zero; allow only if sdio is also ~zero
            return sdio_val if abs(sv) < 1.0 else primary_val
        if abs(sv - pv) / abs(pv) > _MAX_DEVIATION:
            return primary_val
        return sdio_val
    except (TypeError, ValueError):
        return sdio_val


def merge_with_validation(
    sdio_record: dict,
    primary_record: dict,
    numeric_keys: list[str],
) -> dict:
    """
    Return a copy of *sdio_record* where each key in *numeric_keys* has been
    validated against the corresponding value in *primary_record*.  Keys not
    in *numeric_keys* are left untouched.
    """
    merged = dict(sdio_record)
    for key in numeric_keys:
        if key in merged:
            merged[key] = _validate_numeric(merged[key], primary_record.get(key))
    return merged


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SportsDataIOClient:
    """
    Thin wrapper around the SportsDataIO v3 REST API.

    Usage::

        from src.sportsdataio_client import SportsDataIOClient
        sdio = SportsDataIOClient()
        teams  = sdio.mlb_teams()
        stats  = sdio.mlb_team_season_stats(2024)
        tstats = sdio.wnba_team_season_stats(2024)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        self._key   = api_key or os.environ.get("SPORTSDATAIO_API_KEY", "")
        self._cache = cache or Cache()
        self._sess  = requests.Session()
        self._sess.headers.update({
            "Accept":     "application/json",
            "User-Agent": "sports-betting-ai/1.0",
        })

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get(self, url: str, cache_key: str, ttl: int = 3600) -> Any:
        """
        GET *url*, returning the parsed JSON body.
        Results are cached for *ttl* seconds.
        Returns None on any error (missing key, network failure, bad JSON).
        """
        if not self._key:
            return None

        cached = self._cache.get(cache_key, ttl=ttl)
        if cached is not None:
            return cached

        try:
            resp = self._sess.get(
                url,
                params={"key": self._key},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"  [sdio] HTTP {status} fetching {url}")
            return None
        except Exception as exc:
            print(f"  [sdio] Request failed ({exc}) for {url}")
            return None

        self._cache.set(cache_key, data)
        return data

    # ------------------------------------------------------------------
    # MLB endpoints
    # ------------------------------------------------------------------

    def mlb_teams(self) -> list[dict]:
        """Return all MLB teams.  Cached for 24 h."""
        data = self._get(
            f"{_MLB_BASE}/scores/json/Teams",
            cache_key="sdio_mlb_teams",
            ttl=86400,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] mlb_teams: unexpected response type {type(data)}")
            return []
        print(f"  [sdio] {len(data)} MLB teams [SOURCE: SportsDataIO]")
        return data

    def mlb_standings(self, year: int) -> list[dict]:
        """Return MLB standings for *year*.  Cached for 1 h."""
        data = self._get(
            f"{_MLB_BASE}/scores/json/Standings/{year}",
            cache_key=f"sdio_mlb_standings_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] mlb_standings {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} MLB standings entries for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    def mlb_players(self) -> list[dict]:
        """Return all current MLB players.  Cached for 24 h."""
        data = self._get(
            f"{_MLB_BASE}/scores/json/Players",
            cache_key="sdio_mlb_players",
            ttl=86400,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] mlb_players: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} MLB players [SOURCE: SportsDataIO]")
        return data

    def mlb_player_season_stats(self, year: int) -> list[dict]:
        """Return MLB player season stats for *year*.  Cached for 1 h."""
        data = self._get(
            f"{_MLB_BASE}/stats/json/PlayerSeasonStats/{year}",
            cache_key=f"sdio_mlb_player_stats_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] mlb_player_season_stats {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} MLB player season stats for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    def mlb_team_season_stats(self, year: int) -> list[dict]:
        """
        Return MLB team season stats for *year*.  Cached for 1 h.

        Key fields per record (when available):
          TeamID, Team, Name, Wins, Losses, RunsScored, RunsAllowed,
          ERA, BattingAverage, HomeRuns, StolenBases, WHIP, ...
        """
        data = self._get(
            f"{_MLB_BASE}/stats/json/TeamSeasonStats/{year}",
            cache_key=f"sdio_mlb_team_stats_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] mlb_team_season_stats {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} MLB team season stats for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    # ------------------------------------------------------------------
    # WNBA endpoints
    # ------------------------------------------------------------------

    def wnba_teams(self) -> list[dict]:
        """Return all WNBA teams.  Cached for 24 h."""
        data = self._get(
            f"{_WNBA_BASE}/scores/json/Teams",
            cache_key="sdio_wnba_teams",
            ttl=86400,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] wnba_teams: unexpected response type {type(data)}")
            return []
        print(f"  [sdio] {len(data)} WNBA teams [SOURCE: SportsDataIO]")
        return data

    def wnba_standings(self, year: int) -> list[dict]:
        """Return WNBA standings for *year*.  Cached for 1 h."""
        data = self._get(
            f"{_WNBA_BASE}/scores/json/Standings/{year}",
            cache_key=f"sdio_wnba_standings_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] wnba_standings {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} WNBA standings entries for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    def wnba_players(self) -> list[dict]:
        """Return all current WNBA players.  Cached for 24 h."""
        data = self._get(
            f"{_WNBA_BASE}/scores/json/Players",
            cache_key="sdio_wnba_players",
            ttl=86400,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] wnba_players: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} WNBA players [SOURCE: SportsDataIO]")
        return data

    def wnba_player_season_stats(self, year: int) -> list[dict]:
        """Return WNBA player season stats for *year*.  Cached for 1 h."""
        data = self._get(
            f"{_WNBA_BASE}/stats/json/PlayerSeasonStats/{year}",
            cache_key=f"sdio_wnba_player_stats_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] wnba_player_season_stats {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} WNBA player season stats for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    def wnba_team_season_stats(self, year: int) -> list[dict]:
        """
        Return WNBA team season stats for *year*.  Cached for 1 h.

        Key fields per record (when available):
          TeamID, Team, Name, Wins, Losses, PointsPerGame, OpponentPointsPerGame,
          FieldGoalPercentage, ThreePointersMade, Rebounds, Assists, ...
        """
        data = self._get(
            f"{_WNBA_BASE}/stats/json/TeamSeasonStats/{year}",
            cache_key=f"sdio_wnba_team_stats_{year}",
            ttl=3600,
        )
        if not isinstance(data, list):
            if data is not None:
                print(f"  [sdio] wnba_team_season_stats {year}: unexpected response type")
            return []
        print(f"  [sdio] {len(data)} WNBA team season stats for {year} "
              f"[SOURCE: SportsDataIO]")
        return data

    # ------------------------------------------------------------------
    # Availability check
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if SPORTSDATAIO_API_KEY is set."""
        return bool(self._key)
