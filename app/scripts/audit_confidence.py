"""
audit_confidence.py
===================
Diagnostic-only audit for the "all picks 100% confidence" symptom.

This script makes NO changes to prod scoring code.  It loads the
existing artifacts and prints a structured report covering the five
checks from the audit brief:

  (1) What class is saved in props_model_{pitcher,batter}.joblib?
      Print type and __class__.__name__.
  (2) If calibrated, report method / cv / calibrator count.
  (3) predict_proba() on 20 real props from today's scored cache --
      RAW probabilities before any squashing.
  (4) Grep src/props_model.py for any leftover squash / cap code.
  (5) Feature-vector mean / std / min / max for 10 pitcher + 10
      batter picks -- flag any feature whose value range hints at
      a scaling-mismatch outlier.

Run via:
  python -m scripts.audit_confidence
  or
  POST /api/admin/audit/confidence  (admin button in pages/admin.py)

Output goes to stderr -- on Railway, ``railway logs`` captures it.
"""
from __future__ import annotations

import math
import os
import re
import sys
from pathlib import Path
from typing import Optional


# ── Path bootstrap so ``from src.* import ...`` works whether the script
#    is invoked from the project root, from inside ``app/``, or via the
#    admin route's in-process call.
_APP_DIR = Path(__file__).resolve().parent.parent     # app/
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))


# ── Tiny structured-log helpers ─────────────────────────────────────────────

def _emit(msg: str = "") -> None:
    """Write a line to stderr with a tagged prefix so Railway logs can
    grep ``CONF-AUDIT`` to isolate the report."""
    print(f"CONF-AUDIT: {msg}", flush=True, file=sys.stderr)


def _section(title: str) -> None:
    _emit("")
    _emit("=" * 78)
    _emit(title)
    _emit("=" * 78)


# ── Check 1 + 2: Joblib types + calibration parameters ─────────────────────

def _check_models() -> None:
    _section("CHECK 1+2: joblib types + calibration parameters")
    try:
        import joblib  # type: ignore
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"joblib import failed: {exc}")
        return

    # Best-effort: restore from Supabase first so an ephemeral Railway
    # redeploy with a cold ``.cache/`` doesn't make the script no-op.
    try:
        from src.props_model import restore_models_from_supabase
        restore_models_from_supabase()
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"NOTE: supabase model restore failed: {exc}")

    paths = {
        "pitcher": Path(".cache/props_model_pitcher.joblib"),
        "batter":  Path(".cache/props_model_batter.joblib"),
    }
    for bucket, path in paths.items():
        _emit("")
        _emit(f"[{bucket}]  path={path}")
        if not path.exists():
            _emit(f"  >>> joblib NOT FOUND on disk")
            continue
        size_kb = path.stat().st_size // 1024
        _emit(f"  size: {size_kb} KB")
        try:
            model = joblib.load(path)
        except Exception as exc:                                          # noqa: BLE001
            _emit(f"  >>> joblib load FAILED: {exc}")
            continue
        cls_full = type(model)
        cls_name = cls_full.__name__
        _emit(f"  type:  {cls_full}")
        _emit(f"  class: {cls_name}")

        # Inner estimator (CalibratedClassifierCV exposes .estimator;
        # older sklearn versions use .base_estimator).
        inner = getattr(model, "estimator", None) \
                or getattr(model, "base_estimator", None)
        if inner is not None:
            _emit(f"  inner estimator: {type(inner).__name__}")

        if cls_name == "CalibratedClassifierCV":
            method = getattr(model, "method", "?")
            cv     = getattr(model, "cv", "?")
            cals   = getattr(model, "calibrated_classifiers_", None) or []
            _emit(f"  method: {method}")
            _emit(f"  cv:     {cv}")
            _emit(f"  calibrators fitted: {len(cals)}")
        else:
            _emit("  >>> NOT A CALIBRATED WRAPPER -- raw classifier")
            _emit("  >>> XGBoost without calibration outputs extreme")
            _emit("  >>> probabilities (near 0 / 1).  This is the prime")
            _emit("  >>> suspect for the 100%-confidence cluster.")

        # Sanity: does the model have predict_proba?
        _emit(f"  predict_proba available: {hasattr(model, 'predict_proba')}")


