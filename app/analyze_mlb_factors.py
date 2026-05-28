#!/usr/bin/env python3
"""
MLB Factor Importance Analysis - V2 (Enriched Dataset)
-------------------------------------------------------
Identical analysis to analyze_mlb_factors.py but uses the ENRICHED historical
dataset from src/enriched_historical_data.py.

The enriched dataset attaches real starting pitcher stats (ERA, WHIP, K-rate,
rest days, handedness) from the MLB Stats API to every Retrosheet game, so
features 10-15 now have genuine variation in the training data instead of
the neutral baselines used in v1.

Expected v2 improvement: sp_era_diff, sp_whip_diff, sp_k_rate_diff,
home_sp_rest, away_sp_rest, and sp_hand_adv should now show non-zero
combined importance scores.

Results saved to: mlb_factor_analysis_v2.json
"""
from __future__ import annotations

import logging
import json
import sys
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Feature metadata
# ---------------------------------------------------------------------------

FEATURE_NAMES: list[str] = [
    "net_run_diff",           # 0
    "rpg_diff",               # 1
    "rapg_diff",              # 2
    "win_pct_diff",           # 3
    "home_away_split_diff",   # 4
    "last10_diff",            # 5
    "hits_diff",              # 6
    "errors_diff",            # 7  still neutral (no historical source)
    "home_implied_prob",      # 8  still neutral (constant 0.54)
    "run_line",               # 9  still neutral (constant -1.5)
    "sp_era_diff",            # 10 ENRICHED — real MLB Stats API values
    "sp_whip_diff",           # 11 ENRICHED
    "sp_k_rate_diff",         # 12 ENRICHED
    "home_sp_rest",           # 13 ENRICHED — actual rest days
    "away_sp_rest",           # 14 ENRICHED
    "sp_hand_adv",            # 15 ENRICHED — LHP/RHP handedness advantage
    "park_run_factor",        # 16 REAL — static per-stadium lookup
    "wind_speed",             # 17 still neutral
    "wind_direction",         # 18 still neutral
    "bullpen_era_diff",       # 19 REAL — pybaseball season pitching ERA
    "bullpen_fatigue_diff",   # 20 still neutral
    "lineup_confirmed",       # 21 still neutral
    "line_movement",          # 22 still neutral
]

# In v2: indices 10-15 are enriched (real SP data) in addition to 0-6, 16, 19
REAL_FEATURES = {0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 16, 19}
NEUTRAL_NOTE = (
    "Neutral/constant in training data even in enriched dataset — "
    "contributes at inference when real values are supplied"
)

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
    "sp_era_diff":           "Starting Pitcher ERA Differential (A-H)  [ENRICHED]",
    "sp_whip_diff":          "Starting Pitcher WHIP Differential (A-H)  [ENRICHED]",
    "sp_k_rate_diff":        "Starting Pitcher K-Rate Differential (H-A)  [ENRICHED]",
    "home_sp_rest":          "Home Starting Pitcher Days of Rest  [ENRICHED]",
    "away_sp_rest":          "Away Starting Pitcher Days of Rest  [ENRICHED]",
    "sp_hand_adv":           "Pitcher Handedness Advantage  [ENRICHED]",
    "park_run_factor":       "Ballpark Run Factor",
    "wind_speed":            "Wind Speed (mph)",
    "wind_direction":        "Wind Direction (degrees)",
    "bullpen_era_diff":      "Bullpen ERA Differential (A-H, pybaseball)",
    "bullpen_fatigue_diff":  "Bullpen Fatigue Differential",
    "lineup_confirmed":      "Starting Lineup Confirmed",
    "line_movement":         "Line Movement (closing - opening implied prob)",
}

N_FEATURES = len(FEATURE_NAMES)
assert N_FEATURES == 23


# ---------------------------------------------------------------------------
# Analysis helpers (identical to v1)
# ---------------------------------------------------------------------------

def xgb_importance(X: np.ndarray, y: np.ndarray, task: str = "classification"):
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
    return model.feature_importances_, model, scaler


def shap_importance(model, scaler, X: np.ndarray) -> np.ndarray:
    import shap
    Xs = scaler.transform(X)
    explainer = shap.TreeExplainer(model)
    shap_vals = explainer.shap_values(Xs)
    if isinstance(shap_vals, list):
        shap_vals = shap_vals[1]
    return np.abs(shap_vals).mean(axis=0)


