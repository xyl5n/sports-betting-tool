"""
props_model.py
==============
Per-market prediction model for MLB player props.

Two model artifacts are loaded at module level:
  .cache/props_model_pitcher.joblib   (xgb classifier for pitcher markets)
  .cache/props_model_batter.joblib    (xgb classifier for batter markets)

Both are restored from Supabase on cold boot (the same pattern
src.model uses for the moneyline / run-line / totals joblibs).  When
no trained artifact is available the predictor falls back to a market-
neutral heuristic that uses the prop line's implied probability so the
UI always renders something rather than blanking out.

Training lives in scripts/train_props_models.py (pybaseball-driven).
The runtime API here is read-only -- prediction + record tracking.

Logging
-------
Every predict() and settle() call emits a PROPS-MODEL / PROPS-SETTLE
stderr line so Railway captures the predictor's behaviour the same way
the existing model.py emits "model loaded" / "predict" lines.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

from .utils import _safe

_CACHE_DIR = Path(".cache")

# Artifact paths.  joblib because that's what BettingModel / RunLineModel
# / TotalsModel already use, and the Supabase sync logic in
# src.model.py + the boot mirror code in app.py can be reused as-is.
PITCHER_MODEL_PATH = _CACHE_DIR / "props_model_pitcher.joblib"
BATTER_MODEL_PATH  = _CACHE_DIR / "props_model_batter.joblib"

# Per-classifier picks history (mirrors xgb / lr / nn picks history
# in /api/admin/reset/model_record so the new sinks plug into the
# existing reset machinery).
PITCHER_HISTORY_PATH = _CACHE_DIR / "props_pitcher_picks_history.json"
BATTER_HISTORY_PATH  = _CACHE_DIR / "props_batter_picks_history.json"


def _log(msg: str) -> None:
    print(f"PROPS-MODEL: {msg}", flush=True, file=sys.stderr)


def _log_settle(msg: str) -> None:
    print(f"PROPS-SETTLE: {msg}", flush=True, file=sys.stderr)


# ── American odds -> implied probability (no-vig) ───────────────────────────

def _american_to_prob(american) -> float:
    """+150 -> 0.40, -110 -> ~0.524.  No-vig adjustment is left to the
    caller because over/under pairs let us de-vig in pairs more
    accurately than per-side."""
    try:
        v = int(american)
    except (TypeError, ValueError):
        return 0.5
    if v > 0:
        return 100.0 / (v + 100.0)
    return abs(v) / (abs(v) + 100.0)


def _no_vig_pair(over_odds, under_odds) -> tuple[float, float]:
    """Return (over_prob, under_prob) with the bookmaker juice removed.
    Defaults to (0.5, 0.5) when either side is missing."""
    if over_odds is None or under_odds is None:
        return 0.5, 0.5
    op = _american_to_prob(over_odds)
    up = _american_to_prob(under_odds)
    total = op + up
    if total <= 0:
        return 0.5, 0.5
    return op / total, up / total


# ── Joblib + Supabase sync helpers ──────────────────────────────────────────

class _LoadedModel:
    """Lazy joblib loader so an import-time read can't crash the
    process if scikit-learn / xgboost aren't available yet."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded: object | None = None
        self._tried: bool = False

    def load(self) -> object | None:
        if self._tried:
            return self._loaded
        self._tried = True
        if not self.path.exists():
            _log(f"joblib {self.path} not on disk -- predictor will use heuristic")
            return None
        try:
            import joblib  # type: ignore
            self._loaded = joblib.load(self.path)
            _log(f"joblib loaded {self.path}")
        except Exception as exc:                                          # noqa: BLE001
            _log(f"joblib load failed for {self.path}: {exc}")
            self._loaded = None
        return self._loaded


_pitcher_model = _LoadedModel(PITCHER_MODEL_PATH)
_batter_model  = _LoadedModel(BATTER_MODEL_PATH)


def restore_models_from_supabase() -> dict:
    """Mirror of the existing model joblib restore (see app.py boot
    flow).  Pulls the two props joblibs from Supabase app_cache when
    the local files are missing.  Idempotent.

    Returns a small status dict the boot health report can include.
    """
    out: dict = {}
    try:
        from . import db as _db
        if not _db.is_supabase():
            _log("Supabase off -- skipping joblib restore")
            return {"supabase": False}
    except Exception:                                                     # noqa: BLE001
        return {"supabase": False}

    pairs = (
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    )
    for key, path in pairs:
        if path.exists():
            out[key] = "local"
            continue
        try:
            from . import db as _db
            row = _db.cache_get(key)
            if not isinstance(row, dict):
                out[key] = "missing"
                _log(f"restore {key}: no Supabase row -- predictor will use heuristic")
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            # Supabase stores the joblib bytes base64-encoded in
            # data["b64"] (same convention src.model uses).  Decode
            # and write to disk.
            import base64
            b64 = (data or {}).get("b64")
            if not b64:
                out[key] = "no_b64"
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(b64))
            out[key] = "restored"
            _log(f"restore {key}: wrote {path} ({path.stat().st_size} bytes)")
        except Exception as exc:                                          # noqa: BLE001
            out[key] = f"error: {exc}"
            _log(f"restore {key} failed: {exc}")
    return out


