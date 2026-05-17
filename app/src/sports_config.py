"""
Per-sport configuration: API endpoints, feature names, and heuristic weights.
Active sports: MLB (baseball_mlb) and WNBA (basketball_wnba).
Feature sets are limited to what the free API-Sports plan actually returns.
"""
from dataclasses import dataclass
import numpy as np

# ── Shared season constant ────────────────────────────────────────────────────
# Update this single value each new season; referenced by bullpen_client.py,
# retrain_mlb_models.py, and any other module that embeds the season in URLs.
CURRENT_SEASON: int = 2026


@dataclass
class SportConfig:
    name: str
    odds_key: str
    api_sports_base: str
    league_id: int
    feature_names: list[str]
    heuristic_weights: np.ndarray   # len == len(feature_names)
    heuristic_stds: np.ndarray
    home_field_logit: float = 0.15
    min_training_games: int = 30


# ── MLB ────────────────────────────────────────────────────────────────────────
# Team stats come from API-Sports game results (free plan).
# Pitcher / park / weather / bullpen / lineup / line-movement features are
# populated from the free MLB Stats API and Open-Meteo at prediction time;
# training rows use neutral baseline values for those columns.

MLB_FEATURES = [
    # ── Team statistics ───────────────────────────────────────────────────────
    "net_run_diff",         # (home RPG − RAPG) − (away RPG − RAPG)
    "rpg_diff",             # home RPG − away RPG
    "rapg_diff",            # home RAPG − away RAPG  (positive → home allows fewer)
    "win_pct_diff",         # home win% − away win%
    "home_away_split_diff", # home team's home win% − away team's away win%
    "last10_diff",          # last-10 win% differential
    "hits_diff",            # hits per game differential
    "errors_diff",          # errors per game diff (positive = home fewer errors)
    "home_implied_prob",    # market vig-free P(home wins)
    "run_line",             # run line (typically ±1.5; negative = home favoured)
    # ── Starting pitcher ─────────────────────────────────────────────────────
    "sp_era_diff",          # away SP ERA − home SP ERA  (positive = home pitcher better)
    "sp_whip_diff",         # away SP WHIP − home SP WHIP
    "sp_k_rate_diff",       # home SP K% − away SP K%
    "home_sp_rest",         # home SP days of rest (0–6+)
    "away_sp_rest",         # away SP days of rest
    "sp_hand_adv",          # +1 home LHP, −1 away LHP, 0 same handedness
    # ── Ballpark & environment ────────────────────────────────────────────────
    "park_run_factor",      # stadium run factor (1.16 = Coors, 0.91 = Marlins)
    "wind_speed",           # mph at game time (0 for domes)
    "wind_direction",       # degrees 0–360 (0 for domes)
    # ── Bullpen ───────────────────────────────────────────────────────────────
    "bullpen_era_diff",     # away team ERA − home team ERA  (positive = home bullpen better)
    "bullpen_fatigue_diff", # away games last 5 d − home games last 5 d
    # ── Lineup confirmation ───────────────────────────────────────────────────
    "lineup_confirmed",     # 1.0 if both starting lineups officially confirmed
    # ── Line movement ─────────────────────────────────────────────────────────
    "line_movement",        # current home implied prob − opening home implied prob
    # ── Season trend (recency signal) ────────────────────────────────────────
    "trend_diff",           # (home last-20 win% − home season win%) −
                            # (away last-20 win% − away season win%)
                            # positive = home team improving relative to away
    # ── Player-level pitcher features (24-26) ────────────────────────────────
    "bb9_diff",                 # away SP BB/9 − home SP BB/9 (pos = home pitcher better control)
    "sp_split_era_diff",        # (home SP away-ERA) − (home SP home-ERA)
                                # − [(away SP home-ERA) − (away SP away-ERA)]
                                # positive = home SP gets a venue boost vs away SP
    "sp_recent_form_diff",      # away SP last-3-start ERA − home SP last-3-start ERA
                                # positive = home pitcher is currently hotter
    # ── Composite features (27-29) ───────────────────────────────────────────
    "pitcher_dominance_diff",   # z(K%) − z(ERA) − z(WHIP) for home SP minus same for away SP
                                # positive = home pitcher dominates by composite measure
    "lineup_vuln_diff",         # (home top-5 OPS vs away SP hand) − (away top-5 OPS vs home SP hand)
                                # positive = home offence more dangerous against opposing starter
                                # NOTE: NEUTRAL in historical training (batter splits not backfilled)
    "blowout_prob",             # logistic(sum of pitcher/bullpen/run advantages) ∈ [0,1]
                                # informational signal feeding the run-line classifier
]

