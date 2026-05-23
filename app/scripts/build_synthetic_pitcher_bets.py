"""
build_synthetic_pitcher_bets.py
================================
Constructs a synthetic "settled bets" file the props backtest harness can
score against, from cached MLB Stats API game logs.

Why this exists
---------------
The production props ledger is empty in dev environments, so the standard
backtest_props_model.py workflow has nothing to score.  This helper walks
the 2025 pitcher game-log cache and emits one synthetic bet per (pitcher,
game, market) combination using the player's REAL stat as actual_value.

Output schema mirrors props_ledger.PropsLedger entries so backtest can
load via --bets-file without code changes:
    {
        "bets": [
            {
                "id":             "<uuid>",
                "market":         "pitcher_strikeouts",
                "player":         "Andrew Abbott",
                "team":           "CIN",
                "line":           5.5,
                "side":           "Over" | "Under",
                "odds":           -110,
                "commence_time":  "2025-08-15T19:35:00Z",
                "event_id":       "synth_<pid>_<date>_K",
                "actual_value":   7,
                "result":         "win" | "loss",
            },
            ...
        ]
    }

Lines are set 0.5 below the rounded league median for the market so the
harness reliably hits the predicted_value comparison code path (any line
works since we report MAE on predicted_value vs actual_value, not
win/loss correctness).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

_CACHE_DIR = Path(".cache")

# Markets we emit per pitcher game.  Maps market name -> (stat_key_in_log,
# default_line).  Lines roughly mirror modern-era median per-start values.
_MARKETS: dict[str, tuple[str, float]] = {
    "pitcher_strikeouts":   ("K",    5.5),
    "pitcher_earned_runs":  ("ER",   2.5),
    "pitcher_hits_allowed": ("H",    4.5),
    "pitcher_walks":        ("BB",   1.5),
    "pitcher_outs":         ("outs", 17.5),
}


def _log(msg: str) -> None:
    print(f"[synth-bets] {msg}", flush=True, file=sys.stderr)


# Cached gamelogs store opp_team as full names; harness only reads team
# (pitcher's own team) via [:3] uppercase.  We'll set team to the pitcher's
# team_abbrev when available, else "" (harness uses neutral fallback).
def _commence_iso(date_str: str) -> str:
    """Convert MLB Stats API date YYYY-MM-DD to ISO UTC midnight Z."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return d.isoformat() + "T19:00:00Z"   # 19:00 UTC ~ standard MLB slot
    except (TypeError, ValueError):
        return ""


def build_bets(season: int, *, starts_only: bool = True) -> list[dict]:
    """Walk .cache/props_training_data_<season>.json and emit synthetic bets."""
    cache_path = _CACHE_DIR / f"props_training_data_{season}.json"
    if not cache_path.exists():
        _log(f"cache missing: {cache_path}")
        return []
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    pitchers = payload.get("pitchers") or []
    _log(f"loaded {len(pitchers)} pitcher-seasons from {cache_path}")

    bets: list[dict] = []
    for p in pitchers:
        pid   = p.get("id")
        name  = p.get("name") or ""
        team  = (p.get("team") or "").strip().upper()
        for g in (p.get("games") or []):
            if starts_only and not int(g.get("games_started") or 0):
                continue
            ip = float(g.get("IP") or 0.0)
            outs = round(ip * 3)
            date = g.get("date") or ""
            commence = _commence_iso(date)
            if not commence:
                continue
            stats = {
                "K":    int(g.get("K")  or 0),
                "ER":   int(g.get("ER") or 0),
                "H":    int(g.get("H")  or 0),
                "BB":   int(g.get("BB") or 0),
                "outs": outs,
            }
            for market, (stat_key, default_line) in _MARKETS.items():
                actual = stats[stat_key]
                line = default_line
                side = "Over" if actual > line else "Under"
                # win/loss determination: actual vs line; ties (half-integer
                # lines exclude ties) are decided by the side label.
                if actual > line:
                    result = "win" if side == "Over" else "loss"
                elif actual < line:
                    result = "win" if side == "Under" else "loss"
                else:
                    result = "push"
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

    _log(f"emitted {len(bets)} synthetic bets for season {season}")
    return bets


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season",  type=int, default=2025,
                    help="Season to synthesize bets from (default: 2025)")
    ap.add_argument("--output",  type=Path,
                    default=_CACHE_DIR / "synthetic_pitcher_bets.json",
                    help="Output JSON path")
    ap.add_argument("--all-appearances", action="store_true",
                    help="Include relief appearances (default: starts only)")
    args = ap.parse_args()

    bets = build_bets(args.season, starts_only=not args.all_appearances)
    if not bets:
        _log("no bets generated -- exiting")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"bets": bets}, ensure_ascii=False),
        encoding="utf-8",
    )
    _log(f"wrote {args.output} ({args.output.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
