"""
Upset factor: per-game unpredictability score (1–10 scale).
Pulls real 2026 season data entirely from the free MLB Stats API
(statsapi.mlb.com — no key required).

Nine signals combined into the score:
  1. run_scoring_variance  — stdev of runs scored per game (all 2026 games)
  2. pitching_variance     — stdev of runs allowed per game (all 2026 games)
  3. streak_factor         — current W/L streak magnitude
  4. underdog_win_rate     — wins as underdog (away or sub-.480 running W%) / underdog games
  5. blown_lead_rate       — led after 6 innings but lost (last 20 games)
  6. h2h_divergence        — closeness of H2H record between these two teams this season
  7. bullpen_volatility    — avg runs allowed in innings 7-9 (last 20 games)
  8. pitcher_consistency   — last-3-start ERA stdev for each probable starter
  9. series_game           — game 1 / 2 / 3+ of this specific series

Cache policy:
  - Team game logs:   86 400 s (once per day)
  - H2H record:       86 400 s (once per day)
  - Pitcher starts:   86 400 s (once per day)
  - Series game num:   3 600 s (hourly — today's data)
  - Probable pitchers: 3 600 s (hourly)

Underdog detection (no historical odds available from free APIs):
  A game is treated as an "underdog game" when the team was the away/road
  team OR when their running win-percentage entering that game was below
  .480 (the approximate threshold where a team starts trading at a positive
  moneyline in MLB markets).  This is the closest free proxy to the true
  definition of "positive moneyline" without paid historical odds data.
"""
from __future__ import annotations

import statistics
from typing import Optional

import requests

from .cache import Cache


MLB_API = "https://statsapi.mlb.com/api/v1"

TEAM_ABBR: dict[str, str] = {
    "Arizona Diamondbacks":  "ARI",
    "Atlanta Braves":        "ATL",
    "Baltimore Orioles":     "BAL",
    "Boston Red Sox":        "BOS",
    "Chicago Cubs":          "CHC",
    "Chicago White Sox":     "CWS",
    "Cincinnati Reds":       "CIN",
    "Cleveland Guardians":   "CLE",
    "Colorado Rockies":      "COL",
    "Detroit Tigers":        "DET",
    "Houston Astros":        "HOU",
    "Kansas City Royals":    "KC",
    "Los Angeles Angels":    "LAA",
    "Los Angeles Dodgers":   "LAD",
    "Miami Marlins":         "MIA",
    "Milwaukee Brewers":     "MIL",
    "Minnesota Twins":       "MIN",
    "New York Mets":         "NYM",
    "New York Yankees":      "NYY",
    "Oakland Athletics":     "OAK",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates":    "PIT",
    "San Diego Padres":      "SD",
    "San Francisco Giants":  "SF",
    "Seattle Mariners":      "SEA",
    "St. Louis Cardinals":   "STL",
    "Tampa Bay Rays":        "TB",
    "Texas Rangers":         "TEX",
    "Toronto Blue Jays":     "TOR",
    "Washington Nationals":  "WSH",
}

MLB_TEAM_IDS: dict[str, int] = {
    "Arizona Diamondbacks":  109,
    "Atlanta Braves":        144,
    "Baltimore Orioles":     110,
    "Boston Red Sox":        111,
    "Chicago Cubs":          112,
    "Chicago White Sox":     145,
    "Cincinnati Reds":       113,
    "Cleveland Guardians":   114,
    "Colorado Rockies":      115,
    "Detroit Tigers":        116,
    "Houston Astros":        117,
    "Kansas City Royals":    118,
    "Los Angeles Angels":    108,
    "Los Angeles Dodgers":   119,
    "Miami Marlins":         146,
    "Milwaukee Brewers":     158,
    "Minnesota Twins":       142,
    "New York Mets":         121,
    "New York Yankees":      147,
    "Oakland Athletics":     133,
    "Philadelphia Phillies": 143,
    "Pittsburgh Pirates":    134,
    "San Diego Padres":      135,
    "San Francisco Giants":  137,
    "Seattle Mariners":      136,
    "St. Louis Cardinals":   138,
    "Tampa Bay Rays":        139,
    "Texas Rangers":         140,
    "Toronto Blue Jays":     141,
    "Washington Nationals":  120,
}


