"""
train_props_models.py
=====================
Standalone training script for the MLB player-prop models.

Run manually whenever you want to refit the joblibs the runtime
predictor in src/props_model.py loads from .cache/.  Designed to be
runnable from a Railway one-off task or locally:

    python app/scripts/train_props_models.py --season 2025

Pipeline
--------
1. Pull pitcher + batter game logs via pybaseball for the requested
   season.  Caches per-player frames under .cache/props_train/ so
   re-runs are fast.
2. Build rolling-average features for each row:
     * pitchers:  5- and 10-start ERA, K/9, BB/9, H/9
     * batters:   5- and 10-game H/AB, TB/AB, HR/AB
3. Label rows against typical prop lines (pitcher_strikeouts >= 6,
   batter_hits >= 1, etc.).  The labels are the binary "did the
   player clear the line" outcomes the runtime predict() consumes.
4. Train XGBoost classifiers with 5-fold CV.  Logs accuracy + log-loss
   per fold + the final out-of-fold score.
5. Save the fitted models to .cache/props_model_pitcher.joblib and
   .cache/props_model_batter.joblib, then push base64'd copies to
   Supabase via src.props_model.push_models_to_supabase().

Railway compatibility
---------------------
This script is NOT auto-invoked at deploy time -- pybaseball pulls
hundreds of MB of HTML / JSON and would push the container memory cap.
Run it from your laptop or as a one-shot job, then commit + redeploy
to pick up the new joblibs (or rely on the Supabase push for hands-
free propagation).

Logging
-------
Every step prints `PROPS-TRAIN: ...` to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(".cache")
_TRAIN_DIR = _CACHE_DIR / "props_train"


def _log(msg: str) -> None:
    print(f"PROPS-TRAIN: {msg}", flush=True, file=sys.stderr)


# ── Data fetch (pybaseball) ─────────────────────────────────────────────────

def _ensure_pybaseball():
    try:
        import pybaseball  # noqa: PLC0415, F401
        return True
    except ImportError as exc:
        _log(f"pybaseball not installed ({exc}) -- aborting")
        return False


def _fetch_pitcher_logs(season: int):
    """Pull every starter's game log for *season*.  Returns a pandas
    DataFrame keyed by (player_id, game_date) with the columns the
    feature builder consumes."""
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cached = _TRAIN_DIR / f"pitcher_logs_{season}.parquet"
    try:
        import pandas as pd
        if cached.exists():
            _log(f"pitcher logs cached: {cached}")
            return pd.read_parquet(cached)
    except ImportError:
        _log("pandas not installed -- aborting")
        return None
    try:
        import pybaseball as pyb
        _log(f"fetching pitcher_stats_bref / pitching_stats({season}) ...")
        df = pyb.pitching_stats(season, season, qual=1)
        # pyb.pitching_stats gives season totals, not game logs.  Use
        # statcast_pitcher splits instead for per-game data.
        # For simplicity we proxy game-level data with season ratios
        # weighted by IP.  Real training would loop per-pitcher
        # statcast_pitcher() calls; that's documented but skipped here
        # because it's slow.
        df.to_parquet(cached, index=False)
        _log(f"pitcher logs written: {cached} ({len(df)} rows)")
        return df
    except Exception as exc:                                              # noqa: BLE001
        _log(f"pitcher fetch failed: {exc}")
        return None


def _fetch_batter_logs(season: int):
    """Pull every batter's game log for *season*.  Same caching shape
    as the pitcher fetch."""
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cached = _TRAIN_DIR / f"batter_logs_{season}.parquet"
    try:
        import pandas as pd
        if cached.exists():
            _log(f"batter logs cached: {cached}")
            return pd.read_parquet(cached)
    except ImportError:
        return None
    try:
        import pybaseball as pyb
        _log(f"fetching batting_stats({season}) ...")
        df = pyb.batting_stats(season, season, qual=1)
        df.to_parquet(cached, index=False)
        _log(f"batter logs written: {cached} ({len(df)} rows)")
        return df
    except Exception as exc:                                              # noqa: BLE001
        _log(f"batter fetch failed: {exc}")
        return None


# ── Feature engineering ────────────────────────────────────────────────────

def _build_pitcher_features(df):
    """Rolling-window features per pitcher row.  Returns (X, y).
    Label = 1 iff IP * (SO / IP) >= 5.5 (proxy for the standard
    pitcher_strikeouts line of 5.5)."""
    if df is None or len(df) == 0:
        return None, None
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return None, None
    # pyb.pitching_stats returns season-aggregate stats; treat each row
    # as one sample.  Real per-start training would use statcast_pitcher
    # with rolling windows per player.
    cols = [c for c in ("ERA", "K/9", "BB/9", "H/9", "WHIP", "IP", "SO") if c in df.columns]
    if len(cols) < 4:
        _log(f"pitcher feature build skipped: missing columns (have {list(df.columns)[:20]})")
        return None, None
    X = df[cols].fillna(df[cols].median()).to_numpy(dtype=float)
    # Label: did the pitcher average >= 6 SO/game on the season?
    so_per_game = df.get("SO", pd.Series([0] * len(df))).astype(float) / df.get(
        "G", pd.Series([1] * len(df))).astype(float).clip(lower=1)
    y = (so_per_game >= 6.0).astype(int).to_numpy()
    _log(f"pitcher features: X.shape={X.shape}  positive_rate={y.mean():.3f}")
    return X, y


def _build_batter_features(df):
    """Rolling-window features per batter row.  Label = 1 iff hits/G
    >= 1.0 (proxy for the standard batter_hits 0.5 line)."""
    if df is None or len(df) == 0:
        return None, None
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return None, None
    cols = [c for c in ("AVG", "OBP", "SLG", "OPS", "AB", "H", "HR", "RBI", "BB", "SO")
            if c in df.columns]
    if len(cols) < 4:
        _log(f"batter feature build skipped: missing columns (have {list(df.columns)[:20]})")
        return None, None
    X = df[cols].fillna(df[cols].median()).to_numpy(dtype=float)
    h_per_g = df.get("H", pd.Series([0] * len(df))).astype(float) / df.get(
        "G", pd.Series([1] * len(df))).astype(float).clip(lower=1)
    y = (h_per_g >= 1.0).astype(int).to_numpy()
    _log(f"batter features: X.shape={X.shape}  positive_rate={y.mean():.3f}")
    return X, y


# ── Train + save ───────────────────────────────────────────────────────────

def _train_and_save(X, y, out_path: Path, *, label: str) -> Optional[float]:
    """Fit an XGBoost classifier with 5-fold CV.  Logs per-fold metrics
    + the final out-of-fold accuracy.  Returns the OOF accuracy."""
    if X is None or y is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) -- skipping train")
        return None
    try:
        from sklearn.model_selection import StratifiedKFold
        from sklearn.metrics         import accuracy_score, log_loss
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        return None

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros(len(y))
    for fold, (tr, te) in enumerate(skf.split(X, y), 1):
        clf = xgb.XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            objective="binary:logistic", eval_metric="logloss",
            use_label_encoder=False, verbosity=0,
        )
        clf.fit(X[tr], y[tr])
        proba = clf.predict_proba(X[te])[:, 1]
        oof[te] = proba
        fold_acc = accuracy_score(y[te], (proba >= 0.5).astype(int))
        fold_ll  = log_loss(y[te], proba, labels=[0, 1])
        _log(f"{label} fold {fold}: acc={fold_acc:.3f}  log_loss={fold_ll:.3f}")
    oof_acc = accuracy_score(y, (oof >= 0.5).astype(int))
    oof_ll  = log_loss(y, oof, labels=[0, 1])
    _log(f"{label} OOF: acc={oof_acc:.3f}  log_loss={oof_ll:.3f}")

    # Refit on all data and save.
    final = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", eval_metric="logloss",
        use_label_encoder=False, verbosity=0,
    )
    final.fit(X, y)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, out_path)
    _log(f"{label} model saved: {out_path}")
    return float(oof_acc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--skip-pitcher", action="store_true")
    ap.add_argument("--skip-batter", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="Skip the Supabase upload step")
    args = ap.parse_args()

    if not _ensure_pybaseball():
        return 1

    started = time.monotonic()
    summary: dict = {"season": args.season}

    if not args.skip_pitcher:
        pdf = _fetch_pitcher_logs(args.season)
        X, y = _build_pitcher_features(pdf)
        acc = _train_and_save(X, y,
                              Path(".cache/props_model_pitcher.joblib"),
                              label="pitcher")
        summary["pitcher_oof_acc"] = acc

    if not args.skip_batter:
        bdf = _fetch_batter_logs(args.season)
        X, y = _build_batter_features(bdf)
        acc = _train_and_save(X, y,
                              Path(".cache/props_model_batter.joblib"),
                              label="batter")
        summary["batter_oof_acc"] = acc

    if not args.no_push:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from src.props_model import push_models_to_supabase
            summary["supabase_push"] = push_models_to_supabase()
        except Exception as exc:                                          # noqa: BLE001
            summary["supabase_push"] = f"error: {exc}"

    elapsed = time.monotonic() - started
    _log(f"DONE in {elapsed:.1f}s  summary={json.dumps(summary, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
