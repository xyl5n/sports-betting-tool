"""
props_model.py
==============
Per-market prediction model for MLB player props.

Two classifier artifacts are loaded at module level:
  .cache/props_model_pitcher.joblib   (xgb classifier for pitcher markets)
  .cache/props_model_batter.joblib    (xgb classifier for batter markets)

Alongside those, per-stat XGBRegressor models produce a numeric
predicted value:
  .cache/props_model_pitcher_reg_{stat}.joblib  (K, ER, H, BB, outs)
  .cache/props_model_batter_reg_{stat}.joblib   (H, TB, HR, RBI, R, BB)

Feature names for the regression inference vector are loaded lazily
from .cache/props_reg_metadata.json (written by the training script).

Both classifiers are restored from Supabase on cold boot (the same
pattern src.model uses for the moneyline / run-line / totals joblibs).
When no trained artifact is available the predictor falls back to a
market-neutral heuristic that uses the prop line's implied probability
so the UI always renders something rather than blanking out.

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

# Classifier artifact paths.
PITCHER_MODEL_PATH = _CACHE_DIR / "props_model_pitcher.joblib"
BATTER_MODEL_PATH  = _CACHE_DIR / "props_model_batter.joblib"

# Per-stat XGBRegressor paths (trained alongside the classifiers).
_PITCHER_REG_STATS = ("K", "ER", "H", "BB", "outs")
_BATTER_REG_STATS  = ("H", "TB", "HR", "RBI", "R", "BB")

_PITCHER_REG_PATHS: dict[str, Path] = {
    s: _CACHE_DIR / f"props_model_pitcher_reg_{s}.joblib" for s in _PITCHER_REG_STATS
}
_BATTER_REG_PATHS: dict[str, Path] = {
    s: _CACHE_DIR / f"props_model_batter_reg_{s}.joblib" for s in _BATTER_REG_STATS
}

# Which regression stat does each prop market target?
# Value: (bucket, stat_key) — None when no regressor is available.
_MARKET_REG_KEY: dict[str, tuple[str, str]] = {
    "pitcher_strikeouts":   ("pitcher", "K"),
    "pitcher_earned_runs":  ("pitcher", "ER"),
    "pitcher_hits_allowed": ("pitcher", "H"),
    "pitcher_walks":        ("pitcher", "BB"),
    "pitcher_outs":         ("pitcher", "outs"),
    "batter_hits":          ("batter",  "H"),
    "batter_total_bases":   ("batter",  "TB"),
    "batter_home_runs":     ("batter",  "HR"),
    "batter_rbis":          ("batter",  "RBI"),
    "batter_runs_scored":   ("batter",  "R"),
    "batter_walks":         ("batter",  "BB"),
}

# Feature-name metadata written by the training script.
_REG_META_PATH = _CACHE_DIR / "props_reg_metadata.json"

# Per-classifier picks history (mirrors xgb / lr / nn picks history
# in /api/admin/reset/model_record so the new sinks plug into the
# existing reset machinery).
PITCHER_HISTORY_PATH = _CACHE_DIR / "props_pitcher_picks_history.json"
BATTER_HISTORY_PATH  = _CACHE_DIR / "props_batter_picks_history.json"

# Park factor tables (same values as train_props_models.py — kept in
# sync manually; a mismatch shifts predictions slightly but not
# catastrophically because park factors are small continuous signals).
_PARK_K: dict[str, float] = {
    "ARI": 0.96, "ATL": 0.99, "BAL": 1.00, "BOS": 0.96, "CHC": 0.97,
    "CIN": 0.96, "CLE": 1.01, "COL": 0.88, "CWS": 0.99, "DET": 1.02,
    "HOU": 1.00, "KC":  1.02, "LAA": 1.01, "LAD": 1.04, "MIA": 1.00,
    "MIL": 1.00, "MIN": 1.00, "NYM": 1.03, "NYY": 0.94, "OAK": 1.04,
    "PHI": 0.95, "PIT": 1.04, "SD":  1.05, "SEA": 1.04, "SF":  1.05,
    "STL": 1.03, "TB":  1.03, "TEX": 0.97, "TOR": 0.99, "WSH": 1.00,
}
_PARK_H: dict[str, float] = {
    "ARI": 0.97, "ATL": 0.99, "BAL": 1.01, "BOS": 1.08, "CHC": 1.05,
    "CIN": 1.08, "CLE": 0.99, "COL": 1.25, "CWS": 1.00, "DET": 0.97,
    "HOU": 0.99, "KC":  0.97, "LAA": 1.01, "LAD": 0.93, "MIA": 0.98,
    "MIL": 1.00, "MIN": 1.00, "NYM": 0.95, "NYY": 1.05, "OAK": 0.96,
    "PHI": 1.07, "PIT": 0.95, "SD":  0.90, "SEA": 0.93, "SF":  0.89,
    "STL": 0.95, "TB":  0.97, "TEX": 1.02, "TOR": 0.99, "WSH": 1.00,
}
_PARK_HR: dict[str, float] = {
    "ARI": 1.05, "ATL": 1.08, "BAL": 1.10, "BOS": 1.12, "CHC": 1.08,
    "CIN": 1.35, "CLE": 0.95, "COL": 1.26, "CWS": 1.15, "DET": 0.85,
    "HOU": 0.95, "KC":  0.88, "LAA": 1.05, "LAD": 0.87, "MIA": 0.85,
    "MIL": 1.05, "MIN": 1.10, "NYM": 0.93, "NYY": 1.40, "OAK": 0.80,
    "PHI": 1.30, "PIT": 0.82, "SD":  0.72, "SEA": 0.82, "SF":  0.60,
    "STL": 0.90, "TB":  0.90, "TEX": 1.10, "TOR": 1.05, "WSH": 0.95,
}


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

# Regression model lazy-loaders keyed by stat name.
_pitcher_reg_models: dict[str, _LoadedModel] = {
    s: _LoadedModel(p) for s, p in _PITCHER_REG_PATHS.items()
}
_batter_reg_models: dict[str, _LoadedModel] = {
    s: _LoadedModel(p) for s, p in _BATTER_REG_PATHS.items()
}

# ── Regression metadata (feature names) ─────────────────────────────────────

_reg_meta_cache: Optional[dict] = None


def _load_reg_meta() -> dict:
    """Lazily load props_reg_metadata.json once per process."""
    global _reg_meta_cache
    if _reg_meta_cache is not None:
        return _reg_meta_cache
    if not _REG_META_PATH.exists():
        _reg_meta_cache = {}
        return {}
    try:
        _reg_meta_cache = json.loads(_REG_META_PATH.read_text(encoding="utf-8"))
        _log(f"reg metadata loaded: {len(_reg_meta_cache.get('pitcher_feature_names', []))} "
             f"pitcher feats, {len(_reg_meta_cache.get('batter_feature_names', []))} batter feats")
    except Exception as exc:                                               # noqa: BLE001
        _log(f"reg metadata load failed: {exc}")
        _reg_meta_cache = {}
    return _reg_meta_cache


def _build_reg_vector(prop: dict, bucket: str) -> tuple[Optional[list[float]], Optional[list[str]]]:
    """Build a feature vector for a regression model inference call.

    Uses the feature names saved at training time (props_reg_metadata.json)
    to construct a zero vector of the correct length, then fills in the fields
    available from the prop payload.  Missing fields stay zero — the same
    convention used during training for inference-time placeholder features.

    Returns (vector, feature_names) or (None, None) when metadata unavailable.
    """
    meta = _load_reg_meta()
    fn_key = f"{bucket}_feature_names"
    feature_names: Optional[list[str]] = meta.get(fn_key)
    if not feature_names:
        return None, None

    fn_idx = {name: i for i, name in enumerate(feature_names)}
    vec    = [0.0] * len(feature_names)

    line = float(prop.get("line") or 0.0)

    # Use prop line as a proxy for season-to-date and rolling averages.
    # This is the best available signal at inference time: the sportsbook
    # line itself reflects the expected per-game stat total.
    for fname in feature_names:
        if fname.startswith(("szn_", "r7_", "r14_")):
            idx = fn_idx.get(fname)
            if idx is not None:
                vec[idx] = line

    # is_home_i — whether this player is at home
    is_home = bool(prop.get("is_home"))
    if "is_home_i" in fn_idx:
        vec[fn_idx["is_home_i"]] = float(is_home)

    # Park team = always the home team's stadium, regardless of which side
    # the player is on.
    park_team = (prop.get("home_team") or "").strip().upper()[:3]

    if "ballpark_factor_k" in fn_idx:
        vec[fn_idx["ballpark_factor_k"]] = _PARK_K.get(park_team, 1.0)
    if "ballpark_factor_hits" in fn_idx:
        vec[fn_idx["ballpark_factor_hits"]] = _PARK_H.get(park_team, 1.0)
    if "ballpark_factor_hr" in fn_idx:
        vec[fn_idx["ballpark_factor_hr"]] = _PARK_HR.get(park_team, 1.0)

    # days_since_last_start: neutral default (~5 days rest for starters)
    if "days_since_last_start" in fn_idx:
        vec[fn_idx["days_since_last_start"]] = 5.0

    return vec, feature_names


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

    all_pairs = [
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    ] + [
        (f"props_model_pitcher_reg_{s}", p)
        for s, p in _PITCHER_REG_PATHS.items()
    ] + [
        (f"props_model_batter_reg_{s}", p)
        for s, p in _BATTER_REG_PATHS.items()
    ]
    for key, path in all_pairs:
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
    all_pairs = [
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    ] + [
        (f"props_model_pitcher_reg_{s}", p)
        for s, p in _PITCHER_REG_PATHS.items()
    ] + [
        (f"props_model_batter_reg_{s}", p)
        for s, p in _BATTER_REG_PATHS.items()
    ]
    for key, path in all_pairs:
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
          recommendation:  "Over" | "Under" | "Pass",
          confidence:      float,          # 0..1
          model_prob:      float,          # raw P(Over)
          market_prob:     float,          # de-vigged P(Over) from the line
          edge:            float,          # model_prob - market_prob, signed
          source:          "joblib" | "heuristic",
          predicted_value: float | None,   # numeric stat prediction (regressor)
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

    # ── Regression: predicted numeric stat value ─────────────────────────
    predicted_value: Optional[float] = None
    reg_info = _MARKET_REG_KEY.get(prop.get("market", ""))
    if reg_info is not None:
        reg_bucket, reg_stat = reg_info
        reg_loaders = _pitcher_reg_models if reg_bucket == "pitcher" else _batter_reg_models
        reg_model = reg_loaders.get(reg_stat, _LoadedModel(Path("_nonexistent_"))).load()
        if reg_model is not None:
            try:
                import numpy as np  # noqa: PLC0415
                vec, _ = _build_reg_vector(prop, reg_bucket)
                if vec is not None:
                    X_reg = np.array([vec], dtype=float)
                    predicted_value = round(float(reg_model.predict(X_reg)[0]), 2)
            except Exception as exc:                                      # noqa: BLE001
                _log(f"regression predict failed for {prop.get('market')}: {exc}")

    return {
        "recommendation":  recommendation,
        "confidence":      round(confidence, 4),
        "model_prob":      round(over_prob, 4),
        "market_prob":     round(market_prob, 4),
        "edge":            round(edge, 4),
        "source":          source,
        "predicted_value": predicted_value,
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
