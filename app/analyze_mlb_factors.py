#!/usr/bin/env python3
"""
MLB Factor Importance Analysis
-------------------------------
Rebuilds historical game data from Retrosheet + pybaseball (2022-2024),
then measures which factors most reliably predict:

  1. Game winner (moneyline)
  2. Run-line coverage  (home covers -1.5)
  3. Total runs (over / under the game median)

Three measurement methods per target:
  A. XGBoost built-in feature importance (gain-based)
  B. SHAP mean absolute value across all games
  C. Pearson / point-biserial correlation with actual outcome

Results saved to:  mlb_factor_analysis.json
"""
from __future__ import annotations

import json
import sys
import warnings
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

warnings.filterwarnings("ignore")

# -- Path setup ----------------------------------------------------------------
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# -- Feature metadata ---------------------------------------------------------
# Aligned 1-for-1 with the 23-element MLB_FEATURES list in sports_config.py

FEATURE_NAMES: list[str] = [
    "net_run_diff",           # 0  (home RPG-RAPG) - (away RPG-RAPG)
    "rpg_diff",               # 1  home RPG - away RPG
    "rapg_diff",              # 2  home RAPG - away RAPG  (+  = home better D)
    "win_pct_diff",           # 3  home W% - away W%
    "home_away_split_diff",   # 4  home team H-W% - away team A-W%
    "last10_diff",            # 5  last-10 win% differential
    "hits_diff",              # 6  home H/G - away H/G  (pybaseball, season-level)
    "errors_diff",            # 7  NEUTRAL in training (=0)
    "home_implied_prob",      # 8  CONSTANT in training (=0.54)
    "run_line",               # 9  CONSTANT in training (=-1.5)
    "sp_era_diff",            # 10 NEUTRAL (=0)  - matters at inference
    "sp_whip_diff",           # 11 NEUTRAL (=0)
    "sp_k_rate_diff",         # 12 NEUTRAL (=0)
    "home_sp_rest",           # 13 CONSTANT in training (=4 days)
    "away_sp_rest",           # 14 CONSTANT in training (=4 days)
    "sp_hand_adv",            # 15 NEUTRAL (=0)
    "park_run_factor",        # 16 REAL - static per-stadium lookup
    "wind_speed",             # 17 NEUTRAL (=0)
    "wind_direction",         # 18 NEUTRAL (=0)
    "bullpen_era_diff",       # 19 REAL - from pybaseball season pitching ERA
    "bullpen_fatigue_diff",   # 20 NEUTRAL (=0)
    "lineup_confirmed",       # 21 NEUTRAL (=0)
    "line_movement",          # 22 NEUTRAL (=0)
]

# Which features have real historical variation vs. neutral constants
REAL_FEATURES = {0, 1, 2, 3, 4, 5, 6, 16, 19}
NEUTRAL_NOTE = (
    "Neutral/constant in training data - contributes at inference "
    "when real values are supplied but shows no historical variation here"
)

# -- Pretty display labels -----------------------------------------------------
FEATURE_LABELS: dict[str, str] = {
    "net_run_diff":          "Net Run Differential (PPG-PAPG diff)",
    "rpg_diff":              "Runs Per Game Differential",
    "rapg_diff":             "Runs Allowed Per Game Differential",
    "win_pct_diff":          "Win Percentage Differential",
    "home_away_split_diff":  "Home/Away Win% Split Differential",
    "last10_diff":           "Last-10 Form Differential",
    "hits_diff":             "Hits Per Game Differential",
    "errors_diff":           "Errors Per Game Differential",
    "home_implied_prob":     "Market Implied Home Win Probability",
    "run_line":              "Run Line Point Spread",
    "sp_era_diff":           "Starting Pitcher ERA Differential (A-H)",
    "sp_whip_diff":          "Starting Pitcher WHIP Differential (A-H)",
    "sp_k_rate_diff":        "Starting Pitcher K-Rate Differential (H-A)",
    "home_sp_rest":          "Home Starting Pitcher Days of Rest",
    "away_sp_rest":          "Away Starting Pitcher Days of Rest",
    "sp_hand_adv":           "Pitcher Handedness Advantage",
    "park_run_factor":       "Ballpark Run Factor",
    "wind_speed":            "Wind Speed (mph)",
    "wind_direction":        "Wind Direction (degrees)",
    "bullpen_era_diff":      "Bullpen ERA Differential (A-H, from pybaseball)",
    "bullpen_fatigue_diff":  "Bullpen Fatigue Differential",
    "lineup_confirmed":      "Starting Lineup Confirmed",
    "line_movement":         "Line Movement (closing - opening implied prob)",
}

