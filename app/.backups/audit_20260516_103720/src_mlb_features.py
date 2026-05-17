"""
MLB feature engineering — 23 features covering team stats, starting pitcher,
ballpark, weather, bullpen, lineup confirmation, and line movement.

build_for_game()      — full 23-feature vector for live prediction
build_training_row()  — team-stats only (new features at neutral baselines)
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np

from .bullpen_client import get_bullpen_client
from .game_store import GameStore
from .lineup_client import get_lineup_client
from .line_tracker import record_and_get_movement
from .park_factors import get_park_factors
from .pitcher_client import get_pitcher_client
from .sports_config import MLB_FEATURES
from .weather_client import get_game_weather

N_MLB_FEATURES = len(MLB_FEATURES)

# Neutral / baseline values used when real data is unavailable or during training
_NEUTRAL_SP = {"era": 4.50, "whip": 1.30, "k_rate": 0.215, "hand": 0, "rest": 4}
_NEUTRAL_BULLPEN = {"era": 4.20, "fatigue": 2}


def _safe(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if (f == f) else default
    except (TypeError, ValueError):
        return default


def _hand_adv(home_hand: int, away_hand: int) -> float:
    """
    +1  home pitcher is LHP (slight home edge from park/scouting familiarity)
    -1  away pitcher is LHP
     0  same handedness
    """
    if home_hand == 1 and away_hand == 0:
        return 1.0
    if home_hand == 0 and away_hand == 1:
        return -1.0
    return 0.0


def _assemble(
    h: dict,
    a: dict,
    home_implied_prob: float,
    run_line: float,
    home_sp: dict,
    away_sp: dict,
    park_run: float,
    wind_speed: float,
    wind_dir: float,
    home_bp: dict,
    away_bp: dict,
    lineup_confirmed: float,
    line_movement: float,
) -> np.ndarray:
    """Build the 23-element feature vector in MLB_FEATURES order."""
    net_h = h["ppg"] - h["papg"]
    net_a = a["ppg"] - a["papg"]

    return np.array([
        # ── Team statistics ───────────────────────────────────────────────────
        net_h - net_a,                             # net_run_diff
        h["ppg"]  - a["ppg"],                      # rpg_diff
        h["papg"] - a["papg"],                     # rapg_diff
        h["win_pct"] - a["win_pct"],               # win_pct_diff
        h["home_win_pct"] - a["away_win_pct"],     # home_away_split_diff
        h["last10_win_pct"] - a["last10_win_pct"], # last10_diff
        h["hits_pg"]   - a["hits_pg"],             # hits_diff
        a["errors_pg"] - h["errors_pg"],           # errors_diff (pos = home fewer)
        home_implied_prob,                          # home_implied_prob
        run_line if run_line is not None else -1.5, # run_line
        # ── Starting pitcher ──────────────────────────────────────────────────
        away_sp["era"]    - home_sp["era"],         # sp_era_diff   (pos = home better)
        away_sp["whip"]   - home_sp["whip"],        # sp_whip_diff
        home_sp["k_rate"] - away_sp["k_rate"],      # sp_k_rate_diff
        float(home_sp["rest"]),                     # home_sp_rest
        float(away_sp["rest"]),                     # away_sp_rest
        _hand_adv(home_sp["hand"], away_sp["hand"]),# sp_hand_adv
        # ── Ballpark & environment ────────────────────────────────────────────
        park_run,                                   # park_run_factor
        wind_speed,                                 # wind_speed
        wind_dir,                                   # wind_direction
        # ── Bullpen ───────────────────────────────────────────────────────────
        away_bp["era"]     - home_bp["era"],        # bullpen_era_diff
        away_bp["fatigue"] - home_bp["fatigue"],    # bullpen_fatigue_diff
        # ── Lineup ───────────────────────────────────────────────────────────
        lineup_confirmed,                           # lineup_confirmed
        # ── Line movement ────────────────────────────────────────────────────
        line_movement,                              # line_movement
    ], dtype=np.float32)


class MLBFeatureBuilder:
    def __init__(self, store: GameStore):
        self.store = store

    # ── Live prediction ───────────────────────────────────────────────────────

    def build_for_game(self, game: dict) -> Optional[tuple[np.ndarray, dict]]:
        """Full 23-feature vector for an upcoming game using all data sources."""
        home_team_obj = self.store.find_team(game["home_team"])
        away_team_obj = self.store.find_team(game["away_team"])
        if home_team_obj is None or away_team_obj is None:
            return None

        home_id = home_team_obj["id"]
        away_id = away_team_obj["id"]
        h = self.store.get_team_stats(home_id)
        a = self.store.get_team_stats(away_id)
        if h is None or a is None:
            return None

        home_team  = game["home_team"]
        away_team  = game["away_team"]
        game_date  = game.get("commence_time", "")[:10] or date.today().isoformat()
        commence   = game.get("commence_time", "")

        # Starting pitchers
        pitcher_data = get_pitcher_client().get_starters_for_game(
            home_team, away_team, game_date
        )
        home_sp = pitcher_data["home"]
        away_sp = pitcher_data["away"]

        # Ballpark
        park_run, _ = get_park_factors(home_team)

        # Weather
        wx = get_game_weather(home_team, commence) if commence else {
            "wind_speed": 0.0, "wind_direction": 0.0, "temperature": 72.0
        }

        # Bullpen
        bp = get_bullpen_client().get_bullpen_for_game(home_team, away_team, game_date)
        home_bp = bp["home"]
        away_bp = bp["away"]

        # Lineup confirmation
        lineup_ok = get_lineup_client().is_lineup_confirmed(home_team, away_team, game_date)

        # Line movement
        line_move = record_and_get_movement(
            game_id=game.get("id", ""),
            current_home_odds=game.get("h2h_home_odds"),
            current_away_odds=game.get("h2h_away_odds"),
        )

        vec = _assemble(
            h=h, a=a,
            home_implied_prob=game.get("home_implied_prob", 0.54),
            run_line=game.get("spread") or -1.5,
            home_sp=home_sp, away_sp=away_sp,
            park_run=park_run,
            wind_speed=_safe(wx.get("wind_speed"), 0.0),
            wind_dir=_safe(wx.get("wind_direction"), 0.0),
            home_bp=home_bp, away_bp=away_bp,
            lineup_confirmed=lineup_ok,
            line_movement=line_move,
        )

        meta = {
            "home_team":  home_team,
            "away_team":  away_team,
            "home_id":    home_id,
            "away_id":    away_id,
            "home_stats": h,
            "away_stats": a,
            "home_sp":    home_sp,
            "away_sp":    away_sp,
            "park_run_factor": park_run,
            "weather":    wx,
            "home_bp":    home_bp,
            "away_bp":    away_bp,
            "lineup_confirmed": lineup_ok,
            "line_movement":    line_move,
        }
        return vec, meta

    # ── Totals feature vector (9 features, absolute values / sums) ───────────

    def build_totals_from_meta(self, meta: dict) -> Optional[np.ndarray]:
        """Build 9-feature totals vector from a pre-computed meta dict."""
        h  = meta.get("home_stats") or {}
        a  = meta.get("away_stats") or {}
        if not h or not a:
            return None

        home_sp = meta.get("home_sp") or {}
        away_sp = meta.get("away_sp") or {}
        home_bp = meta.get("home_bp") or {}
        away_bp = meta.get("away_bp") or {}
        wx      = meta.get("weather") or {}

        return np.array([
            h.get("ppg",  4.5) + a.get("ppg",  4.5),   # combined_rpg
            h.get("papg", 4.5) + a.get("papg", 4.5),   # combined_rapg
            home_sp.get("era", 4.5) + away_sp.get("era", 4.5),  # combined_sp_era
            home_sp.get("k_rate", 0.215),               # home_sp_k_rate
            away_sp.get("k_rate", 0.215),               # away_sp_k_rate
            meta.get("park_run_factor", 1.0),           # park_run_factor
            _safe(wx.get("wind_speed"), 0.0),           # wind_speed
            home_bp.get("era", 4.2) + away_bp.get("era", 4.2),  # combined_bullpen_era
            _safe(wx.get("temperature"), 72.0),         # temperature
        ], dtype=np.float32)

    def build_totals_training_row(self, home_id: int, away_id: int) -> Optional[np.ndarray]:
        """Training-time totals vector — team RPG/RAPG only; everything else neutral."""
        h = self.store.get_team_stats(home_id)
        a = self.store.get_team_stats(away_id)
        if h is None or a is None:
            return None

        return np.array([
            h.get("ppg",  4.5) + a.get("ppg",  4.5),   # combined_rpg
            h.get("papg", 4.5) + a.get("papg", 4.5),   # combined_rapg
            9.0,    # combined_sp_era (4.5 * 2 neutral)
            0.215,  # home_sp_k_rate
            0.215,  # away_sp_k_rate
            1.0,    # park_run_factor
            0.0,    # wind_speed
            8.4,    # combined_bullpen_era (4.2 * 2 neutral)
            72.0,   # temperature
        ], dtype=np.float32)

    # ── Model training ────────────────────────────────────────────────────────

    def build_training_row(self, home_id: int, away_id: int) -> Optional[np.ndarray]:
        """
        Training-time row — team stats only.
        Game-specific features (pitcher, weather, bullpen, lineup, lines) are
        set to neutral baselines so XGBoost learns to use zero as the reference
        point; real deviations at inference time will then register as SHAP signal.
        """
        h = self.store.get_team_stats(home_id)
        a = self.store.get_team_stats(away_id)
        if h is None or a is None:
            return None

        return _assemble(
            h=h, a=a,
            home_implied_prob=0.54,
            run_line=-1.5,
            home_sp=dict(_NEUTRAL_SP), away_sp=dict(_NEUTRAL_SP),
            park_run=1.0,
            wind_speed=0.0, wind_dir=0.0,
            home_bp=dict(_NEUTRAL_BULLPEN), away_bp=dict(_NEUTRAL_BULLPEN),
            lineup_confirmed=0.0,
            line_movement=0.0,
        )
