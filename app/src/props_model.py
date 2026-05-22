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


# ── Hardcoded feature name lists (must match train_props_models.py) ──────────
# These are duplicated here so inference never requires props_reg_metadata.json
# to be present on disk (the file is lost on every Railway redeploy).

_P_ROLL = ["K", "BB", "H", "ER", "IP", "k_per_9", "bb_per_9"]
_B_ROLL = [
    "H", "HR", "RBI", "R", "BB", "SO", "TB", "AB", "PA",
    "H_per_AB", "TB_per_AB", "HR_per_AB", "BB_per_PA", "SO_per_PA",
]

_PITCHER_FEATURE_NAMES: list[str] = (
    [f"szn_{c}" for c in _P_ROLL]   # 7
    + [f"r7_{c}"  for c in _P_ROLL] # 7
    + [f"r14_{c}" for c in _P_ROLL] # 7
    + ["is_home_i", "days_since_last_start", "ip_last_30d", "ballpark_factor_k"]  # 4
    + ["era_vs_lhb", "k_rate_vs_lhb", "era_vs_rhb", "k_rate_vs_rhb"]             # 4
    + [
        "lineup_avg_k_rate", "lineup_lhb_count", "lineup_rhb_count",
        "weather_temp", "weather_wind_speed", "weather_wind_dir_num",
        "time_of_day", "umpire_k_rate", "implied_total",
        "first_inning_k_pct", "pitch_mix_fastball_pct",
        "pitch_mix_breaking_pct", "pitch_mix_offspeed_pct",
    ]  # 13
)  # 7+7+7+4+4+13 = 42

_BATTER_FEATURE_NAMES: list[str] = (
    [f"szn_{c}" for c in _B_ROLL]   # 14
    + [f"r7_{c}"  for c in _B_ROLL] # 14
    + [f"r14_{c}" for c in _B_ROLL] # 14
    + [
        "k_pct_7d", "k_pct_14d", "babip_7d", "babip_14d",
        "batting_order", "is_home_i", "ballpark_factor_hits", "ballpark_factor_hr",
    ]  # 8
    + ["ops_vs_lhp", "obp_vs_lhp", "slg_vs_lhp", "ops_vs_rhp", "obp_vs_rhp", "slg_vs_rhp"]  # 6
    + [
        "whiff_pct", "chase_pct", "hard_hit_rate", "barrel_rate", "sprint_speed",
        "platoon_matchup_flag", "weather_temp", "weather_wind_speed", "time_of_day",
        "ba_vs_breaking", "ba_vs_fastball", "ba_vs_offspeed",
        "h2h_career_ab", "h2h_career_avg", "h2h_career_k_rate", "implied_total",
    ]  # 16
)  # 14+14+14+8+6+16 = 72

# Neutral inference-time defaults for features that require live data
# (lineup, weather, umpire stats, etc.).  These match league-average
# values so missing context shifts predictions minimally.
_PITCHER_DEFAULTS: dict[str, float] = {
    "ip_last_30d":            30.0,
    "days_since_last_start":   5.0,
    "era_vs_lhb":              4.50,
    "k_rate_vs_lhb":           0.215,
    "era_vs_rhb":              4.50,
    "k_rate_vs_rhb":           0.215,
    "lineup_avg_k_rate":       0.220,
    "lineup_lhb_count":        4.0,
    "lineup_rhb_count":        5.0,
    "weather_temp":           72.0,
    "weather_wind_speed":      8.0,
    "weather_wind_dir_num":    0.0,
    "time_of_day":             1.0,  # 1 = day, 0 = night (league avg ~0.25 day)
    "umpire_k_rate":           0.215,
    "implied_total":           8.5,
    "first_inning_k_pct":      0.210,
    "pitch_mix_fastball_pct":  0.55,
    "pitch_mix_breaking_pct":  0.25,
    "pitch_mix_offspeed_pct":  0.20,
}

