"""
build_synthetic_batter_bets.py
==============================
Batter counterpart to build_synthetic_pitcher_bets.py.  Walks the cached
batter game logs and emits one synthetic "settled bet" per (player, game,
market) using the real stat as actual_value.

Used to drive the props backtest harness when the production ledger is
empty (no real settled prop bets yet).  MAE numbers are honest because
actual stats are real; hit_rate / ROI numbers reflect synthetic line
placement (default half-integer lines) so they're indicative not
production-grade.

Important caveat: the model's batter_rolling_snapshots.json includes
data from these very games, so MAE numbers are optimistically biased
(snapshot leakage).  Bias is constant across PR iterations so deltas
between runs remain interpretable.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

_CACHE_DIR = Path(".cache")

# (stat_key, line) per market — lines chosen near the league median.
_MARKETS: dict[str, tuple[str, float]] = {
    "batter_hits":         ("H",   0.5),
    "batter_total_bases":  ("TB",  1.5),
    "batter_home_runs":    ("HR",  0.5),
    "batter_rbis":         ("RBI", 0.5),
    "batter_runs_scored":  ("R",   0.5),
    "batter_walks":        ("BB",  0.5),
}


def _log(msg: str) -> None:
    print(f"[synth-batter-bets] {msg}", flush=True, file=sys.stderr)


def _commence_iso(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.isoformat() + "T19:00:00Z"
    except (TypeError, ValueError):
        return ""


def build_bets(season: int, *, min_pa: int = 3) -> list[dict]:
    cache_path = _CACHE_DIR / f"props_training_data_{season}.json"
    if not cache_path.exists():
        _log(f"cache missing: {cache_path}")
        return []
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    batters = payload.get("batters") or []
    _log(f"loaded {len(batters)} batter-seasons from {cache_path}")

    bets: list[dict] = []
    for b in batters:
        pid  = b.get("id")
        name = b.get("name") or ""
        team = (b.get("team") or "").strip().upper()
        for g in (b.get("games") or []):
            pa = int(g.get("PA") or 0)
            if pa < min_pa:
                continue
            date = g.get("date") or ""
            commence = _commence_iso(date)
            if not commence:
                continue
            stats = {
                "H":   int(g.get("H")   or 0),
                "TB":  int(g.get("TB")  or 0),
                "HR":  int(g.get("HR")  or 0),
                "RBI": int(g.get("RBI") or 0),
                "R":   int(g.get("R")   or 0),
                "BB":  int(g.get("BB")  or 0),
            }
            for market, (stat_key, line) in _MARKETS.items():
                actual = stats[stat_key]
                side = "Over" if actual > line else "Under"
                result = "win" if (
                    (side == "Over"  and actual > line) or
                    (side == "Under" and actual < line)
                ) else "loss"
                bets.append({
                    "id":             uuid.uuid4().hex,
                    "market":         market,
                    "player":         name,
                    "team":           team,
                    "line":           line,
                    "side":           side,
                    "odds":           -110,
                    "commence_time":  commence,
                    "event_id":       f"synth_{pid}_{date}_{stat_key}",
                    "actual_value":   actual,
                    "result":         result,
                })

    _log(f"emitted {len(bets)} synthetic batter bets for season {season}")
    return bets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season",  type=int, default=2025)
    ap.add_argument("--output",  type=Path, default=Path("synthetic_batter_bets.json"))
    ap.add_argument("--min-pa",  type=int, default=3)
    args = ap.parse_args()

    bets = build_bets(args.season, min_pa=args.min_pa)
    if not bets:
        _log("no bets generated -- aborting")
        return 1
    args.output.write_text(json.dumps({"bets": bets}, ensure_ascii=False))
    _log(f"wrote {args.output} ({len(bets)} bets, {args.output.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
