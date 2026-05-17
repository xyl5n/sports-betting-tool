"""
wnba_fallback_fetcher.py
=========================
Fallback data sources for WNBA game data.

Waterfall order (WNBAStatsClient tries each in sequence):
  Source 1 -- stats.wnba.com    (official WNBA stats API -- new primary)
  Source 2 -- ESPN               (site.api.espn.com -- reliable fallback)
  Source 3 -- BallDontLie SDK    (api.balldontlie.io/v2/wnba -- paid plan)
  Source 4 -- sportsdataverse    (Python equivalent of R wehoop package)

All normalise into WNBAStatsClient's game-dict schema:
  {
    "id":     str,
    "date":   "YYYY-MM-DD",
    "status": "FT",
    "teams":  {"home": {"id": int, "name": str},
               "away": {"id": int, "name": str}},
    "scores": {"home": {"total": int | None},
               "away": {"total": int | None}},
    "_source": "wnba_stats" | "espn" | "balldontlie" | "sportsdataverse",
  }

Fields missing from a fallback source are filled with neutral baseline values
so the pipeline never crashes on a partial response.

sportsdataverse is the Python equivalent of the R wehoop package and is used
ONLY for historical WNBA game data and play-by-play for model training --
it is NOT used for live / upcoming game data.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from .cache import Cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WNBA_STATS_BASE = "https://stats.wnba.com/stats"

# stats.wnba.com requires these headers to bypass its bot-detection layer
_WNBA_STATS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer":             "https://stats.wnba.com",
    "x-nba-stats-origin":  "stats",
    "x-nba-stats-token":   "true",
    "Accept":              "application/json, text/plain, */*",
    "Accept-Language":     "en-US,en;q=0.9",
    "Connection":          "keep-alive",
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _safe_int(val) -> Optional[int]:
    """Convert *val* to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Source 1: stats.wnba.com -- LeagueGameLog
# ---------------------------------------------------------------------------

def _parse_wnba_stats_gamelog(result_set: dict) -> list[dict]:
    """
    Reconstruct per-game dicts from the LeagueGameLog result set.

    The endpoint returns one row per team per game.  Rows sharing the same
    GAME_ID are paired into a single game dict.  The MATCHUP field identifies
    the home team: "TeamA vs. TeamB" (home perspective) vs.
    "TeamA @ TeamB" (away perspective).
    """
    headers = result_set.get("headers", [])
    rows    = result_set.get("rowSet",  [])
    if not headers or not rows:
        return []

    idx = {h: i for i, h in enumerate(headers)}
    needed = ("TEAM_ID", "TEAM_NAME", "GAME_ID", "GAME_DATE", "MATCHUP", "PTS")
    if any(n not in idx for n in needed):
        return []   # unexpected schema

    tid_i     = idx["TEAM_ID"]
    tname_i   = idx["TEAM_NAME"]
    gid_i     = idx["GAME_ID"]
    date_i    = idx["GAME_DATE"]
    matchup_i = idx["MATCHUP"]
    pts_i     = idx["PTS"]

    # Group rows by GAME_ID
    by_game: dict[str, list] = {}
    for row in rows:
        gid = str(row[gid_i])
        by_game.setdefault(gid, []).append(row)

    games: list[dict] = []
    for gid, grows in by_game.items():
        if len(grows) < 2:
            continue   # incomplete game record

        # "vs." = home team's perspective; "@" = away team's perspective
        home_row = next(
            (r for r in grows if " vs. " in str(r[matchup_i])), None
        )
        away_row = next(
            (r for r in grows if " @ " in str(r[matchup_i])), None
        )
        # Fallback: assign arbitrarily if pattern not found
        if home_row is None or away_row is None:
            home_row, away_row = grows[0], grows[1]

        try:
            games.append({
                "id":     gid,
                "date":   str(home_row[date_i])[:10],
                "status": "FT",
                "teams": {
                    "home": {
                        "id":   int(home_row[tid_i]),
                        "name": str(home_row[tname_i]),
                    },
                    "away": {
                        "id":   int(away_row[tid_i]),
                        "name": str(away_row[tname_i]),
                    },
                },
                "scores": {
                    "home": {"total": _safe_int(home_row[pts_i])},
                    "away": {"total": _safe_int(away_row[pts_i])},
                },
                "_source": "wnba_stats",
            })
        except Exception:
            continue

    return games


