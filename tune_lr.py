"""
One-off Logistic Regression tuning sweep for MLB moneyline & run-line models.
Reproduces production training pipeline:
  - Loads enriched historical dataset (.cache/enriched_mlb_dataset.joblib)
  - Splits rows by season (old <=2024 / prev 2025)
  - Applies the production recency sample-weight scheme (n_current=0 here
    since no current-season API data is available offline; historical is
    ~95% of production training anyway, so optimal C is stable.)
  - Fits a *fresh* StandardScaler (the production scalers are not touched)
  - Sweeps C in {0.01, 0.1, 0.5, 1.0, 2.0, 5.0}
                class_weight in {None, "balanced"}
                solver in {"lbfgs", "saga"}
  - Reports 5-fold CV accuracy for each combination
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

# Suppress saga convergence chatter — we report mean CV accuracy regardless
warnings.filterwarnings("ignore")

REPO   = Path(__file__).parent
sys.path.insert(0, str(REPO / "app"))

from src.recency_weights import compute_sample_weights  # noqa: E402

DATASET = REPO / "app" / ".backups" / "pre_restructure_20260516_141953" / ".cache" / "enriched_mlb_dataset.joblib"

C_GRID            = [0.01, 0.1, 0.5, 1.0, 2.0, 5.0]
CLASS_WEIGHTS     = [None, "balanced"]
SOLVERS           = ["lbfgs", "saga"]
N_SPLITS          = 5
BASELINE_C        = 1.0
BASELINE_CW       = None
BASELINE_SOLVER   = "lbfgs"


def load_data():
    d = joblib.load(DATASET)
    X        = d["X"]
    y_ml     = d["y_ml"]
    y_rl     = d["y_rl"]
    seasons  = d["row_seasons"]
    # Production layout: [old (<=2024) | prev (2025) | current (none here)]
    mask_prev = seasons == 2025
    mask_old  = ~mask_prev
    X_ord  = np.vstack([X[mask_old],     X[mask_prev]])
    y_ml_o = np.concatenate([y_ml[mask_old], y_ml[mask_prev]])
    y_rl_o = np.concatenate([y_rl[mask_old], y_rl[mask_prev]])
    n_old  = int(mask_old.sum())
    n_prev = int(mask_prev.sum())
    weights = compute_sample_weights(n_old, n_prev, n_current=0)
    return X_ord, y_ml_o, y_rl_o, weights


def cv_acc(clf, X, y, w) -> float:
    try:
        scores = cross_val_score(
            clf, X, y, cv=N_SPLITS, scoring="accuracy",
            fit_params={"sample_weight": w},
        )
    except TypeError:
        scores = cross_val_score(clf, X, y, cv=N_SPLITS, scoring="accuracy")
    return float(scores.mean())


def sweep(label: str, X_scaled, y, w):
    print(f"\n=== {label} ===")
    # Baseline: current production config (C=1.0, lbfgs, no class_weight)
    base = LogisticRegression(
        C=BASELINE_C, solver=BASELINE_SOLVER,
        class_weight=BASELINE_CW, max_iter=2000, random_state=42,
    )
    base_acc = cv_acc(base, X_scaled, y, w)
    print(f"BASELINE  C={BASELINE_C}  solver={BASELINE_SOLVER}  "
          f"class_weight={BASELINE_CW}  -> CV {base_acc:.4f}")

    rows = []
    for solver in SOLVERS:
        for cw in CLASS_WEIGHTS:
            for C in C_GRID:
                clf = LogisticRegression(
                    C=C, solver=solver, class_weight=cw,
                    max_iter=5000, random_state=42,
                )
                acc = cv_acc(clf, X_scaled, y, w)
                rows.append((solver, cw, C, acc))
    rows.sort(key=lambda r: r[3], reverse=True)
    print(f"{'solver':>6}  {'class_w':>9}  {'C':>5}  {'CV acc':>8}  delta")
    for solver, cw, C, acc in rows:
        delta = (acc - base_acc) * 100
        print(f"{solver:>6}  {str(cw):>9}  {C:>5}  {acc:.4f}  {delta:+.2f} pp")
    best = rows[0]
    print(f"\nBest for {label}: solver={best[0]} class_weight={best[1]} "
          f"C={best[2]} -> CV {best[3]:.4f} "
          f"(vs baseline {base_acc:.4f}, +{(best[3] - base_acc) * 100:.2f} pp)")
    return base_acc, best


def main():
    X, y_ml, y_rl, w = load_data()
    print(f"Loaded {len(y_ml):,} rows; old+prev split = "
          f"{int((w == w[0]).sum())} old / {len(y_ml) - int((w == w[0]).sum())} prev")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    ml_base, ml_best = sweep("MONEYLINE (y_ml)",  X_scaled, y_ml, w)
    rl_base, rl_best = sweep("RUN LINE (y_rl)",   X_scaled, y_rl, w)

    print("\n=== SUMMARY ===")
    print(f"Moneyline  baseline {ml_base:.4f}  ->  best {ml_best[3]:.4f}  "
          f"(solver={ml_best[0]}, class_weight={ml_best[1]}, C={ml_best[2]})")
    print(f"Run line   baseline {rl_base:.4f}  ->  best {rl_best[3]:.4f}  "
          f"(solver={rl_best[0]}, class_weight={rl_best[1]}, C={rl_best[2]})")


if __name__ == "__main__":
    main()