# ── XGBoost "pure confidence" feature selection ──────────────────────────────
# The XGBoost moneyline + run-line classifiers train on a SUBSET of MLB_FEATURES
# that excludes every column derived from the betting market. This makes the
# model's probability output a pure team / pitcher / situation signal, with
# zero reference to the odds line.  Edge against the market is then computed
# as a separate downstream step: edge = model_prob - implied_market_prob.
#
# Excluded names:
#   home_implied_prob -- vig-free P(home wins) parsed from the market
#   run_line          -- the bookmaker's spread (almost always +/- 1.5 in MLB)
#   line_movement     -- change in implied prob since open
MLB_XGB_ODDS_FEATURE_NAMES: tuple[str, ...] = (
    "home_implied_prob",
    "run_line",
    "line_movement",
)
MLB_XGB_CONFIDENCE_COLUMNS: tuple[int, ...] = tuple(
    i for i, name in enumerate(MLB_FEATURES)
    if name not in MLB_XGB_ODDS_FEATURE_NAMES
)
MLB_XGB_CONFIDENCE_FEATURE_NAMES: tuple[str, ...] = tuple(
    name for name in MLB_FEATURES if name not in MLB_XGB_ODDS_FEATURE_NAMES
)

MLB = SportConfig(
    name="MLB",
    odds_key="baseball_mlb",
    api_sports_base="https://v1.baseball.api-sports.io",
    league_id=1,
    feature_names=MLB_FEATURES,
    heuristic_weights=np.array([
        # Team stats (10) — scaled from factor analysis combined scores (max=0.15)
        # net_run_diff (0.955), rpg_diff (0.732), rapg_diff (0.811),
        # win_pct_diff (0.967), home_away_split_diff (0.768), last10_diff (0.575)
        0.15, 0.11, 0.13, 0.15, 0.12, 0.09, 0.03, 0.02, 0.00, 0.00,
        # Starting pitcher (6) — ERA diff most predictive single feature
        0.14, 0.09, 0.05,  0.03, -0.03, 0.02,
        # Park & environment (3) — park_run_factor important for totals (0.466)
        0.07, 0.00, 0.00,
        # Bullpen (2)
        0.08, 0.03,
        # Lineup (1)
        0.01,
        # Line movement (1)
        0.07,
        # Season trend (1) — within-season momentum signal
        0.05,
        # Player-level pitcher features (3)
        0.04,   # bb9_diff
        0.03,   # sp_split_era_diff
        0.04,   # sp_recent_form_diff
        # Composites (3) — modest weights; LR will reweight via training
        0.06,   # pitcher_dominance_diff
        0.04,   # lineup_vuln_diff   (neutral in historical → effective weight ~0)
        0.05,   # blowout_prob
    ], dtype=np.float32),
    heuristic_stds=np.array([
        # Team stats
        2.0, 1.5, 1.5, 0.20, 0.25, 0.25, 1.5, 0.30, 0.10, 1.5,
        # Starting pitcher
        1.5, 0.30, 0.05, 1.5, 1.5, 0.70,
        # Park & environment
        0.06, 7.0, 100.0,
        # Bullpen
        0.80, 1.5,
        # Lineup
        0.50,
        # Line movement
        0.03,
        # Season trend — typical last-20 vs. season-avg diff, ±0.15 → std ≈ 0.15
        0.15,
        # Player-level pitcher features
        1.2,    # bb9_diff (BB/9 typically 2-5 → diff std ~1)
        1.0,    # sp_split_era_diff
        1.5,    # sp_recent_form_diff (3-start ERA more volatile)
        # Composites
        1.0,    # pitcher_dominance_diff (z-score scale)
        0.10,   # lineup_vuln_diff (OPS diff typically ±0.10)
        0.20,   # blowout_prob (already in [0,1], centered ~0.5)
    ], dtype=np.float32),
    home_field_logit=0.10,
    min_training_games=30,
)


