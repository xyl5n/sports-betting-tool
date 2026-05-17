"""
Per-sport configuration: API endpoints, feature names, and heuristic weights.
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


# ── NFL ────────────────────────────────────────────────────────────────────────
# Stats available from free plan via game results:
# PPG, PAPG, win%, home/away win%, last-5 form, scoring margin

NFL_FEATURES = [
    "net_scoring_diff",    # (home PPG − home PAPG) − (away PPG − away PAPG)
    "ppg_diff",            # home PPG − away PPG
    "papg_diff",           # home PAPG − away PAPG  (positive → home allows fewer)
    "win_pct_diff",        # home win% − away win%
    "home_away_split_diff",# home team's home win% − away team's away win%
    "last5_diff",          # last-5 win% differential
    "home_implied_prob",   # market vig-free P(home wins)
    "spread",              # point spread (negative = home favoured)
]

NFL = SportConfig(
    name="NFL",
    odds_key="americanfootball_nfl",
    api_sports_base="https://v1.american-football.api-sports.io",
    league_id=1,
    feature_names=NFL_FEATURES,
    heuristic_weights=np.array([
        0.22, 0.15, 0.13, 0.18, 0.10, 0.12, 0.00, 0.00,
    ], dtype=np.float32),
    heuristic_stds=np.array([
        10.0, 7.0, 7.0, 0.30, 0.30, 0.30, 0.10, 5.0,
    ], dtype=np.float32),
    home_field_logit=0.15,
    min_training_games=20,
)

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
]

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
    "nfl": NFL,
    "mlb": MLB,
    "wnba": WNBA,
}
