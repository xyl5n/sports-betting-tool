"""
Before/after CV comparison for the LR run-line hurdle refactor.

BEFORE: one LR(C=0.01) trained on y_rl directly (independent marginal classifier).
        This is the exact pattern used in production before this turn.

AFTER:  TWO LRs composed by the hurdle identity
            P(margin >= 2) = P(margin > 0) * P(margin >= 2 | margin > 0)
        - ml_lr  : LR(C=2.0)  trained on y_ml (full data)
        - cond_lr: LR(C=0.01) trained on y_rl restricted to home-won subset
        Predicted P(margin >= 2) = ml_lr_prob * cond_lr_prob, by construction
        always <= ml_lr_prob. Threshold at 0.5 to get the RL class label.

Both run on the same enriched 30-feature dataset with the same recency
sample weights (15% old / 25% prev). Five-fold CV.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import KFold, cross_val_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO / "app"))
from src.recency_weights import compute_sample_weights  # noqa: E402

DATASET = REPO / "app" / ".cache" / "enriched_mlb_dataset.joblib"

LR_ML_C = 2.0
LR_RL_C = 0.01
N_SPLITS = 5
SEED = 42


def load():
    d = joblib.load(DATASET)
    X       = d["X"]
    y_ml    = d["y_ml"].astype(np.int32)
    y_rl    = d["y_rl"].astype(np.int32)
    seasons = d["row_seasons"]
    mask_prev = seasons == 2025
    mask_old  = ~mask_prev
    X_ord  = np.vstack([X[mask_old], X[mask_prev]])
    y_ml_o = np.concatenate([y_ml[mask_old], y_ml[mask_prev]])
    y_rl_o = np.concatenate([y_rl[mask_old], y_rl[mask_prev]])
    n_old, n_prev = int(mask_old.sum()), int(mask_prev.sum())
    w = compute_sample_weights(n_old, n_prev, n_current=0)
    return X_ord, y_ml_o, y_rl_o, w


def before_cv(X, y_rl, w):
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    clf = LogisticRegression(
        C=LR_RL_C, solver="lbfgs", max_iter=2000, random_state=SEED,
    )
    try:
        s = cross_val_score(
            clf, Xs, y_rl, cv=N_SPLITS, scoring="accuracy",
            fit_params={"sample_weight": w},
        )
    except TypeError:
        s = cross_val_score(clf, Xs, y_rl, cv=N_SPLITS, scoring="accuracy")
    return float(s.mean())


def after_cv(X, y_ml, y_rl, w):
    """
    Manual 5-fold CV that trains two LRs per fold and composes them.
    Reproduces what the production RunLineModel.predict now does.
    """
    n = len(y_rl)
    kf = KFold(n_splits=N_SPLITS, shuffle=True, random_state=SEED)
    fold_accs = []
    for fold_idx, (train_idx, test_idx) in enumerate(kf.split(X), 1):
        X_tr, X_te = X[train_idx], X[test_idx]
        y_ml_tr, y_rl_tr = y_ml[train_idx], y_rl[train_idx]
        y_rl_te = y_rl[test_idx]
        w_tr = w[train_idx]

        scaler = StandardScaler()
        X_tr_s = scaler.fit_transform(X_tr)
        X_te_s = scaler.transform(X_te)

        # ml_lr on full training fold
        ml_lr = LogisticRegression(
            C=LR_ML_C, solver="lbfgs", max_iter=2000, random_state=SEED,
        )
        ml_lr.fit(X_tr_s, y_ml_tr, sample_weight=w_tr)

        # cond_lr on home-won subset
        hw_mask = (y_ml_tr == 1)
        if hw_mask.sum() < 30 or len(np.unique(y_rl_tr[hw_mask])) < 2:
            # Degenerate fold — fall back to independent
            rl_lr = LogisticRegression(
                C=LR_RL_C, solver="lbfgs", max_iter=2000, random_state=SEED,
            )
            rl_lr.fit(X_tr_s, y_rl_tr, sample_weight=w_tr)
            pred = (rl_lr.predict_proba(X_te_s)[:, 1] >= 0.5).astype(int)
        else:
            cond_lr = LogisticRegression(
                C=LR_RL_C, solver="lbfgs", max_iter=2000, random_state=SEED,
            )
            cond_lr.fit(X_tr_s[hw_mask], y_rl_tr[hw_mask],
                        sample_weight=w_tr[hw_mask])
            ml_p   = ml_lr.predict_proba(X_te_s)[:, 1]
            cond_p = cond_lr.predict_proba(X_te_s)[:, 1]
            composed = ml_p * cond_p
            pred = (composed >= 0.5).astype(int)
        acc = float(np.mean(pred == y_rl_te))
        fold_accs.append(acc)
        print(f"  fold {fold_idx}: acc={acc:.4f}  "
              f"(home_won_train={int(hw_mask.sum()):,} rows)")
    return float(np.mean(fold_accs))


def main():
    X, y_ml, y_rl, w = load()
    print(f"Loaded {len(y_rl):,} rows  (y_rl rate={y_rl.mean():.1%}, "
          f"y_ml rate={y_ml.mean():.1%})")
    print(f"Within home-wins: y_rl rate = {y_rl[y_ml == 1].mean():.1%}  "
          f"(conditional task base rate)")

    print("\n=== BEFORE: independent LR(C=0.01) on y_rl ===")
    before = before_cv(X, y_rl, w)
    print(f"  CV accuracy = {before:.4f}")

    print("\n=== AFTER: composed ml_lr * cond_lr ===")
    after = after_cv(X, y_ml, y_rl, w)
    print(f"  CV accuracy = {after:.4f}")

    diff = (after - before) * 100
    print("\n=== SUMMARY ===")
    print(f"Run-line LR  before={before:.4f}  ->  after={after:.4f}  "
          f"delta={diff:+.2f} pp")


if __name__ == "__main__":
    main()