N_FEATURES = len(FEATURE_NAMES)
assert N_FEATURES == 23

# -- Rolling-stats tracker (replicated from historical_data.py) ---------------

class _TeamSeason:
    def __init__(self) -> None:
        self.g = self.w = 0
        self.hg = self.hw = 0
        self.ag = self.aw = 0
        self.rs = self.ra = 0
        self.last10: deque = deque(maxlen=10)

    def stats(self) -> dict:
        def p(n, d): return n / d if d > 0 else 0.5
        return {
            "games":        self.g,
            "win_pct":      p(self.w, self.g),
            "home_win_pct": p(self.hw, self.hg),
            "away_win_pct": p(self.aw, self.ag),
            "rpg":          self.rs / self.g if self.g > 0 else 4.5,
            "rapg":         self.ra / self.g if self.g > 0 else 4.5,
            "last10":       sum(self.last10) / len(self.last10) if self.last10 else 0.5,
        }

    def update(self, runs_scored, runs_allowed, *, is_home: bool) -> None:
        won = runs_scored > runs_allowed
        self.g += 1; self.rs += runs_scored; self.ra += runs_allowed
        self.last10.append(1 if won else 0)
        if won: self.w += 1
        if is_home:
            self.hg += 1
            if won: self.hw += 1
        else:
            self.ag += 1
            if won: self.aw += 1


# -- Dataset builder -----------------------------------------------------------

