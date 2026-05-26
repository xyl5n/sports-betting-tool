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

# ── Chip #1 -- overall win rate ────────────────────────────────────────────

def overall_record(backend) -> dict:
    """GAME MODELS box -- the ENSEMBLE's actual recommended bets: ONE result
    per market per game (one ML, one RL, one total), graded W/L.  Reads the
    model_picks 'combined' store (the ensemble/final pick the app displays),
    NOT the per-classifier xgb/lr/nn trackers (which would count each model x
    bet type = up to 9 per game).  All-time, Supabase (model_picks)."""
    try:
        from src import model_picks as _mp
        return _mp.store_record("mlb", "combined")
    except Exception:                                                      # noqa: BLE001
        return {"wins": 0, "losses": 0, "pct": None}


def props_record(backend) -> dict:
    """PROPS MODELS box -- the MLB pitcher + batter prop models aggregated
    into one collective W/L, ONE result per settled prop.  Reads model_picks
    (the store grading writes to).  All-time.  Shows 0-0 while props are still
    pending (none settled yet) -- that is correct-for-now, not a read bug."""
    try:
        from src import model_picks as _mp
        return _mp.models_record("mlb", ["pitcher", "batter"])
    except Exception:                                                      # noqa: BLE001
        return {"wins": 0, "losses": 0, "pct": None}


# ── Model Performance section (bottom of home page) ───────────────────────

def model_performance(backend) -> dict:
    """Model-only settled record (W/L/pct) -- the ENSEMBLE 'combined' record
    from model_picks (one result per market per game).  Never the ledger, so
    model performance is never mixed with the personal-bet ledger.  Shape:
    {wins, losses, pct}."""
    try:
        from src import model_picks as _mp
        rec = _mp.store_record("mlb", "combined")
        return {"wins": rec["wins"], "losses": rec["losses"], "pct": rec["pct"]}
    except Exception:                                                      # noqa: BLE001
        return {"wins": 0, "losses": 0, "pct": None}


# ── Per-classifier picks tracker loader ─────────────────────────────────────

# Bet-type categories shared across helpers in this module.
_TRACKER_CATS = {
    "moneyline":       ("moneyline", "single"),
    "run_line_spread": ("run_line", "spread"),
    "totals":          ("totals",),
}

# model_picks bet_type -> the category keys above.
_BT_TO_CAT = {"ml": "moneyline", "rl": "run_line_spread", "total": "totals"}


def tracker_records() -> dict:
    """Aggregate finished MLB model picks from the Supabase model_picks table
    (PostgREST only).

    Game side: ``overall`` + ``by_bet_type`` come from the ENSEMBLE 'combined'
    store (one result per market per game -- ml/rl/total, so at most 3 per
    game); ``by_model`` from the per-classifier xgb/lr/nn stores (used only by
    the Best Game Model badge + the Model-tab classifier-accuracy card, which
    are intentionally per-model).  All-time.

    Returns:
      {
        "overall":     {"wins", "losses", "pct"},        # ensemble (combined)
        "by_bet_type": {cat: {"wins", "losses", "pct"}}, # ensemble (combined)
        "by_model":    {"xgb": {"overall":[c,t], <cat>:[c,t], ...}, "lr":..., "nn":...},
      }
    """
    try:
        from src import db as _db
        rows = _db.model_picks_list(sport="mlb")
    except Exception:                                                      # noqa: BLE001
        rows = []

    by_cat: dict[str, list[int]] = {k: [0, 0] for k in _TRACKER_CATS}
    by_model: dict[str, dict[str, list[int]]] = {
        m: {"overall": [0, 0], **{k: [0, 0] for k in _TRACKER_CATS}}
        for m in ("xgb", "lr", "nn")
    }
    for r in rows:
        if (r.get("status") or "").lower() != "finished":
            continue
        cat = _BT_TO_CAT.get((r.get("bet_type") or "").lower())
        if cat is None:
            continue
        res = (r.get("result") or "").lower()
        if res not in ("win", "loss"):
            continue
        won = int(res == "win")
        model = r.get("model")
        if model == "combined":                 # ensemble pick: 1 per market
            by_cat[cat][0 if won else 1] += 1
        elif model in by_model:                 # per-classifier (badge only)
            by_model[model]["overall"][1] += 1
            by_model[model]["overall"][0] += won
            by_model[model][cat][1] += 1
            by_model[model][cat][0] += won

    overall_w = sum(c[0] for c in by_cat.values())
    overall_l = sum(c[1] for c in by_cat.values())

    def _pct(w: int, l: int) -> float | None:
        return (w / (w + l)) if (w + l) else None

    return {
        "overall":     {"wins": overall_w, "losses": overall_l,
                        "pct": _pct(overall_w, overall_l)},
        "by_bet_type": {
            cat: {"wins": w, "losses": l, "pct": _pct(w, l)}
            for cat, (w, l) in by_cat.items()
        },
        "by_model":    by_model,
    }


def classifier_accuracy_from_trackers() -> dict:
    """Per-classifier (xgb/lr/nn) accuracy from model_picks.  Thin wrapper
    over tracker_records()["by_model"] kept for existing call sites."""
    return tracker_records()["by_model"]


# ── Chip #2 -- best classifier (XGB / LR / NN) ─────────────────────────────

def best_classifier(backend) -> dict | None:
    """BEST GAME MODEL -- whichever of MLB xgb/lr/nn has the highest finished
    win% in model_picks (per-model by design, same model_picks basis as the
    GAME MODELS chip).  Returns {'model','correct','total','pct'} or None."""
    try:
        from src import model_picks as _mp
        return _mp.best_game_model("mlb")
    except Exception:                                                      # noqa: BLE001
        return None


def best_bet_type(backend) -> dict | None:
    """BEST PROP MODEL -- pitcher vs batter, whichever has the higher finished
    win% in model_picks.  Returns {'label','wins','losses','pct'} or None."""
    try:
        from src import model_picks as _mp
        return _mp.best_prop_model("mlb")
    except Exception:                                                      # noqa: BLE001
        return None


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
