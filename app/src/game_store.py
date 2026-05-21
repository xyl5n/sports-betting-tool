"""
GameStore: downloads all games for a season in one API call, then computes
per-team statistics in memory. Works within the 100 req/day free-plan limit.

NFL stats computed: PPG, PAPG, win%, home/away splits, last-N form
MLB stats computed: RPG, RAPG, hits/game, errors/game, win%, last-N form

Fallback (MLB only)
-------------------
If API-Sports is unavailable the load() method automatically falls back:
  1. API-Sports  (primary)
  2. ESPN        (site.api.espn.com/apis/site/v2/sports/baseball/mlb)
  3. BallDontLie (api.balldontlie.io/v1 -- requires BALLDONTLIE_API_KEY)

Which source was actually used is printed to stdout for visibility.
"""
from __future__ import annotations

import os

import requests
from collections import defaultdict
from statistics import mean
from typing import Optional

from .cache import Cache


def _fetch_espn_wnba_scoreboard() -> list[dict]:
    """Fetch the current ESPN WNBA scoreboard window and normalize the
    response shape to the API-Sports schema that GameStore._build_stats
    expects.  Used as the secondary WNBA fallback after sportsdataverse
    (see GameStore._load_wnba_fallback).

    ESPN's scoreboard endpoint is keyless and returns a rolling window
    of upcoming + recent games -- much smaller than a full season, but
    enough for the no-odds predictor to know which teams play today
    and build a thin team index for feature lookups.  Returns [] on
    any HTTP / parse failure (caller logs the source).
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:                                              # noqa: BLE001
        print(f"  [wnba] ESPN scoreboard request failed: {exc}")
        return []

    games: list[dict] = []
    for ev in (body.get("events") or []):
        try:
            comp = (ev.get("competitions") or [None])[0] or {}
            comps = comp.get("competitors") or []
            home_c = next((c for c in comps if c.get("homeAway") == "home"), None)
            away_c = next((c for c in comps if c.get("homeAway") == "away"), None)
            if not home_c or not away_c:
                continue
            home_team = home_c.get("team") or {}
            away_team = away_c.get("team") or {}
            status = (ev.get("status") or {}).get("type", {}).get("name", "")
            home_score = home_c.get("score")
            away_score = away_c.get("score")
            try:
                home_runs = int(home_score) if home_score not in (None, "") else None
            except (TypeError, ValueError):
                home_runs = None
            try:
                away_runs = int(away_score) if away_score not in (None, "") else None
            except (TypeError, ValueError):
                away_runs = None
            # API-Sports-compatible game dict:
            games.append({
                "id":     str(ev.get("id", "") or ""),
                "date":   {"start": ev.get("date", "")},
                "status": {"long": status,
                           "short": "FT" if "Final" in status else "NS"},
                "teams": {
                    "home": {
                        "id":   int(home_team.get("id") or 0),
                        "name": home_team.get("displayName") or
                                home_team.get("name") or "",
                    },
                    "away": {
                        "id":   int(away_team.get("id") or 0),
                        "name": away_team.get("displayName") or
                                away_team.get("name") or "",
                    },
                },
                "scores": {
                    "home": {"total": home_runs},
                    "away": {"total": away_runs},
                },
                "_source": "espn_scoreboard",
            })
        except Exception:                                                # noqa: BLE001
            continue
    return games


def _status_ok(game: dict) -> bool:
    """Return True if the game is fully completed."""
    # NFL structure: game["game"]["status"]["short"]
    # MLB structure: game["status"]["short"]
    status = (
        game.get("game", {}).get("status", {}).get("short")
        or game.get("status", {}).get("short")
        or ""
    )
    return status in ("FT", "AET", "AOT")


def _teams_and_scores(game: dict) -> Optional[tuple[dict, dict, int, int]]:
    """Return (home_team, away_team, home_score, away_score) or None."""
    home = game.get("teams", {}).get("home")
    away = game.get("teams", {}).get("away")
    scores = game.get("scores", {})
    home_score = scores.get("home", {}).get("total")
    away_score = scores.get("away", {}).get("total")

    if not home or not away or home_score is None or away_score is None:
        return None
    try:
        return home, away, int(home_score), int(away_score)
    except (TypeError, ValueError):
        return None


def _last_n_win_pct(results: list[dict], n: int = 10) -> float:
    recent = results[-n:] if len(results) >= n else results
    if not recent:
        return 0.5
    return sum(1 for r in recent if r["win"]) / len(recent)


class GameStore:
    """
    One instance per sport. Fetches the full season game list once,
    then exposes computed per-team stats and a team name lookup.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        league_id: int,
        sport_tag: str,
        cache: Optional[Cache] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.league_id = league_id
        self.sport_tag = sport_tag
        self.cache = cache or Cache()
        self.session = requests.Session()
        self.session.headers.update({"x-apisports-key": api_key})

        self._raw_games: list[dict] = []
        self._team_stats: dict[int, dict] = {}       # team_id → stats dict
        self._team_by_id: dict[int, dict] = {}       # team_id → {id, name}
        self._team_index: dict[str, dict] = {}       # name.lower() → {id, name}
        self._loaded_season: Optional[int] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, season: int) -> int:
        """
        Fetch all games for *season* (cached 24 h) and compute stats.
        Returns the number of completed games found.

        For MLB, if API-Sports fails the method automatically falls back to
        ESPN then BallDontLie and logs which source was used.
        """
        if self._loaded_season == season:
            return len(self._raw_games)

        cache_key = f"games_{self.sport_tag}_{season}"
        games = self.cache.get(cache_key, ttl=86400)
        if games is None:
            games = self._fetch_from_api_sports(season, cache_key)

        self._raw_games = games
        self._loaded_season = season
        self._build_stats()
        return sum(1 for g in games if _status_ok(g))

    def _fetch_from_api_sports(self, season: int, cache_key: str) -> list[dict]:
        """
        Try API-Sports first.  For MLB, fall back to ESPN then BallDontLie
        if API-Sports is unavailable or returns an error response.
        Raises RuntimeError for non-MLB sports so existing callers are unaffected.
        """
        try:
            resp = self.session.get(
                f"{self.base_url}/games",
                params={"league": self.league_id, "season": season},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("errors"):
                raise RuntimeError(
                    f"API-Sports error for {self.sport_tag} season {season}: "
                    f"{body['errors']}"
                )
            games = body.get("response", [])
            remaining = resp.headers.get("x-ratelimit-requests-remaining", "?")
            limit     = resp.headers.get("x-ratelimit-requests-limit", "?")
            print(f"  [{self.sport_tag}] {len(games)} games from API-Sports "
                  f"(quota: {remaining}/{limit} remaining) [SOURCE: API-Sports]")
            self.cache.set(cache_key, games)
            return games

        except Exception as primary_exc:
            if self.sport_tag == "mlb":
                print(f"  [mlb] API-Sports unavailable ({primary_exc}) -- "
                      f"trying fallback sources")
                games = self._load_mlb_fallback(season)
                if games:
                    self.cache.set(cache_key, games)
                return games or []

            # WNBA fallback chain: sportsdataverse (Python port of the R
            # wehoop package) first, then ESPN scoreboard.  Same pattern
            # as the MLB chain above so the no-odds predictor can build
            # the GameStore from free sources when API-Sports is
            # unavailable (e.g. WNBA is gated behind API-Sports' paid
            # plan).  Other non-MLB sports keep the original behaviour
            # of propagating the API-Sports failure.
            if self.sport_tag == "wnba":
                print(f"  [wnba] API-Sports unavailable ({primary_exc}) -- "
                      f"trying free fallback sources (wehoop / ESPN)")
                games = self._load_wnba_fallback(season)
                if games:
                    self.cache.set(cache_key, games)
                return games or []

            raise   # other sports: propagate as before

    def _load_wnba_fallback(self, season: int) -> list[dict]:
        """
        WNBA fallback chain: sportsdataverse (wehoop equivalent) -> ESPN.
        Returns the first non-empty result, or [] if all sources fail.

        Both sources are free and key-less.  sportsdataverse pulls a full
        season from ESPN's internal WNBA API (more history, used during
        training); the ESPN scoreboard endpoint is a smaller live-window
        view (used when sportsdataverse fails to import or returns
        empty).  Either way the returned shape matches the API-Sports
        response that GameStore._build_stats expects.
        """
        # Fallback 1: sportsdataverse (the existing wnba_fallback_fetcher
        # helper already wraps espn_wnba_schedule + result normalization).
        # Its return shape uses scalar "status": "FT" and scalar "date":
        # "YYYY-MM-DD", but _status_ok / _build_stats below expect the
        # API-Sports nested {"status": {"short": "FT"}} and
        # {"date": {"start": iso}}.  Promote each game's status / date
        # to that nested shape before returning so callers don't care
        # which fallback produced the row.
        try:
            from .wnba_fallback_fetcher import fetch_sportsdataverse_wnba_season
            sdv_games = fetch_sportsdataverse_wnba_season(season, cache=self.cache)
            if sdv_games:
                for g in sdv_games:
                    st = g.get("status")
                    if isinstance(st, str):
                        g["status"] = {"short": st, "long": st}
                    dt = g.get("date")
                    if isinstance(dt, str):
                        g["date"] = {"start": dt}
                print(f"  [wnba] {len(sdv_games)} games from sportsdataverse "
                      f"(wehoop equivalent) [SOURCE: sportsdataverse]")
                return sdv_games
            print(f"  [wnba] sportsdataverse returned 0 games for {season}")
        except Exception as exc:
            print(f"  [wnba] sportsdataverse fallback failed: {exc}")

        # Fallback 2: ESPN scoreboard (free, no key).  This is a live
        # window of upcoming + recent games -- much smaller than a full
        # season, but enough to populate today's teams + give the
        # predictor a baseline.
        try:
            espn_games = _fetch_espn_wnba_scoreboard()
            if espn_games:
                print(f"  [wnba] {len(espn_games)} games from ESPN scoreboard "
                      f"[SOURCE: ESPN]")
                return espn_games
            print("  [wnba] ESPN scoreboard returned 0 games")
        except Exception as exc:
            print(f"  [wnba] ESPN fallback failed: {exc}")

        print(f"  [wnba] All fallback sources exhausted for season {season} -- "
              f"no game data available")
        return []

    def _load_mlb_fallback(self, season: int) -> list[dict]:
        """
        MLB-specific fallback chain: ESPN -> BallDontLie.
        Returns the first non-empty result, or [] if all sources fail.
        """
        # Fallback 1: ESPN
        try:
            from .mlb_fallback_fetcher import fetch_espn_mlb_season
            espn_games = fetch_espn_mlb_season(season, cache=self.cache)
            if espn_games:
                return espn_games
        except Exception as exc:
            print(f"  [mlb] ESPN fallback failed: {exc}")

        # Fallback 2: BallDontLie
        bdl_key = os.environ.get("BALLDONTLIE_API_KEY", "")
        if bdl_key:
            try:
                from .mlb_fallback_fetcher import fetch_bdl_mlb_season
                bdl_games = fetch_bdl_mlb_season(season, bdl_key, cache=self.cache)
                if bdl_games:
                    return bdl_games
            except Exception as exc:
                print(f"  [mlb] BallDontLie fallback failed: {exc}")
        else:
            print("  [mlb] BallDontLie fallback skipped: BALLDONTLIE_API_KEY not set")

        print(f"  [mlb] All fallback sources exhausted for season {season} -- "
              f"no game data available")
        return []

    def _build_stats(self) -> None:
        """Compute per-team aggregate stats from all completed games."""
        # team_id → ordered list of result dicts
        records: dict[int, list[dict]] = defaultdict(list)
        teams: dict[int, dict] = {}

        for game in self._raw_games:
            if not _status_ok(game):
                continue
            parsed = _teams_and_scores(game)
            if parsed is None:
                continue
            home, away, hs, as_ = parsed

            teams[home["id"]] = home
            teams[away["id"]] = away

            date_str = (
                game.get("game", {}).get("date", {}).get("date")
                or game.get("date", "")[:10]
            )

            # Extra MLB fields
            home_hits = game.get("scores", {}).get("home", {}).get("hits", 0) or 0
            away_hits = game.get("scores", {}).get("away", {}).get("hits", 0) or 0
            home_err = game.get("scores", {}).get("home", {}).get("errors", 0) or 0
            away_err = game.get("scores", {}).get("away", {}).get("errors", 0) or 0

            records[home["id"]].append({
                "scored": hs, "allowed": as_,
                "hits": home_hits, "errors": home_err,
                "at_home": True, "win": hs > as_, "date": date_str,
            })
            records[away["id"]].append({
                "scored": as_, "allowed": hs,
                "hits": away_hits, "errors": away_err,
                "at_home": False, "win": as_ > hs, "date": date_str,
            })

        self._team_by_id = teams
        self._team_index = {t["name"].lower(): t for t in teams.values()}

        for tid, results in records.items():
            if not results:
                continue
            n = len(results)
            home_res = [r for r in results if r["at_home"]]
            away_res = [r for r in results if not r["at_home"]]

            ppg = mean(r["scored"] for r in results)
            papg = mean(r["allowed"] for r in results)
            wins = sum(1 for r in results if r["win"])

            overall_win_pct = wins / n
            last20_win_pct  = _last_n_win_pct(results, 20)
            self._team_stats[tid] = {
                "ppg": ppg,
                "papg": papg,
                "net_pts": ppg - papg,
                "win_pct": overall_win_pct,
                "home_win_pct": (
                    sum(1 for r in home_res if r["win"]) / len(home_res)
                    if home_res else 0.5
                ),
                "away_win_pct": (
                    sum(1 for r in away_res if r["win"]) / len(away_res)
                    if away_res else 0.5
                ),
                "last10_win_pct": _last_n_win_pct(results, 10),
                "last20_win_pct": last20_win_pct,
                "last5_win_pct":  _last_n_win_pct(results, 5),
                "hits_pg":   mean(r["hits"]   for r in results),
                "errors_pg": mean(r["errors"] for r in results),
                "games_played": n,
                # Positive = team has been improving in its most recent 20 games
                # relative to its full-season average; negative = declining.
                "season_trend": last20_win_pct - overall_win_pct,
            }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_team_stats(self, team_id: int) -> Optional[dict]:
        return self._team_stats.get(team_id)

    def find_team(self, name: str) -> Optional[dict]:
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

    def get_completed_games(self, season: int) -> list[dict]:
        """Return completed games — used by BettingModel for training."""
        if self._loaded_season != season:
            self.load(season)
        return [g for g in self._raw_games if _status_ok(g)]

    def all_team_ids(self) -> list[int]:
        return list(self._team_stats.keys())
