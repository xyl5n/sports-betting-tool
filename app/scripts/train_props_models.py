"""
train_props_models.py
=====================
CLI entry point for the props model training pipeline.

The actual pipeline lives in src.props_training so both this script and
the /api/admin/train_props_models route can call it without duplication.

Usage:
    python app/scripts/train_props_models.py --season 2025
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--skip-pitcher", action="store_true")
    ap.add_argument("--skip-batter",  action="store_true")
    ap.add_argument("--no-push",      action="store_true",
                    help="Skip the Supabase upload step")
    args = ap.parse_args()

    # Add app/ to sys.path so `from src.props_training import run_training`
    # works when this script is run directly (python app/scripts/...).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.props_training import run_training

    result = run_training(
        season       = args.season,
        skip_pitcher = args.skip_pitcher,
        skip_batter  = args.skip_batter,
        push         = not args.no_push,
    )
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
