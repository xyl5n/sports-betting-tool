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

# Mirror of TEAM_NAME_TO_ABBREV in train_props_models.py / props_model.py.
# Cached opp_team values are full team names ("Cincinnati Reds"), so we
# normalize to abbrev for the bet's home_team field.
_TEAM_NAME_TO_ABBREV: dict[str, str] = {
    "Arizona Diamondbacks":"ARI","Atlanta Braves":"ATL","Baltimore Orioles":"BAL",
    "Boston Red Sox":"BOS","Chicago Cubs":"CHC","Cincinnati Reds":"CIN",
    "Cleveland Guardians":"CLE","Colorado Rockies":"COL","Chicago White Sox":"CWS",
    "Detroit Tigers":"DET","Houston Astros":"HOU","Kansas City Royals":"KC",
    "Los Angeles Angels":"LAA","Los Angeles Dodgers":"LAD","Miami Marlins":"MIA",
    "Milwaukee Brewers":"MIL","Minnesota Twins":"MIN","New York Mets":"NYM",
    "New York Yankees":"NYY","Oakland Athletics":"OAK","Athletics":"OAK",
    "Philadelphia Phillies":"PHI","Pittsburgh Pirates":"PIT","San Diego Padres":"SD",
    "Seattle Mariners":"SEA","San Francisco Giants":"SF","St. Louis Cardinals":"STL",
    "Tampa Bay Rays":"TB","Texas Rangers":"TEX","Toronto Blue Jays":"TOR",
    "Washington Nationals":"WSH",
}


def _abbrev(team_str: str) -> str:
    if not team_str:
        return ""
    s = str(team_str).strip()
    if s in _TEAM_NAME_TO_ABBREV:
        return _TEAM_NAME_TO_ABBREV[s]
    upper = s.upper()
    # 3-letter abbrev passes through if it's a real team
    if upper in set(_TEAM_NAME_TO_ABBREV.values()):
        return upper
    return ""

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

    # PR6: roster map (built once via batter_teams.json) covers every active
    # MLB player including pitchers.  Used to recover the pitcher's team when
    # the cached training entry has team="" (most of 2025).
    roster_path = _CACHE_DIR / "batter_teams.json"
    roster: dict[str, str] = {}
    if roster_path.exists():
        try:
            roster = json.loads(roster_path.read_text())
        except Exception:
            roster = {}

    bets: list[dict] = []
    for p in pitchers:
        pid   = p.get("id")
        name  = p.get("name") or ""
        team  = (p.get("team") or "").strip().upper()
        if not team and pid is not None:
            team = (roster.get(f"{season}:{pid}") or "").strip().upper()
        for g in (p.get("games") or []):
            if starts_only and not int(g.get("games_started") or 0):
                continue
            ip = float(g.get("IP") or 0.0)
            outs = round(ip * 3)
            date = g.get("date") or ""
            commence = _commence_iso(date)
            if not commence:
                continue
            # PR6 backtest fidelity: derive real per-game park context.
            # is_home flag comes straight from the gameLog row; home_team
            # is the pitcher's team when is_home else opp_team (the
            # visiting team plays at the home team's stadium).  Both are
            # carried to the bet so backtest_props_model's _bet_to_prop_dict
            # can hand the right park-factor key to predict() rather than
            # forcing every bet to look like a home start.
            is_home   = bool(g.get("is_home"))
            opp_full  = g.get("opp_team") or ""
            opp_abbr  = _abbrev(opp_full)
            if is_home:
                home_team = team or ""
                away_team = opp_abbr
            else:
                home_team = opp_abbr
                away_team = team or ""
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
                    "home_team":      home_team,  # actual venue (PR6)
                    "away_team":      away_team,  # opposing batting team (PR6)
                    "is_home":        is_home,    # per-game flag (PR6)
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
