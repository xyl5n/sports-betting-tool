"""
NFL feature engineering — built from GameStore computed stats.
"""
from typing import Optional
import numpy as np

from .game_store import GameStore
from .sports_config import NFL_FEATURES
from .utils import _safe  # shared across all feature builders

N_NFL_FEATURES = len(NFL_FEATURES)


class FeatureBuilder:
    def __init__(self, store: GameStore):
        self.store = store

    def _diff(self, home: dict, away: dict, key: str) -> float:
        return _safe(home.get(key, 0)) - _safe(away.get(key, 0))

    def build_for_ids(
        self,
        home_id: int,
        away_id: int,
        home_implied_prob: float = 0.55,
        spread: float = 0.0,
    ) -> Optional[np.ndarray]:
        h = self.store.get_team_stats(home_id)
        a = self.store.get_team_stats(away_id)
        if h is None or a is None:
            return None

        net_h = h["ppg"] - h["papg"]
        net_a = a["ppg"] - a["papg"]

        # home team's home win% vs away team's away win%
        home_away_split = h["home_win_pct"] - a["away_win_pct"]

        vec = np.array([
            net_h - net_a,                         # net_scoring_diff
            h["ppg"] - a["ppg"],                   # ppg_diff
            h["papg"] - a["papg"],                 # papg_diff
            h["win_pct"] - a["win_pct"],           # win_pct_diff
            home_away_split,                        # home_away_split_diff
            h["last5_win_pct"] - a["last5_win_pct"],  # last5_diff
            home_implied_prob,                     # home_implied_prob
            spread if spread is not None else 0.0, # spread
        ], dtype=np.float32)

        return vec

    def build_for_game(self, game: dict) -> Optional[tuple[np.ndarray, dict]]:
        home_team = self.store.find_team(game["home_team"])
        away_team = self.store.find_team(game["away_team"])

        if home_team is None or away_team is None:
            return None

        home_id = home_team["id"]
        away_id = away_team["id"]

        vec = self.build_for_ids(
            home_id=home_id,
            away_id=away_id,
            home_implied_prob=game.get("home_implied_prob", 0.55),
            spread=game.get("spread") or 0.0,
        )
        if vec is None:
            return None

        h = self.store.get_team_stats(home_id) or {}
        a = self.store.get_team_stats(away_id) or {}

        meta = {
            "home_team": game["home_team"],
            "away_team": game["away_team"],
            "home_id": home_id,
            "away_id": away_id,
            "home_injury_score": 1.0,   # not available on free plan
            "away_injury_score": 1.0,
            "home_stats": h,
            "away_stats": a,
        }
        return vec, meta

    def build_training_row(self, home_id: int, away_id: int) -> Optional[np.ndarray]:
        return self.build_for_ids(home_id=home_id, away_id=away_id,
                                  home_implied_prob=0.55, spread=0.0)
