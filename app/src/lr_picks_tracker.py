"""
Silent background record of every Logistic Regression prediction the
ensemble makes — moneyline and run-line, MLB or WNBA, regardless of
whether the ensemble agreed with the LR or overruled it.

This file is deliberately decoupled from the ensemble output and the bet
ledger.  It exists only so we can measure how the LR component is doing
on its own, separately from the consensus pick.

Storage
-------
.cache/lr_picks_history.json
    {
      "version": 1,
      "picks": [
        {
          "pick_id":       "<sport>_<date>_<away>_at_<home>_<bet_type>",
          "game_id":       "<odds-api game id when known, else synthesised>",
          "sport":         "MLB" | "WNBA",
          "date":          "YYYY-MM-DD",
          "matchup":       "<away> @ <home>",
          "home_team":     "<home full name>",
          "away_team":     "<away full name>",
          "bet_type":      "moneyline" | "run_line",
          "lr_pick":       "home" | "away",
          "lr_prob_home":  <raw P(home wins / covers) from LR>,
          "lr_confidence": <max(lr_prob_home, 1 - lr_prob_home), >= 0.5>,
          "recorded_at":   "<UTC ISO timestamp>",
          "outcome":       null | "home" | "away",     # filled by settle_pending
          "correct":       null | true | false,        # filled by settle_pending
          "settled_at":    null | "<UTC ISO timestamp>",
        },
        ...
      ]
    }

Public API
----------
record_lr_pick(...)         called from BettingModel.predict + RunLineModel.predict
settle_pending(odds_client, sport_key, days_from=3) -> int
get_lr_accuracy_stats(history_path=None) -> dict

The recorder is best-effort: any I/O exception is swallowed so a failed
record never breaks live prediction.
"""
from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_HISTORY_PATH = Path(".cache/lr_picks_history.json")
_SCHEMA_VERSION = 1

# Confidence tier boundaries (inclusive lower, exclusive upper).
# A pick whose lr_confidence < 0.50 cannot happen by construction
# (lr_confidence is the prob assigned to the chosen side).
_TIER_BOUNDARIES = [
    ("low",    0.50, 0.55),
    ("medium", 0.55, 0.65),
    ("high",   0.65, 1.01),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(s: str) -> str:
    s = (s or "").strip().lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9\-]+", "", s)


def _make_pick_id(sport: str, date: str, away: str, home: str, bet_type: str) -> str:
    return f"{_slugify(sport)}_{date}_{_slugify(away)}_at_{_slugify(home)}_{bet_type}"


# Supabase durability (FIX 3): mirror writes + restore once on first read so
# Railway redeploys don't revert graded picks to the git-committed snapshot.
# Key contains "history" so the daily cache cleaner never purges it.
_SUPA_KEY = "lr_picks_history"
_restored_from_supabase = False


def _restore_once(path: Path) -> None:
    global _restored_from_supabase
    if _restored_from_supabase:
        return
    _restored_from_supabase = True
    try:
        from . import db
        if not db.is_supabase():
            return
        row = db.cache_get(_SUPA_KEY)
        if not isinstance(row, dict):
            return
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        if isinstance(data, dict) and isinstance(data.get("picks"), list):
            _save(data, path, _mirror=False)   # overwrite stale local copy
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


def _mirror_to_supabase(d: dict) -> None:
    try:
        from . import db
        if not db.is_supabase():
            return
        today = datetime.now(timezone.utc).date().isoformat()
        db.cache_set(_SUPA_KEY, None, today, d)
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