def pearson_importance(X: np.ndarray, y: np.ndarray) -> np.ndarray:
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
    xgb_n  = normalize(np.nan_to_num(xgb_imp,     nan=0.0))
    shap_n = normalize(np.nan_to_num(shap_imp,    nan=0.0))
    pear_n = normalize(np.nan_to_num(pearson_imp, nan=0.0))
    combined = (xgb_n + shap_n + pear_n) / 3.0

    order = np.argsort(-combined, kind="stable")
    rows = []
    for rank, idx in enumerate(order[:top_n], 1):
        name = feat_names[idx]
        has_real = idx in REAL_FEATURES
        rows.append({
            "rank":               rank,
            "feature":            name,
            "label":              FEATURE_LABELS[name],
            "xgb_importance":     round(float(xgb_imp[idx]), 6),
            "shap_mean_abs":      round(float(shap_imp[idx]), 6),
            "pearson_corr":       round(float(pearson_imp[idx]), 6),
            "xgb_normalized":     round(float(xgb_n[idx]), 4),
            "shap_normalized":    round(float(shap_n[idx]), 4),
            "pearson_normalized": round(float(pear_n[idx]), 4),
            "combined_score":     round(float(combined[idx]), 4),
            "has_historical_data": has_real,
            "note": "" if has_real else NEUTRAL_NOTE,
        })
    return rows


def print_table(title: str, rows: list[dict], total_games: int,
                sp_enriched: int) -> None:
    sep  = "-" * 104
    sep2 = "=" * 104
    print(f"\n{sep2}")
    print(f"  {title}")
    print(f"  {total_games:,} games | {sp_enriched:,} with real SP data "
          f"| Retrosheet 2022-2024 + MLB Stats API")
    print(sep2)
    print(f"  {'Rank':<5} {'Feature':<28} {'XGB':>8} {'SHAP':>8} "
          f"{'Pearson':>8} {'Combined':>9}  {'Data':<12}")
    print(sep)
    for r in rows:
        flag = "[OK] real" if r["has_historical_data"] else "~ neutral"
        print(
            f"  {r['rank']:<5} {r['feature']:<28} "
            f"{r['xgb_importance']:>8.4f} "
            f"{r['shap_mean_abs']:>8.4f} "
            f"{r['pearson_corr']:>8.4f} "
            f"{r['combined_score']:>9.4f}  "
            f"{flag:<12}"
        )
    print(sep)
    print()


# ---------------------------------------------------------------------------
# Comparison helper: load v1 results if present
# ---------------------------------------------------------------------------

def _load_v1_scores() -> dict[str, float]:
    """Return {feature: combined_score} from v1 analysis if available."""
    p = ROOT / "mlb_factor_analysis.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            r["feature"]: r["combined_score"]
            for r in data.get("winner_prediction", {}).get("all_23", [])
        }
    except Exception:
        return {}


