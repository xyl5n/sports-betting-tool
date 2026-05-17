"""
Silent background tracker for the XGBoost model's individual picks.

Records EVERY XGBoost prediction (moneyline, run line, totals) regardless of
whether the ensemble agreed or disagreed.  Never modifies the ensemble's
output -- purely a side-channel observability tool to evaluate XGBoost's
solo accuracy over time.

Persists to .cache/xgb_picks_history.json.  Schema:

    {
      "picks": [
        {
          "id":             "<game_id>_<bet_type>",   # dedupe key
          "game_id":        "<sportsbook id>",
          "sport":          "mlb" | "wnba",
          "bet_type":       "moneyline" | "run_line" | "totals",
          "date":           "YYYY-MM-DD",
          "matchup":        "Away @ Home",
          "home_team":      "...",
          "away_team":      "...",

          "pick":           "home" | "away" | "over" | "under",
          "pick_label":     "Boston Red Sox" | "Over 8.5" | ...,
          "xgb_prob":       float | None,    # P(home) for classifiers
          "xgb_confidence": float,           # in [0,1]: |2*prob - 1| or edge/anchor for totals
          "predicted_total": float | None,   # totals only
          "market_line":     float | None,   # totals only
          "recorded_at":     "YYYY-MM-DDTHH:MM:SSZ",

          # populated by settle_picks(...):
          "settled":         bool,
          "actual_home":     int | None,
          "actual_away":     int | None,
          "actual_total":    float | None,
          "result":          "win" | "loss" | "push" | None,
          "correct":         bool | None,
          "settled_at":      "...Z" | None,
        },
        ...
      ]
    }
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Anchor for converting totals edge (runs) to a 0-1 confidence so the same
# bucketing applies across all bet types: 2.5+ run edge == max confidence.
_TOTALS_EDGE_ANCHOR = 2.5

_PICKS_PATH = Path(".cache/xgb_picks_history.json")


# ── File I/O ─────────────────────────────────────────────────────────────────

def _load_history() -> dict:
    if not _PICKS_PATH.exists():
        return {"picks": []}
    try:
        return json.loads(_PICKS_PATH.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt or partial write -- start fresh rather than crash the app.
        return {"picks": []}


def _save_history(data: dict) -> None:
    """Atomic write: tmp -> os.replace, so a crash mid-write can't corrupt the file."""
    _PICKS_PATH.parent.mkdir(exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="xgb_picks_", suffix=".json.tmp",
        dir=str(_PICKS_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, _PICKS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        # Swallow -- tracker must never break prediction flow.


# ── Confidence bucketing (uniform across all bet types) ──────────────────────

def _confidence_tier(conf: float) -> str:
    """conf is the 0-1 scale stored on each pick."""
    if conf >= 0.30:  return "strong (65%+)"
    if conf >= 0.20:  return "confident (60-65%)"
    if conf >= 0.10:  return "lean (55-60%)"
    return "toss-up (50-55%)"


# ── Recording ────────────────────────────────────────────────────────────────

def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _game_date(game: dict) -> str:
    """Pull a YYYY-MM-DD date from the game dict's commence_time, or today."""
    ct = game.get("commence_time") or ""
    return ct[:10] if len(ct) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _sport_from_game(game: dict, default: str = "mlb") -> str:
    sport_key = (game.get("sport_key") or "").lower()
    if "mlb" in sport_key or "baseball" in sport_key:
        return "mlb"
    if "wnba" in sport_key or "basketball" in sport_key:
        return "wnba"
    return default


def _upsert(picks: list[dict], entry: dict) -> None:
    """Insert or update by dedupe id (latest prediction wins)."""
    for i, p in enumerate(picks):
        if p.get("id") == entry["id"]:
            # Preserve settlement fields if already settled.
            if p.get("settled"):
                for k in ("settled", "actual_home", "actual_away", "actual_total",
                          "result", "correct", "settled_at"):
                    entry[k] = p.get(k)
            picks[i] = entry
            return
    picks.append(entry)


def record_classifier_pick(
    *,
    bet_type:  str,                # "moneyline" | "run_line"
    game:      dict,
    xgb_prob:  float,              # P(home_win) or P(home_covers)
    sport:     Optional[str] = None,
) -> None:
    """Record an XGBoost classifier pick (home vs away).  Best-effort; swallows errors."""
    try:
        prob       = float(xgb_prob)
        pick_home  = prob >= 0.5
        pick       = "home" if pick_home else "away"
        confidence = abs(prob - 0.5) * 2.0

        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        game_id   = str(
            game.get("id") or game.get("game_id")
            or f"{_game_date(game)}_{away_team}_at_{home_team}"
        )
        sport_v   = (sport or _sport_from_game(game)).lower()

        entry = {
            "id":             f"{game_id}_{bet_type}",
            "game_id":        game_id,
            "sport":          sport_v,
            "bet_type":       bet_type,
            "date":           _game_date(game),
            "matchup":        f"{away_team} @ {home_team}",
            "home_team":      home_team,
            "away_team":      away_team,
            "pick":           pick,
            "pick_label":     home_team if pick_home else away_team,
            "xgb_prob":       prob,
            "xgb_confidence": confidence,
            "predicted_total": None,
            "market_line":     None,
            "recorded_at":    _now_z(),
            "settled":        False,
            "actual_home":    None,
            "actual_away":    None,
            "actual_total":   None,
            "result":         None,
            "correct":        None,
            "settled_at":     None,
        }
        data = _load_history()
        _upsert(data["picks"], entry)
        _save_history(data)
    except Exception:
        # Recording must never disturb prediction output.
        pass


def record_totals_pick(
    *,
    game:           dict,
    predicted_total: float,
    market_line:    float,
    sport:          Optional[str] = None,
) -> None:
    """Record an XGBoost totals (over/under) pick.  Best-effort; swallows errors."""
    try:
        pred = float(predicted_total)
        line = float(market_line)
        edge = pred - line                                # signed
        pick = "over" if edge > 0 else "under"            # ties go to under by convention
        # Map runs-of-edge to [0, 1] using the same scale as classifier confidence.
        confidence = min(abs(edge) / _TOTALS_EDGE_ANCHOR, 1.0)

        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        game_id   = str(
            game.get("id") or game.get("game_id")
            or f"{_game_date(game)}_{away_team}_at_{home_team}"
        )
        sport_v   = (sport or _sport_from_game(game)).lower()

        entry = {
            "id":             f"{game_id}_totals",
            "game_id":        game_id,
            "sport":          sport_v,
            "bet_type":       "totals",
            "date":           _game_date(game),
            "matchup":        f"{away_team} @ {home_team}",
            "home_team":      home_team,
            "away_team":      away_team,
            "pick":           pick,
            "pick_label":     f"{'Over' if pick == 'over' else 'Under'} {line}",
            "xgb_prob":       None,
            "xgb_confidence": confidence,
            "predicted_total": pred,
            "market_line":     line,
            "recorded_at":    _now_z(),
            "settled":        False,
            "actual_home":    None,
            "actual_away":    None,
            "actual_total":   None,
            "result":         None,
            "correct":        None,
            "settled_at":     None,
        }
        data = _load_history()
        _upsert(data["picks"], entry)
        _save_history(data)
    except Exception:
        pass


# ── Settlement ───────────────────────────────────────────────────────────────

def settle_picks(completed_games: list[dict]) -> int:
    """
    Update pending picks with actual outcomes.

    completed_games entries are dicts with whichever fields are known.  Matching
    is done by game_id.  Recognised fields:
        id            -- the game_id used at record time
        home_score    -- int
        away_score    -- int
        total_runs    -- float (optional; defaults to home+away)

    Returns the number of picks newly marked as settled.
    """
    if not completed_games:
        return 0

    # Build a quick lookup once.
    results_by_id: dict[str, dict] = {}
    for g in completed_games:
        gid = str(g.get("id") or "")
        if not gid:
            continue
        results_by_id[gid] = g

    data = _load_history()
    newly_settled = 0

    for p in data["picks"]:
        if p.get("settled"):
            continue
        result = results_by_id.get(p.get("game_id", ""))
        if not result:
            continue

        try:
            hs = result.get("home_score")
            as_ = result.get("away_score")
            if hs is None or as_ is None:
                continue
            hs = int(hs); as_ = int(as_)
            total = float(result.get("total_runs", hs + as_))

            bet_type = p.get("bet_type")
            pick     = p.get("pick")
            outcome  = None        # "win" | "loss" | "push"

            if bet_type == "moneyline":
                home_won = hs > as_
                if hs == as_:
                    outcome = "push"   # rare in baseball/basketball but theoretically possible
                elif (pick == "home" and home_won) or (pick == "away" and not home_won):
                    outcome = "win"
                else:
                    outcome = "loss"

            elif bet_type == "run_line":
                # Default run line: home -1.5 / away +1.5.  Home covers iff (hs - as_) >= 2.
                home_covers = (hs - as_) >= 2
                if (pick == "home" and home_covers) or (pick == "away" and not home_covers):
                    outcome = "win"
                else:
                    outcome = "loss"

            elif bet_type == "totals":
                line = p.get("market_line")
                if line is None:
                    continue
                if abs(total - float(line)) < 1e-9:
                    outcome = "push"
                else:
                    over_hit = total > float(line)
                    if (pick == "over" and over_hit) or (pick == "under" and not over_hit):
                        outcome = "win"
                    else:
                        outcome = "loss"
            else:
                continue

            p["settled"]      = True
            p["actual_home"]  = hs
            p["actual_away"]  = as_
            p["actual_total"] = total if bet_type == "totals" else None
            p["result"]       = outcome
            p["correct"]      = (outcome == "win") if outcome != "push" else None
            p["settled_at"]   = _now_z()
            newly_settled += 1
        except Exception:
            # One bad row shouldn't stop the rest.
            continue

    if newly_settled:
        _save_history(data)
    return newly_settled


# ── Accuracy reporting ───────────────────────────────────────────────────────

def _accuracy_bucket() -> dict:
    return {"correct": 0, "wrong": 0, "push": 0, "settled": 0, "accuracy": None}


def _finalize(bucket: dict) -> dict:
    denom = bucket["correct"] + bucket["wrong"]
    bucket["accuracy"] = (bucket["correct"] / denom) if denom > 0 else None
    return bucket


def get_xgb_accuracy() -> dict:
    """
    Return a structured breakdown of XGBoost's solo accuracy.

      {
        "total_picks":   int,
        "settled_picks": int,
        "pending_picks": int,
        "overall":            {correct, wrong, push, settled, accuracy},
        "by_bet_type":        {moneyline: {...}, run_line: {...}, totals: {...}},
        "by_confidence_tier": {"toss-up (50-55%)": {...}, ...},
      }

    Pushes are counted separately and excluded from accuracy denominators.
    """
    data = _load_history()
    picks = data.get("picks", [])

    overall = _accuracy_bucket()
    by_bet:  dict[str, dict] = {}
    by_conf: dict[str, dict] = {}

    settled = 0
    for p in picks:
        if not p.get("settled"):
            continue
        settled += 1
        outcome  = p.get("result")
        bet_type = p.get("bet_type", "unknown")
        tier     = _confidence_tier(float(p.get("xgb_confidence", 0.0)))

        bb = by_bet.setdefault(bet_type, _accuracy_bucket())
        bc = by_conf.setdefault(tier,    _accuracy_bucket())

        for bucket in (overall, bb, bc):
            bucket["settled"] += 1
            if outcome == "win":
                bucket["correct"] += 1
            elif outcome == "loss":
                bucket["wrong"]   += 1
            elif outcome == "push":
                bucket["push"]    += 1

    return {
        "total_picks":        len(picks),
        "settled_picks":      settled,
        "pending_picks":      len(picks) - settled,
        "overall":            _finalize(overall),
        "by_bet_type":        {k: _finalize(v) for k, v in by_bet.items()},
        "by_confidence_tier": {k: _finalize(v) for k, v in by_conf.items()},
    }


def format_xgb_accuracy_report(stats: Optional[dict] = None) -> str:
    """Human-readable accuracy report.  Pass a stats dict to skip the recompute."""
    s = stats or get_xgb_accuracy()
    lines: list[str] = []

    def _fmt_row(label: str, b: dict, label_width: int = 22) -> str:
        n = b["correct"] + b["wrong"]
        acc = f"{b['accuracy']:.1%}" if b["accuracy"] is not None else "  n/a "
        push = f"  pushes={b['push']}" if b["push"] else ""
        return f"  {label:<{label_width}}  {acc:>7}  ({b['correct']}/{n} settled){push}"

    lines.append(f"XGBoost individual picks: {s['total_picks']} total "
                 f"({s['settled_picks']} settled, {s['pending_picks']} pending)")
    lines.append("")
    lines.append("Overall:")
    lines.append(_fmt_row("all bet types", s["overall"]))

    if s["by_bet_type"]:
        lines.append("")
        lines.append("By bet type:")
        for bt in ("moneyline", "run_line", "totals"):
            if bt in s["by_bet_type"]:
                lines.append(_fmt_row(bt, s["by_bet_type"][bt]))
        for bt, b in s["by_bet_type"].items():
            if bt not in ("moneyline", "run_line", "totals"):
                lines.append(_fmt_row(bt, b))

    if s["by_confidence_tier"]:
        lines.append("")
        lines.append("By confidence tier:")
        for tier in ("toss-up (50-55%)", "lean (55-60%)",
                     "confident (60-65%)", "strong (65%+)"):
            if tier in s["by_confidence_tier"]:
                lines.append(_fmt_row(tier, s["by_confidence_tier"][tier], label_width=22))

    return "\n".join(lines)


def settle_xgb_pick(game_id: str, home_score: int, away_score: int) -> int:
    """
    Settle all pending XGBoost picks for *game_id* using the known final scores.

    This is the ledger-integration entry point: call it after a bet is
    auto-settled so the individual XGB history file stays in sync.

    Returns the number of picks newly marked as settled.
    """
    return settle_picks([{
        "id":         str(game_id),
        "home_score": int(home_score),
        "away_score": int(away_score),
    }])
