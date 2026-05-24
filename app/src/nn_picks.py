"""
Silent NN-only pick tracking.

Records every Neural Network prediction (across moneyline, run-line, and
totals models) to data/nn_picks_history.json so we can measure how the NN
performs on its own — independent of the ensemble vote, agreement state,
or whether the user actually placed the bet.

This module does NOT influence:
  * the ensemble probability returned by *.predict()
  * models_agree
  * the bet ledger
  * any displayed pick

It is a side-channel observability log of the NN's individual opinions.
File writes are best-effort: failures are swallowed so logging never
breaks a prediction.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

HISTORY_PATH    = Path("data/nn_picks_history.json")
_SCHEMA_VERSION = 1

# Confidence tiers, where confidence = 2*|prob - 0.5| ∈ [0, 1].
# low    : prob in [0.50, 0.55] (under-confident)
# medium : prob in (0.55, 0.65]
# high   : prob >  0.65
_TIER_LOW_MAX = 0.10
_TIER_MED_MAX = 0.30


# ── Disk I/O ────────────────────────────────────────────────────────────────

# Supabase durability (FIX 3): mirror every write to app_cache and restore
# from it once on first read, so Railway redeploys don't revert graded picks
# to the git-committed snapshot.  Key contains "history" so the daily cache
# cleaner never purges it.
_SUPA_KEY = "nn_picks_history"
_restored_from_supabase = False


def _restore_once() -> None:
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
            # Supabase is the durable source of truth -- overwrite the (possibly
            # stale, git-committed) local file with it.
            _save_atomic(data, _mirror=False)
    except Exception:                                                       # noqa: BLE001
        pass


def _mirror_to_supabase(data: dict) -> None:
    try:
        from . import db
        if not db.is_supabase():
            return
        today = time.strftime("%Y-%m-%d", time.gmtime())
        db.cache_set(_SUPA_KEY, None, today, data)
    except Exception:                                                       # noqa: BLE001
        pass


def _load() -> dict:
    _restore_once()
    if not HISTORY_PATH.exists():
        return {"version": _SCHEMA_VERSION, "picks": []}
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "picks" not in data:
            return {"version": _SCHEMA_VERSION, "picks": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": _SCHEMA_VERSION, "picks": []}


def _save_atomic(data: dict, *, _mirror: bool = True) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".nn_picks_", suffix=".json", dir=HISTORY_PATH.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, HISTORY_PATH)
    except OSError:
        try: os.unlink(tmp_path)
        except OSError: pass
        raise
    if _mirror:
        _mirror_to_supabase(data)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _pick_id(game_date: str, matchup: str, bet_type: str) -> str:
    """Stable identifier for upsert.  Replaces spaces and @ for filesystem-safety."""
    safe = matchup.replace(" ", "_").replace("@", "at")
    return f"{game_date}_{safe}_{bet_type}"


def _tier(confidence: float) -> str:
    if confidence <= _TIER_LOW_MAX: return "low"
    if confidence <= _TIER_MED_MAX: return "medium"
    return "high"


def _tier_label(tier: str) -> str:
    if tier == "low":    return "prob 0.50-0.55"
    if tier == "medium": return "prob 0.55-0.65"
    return "prob > 0.65"


# ── Public API ──────────────────────────────────────────────────────────────

def record_nn_pick(
    *,
    game_date:     str,
    matchup:       str,
    sport:         str,
    bet_type:      str,
    nn_prob:       float,
    nn_pick:       str,
    nn_confidence: Optional[float] = None,
    extra:         Optional[dict]  = None,
) -> bool:
    """
    Append (or upsert) a single NN pick.  Returns True on successful write.

    nn_prob is the post-clip calibrated probability the NN emitted.  For
    moneyline this is the home-win probability; for totals it is the predicted
    total runs; for run-line it is the cover probability.

    nn_pick is the discrete label the NN would have chosen on its own
    ("home"/"away" for moneyline & run-line, "over"/"under" for totals).

    nn_confidence defaults to 2*|nn_prob - 0.5| when not provided (correct
    for probability bet types; pass an explicit value for totals where the
    raw prediction is not a probability).

    Idempotent: re-recording the same (game_date, matchup, bet_type)
    overwrites the prior prediction fields but preserves any prior
    settlement state (so historical picks don't lose their outcome when
    predictions are re-run live).
    """
    if nn_confidence is None:
        nn_confidence = 2.0 * abs(float(nn_prob) - 0.5)

    pid = _pick_id(game_date, matchup, bet_type)
    data  = _load()
    picks = data.setdefault("picks", [])
    by_id = {p.get("pick_id"): i for i, p in enumerate(picks)}

    entry = {
        "pick_id":        pid,
        "recorded_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_date":      game_date,
        "matchup":        matchup,
        "sport":          sport,
        "bet_type":       bet_type,
        "nn_pick":        nn_pick,
        "nn_prob":        round(float(nn_prob), 6),
        "nn_confidence":  round(float(nn_confidence), 6),
        "settled":        False,
        "actual_outcome": None,
        "correct":        None,
    }
    if extra:
        entry["extra"] = extra

    if pid in by_id:
        prior = picks[by_id[pid]]
        if prior.get("settled"):
            entry["settled"]        = True
            entry["actual_outcome"] = prior.get("actual_outcome")
            entry["correct"]        = prior.get("correct")
        picks[by_id[pid]] = entry
    else:
        picks.append(entry)

    data["version"] = _SCHEMA_VERSION
    try:
        _save_atomic(data)
        return True
    except OSError:
        return False


def settle_nn_pick(
    *,
    game_date:      str,
    matchup:        str,
    bet_type:       str,
    actual_outcome: str,
) -> bool:
    """
    Mark a previously-recorded NN pick as settled.

    Correctness is computed by comparing actual_outcome to the stored
    nn_pick — both should use the same vocabulary ("home"/"away",
    "over"/"under", etc.).

    Returns True when an existing entry was found and updated; False when
    no matching pick exists.
    """
    pid   = _pick_id(game_date, matchup, bet_type)
    data  = _load()
    for entry in data.get("picks", []):
        if entry.get("pick_id") == pid:
            entry["settled"]        = True
            entry["actual_outcome"] = actual_outcome
            entry["correct"]        = bool(entry.get("nn_pick") == actual_outcome)
            try:
                _save_atomic(data)
            except OSError:
                return False
            return True
    return False


def settle_completed_games(completed_games: list[dict]) -> int:
    """Bulk-grade pending NN picks against completed games.

    NN picks are keyed by (game_date, matchup) -- not game_id -- so each
    completed game must carry game_date + home_team/away_team + scores.
    Grades moneyline (by final score), run_line/spread (default ±1.5:
    home covers iff margin >= 2), and totals (only when the pick stored a
    `line`).  Writes correct/settled back.  Returns count newly settled.
    """
    if not completed_games:
        return 0
    idx: dict[tuple, dict] = {}
    for g in completed_games:
        gd = (g.get("game_date") or "")[:10]
        mu = f"{g.get('away_team')} @ {g.get('home_team')}"
        if gd and g.get("home_score") is not None and g.get("away_score") is not None:
            idx[(gd, mu)] = g
    if not idx:
        return 0

    data  = _load()
    newly = 0
    for p in data.get("picks", []):
        if p.get("settled"):
            continue
        g = idx.get((p.get("game_date"), p.get("matchup")))
        if not g:
            continue
        try:
            hs = int(g["home_score"]); as_ = int(g["away_score"])
        except (TypeError, ValueError):
            continue
        bt = (p.get("bet_type") or "moneyline").lower()
        outcome = None
        if bt == "moneyline":
            outcome = "home" if hs > as_ else ("away" if as_ > hs else None)
        elif bt in ("run_line", "spread"):
            outcome = "home" if (hs - as_) >= 2 else "away"
        elif bt == "totals":
            line = p.get("line")
            if line is None and isinstance(p.get("extra"), dict):
                line = p["extra"].get("line")
            if line is None:
                continue                       # can't grade O/U without the line
            total = hs + as_
            if abs(total - float(line)) < 1e-9:
                continue                       # push -- leave for explicit handling
            outcome = "over" if total > float(line) else "under"
        if outcome is None:
            continue
        p["settled"]        = True
        p["actual_outcome"] = outcome
        p["correct"]        = bool(p.get("nn_pick") == outcome)
        newly += 1

    if newly:
        _save_atomic(data)
    return newly


def compute_nn_accuracy() -> dict:
    """
    Return accuracy stats across all settled NN picks:

        {
          "total_settled":      N,
          "overall_correct":    K,
          "overall_accuracy":   K/N,
          "by_bet_type": {
            "<bet_type>": {"correct": k, "total": n, "accuracy": k/n},
            ...
          },
          "by_confidence_tier": {
            "low" | "medium" | "high": {
              "correct": k, "total": n, "accuracy": k/n, "prob_range": "..."
            }
          }
        }

    Unsettled picks are excluded entirely.  When a category has zero
    settled picks, its "accuracy" field is None.
    """
    data  = _load()
    picks = [p for p in data.get("picks", []) if p.get("settled")]
    total = len(picks)

    by_type: dict[str, list[int]] = {}            # bet_type -> [correct, total]
    by_tier: dict[str, list[int]] = {
        "low": [0, 0], "medium": [0, 0], "high": [0, 0]
    }
    correct = 0

    for p in picks:
        ok = bool(p.get("correct"))
        correct += int(ok)
        bt = str(p.get("bet_type", "unknown"))
        by_type.setdefault(bt, [0, 0])
        by_type[bt][0] += int(ok)
        by_type[bt][1] += 1
        tier = _tier(float(p.get("nn_confidence", 0.0)))
        by_tier[tier][0] += int(ok)
        by_tier[tier][1] += 1

    def _pct(c: int, n: int) -> Optional[float]:
        return (c / n) if n else None

    return {
        "total_settled":    total,
        "overall_correct":  correct,
        "overall_accuracy": _pct(correct, total),
        "by_bet_type": {
            bt: {"correct": c, "total": n, "accuracy": _pct(c, n)}
            for bt, (c, n) in sorted(by_type.items())
        },
        "by_confidence_tier": {
            tier: {
                "correct":    c,
                "total":      n,
                "accuracy":   _pct(c, n),
                "prob_range": _tier_label(tier),
            }
            for tier, (c, n) in by_tier.items()
        },
    }


def format_accuracy_report(stats: Optional[dict] = None) -> str:
    """Render compute_nn_accuracy() output as a one-shot text report."""
    s = stats if stats is not None else compute_nn_accuracy()
    n = s["total_settled"]
    if n == 0:
        return "NN pick history: no settled picks yet."

    lines = []
    acc = s["overall_accuracy"]
    lines.append(
        f"NN individual accuracy: {s['overall_correct']}/{n} = "
        f"{acc:.1%} (settled picks only)"
    )
    lines.append("")
    lines.append("By bet type:")
    for bt, row in s["by_bet_type"].items():
        a = row["accuracy"]
        a_s = f"{a:.1%}" if a is not None else "—"
        lines.append(f"  {bt:<12} {row['correct']:>3}/{row['total']:<3}  {a_s}")
    lines.append("")
    lines.append("By confidence tier:")
    for tier in ("low", "medium", "high"):
        row = s["by_confidence_tier"][tier]
        a = row["accuracy"]
        a_s = f"{a:.1%}" if a is not None else "—"
        lines.append(
            f"  {tier:<6} ({row['prob_range']:<18}) "
            f"{row['correct']:>3}/{row['total']:<3}  {a_s}"
        )
    return "\n".join(lines)