def _load(path: Path = _HISTORY_PATH) -> dict:
    if path == _HISTORY_PATH:
        _restore_once(path)
    if not path.exists():
        return {"version": _SCHEMA_VERSION, "picks": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if "picks" not in d or not isinstance(d.get("picks"), list):
            return {"version": _SCHEMA_VERSION, "picks": []}
        return d
    except Exception:
        # Corrupt file — start fresh rather than abort.
        return {"version": _SCHEMA_VERSION, "picks": []}


def _save(d: dict, path: Path = _HISTORY_PATH, *, _mirror: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(path)
    if _mirror and path == _HISTORY_PATH:
        _mirror_to_supabase(d)


# ── recording ─────────────────────────────────────────────────────────────────

def record_lr_pick(
    *,
    sport:        str,
    home_team:    str,
    away_team:    str,
    game_date:    str,
    bet_type:     str,
    lr_prob_home: float,
    game_id:      Optional[str] = None,
    history_path: Path = _HISTORY_PATH,
) -> None:
    """
    Append (or refresh) the LR's individual pick for one game/bet_type.

    Idempotent by pick_id = (sport, date, away, home, bet_type). If the
    same pick is re-recorded before settlement, the lr_prob_home and
    lr_confidence fields are updated to the latest value but the entry's
    pick_id and recorded_at stay stable. Already-settled entries are not
    overwritten.

    Wrapped in a broad try/except so any failure (disk, JSON, malformed
    inputs) is silently swallowed — recording must never affect live
    prediction output.
    """
    try:
        if lr_prob_home is None:
            return
        # Coerce to plain float so numpy scalars don't poison JSON
        prob_home = float(lr_prob_home)
        conf      = max(prob_home, 1.0 - prob_home)
        pick      = "home" if prob_home >= 0.5 else "away"
        date_str  = (game_date or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        pick_id   = _make_pick_id(sport, date_str, away_team, home_team, bet_type)

        store = _load(history_path)
        existing = next((p for p in store["picks"] if p["pick_id"] == pick_id), None)

        if existing and existing.get("settled_at"):
            # Don't disturb settled history
            return

        if existing is None:
            entry: dict[str, Any] = {
                "pick_id":      pick_id,
                "game_id":      game_id,
                "sport":        sport,
                "date":         date_str,
                "matchup":      f"{away_team} @ {home_team}",
                "home_team":    home_team,
                "away_team":    away_team,
                "bet_type":     bet_type,
                "lr_pick":      pick,
                "lr_prob_home": prob_home,
                "lr_confidence": conf,
                "recorded_at":  _utc_now_iso(),
                "outcome":      None,
                "correct":      None,
                "settled_at":   None,
            }
            store["picks"].append(entry)
        else:
            existing.update({
                "game_id":       game_id or existing.get("game_id"),
                "lr_pick":       pick,
                "lr_prob_home":  prob_home,
                "lr_confidence": conf,
                "recorded_at":   _utc_now_iso(),
            })

        _save(store, history_path)
    except Exception:
        # Silent record — never propagate
        pass


def record_lr_pick_totals(
    *,
    sport:           str,
    home_team:       str,
    away_team:       str,
    game_date:       str,
    predicted_total: float,
    market_line:     float,
    game_id:         Optional[str] = None,
    history_path:    Path = _HISTORY_PATH,
) -> None:
    """LR-only recorder for totals (over/under) predictions.

    Stores in the same history file as moneyline + run_line picks, with
    bet_type="totals", lr_pick in {"over","under"}, and a `line` field
    that settle_lr_pick uses to compute correctness.  Same idempotent
    (sport,date,away,home,bet_type) pick_id keying as the other
    record_*  functions in this module.
    """
    try:
        if predicted_total is None or market_line is None:
            return
        pt = float(predicted_total)
        ln = float(market_line)
        pick = "over" if pt > ln else "under"
        date_str = (game_date or "")[:10] or datetime.now(timezone.utc).date().isoformat()
        pick_id  = _make_pick_id(sport, date_str, away_team, home_team, "totals")

        store = _load(history_path)
        existing = next((p for p in store["picks"] if p["pick_id"] == pick_id), None)
        if existing and existing.get("settled_at"):
            return

        if existing is None:
            entry: dict[str, Any] = {
                "pick_id":            pick_id,
                "game_id":            game_id,
                "sport":              sport,
                "date":               date_str,
                "matchup":            f"{away_team} @ {home_team}",
                "home_team":          home_team,
                "away_team":          away_team,
                "bet_type":           "totals",
                "lr_pick":            pick,
                "lr_predicted_total": pt,
                "line":               ln,
                "recorded_at":        _utc_now_iso(),
                "outcome":            None,
                "correct":            None,
                "settled_at":         None,
            }
            store["picks"].append(entry)
        else:
            existing.update({
                "game_id":            game_id or existing.get("game_id"),
                "lr_pick":            pick,
                "lr_predicted_total": pt,
                "line":               ln,
                "recorded_at":        _utc_now_iso(),
            })

        _save(store, history_path)
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# ── settlement ────────────────────────────────────────────────────────────────

def _decide_correct(
    bet_type: str, lr_pick: str, home_runs: int, away_runs: int,
    line: Optional[float] = None,
) -> tuple[str, bool]:
    """Return (outcome_side, lr_correct) for one finished game.

    bet_type:
      moneyline  -> outcome in {"home","away"}, picker side matches the
                    winning side
      run_line   -> outcome in {"home","away"}, home covers -1.5 iff
                    margin > 1.5
      totals     -> outcome in {"over","under","push"}, decided by
                    (home+away) vs `line` (caller passes it from the
                    stored entry's "line" field)
    """
    margin = home_runs - away_runs
    if bet_type == "run_line":
        outcome = "home" if margin > 1.5 else "away"
        return outcome, (lr_pick == outcome)
    if bet_type == "totals":
        if line is None:
            # Shouldn't happen -- totals entries are written with `line`.
            # Mark settle correctness False so the audit log surfaces it.
            return ("push", False)
        total = home_runs + away_runs
        if   total >  line: outcome = "over"
        elif total <  line: outcome = "under"
        else:               outcome = "push"
        return outcome, (lr_pick == outcome)
    # Moneyline (or anything else): winner of the game.
    outcome = "home" if margin > 0 else "away"
    return outcome, (lr_pick == outcome)


def settle_pending(
    odds_client,
    sport_key:    str,
    days_from:    int = 3,
    history_path: Path = _HISTORY_PATH,
) -> int:
    """
    Walk every unsettled entry and fill in outcome + correct using the
    Odds API scores feed. Mirrors the matching logic in BetLedger.settle:
    score_map keyed by game_id, requires score['completed'].

    Returns the count of entries newly settled.
    """
    store = _load(history_path)
    pending = [p for p in store["picks"] if not p.get("settled_at")]
    if not pending:
        return 0

    try:
        scores = odds_client.get_scores(sport_key=sport_key, days_from=days_from)
    except Exception:
        return 0

    score_map = {s["id"]: s for s in scores if s.get("id")}
    newly = 0

    for entry in pending:
        gid = entry.get("game_id")
        if not gid or gid not in score_map:
            continue
        score = score_map[gid]
        if not score.get("completed") or not score.get("scores"):
            continue
        try:
            tally     = {s["name"]: int(float(s["score"])) for s in score["scores"]}
            home_name = score.get("home_team")
            away_name = next(n for n in tally if n != home_name)
            home_runs = tally[home_name]
            away_runs = tally[away_name]
        except Exception:
            continue

        outcome, correct = _decide_correct(
            entry["bet_type"], entry["lr_pick"], home_runs, away_runs,
            line=entry.get("line"),
        )
        entry["outcome"]    = outcome
        entry["correct"]    = bool(correct)
        entry["settled_at"] = _utc_now_iso()
        newly += 1

    if newly:
        _save(store, history_path)
    return newly


def settle_manual(
    *,
    pick_id:      str,
    home_runs:    int,
    away_runs:    int,
    history_path: Path = _HISTORY_PATH,
) -> bool:
    """
    Manually settle one pick from a known final score. Useful for tests
    or backfilling outcomes the odds API doesn't return. Returns True
    when an unsettled pick with matching id is found and updated.
    """
    store = _load(history_path)
    for entry in store["picks"]:
        if entry["pick_id"] == pick_id and not entry.get("settled_at"):
            outcome, correct = _decide_correct(
                entry["bet_type"], entry["lr_pick"], int(home_runs), int(away_runs)
            )
            entry["outcome"]    = outcome
            entry["correct"]    = bool(correct)
            entry["settled_at"] = _utc_now_iso()
            _save(store, history_path)
            return True
    return False


# ── stats ─────────────────────────────────────────────────────────────────────

def _tier_for_confidence(conf: float) -> str:
    for name, lo, hi in _TIER_BOUNDARIES:
        if lo <= conf < hi:
            return name
    return "low"


def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    k = sum(1 for r in rows if r.get("correct"))
    return {
        "n":        n,
        "correct":  k,
        "accuracy": (k / n) if n else None,
    }


def get_lr_accuracy_stats(history_path: Path = _HISTORY_PATH) -> dict:
    """
    Read the LR pick history and return overall / by-bet-type / by-tier
    accuracy. Only settled entries (correct is True or False) are counted.

    Returned shape:
        {
          "overall":            {"n": int, "correct": int, "accuracy": float|None},
          "by_bet_type":        {"moneyline": {...}, "run_line": {...}},
          "by_confidence_tier": {"low": {...}, "medium": {...}, "high": {...}},
          "pending":            int,   # unsettled picks
        }
    """
    store = _load(history_path)
    picks = store.get("picks", [])
    settled = [p for p in picks if p.get("correct") is not None]
    pending = sum(1 for p in picks if p.get("correct") is None)

    by_bet: dict[str, list[dict]] = {}
    by_tier: dict[str, list[dict]] = {name: [] for name, _, _ in _TIER_BOUNDARIES}

    for p in settled:
        by_bet.setdefault(p.get("bet_type", "unknown"), []).append(p)
        tier = _tier_for_confidence(float(p.get("lr_confidence", 0.5)))
        by_tier[tier].append(p)

    return {
        "overall":            _summary(settled),
        "by_bet_type":        {bt: _summary(rows) for bt, rows in by_bet.items()},
        "by_confidence_tier": {t:  _summary(rows) for t, rows in by_tier.items()},
        "pending":            pending,
    }


def format_accuracy_report(stats: Optional[dict] = None,
                           history_path: Path = _HISTORY_PATH) -> str:
    """
    Pretty-print the accuracy stats as a multi-line string suitable for
    console output. Pass an existing stats dict to skip the file read.
    """
    s = stats if stats is not None else get_lr_accuracy_stats(history_path)
    o = s["overall"]
    lines = ["LR individual pick accuracy"]
    if o["n"] == 0:
        lines.append(f"  No settled picks yet ({s['pending']} pending).")
        return "\n".join(lines)

    lines.append(
        f"  Overall: {o['correct']}/{o['n']} = {o['accuracy']:.1%}  "
        f"({s['pending']} pending)"
    )
    lines.append("  By bet type:")
    for bt, sub in s["by_bet_type"].items():
        if sub["n"] == 0:
            continue
        lines.append(
            f"    {bt:<10} {sub['correct']:>4}/{sub['n']:<4} = "
            f"{sub['accuracy']:.1%}"
        )
    lines.append("  By confidence tier:")
    for tier in ("low", "medium", "high"):
        sub = s["by_confidence_tier"].get(tier, {"n": 0, "correct": 0, "accuracy": None})
        if sub["n"] == 0:
            continue
        lo, hi = next((l, h) for name, l, h in _TIER_BOUNDARIES if name == tier)
        rng = f"[{lo:.2f}-{min(hi, 1.0):.2f})"
        lines.append(
            f"    {tier:<7} {rng:<14} {sub['correct']:>4}/{sub['n']:<4} = "
            f"{sub['accuracy']:.1%}"
        )
    return "\n".join(lines)


def settle_lr_pick(
    game_id:      str,
    home_score:   int,
    away_score:   int,
    history_path: Path = _HISTORY_PATH,
) -> int:
    """
    Settle all pending LR picks for *game_id* using the known final scores.

    This is the ledger-integration entry point: call it after a bet is
    auto-settled so the individual LR history file stays in sync.

    Returns the number of picks newly settled.
    """
    try:
        store = _load(history_path)
        pending = [p for p in store["picks"]
                   if p.get("game_id") == str(game_id) and not p.get("settled_at")]
        if not pending:
            return 0

        hs  = int(home_score)
        as_ = int(away_score)
        newly = 0
        for entry in pending:
            outcome, correct = _decide_correct(
                entry.get("bet_type", "moneyline"), entry["lr_pick"], hs, as_,
                line=entry.get("line"),
            )
            entry["outcome"]    = outcome
            entry["correct"]    = bool(correct)
            entry["settled_at"] = _utc_now_iso()
            newly += 1

        if newly:
            _save(store, history_path)
        return newly
    except Exception:
        return 0