_BATTER_DEFAULTS: dict[str, float] = {
    "k_pct_7d":            0.230,
    "k_pct_14d":           0.230,
    "babip_7d":            0.295,
    "babip_14d":           0.295,
    "batting_order":       5.0,
    "ops_vs_lhp":          0.720,
    "obp_vs_lhp":          0.315,
    "slg_vs_lhp":          0.405,
    "ops_vs_rhp":          0.720,
    "obp_vs_rhp":          0.315,
    "slg_vs_rhp":          0.405,
    "whiff_pct":           0.245,
    "chase_pct":           0.295,
    "hard_hit_rate":       0.370,
    "barrel_rate":         0.075,
    "sprint_speed":       27.0,
    "platoon_matchup_flag": 0.0,
    "weather_temp":        72.0,
    "weather_wind_speed":   8.0,
    "time_of_day":          1.0,
    "ba_vs_breaking":      0.235,
    "ba_vs_fastball":      0.265,
    "ba_vs_offspeed":      0.255,
    "h2h_career_ab":       12.0,
    "h2h_career_avg":       0.255,
    "h2h_career_k_rate":    0.220,
    "implied_total":        8.5,
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


def _build_reg_vector(prop: dict, bucket: str) -> tuple[list[float], list[str]]:
    """Build a full-length feature vector for a regression or classifier call.

    Preference order for feature names:
      1. props_reg_metadata.json saved at training time (same process-lifetime
         cache as before)
      2. Hardcoded _PITCHER_FEATURE_NAMES / _BATTER_FEATURE_NAMES constants
         (always available, never requires a file on disk)

    The vector is constructed as follows:
      - Rolling/season stats (szn_*, r7_*, r14_*): use the prop line as a
        proxy — the sportsbook line encodes the expected per-game total.
      - Park factors: looked up from the hardcoded tables above.
      - is_home_i: from prop payload.
      - All other features: filled from _PITCHER_DEFAULTS / _BATTER_DEFAULTS
        (league-average neutrals) so missing context minimally shifts predictions.

    Always returns (vector, feature_names) — never (None, None).
    """
    # Prefer metadata from training file; fall back to hardcoded constants.
    meta = _load_reg_meta()
    fn_key = f"{bucket}_feature_names"
    feature_names: list[str] = (
        meta.get(fn_key)
        or (_PITCHER_FEATURE_NAMES if bucket == "pitcher" else _BATTER_FEATURE_NAMES)
    )

    defaults = _PITCHER_DEFAULTS if bucket == "pitcher" else _BATTER_DEFAULTS

    fn_idx = {name: i for i, name in enumerate(feature_names)}
    vec    = [0.0] * len(feature_names)

    line = float(prop.get("line") or 0.0)

    # Rolling/season averages — use line as best available proxy.
    for fname in feature_names:
        if fname.startswith(("szn_", "r7_", "r14_")):
            idx = fn_idx.get(fname)
            if idx is not None:
                vec[idx] = line

    # is_home_i
    is_home = bool(prop.get("is_home"))
    if "is_home_i" in fn_idx:
        vec[fn_idx["is_home_i"]] = float(is_home)

    # Park factors — home_team drives the ballpark.
    park_team = (prop.get("home_team") or "").strip().upper()[:3]
    if "ballpark_factor_k" in fn_idx:
        vec[fn_idx["ballpark_factor_k"]] = _PARK_K.get(park_team, 1.0)
    if "ballpark_factor_hits" in fn_idx:
        vec[fn_idx["ballpark_factor_hits"]] = _PARK_H.get(park_team, 1.0)
    if "ballpark_factor_hr" in fn_idx:
        vec[fn_idx["ballpark_factor_hr"]] = _PARK_HR.get(park_team, 1.0)

    # Fill all other features from league-average defaults.
    for fname, default_val in defaults.items():
        idx = fn_idx.get(fname)
        if idx is not None and vec[idx] == 0.0:
            vec[idx] = default_val

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

    import base64

    # Restore joblib model files (base64-encoded bytes in data["b64"]).
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
            # Supabase stores the joblib bytes base64-encoded in data["b64"].
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

    # Restore props_reg_metadata.json (stored as JSON in data["json"]).
    if not _REG_META_PATH.exists():
        try:
            from . import db as _db
            row = _db.cache_get("props_reg_metadata")
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                raw_json = (data or {}).get("json")
                if raw_json:
                    _REG_META_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _REG_META_PATH.write_text(
                        raw_json if isinstance(raw_json, str) else json.dumps(raw_json),
                        encoding="utf-8",
                    )
                    out["props_reg_metadata"] = "restored"
                    _log(f"restore props_reg_metadata: wrote {_REG_META_PATH}")
                else:
                    out["props_reg_metadata"] = "no_json"
            else:
                out["props_reg_metadata"] = "missing"
        except Exception as exc:                                          # noqa: BLE001
            out["props_reg_metadata"] = f"error: {exc}"
            _log(f"restore props_reg_metadata failed: {exc}")

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

    # Push props_reg_metadata.json so cold boots can restore it.
    if _REG_META_PATH.exists():
        try:
            from . import db as _db
            raw = _REG_META_PATH.read_text(encoding="utf-8")
            _db.cache_set("props_reg_metadata", None, "models", {"json": raw})
            out["props_reg_metadata"] = "pushed"
            _log(f"push props_reg_metadata: uploaded {_REG_META_PATH}")
        except Exception as exc:                                          # noqa: BLE001
            out["props_reg_metadata"] = f"error: {exc}"
            _log(f"push props_reg_metadata failed: {exc}")

    return out


# ── Prediction API ──────────────────────────────────────────────────────────

# Probability bounds applied to every raw classifier output.
# XGBoost is systematically overconfident — it pushes P(Over) toward 0/1
# far beyond what the empirical frequency justifies.
# CalibratedClassifierCV (isotonic, baked into retrained artifacts) corrects
# this natively; these bounds are a belt-and-suspenders guard that stays
# effective before a retrained artifact is available.
#
# Critical invariant: squashing is applied to P(Over) BEFORE any side-flip
# so that P(Over) + P(Under) = 1.0 is preserved exactly.
_PROB_LO: float = 0.10   # model can never be more than 90% against
_PROB_HI: float = 0.90   # model can never be more than 90% for
_CONF_CAP: float = 0.85  # maximum confidence displayed to the user


def _squash_prob(p: float) -> float:
    """Clamp a raw classifier probability to [_PROB_LO, _PROB_HI]."""
    return max(_PROB_LO, min(_PROB_HI, p))


def _bucket_for_market(market: str) -> str:
    return "pitcher" if (market or "").startswith("pitcher_") else "batter"


def _run_regressor(prop: dict, bucket: str) -> "Optional[float]":
    """Run the per-stat regression model and return a predicted numeric value,
    or None when no regressor is available for this market."""
    predicted_value: Optional[float] = None
    reg_info = _MARKET_REG_KEY.get(prop.get("market", ""))
    if reg_info is None:
        return None
    reg_bucket, reg_stat = reg_info
    reg_loaders = _pitcher_reg_models if reg_bucket == "pitcher" else _batter_reg_models
    reg_model = reg_loaders.get(reg_stat, _LoadedModel(Path("_nonexistent_"))).load()
    if reg_model is None:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        vec, _ = _build_reg_vector(prop, reg_bucket)
        X_reg = np.array([vec], dtype=float)
        predicted_value = round(float(reg_model.predict(X_reg)[0]), 2)
    except Exception as exc:                                                # noqa: BLE001
        _log(f"regression predict failed for {prop.get('market')}: {exc}")
    return predicted_value


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
          confidence:      float,          # 0..1, capped at _CONF_CAP (0.85)
          model_prob:      float,          # calibrated P(this side)
          market_prob:     float,          # de-vigged P(this side) from the line
          edge:            float,          # model_prob - market_prob, signed
          source:          "joblib" | "heuristic",
          predicted_value: float | None,   # numeric stat prediction (regressor)
        }

    Probability calibration
    -----------------------
    Raw XGBoost probabilities are squashed to [_PROB_LO, _PROB_HI] = [0.10, 0.90]
    BEFORE any side flip.  This caps displayed confidence at _CONF_CAP (0.85)
    and is belt-and-suspenders for the CalibratedClassifierCV wrapper baked
    into retrained artifacts (see train_props_models.py).

    Side symmetry
    -------------
    The squash is applied to P(Over) before flipping for the Under side, so
    predict(over_prop).model_prob + predict(under_prop).model_prob == 1.0
    exactly when both props share the same underlying line.  Use predict_pair()
    to make this invariant explicit and avoid a second model call.
    """
    bucket = _bucket_for_market(prop.get("market", ""))
    model = (_pitcher_model if bucket == "pitcher" else _batter_model).load()

    over_prob = market_prob = _american_to_prob(prop.get("best_odds"))

    if model is not None:
        try:
            import numpy as np  # noqa: PLC0415
            vec, _ = _build_reg_vector(prop, bucket)
            X = np.array([vec], dtype=float)
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba(X)[0]
                raw_p = float(proba[1]) if len(proba) > 1 else float(proba[0])
            else:
                raw_p = float(model.predict(X)[0])
            # Squash BEFORE side flip so Over + Under still sum to 1.0.
            over_prob = _squash_prob(raw_p)
            source = "joblib"
        except Exception as exc:                                          # noqa: BLE001
            _log(f"joblib predict failed for {bucket}: {exc} -- heuristic")
            source = "heuristic"
    else:
        source = "heuristic"

    # Flip for the Under side AFTER squashing.
    side = (prop.get("side") or "Over").strip().title()
    if side == "Under":
        over_prob   = 1.0 - over_prob
        market_prob = 1.0 - market_prob

    edge = over_prob - market_prob
    if   edge >  0.03: recommendation = "Over"
    elif edge < -0.03: recommendation = "Under"
    else:               recommendation = "Pass"
    confidence = min(_CONF_CAP, max(0.50, abs(edge) * 2.0 + 0.50))

    return {
        "recommendation":  recommendation,
        "confidence":      round(confidence, 4),
        "model_prob":      round(over_prob, 4),
        "market_prob":     round(market_prob, 4),
        "edge":            round(edge, 4),
        "source":          source,
        "predicted_value": _run_regressor(prop, bucket),
    }


def predict_pair(over_prop: dict, under_prop: dict) -> tuple[dict, dict]:
    """Score both sides of a prop with a single model call.

    Guarantees over_result["model_prob"] + under_result["model_prob"] == 1.0
    exactly — the Under result is derived by complementing the Over probability,
    not by an independent model call.

    Market probabilities are de-vigged per-side using each prop's own
    best_odds so the market_prob pair also sums to 1.0 correctly.

    Use this instead of two separate predict() calls whenever both sides of
    the same line are available.
    """
    bucket = _bucket_for_market(
        over_prop.get("market") or under_prop.get("market") or ""
    )
    model = (_pitcher_model if bucket == "pitcher" else _batter_model).load()

    # ── Model: single call, P(Over) direction ────────────────────────────
    raw_over_prob = _american_to_prob(over_prop.get("best_odds"))
    source = "heuristic"
    if model is not None:
        try:
            import numpy as np  # noqa: PLC0415
            vec, _ = _build_reg_vector(over_prop, bucket)
            X = np.array([vec], dtype=float)
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba(X)[0]
                raw = float(proba[1]) if len(proba) > 1 else float(proba[0])
            else:
                raw = float(model.predict(X)[0])
            raw_over_prob = _squash_prob(raw)
            source = "joblib"
        except Exception as exc:                                          # noqa: BLE001
            _log(f"predict_pair joblib failed for {bucket}: {exc} -- heuristic")

    # ── Market: no-vig per side ───────────────────────────────────────────
    mkt_over_raw  = _american_to_prob(over_prop.get("best_odds"))
    mkt_under_raw = _american_to_prob(under_prop.get("best_odds"))
    total_mkt = mkt_over_raw + mkt_under_raw
    if total_mkt > 0:
        mkt_over  = mkt_over_raw  / total_mkt
        mkt_under = mkt_under_raw / total_mkt
    else:
        mkt_over  = 0.5
        mkt_under = 0.5

    # ── Under is the exact complement ────────────────────────────────────
    raw_under_prob = 1.0 - raw_over_prob

    # ── Regression (shared; keyed off the Over prop) ─────────────────────
    predicted_value = _run_regressor(over_prop, bucket)

    def _make(model_p: float, market_p: float) -> dict:
        edge = model_p - market_p
        if   edge >  0.03: rec = "Over"
        elif edge < -0.03: rec = "Under"
        else:               rec = "Pass"
        conf = min(_CONF_CAP, max(0.50, abs(edge) * 2.0 + 0.50))
        return {
            "recommendation":  rec,
            "confidence":      round(conf, 4),
            "model_prob":      round(model_p, 4),
            "market_prob":     round(market_p, 4),
            "edge":            round(edge, 4),
            "source":          source,
            "predicted_value": predicted_value,
        }

    return _make(raw_over_prob, mkt_over), _make(raw_under_prob, mkt_under)


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
