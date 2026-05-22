"""
props_training.py
=================
Importable wrapper around the props model training pipeline.

Pulled out of scripts/train_props_models.py so the admin Train Props
Models button can invoke training in a background thread without
shelling out to a subprocess.  The CLI script still works -- it now
imports `run_training` from this module.

Public API
----------
    run_training(season, *, skip_pitcher=False, skip_batter=False,
                 push=True, status_callback=None) -> dict

The optional `status_callback(stage, **details)` is invoked at each
pipeline boundary so a long-running call can stream progress back to a
polling UI without the caller having to thread a global through the
training functions.  Stages the callback receives, in order:

    "started"          {}
    "fetching"         {target: "pitcher" | "batter"}
    "features"         {target, rows: int, positive_rate: float}
    "fold"             {target, fold: int, acc: float, log_loss: float}
    "trained"          {target, oof_acc: float, saved_to: str}
    "skipped"          {target, reason: str}
    "supabase_push"    {result: dict}
    "complete"         {summary: dict}
    "error"            {message: str}

Logging
-------
Every step emits `PROPS-TRAIN: ...` to stderr.  Same prefix the CLI
script uses so deploy logs read the same regardless of caller.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional

_CACHE_DIR = Path(".cache")
_TRAIN_DIR = _CACHE_DIR / "props_train"

StatusCallback = Optional[Callable[..., None]]


def _log(msg: str) -> None:
    print(f"PROPS-TRAIN: {msg}", flush=True, file=sys.stderr)


def _emit(cb: StatusCallback, stage: str, **details) -> None:
    """Best-effort status emitter.  Failures in the callback never
    abort training."""
    if cb is None:
        return
    try:
        cb(stage, **details)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"status_callback {stage} raised {type(exc).__name__}: {exc} (ignored)")


# ── pybaseball pulls (cached per season to .cache/props_train/) ─────────────

def _ensure_pybaseball() -> bool:
    try:
        import pybaseball  # noqa: PLC0415, F401
        return True
    except ImportError as exc:
        _log(f"pybaseball not installed ({exc}) -- aborting")
        return False


def fetch_pitcher_logs(season: int):
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cached = _TRAIN_DIR / f"pitcher_logs_{season}.parquet"
    try:
        import pandas as pd  # noqa: F401
        if cached.exists():
            _log(f"pitcher logs cached: {cached}")
            return pd.read_parquet(cached)
    except ImportError:
        _log("pandas not installed -- aborting")
        return None
    try:
        import pybaseball as pyb
        _log(f"fetching pitching_stats({season}, qual=1) ...")
        df = pyb.pitching_stats(season, season, qual=1)
        df.to_parquet(cached, index=False)
        _log(f"pitcher logs written: {cached} ({len(df)} rows)")
        return df
    except Exception as exc:                                              # noqa: BLE001
        _log(f"pitcher fetch failed: {exc}")
        return None


def fetch_batter_logs(season: int):
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cached = _TRAIN_DIR / f"batter_logs_{season}.parquet"
    try:
        import pandas as pd  # noqa: F401
        if cached.exists():
            _log(f"batter logs cached: {cached}")
            return pd.read_parquet(cached)
    except ImportError:
        return None
    try:
        import pybaseball as pyb
        _log(f"fetching batting_stats({season}, qual=1) ...")
        df = pyb.batting_stats(season, season, qual=1)
        df.to_parquet(cached, index=False)
        _log(f"batter logs written: {cached} ({len(df)} rows)")
        return df
    except Exception as exc:                                              # noqa: BLE001
        _log(f"batter fetch failed: {exc}")
        return None


# ── Feature builders ────────────────────────────────────────────────────────

def build_pitcher_features(df):
    if df is None or len(df) == 0:
        return None, None
    try:
        import pandas as pd
        import numpy as np  # noqa: F401
    except ImportError:
        return None, None
    cols = [c for c in ("ERA", "K/9", "BB/9", "H/9", "WHIP", "IP", "SO") if c in df.columns]
    if len(cols) < 4:
        _log(f"pitcher feature build skipped: missing columns (have {list(df.columns)[:20]})")
        return None, None
    X = df[cols].fillna(df[cols].median()).to_numpy(dtype=float)
    so_per_game = df.get("SO", pd.Series([0] * len(df))).astype(float) / df.get(
        "G", pd.Series([1] * len(df))).astype(float).clip(lower=1)
    y = (so_per_game >= 6.0).astype(int).to_numpy()
    _log(f"pitcher features: X.shape={X.shape}  positive_rate={y.mean():.3f}")
    return X, y


def build_batter_features(df):
    if df is None or len(df) == 0:
        return None, None
    try:
        import pandas as pd
        import numpy as np  # noqa: F401
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


# ── Fit + save ──────────────────────────────────────────────────────────────

def _train_and_save(
    X, y, out_path: Path, *, label: str,
    status_callback: StatusCallback = None,
) -> Optional[float]:
    if X is None or y is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) -- skipping train")
        _emit(status_callback, "skipped",
              target=label, reason=f"only {0 if X is None else len(X)} rows")
        return None
    try:
        from sklearn.model_selection import StratifiedKFold
        from sklearn.metrics         import accuracy_score, log_loss
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        _emit(status_callback, "skipped",
              target=label, reason=f"missing dep: {exc}")
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
        _emit(status_callback, "fold",
              target=label, fold=fold, acc=float(fold_acc),
              log_loss=float(fold_ll))
    oof_acc = accuracy_score(y, (oof >= 0.5).astype(int))
    oof_ll  = log_loss(y, oof, labels=[0, 1])
    _log(f"{label} OOF: acc={oof_acc:.3f}  log_loss={oof_ll:.3f}")

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
    _emit(status_callback, "trained",
          target=label, oof_acc=float(oof_acc), saved_to=str(out_path))
    return float(oof_acc)


# ── Public driver ──────────────────────────────────────────────────────────

def run_training(
    season: int,
    *,
    skip_pitcher: bool = False,
    skip_batter:  bool = False,
    push:         bool = True,
    status_callback: StatusCallback = None,
) -> dict:
    """Run the full pipeline for *season* and return a summary dict.

    Identical to the CLI's behavior but callable from a long-lived
    process (e.g. the admin Train Props Models button's background
    thread).  status_callback(stage, **details) is invoked at every
    pipeline boundary -- see module docstring for the stage list.
    """
    _emit(status_callback, "started")
    if not _ensure_pybaseball():
        _emit(status_callback, "error", message="pybaseball not installed")
        return {"season": season, "error": "pybaseball not installed"}

    started = time.monotonic()
    summary: dict = {"season": season}

    if not skip_pitcher:
        _emit(status_callback, "fetching", target="pitcher")
        pdf = fetch_pitcher_logs(season)
        X, y = build_pitcher_features(pdf)
        if X is not None and y is not None:
            _emit(status_callback, "features",
                  target="pitcher", rows=int(len(X)),
                  positive_rate=float(y.mean()) if len(y) else 0.0)
        acc = _train_and_save(
            X, y, Path(".cache/props_model_pitcher.joblib"),
            label="pitcher", status_callback=status_callback,
        )
        summary["pitcher_oof_acc"] = acc

    if not skip_batter:
        _emit(status_callback, "fetching", target="batter")
        bdf = fetch_batter_logs(season)
        X, y = build_batter_features(bdf)
        if X is not None and y is not None:
            _emit(status_callback, "features",
                  target="batter", rows=int(len(X)),
                  positive_rate=float(y.mean()) if len(y) else 0.0)
        acc = _train_and_save(
            X, y, Path(".cache/props_model_batter.joblib"),
            label="batter", status_callback=status_callback,
        )
        summary["batter_oof_acc"] = acc

    if push:
        try:
            from .props_model import push_models_to_supabase
            result = push_models_to_supabase()
            summary["supabase_push"] = result
            _emit(status_callback, "supabase_push", result=result)
        except Exception as exc:                                          # noqa: BLE001
            summary["supabase_push"] = f"error: {exc}"
            _log(f"supabase push failed: {exc}")
            _emit(status_callback, "supabase_push",
                  result={"error": str(exc)})

    elapsed = time.monotonic() - started
    summary["elapsed_seconds"] = round(elapsed, 1)
    _log(f"DONE in {elapsed:.1f}s  summary={json.dumps(summary, default=str)}")
    _emit(status_callback, "complete", summary=summary)
    return summary
