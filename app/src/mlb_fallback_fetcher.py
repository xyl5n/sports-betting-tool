"""
mlb_fallback_fetcher.py
========================
Fallback data sources for MLB game and schedule data.

Game-result fallback chain (API-Sports is tried first in GameStore itself):
  Fallback 1 -- ESPN        (site.api.espn.com/apis/site/v2/sports/baseball/mlb)
  Fallback 2 -- BallDontLie (api.balldontlie.io/v1/baseball/games)

Schedule/pitcher fallback chain (MLB Stats API tried first in enriched_historical_data):
  Fallback 1 -- ESPN scoreboard for date  (team names only; pitcher -> neutral)
  Fallback 2 -- BallDontLie for date      (team names only; pitcher -> neutral)

All sources normalise their output into the GameStore-compatible game dict:
  {
    "status": {"short": "FT"},
    "teams":  {"home": {"id": int, "name": str},
               "away": {"id": int, "name": str}},
    "scores": {"home": {"total": int, "hits": int, "errors": int},
               "away": {"total": int, "hits": int, "errors": int}},
    "date":   "YYYY-MM-DD",
    "_source": "espn" | "balldontlie",
  }

Fields that the fallback sources cannot provide (hits, errors, pitcher data) are
substituted with neutral baseline values so the rest of the pipeline never crashes.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from .cache import Cache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ESPN_MLB = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"

# Approximate MLB season date windows for ESPN bulk scoreboard fetch
_MLB_SEASON_DATES: dict[int, tuple[str, str]] = {
    2022: ("20220401", "20221101"),
    2023: ("20230401", "20231101"),
    2024: ("20240401", "20241101"),
    2025: ("20250401", "20251101"),
    2026: ("20260401", "20261101"),
}

_ESPN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Session factories
# ---------------------------------------------------------------------------

def _espn_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _ESPN_UA, "Accept": "application/json"})
    return s


# ---------------------------------------------------------------------------
# ESPN MLB -- game results
# ---------------------------------------------------------------------------

def _parse_espn_mlb_event(event: dict) -> Optional[dict]:
    """Parse a single ESPN baseball scoreboard event into GameStore format.
    Returns None for non-final or malformed events."""
    try:
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        status_type = comp.get("status", {}).get("type", {})
        completed   = status_type.get("completed", False)
        state_name  = status_type.get("name", "")
        if not (completed or "FINAL" in state_name):
            return None  # only completed games

        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            return None

        def _team(c: dict) -> dict:
            t = c.get("team", {})
            return {
                "id":   int(t.get("id", 0)),
                "name": t.get("displayName") or t.get("name") or "",
            }

        def _score(c: dict) -> int:
            try:
                return int(c.get("score") or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "status": {"short": "FT"},
            "teams": {
                "home": _team(home_c),
                "away": _team(away_c),
            },
            "scores": {
                # hits/errors not available from ESPN scoreboard -- neutral 0
                "home": {"total": _score(home_c), "hits": 0, "errors": 0},
                "away": {"total": _score(away_c), "hits": 0, "errors": 0},
            },
            "date":    event.get("date", "")[:10],
            "_source": "espn",
        }
    except Exception:
        return None


def fetch_espn_mlb_season(
    season: int,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Fetch completed MLB games for *season* from ESPN public scoreboard.
    Logs which source was used. Returns [] on any failure.
    """
    _cache = cache or Cache()
    cache_key = f"fb_espn_mlb_season_{season}"
    cached = _cache.get(cache_key, ttl=86400)
    if cached is not None:
        completed = sum(1 for g in cached
                        if g.get("status", {}).get("short") == "FT")
        print(f"  [mlb-fallback] ESPN {season}: {completed} games (from cache)")
        return cached

    start, end = _MLB_SEASON_DATES.get(season, (f"{season}0401", f"{season}1101"))
    sess = _espn_session()
    try:
        resp = sess.get(
            f"{_ESPN_MLB}/scoreboard",
            params={"dates": f"{start}-{end}", "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception as exc:
        print(f"  [mlb-fallback] ESPN scoreboard unavailable for {season}: {exc}")
        return []

    games = [g for g in (_parse_espn_mlb_event(ev) for ev in events) if g]
    print(f"  [mlb-fallback] {len(games)} completed games for {season} "
          f"[SOURCE: ESPN fallback]")

    if games:
        ttl = 86400 * 30 if len(games) > 500 else 3600
        _cache.set(cache_key, games)
    return games


# ---------------------------------------------------------------------------
# ESPN MLB -- schedule for a specific date (enriched_historical_data fallback)
# ---------------------------------------------------------------------------

def fetch_espn_mlb_schedule_for_date(
    date_str: str,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Return schedule entries for *date_str* (YYYY-MM-DD) from ESPN scoreboard.
    Format matches _fetch_date_schedule() in enriched_historical_data:
      [{game_pk, home_name, away_name, home_pitcher: None, away_pitcher: None}]

    Pitcher data is unavailable from the ESPN scoreboard endpoint.
    Callers should fall back to _NEUTRAL_SP for any None pitcher entry.
    """
    _cache = cache or Cache()
    yyyymmdd  = date_str.replace("-", "")
    cache_key = f"fb_espn_mlb_sched_{yyyymmdd}"
    cached = _cache.get(cache_key, ttl=86400 * 365)
    if cached is not None:
        return cached

    sess = _espn_session()
    try:
        resp = sess.get(
            f"{_ESPN_MLB}/scoreboard",
            params={"dates": yyyymmdd, "limit": 50},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception as exc:
        print(f"  [mlb-fallback] ESPN schedule unavailable for {date_str}: {exc}")
        return []

    results: list[dict] = []
    for ev in events:
        try:
            comp  = ev.get("competitions", [{}])[0]
            comps = comp.get("competitors", [])
            if len(comps) < 2:
                continue
            home_c = next((c for c in comps if c.get("homeAway") == "home"), None)
            away_c = next((c for c in comps if c.get("homeAway") == "away"), None)
            if not home_c or not away_c:
                continue
            results.append({
                "game_pk":      int(ev.get("id", 0)),
                "home_name":    home_c.get("team", {}).get("displayName", ""),
                "away_name":    away_c.get("team", {}).get("displayName", ""),
                "home_pitcher": None,   # not available from ESPN scoreboard
                "away_pitcher": None,
            })
        except Exception:
            continue

    if results:
        print(f"  [mlb-fallback] {date_str}: {len(results)} games from ESPN "
              f"[no pitcher data -- neutral SP values will be used]")
        _cache.set(cache_key, results)
    return results


# ---------------------------------------------------------------------------
# BallDontLie MLB -- game results (official SDK)
# ---------------------------------------------------------------------------

def fetch_bdl_mlb_season(
    season: int,
    api_key: str,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Fetch completed MLB games for *season* via the official BallDontLie Python
    SDK (pip install balldontlie).  Requires BALLDONTLIE_API_KEY.
    Returns [] if key absent, SDK unavailable, or plan does not cover MLB.
    """
    if not api_key:
        print("  [mlb-fallback] BallDontLie skipped: BALLDONTLIE_API_KEY not set")
        return []

    try:
        from balldontlie import BalldontlieAPI
        from balldontlie.exceptions import (
            AuthenticationError,
            RateLimitError,
            NotFoundError,
        )
    except ImportError:
        print("  [mlb-fallback] BallDontLie SDK not installed (pip install balldontlie)")
        return []

    _cache = cache or Cache()
    cache_key = f"fb_bdl_mlb_season_{season}"
    cached = _cache.get(cache_key, ttl=86400)
    if cached is not None:
        print(f"  [mlb-fallback] BallDontLie {season}: {len(cached)} games (cache)")
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
            resp = api.mlb.games.list(**kwargs)
        except (AuthenticationError, NotFoundError) as exc:
            print(f"  [mlb-fallback] BallDontLie MLB plan limit ({exc}) -- skipping")
            break
        except RateLimitError:
            print("  [mlb-fallback] BallDontLie rate-limited -- stopping pagination")
            break
        except Exception as exc:
            print(f"  [mlb-fallback] BallDontLie page failed ({exc}) "
                  f"after {len(games)} games")
            break

        pages += 1
        for game in (resp.data or []):
            try:
                if getattr(game, "status", "") != "STATUS_FINAL":
                    continue
                ht  = game.home_team
                at  = game.away_team
                htd = getattr(game, "home_team_data", None)
                atd = getattr(game, "away_team_data", None)
                games.append({
                    "status": {"short": "FT"},
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
                        "home": {
                            "total":  int(getattr(htd, "runs",   0) or 0),
                            "hits":   int(getattr(htd, "hits",   0) or 0),
                            "errors": int(getattr(htd, "errors", 0) or 0),
                        },
                        "away": {
                            "total":  int(getattr(atd, "runs",   0) or 0),
                            "hits":   int(getattr(atd, "hits",   0) or 0),
                            "errors": int(getattr(atd, "errors", 0) or 0),
                        },
                    },
                    "date":    str(getattr(game, "date", "") or "")[:10],
                    "_source": "balldontlie",
                })
            except Exception:
                continue

        meta   = getattr(resp, "meta", None)
        cursor = getattr(meta, "next_cursor", None) if meta else None
        if not cursor:
            break
        time.sleep(0.2)   # respect rate limit

    print(f"  [mlb-fallback] {len(games)} completed games for {season} "
          f"({pages} pages) [SOURCE: BallDontLie SDK]")
    if games:
        _cache.set(cache_key, games)
    return games


# ---------------------------------------------------------------------------
# BallDontLie MLB -- schedule for a specific date
# ---------------------------------------------------------------------------

def fetch_bdl_mlb_schedule_for_date(
    date_str: str,
    api_key: str,
    cache: Optional[Cache] = None,
) -> list[dict]:
    """
    Return schedule entries for *date_str* from BallDontLie (SDK).
    Same format as fetch_espn_mlb_schedule_for_date.
    Pitcher data unavailable -- callers use neutral values.
    """
    if not api_key:
        return []

    try:
        from balldontlie import BalldontlieAPI
        from balldontlie.exceptions import AuthenticationError, NotFoundError
    except ImportError:
        return []

    _cache = cache or Cache()
    cache_key = f"fb_bdl_mlb_sched_{date_str}"
    cached = _cache.get(cache_key, ttl=86400 * 365)
    if cached is not None:
        return cached

    api = BalldontlieAPI(api_key=api_key)
    try:
        resp = api.mlb.games.list(dates=[date_str], per_page=50)
    except (AuthenticationError, NotFoundError):
        return []
    except Exception as exc:
        print(f"  [mlb-fallback] BallDontLie schedule unavailable for {date_str}: {exc}")
        return []

    results: list[dict] = []
    for game in (resp.data or []):
        try:
            ht = game.home_team
            at = game.away_team
            results.append({
                "game_pk":      int(getattr(game, "id", 0)),
                "home_name":    getattr(ht, "display_name", "")
                                or getattr(ht, "full_name", "")
                                or getattr(ht, "name", ""),
                "away_name":    getattr(at, "display_name", "")
                                or getattr(at, "full_name", "")
                                or getattr(at, "name", ""),
                "home_pitcher": None,
                "away_pitcher": None,
            })
        except Exception:
            continue

    if results:
        print(f"  [mlb-fallback] {date_str}: {len(results)} games from BallDontLie "
              f"[no pitcher data -- neutral SP values will be used]")
        _cache.set(cache_key, results)
    return results
