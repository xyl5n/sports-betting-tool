"""
MLB feature engineering — 24 features covering team stats, starting pitcher,
ballpark, weather, bullpen, lineup confirmation, line movement, and season trend.

build_for_game()      — full 24-feature vector for live prediction
build_training_row()  — team-stats only (new features at neutral baselines)

Feature index 23 (trend_diff): (home last-20 win% − home season win%) minus
the same quantity for the away team.  Positive = home team has been trending
upward relative to the away team within the current season.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np

from .batter_splits_client import get_batter_splits_client
from .bullpen_client import get_bullpen_client
from .game_store import GameStore
from .lineup_client import get_lineup_client
from .line_tracker import record_and_get_movement
from .park_factors import get_park_factors
from .pitcher_client import get_pitcher_client
from .pitcher_splits_client import get_pitcher_splits_client
from .sports_config import MLB_FEATURES, CURRENT_SEASON
from .utils import _safe  # shared across all feature builders
from .weather_client import get_game_weather

N_MLB_FEATURES = len(MLB_FEATURES)   # 30 after adding player-level + composite features

# Neutral / baseline values used when real data is unavailable or during training.
# All "_diff" features come out to zero in the training row when both SPs are
# at neutral, so the LR/XGB scaler centres them naturally.
_NEUTRAL_SP = {
    "era": 4.50, "whip": 1.30, "k_rate": 0.215,
    "bb9": 3.30, "era_home": 4.50, "era_away": 4.50, "last3_era": 4.50,
    "hand": 0, "rest": 4,
}
_NEUTRAL_BULLPEN = {"era": 4.20, "fatigue": 2}

# League-wide stat baselines used by the composite formulas
_LEAGUE = {
    "k_rate":   0.215, "k_rate_std":   0.05,
    "era":      4.50,  "era_std":      1.5,
    "whip":     1.30,  "whip_std":     0.30,
    "ops":      0.720,                          # avg lineup OPS vs avg pitcher hand
}


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


# ── Composite feature helpers ─────────────────────────────────────────────────

def _pitcher_dominance(sp: dict) -> float:
    """
    Z-score composite: z(K%) − z(ERA) − z(WHIP).
    Positive → pitcher is dominant relative to league average.
    Returns 0.0 when sp is at neutral baseline.
    """
    z_k = (sp.get("k_rate", _LEAGUE["k_rate"]) - _LEAGUE["k_rate"]) / _LEAGUE["k_rate_std"]
    z_e = (sp.get("era",    _LEAGUE["era"])    - _LEAGUE["era"])    / _LEAGUE["era_std"]
    z_w = (sp.get("whip",   _LEAGUE["whip"])   - _LEAGUE["whip"])   / _LEAGUE["whip_std"]
    return float(z_k - z_e - z_w)


def _lineup_vulnerability(lineup_top5_ops_vs_hand: float | None) -> float:
    """
    Lineup score = mean OPS of top-5 batters vs opposing pitcher's hand − league avg OPS.
    Positive → dangerous lineup, negative → vulnerable lineup.
    Returns 0.0 when no per-batter data is available (historical training rows).
    """
    if lineup_top5_ops_vs_hand is None:
        return 0.0
    return float(lineup_top5_ops_vs_hand - _LEAGUE["ops"])


def _blowout_probability(
    sp_era_diff:        float,
    bullpen_era_diff:   float,
    net_run_diff:       float,
    sp_recent_form_diff:float,
) -> float:
    """
    Logistic of a weighted advantage score; estimates P(home wins by 2+) before
    bookmaker odds enter the picture. Sigma chosen so a one-run advantage maps
    to roughly p≈0.62. Independent of run-line market — purely from team/SP signals.
    """
    score = (0.40 * net_run_diff
             + 0.30 * sp_era_diff
             + 0.20 * bullpen_era_diff
             + 0.10 * sp_recent_form_diff)
    sigma = 2.0
    # Stable logistic via np.exp; clip to [0.02, 0.98] to keep LR scaler well-behaved.
    p = 1.0 / (1.0 + float(np.exp(-score / sigma)))
    return float(np.clip(p, 0.02, 0.98))


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
    home_lineup_ops_vs_away_hand: float | None = None,
    away_lineup_ops_vs_home_hand: float | None = None,
) -> np.ndarray:
    """Build the 30-element feature vector in MLB_FEATURES order."""
    net_h = h["ppg"] - h["papg"]
    net_a = a["ppg"] - a["papg"]

    # Season trend: (home last20 win% − home season win%) − (away last20 win% − away season win%)
    # Positive = home team has been improving relative to away within this season
    trend_diff = h.get("season_trend", 0.0) - a.get("season_trend", 0.0)

    # Pre-compute the diffs used by the composites so we don't recompute below
    sp_era_diff      = away_sp["era"]  - home_sp["era"]
    bullpen_era_diff = away_bp["era"]  - home_bp["era"]
    bb9_diff         = away_sp.get("bb9", 3.30) - home_sp.get("bb9", 3.30)
    # Venue-context ERA: away SP's road ERA vs home SP's home ERA. Pos = home edge.
    sp_split_era_diff = (
        away_sp.get("era_away", away_sp["era"])
        - home_sp.get("era_home", home_sp["era"])
    )
    sp_recent_form_diff = (
        away_sp.get("last3_era", away_sp["era"])
        - home_sp.get("last3_era", home_sp["era"])
    )
    pitcher_dom_diff = _pitcher_dominance(home_sp) - _pitcher_dominance(away_sp)
    lineup_vuln_diff = (
        _lineup_vulnerability(home_lineup_ops_vs_away_hand)
        - _lineup_vulnerability(away_lineup_ops_vs_home_hand)
    )
    blowout_prob = _blowout_probability(
        sp_era_diff       = sp_era_diff,
        bullpen_era_diff  = bullpen_era_diff,
        net_run_diff      = (net_h - net_a),
        sp_recent_form_diff = sp_recent_form_diff,
    )

    return np.array([
        # ── Team statistics ───────────────────────────────────────────────────
        net_h - net_a,                             # 0  net_run_diff
        h["ppg"]  - a["ppg"],                      # 1  rpg_diff
        h["papg"] - a["papg"],                     # 2  rapg_diff
        h["win_pct"] - a["win_pct"],               # 3  win_pct_diff
        h["home_win_pct"] - a["away_win_pct"],     # 4  home_away_split_diff
        h["last10_win_pct"] - a["last10_win_pct"], # 5  last10_diff
        h["hits_pg"]   - a["hits_pg"],             # 6  hits_diff
        a["errors_pg"] - h["errors_pg"],           # 7  errors_diff (pos = home fewer)
        home_implied_prob,                          # 8  home_implied_prob
        run_line if run_line is not None else -1.5, # 9  run_line
        # ── Starting pitcher ──────────────────────────────────────────────────
        sp_era_diff,                                # 10 sp_era_diff   (pos = home better)
        away_sp["whip"]   - home_sp["whip"],        # 11 sp_whip_diff
        home_sp["k_rate"] - away_sp["k_rate"],      # 12 sp_k_rate_diff
        float(home_sp["rest"]),                     # 13 home_sp_rest
        float(away_sp["rest"]),                     # 14 away_sp_rest
        _hand_adv(home_sp["hand"], away_sp["hand"]),# 15 sp_hand_adv
        # ── Ballpark & environment ────────────────────────────────────────────
        park_run,                                   # 16 park_run_factor
        wind_speed,                                 # 17 wind_speed
        wind_dir,                                   # 18 wind_direction
        # ── Bullpen ───────────────────────────────────────────────────────────
        bullpen_era_diff,                           # 19 bullpen_era_diff
        away_bp["fatigue"] - home_bp["fatigue"],    # 20 bullpen_fatigue_diff
        # ── Lineup ───────────────────────────────────────────────────────────
        lineup_confirmed,                           # 21 lineup_confirmed
        # ── Line movement ────────────────────────────────────────────────────
        line_movement,                              # 22 line_movement
        # ── Season trend ─────────────────────────────────────────────────────
        trend_diff,                                 # 23 trend_diff
        # ── Player-level pitcher features ─────────────────────────────────────
        bb9_diff,                                   # 24 bb9_diff
        sp_split_era_diff,                          # 25 sp_split_era_diff
        sp_recent_form_diff,                        # 26 sp_recent_form_diff
        # ── Composite features ───────────────────────────────────────────────
        pitcher_dom_diff,                           # 27 pitcher_dominance_diff
        lineup_vuln_diff,                           # 28 lineup_vuln_diff (neutral in historical)
        blowout_prob,                               # 29 blowout_prob
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

        # Starting pitchers (basic season stats + handedness)
        pitcher_data = get_pitcher_client().get_starters_for_game(
            home_team, away_team, game_date
        )
        home_sp = dict(pitcher_data["home"])
        away_sp = dict(pitcher_data["away"])

        # Extended pitcher splits (BB/9, home/away ERA, last-3-starts ERA) ──
        # We need pitcher IDs from the schedule entry. Look them up via the
        # same hydrated schedule the PitcherClient already cached.
        sp_client = get_pitcher_splits_client()
        sched = get_pitcher_client()._get_schedule(game_date)  # cached
        home_pid = away_pid = None
        from .utils import _team_tokens
        for entry in sched:
            h_ov = len(_team_tokens(entry.get("home_name", "")) & _team_tokens(home_team))
            a_ov = len(_team_tokens(entry.get("away_name", "")) & _team_tokens(away_team))
            if h_ov >= 1 and a_ov >= 1:
                home_pid = (entry.get("home_pitcher") or {}).get("id")
                away_pid = (entry.get("away_pitcher") or {}).get("id")
                break

        season = CURRENT_SEASON
        home_splits = sp_client.get_splits(home_pid, season)
        away_splits = sp_client.get_splits(away_pid, season)
        # Map pitcher_splits_client keys -> _assemble's expected keys.
        home_sp.setdefault("bb9",       home_splits["bb_per_9"])
        home_sp.setdefault("era_home",  home_splits["home_era"])
        home_sp.setdefault("era_away",  home_splits["away_era"])
        home_sp.setdefault("last3_era", home_splits["last3_era"])
        away_sp.setdefault("bb9",       away_splits["bb_per_9"])
        away_sp.setdefault("era_home",  away_splits["home_era"])
        away_sp.setdefault("era_away",  away_splits["away_era"])
        away_sp.setdefault("last3_era", away_splits["last3_era"])
        sp_client.save()

        # Top-5 batter OPS vs opposing starter's hand (for Lineup Vulnerability)
        bs_client = get_batter_splits_client()
        # Home lineup faces the away starter -> need top-5 home batters vs away hand
        home_lineup_inputs = bs_client.get_lvs_inputs(
            team_id=home_id, season=season, opp_hand=away_sp.get("hand", 0)
        )
        # Away lineup faces the home starter -> need top-5 away batters vs home hand
        away_lineup_inputs = bs_client.get_lvs_inputs(
            team_id=away_id, season=season, opp_hand=home_sp.get("hand", 0)
        )
        bs_client.save()

        home_lineup_ops = (
            sum(r["ops"] for r in home_lineup_inputs) / len(home_lineup_inputs)
            if home_lineup_inputs else None
        )
        away_lineup_ops = (
            sum(r["ops"] for r in away_lineup_inputs) / len(away_lineup_inputs)
            if away_lineup_inputs else None
        )

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
            home_lineup_ops_vs_away_hand=home_lineup_ops,
            away_lineup_ops_vs_home_hand=away_lineup_ops,
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
        New player-level features (bb9_diff, sp_split_era_diff, sp_recent_form_diff,
        composites) inherit the neutral SP dict and therefore evaluate to 0 here.
        lineup_vuln_diff is None → 0 (batter data not backfilled in historical).
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
            home_lineup_ops_vs_away_hand=None,
            away_lineup_ops_vs_home_hand=None,
        )
