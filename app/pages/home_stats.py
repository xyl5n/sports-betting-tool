"""
Home-page stats helpers.

Three stat chips at the top of the home page need aggregated data from
both MLB + WNBA ledger history.  Each helper here scans the ledger
files once and returns a small dict the renderer can read directly --
keeps pages/home.py focused on layout.

All functions tolerate missing ledger files / empty history (return
zeros / None) so the home page always renders, even on a fresh deploy
with no settled bets yet.
"""
from __future__ import annotations

from typing import Iterable


# ── Bet-type label rendering (Section 1, chip #3) ───────────────────────────

_BET_TYPE_LABEL = {
    "single":    "Moneyline",
    "moneyline": "Moneyline",
    "run_line":  "Run Line",
    "runline":   "Run Line",
    "spread":    "Spread",
    "totals":    "Totals",
    "total":     "Totals",
}


def _bet_type_label(raw: str | None) -> str:
    return _BET_TYPE_LABEL.get((raw or "single").lower(), (raw or "—").title())


# ── History loader ─────────────────────────────────────────────────────────

def _all_history(backend) -> list[dict]:
    """Concatenate settled bet history from both ledger files.

    Reads via backend.Ledger(...).data['history'] so the source of
    truth is the same as the Model page + Sidebar Confidence Performance
    section -- everything stays in sync.
    """
    out: list[dict] = []
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
            for h in (led.data.get("history") or []):
                out.append(h)
        except Exception:                                                 # noqa: BLE001
            continue
    return out


# ── Chip #1 -- overall win rate ────────────────────────────────────────────

def overall_record(backend) -> dict:
    """Return {'wins': N, 'losses': N, 'pct': float | None}.

    Push / void results are excluded -- they don't move the W/L ratio.
    """
    history = _all_history(backend)
    w = sum(1 for h in history if (h.get("result") or "").lower() == "win")
    l = sum(1 for h in history if (h.get("result") or "").lower() == "loss")
    total = w + l
    return {
        "wins":   w,
        "losses": l,
        "pct":    (w / total) if total else None,
    }


# ── Model Performance section (bottom of home page) ───────────────────────

def model_performance(backend) -> dict:
    """Return aggregate settled-history performance for the model.

    Shape:
        {wins: N, losses: N, pct: float | None, units: float}

    `units` is a bankroll-independent P/L computed with flat 1U-per-bet
    sizing (i.e. the unit-tracking convention sports cappers use):

        win at +N        ->  +N/100  units
        win at -N        ->  +100/N  units
        loss             ->  -1      units
        push / void      ->   0      units (no W/L change either)

    Missing american_odds default to -110 (the most common line) so a
    pre-migration ledger entry without the field doesn't get silently
    omitted from the unit calc.  Push / void / no-result rows skip the
    P/L update entirely.
    """
    history = _all_history(backend)
    wins = losses = 0
    units = 0.0
    for h in history:
        result = (h.get("result") or "").lower()
        if result == "win":
            wins += 1
            odds = h.get("american_odds")
            if not isinstance(odds, (int, float)):
                odds = -110                                               # default
            if odds > 0:
                units += odds / 100.0
            elif odds < 0:
                units += 100.0 / abs(odds)
        elif result == "loss":
            losses += 1
            units -= 1.0
        # push / void: no change to either counter
    total = wins + losses
    return {
        "wins":   wins,
        "losses": losses,
        "pct":    (wins / total) if total else None,
        "units":  round(units, 2),
    }


# ── Per-classifier picks tracker loader ─────────────────────────────────────

import json as _json
from pathlib import Path as _Path

_TRACKER_PATHS = {
    "xgb": _Path(".cache/xgb_picks_history.json"),
    "lr":  _Path(".cache/lr_picks_history.json"),
    "nn":  _Path("data/nn_picks_history.json"),
}


