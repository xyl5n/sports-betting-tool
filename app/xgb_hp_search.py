"""
One-off XGBoost hyperparameter search for MLB moneyline + run-line classifiers.

Uses the cached enriched historical dataset (.cache/enriched_mlb_dataset.joblib)
so the search is reproducible without API-Sports access.  The relative ranking
of (n_estimators, max_depth) configs found here should transfer to the
production training data flow (historical + current-season + weighting).

Reports CV accuracy for:
  - "Before"   : original hyperparams (min_child_weight=5, gamma=1.0)
  - "Reg-only" : moneyline with min_child_weight=2, gamma=0.3  (run-line unchanged)
  - Grid sweep : every (n_estimators, max_depth) combination
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import joblib
import numpy as np
import xgboost as xgb
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

from src.sports_config import MLB_FEATURES

DATA = joblib.load(".cache/enriched_mlb_dataset.joblib")
X = DATA["X"]
y_ml = DATA["y_ml"]
y_rl = DATA["y_rl"]
print(f"Dataset: {X.shape[0]:,} rows x {X.shape[1]} features  "
      f"(moneyline pos rate = {y_ml.mean():.3f},  "
      f"run-line pos rate = {y_rl.mean():.3f})")

scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)


def cv_acc(params: dict, y: np.ndarray) -> float:
    clf = xgb.XGBClassifier(**params)
    scores = cross_val_score(clf, X_scaled, y, cv=5, scoring="accuracy", n_jobs=-1)
    return float(scores.mean())


BASELINE = dict(
    n_estimators=200, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    min_child_weight=5, gamma=1.0, reg_lambda=2.0,
    eval_metric="logloss", random_state=42,
)

MONEYLINE_LOWREG = {**BASELINE, "min_child_weight": 2, "gamma": 0.3}

print("\n" + "=" * 72)
print("  Baseline CV (original shared hyperparams, identical for both models)")
print("=" * 72)
t0 = time.time()
ml_before = cv_acc(BASELINE, y_ml)
rl_before = cv_acc(BASELINE, y_rl)
print(f"  Moneyline : {ml_before:.4%}")
print(f"  Run line  : {rl_before:.4%}")
print(f"  ({time.time()-t0:.1f}s)")

print("\n" + "=" * 72)
print("  Moneyline with reduced regularization (min_child_weight=2, gamma=0.3)")
print("=" * 72)
t0 = time.time()
ml_lowreg = cv_acc(MONEYLINE_LOWREG, y_ml)
print(f"  Moneyline : {ml_lowreg:.4%}   (delta {ml_lowreg - ml_before:+.4%})")
print(f"  ({time.time()-t0:.1f}s)")

print("\n" + "=" * 72)
print("  Grid search: n_estimators x max_depth")
print("  (moneyline uses low-reg base; run-line uses original-reg base)")
print("=" * 72)

N_EST_GRID = [100, 200, 300, 400]
DEPTH_GRID = [3, 4, 5, 6]


def grid(base: dict, y: np.ndarray, label: str) -> tuple[dict, float, list]:
    results = []
    print(f"\n  {label}:")
    print(f"  {'n_est':>6} {'depth':>6}  {'CV acc':>8}")
    print(f"  {'-'*6} {'-'*6}  {'-'*8}")
    t0 = time.time()
    for ne in N_EST_GRID:
        for md in DEPTH_GRID:
            params = {**base, "n_estimators": ne, "max_depth": md}
            acc = cv_acc(params, y)
            results.append((ne, md, acc))
            print(f"  {ne:>6} {md:>6}  {acc:>8.4%}")
    best = max(results, key=lambda r: r[2])
    print(f"  best: n_est={best[0]}, max_depth={best[1]}  ->{best[2]:.4%}  "
          f"({time.time()-t0:.1f}s)")
    return ({**base, "n_estimators": best[0], "max_depth": best[1]},
            best[2], results)


ml_best_params, ml_best, ml_grid = grid(MONEYLINE_LOWREG, y_ml, "Moneyline (low-reg base)")
rl_best_params, rl_best, rl_grid = grid(BASELINE,         y_rl, "Run line (original-reg base)")

print("\n" + "=" * 72)
print("  Summary  --  before vs. after for each change")
print("=" * 72)
print(f"\n  Moneyline (target {y_ml.shape[0]:,} rows, 24 features):")
print(f"    Before (shared hyperparams + high reg)    : {ml_before:.4%}")
print(f"    + Reduced regularization (mcw=2, gamma=0.3)   : {ml_lowreg:.4%}   "
      f"(delta {ml_lowreg - ml_before:+.4%})")
print(f"    + Grid-best n_est={ml_best_params['n_estimators']}, "
      f"depth={ml_best_params['max_depth']}                : {ml_best:.4%}   "
      f"(delta vs. before: {ml_best - ml_before:+.4%})")

print(f"\n  Run line  (target {y_rl.shape[0]:,} rows, 24 features):")
print(f"    Before (shared hyperparams)               : {rl_before:.4%}")
print(f"    + Grid-best n_est={rl_best_params['n_estimators']}, "
      f"depth={rl_best_params['max_depth']}                : {rl_best:.4%}   "
      f"(delta vs. before: {rl_best - rl_before:+.4%})")

print("\n  Apply to source:")
print(f"    XGB_MONEYLINE_PARAMS  -> n_estimators={ml_best_params['n_estimators']}, "
      f"max_depth={ml_best_params['max_depth']}  (keep mcw=2, gamma=0.3)")
print(f"    XGB_RUN_LINE_PARAMS   -> n_estimators={rl_best_params['n_estimators']}, "
      f"max_depth={rl_best_params['max_depth']}  (keep mcw=5, gamma=1.0)")
