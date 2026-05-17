"""
retrain_mlb_models.py
=====================
Full MLB model retraining pipeline with recency weighting.

Steps
-----
1. Delete cached MLB models (and optionally the enriched dataset cache).
2. Build the enriched historical dataset (2022-2025 from Retrosheet + MLB Stats API).
3. Load the current MLB season (2026) from API-Sports and last season (2025)
   for dynamic team-weight comparison.
4. Identify teams with >15pp win-rate change (2025 → 2026) — those teams'
   current-season rows receive a 75% weight boost instead of the 60% default.
5. Retrain moneyline, run-line, and totals models with the 60/25/15% weighting.
6. Print a weight-shift summary showing which teams changed the most.

Usage
-----
    python retrain_mlb_models.py              # normal retrain
    python retrain_mlb_models.py --fresh-sp   # re-fetch all pitcher data
    python retrain_mlb_models.py --dry-run    # show what would change, then quit
"""

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

MODEL_FILES = [
    Path(".cache/model_baseball_mlb.joblib"),
    Path(".cache/model_run_line_mlb.joblib"),
    Path(".cache/model_totals_mlb.joblib"),
]
ENRICHED_CACHE = Path(".cache/enriched_mlb_dataset.joblib")


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
    parser = argparse.ArgumentParser(description="Retrain MLB prediction models.")
    parser.add_argument(
        "--fresh-sp", action="store_true",
        help="Also delete enriched dataset cache (re-fetches all pitcher data).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted/retrained, then exit without changes.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  MLB Model Retraining Pipeline  --  Recency Weighting 60/25/15%")
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
        print(f"    {ENRICHED_CACHE}  (always deleted - season list changed)")
        print("\n[DRY RUN] Exiting without changes.")
        return

    # ── Step 1: delete cached model files ────────────────────────────────────
    print("\n[1] Deleting cached ML model files...")
    for p in MODEL_FILES:
        _delete_if_exists(p, p.name)

    # Always delete the enriched cache: _SEASONS now includes 2025 and feature
    # count changed from 23 → 24.  A stale cache would give wrong dimensions.
    print("\n[1b] Deleting enriched dataset cache (seasons + features changed)...")
    _delete_if_exists(ENRICHED_CACHE, ENRICHED_CACHE.name)

    if args.fresh_sp:
        # Already deleted above; this flag now mainly signals intent
        print("  [--fresh-sp] All MLB Stats API schedule caches will also be rebuilt.")

    # ── Step 2: build enriched historical dataset ─────────────────────────────
    print("\n[2] Building enriched historical dataset (2022-2025)...")
    t0 = time.time()
    from src.enriched_historical_data import build_enriched_dataset, _SEASONS

    print(f"    Seasons: {list(_SEASONS)}")
    X, y_ml, y_rl, totals = build_enriched_dataset(force_rebuild=True)
    elapsed = time.time() - t0
    print(f"    Done: {len(y_ml):,} moneyline rows, {len(y_rl):,} run-line rows, "
          f"{len(totals):,} totals rows  ({elapsed:.1f}s)")

    if len(y_ml) < 100:
        print("\n  ERROR: Enriched dataset has fewer than 100 rows - aborting retrain.")
        sys.exit(1)

    # ── Step 3: import model stack ────────────────────────────────────────────
    print("\n[3] Importing model stack...")
    from src.sports_config import MLB, CURRENT_SEASON
    from src.model import BettingModel
    from src.run_line_model import RunLineModel
    from src.totals_model import TotalsModel
    from src.game_store import GameStore
    from src.mlb_features import MLBFeatureBuilder
    from src.recency_weights import find_high_change_teams, format_weight_shift_summary

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    api_key      = os.environ.get("API_SPORTS_KEY", "")
    # API-Sports free plan supports 2022-2024 only.  CURRENT_SEASON is used for
    # context; we probe the API starting from it and fall back to 2024 if needed.
    prev_season  = CURRENT_SEASON - 1   # 2025 (may not be accessible on free plan)
    curr_season  = CURRENT_SEASON        # 2026 (may not be accessible on free plan)

    # ── Step 4: load current and previous seasons ─────────────────────────────
    print(f"\n[4] Loading {curr_season} (current) and {prev_season} (previous) seasons...")

    store_curr = GameStore(
        api_key=api_key,
        base_url=MLB.api_sports_base,
        league_id=MLB.league_id,
        sport_tag="mlb",
    )
    store_prev = GameStore(
        api_key=api_key,
        base_url=MLB.api_sports_base,
        league_id=MLB.league_id,
        sport_tag="mlb",
    )

    # Try loading current season; fall back through recent years if plan blocks it
    n_curr = 0
    loaded_curr_season = None
    for try_season in [curr_season, prev_season, 2024]:
        try:
            n_curr = store_curr.load(try_season)
            loaded_curr_season = try_season
            print(f"    Current season {try_season}: {n_curr} completed games")
            break
        except Exception as e:
            print(f"    {try_season}: not accessible ({e}) - trying earlier year...")
    if loaded_curr_season is None:
        print("    Could not load any current season - training on historical data only.")
        loaded_curr_season = 2024  # placeholder (won't matter, n_curr=0)

    # Load previous season for comparison (one year behind whatever we loaded)
    n_prev_comp = 0
    prev_comp_season = loaded_curr_season - 1
    try:
        n_prev_comp = store_prev.load(prev_comp_season)
        print(f"    Previous season {prev_comp_season}: {n_prev_comp} completed games (for comparison)")
    except Exception as e:
        print(f"    {prev_comp_season}: not accessible ({e}) - no team comparison available")

    feature_builder = MLBFeatureBuilder(store_curr)

    # ── Step 5: find high-change teams ────────────────────────────────────────
    print("\n[5] Identifying high-change teams (>15pp win-rate shift)...")
    high_change = {}
    if n_curr > 0 and n_prev_comp > 0:
        high_change = find_high_change_teams(store_curr, store_prev)
        print(f"    {len(high_change)} team(s) exceed the 15pp threshold "
              f"-> current-season weight boosted to 75%")
    else:
        print("    Skipped (one or both seasons unavailable) - using standard 60/25/15% weights")

    high_change_ids = set(high_change.keys())

    # ── Step 6: retrain moneyline model ───────────────────────────────────────
    print(f"\n[6] Retraining MLB moneyline model (XGB + LR + NN)...")
    t0 = time.time()
    model = BettingModel(MLB)
    status_ml = model._train(store_curr, feature_builder, loaded_curr_season, high_change_ids)
    print(f"    {status_ml}  ({time.time()-t0:.1f}s)")

    # ── Step 7: retrain run-line model ────────────────────────────────────────
    print(f"\n[7] Retraining MLB run-line model (XGB + LR + NN)...")
    t0 = time.time()
    run_line = RunLineModel()
    status_rl = run_line._train(store_curr, feature_builder, loaded_curr_season, high_change_ids)
    print(f"    {status_rl}  ({time.time()-t0:.1f}s)")

    # ── Step 8: retrain totals model ──────────────────────────────────────────
    print(f"\n[8] Retraining MLB totals model (XGB + LR + NN)...")
    t0 = time.time()
    totals_model = TotalsModel()
    status_tot = totals_model._train(store_curr, feature_builder, loaded_curr_season, high_change_ids)
    print(f"    {status_tot}  ({time.time()-t0:.1f}s)")

    # ── Step 9: accuracy comparison ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  Retraining Complete - Accuracy Comparison")
    print("=" * 70)
    rows = [
        ("Moneyline", ".cache/model_baseball_mlb.joblib",
         prev_acc.get(".cache/model_baseball_mlb.joblib", "N/A"), status_ml),
        ("Run Line",  ".cache/model_run_line_mlb.joblib",
         prev_acc.get(".cache/model_run_line_mlb.joblib", "N/A"), status_rl),
        ("Totals",    ".cache/model_totals_mlb.joblib",
         prev_acc.get(".cache/model_totals_mlb.joblib", "N/A"), status_tot),
    ]
    for label, _, before, after in rows:
        print(f"\n  {label}:")
        print(f"    Before : {before}")
        print(f"    After  : {after}")

    # ── Step 10: weight-shift summary ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  Recency Weight Summary")
    print("=" * 70)
    print(f"\n  Base season weights applied to ALL teams:")
    print(f"    Historical (<= 2024) : 15%")
    print(f"    Previous   (2025)   : 25%")
    print(f"    Current    (2026)   : 60%")

    if n_curr > 0 and n_prev_comp > 0:
        summary = format_weight_shift_summary(high_change, store_curr, store_prev)
        print(summary)
    else:
        print("\n  (Season data unavailable - no team-level comparison possible)")

    print()


if __name__ == "__main__":
    main()
