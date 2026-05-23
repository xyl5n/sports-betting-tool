"""
backtest_props_model.py
=======================
Inference-time backtest harness for the MLB player-prop models.

What this measures
------------------
For every settled bet in the props ledger (Supabase or local file),
re-run the CURRENT model's predict() against the stored prop context
(player, line, side, home_team, odds, ...) and compare:

  * predicted_value_now  vs  actual_value    -> per-stat MAE
  * recommendation_now   vs  actual outcome  -> per-stat hit rate
  * confidence_now       vs  win/loss        -> calibration spot check
  * model_now ROI @ -110 vig                 -> rough EV proxy

Two predictions per bet are reported side-by-side:

  baseline = predicted_value stored at bet placement time
             (whatever model was deployed then)
  current  = re-running the joblib that's in .cache/ right now

The PR-to-PR delta lives in the *current* column.  The baseline column
exists for context — it shows the model that was actually deployed for
each bet.

Caveat: snapshot leakage
------------------------
The batter snapshots in .cache/batter_rolling_snapshots.json are computed
from the most-recent game in the training data (post-2025).  A backtest
bet from May 2025 therefore uses a snapshot that includes its own future
games.  This biases the current-model MAE *optimistically* but the bias
is constant across PR iterations, so PR deltas remain interpretable as
*relative* improvements.  Per-date snapshot replay is a separate piece
of work.

Sample sizes are typically small (<200 settled bets per market) — every
metric is reported with n alongside, and MAE without enough samples is
flagged with a warning so we don't over-interpret 5-bet HR results.

Output
------
Writes a JSON snapshot to:
    .cache/backtest_results_<utc-timestamp>.json
and overwrites:
    .cache/backtest_baseline.json
which the next PR can diff against to produce a before/after table.

CLI
---
    python app/scripts/backtest_props_model.py
    python app/scripts/backtest_props_model.py --market-prefix batter
    python app/scripts/backtest_props_model.py --bets-file path/to/bets.json
    python app/scripts/backtest_props_model.py --label "PR1-batter-snapshots"

All progress is prefixed PROPS-BACKTEST so it's easy to grep in logs.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(".cache")
_RESULTS_DIR = _CACHE_DIR
_BASELINE_PATH = _CACHE_DIR / "backtest_baseline.json"


def _log(msg: str) -> None:
    print(f"PROPS-BACKTEST: {msg}", flush=True, file=sys.stderr)


# ---------------------------------------------------------------------------
# Bets source
# ---------------------------------------------------------------------------

def _load_bets_from_ledger() -> list[dict]:
    """Pull settled bets via PropsLedger (Supabase first, local file fallback)."""
    try:
        # Load .env so SUPABASE_URL/KEY surface when running from CLI locally.
        try:
            from dotenv import load_dotenv  # type: ignore[import-not-found]
            for cand in (Path(".env"), Path("app/.env"), Path("../.env")):
                if cand.exists():
                    load_dotenv(cand)
                    _log(f"loaded env: {cand}")
                    break
        except ImportError:
            pass

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.props_ledger import get_props_ledger
    except Exception as exc:  # noqa: BLE001
        _log(f"ledger import failed ({exc}) -- cannot load bets")
        return []
    led = get_props_ledger()
    led.reload()
    history = led.get_history()
    _log(f"ledger history: {len(history)} settled bets")
    return history


def _load_bets_from_file(path: Path) -> list[dict]:
    """Load bets from a JSON file matching the props_ledger schema.

    Expected shape: {"bets": [...]} or a bare list of bet dicts.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    bets = raw.get("bets") if isinstance(raw, dict) else raw
    if not isinstance(bets, list):
        return []
    history = [b for b in bets if b.get("result")]
    _log(f"file history: {len(history)} settled bets ({path})")
    return history


# ---------------------------------------------------------------------------
# Reconstruct a predict() input from a stored bet
# ---------------------------------------------------------------------------