def push_models_to_supabase() -> dict:
    """Counterpart of restore_models_from_supabase.  Called by the
    training script after a fresh joblib is written so the next worker
    boot can pull it down."""
    out: dict = {}
    try:
        from . import db as _db
        if not _db.is_supabase():
            return {"supabase": False}
    except Exception:                                                     # noqa: BLE001
        return {"supabase": False}

    import base64
    pairs = (
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    )
    for key, path in pairs:
        if not path.exists():
            out[key] = "no_local_file"
            continue
        try:
            from . import db as _db
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            _db.cache_set(key, None, "models", {"b64": b64})
            out[key] = "pushed"
            _log(f"push {key}: uploaded {path} ({path.stat().st_size} bytes)")
        except Exception as exc:                                          # noqa: BLE001
            out[key] = f"error: {exc}"
            _log(f"push {key} failed: {exc}")
    return out


# ── Prediction API ──────────────────────────────────────────────────────────

def _bucket_for_market(market: str) -> str:
    return "pitcher" if (market or "").startswith("pitcher_") else "batter"


def _feature_vector_for_prop(prop: dict) -> list[float]:
    """Minimal feature vector used by the joblib classifiers.  Picks up
    only the fields available in the prop payload (line, odds, no-vig
    market prob).  When a richer feature set is wanted, extend this
    function AND the training script's matching builder."""
    line = float(prop.get("line") or 0.0)
    odds = float(prop.get("best_odds") or -110)
    market_prob = _american_to_prob(odds)
    # All_books spread = best - worst odds.  Wider spreads mean more
    # disagreement among books = noisier line.
    book_odds = [
        float(b.get("odds"))
        for b in (prop.get("all_books") or [])
        if isinstance(b.get("odds"), (int, float))
    ]
    spread = (max(book_odds) - min(book_odds)) if book_odds else 0.0
    return [line, odds, market_prob, spread, float(len(book_odds))]


def predict(prop: dict) -> dict:
    """Return the model's call for a single prop.

    Output shape:
        {
          recommendation: "Over" | "Under" | "Pass",
          confidence:    float,    # 0..1
          model_prob:    float,    # raw P(Over)
          market_prob:   float,    # de-vigged P(Over) from the line
          edge:          float,    # model_prob - market_prob, signed
          source:        "joblib"  | "heuristic",
        }

    Heuristic fallback (when joblib missing):  market_prob is used
    directly, so recommendation = Over iff market_prob >= 0.5, with a
    confidence floor at 0.50 so the UI never shows "100% confidence
    based on no data".
    """
    bucket = _bucket_for_market(prop.get("market", ""))
    model = (_pitcher_model if bucket == "pitcher" else _batter_model).load()

    # Pair the over/under for no-vig market prob when both sides came
    # back in the same payload.  Caller passes the over row; under
    # picks up the inverse below.
    over_prob = market_prob = _american_to_prob(prop.get("best_odds"))

    if model is not None:
        try:
            import numpy as np  # noqa: PLC0415
            X = np.array([_feature_vector_for_prop(prop)], dtype=float)
            # Most sklearn / xgb classifiers expose predict_proba.
            # Fall back to predict() returning {0,1}.
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba(X)[0]
                # Assume class 1 = Over per train script convention.
                over_prob = float(proba[1]) if len(proba) > 1 else float(proba[0])
            else:
                over_prob = float(model.predict(X)[0])
            source = "joblib"
        except Exception as exc:                                          # noqa: BLE001
            _log(f"joblib predict failed for {bucket}: {exc} -- heuristic")
            source = "heuristic"
    else:
        source = "heuristic"

    # If the caller passed the Under side, flip the model output.
    side = (prop.get("side") or "Over").strip().title()
    if side == "Under":
        over_prob = 1.0 - over_prob
        market_prob = 1.0 - market_prob

    edge = over_prob - market_prob
    # Recommend Over when model_prob beats market_prob materially,
    # Under for the inverse, otherwise Pass.  Threshold 3% mirrors the
    # EV Scan default on the home page.
    if   over_prob - market_prob >  0.03: recommendation = "Over"
    elif over_prob - market_prob < -0.03: recommendation = "Under"
    else:                                  recommendation = "Pass"
    # Confidence = how far the model is from the market.  Multiplied
    # by 2 so a 50% model on a 35% market = 30% confidence (sane scale).
    confidence = min(0.99, max(0.50, abs(over_prob - market_prob) * 2.0 + 0.50))
    return {
        "recommendation": recommendation,
        "confidence":     round(confidence, 4),
        "model_prob":     round(over_prob, 4),
        "market_prob":    round(market_prob, 4),
        "edge":           round(edge, 4),
        "source":         source,
    }


# ── Record tracking ─────────────────────────────────────────────────────────

def _read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("picks") or []
    except Exception:                                                     # noqa: BLE001
        return []


def get_record(bucket: str) -> dict:
    """Return {wins, losses, pct, total} for the pitcher or batter
    props model.  Reads the local per-bucket picks history file.
    """
    path = PITCHER_HISTORY_PATH if bucket == "pitcher" else BATTER_HISTORY_PATH
    rows = _read_history(path)
    wins = sum(1 for r in rows if (r.get("result") or "").lower() == "win")
    losses = sum(1 for r in rows if (r.get("result") or "").lower() == "loss")
    total = wins + losses
    pct = (wins / total) if total else None
    return {"wins": wins, "losses": losses, "total": total, "pct": pct}