def fetch_wnba_stats_season(
    season: int,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Fetch completed WNBA games from stats.wnba.com LeagueGameLog.
    Returns [] on any failure (bot detection, network error, schema change).
    """
    _cache = cache or Cache()
    cache_key = f"fb_wnba_stats_season_{season}"
    cached = _cache.get(cache_key, ttl=3600)
    if cached is not None:
        print(f"  [wnba-fallback] stats.wnba.com {season}: {len(cached)} games (cache)")
        return cached

    try:
        resp = requests.get(
            f"{_WNBA_STATS_BASE}/leaguegamelog",
            headers=_WNBA_STATS_HEADERS,
            params={
                "Counter":      1000,
                "DateFrom":     "",
                "DateTo":       "",
                "Direction":    "DESC",
                "LeagueID":     "10",
                "PlayerOrTeam": "T",
                "Season":       str(season),
                "SeasonType":   "Regular Season",
                "Sorter":       "DATE",
            },
            timeout=25,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        print(f"  [wnba-fallback] stats.wnba.com {season} unavailable: {exc}")
        return []

    result_sets = body.get("resultSets", [])
    if not result_sets:
        print(f"  [wnba-fallback] stats.wnba.com {season}: empty resultSets")
        return []

    games = _parse_wnba_stats_gamelog(result_sets[0])
    print(f"  [wnba-fallback] {len(games)} completed games for {season} "
          f"[SOURCE: stats.wnba.com]")

    if games:
        ttl = 86400 * 30 if len(games) > 200 else 3600
        _cache.set(cache_key, games)
    return games


# ---------------------------------------------------------------------------
# Source 3: BallDontLie WNBA (official Python SDK)
# ---------------------------------------------------------------------------

def fetch_bdl_wnba_season(
    season: int,
    api_key: str,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Fetch completed WNBA games via the official BallDontLie Python SDK.
    WNBA requires a paid plan ($9.99/mo Elite or higher); free tier returns
    AuthenticationError / NotFoundError which are caught gracefully.
    Returns [] if key absent, SDK unavailable, or plan does not cover WNBA.
    """
    if not api_key:
        print("  [wnba-fallback] BallDontLie skipped: BALLDONTLIE_API_KEY not set")
        return []

    try:
        from balldontlie import BalldontlieAPI
        from balldontlie.exceptions import (
            AuthenticationError,
            RateLimitError,
            NotFoundError,
        )
    except ImportError:
        print("  [wnba-fallback] BallDontLie SDK not installed (pip install balldontlie)")
        return []

    _cache = cache or Cache()
    cache_key = f"fb_bdl_wnba_season_{season}"
    cached = _cache.get(cache_key, ttl=3600)
    if cached is not None:
        print(f"  [wnba-fallback] BallDontLie {season}: {len(cached)} games (cache)")
        return cached

    api    = BalldontlieAPI(api_key=api_key)
    games: list[dict] = []
    cursor = None
    pages  = 0

    while True:
        try:
            kwargs: dict = {"seasons": [season], "per_page": 100}
            if cursor is not None:
                kwargs["cursor"] = cursor
            resp = api.wnba.games.list(**kwargs)
        except (AuthenticationError, NotFoundError) as exc:
            print(f"  [wnba-fallback] BallDontLie WNBA plan limit ({exc}) -- skipping")
            break
        except RateLimitError:
            print("  [wnba-fallback] BallDontLie rate-limited -- stopping pagination")
            break
        except Exception as exc:
            print(f"  [wnba-fallback] BallDontLie WNBA page failed ({exc}) "
                  f"after {len(games)} games")
            break

        pages += 1
        for game in (resp.data or []):
            try:
                status = str(getattr(game, "status", "") or "").lower()
                if "final" not in status and status not in ("f", "ft", "status_final"):
                    continue
                ht = game.home_team
                at = getattr(game, "away_team", None) or getattr(game, "visitor_team", None)
                if ht is None or at is None:
                    continue
                games.append({
                    "id":     str(getattr(game, "id", "")),
                    "date":   str(getattr(game, "date", "") or "")[:10],
                    "status": "FT",
                    "teams": {
                        "home": {
                            "id":   int(getattr(ht, "id", 0)),
                            "name": getattr(ht, "display_name", "")
                                    or getattr(ht, "full_name", "")
                                    or getattr(ht, "name", ""),
                        },
                        "away": {
                            "id":   int(getattr(at, "id", 0)),
                            "name": getattr(at, "display_name", "")
                                    or getattr(at, "full_name", "")
                                    or getattr(at, "name", ""),
                        },
                    },
                    "scores": {
                        "home": {"total": _safe_int(
                            getattr(game, "home_team_score", None)
                        )},
                        "away": {"total": _safe_int(
                            getattr(game, "visitor_team_score", None)
                            or getattr(game, "away_team_score", None)
                        )},
                    },
                    "_source": "balldontlie",
                })
            except Exception:
                continue

        meta   = getattr(resp, "meta", None)
        cursor = getattr(meta, "next_cursor", None) if meta else None
        if not cursor:
            break
        time.sleep(0.2)   # respect rate limit

    print(f"  [wnba-fallback] {len(games)} completed games for {season} "
          f"({pages} pages) [SOURCE: BallDontLie SDK]")
    if games:
        _cache.set(cache_key, games)
    return games


# ---------------------------------------------------------------------------
# Source 4: sportsdataverse (Python equivalent of R wehoop package)
# Historical WNBA game data and play-by-play for model training ONLY.
# NOT used for live or upcoming game data.
# ---------------------------------------------------------------------------

def fetch_sportsdataverse_wnba_season(
    season: int,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Fetch completed historical WNBA games via sportsdataverse
    (pip install sportsdataverse -- the Python port of the R wehoop package).

    Uses espn_wnba_schedule() which pulls from ESPN's internal WNBA API.
    Returns [] if package unavailable or data cannot be parsed.

    NOTE: For historical / training data only.  Do NOT use for live games.
    """
    try:
        import pandas as pd
        from sportsdataverse.wnba.wnba_schedule import espn_wnba_schedule
    except ImportError as exc:
        print(f"  [wnba-fallback] sportsdataverse not available ({exc}) -- skipping")
        return []

    _cache = cache or Cache()
    cache_key = f"fb_sdv_wnba_season_{season}"
    cached = _cache.get(cache_key, ttl=3600)
    if cached is not None:
        print(f"  [wnba-fallback] sportsdataverse {season}: {len(cached)} games (cache)")
        return cached

    try:
        # season_type=2 = Regular Season
        df = espn_wnba_schedule(dates=season, season_type=2, return_as_pandas=True)
        if df is None or (hasattr(df, "empty") and df.empty):
            print(f"  [wnba-fallback] sportsdataverse {season}: empty schedule")
            return []
        # Ensure we have a DataFrame (some versions return a polars frame)
        if not isinstance(df, pd.DataFrame):
            try:
                df = df.to_pandas()
            except Exception:
                pass
    except Exception as exc:
        print(f"  [wnba-fallback] sportsdataverse {season} unavailable: {exc}")
        return []

    games: list[dict] = []
    for _, row in df.iterrows():
        try:
            # Only include completed games
            completed = row.get("status_type_completed")
            if not completed:
                continue

            home_score = _safe_int(row.get("home_score"))
            away_score = _safe_int(row.get("away_score"))

            games.append({
                "id":     str(row.get("game_id", "") or ""),
                "date":   str(row.get("game_date", "") or "")[:10],
                "status": "FT",
                "teams": {
                    "home": {
                        "id":   _safe_int(row.get("home_id")) or 0,
                        "name": str(row.get("home_display_name")
                                    or row.get("home_name")
                                    or ""),
                    },
                    "away": {
                        "id":   _safe_int(row.get("away_id")) or 0,
                        "name": str(row.get("away_display_name")
                                    or row.get("away_name")
                                    or ""),
                    },
                },
                "scores": {
                    "home": {"total": home_score},
                    "away": {"total": away_score},
                },
                "_source": "sportsdataverse",
            })
        except Exception:
            continue

    print(f"  [wnba-fallback] {len(games)} completed games for {season} "
          f"[SOURCE: sportsdataverse (wehoop equivalent)]")
    if games:
        ttl = 86400 * 30 if len(games) > 200 else 3600
        _cache.set(cache_key, games)
    return games


def fetch_wnba_pbp_sportsdataverse(game_id: int | str) -> dict:
    """
    Fetch play-by-play data for a single historical WNBA game via
    sportsdataverse.  Returns an empty dict on any failure.

    For model training use only -- not for live games.
    """
    try:
        from sportsdataverse.wnba.wnba_pbp import espn_wnba_pbp
        result = espn_wnba_pbp(game_id=int(game_id))
        return result if isinstance(result, dict) else {}
    except Exception as exc:
        print(f"  [wnba-fallback] sportsdataverse PBP failed for game {game_id}: {exc}")
        return {}