def classifier_accuracy_from_trackers() -> dict:
    """Aggregate per-classifier correct/total from the three picks-history
    files (xgb_picks_history.json, lr_picks_history.json,
    nn_picks_history.json).

    These files carry one entry PER ANALYZED GAME per bet_type -- the
    full slate, not just the top-5 picks the daily-picks selector
    placed bets on.  Reading them here gives the user a true model
    accuracy across the full prediction surface (which is what they
    want to see on the Model page + Home chip; the ledger history
    only ever covered top-5).

    Returns:
        {
          "xgb": {"overall": [correct, total],
                  "moneyline": [c, t], "run_line_spread": [c, t],
                  "totals": [c, t]},
          "lr":  {...},
          "nn":  {...},
        }

    Each entry's `correct` field is True/False/None (None when the game
    hasn't settled yet).  None rows don't count toward either side.
    Silent fallback to zeros on missing / corrupted files so the page
    always renders.
    """
    # bet_type categories -- align with pages/model.py::_CATS
    _CATS = {
        "moneyline":       ("moneyline", "single"),
        "run_line_spread": ("run_line", "spread"),
        "totals":          ("totals",),
    }

    out: dict[str, dict[str, list[int]]] = {
        m: {"overall": [0, 0], **{c: [0, 0] for c in _CATS}}
        for m in _TRACKER_PATHS
    }

    for model_key, path in _TRACKER_PATHS.items():
        if not path.exists():
            continue
        try:
            payload = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in (payload.get("picks") or []):
            correct = entry.get("correct")
            if correct is None:
                continue
            bt = (entry.get("bet_type") or "moneyline").lower()
            cat = next(
                (k for k, aliases in _CATS.items() if bt in aliases),
                None,
            )
            if cat is None:
                continue
            out[model_key][cat][1] += 1
            out[model_key][cat][0] += int(bool(correct))
            out[model_key]["overall"][1] += 1
            out[model_key]["overall"][0] += int(bool(correct))
    return out


# ── Chip #2 -- best classifier (XGB / LR / NN) ─────────────────────────────

def best_classifier(backend) -> dict | None:
    """Return {'model': 'XGBoost'|..., 'correct': N, 'total': N, 'pct': float}
    for the classifier with the highest correct-call rate across all
    settled predictions, or None if fewer than 10 settled predictions
    exist in any classifier's tracker.

    Reads from the per-classifier tracker files (not ledger history) so
    EVERY analyzed game contributes -- not just the top-5 placed bets.
    The Model page's CLASSIFIER ACCURACY card uses the same source so
    the chip and that card always agree.
    """
    tallies = classifier_accuracy_from_trackers()
    pretty = {"xgb": "XGBoost", "lr": "Logistic Regression", "nn": "Neural Net"}

    qualified = [
        (m, *tallies[m]["overall"])
        for m in tallies
        if tallies[m]["overall"][1] >= 10
    ]
    if not qualified:
        return None
    best = max(qualified, key=lambda r: (r[1] / r[2]) if r[2] else 0)
    m, correct, total = best
    return {
        "model":   pretty[m],
        "correct": correct,
        "total":   total,
        "pct":     (correct / total) if total else 0.0,
    }


# ── Chip #3 -- best bet type ────────────────────────────────────────────────

def best_bet_type(backend) -> dict | None:
    """Return {'label': str, 'wins': N, 'losses': N, 'pct': float} for the
    bet type with the highest W/(W+L) rate, or None if no bet type has
    at least 5 settled bets.

    Aggregates across both sports.  Bet types come from bet['bet_type']:
        'single' (moneyline) / 'run_line' / 'spread' / 'totals'.
    """
    history = _all_history(backend)
    counts: dict[str, list[int]] = {}     # label -> [W, L]
    for bet in history:
        result = (bet.get("result") or "").lower()
        if result not in ("win", "loss"):
            continue
        label = _bet_type_label(bet.get("bet_type"))
        counts.setdefault(label, [0, 0])
        if result == "win":  counts[label][0] += 1
        else:                counts[label][1] += 1

    qualified = [
        (label, w, l) for label, (w, l) in counts.items()
        if (w + l) >= 5
    ]
    if not qualified:
        return None
    best = max(qualified, key=lambda r: (r[1] / (r[1] + r[2])) if (r[1] + r[2]) else 0)
    label, w, l = best
    total = w + l
    return {
        "label":  label,
        "wins":   w,
        "losses": l,
        "pct":    (w / total) if total else 0.0,
    }


# ── Section 2 -- enumerate per-market value picks across cached games ──────