def build_dataset(seasons=(2022, 2023, 2024), min_games=10):
    """
    Rebuild the historical dataset from Retrosheet + pybaseball.

    Returns:
        X          : (n, 23) float32  - feature matrix
        y_ml       : (n,)   int32     - 1 = home wins
        y_rl       : (n,)   int32     - 1 = home covers -1.5 (wins by 2+)
        run_totals : (n,)   float32   - actual combined runs scored
        metadata   : list of {date, home, away, home_runs, away_runs}
    """
    from src.retrosheet_client import get_season_gamelogs, RETRO_TO_FG
    from src.park_factors import get_park_factors

    # -- Load pybaseball stats ------------------------------------------------
    batting_lu: dict = {}
    pitching_lu: dict = {}
    FG_TO_RETRO = {v: k for k, v in RETRO_TO_FG.items()}

    try:
        import pybaseball
        print("  Loading pybaseball batting stats...", flush=True)
        try:
            tb = pybaseball.team_batting(min(seasons), max(seasons))
            for _, row in tb.iterrows():
                fg = str(row.get("teamIDfg") or "")
                rc = FG_TO_RETRO.get(fg)
                if not rc: continue
                season = int(row.get("Season", 0))
                g = float(row.get("G", 0) or 0) or 162.0
                h = float(row.get("H", 0) or 0)
                batting_lu[(rc, season)] = {"hpg": h / g}
        except Exception as e:
            print(f"    pybaseball batting error: {e}")

        print("  Loading pybaseball pitching stats...", flush=True)
        try:
            tp = pybaseball.team_pitching(min(seasons), max(seasons))
            for _, row in tp.iterrows():
                fg = str(row.get("teamIDfg") or "")
                rc = FG_TO_RETRO.get(fg)
                if not rc: continue
                season = int(row.get("Season", 0))
                era = float(row.get("ERA", 4.20) or 4.20)
                pitching_lu[(rc, season)] = {"era": era}
        except Exception as e:
            print(f"    pybaseball pitching error: {e}")

        print(f"  pybaseball: {len(batting_lu)} batting, {len(pitching_lu)} pitching rows")
    except ImportError:
        print("  pybaseball not installed - hits_diff and bullpen_era_diff will use neutral values")

    # -- Build game rows -------------------------------------------------------
    X_rows, y_ml_rows, y_rl_rows, totals_rows, meta_rows = [], [], [], [], []
    total_skipped = 0

    for season in seasons:
        games = get_season_gamelogs(season)
        if not games:
            print(f"  Retrosheet {season}: download failed - skipping")
            continue

        print(f"  Retrosheet {season}: {len(games)} raw game records", flush=True)
        games.sort(key=lambda g: g["date"])

        trackers: dict[str, _TeamSeason] = {}
        season_rows = 0

        for game in games:
            hc = game["home_code"]
            ac = game["away_code"]
            if hc not in trackers: trackers[hc] = _TeamSeason()
            if ac not in trackers: trackers[ac] = _TeamSeason()

            ht = trackers[hc]; at = trackers[ac]
            hs_pre  = ht.stats()
            as_pre  = at.stats()
            home_r  = game["home_runs"]
            away_r  = game["away_runs"]

            ht.update(home_r, away_r, is_home=True)
            at.update(away_r, home_r, is_home=False)

            if hs_pre["games"] < min_games or as_pre["games"] < min_games:
                total_skipped += 1
                continue

            h_bat  = batting_lu.get((hc, season), {})
            a_bat  = batting_lu.get((ac, season), {})
            h_era  = pitching_lu.get((hc, season), {}).get("era", 4.20)
            a_era  = pitching_lu.get((ac, season), {}).get("era", 4.20)
            park_r, _ = get_park_factors(game["home_name"])

            h_rpg  = hs_pre["rpg"];  a_rpg  = as_pre["rpg"]
            h_rapg = hs_pre["rapg"]; a_rapg = as_pre["rapg"]
            h_hpg  = h_bat.get("hpg", 8.5)
            a_hpg  = a_bat.get("hpg", 8.5)

            vec = np.array([
                (h_rpg - h_rapg) - (a_rpg - a_rapg),   # net_run_diff
                h_rpg  - a_rpg,                          # rpg_diff
                h_rapg - a_rapg,                         # rapg_diff
                hs_pre["win_pct"]      - as_pre["win_pct"],      # win_pct_diff
                hs_pre["home_win_pct"] - as_pre["away_win_pct"], # home_away_split_diff
                hs_pre["last10"]       - as_pre["last10"],        # last10_diff
                h_hpg - a_hpg,          # hits_diff
                0.0,                    # errors_diff (neutral)
                0.54,                   # home_implied_prob (neutral)
                -1.5,                   # run_line (neutral)
                0.0,                    # sp_era_diff (neutral)
                0.0,                    # sp_whip_diff (neutral)
                0.0,                    # sp_k_rate_diff (neutral)
                4.0,                    # home_sp_rest (neutral)
                4.0,                    # away_sp_rest (neutral)
                0.0,                    # sp_hand_adv (neutral)
                park_r,                 # park_run_factor (real)
                0.0,                    # wind_speed (neutral)
                0.0,                    # wind_direction (neutral)
                a_era - h_era,          # bullpen_era_diff (real proxy)
                0.0,                    # bullpen_fatigue_diff (neutral)
                0.0,                    # lineup_confirmed (neutral)
                0.0,                    # line_movement (neutral)
            ], dtype=np.float32)

            X_rows.append(vec)
            y_ml_rows.append(1 if home_r > away_r else 0)
            y_rl_rows.append(1 if (home_r - away_r) >= 2 else 0)   # covers -1.5
            totals_rows.append(float(home_r + away_r))
            meta_rows.append({
                "date":       game["date"],
                "home":       game["home_name"],
                "away":       game["away_name"],
                "home_runs":  home_r,
                "away_runs":  away_r,
            })
            season_rows += 1

        print(f"  Retrosheet {season}: {season_rows} usable training rows")

    X = np.vstack(X_rows).astype(np.float32)
    y_ml = np.array(y_ml_rows, dtype=np.int32)
    y_rl = np.array(y_rl_rows, dtype=np.int32)
    totals = np.array(totals_rows, dtype=np.float32)

    print(f"\n  Dataset: {len(y_ml)} games, {total_skipped} skipped (early-season)")
    return X, y_ml, y_rl, totals, meta_rows


# -- Analysis helpers ----------------------------------------------------------

def xgb_importance(X: np.ndarray, y: np.ndarray,
                   task: str = "classification") -> np.ndarray:
    """Train an XGBoost model and return gain-based feature importances."""
    import xgboost as xgb
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    if task == "regression":
        model = xgb.XGBRegressor(
            n_estimators=400, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
            reg_lambda=2.0, random_state=42, verbosity=0,
        )
    else:
        model = xgb.XGBClassifier(
            n_estimators=400, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
            reg_lambda=2.0, random_state=42, verbosity=0,
            use_label_encoder=False, eval_metric="logloss",
        )

    model.fit(Xs, y)
    importances = model.feature_importances_
    return importances, model, scaler