def _parse_ip(ip_str: str) -> float:
    """Convert MLB innings-pitched notation (e.g. '6.2') to decimal innings."""
    try:
        parts = str(ip_str).split(".")
        whole = int(parts[0])
        thirds = int(parts[1]) if len(parts) > 1 else 0
        return whole + thirds / 3.0
    except (ValueError, TypeError):
        return 0.0


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class UpsetCalculator:
    def __init__(self, cache: Optional[Cache] = None, season: int = 2026):
        self.cache  = cache or Cache()
        self.season = season

    # ── Data fetching ──────────────────────────────────────────────────────────

    def _get_team_gamelog(self, team_mlb_id: int) -> list[dict]:
        """
        All completed 2026 regular-season games for one team via MLB Stats API.
        Each row: {r, ra, won, is_home, innings: [{num, team_runs, opp_runs}]}
        Cached 24 h (once per day).
        """
        key    = f"upset_gamelog_{self.season}_{team_mlb_id}"
        cached = self.cache.get(key, ttl=86400)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{MLB_API}/schedule",
                params={
                    "sportId":  1,
                    "season":   self.season,
                    "teamId":   team_mlb_id,
                    "gameType": "R",
                    "hydrate":  "linescore",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            rows: list[dict] = []
            for date_obj in data.get("dates", []):
                for game in date_obj.get("games", []):
                    if game.get("status", {}).get("abstractGameState") != "Final":
                        continue

                    home_data = game["teams"]["home"]
                    away_data = game["teams"]["away"]
                    is_home   = (home_data["team"]["id"] == team_mlb_id)
                    my_data   = home_data if is_home else away_data
                    op_data   = away_data if is_home else home_data

                    r   = float(my_data.get("score") or 0)
                    ra  = float(op_data.get("score") or 0)
                    won = bool(my_data.get("isWinner", False))

                    # Per-inning breakdown from team's perspective
                    raw_innings = game.get("linescore", {}).get("innings", [])
                    my_side = "home" if is_home else "away"
                    op_side = "away" if is_home else "home"
                    innings: list[dict] = []
                    for inn in raw_innings:
                        num    = inn.get("num", 0)
                        t_runs = int((inn.get(my_side) or {}).get("runs") or 0)
                        o_runs = int((inn.get(op_side) or {}).get("runs") or 0)
                        innings.append({"num": num, "team_runs": t_runs, "opp_runs": o_runs})

                    rows.append({
                        "r":       r,
                        "ra":      ra,
                        "won":     won,
                        "is_home": is_home,
                        "innings": innings,
                        # is_underdog filled in below after all rows collected
                    })

            # Compute running win% to flag underdog games.
            # Games arrive in chronological order from the MLB API (date by date).
            # A game is an "underdog game" when:
            #   - team was the road/away team (primary indicator), OR
            #   - team's running win% entering that game was below .480
            #     (the approximate threshold where a team trades at a positive
            #      moneyline in MLB markets — no free historical odds available).
            running_wins  = 0
            running_games = 0
            for row in rows:
                pre_wpct = running_wins / running_games if running_games > 0 else 0.500
                row["is_underdog"] = (not row["is_home"]) or (pre_wpct < 0.480)
                running_games += 1
                if row["won"]:
                    running_wins += 1

            self.cache.set(key, rows)
            return rows
        except Exception:
            return []

    def _get_h2h(self, home_mlb_id: int, away_mlb_id: int) -> tuple[int, int]:
        """
        H2H completed games between these two teams this season.
        Returns (home_wins, away_wins) where home/away refers to today's matchup.
        Cached 24 h.
        """
        key    = f"upset_h2h_{self.season}_{home_mlb_id}_{away_mlb_id}"
        cached = self.cache.get(key, ttl=86400)
        if cached is not None:
            return tuple(cached)  # type: ignore[return-value]

        try:
            resp = requests.get(
                f"{MLB_API}/schedule",
                params={
                    "sportId":    1,
                    "season":     self.season,
                    "teamId":     home_mlb_id,
                    "opponentId": away_mlb_id,
                    "gameType":   "R",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            home_wins = away_wins = 0
            for date_obj in data.get("dates", []):
                for game in date_obj.get("games", []):
                    if game.get("status", {}).get("abstractGameState") != "Final":
                        continue
                    hd   = game["teams"]["home"]
                    ad   = game["teams"]["away"]
                    h_id = hd["team"]["id"]
                    a_id = ad["team"]["id"]
                    # Confirm it's exactly this pair (no partial matches)
                    if {h_id, a_id} != {home_mlb_id, away_mlb_id}:
                        continue
                    if h_id == home_mlb_id:
                        if hd.get("isWinner"):
                            home_wins += 1
                        elif ad.get("isWinner"):
                            away_wins += 1
                    else:
                        if ad.get("isWinner"):
                            away_wins += 1
                        elif hd.get("isWinner"):
                            home_wins += 1

            result = (home_wins, away_wins)
            self.cache.set(key, list(result))
            return result
        except Exception:
            return (0, 0)

    def _get_series_game_number(self, home_mlb_id: int, game_date: str) -> int:
        """
        Which game of the current series is today's game (1, 2, 3…)?
        Uses the seriesGameNumber field from the MLB Stats API schedule.
        Cached 1 h.
        """
        key    = f"upset_series_{game_date}_{home_mlb_id}"
        cached = self.cache.get(key, ttl=3600)
        if cached is not None:
            return int(cached)

        try:
            resp = requests.get(
                f"{MLB_API}/schedule",
                params={
                    "sportId":  1,
                    "date":     game_date,
                    "teamId":   home_mlb_id,
                    "gameType": "R",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            for date_obj in data.get("dates", []):
                for game in date_obj.get("games", []):
                    num = int(game.get("seriesGameNumber", 1))
                    self.cache.set(key, num)
                    return num

            self.cache.set(key, 1)
            return 1
        except Exception:
            return 1

    def _get_probable_pitchers(self, game_date: str) -> dict[int, Optional[int]]:
        """
        MLB Stats API → {mlb_team_id: pitcher_person_id or None}
        for all games on game_date (YYYY-MM-DD).
        Cached 1 h.
        """
        key    = f"upset_probable_{game_date}"
        cached = self.cache.get(key, ttl=3600)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{MLB_API}/schedule",
                params={
                    "sportId":  1,
                    "date":     game_date,
                    "hydrate":  "probablePitcher,teams",
                    "gameType": "R",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            result: dict[int, Optional[int]] = {}
            for date_obj in data.get("dates", []):
                for game in date_obj.get("games", []):
                    for side in ("home", "away"):
                        team_id = (
                            game.get("teams", {})
                                .get(side, {})
                                .get("team", {})
                                .get("id")
                        )
                        pitcher = (
                            game.get("teams", {})
                                .get(side, {})
                                .get("probablePitcher")
                        )
                        if team_id is not None:
                            result[int(team_id)] = (
                                int(pitcher["id"]) if pitcher else None
                            )
            self.cache.set(key, result)
            return result
        except Exception:
            return {}

    def _get_pitcher_starts(self, pitcher_id: int) -> list[float]:
        """
        Per-start ERA values (season game log) from MLB Stats API.
        Returns up to the full season's worth of starts.
        Cached 24 h.
        """
        key    = f"upset_pitcher_{pitcher_id}_{self.season}"
        cached = self.cache.get(key, ttl=86400)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{MLB_API}/people/{pitcher_id}/stats",
                params={
                    "stats":    "gameLog",
                    "group":    "pitching",
                    "season":   self.season,
                    "gameType": "R",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data   = resp.json()
            splits = data.get("stats", [{}])[0].get("splits", [])
            eras: list[float] = []
            for s in splits:
                stat = s.get("stat", {})
                if not stat.get("gamesStarted", 0):
                    continue
                ip = _parse_ip(stat.get("inningsPitched", "0"))
                er = float(stat.get("earnedRuns", 0) or 0)
                if ip < 0.333:
                    per_start_era = 27.0
                else:
                    per_start_era = round((er / ip) * 9, 2)
                eras.append(per_start_era)
            self.cache.set(key, eras)
            return eras
        except Exception:
            return []

    # ── Component scorers (each returns 0.0–1.0) ──────────────────────────────

    @staticmethod
    def _stdev_score(vals: list[float], low: float, high: float) -> float:
        """Normalise stdev of vals to [0, 1] between low (→0) and high (→1).
        Floor 0.05 when real data exists so consistent teams don't show 0.00."""
        if len(vals) < 3:
            return 0.35
        return max(0.05, _clamp((statistics.stdev(vals) - low) / (high - low)))

    def _score_run_scoring(self, games: list[dict]) -> float:
        """Offensive volatility: stdev of runs scored, last 15 games."""
        return self._stdev_score([g["r"] for g in games[-15:]], low=1.0, high=3.5)

    def _score_run_allowed(self, games: list[dict]) -> float:
        """Defensive volatility: stdev of runs allowed, last 15 games."""
        return self._stdev_score([g["ra"] for g in games[-15:]], low=1.0, high=3.5)

    @staticmethod
    def _get_streak(games: list[dict]) -> int:
        """Current W/L streak. Positive = win streak, negative = loss streak."""
        if not games:
            return 0
        is_win = games[-1]["won"]
        count  = 0
        for g in reversed(games):
            if g["won"] == is_win:
                count += 1
            else:
                break
        return count if is_win else -count

    @staticmethod
    def _score_streak(streak: int) -> float:
        n = abs(streak)
        if n >= 8: return 1.00
        if n >= 6: return 0.80
        if n >= 5: return 0.65
        if n >= 3: return 0.30
        if n >= 1: return 0.05  # 1-2 game streak: small but non-zero signal
        return 0.0              # 0.0 only when streak is genuinely zero

    @staticmethod
    def _score_underdog_wins(games: list[dict]) -> float:
        """
        Underdog win rate: wins in games where the team was the away team or
        entered with a running win% below .480 (best free proxy for positive
        moneyline, since historical per-game odds are not in the free MLB API).

        High underdog win rate → team consistently beats expectations → more
        upset potential in today's game.

        Formula: underdog_wins / underdog_games
          mapped from [0.28 → 0.0, 0.55 → 1.0], floor 0.05.
        Falls back to road games only when fewer than 5 underdog games flagged.
        """
        underdog_games = [g for g in games if g.get("is_underdog", not g["is_home"])]
        if len(underdog_games) < 5:
            # Fallback: road games (always underdog proxy)
            underdog_games = [g for g in games if not g["is_home"]]
        if len(underdog_games) < 3:
            return 0.25
        win_rate = sum(1 for g in underdog_games if g["won"]) / len(underdog_games)
        # 0.28 underdog W% → 0.0; 0.55 W% → 1.0
        return max(0.05, _clamp((win_rate - 0.28) / 0.27))

    @staticmethod
    def _score_blown_lead(games: list[dict]) -> float:
        """
        Games where team led in total runs after 6 innings but lost.
        Uses stored per-inning linescore data from the game log.
        0 blown leads → ~0.0; 4+ in last 20 games → 1.0.
        """
        recent = games[-20:]
        if len(recent) < 5:
            return 0.20
        blown = 0
        for g in recent:
            innings = g.get("innings", [])
            team_after_6 = sum(i["team_runs"] for i in innings if i["num"] <= 6)
            opp_after_6  = sum(i["opp_runs"]  for i in innings if i["num"] <= 6)
            # Only count as a "lead" if strictly ahead (not tied)
            if team_after_6 > opp_after_6 and not g["won"]:
                blown += 1
        return _clamp(blown / 4.0)

    @staticmethod
    def _score_h2h(home_wins: int, away_wins: int) -> float:
        """
        50/50 H2H split = max chaos (1.0); complete dominance = low chaos (0.0).
        Fewer than 2 games → neutral 0.50.
        """
        total = home_wins + away_wins
        if total < 2:
            return 0.50
        balance = min(home_wins, away_wins) / total  # 0.0 = sweep, 0.5 = even
        return _clamp(balance / 0.50)

    @staticmethod
    def _score_series_game(game_num: int) -> float:
        """
        Later games in a series → more chaos (bullpen fatigue, familiarity).
        Game 1 = 0.10, Game 2 = 0.50, Game 3+ = 1.0.
        """
        if game_num >= 3: return 1.00
        if game_num == 2: return 0.50
        return 0.10

    @staticmethod
    def _score_bullpen_volatility(games: list[dict]) -> float:
        """
        Average runs allowed in innings 7-9 per game (last 20 games).
        < 0.5 runs/game → 0.0; > 2.0 runs/game → 1.0.
        """
        recent = games[-20:]
        if len(recent) < 5:
            return 0.30
        late_ra = [
            sum(i["opp_runs"] for i in g.get("innings", []) if i["num"] >= 7)
            for g in recent
        ]
        if not late_ra:
            return 0.30
        avg = statistics.mean(late_ra)
        return _clamp((avg - 0.5) / 1.5)

    def _score_pitcher_consistency(self, eras: list[float]) -> float:
        """
        ERA variance across last 3 starts for a probable starter.
        High stdev (inconsistent pitcher) → higher upset potential.
        stdev ≤ 1.0 → 0.05 (consistent, but always a small non-zero signal);
        stdev ≈ 4.0 → 1.0 (very inconsistent).
        Returns 0.30 fallback when fewer than 2 starts available.
        """
        recent = eras[-3:] if len(eras) >= 3 else eras
        if len(recent) < 2:
            return 0.30
        # floor at 0.05: even a perfectly consistent pitcher carries a small
        # non-zero chaos signal (hot streaks can end, fatigue, etc.)
        return max(0.05, _clamp((statistics.stdev(recent) - 1.0) / 3.0))

    # ── Public entry point ─────────────────────────────────────────────────────

    def compute(
        self,
        home_team: str,
        away_team: str,
        game_date: str,     # "YYYY-MM-DD"
    ) -> dict:
        """
        Return upset factor dict:
          score (int 1-10), components (dict of 0-1 sub-scores),
          streak_home/away (int), h2h_home_wins/away_wins (int),
          series_game_number (int), confidence_reduction, kelly_reduction.
        """
        home_mlb_id = MLB_TEAM_IDS.get(home_team)
        away_mlb_id = MLB_TEAM_IDS.get(away_team)

        home_games = self._get_team_gamelog(home_mlb_id) if home_mlb_id else []
        away_games = self._get_team_gamelog(away_mlb_id) if away_mlb_id else []

        # H2H and series context
        if home_mlb_id and away_mlb_id:
            h2h_home_wins, h2h_away_wins = self._get_h2h(home_mlb_id, away_mlb_id)
        else:
            h2h_home_wins = h2h_away_wins = 0

        series_game_number = (
            self._get_series_game_number(home_mlb_id, game_date)
            if home_mlb_id else 1
        )

        # Probable pitcher ERA variance (last 3 starts each)
        probable  = self._get_probable_pitchers(game_date)
        home_pid  = probable.get(home_mlb_id) if home_mlb_id else None
        away_pid  = probable.get(away_mlb_id) if away_mlb_id else None
        home_eras = self._get_pitcher_starts(home_pid) if home_pid else []
        away_eras = self._get_pitcher_starts(away_pid) if away_pid else []
        pitcher_s = (
            self._score_pitcher_consistency(home_eras) +
            self._score_pitcher_consistency(away_eras)
        ) / 2

        # 1. Run scoring variance (stdev of runs per game)
        off_s = (
            self._score_run_scoring(home_games) +
            self._score_run_scoring(away_games)
        ) / 2

        # 2. Pitching variance (stdev of runs allowed per game)
        def_s = (
            self._score_run_allowed(home_games) +
            self._score_run_allowed(away_games)
        ) / 2

        # 3. Streak factor (larger streak = more volatile momentum)
        home_streak = self._get_streak(home_games)
        away_streak = self._get_streak(away_games)
        streak_s    = max(
            self._score_streak(home_streak),
            self._score_streak(away_streak),
        )

        # 4. Underdog win rate (away-game + sub-.480-running-W% proxy)
        ud_s = (
            self._score_underdog_wins(home_games) +
            self._score_underdog_wins(away_games)
        ) / 2

        # 5. Blown lead rate (led after 6, lost)
        bl_s = (
            self._score_blown_lead(home_games) +
            self._score_blown_lead(away_games)
        ) / 2

        # 6. H2H balance (close record = higher chaos)
        h2h_s = self._score_h2h(h2h_home_wins, h2h_away_wins)

        # 7. Bullpen volatility (late-inning RA from linescore)
        bp_s = (
            self._score_bullpen_volatility(home_games) +
            self._score_bullpen_volatility(away_games)
        ) / 2

        # 8. Series game number (later game = more fatigue/chaos)
        series_s = self._score_series_game(series_game_number)

        # Weighted combination (weights sum to 1.0)
        combined = (
            off_s     * 0.14 +   # run_scoring_variance
            def_s     * 0.08 +   # pitching_variance
            streak_s  * 0.20 +   # streak_factor
            ud_s      * 0.10 +   # underdog_win_rate
            bl_s      * 0.12 +   # blown_lead_rate
            h2h_s     * 0.08 +   # h2h_divergence
            bp_s      * 0.10 +   # bullpen_volatility
            pitcher_s * 0.13 +   # pitcher_consistency
            series_s  * 0.05     # series_game
        )
        # Sum: 0.14+0.08+0.20+0.10+0.12+0.08+0.10+0.13+0.05 = 1.00

        score = max(1, min(10, round(combined * 9.0 + 1.0)))

        if score >= 7:
            conf_reduction  = 0.08
            kelly_reduction = 0.25
        elif score >= 4:
            conf_reduction  = 0.03
            kelly_reduction = 0.10
        else:
            conf_reduction  = 0.0
            kelly_reduction = 0.0

        # Count underdog games played this season (for display)
        home_ud_total  = sum(1 for g in home_games if g.get("is_underdog", not g["is_home"]))
        away_ud_total  = sum(1 for g in away_games if g.get("is_underdog", not g["is_home"]))
        home_ud_wins   = sum(1 for g in home_games if g.get("is_underdog", not g["is_home"]) and g["won"])
        away_ud_wins   = sum(1 for g in away_games if g.get("is_underdog", not g["is_home"]) and g["won"])

        return {
            "score": score,
            "components": {
                "run_scoring_variance": round(off_s,     3),
                "pitching_variance":    round(def_s,     3),
                "streak_factor":        round(streak_s,  3),
                "underdog_win_rate":    round(ud_s,      3),
                "blown_lead_rate":      round(bl_s,      3),
                "h2h_divergence":       round(h2h_s,     3),
                "bullpen_volatility":   round(bp_s,      3),
                "pitcher_consistency":  round(pitcher_s, 3),
                "series_game":          round(series_s,  3),
            },
            "streak_home":            home_streak,
            "streak_away":            away_streak,
            "h2h_home_wins":          h2h_home_wins,
            "h2h_away_wins":          h2h_away_wins,
            "series_game_number":     series_game_number,
            # Underdog record this season (wins / games played as underdog)
            "underdog_home_wins":     home_ud_wins,
            "underdog_home_games":    home_ud_total,
            "underdog_away_wins":     away_ud_wins,
            "underdog_away_games":    away_ud_total,
            "confidence_reduction":   conf_reduction,
            "kelly_reduction":        kelly_reduction,
        }