# ── Check 3: predict_proba on a real-prop sample ────────────────────────────

def _check_predict_proba_sample(n_total: int = 20) -> None:
    _section(f"CHECK 3: predict_proba on {n_total} real props from props_client cache")
    try:
        import numpy as np  # type: ignore
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"numpy import failed: {exc}")
        return
    try:
        from src.props_client import (
            get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS,
            restore_from_supabase_if_missing,
        )
        from src.props_model import (
            _compute_raw_over_prob, _pitcher_model, _batter_model,
        )
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"prop imports failed: {exc}")
        return

    # Hydrate raw-line cache from Supabase if local file is missing
    # (Railway-friendly).
    try:
        restore_from_supabase_if_missing()
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"NOTE: raw-line restore failed: {exc}")

    payload = get_client().get_today_props() or {}
    all_markets = payload.get("markets") or {}
    if not all_markets:
        _emit(">>> NO RAW PROPS IN CACHE -- skipping predict_proba sample")
        return

    # Build a flat sample, OVER side only (the model treats sides
    # symmetrically; one side per (player, line) keeps the sample
    # tidy without halving the raw-p distribution shape).
    pool: list[tuple[str, dict]] = []
    for market, props in all_markets.items():
        if market not in ALL_PITCHER_MARKETS and market not in ALL_BATTER_MARKETS:
            continue
        for p in (props or []):
            if (p.get("side") or "").strip().lower() == "over":
                pool.append((market, p))
    if not pool:
        _emit(">>> raw cache is empty of OVER-side props")
        return

    # Reservoir-style deterministic pick: every Kth row for a balanced
    # cross-section of markets without needing a PRNG.
    step = max(1, len(pool) // n_total)
    sample = pool[::step][:n_total]
    _emit(f"sampled {len(sample)} of {len(pool)} props "
          f"(step={step}, OVER side only)")
    _emit("")

    # Run _compute_raw_over_prob and capture (raw_p, source) for each.
    # We also call predict_proba directly on the classifier for the
    # rows where the source is the classifier so the audit can compare
    # the "raw model output" against the post-Poisson value.
    rows: list[tuple[str, str, float, str, Optional[float]]] = []
    for market, prop in sample:
        bucket = "pitcher" if market.startswith("pitcher_") else "batter"
        try:
            raw_p, source = _compute_raw_over_prob(prop, bucket)
        except Exception as exc:                                          # noqa: BLE001
            _emit(f"  {market} {prop.get('player_name')!r}: scoring FAILED: {exc}")
            continue
        # Optional: raw classifier predict_proba (skip Poisson path)
        classifier_raw: Optional[float] = None
        try:
            from src.props_model import _build_reg_vector
            model = (_pitcher_model if bucket == "pitcher" else _batter_model).load()
            if model is not None and hasattr(model, "predict_proba"):
                vec, _names = _build_reg_vector(prop, bucket)
                X = np.array([vec], dtype=float)
                proba = model.predict_proba(X)[0]
                classifier_raw = float(proba[1]) if len(proba) > 1 else float(proba[0])
        except Exception:                                                 # noqa: BLE001
            classifier_raw = None
        rows.append((
            market, prop.get("player_name") or "?",
            raw_p, source, classifier_raw,
        ))
        _emit(
            f"  {market[:24]:24s}  "
            f"{(prop.get('player_name') or '?')[:24]:24s}  "
            f"line={str(prop.get('line')):>5}  "
            f"raw_p={raw_p:.4f}  "
            f"src={source:9s}"
            + (f"  cls_raw={classifier_raw:.4f}" if classifier_raw is not None else "")
        )

    if not rows:
        _emit(">>> no rows scored -- nothing to summarise")
        return

    _emit("")
    _emit("Distribution of raw_p across the sample:")
    raw_ps = sorted(r[2] for r in rows)
    n = len(raw_ps)
    p25 = raw_ps[max(0, n // 4 - 1)]
    p75 = raw_ps[min(n - 1, (3 * n) // 4)]
    median = raw_ps[n // 2]
    mean = sum(raw_ps) / n
    extreme_lo = sum(1 for p in raw_ps if p <= 0.05)
    extreme_hi = sum(1 for p in raw_ps if p >= 0.95)
    _emit(f"  n={n} min={raw_ps[0]:.3f} p25={p25:.3f} median={median:.3f} "
          f"mean={mean:.3f} p75={p75:.3f} max={raw_ps[-1]:.3f}")
    _emit(f"  extreme (<=0.05): {extreme_lo}/{n}  "
          f"({extreme_lo / n * 100:.0f}%)")
    _emit(f"  extreme (>=0.95): {extreme_hi}/{n}  "
          f"({extreme_hi / n * 100:.0f}%)")

    # Break down by source so the Poisson-PMF path vs classifier-path
    # raw-p distributions can be compared in isolation.
    by_source: dict[str, list[float]] = {}
    for _m, _p, raw, src, _c in rows:
        by_source.setdefault(src, []).append(raw)
    _emit("")
    for src, vals in sorted(by_source.items()):
        if not vals:
            continue
        vals.sort()
        n = len(vals)
        med = vals[n // 2]
        mean = sum(vals) / n
        _emit(f"  by_source[{src}]  n={n}  min={vals[0]:.3f}  "
              f"median={med:.3f}  mean={mean:.3f}  max={vals[-1]:.3f}")


# ── Check 4: source-tree grep for squash / cap leftovers ────────────────────

def _check_squash_cap_grep() -> None:
    _section("CHECK 4: leftover squash / cap code in src/props_model.py")
    src_path = _APP_DIR / "src" / "props_model.py"
    if not src_path.exists():
        _emit(f">>> {src_path} not found")
        return
    text = src_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    patterns = [
        (r"0\.85",            "literal 0.85 (the old confidence cap)"),
        (r"_squash_prob",     "_squash_prob function or call"),
        (r"_CONF_CAP",        "_CONF_CAP constant or use"),
        (r"_PROB_LO|_PROB_HI", "_PROB_LO / _PROB_HI bounds"),
        (r"\bcap\b",          "any 'cap' word (case-sensitive, word-bounded)"),
        (r"squash",           "any 'squash' substring"),
    ]
    for pat, label in patterns:
        _emit("")
        _emit(f"  pattern: {label}  ({pat!r})")
        hits = []
        for i, line in enumerate(lines, 1):
            if re.search(pat, line):
                hits.append((i, line.strip()))
        if not hits:
            _emit("    (no matches)")
            continue
        for i, line in hits[:15]:
            _emit(f"    line {i:4d}: {line[:110]}")
        if len(hits) > 15:
            _emit(f"    ... and {len(hits) - 15} more")


# ── Check 5: feature-vector mean / std ──────────────────────────────────────

def _check_feature_stats(n_pitcher: int = 10, n_batter: int = 10) -> None:
    _section(f"CHECK 5: feature-vector stats ({n_pitcher} pitcher + {n_batter} batter)")
    try:
        from src.props_client import (
            get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS,
        )
        from src.props_model import _build_reg_vector
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"imports failed: {exc}")
        return

    payload = get_client().get_today_props() or {}
    all_markets = payload.get("markets") or {}
    if not all_markets:
        _emit(">>> NO RAW PROPS IN CACHE -- skipping feature stats")
        return

    pitcher_pool: list[dict] = []
    batter_pool: list[dict] = []
    for market, props in all_markets.items():
        for p in (props or []):
            if (p.get("side") or "").strip().lower() != "over":
                continue
            if market in ALL_PITCHER_MARKETS and len(pitcher_pool) < n_pitcher * 4:
                pitcher_pool.append(p)
            elif market in ALL_BATTER_MARKETS and len(batter_pool) < n_batter * 4:
                batter_pool.append(p)

    pitcher_sample = pitcher_pool[:n_pitcher]
    batter_sample  = batter_pool[:n_batter]

    for bucket, sample in (("pitcher", pitcher_sample), ("batter", batter_sample)):
        _emit("")
        _emit(f"[{bucket}]  sample_size={len(sample)}")
        if not sample:
            _emit("  >>> sample empty -- nothing to report")
            continue

        vectors: list[list[float]] = []
        feature_names: Optional[list[str]] = None
        for p in sample:
            try:
                vec, names = _build_reg_vector(p, bucket)
                vectors.append(vec)
                if feature_names is None:
                    feature_names = names
            except Exception as exc:                                      # noqa: BLE001
                _emit(f"  build_reg_vector failed for "
                      f"{p.get('player_name')!r}: {exc}")
        if not vectors or not feature_names:
            _emit("  >>> no vectors built; cannot compute stats")
            continue

        # Column stats
        n_feats = len(feature_names)
        n_rows  = len(vectors)
        _emit(f"  vectors built: {n_rows} rows × {n_feats} features")
        _emit(f"  {'feature':32s}  {'mean':>10s}  {'std':>10s}  "
              f"{'min':>10s}  {'max':>10s}  notes")
        _emit(f"  {'-' * 32}  {'-' * 10}  {'-' * 10}  "
              f"{'-' * 10}  {'-' * 10}  -----")
        suspicious_count = 0
        for i, name in enumerate(feature_names):
            col = [vectors[r][i] for r in range(n_rows)]
            try:
                mn   = min(col)
                mx   = max(col)
                mean = sum(col) / n_rows
                var  = sum((x - mean) ** 2 for x in col) / max(1, n_rows - 1)
                std  = math.sqrt(var)
            except Exception:                                             # noqa: BLE001
                continue
            notes: list[str] = []
            if abs(mn) > 1e3 or abs(mx) > 1e3:
                notes.append("OUTLIER_MAGNITUDE")
            if std > 100:
                notes.append("HIGH_STD")
            if mx == mn:
                notes.append("ZERO_VARIANCE")
            note_text = ", ".join(notes) if notes else ""
            if notes:
                suspicious_count += 1
            _emit(
                f"  {name[:32]:32s}  {mean:10.3f}  {std:10.3f}  "
                f"{mn:10.3f}  {mx:10.3f}  {note_text}"
            )
        _emit(f"  flagged features: {suspicious_count} / {n_feats}")


# ── Bonus: confidence-distribution snapshot from the scored cache ───────────

def _check_scored_cache_distribution() -> None:
    _section("BONUS: confidence distribution snapshot from scored cache")
    try:
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _emit(f"import failed: {exc}")
        return
    cached = load_scored_props() or {}
    picks = cached.get("picks") or []
    if not picks:
        _emit(">>> scored cache is empty")
        return
    confs = sorted(float(p.get("confidence") or 0.0) for p in picks)
    n = len(confs)
    p25 = confs[max(0, n // 4 - 1)]
    p75 = confs[min(n - 1, (3 * n) // 4)]
    median = confs[n // 2]
    mean = sum(confs) / n
    saturated = sum(1 for c in confs if c >= 0.95)
    _emit(f"  generated_at: {cached.get('generated_at')}")
    _emit(f"  n={n}  min={confs[0]:.3f}  p25={p25:.3f}  median={median:.3f} "
          f"mean={mean:.3f}  p75={p75:.3f}  max={confs[-1]:.3f}")
    _emit(f"  saturated (conf >= 0.95): {saturated}/{n}  "
          f"({saturated / n * 100:.0f}%)")


# ── Driver ──────────────────────────────────────────────────────────────────

def run_audit() -> None:
    """Run all five checks back-to-back.  Idempotent + read-only --
    safe to call from a Flask admin route or via ``python -m``."""
    _emit("PROPS CONFIDENCE AUDIT START")
    _check_models()
    _check_squash_cap_grep()
    _check_predict_proba_sample()
    _check_feature_stats()
    _check_scored_cache_distribution()
    _section("AUDIT COMPLETE")


if __name__ == "__main__":
    run_audit()