def print_comparison(
    title: str,
    v2_rows: list[dict],
    v1_scores: dict[str, float],
) -> None:
    if not v1_scores:
        return
    print(f"\n  IMPROVEMENT vs v1: {title}")
    print(f"  {'Feature':<28} {'v1 score':>10} {'v2 score':>10} {'delta':>8}")
    print("  " + "-" * 62)
    for r in v2_rows[:15]:
        feat = r["feature"]
        v1 = v1_scores.get(feat, 0.0)
        v2 = r["combined_score"]
        delta = v2 - v1
        marker = "  +++" if delta > 0.05 else ("  ---" if delta < -0.05 else "")
        print(f"  {feat:<28} {v1:>10.4f} {v2:>10.4f} {delta:>+8.4f}{marker}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 104)
    print("  MLB FACTOR IMPORTANCE ANALYSIS  --  V2 (ENRICHED DATASET)")
    print("  Three methods  |  Three targets  |  23 features")
    print("  Data: Retrosheet 2022-2024 + MLB Stats API real pitcher stats")
    print("=" * 104)

    # -- Step 1: load enriched dataset -----------------------------------------
    print("\n[ STEP 1 ] Loading enriched historical dataset…\n")
    from src.enriched_historical_data import build_enriched_dataset

    X, y_ml, y_rl, run_totals = build_enriched_dataset()
    n = len(y_ml)

    # Retrieve sp_enriched count from cache metadata
    import joblib
    from pathlib import Path as _P
    _cache = _P(".cache/enriched_mlb_dataset.joblib")
    sp_enriched = 0
    if _cache.exists():
        try:
            sp_enriched = joblib.load(_cache).get("sp_enriched", 0)
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    home_win_rate = y_ml.mean()
    rl_cover_rate = y_rl.mean()
    median_total  = float(np.median(run_totals))
    y_ou          = (run_totals > median_total).astype(np.int32)

    print(f"  Dataset summary:")
    print(f"    Total games:      {n:,}")
    print(f"    SP data games:    {sp_enriched:,}  (with real pitcher ERA/WHIP/K-rate)")
    print(f"    Home win rate:    {home_win_rate:.1%}")
    print(f"    RL cover rate:    {rl_cover_rate:.1%}  (home covers -1.5)")
    print(f"    Median total:     {median_total:.1f} runs")
    print(f"    Over rate:        {y_ou.mean():.1%}  (above {median_total:.1f})")

    # -- Step 2: SHAP check ----------------------------------------------------
    shap_available = True
    try:
        import shap  # noqa: F401
        print("\n  SHAP library: available [OK]")
    except ImportError:
        shap_available = False
        print("\n  SHAP library: not installed — doubling XGB importance in its place")

    # Load v1 scores for comparison
    v1_scores = _load_v1_scores()
    if v1_scores:
        print(f"  v1 comparison data loaded [OK] ({len(v1_scores)} features)")
    else:
        print("  v1 comparison: mlb_factor_analysis.json not found — skipping delta")

    # -- Step 3: Moneyline analysis --------------------------------------------
    print("\n[ STEP 2 ] Moneyline / Winner prediction…", flush=True)
    xgb_ml, model_ml, scaler_ml = xgb_importance(X, y_ml, "classification")
    print("  XGBoost trained [OK]", flush=True)
    shap_ml = shap_importance(model_ml, scaler_ml, X) if shap_available else xgb_ml.copy()
    if shap_available:
        print("  SHAP computed [OK]", flush=True)
    pearson_ml = pearson_importance(X, y_ml)
    print("  Pearson computed [OK]", flush=True)
    ml_ranked = rank_features(FEATURE_NAMES, xgb_ml, shap_ml, pearson_ml)

    # -- Step 4: Run-line analysis ---------------------------------------------
    print("\n[ STEP 3 ] Run line (-1.5) coverage…", flush=True)
    xgb_rl, model_rl, scaler_rl = xgb_importance(X, y_rl, "classification")
    print("  XGBoost trained [OK]", flush=True)
    shap_rl = shap_importance(model_rl, scaler_rl, X) if shap_available else xgb_rl.copy()
    if shap_available:
        print("  SHAP computed [OK]", flush=True)
    pearson_rl = pearson_importance(X, y_rl)
    print("  Pearson computed [OK]", flush=True)
    rl_ranked = rank_features(FEATURE_NAMES, xgb_rl, shap_rl, pearson_rl)

    # -- Step 5: Totals analysis -----------------------------------------------
    print(f"\n[ STEP 4 ] Totals (O/U {median_total:.1f} runs)…", flush=True)
    xgb_tot, model_tot, scaler_tot = xgb_importance(X, run_totals, "regression")
    print("  XGBoost trained [OK]", flush=True)
    shap_tot = shap_importance(model_tot, scaler_tot, X) if shap_available else xgb_tot.copy()
    if shap_available:
        print("  SHAP computed [OK]", flush=True)
    pearson_tot = pearson_importance(X, run_totals)
    print("  Pearson computed [OK]", flush=True)
    tot_ranked = rank_features(FEATURE_NAMES, xgb_tot, shap_tot, pearson_tot)

    # -- Step 6: Display results -----------------------------------------------
    print("\n\n" + "=" * 104)
    print("  RESULTS  (V2 - Enriched Dataset)")
    print("=" * 104)

    print_table("TOP 20 FACTORS - WINNER PREDICTION (Moneyline)",
                ml_ranked, n, sp_enriched)
    print_table("TOP 20 FACTORS - RUN LINE COVERAGE (Home Covers -1.5)",
                rl_ranked, n, sp_enriched)
    print_table(f"TOP 20 FACTORS - TOTALS (Over/Under {median_total:.1f} runs)",
                tot_ranked, n, sp_enriched)

    # -- Step 7: v1 vs v2 comparison -------------------------------------------
    if v1_scores:
        print("=" * 104)
        print("  V1 vs V2 COMPARISON  (winner prediction; + = improved in v2)")
        print("=" * 104)
        print_comparison("Winner prediction", ml_ranked, v1_scores)

    # -- Step 8: Heuristic weights ---------------------------------------------
    from src.sports_config import MLB
    heur_w = MLB.heuristic_weights
    heur_stds = MLB.heuristic_stds
    print("  UPDATED HEURISTIC INFERENCE WEIGHTS (sports_config.py)")
    print()
    heur_rows = sorted(
        [(n, w, s) for n, w, s in zip(FEATURE_NAMES, heur_w, heur_stds)],
        key=lambda x: abs(x[1]),
        reverse=True,
    )
    for name, w, std in heur_rows:
        bar = "#" * max(0, int(abs(w) * 100))
        print(f"    {name:<28} weight={w:+.3f}  std={std:.3f}  {bar}")
    print()

    # -- Step 9: Save JSON -----------------------------------------------------
    print("[ STEP 5 ] Saving to mlb_factor_analysis_v2.json…")

    def full_table(xi, si, pi):
        return rank_features(FEATURE_NAMES, xi, si, pi, top_n=23)

    output = {
        "meta": {
            "version":              "v2_enriched",
            "generated_at":         __import__("datetime").datetime.now().isoformat(),
            "total_games":          int(n),
            "sp_enriched_games":    int(sp_enriched),
            "seasons":              [2022, 2023, 2024],
            "home_win_rate":        round(float(home_win_rate), 4),
            "run_line_cover_rate":  round(float(rl_cover_rate), 4),
            "median_total_runs":    round(float(median_total), 1),
            "totals_over_rate":     round(float(y_ou.mean()), 4),
            "shap_used":            shap_available,
            "vs_v1": (
                "v2 uses real pitcher stats (ERA/WHIP/K-rate/rest/handedness) "
                "for indices 10-15 via MLB Stats API, vs neutral baselines in v1. "
                "sp_era_diff, sp_whip_diff, sp_k_rate_diff, home_sp_rest, "
                "away_sp_rest, sp_hand_adv should now show non-zero importance."
            ),
        },
        "winner_prediction": {
            "target":  "1 = home team wins, 0 = away wins",
            "top_20":  ml_ranked,
            "all_23":  full_table(xgb_ml, shap_ml, pearson_ml),
        },
        "run_line_coverage": {
            "target":     "1 = home covers -1.5, 0 = does not",
            "cover_rate": round(float(rl_cover_rate), 4),
            "top_20":     rl_ranked,
            "all_23":     full_table(xgb_rl, shap_rl, pearson_rl),
        },
        "totals_over_under": {
            "target":      f"regression on actual combined runs; O/U {median_total:.1f}",
            "median_line": round(float(median_total), 1),
            "top_20":      tot_ranked,
            "all_23":      full_table(xgb_tot, shap_tot, pearson_tot),
        },
        "heuristic_inference_weights": [
            {
                "feature":             name,
                "label":               FEATURE_LABELS[name],
                "heuristic_weight":    round(float(w), 4),
                "heuristic_std":       round(float(s), 4),
                "has_historical_data": (i in REAL_FEATURES),
            }
            for i, (name, w, s) in enumerate(zip(FEATURE_NAMES, heur_w, heur_stds))
        ],
    }

    out_path = ROOT / "mlb_factor_analysis_v2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"  Saved  {out_path}")
    print()
    print("=" * 104)
    print("  V2 ANALYSIS COMPLETE")
    print(f"  Winner prediction  top: {ml_ranked[0]['feature']}  "
          f"(score {ml_ranked[0]['combined_score']:.4f})")
    print(f"  Run line coverage  top: {rl_ranked[0]['feature']}  "
          f"(score {rl_ranked[0]['combined_score']:.4f})")
    print(f"  Totals             top: {tot_ranked[0]['feature']}  "
          f"(score {tot_ranked[0]['combined_score']:.4f})")
    print("  SP feature importance: ", end="")
    sp_feats = ["sp_era_diff", "sp_whip_diff", "sp_k_rate_diff",
                "home_sp_rest", "away_sp_rest", "sp_hand_adv"]
    sp_scores = {r["feature"]: r["combined_score"] for r in ml_ranked}
    for feat in sp_feats:
        score = sp_scores.get(feat, 0.0)
        print(f"{feat}={score:.3f}", end="  ")
    print()
    print("=" * 104)


if __name__ == "__main__":
    main()
