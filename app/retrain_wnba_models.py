"""
retrain_wnba_models.py
======================
Full WNBA model retraining pipeline with recency weighting.

Steps
-----
1. Delete cached WNBA models.
2. Load the current WNBA season from the ESPN free API via WNBAStatsClient.
3. Build WNBA features.
4. Retrain moneyline, spread, and totals models.
5. Print a summary of before/after accuracy.

Usage
-----
    python retrain_wnba_models.py          # normal retrain
    python retrain_wnba_models.py --dry-run  # show what would change, then quit
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

MODEL_FILES = [
    Path(".cache/model_basketball_wnba.joblib"),
    Path(".cache/model_spread_wnba.joblib"),
    Path(".cache/model_totals_wnba.joblib"),
]


def _delete_if_exists(p: Path, label: str) -> bool:
    if p.exists():
        p.unlink()
        print(f"  [deleted] {label}")
        return True
    print(f"  [absent]  {label}")
    return False


def _load_prev_accuracy() -> dict:
    import joblib
    result = {}
    for p in MODEL_FILES:
        if not p.exists():
            result[str(p)] = "N/A (no saved model)"
            continue
        try:
            saved = joblib.load(p)
            if "cv_accuracy" in saved:
                result[str(p)] = f"XGB CV {saved['cv_accuracy']:.1%}"
            elif "xgb_cv" in saved:
                result[str(p)] = f"XGB CV {saved['xgb_cv']:.1%}"
            elif "xgb_rmse" in saved:
                result[str(p)] = f"XGB RMSE {saved['xgb_rmse']:.2f}"
            else:
                result[str(p)] = "unknown"
        except Exception as e:
            result[str(p)] = f"load error: {e}"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrain WNBA prediction models.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted/retrained, then exit without changes.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  WNBA Model Retraining Pipeline  --  Recency Weighting 60/25/15%")
    print("=" * 70)

    # ── Step 0: capture before-accuracy ───────────────────────────────────────
    print("\n[0] Current model accuracy (before retraining):")
    prev_acc = _load_prev_accuracy()
    for path_str, acc in prev_acc.items():
        print(f"    {Path(path_str).name:40s}  {acc}")

    if args.dry_run:
        print("\n[DRY RUN] Files that would be deleted:")
        for p in MODEL_FILES:
            print(f"    {p}")
        print("\n[DRY RUN] Exiting without changes.")
        return

    # ── Step 1: delete cached model files ────────────────────────────────────
    print("\n[1] Deleting cached WNBA model files...")
    for p in MODEL_FILES:
        _delete_if_exists(p, p.name)

    # ── Step 2: import stack ──────────────────────────────────────────────────
    print("\n[2] Importing model stack...")
    from src.sports_config import WNBA
    from src.model import BettingModel
    from src.wnba_stats_client import WNBAStatsClient
    from src.wnba_features import WNBAFeatureBuilder
    from src.wnba_spread_model import WNBASpreadModel
    from src.wnba_totals_model import WNBATotalsModel
    from src.cache import Cache

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    season = int(os.environ.get("SEASON", 2025))

    # ── Step 3: load season data ──────────────────────────────────────────────
    print(f"\n[3] Loading WNBA {season} season data via ESPN API...")
    t0 = time.time()
    cache = Cache()
    sports_key = os.environ.get("API_SPORTS_KEY", "")
    client = WNBAStatsClient(api_key=sports_key, cache=cache)
    n_completed = client.load(season)
    print(f"    {n_completed} completed games loaded  ({time.time()-t0:.1f}s)")

    if n_completed < 10:
        print(f"\n  WARNING: Only {n_completed} completed games found.")
        print("  Models will still retrain but accuracy may be limited.")

    # ── Step 4: build features ────────────────────────────────────────────────
    print("\n[4] Building WNBA feature matrix...")
    fb = WNBAFeatureBuilder(client)

    # ── Step 5: retrain moneyline model ───────────────────────────────────────
    print(f"\n[5] Retraining WNBA moneyline model (XGB + LR + NN)...")
    t0 = time.time()
    ml_model = BettingModel(WNBA)
    status_ml = ml_model._train(client, fb, season)
    print(f"    {status_ml}  ({time.time()-t0:.1f}s)")

    # ── Step 6: retrain spread model ──────────────────────────────────────────
    print(f"\n[6] Retraining WNBA spread model (XGB + LR)...")
    t0 = time.time()
    spread_model = WNBASpreadModel()
    status_sp = spread_model._train(client, fb, season)
    print(f"    {status_sp}  ({time.time()-t0:.1f}s)")

    # ── Step 7: retrain totals model ──────────────────────────────────────────
    print(f"\n[7] Retraining WNBA totals model (XGB + LR)...")
    t0 = time.time()
    totals_model = WNBATotalsModel()
    status_tot = totals_model._train(client, fb, season)
    print(f"    {status_tot}  ({time.time()-t0:.1f}s)")

    # ── Step 8: accuracy comparison ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  Retraining Complete - Accuracy Comparison")
    print("=" * 70)
    rows = [
        ("Moneyline", ".cache/model_basketball_wnba.joblib",
         prev_acc.get(".cache/model_basketball_wnba.joblib", "N/A"), status_ml),
        ("Spread",    ".cache/model_spread_wnba.joblib",
         prev_acc.get(".cache/model_spread_wnba.joblib",    "N/A"), status_sp),
        ("Totals",    ".cache/model_totals_wnba.joblib",
         prev_acc.get(".cache/model_totals_wnba.joblib",    "N/A"), status_tot),
    ]
    for label, _, before, after in rows:
        print(f"\n  {label}:")
        print(f"    Before : {before}")
        print(f"    After  : {after}")

    print()


if __name__ == "__main__":
    main()
