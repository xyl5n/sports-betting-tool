"""
top_plays_tracker.py
====================
A standalone scorecard for the Top Plays (/top-picks) tab — completely
separate from model_picks tracking and the personal/model ledgers.  Its own
Supabase store, its own running totals.

Sizing model (FIXED reference, never a real balance)
----------------------------------------------------
Every Top Play is sized with normal Kelly against a FIXED $1000 reference
bankroll that NEVER changes.  The $1000 is only a denominator for turning a
pick into a stake; nothing is drawn down, added back, capped, or grown.

    1 unit = 1% of $1000 = $10.   stake_units = kelly_dollars / 10.

There is no spending limit and no running balance — total sized bets may
exceed $1000 and that is expected.  The only running totals are cumulative
units won/lost and the W/L record.

Freezing
--------
When a play first appears in Top Plays it is recorded once with its
Kelly-sized unit stake and its American odds FROZEN.  Re-rankings or line
moves never recalculate an already-recorded play (dedup by a deterministic
id).

Settlement
----------
Driven by the existing 15-minute cycle (12 PM–1 AM ET window) once the
underlying game/prop finishes.  Grading reuses model_picks._grade_game /
_grade_prop against the same final scores / player-stat lookup the rest of
settlement uses.  On a win we add the profit in units computed from the
frozen American odds on the frozen unit stake; on a loss we subtract the
staked units; void/push is no change.  Win% + W/L come from settled plays
only.

Storage: a single app_cache row keyed "top_plays_tracker_history" (the
"history" substring exempts it from the daily ET-rollover cache cleaner, so
the cumulative scorecard survives day boundaries and redeploys) plus a local
file mirror.  PostgREST only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_CACHE_KEY  = "top_plays_tracker_history"   # contains "history" -> survives rollover
_LOCAL_PATH = Path(".cache/top_plays_tracker.json")

_REF_BANKROLL = 1000.0     # fixed denominator -- never changes, never depleted
_UNIT_DOLLARS = 10.0       # 1 unit = 1% of the $1000 reference

_plays: list[dict] = []
_index: set[str] = set()
_loaded = False


def _log(msg: str) -> None:
    print(f"TOP-PLAYS: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Persistence ──────────────────────────────────────────────────────────────

def _load_from_supabase() -> Optional[list[dict]]:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_CACHE_KEY)
        if not isinstance(row, dict):
            return None
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        plays = data.get("plays") if isinstance(data, dict) else None
        return plays if isinstance(plays, list) else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase load failed: {exc}")
        return None


def _load_from_file() -> Optional[list[dict]]:
    try:
        if _LOCAL_PATH.exists():
            data = json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
            plays = data.get("plays") if isinstance(data, dict) else None
            return plays if isinstance(plays, list) else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local load failed: {exc}")
    return None


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    plays = _load_from_supabase()
    if plays is None:
        plays = _load_from_file()
    _plays.clear()
    _plays.extend(plays or [])
    _index.clear()
    _index.update(p.get("id") for p in _plays if p.get("id"))
    _loaded = True


def reload() -> None:
    """Force a re-read from Supabase (the scheduler + page share one
    process, but a redeploy starts cold)."""
    global _loaded
    _loaded = False
    _ensure_loaded()


def _save() -> None:
    payload = {"plays": _plays, "updated_at": _now_iso()}
    try:
        _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local save failed: {exc}")
    try:
        from . import db
        if db.is_supabase():
            # Stamp date=today so the row reads as fresh; the "history" key
            # keeps the daily cleaner from purging it.
            db.cache_set(_CACHE_KEY, None, _today_et(), payload)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase save failed: {exc}")


# ── Sizing ─────────────────────────────────────────────────────────────────

def _stake_units(prob, odds) -> float:
    """Kelly stake (in units) off the FIXED $1000 reference.  Returns 0.0
    when Kelly declines the bet or inputs are unusable."""
    try:
        from .kelly import tracked_bet_kelly
        dollars, _flag = tracked_bet_kelly(float(prob), int(odds), _REF_BANKROLL)
        return round(float(dollars) / _UNIT_DOLLARS, 3)
    except Exception:                                                     # noqa: BLE001
        return 0.0


def _profit_units(stake_units: float, odds) -> float:
    """Profit in units on a winning play at frozen American *odds*."""
    try:
        o = float(odds)
        s = float(stake_units)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return round(s * (o / 100.0) if o > 0 else s * (100.0 / abs(o)), 3)


# ── Recording (freeze on first appearance) ───────────────────────────────────

def _play_id(tr: dict, date_str: str) -> str:
    if (tr.get("kind") or "") == "prop":
        return (f"{date_str}:prop:{tr.get('player')}:{tr.get('bet_type')}:"
                f"{tr.get('pick_side')}:{tr.get('line')}")
    return f"{date_str}:game:{tr.get('game_id')}:{tr.get('bet_type')}"


def record_plays(entries: list[dict]) -> int:
    """Record any newly-appearing Top Plays.  *entries* are the ranking rows;
    each carries a ``_track`` payload (built in src.top_picks).  Idempotent:
    a play already in the store is left untouched (stake + odds stay frozen).
    Returns the number of NEW plays recorded."""
    _ensure_loaded()
    date_str = _today_et()
    new = 0
    for r in entries or []:
        tr = r.get("_track")
        if not isinstance(tr, dict):
            continue
        odds = tr.get("odds")
        prob = tr.get("prob")
        if odds is None or not isinstance(prob, (int, float)):
            continue
        pid = _play_id(tr, date_str)
        if pid in _index:
            continue
        stake_units = _stake_units(prob, odds)
        if stake_units <= 0:
            continue                     # Kelly declined -> not a sized play
        _plays.append({
            "id":           pid,
            "date":         date_str,
            "kind":         tr.get("kind"),
            "sport":        tr.get("sport"),
            "name":         tr.get("name"),
            "pick_type":    tr.get("pick_type"),
            "side_display": tr.get("side_display"),
            # frozen grading + pricing
            "bet_type":     tr.get("bet_type"),
            "pick_side":    tr.get("pick_side"),
            "line":         tr.get("line"),
            "odds":         int(odds),
            "stake_units":  stake_units,
            "prob":         round(float(prob), 4),
            # settlement keys
            "game_id":      tr.get("game_id"),
            "event_id":     tr.get("event_id"),
            "player":       tr.get("player"),
            "home_team":    tr.get("home_team"),
            "away_team":    tr.get("away_team"),
            "commence_time": tr.get("commence_time"),
            # running state
            "result":       "pending",
            "profit_units": None,
            "recorded_at":  _now_iso(),
            "settled_at":   None,
        })
        _index.add(pid)
        new += 1
    if new:
        _save()
        _log(f"recorded {new} new play(s); {len(_plays)} tracked total")
    return new


# ── Settlement (15-min cycle) ─────────────────────────────────────────────────

def settle(final_scores: Optional[dict] = None,
           stat_lookup: Optional[Callable] = None) -> dict:
    """Grade every pending play against the same final scores / player stats
    the rest of settlement uses, apply the units movement once, and persist.
    Returns {settled, wins, losses, pushes}."""
    _ensure_loaded()
    final_scores = final_scores or {}
    from . import model_picks as _mp

    s = {"settled": 0, "wins": 0, "losses": 0, "pushes": 0}
    changed = False
    for p in _plays:
        if (p.get("result") or "pending") != "pending":
            continue
        pick = {
            "bet_type":  p.get("bet_type"),
            "pick_side": p.get("pick_side"),
            "line":      p.get("line"),
            "game_id":   p.get("game_id"),
        }
        if (p.get("kind") or "game") == "prop":
            if stat_lookup is None:
                continue
            try:
                actual = stat_lookup(p.get("player"), p.get("bet_type"))
            except Exception:                                             # noqa: BLE001
                actual = None
            result = _mp._grade_prop(pick, actual)
        else:
            sc = final_scores.get(p.get("game_id"))
            result = _mp._grade_game(pick, sc) if sc else None
        if not result:
            continue                     # not finished yet
        stake = float(p.get("stake_units") or 0.0)
        if result == "win":
            p["profit_units"] = _profit_units(stake, p.get("odds"))
            s["wins"] += 1
        elif result in ("push", "void"):
            result = "void"
            p["profit_units"] = 0.0
            s["pushes"] += 1
        else:
            result = "loss"
            p["profit_units"] = round(-stake, 3)
            s["losses"] += 1
        p["result"] = result
        p["settled_at"] = _now_iso()
        s["settled"] += 1
        changed = True
    if changed:
        _save()
        _log(f"settled {s['settled']} ({s['wins']}W/{s['losses']}L/"
             f"{s['pushes']}P); units={scorecard()['units']:+.2f}")
    return s


# ── Scorecard (UI) ────────────────────────────────────────────────────────────

def scorecard() -> dict:
    """Standalone Top Plays scorecard.  Win% + W/L from SETTLED plays only;
    units = cumulative profit/loss in units across settled plays."""
    _ensure_loaded()
    wins = losses = pushes = 0
    units = 0.0
    for p in _plays:
        res = (p.get("result") or "pending")
        if res == "win":
            wins += 1
        elif res == "loss":
            losses += 1
        elif res == "void":
            pushes += 1
        else:
            continue
        units += float(p.get("profit_units") or 0.0)
    decided = wins + losses
    win_pct = (wins / decided * 100.0) if decided else 0.0
    return {
        "wins":     wins,
        "losses":   losses,
        "pushes":   pushes,
        "settled":  wins + losses + pushes,
        "win_pct":  round(win_pct, 1),
        "units":    round(units, 2),
        "pending":  sum(1 for p in _plays if (p.get("result") or "pending") == "pending"),
    }
