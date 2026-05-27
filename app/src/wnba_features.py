"""
WNBA feature engineering — 17 features for moneyline/spread, 8 for totals.

Features are diffs or sums designed to be informative for XGBoost + LogReg.
Back-to-back and star-player availability flags require game date context.

Feature 15 (index 15): trend_diff  — momentum signal (last-10 win% − season win%)
Feature 16 (index 16): college_adj_diff — college performance adjustment for
    rookies and 2nd-year players; populated via WNBACollegeClient; 0.0 at training.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .sports_config import WNBA_FEATURES, WNBA_TOTALS_FEATURES  # noqa: F401 (re-exported)
from .utils import _safe  # shared across all feature builders


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _implied_prob_from_odds(american_odds) -> float:
    """Convert American odds to implied probability (no vig removal)."""
    try:
        odds = float(american_odds)
    except (TypeError, ValueError):
        return 0.5
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        return abs(odds) / (abs(odds) + 100.0)


# ---------------------------------------------------------------------------
# Feature builder
# ---------------------------------------------------------------------------

class WNBAFeatureBuilder:
    """
    Builds fixed-length 17-feature vectors from WNBA game dicts.

    *client* must be a WNBAStatsClient instance that has already had
    ``load(season)`` called so team stats are available.

    College adjustment data is optional.  Call ``set_college_adjustments()``
    before ``build_for_game()`` to enable feature 16 (college_adj_diff).
    """

    def __init__(self, client) -> None:
        self.client = client
        # Populated by set_college_adjustments(); keyed by team_id
        self._college_adjs: dict[int, float] = {}
        self._college_diag: dict[int, list] = {}

    # ------------------------------------------------------------------
    # College adjustment injection
    # ------------------------------------------------------------------

    def set_college_adjustments(
        self,
        adjs: dict[int, float],
        diag: Optional[dict[int, list]] = None,
    ) -> None:
        """
        Inject college-performance adjustments from WNBACollegeClient.

        Parameters
        ----------
        adjs : {team_id: float}  adjustment factor for each team
        diag : {team_id: list}   optional per-player diagnostic records
        """
        self._college_adjs = adjs or {}
        self._college_diag = diag or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_for_game(
        self, game: dict
    ) -> Optional[tuple[np.ndarray, dict]]:
        """
        Build (ml_vec, meta) for a live/upcoming game dict.

        Returns None if either team cannot be resolved or stats are missing.
        """
        home_team_dict = self.client.find_team(game.get("home_team", ""))
        away_team_dict = self.client.find_team(game.get("away_team", ""))
        if home_team_dict is None or away_team_dict is None:
            return None

        home_id = home_team_dict["id"]
        away_id = away_team_dict["id"]

        h = self.client.get_team_stats(home_id)
        a = self.client.get_team_stats(away_id)
        if h is None or a is None:
            return None

        # Optional enrichment signals.  The full odds-path client
        # (WNBAStatsClient) supplies player splits, back-to-back detection,
        # head-to-head and referee foul-rate; the no-odds prefetch passes a
        # bare GameStore that only computes team stats.  Default each missing
        # source to a neutral value (via getattr) so a thin client still
        # produces a feature vector instead of raising AttributeError -- which
        # was silently caught upstream and skipped every no-odds WNBA game.
        get_players = getattr(self.client, "get_player_stats", None)
        hp = get_players(home_id) if callable(get_players) else {}
        ap = get_players(away_id) if callable(get_players) else {}

        game_date = (game.get("commence_time", "") or "")[:10]
        is_b2b = getattr(self.client, "_is_b2b", None)
        home_b2b = 1.0 if (callable(is_b2b) and is_b2b(home_id, game_date)) else 0.0
        away_b2b = 1.0 if (callable(is_b2b) and is_b2b(away_id, game_date)) else 0.0

        get_h2h = getattr(self.client, "get_h2h", None)
        hw, aw = get_h2h(home_id, away_id) if callable(get_h2h) else (0, 0)
        h2h_total = hw + aw
        h2h_diff = (hw - aw) / max(h2h_total, 1)

        get_ref = getattr(self.client, "get_referee_foul_rate", None)
        ref_rate = get_ref(game.get("id")) if callable(get_ref) else 40.0

        # Market data — OddsClient stores vig-free implied probability directly
        home_implied_prob = float(game.get("home_implied_prob") or 0.5)

        spread = _safe(
            game.get("run_line_point") or game.get("spread"), default=0.0
        )

        # College adjustment (0.0 if WNBACollegeClient not configured)
        college_adj_home = self._college_adjs.get(home_id, 0.0)
        college_adj_away = self._college_adjs.get(away_id, 0.0)

        ml_vec = self._build_ml_vec(
            h, a, hp, ap,
            h2h_diff, home_b2b, away_b2b,
            ref_rate, spread, home_implied_prob,
            college_adj_home=college_adj_home,
            college_adj_away=college_adj_away,
        )
        totals_vec = self._build_totals_vec(
            h, a, hp, ap, ref_rate, home_b2b, away_b2b
        )

        meta = {
            "home_id": home_id,
            "away_id": away_id,
            "home_stats": h,
            "away_stats": a,
            "home_player": hp,
            "away_player": ap,
            "h2h": {"home_wins": hw, "away_wins": aw, "diff": h2h_diff},
            "home_b2b": bool(home_b2b),
            "away_b2b": bool(away_b2b),
            "ref_foul_rate": ref_rate,
            "totals_vec": totals_vec,
        }
        return ml_vec, meta

    def build_training_row(
        self, home_id: int, away_id: int
    ) -> Optional[np.ndarray]:
        """
        Build a 17-feature vector for training using neutral context values.
        college_adj_diff is always 0.0 for historical training rows because
        player experience status and college stats are not backfilled.
        Returns None if team stats are unavailable.
        """
        h = self.client.get_team_stats(home_id)
        a = self.client.get_team_stats(away_id)
        if h is None or a is None:
            return None

        neutral_player = {"name": "", "pts_pg": 15.0, "is_available": 1.0}
        return self._build_ml_vec(
            h, a,
            neutral_player, neutral_player,
            h2h_diff=0.0,
            h_b2b=0.0, a_b2b=0.0,
            ref_rate=40.0,
            spread=0.0,
            home_impl=0.5,
            # Neutral baseline values for training — no college data backfill
            college_adj_home=0.0,
            college_adj_away=0.0,
        )

    def build_totals_training_row(
        self, home_id: int, away_id: int
    ) -> Optional[np.ndarray]:
        """
        Build an 8-feature totals vector for training using neutral context values.
        Returns None if team stats are unavailable.
        """
        h = self.client.get_team_stats(home_id)
        a = self.client.get_team_stats(away_id)
        if h is None or a is None:
            return None

        neutral_player = {"name": "", "pts_pg": 15.0, "is_available": 1.0}
        return self._build_totals_vec(
            h, a,
            neutral_player, neutral_player,
            ref_rate=40.0,
            h_b2b=0.0, a_b2b=0.0,
        )

    def build_totals_from_meta(self, meta: dict) -> Optional[np.ndarray]:
        """
        Build an 8-feature totals vector from a pre-computed meta dict
        (as returned by build_for_game).
        """
        h = meta.get("home_stats")
        a = meta.get("away_stats")
        if h is None or a is None:
            return None

        hp = meta.get("home_player", {"name": "", "pts_pg": 15.0, "is_available": 1.0})
        ap = meta.get("away_player", {"name": "", "pts_pg": 15.0, "is_available": 1.0})
        ref_rate = meta.get("ref_foul_rate", 40.0)
        h_b2b = 1.0 if meta.get("home_b2b") else 0.0
        a_b2b = 1.0 if meta.get("away_b2b") else 0.0

        return self._build_totals_vec(h, a, hp, ap, ref_rate, h_b2b, a_b2b)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_ml_vec(
        self,
        h: dict,
        a: dict,
        hp: dict,
        ap: dict,
        h2h_diff: float,
        h_b2b: float,
        a_b2b: float,
        ref_rate: float,
        spread: float,
        home_impl: float,
        college_adj_home: float = 0.0,
        college_adj_away: float = 0.0,
    ) -> np.ndarray:
        """
        Build the 17-feature moneyline/spread vector.

        Features align 1-for-1 with WNBA_FEATURES (indices 0-16).
          0-14 : original 15 features
          15   : trend_diff — momentum (last-10 win% − season win%), home minus away
          16   : college_adj_diff — college perf adj, home minus away (0.0 at training)
        """
        h_net = _safe(h.get("ppg")) - _safe(h.get("papg"))
        a_net = _safe(a.get("ppg")) - _safe(a.get("papg"))

        # Momentum: season_trend = last10_win_pct - win_pct (stored on stats dict)
        h_trend = _safe(h.get("season_trend"), 0.0)
        a_trend = _safe(a.get("season_trend"), 0.0)

        vec = np.array([
            h_net - a_net,                                              # net_scoring_diff
            _safe(h.get("ppg")) - _safe(a.get("ppg")),                  # ppg_diff
            _safe(h.get("papg")) - _safe(a.get("papg")),                # papg_diff
            _safe(h.get("win_pct")) - _safe(a.get("win_pct")),          # win_pct_diff
            _safe(h.get("home_win_pct")) - _safe(a.get("away_win_pct")),# home_away_split_diff
            _safe(h.get("last10_win_pct")) - _safe(a.get("last10_win_pct")),  # last10_diff
            float(h2h_diff),                                            # h2h_diff
            _safe(hp.get("pts_pg")) - _safe(ap.get("pts_pg")),          # top_pts_diff
            _safe(hp.get("is_available"), 1.0),                         # home_star_avail
            _safe(ap.get("is_available"), 1.0),                         # away_star_avail
            float(h_b2b),                                               # home_b2b
            float(a_b2b),                                               # away_b2b
            _safe(h.get("pace")) - _safe(a.get("pace")),                # pace_diff
            float(home_impl),                                           # home_implied_prob
            float(spread),                                              # spread
            float(h_trend - a_trend),                                   # trend_diff (momentum)
            float(college_adj_home - college_adj_away),                 # college_adj_diff
        ], dtype=np.float32)

        return vec

    def _build_totals_vec(
        self,
        h: dict,
        a: dict,
        hp: dict,
        ap: dict,
        ref_rate: float,
        h_b2b: float,
        a_b2b: float,
    ) -> np.ndarray:
        """
        Build the 8-feature totals vector.

        Features align 1-for-1 with WNBA_TOTALS_FEATURES.
        """
        vec = np.array([
            _safe(h.get("ppg")) + _safe(a.get("ppg")),    # combined_ppg
            _safe(h.get("papg")) + _safe(a.get("papg")),  # combined_papg
            _safe(h.get("pace")) + _safe(a.get("pace")),  # combined_pace
            _safe(hp.get("pts_pg"), 15.0),                # home_star_pts
            _safe(ap.get("pts_pg"), 15.0),                # away_star_pts
            float(ref_rate),                              # ref_foul_rate
            float(h_b2b),                                 # home_b2b
            float(a_b2b),                                 # away_b2b
        ], dtype=np.float32)

        return vec
