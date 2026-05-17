"""
WNBA stats client -- multi-source waterfall with automatic fallback.

Source waterfall (tried in order until one returns data):
  1. stats.wnba.com    -- official WNBA API (new primary)
  2. ESPN              -- site.api.espn.com (original primary, reliable fallback)
  3. BallDontLie SDK   -- api.balldontlie.io (requires BALLDONTLIE_API_KEY; paid plan)
  4. sportsdataverse   -- Python equivalent of R wehoop package (historical only)

Which source was actually used is printed to stdout so fallbacks are visible.

Normalized game format (same structure consumed by WNBAFeatureBuilder):
  {
    "id":     str,
    "date":   "YYYY-MM-DD",
    "status": "FT" | "NS" | "IP",
    "teams":  {"home": {"id": int, "name": str},
               "away": {"id": int, "name": str}},
    "scores": {"home": {"total": int | None},
               "away": {"total": int | None}},
  }

API keys are read from environment variables:
  BALLDONTLIE_API_KEY -- BallDontLie (free tier at balldontlie.io)
"""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean
from typing import Optional

import requests

from .cache import Cache

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"

# Minimum completed games before we pull in a prior season for training data
_MIN_TRAINING_GAMES = 20

# Season date ranges (inclusive) — covers regular season + playoffs
_SEASON_DATES: dict[int, tuple[str, str]] = {
    2022: ("20220501", "20221001"),
    2023: ("20230501", "20231001"),
    2024: ("20240501", "20241001"),
    2025: ("20250501", "20251031"),
    2026: ("20260501", "20261031"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_final(game: dict) -> bool:
    return game.get("status") in ("FT", "AET", "AOT")


def _game_date(game: dict) -> str:
    return game.get("date", "")[:10]


def _last_n_win_pct(results: list[dict], n: int = 10) -> float:
    recent = results[-n:] if len(results) >= n else results
    if not recent:
        return 0.5
    return sum(1 for r in recent if r["win"]) / len(recent)


def _parse_espn_event(event: dict) -> Optional[dict]:
    """
    Convert a single ESPN scoreboard event dict into our normalized format.
    Returns None if the event cannot be parsed or has no competitors.
    """
    try:
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        status_type = comp.get("status", {}).get("type", {})
        completed = status_type.get("completed", False)
        state = status_type.get("name", "")  # STATUS_FINAL, STATUS_IN_PROGRESS, etc.

        if completed or "FINAL" in state:
            norm_status = "FT"
        elif "PROGRESS" in state or "HALFTIME" in state:
            norm_status = "IP"
        else:
            norm_status = "NS"

        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if home_c is None or away_c is None:
            return None

        def _team(c: dict) -> dict:
            t = c.get("team", {})
            return {
                "id":   int(t.get("id", 0)),
                "name": t.get("displayName") or t.get("name") or t.get("abbreviation") or "",
            }

        def _score(c: dict) -> Optional[int]:
            s = c.get("score")
            if s is None:
                return None
            try:
                return int(s)
            except (TypeError, ValueError):
                return None

        raw_date = event.get("date", "")[:10]  # "2025-05-17"

        return {
            "id":     event.get("id", ""),
            "date":   raw_date,
            "status": norm_status,
            "teams": {
                "home": _team(home_c),
                "away": _team(away_c),
            },
            "scores": {
                "home": {"total": _score(home_c)},
                "away": {"total": _score(away_c)},
            },
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class WNBAStatsClient:
    """
    WNBA stats client backed by ESPN's free public API.

    Public interface is identical to the previous API-Sports-based client:
      load(season)                  → int (# completed games)
      get_team_stats(team_id)       → dict | None
      get_player_stats(team_id)     → dict  (neutral values — ESPN has no free player endpoint)
      find_team(name)               → dict | None  {id, name}
      get_h2h(home_id, away_id)     → (int, int)
      _is_b2b(team_id, date_str)    → bool
      get_referee_foul_rate(gid)    → float  (constant 40.0 — no referee data)
      get_completed_games(season)   → list[dict]
      all_team_ids()                → list[int]
    """

    def __init__(self, api_key: str = "", cache: Optional[Cache] = None):
        # api_key kept for interface compatibility with app.py
        self._cache = cache or Cache()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })
        # Fallback API keys (read from environment; empty string disables that source)
        self._bdl_key = os.environ.get("BALLDONTLIE_API_KEY", "")

        self._raw_games: list[dict] = []         # normalized, all loaded seasons (tagged with _season)
        self._team_stats: dict[int, dict] = {}   # team_id → stats dict (current season only)
        self._team_by_id: dict[int, dict] = {}   # team_id → {id, name}
        self._team_index: dict[str, dict] = {}   # name.lower() → {id, name}
        self._player_stats: dict[int, dict] = {} # team_id → neutral PPG record
        self._h2h: dict[tuple[int, int], dict[int, int]] = {}
        self._loaded_season: Optional[int] = None
        self._current_season: Optional[int] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, season: int) -> int:
        """
        Fetch and cache up to 3 seasons of WNBA game data for recency weighting.
        Always loads: current season, previous season (season-1), and season-2.
        Each game dict is tagged with ``_season`` so training loops can build
        per-season buckets for the 60 / 25 / 15 % recency weighting scheme.

        Team stats (``_build_stats``) are derived from the **current season only**
        so inference features reflect the team's current form.

        Returns the number of completed games for the *current* season only.
        """
        self._current_season = season
        seasons_to_load = [s for s in (season - 2, season - 1, season) if s >= 2022]

        all_games: list[dict] = []
        for s in seasons_to_load:
            games = self._fetch_season(s)
            for g in games:
                g["_season"] = s          # tag every game with its source season
            all_games.extend(games)

        self._raw_games = all_games

        self._build_teams_from_games()
        self._load_espn_teams(season)   # fills in any missing names
        self._build_stats()             # current season only
        self._build_h2h()               # current season only
        self._build_player_stats()
        self._loaded_season = season
        return len([g for g in self._raw_games
                    if _is_final(g) and g.get("_season") == season])

    def _fetch_season(self, season: int) -> list[dict]:
        """
        Multi-source waterfall.  Tries each source in order and returns the
        first non-empty result.  Which source succeeded is printed to stdout.

        Order:
          1. stats.wnba.com  (official -- most authoritative)
          2. ESPN            (existing reliable source)
          3. BallDontLie     (requires BALLDONTLIE_API_KEY)
          4. sportsdataverse  (Python equivalent of R wehoop -- historical only)
        """
        from .wnba_fallback_fetcher import (
            fetch_wnba_stats_season,
            fetch_bdl_wnba_season,
            fetch_sportsdataverse_wnba_season,
        )

        # Source 1: stats.wnba.com
        games = fetch_wnba_stats_season(season, cache=self._cache)
        if games:
            return games
        print(f"  [wnba] stats.wnba.com returned no data for {season} -- "
              f"trying ESPN")

        # Source 2: ESPN (original primary)
        games = self._fetch_season_espn(season)
        if games:
            return games
        print(f"  [wnba] ESPN returned no data for {season} -- "
              f"trying BallDontLie")

        # Source 3: BallDontLie SDK (paid plan required for WNBA)
        games = fetch_bdl_wnba_season(season, self._bdl_key, cache=self._cache)
        if games:
            return games
        print(f"  [wnba] BallDontLie returned no data for {season} -- "
              f"trying sportsdataverse")

        # Source 4: sportsdataverse (Python equivalent of R wehoop package)
        # Historical / completed game data only -- not live.
        games = fetch_sportsdataverse_wnba_season(season, cache=self._cache)
        if games:
            return games

        print(f"  [wnba] All sources exhausted for {season} -- no game data")
        return []

    def _fetch_season_espn(self, season: int) -> list[dict]:
        """
        Fetch WNBA games for *season* from ESPN public scoreboard.
        Falls back to [] on any network/parse error.
        """
        cache_key = f"espn_wnba_season_{season}"
        cached = self._cache.get(cache_key, ttl=3600)
        if cached is not None:
            return cached

        start, end = _SEASON_DATES.get(season, (f"{season}0501", f"{season}1001"))
        url    = f"{_ESPN_BASE}/scoreboard"
        params = {"dates": f"{start}-{end}", "limit": 500}

        try:
            resp = self._session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            print(f"  [wnba] ESPN scoreboard unavailable for {season}: {exc}")
            return []

        events = body.get("events", [])
        games: list[dict] = []
        for ev in events:
            parsed = _parse_espn_event(ev)
            if parsed is not None:
                games.append(parsed)

        completed_count = sum(1 for g in games if _is_final(g))
        if games:
            print(f"  [wnba] {completed_count} completed games for {season} "
                  f"[SOURCE: ESPN]")

        ttl = 86400 * 30 if completed_count > 200 else 3600
        self._cache.set(cache_key, games)
        return games

    def _build_teams_from_games(self) -> None:
        """Populate team registries from games already in self._raw_games."""
        for game in self._raw_games:
            for side in ("home", "away"):
                t = game["teams"][side]
                tid = t["id"]
                if tid and t["name"]:
                    self._team_by_id[tid] = t
                    self._team_index[t["name"].lower()] = t

    def _load_espn_teams(self, season: int) -> None:
        """
        Fetch the ESPN teams list and fill in any teams missing from game data.
        Also adds short-name / abbreviation variants to the index.
        """
        cache_key = f"espn_wnba_teams_{season}"
        teams_data = self._cache.get(cache_key, ttl=86400)

        if teams_data is None:
            try:
                resp = self._session.get(
                    f"{_ESPN_BASE}/teams",
                    params={"limit": 50},
                    timeout=15,
                )
                resp.raise_for_status()
                body = resp.json()
                teams_data = body.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
                self._cache.set(cache_key, teams_data)
            except Exception as exc:
                print(f"  [wnba] ESPN teams fetch failed: {exc}")
                return

        for entry in teams_data:
            t = entry.get("team", entry)
            try:
                tid = int(t.get("id", 0))
            except (TypeError, ValueError):
                continue
            if not tid:
                continue

            full_name = t.get("displayName") or t.get("name") or ""
            short_name = t.get("shortDisplayName") or t.get("nickname") or ""
            abbrev = t.get("abbreviation") or ""

            team_dict = {"id": tid, "name": full_name}
            self._team_by_id.setdefault(tid, team_dict)

            for variant in (full_name, short_name, abbrev):
                if variant:
                    self._team_index.setdefault(variant.lower(), team_dict)

    def _build_stats(self) -> None:
        """
        Compute per-team aggregate stats from **current-season** completed games only.
        Prior-season games are retained in ``_raw_games`` for training (recency
        weighting) but do NOT contaminate live inference stats.

        Also computes ``season_trend = last10_win_pct − win_pct`` (momentum signal).
        """
        records: dict[int, list[dict]] = defaultdict(list)

        for game in self._raw_games:
            if not _is_final(game):
                continue
            # Only current-season games count for team stats / inference features
            if game.get("_season") != self._current_season:
                continue
            home = game["teams"]["home"]
            away = game["teams"]["away"]
            hid, aid = home["id"], away["id"]
            hs = game["scores"]["home"]["total"]
            as_ = game["scores"]["away"]["total"]
            if hs is None or as_ is None or not hid or not aid:
                continue

            d = game.get("date", "")
            records[hid].append({
                "scored": hs, "allowed": as_,
                "at_home": True, "win": hs > as_, "date": d,
            })
            records[aid].append({
                "scored": as_, "allowed": hs,
                "at_home": False, "win": as_ > hs, "date": d,
            })

        self._team_stats = {}
        for tid, results in records.items():
            if not results:
                continue
            n = len(results)
            home_res = [r for r in results if r["at_home"]]
            away_res = [r for r in results if not r["at_home"]]

            ppg  = mean(r["scored"]  for r in results)
            papg = mean(r["allowed"] for r in results)
            wins = sum(1 for r in results if r["win"])
            win_pct      = wins / n
            last10_win_pct = _last_n_win_pct(results, 10)

            self._team_stats[tid] = {
                "ppg":            ppg,
                "papg":           papg,
                "net_pts":        ppg - papg,
                "win_pct":        win_pct,
                "home_win_pct":   (
                    sum(1 for r in home_res if r["win"]) / len(home_res)
                    if home_res else 0.5
                ),
                "away_win_pct":   (
                    sum(1 for r in away_res if r["win"]) / len(away_res)
                    if away_res else 0.5
                ),
                "last10_win_pct": last10_win_pct,
                "last5_win_pct":  _last_n_win_pct(results, 5),
                "games_played":   n,
                # Momentum: positive = team is hot relative to its season average
                "season_trend":   last10_win_pct - win_pct,
            }

    def _build_h2h(self) -> None:
        """Build H2H win records from current-season completed games only."""
        acc: dict[tuple[int, int], dict[int, int]] = defaultdict(lambda: defaultdict(int))

        for game in self._raw_games:
            if not _is_final(game):
                continue
            if game.get("_season") != self._current_season:
                continue
            hid = game["teams"]["home"]["id"]
            aid = game["teams"]["away"]["id"]
            hs  = game["scores"]["home"]["total"]
            as_ = game["scores"]["away"]["total"]
            if not hid or not aid or hs is None or as_ is None:
                continue
            key = (min(hid, aid), max(hid, aid))
            if hs > as_:
                acc[key][hid] += 1
            elif as_ > hs:
                acc[key][aid] += 1

        self._h2h = {
            k: dict(v) for k, v in acc.items()
        }

    def _build_player_stats(self) -> None:
        """
        ESPN's free API doesn't expose per-player season averages in a reliable
        way via the public scoreboard endpoints, so we use a neutral baseline.
        Any team whose id appears in game data gets the neutral record.
        """
        self._player_stats = {}
        for tid in self._team_by_id:
            self._player_stats[tid] = {
                "name": "",
                "pts_pg": 15.0,
                "is_available": 1.0,
            }

    # ------------------------------------------------------------------
    # Back-to-back detection
    # ------------------------------------------------------------------

    def _is_b2b(self, team_id: int, game_date_str: str) -> bool:
        """Return True if the team played a completed game the previous calendar day."""
        if not game_date_str or len(game_date_str) < 10:
            return False
        try:
            target = date.fromisoformat(game_date_str[:10])
        except ValueError:
            return False

        prev_day = (target - timedelta(days=1)).isoformat()

        for game in self._raw_games:
            if not _is_final(game):
                continue
            hid = game["teams"]["home"]["id"]
            aid = game["teams"]["away"]["id"]
            if team_id not in (hid, aid):
                continue
            if _game_date(game) == prev_day:
                return True
        return False

    # ------------------------------------------------------------------
    # Public getters
    # ------------------------------------------------------------------

    def get_team_stats(self, team_id: int) -> Optional[dict]:
        """Return team stats dict with an added pace estimate (ppg+papg)/2."""
        stats = self._team_stats.get(team_id)
        if stats is None:
            return None
        pace = (stats["ppg"] + stats["papg"]) / 2.0
        return {**stats, "pace": pace}

    def get_player_stats(self, team_id: int) -> dict:
        """Return top-scorer stats (neutral baseline — ESPN free tier)."""
        return self._player_stats.get(
            team_id, {"name": "", "pts_pg": 15.0, "is_available": 1.0}
        )

    def get_h2h(self, home_id: int, away_id: int) -> tuple[int, int]:
        """Return (home_wins, away_wins) for the current season H2H."""
        key = (min(home_id, away_id), max(home_id, away_id))
        record = self._h2h.get(key, {})
        return record.get(home_id, 0), record.get(away_id, 0)

    def get_referee_foul_rate(self, game_id=None) -> float:
        """ESPN has no referee data — return the WNBA baseline."""
        return 40.0

    def find_team(self, name: str) -> Optional[dict]:
        """
        Look up a team by name (case-insensitive).  Falls back to token
        matching so partial names like "Wings" or "Dallas" still resolve.
        """
        if not name:
            return None
        needle = name.lower().strip()
        if needle in self._team_index:
            return self._team_index[needle]

        # Token overlap fallback
        needle_tokens = set(needle.split())
        best, best_score = None, 0
        for key, team in self._team_index.items():
            score = len(needle_tokens & set(key.split()))
            if score > best_score:
                best, best_score = team, score
        return best if best_score >= 1 else None

    def get_completed_games(self, season: int) -> list[dict]:
        """
        Return completed games for training.

        If the current season has at least _MIN_TRAINING_GAMES completed games,
        returns current-season games only.  If the season is young (< _MIN_TRAINING_GAMES),
        also includes prior-season games so the BettingModel has enough samples.
        This preserves the pre-refactor behavior for models that don't use the
        explicit recency-weighting API (get_completed_games_with_season).
        """
        if self._loaded_season != season:
            self.load(season)
        current = [g for g in self._raw_games
                   if _is_final(g) and g.get("_season") == season]
        if len(current) >= _MIN_TRAINING_GAMES:
            return current
        # Too few current-season games — include prior seasons as fallback
        return [g for g in self._raw_games if _is_final(g)]

    def get_completed_games_with_season(self) -> list[tuple[dict, int]]:
        """
        Return (game_dict, season_int) for every completed game across all
        loaded seasons.  Used by the spread / totals training loops to build
        per-season row groups for recency weighting.
        """
        return [
            (g, g.get("_season", self._loaded_season))
            for g in self._raw_games
            if _is_final(g)
        ]

    def find_high_change_wnba_teams(self, threshold: float = 0.15) -> set[int]:
        """
        Return team IDs whose win-rate changed by more than *threshold* (default
        15 pp) from the previous season to the current season.  Requires at least
        10 completed games in each season to avoid noise from small samples.

        Used to assign the boosted recency weight (75 %) to current-season rows
        that involve dramatically improved or declining teams.
        """
        cur_s = self._current_season
        prev_s = (cur_s - 1) if cur_s else None
        if cur_s is None or prev_s is None:
            return set()

        # Accumulate per-season win records
        from collections import defaultdict as _dd
        season_wins: dict[int, dict[int, list]] = {cur_s: _dd(list), prev_s: _dd(list)}

        for game in self._raw_games:
            if not _is_final(game):
                continue
            s = game.get("_season")
            if s not in season_wins:
                continue
            hid = game["teams"]["home"]["id"]
            aid = game["teams"]["away"]["id"]
            hs  = game["scores"]["home"]["total"]
            as_ = game["scores"]["away"]["total"]
            if not hid or not aid or hs is None or as_ is None:
                continue
            season_wins[s][hid].append(hs > as_)
            season_wins[s][aid].append(as_ > hs)

        high_change: set[int] = set()
        for team_id, cur_results in season_wins[cur_s].items():
            prev_results = season_wins[prev_s].get(team_id, [])
            if len(cur_results) < 10 or len(prev_results) < 10:
                continue
            cur_pct  = sum(cur_results)  / len(cur_results)
            prev_pct = sum(prev_results) / len(prev_results)
            if abs(cur_pct - prev_pct) > threshold:
                high_change.add(team_id)

        if high_change:
            print(f"  [wnba recency] {len(high_change)} team(s) exceed "
                  f"{threshold:.0%} win-rate change threshold → 75% weight boost")
        return high_change

    def all_team_ids(self) -> list[int]:
        """Return all team IDs that appeared in current-season game data."""
        return list(self._team_stats.keys())
