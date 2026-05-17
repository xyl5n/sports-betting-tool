"""
WNBA College Performance Client
================================
Pulls college basketball stats for WNBA players in their first or second
professional season and computes a per-team college adjustment factor.

Purpose
-------
Rookies and second-year players carry uncertain WNBA production, but their
college performance is a reliable proxy for early-career quality.  Teams with
multiple strong college performers get a small upward adjustment; teams whose
young players underperformed in college get a mild downward adjustment.

Data sources
------------
  • WNBA team rosters — ESPN public API
      https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{id}/roster
    The ``experience.years`` field encodes career years: 0 = rookie, 1 = 2nd year.

  • Women's college basketball player stats — sportsdataverse
      ``espn_wbb_player_stats(dates=year, season_type=2, return_as_pandas=True)``
    Returns a DataFrame with columns including ``athlete_display_name``,
    ``pts``, ``fg_pct``, ``reb``, ``ast``.

College adjustment formula (per player)
----------------------------------------
    ppg_score  = (ppg  − 15.0) / 5.0
    eff_score  = (fg%  − 0.43) / 0.06
    reb_score  = (rpg  − 5.0)  / 2.0
    ast_score  = (apg  − 3.0)  / 1.5
    raw_score  = 0.50 * ppg_score + 0.30 * eff_score
               + 0.10 * reb_score + 0.10 * ast_score
    adj        = raw_score * 0.10 * 3.0        # scale to ~±0.3 range per player
    adj        = clamp(adj, −1.5, +1.5)        # per-player cap

Per-team cap: clamp(sum(player adjs), −3.0, +3.0)

Caching
-------
All ESPN roster fetches are cached for 24 hours (TTL 86 400 s).
The sportsdataverse WBB stats DataFrame is cached in-process once fetched
per season (re-fetching is expensive).

Usage
-----
    from src.wnba_college_client import WNBACollegeClient
    from src.cache import Cache

    client = WNBACollegeClient(cache=Cache())
    adjs   = client.get_college_adjustments(team_ids, season=2026)
    # adjs → {team_id: float}

    for tid, adj in adjs.items():
        diag = client.get_diagnostics(tid)
        print(f"  {tid}: adj={adj:+.3f}")
        for p in diag:
            print(f"    {p['name']} ({p['exp_years']}yr) — "
                  f"college {p['college']}  ppg={p['ppg']:.1f}  "
                  f"fg%={p['fg_pct']:.3f}  adj={p['adj']:+.3f}  "
                  f"{'found' if p['found'] else 'NOT FOUND'}")
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

import requests

from .cache import Cache

_ESPN_WNBA = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"
_ROSTER_TTL = 86_400        # 24 hours — rosters change, but rarely day-to-day
_WBB_STATS_TTL = 86_400     # cache WBB DataFrame for 24 hours

# Adjustment scaling: multiply raw composite score by this to keep adj in ±1.5 range
_ADJ_SCALE = 0.10 * 3.0
_PLAYER_CAP = 1.5
_TEAM_CAP = 3.0

# Only players in first or second WNBA season receive a college adjustment
_MAX_EXP_YEARS = 1


def _normalize_name(name: str) -> str:
    """Lower-case, strip punctuation/accents for fuzzy name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z ]", "", ascii_str.lower()).strip()


def _match_name(target: str, candidates: list[str]) -> Optional[str]:
    """Return the best matching candidate name, or None if no good match."""
    norm_target = _normalize_name(target)
    # 1. Exact match after normalization
    for c in candidates:
        if _normalize_name(c) == norm_target:
            return c
    # 2. Partial token overlap (at least 2 tokens must match)
    target_tokens = set(norm_target.split())
    best, best_score = None, 0
    for c in candidates:
        tokens = set(_normalize_name(c).split())
        score = len(target_tokens & tokens)
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 2 else None


def _compute_player_adj(ppg: float, fg_pct: float, rpg: float, apg: float) -> float:
    """Convert college per-game stats into a signed adjustment value in [−1.5, +1.5]."""
    ppg_score = (ppg  - 15.0) / 5.0
    eff_score = (fg_pct - 0.43) / 0.06
    reb_score = (rpg  -  5.0) / 2.0
    ast_score = (apg  -  3.0) / 1.5
    raw = 0.50 * ppg_score + 0.30 * eff_score + 0.10 * reb_score + 0.10 * ast_score
    adj = raw * _ADJ_SCALE
    return max(-_PLAYER_CAP, min(_PLAYER_CAP, adj))