# ── WNBA ───────────────────────────────────────────────────────────────────────
# 17 features: team scoring, win%, splits, form, H2H, star player, B2B, pace,
# market, within-season trend (momentum), and college-performance adjustment.

WNBA_FEATURES = [
    "net_scoring_diff",      # (home PPG−PAPG) − (away PPG−PAPG)
    "ppg_diff",              # home PPG − away PPG
    "papg_diff",             # home PAPG − away PAPG
    "win_pct_diff",          # home win% − away win%
    "home_away_split_diff",  # home team's home win% − away team's away win%
    "last10_diff",           # last-10 win% differential (absolute)
    "h2h_diff",              # season H2H normalised diff
    "top_pts_diff",          # home top-player PPG − away top-player PPG
    "home_star_avail",       # 1.0 = home star available, 0.0 = out
    "away_star_avail",       # 1.0 = away star available
    "home_b2b",              # 1.0 = home playing back-to-back
    "away_b2b",              # 1.0 = away playing back-to-back
    "pace_diff",             # home pace − away pace
    "home_implied_prob",     # market vig-free P(home wins)
    "spread",                # spread point (negative = home favored), 0 if unknown
    "trend_diff",            # (home last10−season win%) − (away last10−season win%); captures momentum
    "college_adj_diff",      # home college-perf adj − away college-perf adj (rookies/2nd-yr only)
]

WNBA_TOTALS_FEATURES = [
    "combined_ppg",          # home PPG + away PPG
    "combined_papg",         # home PAPG + away PAPG
    "combined_pace",         # home pace + away pace
    "home_star_pts",         # home top-player PPG
    "away_star_pts",         # away top-player PPG
    "ref_foul_rate",         # referee fouls per game (default 40)
    "home_b2b",              # home back-to-back
    "away_b2b",              # away back-to-back
]

WNBA = SportConfig(
    name="WNBA",
    odds_key="basketball_wnba",
    api_sports_base="https://v1.basketball.api-sports.io",
    league_id=12,
    feature_names=WNBA_FEATURES,
    heuristic_weights=np.array([
        0.14, 0.09, 0.07,   # net_scoring_diff, ppg_diff, papg_diff
        0.14, 0.07, 0.09,   # win_pct_diff, home_away_split_diff, last10_diff
        0.05, 0.04,          # h2h_diff, top_pts_diff
        0.04, -0.04,         # home_star_avail, away_star_avail
        -0.04, 0.04,         # home_b2b, away_b2b
        0.02, 0.00, 0.00,   # pace_diff, home_implied_prob (mkt), spread (mkt)
        0.05, 0.03,          # trend_diff (momentum), college_adj_diff (young players)
    ], dtype=np.float32),
    heuristic_stds=np.array([
        8.0, 6.0, 6.0,
        0.25, 0.25, 0.25,
        0.50, 5.0,
        0.50, 0.50,
        0.50, 0.50,
        3.0, 0.10, 5.0,
        0.20, 2.0,           # trend_diff, college_adj_diff
    ], dtype=np.float32),
    home_field_logit=0.12,
    min_training_games=20,
)

SPORTS: dict[str, SportConfig] = {
    "mlb":  MLB,
    "wnba": WNBA,
}