def _bet_to_prop_dict(bet: dict) -> dict:
    """Mirror the field names the live prop pipeline produces so predict()
    sees the same shape it would in production.

    Park-context resolution (PR6 backtest fidelity):
      * If the bet carries `home_team` and `is_home`, honor them — that's
        the actual venue for this specific game.  Synthetic bet generators
        (build_synthetic_pitcher_bets.py) populate these so per-stat park
        factors evaluate against the real venue per row, not a single
        pitcher-home constant.
      * Otherwise fall back to bet["team"] as home_team + is_home=True
        (the original live-ledger shape, where venue isn't persisted).
    """
    explicit_home_team = (bet.get("home_team") or "").strip()
    explicit_is_home   = bet.get("is_home")
    if explicit_home_team and explicit_is_home is not None:
        home_team = explicit_home_team
        is_home   = bool(explicit_is_home)
    else:
        home_team = (bet.get("team") or "").strip().upper()[:3]
        is_home   = True
    return {
        "market":       bet.get("market", ""),
        "player_name":  bet.get("player", ""),
        "home_team":    home_team,
        "is_home":      is_home,
        "line":         float(bet.get("line") or 0),
        "best_odds":    int(bet.get("odds") or -110),
        "side":         (bet.get("side") or "Over").strip().title(),
        "all_books":    [{"odds": int(bet.get("odds") or -110)}],
        "event_id":     bet.get("event_id"),
        "commence_time": bet.get("commence_time"),
        # PR6: away_team carried through so pitcher-side opp-baseline
        # lookup picks the correct opposing batting team at predict time.
        "away_team":    bet.get("away_team"),
    }


# ---------------------------------------------------------------------------
# Score a single bet under the current model
# ---------------------------------------------------------------------------

def _score_bet(bet: dict, predict_fn) -> Optional[dict]:
    """Re-run predict() on the bet and emit a comparison row.

    Returns None when the bet is missing actual_value (can happen for
    void/manual settlements where actual was never recorded).
    """
    actual = bet.get("actual_value")
    if actual is None:
        return None
    try:
        actual_f = float(actual)
    except (TypeError, ValueError):
        return None

    line = float(bet.get("line") or 0)
    side_book = (bet.get("side") or "Over").strip().title()
    result    = (bet.get("result") or "").strip().lower()

    prop = _bet_to_prop_dict(bet)
    try:
        pred = predict_fn(prop)
    except Exception as exc:  # noqa: BLE001
        _log(f"  predict failed for {bet.get('player')!r} {bet.get('market')}: {exc}")
        return None

    rec_now   = (pred.get("recommendation") or "Pass").strip().title()
    pv_now    = pred.get("predicted_value")
    conf_now  = float(pred.get("confidence") or 0.0)

    # Was the model's CURRENT pick correct (only meaningful for non-Pass)?
    if rec_now in ("Over", "Under") and result != "void":
        if actual_f > line:
            actual_side = "Over"
        elif actual_f < line:
            actual_side = "Under"
        else:
            actual_side = "Push"
        rec_correct = (rec_now == actual_side) if actual_side != "Push" else None
    else:
        rec_correct = None  # Pass / void → not scoreable

    return {
        "id":               bet.get("id"),
        "player":           bet.get("player"),
        "market":           bet.get("market"),
        "line":             line,
        "side_book":        side_book,
        "odds":             int(bet.get("odds") or -110),
        "result":           result,
        "actual_value":     actual_f,
        "baseline_pv":      bet.get("predicted_value"),
        "baseline_conf":    bet.get("confidence"),
        "current_pv":       pv_now,
        "current_conf":     conf_now,
        "current_rec":      rec_now,
        "current_edge":     pred.get("edge"),
        "current_correct":  rec_correct,
    }


# ---------------------------------------------------------------------------
# Aggregate per-market / per-stat
# ---------------------------------------------------------------------------

# Markets the regressor predicted_value covers — only these get MAE.
_REGRESSOR_MARKETS = {
    "pitcher_strikeouts":   "K",
    "pitcher_earned_runs":  "ER",
    "pitcher_hits_allowed": "H",
    "pitcher_walks":        "BB",
    "pitcher_outs":         "outs",
    "batter_hits":          "H",
    "batter_total_bases":   "TB",
    "batter_home_runs":     "HR",
    "batter_rbis":          "RBI",
    "batter_runs_scored":   "R",
    "batter_walks":         "BB",
}


def _mae(pairs: list[tuple[float, float]]) -> Optional[float]:
    if not pairs:
        return None
    return sum(abs(a - b) for a, b in pairs) / len(pairs)