def enumerate_value_picks(games: Iterable[dict], *, min_edge: float = 0.0) -> list[dict]:
    """Walk a list of serialized games (_serialize / _serialize_wnba output)
    and yield one row per market that hit `value_pick` / `value_bet=True`
    AND whose edge >= `min_edge`.

    Returned dicts carry just what the Home compact rows need so the
    renderer doesn't have to reach back into the raw game dict.  Shape:

        {
          matchup:   str,    -- "Braves vs Phillies" (short names)
          pick:      str,    -- "Braves ML" / "Braves -1.5" / "8.5 Over"
          edge:      float,  -- 0.123 means 12.3%
          prob:      float,  -- 0.62 (model's pick probability)
          odds:      int,    -- American odds for the pick
          sport:     str,    -- "mlb" / "wnba"
          game_id:   str,
          bet_type:  str,    -- "single" / "run_line" / "spread" / "totals"
        }
    """
    out: list[dict] = []
    for g in games:
        if g.get("_no_model"):
            continue
        sport = (g.get("_sport") or "mlb").lower()
        away = g.get("away_team", "")
        home = g.get("home_team", "")
        matchup = f"{_team_nick(away)} vs {_team_nick(home)}"
        # Carry the FULL team names alongside the shortened matchup so
        # the home renderer can look up CDN logos (logo lookup uses
        # full names as the dict key).
        away_full = away
        home_full = home
        game_id = g.get("game_id") or g.get("id")

        # 1) Moneyline (top-level value_pick field)
        if g.get("value_pick"):
            edge = float(g.get("pick_edge") or 0)
            if edge >= min_edge:
                out.append({
                    "matchup":  matchup,
                    "pick":     f"{_team_nick(g.get('pick_team') or '')} ML",
                    "edge":     edge,
                    "prob":     float(g.get("pick_prob") or 0),
                    "odds":     g.get("pick_odds"),
                    "sport":    sport,
                    "game_id":  game_id,
                    "away_full": away_full,
                    "home_full": home_full,
                    "bet_type": "single",
                })

        # 2) Run line (MLB) / Spread (WNBA)
        rl = g.get("run_line") or g.get("spread_pick")
        if rl and rl.get("value_bet"):
            edge = float(rl.get("edge") or 0)
            if edge >= min_edge:
                line = rl.get("run_line_point", rl.get("spread_line"))
                try:
                    line_str = f"{float(line):+g}"
                except (TypeError, ValueError):
                    line_str = ""
                team = rl.get("pick_team") or ""
                pick = (f"{_team_nick(team)} {line_str}").strip()
                out.append({
                    "matchup":  matchup,
                    "pick":     pick,
                    "edge":     edge,
                    "prob":     float(rl.get("pick_prob") or 0),
                    "odds":     rl.get("pick_odds"),
                    "sport":    sport,
                    "game_id":  game_id,
                    "away_full": away_full,
                    "home_full": home_full,
                    "bet_type": ("run_line" if g.get("run_line") else "spread"),
                })

        # 3) Totals
        tot = g.get("totals") or {}
        if tot.get("value_bet"):
            edge = float(tot.get("edge") or 0)
            if edge >= min_edge:
                direction = (tot.get("direction") or "over").title()
                line = tot.get("total_line")
                pick = (
                    f"{float(line):g} {direction}" if isinstance(line, (int, float))
                    else direction
                )
                odds = (
                    tot.get("over_odds") if direction.lower() == "over"
                    else tot.get("under_odds")
                )
                out.append({
                    "matchup":  matchup,
                    "pick":     pick,
                    "edge":     edge,
                    "prob":     float(tot.get("pick_prob") or 0),
                    "odds":     odds,
                    "sport":    sport,
                    "game_id":  game_id,
                    "away_full": away_full,
                    "home_full": home_full,
                    "bet_type": "totals",
                })

    return out


# ── Team-name nickname helper (kept here to avoid pulling sidebar's copy) ──

def _team_nick(name: str) -> str:
    """City -> nickname only ("Atlanta Braves" -> "Braves").

    Matches the legacy template's shortName() heuristics: Sox + Blue Jays
    keep the two-word nickname; everything else drops everything before
    the last word.  Returns the input unchanged if it's already 1 word
    or blank.
    """
    if not name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    last = parts[-1]
    if last == "Sox":
        return " ".join(parts[-2:])
    if last == "Jays":
        return "Blue Jays"
    return last


# ── Color helper -- shared by all chips + carousel ──────────────────────────

def winrate_color(pct: float | None, theme) -> str:
    """Map a 0-1 percentage to one of three theme colors per the spec:
       >55%  green
       45-55 yellow
       <45%  red
    `pct` of None falls back to TEXT_DIM so the chip renders gracefully
    when there's no settled history yet.
    """
    if pct is None:
        return theme.TEXT_DIM
    p = float(pct) * 100
    if p > 55:
        return theme.POS
    if p < 45:
        return theme.NEG
    return theme.WARN