class WNBACollegeClient:
    """
    Computes per-team college-performance adjustment factors for young players.
    """

    def __init__(self, cache: Optional[Cache] = None) -> None:
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
        # In-process WBB stats cache keyed by season year
        self._wbb_stats_cache: dict[int, Optional[object]] = {}
        # Per-team adjustment and diagnostic storage
        self._adjustments: dict[int, float] = {}
        self._diagnostics: dict[int, list[dict]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_college_adjustments(
        self,
        team_ids: list[int],
        season: int,
    ) -> dict[int, float]:
        """
        For each team in *team_ids*, compute a college-performance adjustment
        factor by examining players with ≤1 year of WNBA experience.

        Returns {team_id: adj_float} for all teams (0.0 if no young players found).
        Also populates internal diagnostic data accessible via ``get_diagnostics()``.
        """
        # Fetch WBB college stats once for the most recent completed college season
        # (WNBA rookies playing in 2026 had their last college season in 2025/2026)
        college_season = season - 1 if season >= 2025 else season
        wbb_df = self._fetch_wbb_stats(college_season)
        wbb_name_map: dict[str, dict] = {}
        if wbb_df is not None:
            wbb_name_map = self._build_wbb_name_map(wbb_df)

        self._adjustments = {}
        self._diagnostics = {}

        for tid in team_ids:
            adj, diag = self._process_team(tid, season, wbb_name_map)
            self._adjustments[tid] = adj
            self._diagnostics[tid] = diag

        n_with_data = sum(1 for a in self._adjustments.values() if a != 0.0)
        print(f"  [college] Computed adjustments for {len(team_ids)} teams "
              f"({n_with_data} with non-zero college data)")
        return dict(self._adjustments)

    def get_diagnostics(self, team_id: int) -> list[dict]:
        """
        Return per-player diagnostic dicts for the given team.

        Each dict contains:
          name, exp_years, college, ppg, fg_pct, rpg, apg, found, adj
        """
        return self._diagnostics.get(team_id, [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_team(
        self,
        team_id: int,
        season: int,
        wbb_name_map: dict[str, dict],
    ) -> tuple[float, list[dict]]:
        """Compute the team adjustment and diagnostic list for one team."""
        roster = self._fetch_roster(team_id, season)
        young_players = [
            p for p in roster
            if p.get("exp_years", 99) <= _MAX_EXP_YEARS
        ]

        diag: list[dict] = []
        total_adj = 0.0

        for player in young_players:
            name = player["name"]
            college = player.get("college", "")
            exp_years = player.get("exp_years", 0)

            matched_name = _match_name(name, list(wbb_name_map.keys()))
            if matched_name and matched_name in wbb_name_map:
                stats = wbb_name_map[matched_name]
                ppg    = float(stats.get("ppg", 0.0))
                fg_pct = float(stats.get("fg_pct", 0.43))
                rpg    = float(stats.get("rpg", 0.0))
                apg    = float(stats.get("apg", 0.0))
                adj    = _compute_player_adj(ppg, fg_pct, rpg, apg)
                found  = True
            else:
                ppg = fg_pct = rpg = apg = adj = 0.0
                found = False

            total_adj += adj
            diag.append({
                "name":      name,
                "exp_years": exp_years,
                "college":   college,
                "ppg":       ppg,
                "fg_pct":    fg_pct,
                "rpg":       rpg,
                "apg":       apg,
                "found":     found,
                "adj":       round(adj, 4),
            })

        # Apply per-team cap
        team_adj = max(-_TEAM_CAP, min(_TEAM_CAP, total_adj))
        return round(team_adj, 4), diag

    def _fetch_roster(self, team_id: int, season: int) -> list[dict]:
        """
        Fetch the ESPN roster for *team_id*.  Returns a list of player dicts:
          {name: str, exp_years: int, college: str}
        """
        cache_key = f"espn_wnba_roster_{team_id}_{season}"
        cached = self._cache.get(cache_key, ttl=_ROSTER_TTL)
        if cached is not None:
            return cached

        url = f"{_ESPN_WNBA}/teams/{team_id}/roster"
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            print(f"  [college] ESPN roster fetch failed for team {team_id}: {exc}")
            return []

        players: list[dict] = []
        # ESPN roster JSON: body["athletes"] → list of player objects
        for athlete in body.get("athletes", []):
            # Handle nested list (sometimes ESPN wraps by position group)
            if isinstance(athlete, dict) and "items" in athlete:
                items = athlete["items"]
            elif isinstance(athlete, dict):
                items = [athlete]
            else:
                items = athlete if isinstance(athlete, list) else []

            for p in items:
                name = (
                    p.get("displayName")
                    or p.get("fullName")
                    or p.get("shortName")
                    or ""
                )
                # experience.years: 0 = rookie, 1 = 2nd year, 2 = 3rd year, …
                exp_years = p.get("experience", {}).get("years", 99)
                college = (
                    p.get("college", {}).get("name", "")
                    if isinstance(p.get("college"), dict)
                    else str(p.get("college", ""))
                )
                if name:
                    players.append({
                        "name":      name,
                        "exp_years": int(exp_years),
                        "college":   college,
                    })

        self._cache.set(cache_key, players)
        return players

    def _fetch_wbb_stats(self, college_season: int) -> Optional[object]:
        """
        Fetch the WBB player stats DataFrame via sportsdataverse.
        Returns the DataFrame, or None on failure.
        Cached in-process for the session lifetime.
        """
        if college_season in self._wbb_stats_cache:
            return self._wbb_stats_cache[college_season]

        try:
            from sportsdataverse.wbb.wbb_player_stats import espn_wbb_player_stats
            df = espn_wbb_player_stats(
                dates=college_season,
                season_type=2,
                return_as_pandas=True,
            )
            if df is not None and not df.empty:
                print(f"  [college] Loaded WBB player stats for {college_season}: "
                      f"{len(df)} rows  [SOURCE: sportsdataverse]")
            else:
                print(f"  [college] sportsdataverse WBB returned empty for {college_season}")
                df = None
        except Exception as exc:
            print(f"  [college] sportsdataverse WBB fetch failed for {college_season}: {exc}")
            df = None

        self._wbb_stats_cache[college_season] = df
        return df

    def _build_wbb_name_map(self, df) -> dict[str, dict]:
        """
        Convert the WBB DataFrame into a name → stats dict for O(1) lookup.
        Aggregates multiple rows per player by taking the season totals or
        max games row (handles split-season transfers).
        """
        try:
            import pandas as pd

            # Column name variants across sportsdataverse versions
            name_col = next(
                (c for c in ("athlete_display_name", "athlete_name", "name")
                 if c in df.columns),
                None,
            )
            if name_col is None:
                return {}

            def _safe_col(col: str, default: float = 0.0):
                return df[col] if col in df.columns else default

            # Aggregate: for players with multiple rows pick the one with most GP
            gp_col = next(
                (c for c in ("gp", "games", "games_played") if c in df.columns),
                None,
            )
            if gp_col:
                df = df.sort_values(gp_col, ascending=False)
            df_dedup = df.drop_duplicates(subset=[name_col], keep="first")

            ppg_col   = next((c for c in ("pts", "ppg", "points_per_game") if c in df.columns), None)
            fgpct_col = next((c for c in ("fg_pct", "field_goal_pct", "fg%") if c in df.columns), None)
            rpg_col   = next((c for c in ("reb", "rpg", "rebounds_per_game") if c in df.columns), None)
            apg_col   = next((c for c in ("ast", "apg", "assists_per_game") if c in df.columns), None)

            name_map: dict[str, dict] = {}
            for _, row in df_dedup.iterrows():
                name = str(row[name_col]).strip()
                if not name or name == "nan":
                    continue
                name_map[name] = {
                    "ppg":    float(row[ppg_col])   if ppg_col   else 0.0,
                    "fg_pct": float(row[fgpct_col]) if fgpct_col else 0.43,
                    "rpg":    float(row[rpg_col])   if rpg_col   else 0.0,
                    "apg":    float(row[apg_col])   if apg_col   else 0.0,
                }
            return name_map
        except Exception as exc:
            print(f"  [college] Failed to build WBB name map: {exc}")
            return {}