def shap_importance(model, scaler, X: np.ndarray) -> np.ndarray:
    """Compute SHAP mean absolute values per feature."""
    import shap
    Xs = scaler.transform(X)
    explainer = shap.TreeExplainer(model)
    shap_vals = explainer.shap_values(Xs)
    # For classifiers shap_values may be a list [class0, class1]
    if isinstance(shap_vals, list):
        shap_vals = shap_vals[1]  # positive class
    return np.abs(shap_vals).mean(axis=0)


def pearson_importance(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """
    Pearson / point-biserial |correlation| between each feature and target.
    Constant features (std < threshold) get 0.0 to avoid NaN.
    """
    from scipy.stats import pearsonr
    y_f = y.astype(np.float64)
    corrs = np.zeros(X.shape[1])
    for i in range(X.shape[1]):
        col = X[:, i].astype(np.float64)
        if col.std() < 1e-6:
            corrs[i] = 0.0
            continue
        try:
            r, _ = pearsonr(col, y_f)
            corrs[i] = 0.0 if np.isnan(r) else abs(r)
        except Exception:
            corrs[i] = 0.0
    return corrs


def normalize(arr: np.ndarray) -> np.ndarray:
    """
    Min-max normalize to [0, 1].
    NaN values are treated as 0 before normalization.
    """
    a = np.nan_to_num(arr, nan=0.0)
    rng = a.max() - a.min()
    if rng < 1e-10:
        return np.zeros_like(a, dtype=np.float64)
    return (a - a.min()) / rng


def rank_features(
    feat_names: list[str],
    xgb_imp: np.ndarray,
    shap_imp: np.ndarray,
    pearson_imp: np.ndarray,
    top_n: int = 20,
) -> list[dict]:
    """
    Combine three importance vectors into a single ranked list.
    Each method is normalized to [0,1] independently, then averaged.
    Features with NaN or zero in all three methods rank last.
    """
    xgb_n  = normalize(np.nan_to_num(xgb_imp,     nan=0.0))
    shap_n = normalize(np.nan_to_num(shap_imp,    nan=0.0))
    pear_n = normalize(np.nan_to_num(pearson_imp, nan=0.0))
    combined = (xgb_n + shap_n + pear_n) / 3.0

    # Stable sort: highest combined first; ties broken by index
    order = np.argsort(-combined, kind="stable")
    rows = []
    for rank, idx in enumerate(order[:top_n], 1):
        name = feat_names[idx]
        has_real_data = idx in REAL_FEATURES
        rows.append({
            "rank":            rank,
            "feature":         name,
            "label":           FEATURE_LABELS[name],
            "xgb_importance":  round(float(xgb_imp[idx]), 6),
            "shap_mean_abs":   round(float(shap_imp[idx]), 6),
            "pearson_corr":    round(float(pearson_imp[idx]), 6),
            "xgb_normalized":  round(float(xgb_n[idx]), 4),
            "shap_normalized": round(float(shap_n[idx]), 4),
            "pearson_normalized": round(float(pear_n[idx]), 4),
            "combined_score":  round(float(combined[idx]), 4),
            "has_historical_data": has_real_data,
            "note": "" if has_real_data else NEUTRAL_NOTE,
        })
    return rows


def print_table(title: str, rows: list[dict], total_games: int) -> None:
    """Pretty-print a ranked importance table to the terminal."""
    sep  = "-" * 100
    sep2 = "=" * 100
    print(f"\n{sep2}")
    print(f"  {title}")
    print(f"  Based on {total_games:,} historical MLB games (Retrosheet 2022-2024)")
    print(sep2)
    print(f"  {'Rank':<5} {'Feature':<28} {'XGB':>8} {'SHAP':>8} {'Pearson':>8} {'Combined':>9}  {'Data':<8}")
    print(sep)
    for r in rows:
        data_flag = "[OK] real" if r["has_historical_data"] else "~ neutral"
        print(
            f"  {r['rank']:<5} {r['feature']:<28} "
            f"{r['xgb_importance']:>8.4f} "
            f"{r['shap_mean_abs']:>8.4f} "
            f"{r['pearson_corr']:>8.4f} "
            f"{r['combined_score']:>9.4f}  "
            f"{data_flag:<10}"
        )
    print(sep)
    print()


# -- Main ----------------------------------------------------------------------

def main():
    print("=" * 100)
    print("  MLB FACTOR IMPORTANCE ANALYSIS")
    print("  Three methods  Three targets  23 features")
    print("  Data: Retrosheet game logs 2022-2024 + pybaseball season stats")
    print("=" * 100)

    # -- 1. Build dataset -----------------------------------------------------
    print("\n[ STEP 1 ] Building historical dataset...\n")
    X, y_ml, y_rl, run_totals, meta = build_dataset()
    n = len(y_ml)

    home_win_rate  = y_ml.mean()
    rl_cover_rate  = y_rl.mean()
    median_total   = float(np.median(run_totals))
    y_ou = (run_totals > median_total).astype(np.int32)  # over/under median

    print(f"\n  Dataset summary:")
    print(f"    Total games:      {n:,}")
    print(f"    Home win rate:    {home_win_rate:.1%}")
    print(f"    RL cover rate:    {rl_cover_rate:.1%}  (home covers -1.5)")
    print(f"    Median total:     {median_total:.1f} runs")
    print(f"    Over rate:        {y_ou.mean():.1%}  (above {median_total:.1f})")

    # -- 2. Check shap availability -------------------------------------------
    shap_available = True
    try:
        import shap  # noqa: F401
        print("\n  SHAP library: available [OK]")
    except ImportError:
        shap_available = False
        print("\n  SHAP library: not installed - using XGB importances doubled in its place")

    # -- 3. Moneyline (winner prediction) ------------------------------------
    print("\n[ STEP 2 ] Moneyline / Winner prediction analysis...", flush=True)
    xgb_ml, model_ml, scaler_ml = xgb_importance(X, y_ml, "classification")
    print("  XGBoost trained [OK]", flush=True)

    if shap_available:
        shap_ml = shap_importance(model_ml, scaler_ml, X)
        print("  SHAP computed [OK]", flush=True)
    else:
        shap_ml = xgb_ml.copy()

    pearson_ml = pearson_importance(X, y_ml)
    print("  Pearson correlations computed [OK]", flush=True)

    ml_ranked = rank_features(FEATURE_NAMES, xgb_ml, shap_ml, pearson_ml)

    # -- 4. Run line (-1.5 spread coverage) ----------------------------------
    print("\n[ STEP 3 ] Run line (-1.5 spread) coverage analysis...", flush=True)
    xgb_rl, model_rl, scaler_rl = xgb_importance(X, y_rl, "classification")
    print("  XGBoost trained [OK]", flush=True)

    if shap_available:
        shap_rl = shap_importance(model_rl, scaler_rl, X)
        print("  SHAP computed [OK]", flush=True)
    else:
        shap_rl = xgb_rl.copy()

    pearson_rl = pearson_importance(X, y_rl)
    print("  Pearson correlations computed [OK]", flush=True)

    rl_ranked = rank_features(FEATURE_NAMES, xgb_rl, shap_rl, pearson_rl)

    # -- 5. Totals (over / under median combined runs) ------------------------
    print(f"\n[ STEP 4 ] Totals (O/U {median_total:.1f} runs) analysis...", flush=True)
    xgb_tot, model_tot, scaler_tot = xgb_importance(X, run_totals, "regression")
    print("  XGBoost regressor trained [OK]", flush=True)

    if shap_available:
        shap_tot = shap_importance(model_tot, scaler_tot, X)
        print("  SHAP computed [OK]", flush=True)
    else:
        shap_tot = xgb_tot.copy()

    # Pearson: correlation between each feature and actual run total
    pearson_tot = pearson_importance(X, run_totals)
    print("  Pearson correlations computed [OK]", flush=True)

    tot_ranked = rank_features(FEATURE_NAMES, xgb_tot, shap_tot, pearson_tot)

    # -- 6. Terminal display --------------------------------------------------
    print("\n\n" + "=" * 100)
    print("  RESULTS")
    print("=" * 100)

    print_table(
        "TOP 20 FACTORS - WINNER PREDICTION (Moneyline)",
        ml_ranked, n,
    )
    print_table(
        "TOP 20 FACTORS - RUN LINE COVERAGE (Home Covers -1.5)",
        rl_ranked, n,
    )
    print_table(
        f"TOP 20 FACTORS - TOTALS (Over/Under {median_total:.1f} Combined Runs)",
        tot_ranked, n,
    )

    # -- 7. Data quality legend -----------------------------------------------
    print("  DATA QUALITY NOTE")
    print("  -" * 50)
    print("  [OK] real     - Feature has genuine historical variation in the training data")
    print("  ~ neutral  - Feature was set to a league-average baseline in training.")
    print("               These show low importance here but DO contribute at live")
    print("               inference time when real pitcher / weather / market data")
    print("               is supplied. See heuristic_weights in sports_config.py for")
    print("               their expected live-game importance.")
    print()

    # -- 8. Heuristic weights cross-reference ---------------------------------
    from src.sports_config import MLB
    heur_w = MLB.heuristic_weights
    heur_stds = MLB.heuristic_stds

    print("  HEURISTIC INFERENCE WEIGHTS (from sports_config.py - all 23 features)")
    print("  These are the manually calibrated weights used at inference time when")
    print("  real pitcher / weather / market data is available:")
    print()
    heur_rows = sorted(
        zip(FEATURE_NAMES, heur_w, heur_stds),
        key=lambda x: abs(x[1]),
        reverse=True,
    )
    for name, w, std in heur_rows:
        bar = "#" * max(0, int(abs(w) * 100))
        print(f"    {name:<28} weight={w:+.3f}  std={std:.3f}  {bar}")
    print()

    # -- 9. Save JSON ---------------------------------------------------------
    print("[ STEP 5 ] Saving results to mlb_factor_analysis.json...")

    # Build full 23-feature importance table for each target (not just top 20)
    def full_table(xgb_imp, shap_imp, pear_imp):
        return rank_features(FEATURE_NAMES, xgb_imp, shap_imp, pear_imp, top_n=23)

    output = {
        "meta": {
            "generated_at":         __import__("datetime").datetime.now().isoformat(),
            "total_games":          int(n),
            "seasons":              [2022, 2023, 2024],
            "home_win_rate":        round(float(home_win_rate), 4),
            "run_line_cover_rate":  round(float(rl_cover_rate), 4),
            "median_total_runs":    round(float(median_total), 1),
            "totals_over_rate":     round(float(y_ou.mean()), 4),
            "shap_used":            shap_available,
            "data_quality_note": (
                "Features marked has_historical_data=false were set to neutral "
                "baselines in training (pitcher stats, weather, market odds). "
                "Their importance scores reflect no variation in training data. "
                "At live inference time, real values for these features are used "
                "and they contribute meaningfully to predictions."
            ),
        },
        "methods": {
            "xgb_importance":  "XGBoost gain-based feature_importances_ from tree splits",
            "shap_mean_abs":   "SHAP TreeExplainer mean(|SHAP values|) across all games",
            "pearson_corr":    (
                "Pearson / point-biserial |correlation| between feature and outcome. "
                "For totals: correlation with actual combined runs scored."
            ),
            "combined_score":  "Mean of three normalized (0-1) importance scores",
        },
        "winner_prediction": {
            "target":      "1 = home team wins, 0 = away team wins",
            "top_20":      ml_ranked,
            "all_23":      full_table(xgb_ml, shap_ml, pearson_ml),
        },
        "run_line_coverage": {
            "target":      "1 = home covers -1.5 (wins by 2+ runs), 0 = does not cover",
            "cover_rate":  round(float(rl_cover_rate), 4),
            "top_20":      rl_ranked,
            "all_23":      full_table(xgb_rl, shap_rl, pearson_rl),
        },
        "totals_over_under": {
            "target":      f"XGB: regression on actual combined runs; Pearson/SHAP: over/under {median_total:.1f}",
            "median_line": round(float(median_total), 1),
            "top_20":      tot_ranked,
            "all_23":      full_table(xgb_tot, shap_tot, pearson_tot),
        },
        "heuristic_inference_weights": [
            {
                "feature":          name,
                "label":            FEATURE_LABELS[name],
                "heuristic_weight": round(float(w), 4),
                "heuristic_std":    round(float(std), 4),
                "has_historical_data": (i in REAL_FEATURES),
            }
            for i, (name, w, std) in enumerate(zip(FEATURE_NAMES, heur_w, heur_stds))
        ],
    }

    out_path = ROOT / "mlb_factor_analysis.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"  Saved  {out_path}")
    print()
    print("=" * 100)
    print("  ANALYSIS COMPLETE")
    print(f"  Winner prediction  - top feature: {ml_ranked[0]['feature']}")
    print(f"  Run line coverage  - top feature: {rl_ranked[0]['feature']}")
    print(f"  Totals over/under  - top feature: {tot_ranked[0]['feature']}")
    print("=" * 100)


if __name__ == "__main__":
    main()
