"""
Wiring between the app's pick/settlement pipeline and the rebuilt
Supabase ledgers (src/supa_ledger.py).

Two responsibilities, kept independent per system:

  * place_model_daily_picks(payload) -- stake the model's daily selection
    (up to 10 games + 5 props) into the model ledger, sized off the single
    combined $1000 pool with the existing Kelly logic.  Idempotent per
    game/bet_type so the noon re-check + the 15-min cycle never re-stake or
    double-deduct; once a bet is placed it is LOCKED.

  * settle_open_ledger_bets(...) -- grade every active bet in BOTH ledgers
    against the same final scores / player stats the rest of the
    settlement cycle uses, then apply the frozen-stake bankroll movement.

The model has NO per-day money cap (the $1000 is only the sizing basis);
the My Bets daily limit lives in supa_ledger and is refreshed at 4 AM ET.
"""
from __future__ import annotations

import logging
from typing import Optional, Callable

from . import db, supa_ledger as L
from . import model_picks as MP

_logger = logging.getLogger(__name__)


def _model_bet_id(sport: str, bet_type: str, game_id: str) -> str:
    """One model stake per (sport, bet_type, game) -- so a side flip at the
    noon re-check maps to the SAME id and is skipped (the bet is locked)."""
    return f"{(sport or '').lower()}:{bet_type}:{game_id}"


def _prop_bet_id(sport: str, market: str, player: str, game_id: str) -> str:
    return f"{(sport or '').lower()}:{market}:{player}:{game_id}"


def place_model_daily_picks(payload: dict) -> dict:
    """Stake today's model game + prop picks into the model ledger.

    Sizing uses the existing Kelly `size_bet`, pointed at the single
    combined pool ($1000 basis).  Each stake is frozen at placement;
    re-running (noon, 15-min cycle) never re-stakes an existing bet.
    Returns {"games": n_placed, "props": n_placed}.
    """
    if not db.is_supabase():
        return {"games": 0, "props": 0}
    from .kelly import size_bet

    led   = L.model()
    picks = (payload or {}).get("picks") or {}
    placed = {"games": 0, "props": 0}

    for p in (picks.get("game_picks") or []):
        gid = str(p.get("game_id") or "")
        bt  = p.get("bet_type")
        if not gid or not bt:
            continue
        odds = p.get("odds")
        prob = p.get("pick_prob")
        tier = p.get("confidence_tier") or "strong"
        if not isinstance(odds, (int, float)) or not isinstance(prob, (int, float)):
            continue
        _, dollars, _, _ = size_bet(
            prob, int(odds), led.bankroll(), led.starting(),
            0.0, tier, is_user_bet=False,    # model = full Kelly
        )
        if dollars <= 0:
            continue
        row = led.place(
            bet_id=_model_bet_id(p.get("sport"), bt, gid),
            sport=p.get("sport"), bet_type=bt, selection=p.get("side") or p.get("team"),
            odds=int(odds), stake=dollars, kind="game", game_id=gid,
            meta={"line": p.get("prop_line"), "team": p.get("team"),
                  "matchup": p.get("matchup"), "pick_prob": prob,
                  "confidence_tier": tier},
        )
        if row:
            placed["games"] += 1

    for p in (picks.get("prop_picks") or []):
        player = (p.get("player") or "").strip()
        market = p.get("market")
        gid    = str(p.get("event_id") or p.get("game_id") or f"{player}|{market}")
        odds   = p.get("odds")
        prob   = p.get("model_prob") or p.get("confidence")
        if not player or not market or not isinstance(odds, (int, float)) \
           or not isinstance(prob, (int, float)):
            continue
        tier = p.get("confidence_tier") or "strong"
        _, dollars, _, _ = size_bet(
            prob, int(odds), led.bankroll(), led.starting(),
            0.0, tier, is_user_bet=False,
        )
        if dollars <= 0:
            continue
        row = led.place(
            bet_id=_prop_bet_id("mlb", market, player, gid),
            sport="mlb", bet_type=market,
            selection=(p.get("side") or "Over"), odds=int(odds), stake=dollars,
            kind="prop", game_id=gid, player_name=player,
            meta={"line": p.get("line"), "model_prob": prob},
        )
        if row:
            placed["props"] += 1

    if placed["games"] or placed["props"]:
        _logger.info("model ledger: placed %d game + %d prop bet(s); pool=$%.2f",
                     placed["games"], placed["props"], led.bankroll())
    return placed


def _grade_bet(bet: dict, final_scores: dict, stat_lookup: Optional[Callable]) -> Optional[str]:
    """Map a ledger bet onto the model_picks grading helpers and return
    win/loss/push/void, or None if the result isn't known yet."""
    meta = bet.get("meta") or {}
    pick = {
        "bet_type":  bet.get("bet_type"),
        "pick_side": bet.get("selection"),
        "line":      meta.get("line"),
        "game_id":   bet.get("game_id"),
    }
    if (bet.get("kind") or "game") == "prop":
        if stat_lookup is None:
            return None
        try:
            actual = stat_lookup(bet.get("player_name"), bet.get("bet_type"))
        except Exception:                                                 # noqa: BLE001
            actual = None
        return MP._grade_prop(pick, actual)
    sc = (final_scores or {}).get(bet.get("game_id"))
    return MP._grade_game(pick, sc) if sc else None


def settle_open_ledger_bets(final_scores: Optional[dict] = None,
                            stat_lookup: Optional[Callable] = None) -> dict:
    """Grade + settle every active bet in BOTH ledgers.  Same final-scores /
    stat-lookup inputs as model_picks.settle().  Returns a per-system
    {settled, wins, losses, pushes} summary."""
    final_scores = final_scores or {}
    out: dict[str, dict] = {}
    if not db.is_supabase():
        return out
    for system in ("model", "personal"):
        led = L.Ledger(system)
        s = {"settled": 0, "wins": 0, "losses": 0, "pushes": 0}
        for bet in led.active_bets():
            result = _grade_bet(bet, final_scores, stat_lookup)
            if not result:
                continue
            led.settle(bet, result)
            s["settled"] += 1
            s["wins"]   += result == "win"
            s["losses"] += result == "loss"
            s["pushes"] += result in ("push", "void")
        if s["settled"]:
            _logger.info("%s ledger settled %d (%dW/%dL/%dP); pool=$%.2f",
                         system, s["settled"], s["wins"], s["losses"],
                         s["pushes"], led.bankroll())
        out[system] = s
    return out
