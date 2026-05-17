"""
Before/after CV comparison for the LR feature expansion.

BEFORE: 24-feature enriched dataset (old backup at app/.backups/.../enriched_mlb_dataset.joblib)
AFTER : 30-feature enriched dataset (newly rebuilt at app/.cache/enriched_mlb_dataset.joblib)

Both use the same LR config that was tuned earlier:
  Moneyline : C=2.0,  solver=lbfgs
  Run line  : C=0.01, solver=lbfgs

The shared StandardScaler is fit fresh inside the script (mirroring how
production _train() refits it on each retrain). Recency sample weights match
production (15% old / 25% prev / no current-season offline).
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

warnings.filterwarnings("ignore")

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO / "app"))

from src.recency_weights import compute_sample_weights  # noqa: E402

OLD_DS = REPO / "app" / ".backups" / "pre_restructure_20260516_141953" / ".cache" / "enriched_mlb_dataset.joblib"
NEW_DS = REPO / "app" / ".cache" / "enriched_mlb_dataset.joblib"

LR_ML_C = 2.0
LR_RL_C = 0.01


def load(path: Path):
    d = joblib.load(path)
    X       = d["X"]
    y_ml    = d["y_ml"]
    y_rl    = d["y_rl"]
    seasons = d["row_seasons"]
    mask_prev = seasons == 2025
    mask_old  = ~mask_prev
    X_ord = np.vstack([X[mask_old], X[mask_prev]])
    y_ml  = np.concatenate([y_ml[mask_old], y_ml[mask_prev]])
    y_rl  = np.concatenate([y_rl[mask_old], y_rl[mask_prev]])
    n_old, n_prev = int(mask_old.sum()), int(mask_prev.sum())
    w = compute_sample_weights(n_old, n_prev, n_current=0)
    return X_ord, y_ml, y_rl, w


def cv(C, X, y, w):
    clf = LogisticRegression(C=C, solver="lbfgs", max_iter=2000, random_state=42)
    try:
        s = cross_val_score(
            clf, X, y, cv=5, scoring="accuracy",
            fit_params={"sample_weight": w},
        )
    except TypeError:
        s = cross_val_score(clf, X, y, cv=5, scoring="accuracy")
    return float(s.mean())


def evaluate(label: str, ds_path: Path):
    if not ds_path.exists():
        print(f"  {label}: dataset not found at {ds_path}")
        return None, None, 0
    X, y_ml, y_rl, w = load(ds_path)
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    ml = cv(LR_ML_C, Xs, y_ml, w)
    rl = cv(LR_RL_C, Xs, y_rl, w)
    n_feat = X.shape[1]
    print(f"  {label}: n_rows={len(y_ml):,}  n_features={n_feat}  "
          f"LR ML CV={ml:.4f}  LR RL CV={rl:.4f}")
    return ml, rl, n_feat


def main():
    print("\n=== BEFORE (24-feature dataset) ===")
    ml_b, rl_b, n_b = evaluate("before", OLD_DS)

    print("\n=== AFTER (30-feature dataset) ===")
    ml_a, rl_a, n_a = evaluate("after", NEW_DS)

    if ml_b is None or ml_a is None:
        print("\nMissing a dataset — cannot compare.")
        return

    print("\n=== SUMMARY ===")
    print(f"Moneyline LR (C={LR_ML_C}):  "
          f"{ml_b:.4f} (n_feat={n_b}) -> {ml_a:.4f} (n_feat={n_a})  "
          f"Δ = {(ml_a - ml_b) * 100:+.2f} pp")
    print(f"Run line  LR (C={LR_RL_C}): "
          f"{rl_b:.4f} (n_feat={n_b}) -> {rl_a:.4f} (n_feat={n_a})  "
          f"Δ = {(rl_a - rl_b) * 100:+.2f} pp")


if __name__ == "__main__":
    main()