def _aggregate(rows: list[dict]) -> dict:
    """Compute per-market metrics from the per-bet scoring rows."""
    by_market: dict[str, list[dict]] = {}
    for r in rows:
        by_market.setdefault(r["market"], []).append(r)

    out: dict[str, dict] = {}
    for market, mrows in by_market.items():
        n = len(mrows)

        # MAE — current vs baseline.  Only rows with non-null predicted_value.
        cur_pairs = [
            (float(r["current_pv"]), float(r["actual_value"]))
            for r in mrows if r["current_pv"] is not None
        ]
        base_pairs = [
            (float(r["baseline_pv"]), float(r["actual_value"]))
            for r in mrows if r["baseline_pv"] is not None
        ]
        mae_current  = _mae(cur_pairs)
        mae_baseline = _mae(base_pairs)

        # Hit rate — current_correct is True / False / None (Pass or void).
        scoreable = [r for r in mrows if r["current_correct"] is not None]
        hits = sum(1 for r in scoreable if r["current_correct"])
        hit_rate = (hits / len(scoreable)) if scoreable else None

        # Naive ROI @ -110 (or actual stored odds) IF current_correct picks
        # were bet flat-stake.  This is a coarse EV proxy — real ROI uses
        # the actual book odds we stored.
        roi_picks = [r for r in scoreable]
        if roi_picks:
            pnl_units = 0.0
            for r in roi_picks:
                odds = int(r.get("odds") or -110)
                if r["current_correct"]:
                    # Profit on $1 risked at American odds
                    pnl_units += (100.0 / abs(odds)) if odds < 0 else (odds / 100.0)
                else:
                    pnl_units -= 1.0
            roi = pnl_units / len(roi_picks)
        else:
            roi = None

        out[market] = {
            "n_bets":         n,
            "n_scoreable":    len(scoreable),
            "n_with_current_pv":  len(cur_pairs),
            "n_with_baseline_pv": len(base_pairs),
            "mae_current":    round(mae_current, 4) if mae_current is not None else None,
            "mae_baseline":   round(mae_baseline, 4) if mae_baseline is not None else None,
            "hit_rate":       round(hit_rate, 4) if hit_rate is not None else None,
            "hits":           hits,
            "roi_at_book_odds": round(roi, 4) if roi is not None else None,
            "actual_mean":    round(statistics.fmean(
                float(r["actual_value"]) for r in mrows
            ), 3) if mrows else None,
            "low_sample":     n < 30,
        }

    return out


# ---------------------------------------------------------------------------
# Pretty log
# ---------------------------------------------------------------------------

def _fmt(value: Optional[float], spec: str) -> str:
    """Format a maybe-None metric.  '   —' on None so columns align."""
    if value is None:
        return "—"
    return format(value, spec)


def _log_summary_table(per_market: dict, label: str) -> None:
    _log(f"=== BACKTEST SUMMARY [{label}] ===")
    header = (
        f"  {'market':22s} {'n':>4s} {'mae_cur':>8s} {'mae_base':>9s} "
        f"{'hit_rate':>9s} {'roi':>7s} {'avg_actual':>10s}  flags"
    )
    _log(header)
    _log("  " + "-" * (len(header) - 2))
    for market in sorted(per_market):
        m = per_market[market]
        flags = []
        if m["low_sample"]:
            flags.append("LOW_N")
        # Improvement marker when both MAEs are available
        if m["mae_current"] is not None and m["mae_baseline"] is not None:
            if m["mae_current"] < m["mae_baseline"]:
                flags.append("MAE_DOWN")
            elif m["mae_current"] > m["mae_baseline"]:
                flags.append("MAE_UP")
        mae_cur  = _fmt(m["mae_current"],  ".3f")
        mae_base = _fmt(m["mae_baseline"], ".3f")
        hit_rate = _fmt(m["hit_rate"],     ".3f")
        roi      = _fmt(m["roi_at_book_odds"], "+.3f")
        avg_act  = _fmt(m["actual_mean"],  ".2f")
        line = (
            f"  {market:22s} "
            f"{m['n_bets']:>4d} "
            f"{mae_cur:>8s} "
            f"{mae_base:>9s} "
            f"{hit_rate:>9s} "
            f"{roi:>7s} "
            f"{avg_act:>10s}  "
            f"{','.join(flags) if flags else ''}"
        )
        _log(line)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backtest the current props model against settled ledger bets.",
    )
    ap.add_argument(
        "--market-prefix", default="all",
        choices=["all", "batter", "pitcher"],
        help="Filter bets by market prefix (default: all)",
    )
    ap.add_argument(
        "--bets-file", type=Path, default=None,
        help="Read settled bets from a JSON file instead of the live ledger "
             "(useful for offline testing).",
    )
    ap.add_argument(
        "--label", default="",
        help="Free-text label written into the result JSON; helpful for "
             "diffing PR-to-PR (e.g. 'PR1-batter-snapshots').",
    )
    ap.add_argument(
        "--output-json", type=Path, default=None,
        help="Override path for the timestamped per-run JSON snapshot.",
    )
    ap.add_argument(
        "--no-baseline-write", action="store_true",
        help="Skip overwriting .cache/backtest_baseline.json (keeps the "
             "previous baseline intact for delta computation).",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Only score the first N bets (debug).",
    )
    args = ap.parse_args()

    started = time.monotonic()
    label = args.label or "ad-hoc"
    _log(f"=== BACKTEST start ===  label={label!r}  market_prefix={args.market_prefix}")

    # Load bets
    if args.bets_file:
        if not args.bets_file.exists():
            _log(f"--bets-file {args.bets_file} not found -- aborting")
            return 1
        bets = _load_bets_from_file(args.bets_file)
    else:
        bets = _load_bets_from_ledger()

    if not bets:
        _log("no settled bets available -- cannot backtest")
        # Still emit an empty snapshot so the file exists for the diff workflow.
        out = {
            "generated_at":   datetime.now(timezone.utc).isoformat(),
            "label":          label,
            "market_prefix":  args.market_prefix,
            "n_bets":         0,
            "per_market":     {},
            "rows":           [],
            "notes":          "ledger empty",
        }
        _write_results(out, args)
        return 0

    # Filter by market prefix
    if args.market_prefix != "all":
        prefix = args.market_prefix + "_"
        bets = [b for b in bets if (b.get("market") or "").startswith(prefix)]
        _log(f"after market_prefix filter: {len(bets)} bets")

    if args.limit and args.limit > 0:
        bets = bets[: args.limit]

    # Load predict()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from src.props_model import predict
    except Exception as exc:  # noqa: BLE001
        _log(f"props_model import failed ({exc}) -- aborting")
        return 1

    # Score
    rows: list[dict] = []
    skipped = 0
    for i, b in enumerate(bets, 1):
        scored = _score_bet(b, predict)
        if scored is None:
            skipped += 1
            continue
        rows.append(scored)
        if i % 50 == 0 or i == len(bets):
            _log(f"  scored {i}/{len(bets)}  kept={len(rows)}  skipped={skipped}")

    if not rows:
        _log("no bets produced scoreable rows -- exiting")
        return 0

    per_market = _aggregate(rows)
    _log_summary_table(per_market, label)

    out = {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "label":            label,
        "market_prefix":    args.market_prefix,
        "source":           ("file:" + str(args.bets_file)) if args.bets_file else "ledger",
        "n_bets_input":     len(bets),
        "n_bets_scored":    len(rows),
        "n_skipped":        skipped,
        "per_market":       per_market,
        "rows":             rows,
        "notes":            (
            "current_pv/current_rec come from re-running predict() against "
            "the joblibs in .cache/ now.  baseline_pv is the predicted_value "
            "stored at the time the bet was placed (whatever model was deployed "
            "then).  Snapshot leakage: current snapshots cover all training "
            "seasons including future games — bias is constant across PR "
            "iterations so PR-to-PR deltas remain interpretable."
        ),
    }
    _write_results(out, args)

    elapsed = time.monotonic() - started
    _log(f"=== DONE in {elapsed:.1f}s ===  scored={len(rows)}  markets={len(per_market)}")
    return 0


def _write_results(out: dict, args) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.output_json or (_RESULTS_DIR / f"backtest_results_{ts}.json")
    try:
        out_path.write_text(
            json.dumps(out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"wrote {out_path}")
    except Exception as exc:  # noqa: BLE001
        _log(f"write {out_path} failed: {exc}")

    # Only overwrite the baseline file when we actually scored rows — an empty
    # run (no ledger, all skipped) shouldn't silently clobber the previous
    # baseline that future PR diffs depend on.
    n_scored = int(out.get("n_bets_scored") or 0)
    if args.no_baseline_write:
        _log("baseline write skipped (--no-baseline-write)")
    elif n_scored == 0:
        _log("baseline write skipped (0 rows scored — keeping prior baseline intact)")
    else:
        try:
            _BASELINE_PATH.write_text(
                json.dumps(out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _log(f"overwrote baseline: {_BASELINE_PATH}")
        except Exception as exc:  # noqa: BLE001
            _log(f"baseline write failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
