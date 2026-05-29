"""parlay.py -- parlay generation (Phase B, PR #288).

The parlay subsystem: builds correlated/uncorrelated parlay bundles from
the day's analyzed game legs and computes combined odds + Kelly sizing.

Pure compute -- no Flask coupling, and (unusually) no dependency on any
other satellite module: the full BFS closure (4 functions, 215 lines)
references only stdlib + src.kelly.american_to_decimal.  src.kelly is a
verified leaf module.

Direction (no cycles):
    src.kelly  ->  parlay.py  ->  app.py
    parlay.py imports neither state/utils/serializer/scheduler/ai_prompts
    nor app.py.

NOTE: _generate_parlays uses the stdlib `logging` module directly
(logging.warning in a suppressed-exception handler), NOT the app.py
`_logger` instance -- so no parallel logger reference is needed here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

# american_to_decimal converts American odds to decimal for the combined
# parlay multiplier.  src.kelly is a leaf module (no state/app import).
from src.kelly import american_to_decimal

__all__ = [
    "_compute_parlay",
    "_expand_game_legs",
    "_unique_legs",
    "_generate_parlays",
]

# moved from app.py:1035
# ── Parlay generation ─────────────────────────────────────────────────────────
# Note: american_to_decimal() is imported from src.kelly — no local duplicate needed.

def _compute_parlay(legs: list, name: str, desc: str, emoji: str,
                    accent: str, bankroll: float) -> dict:
    """Build one parlay dict from a list of serialized game results."""
    if len(legs) < 2:
        return {"available": False, "name": name, "description": desc,
                "emoji": emoji, "accent": accent}

    combined_prob  = 1.0
    parlay_decimal = 1.0
    for g in legs:
        combined_prob  *= g["pick_prob"]
        parlay_decimal *= american_to_decimal(g["pick_odds"])

    if parlay_decimal >= 2.0:
        parlay_american = int((parlay_decimal - 1) * 100)
    else:
        parlay_american = int(-100 / (parlay_decimal - 1))

    # Positive-edge gate: model combined prob must beat implied parlay odds
    implied_prob = 1.0 / parlay_decimal if parlay_decimal > 0 else 1.0
    edge = combined_prob - implied_prob

    bet_dollars = bet_units = 0.0
    if bankroll > 0 and edge > 0:
        n = len(legs)
        # Reduction multipliers by leg count
        reduction = {2: 0.25, 3: 0.15, 4: 0.10, 5: 0.05}.get(n, 0.05)
        # Hard caps by leg count (fraction of bankroll)
        cap = {2: 0.02, 3: 0.015, 4: 0.01, 5: 0.005}.get(n, 0.005)

        b = parlay_decimal - 1.0
        p = combined_prob
        q = 1.0 - p
        full_kelly = (b * p - q) / b if b > 0 else 0.0

        if full_kelly > 0:
            fraction = full_kelly * reduction
            fraction = min(fraction, cap)
            raw_dollars = fraction * bankroll
            bet_dollars = round(raw_dollars, 2)
            starting = bankroll  # units relative to current bankroll
            unit_size = starting * 0.01
            bet_units = round(bet_dollars / unit_size, 1) if unit_size > 0 else 0.0

    return {
        "available":       True,
        "name":            name,
        "description":     desc,
        "emoji":           emoji,
        "accent":          accent,
        "legs":            legs,
        "combined_prob":   combined_prob,
        "parlay_decimal":  parlay_decimal,
        "parlay_american": parlay_american,
        "fair_mult":       1.0 / combined_prob if combined_prob > 0 else 0,
        "edge_pct":        round(edge * 100, 2),
        "bet_dollars":     bet_dollars,
        "bet_units":       bet_units,
        "n_legs":          len(legs),
    }

# moved from app.py:1097
def _expand_game_legs(g: dict) -> list:
    """Expand one serialized game into individual ML / RL / totals leg dicts."""
    legs    = []
    game_id = g.get("game_id", "")
    home    = g.get("home_team", "")
    away    = g.get("away_team", "")
    ct      = g.get("commence_time", "")

    # Moneyline
    if g.get("pick_prob") is not None and g.get("pick_odds") is not None:
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "ml",
            "pick_team":     g["pick_team"],
            "pick_side":     g.get("pick_side", ""),
            "pick_odds":     g["pick_odds"],
            "pick_prob":     g["pick_prob"],
            "pick_edge":     g.get("pick_edge", 0),
            "value_pick":    g.get("value_pick", False),
            "prop_line":     None,
        })

    # Run line
    rl = g.get("run_line")
    if rl and not rl.get("conflict"):
        pt   = float(rl.get("run_line_point") or -1.5)
        side = rl.get("side", "home")
        # pick_line is the signed handicap for the chosen team (+1.5 or -1.5)
        pick_line = pt if side == "home" else -pt
        # prop_line for settlement: -run_line_point gives correct threshold for both sides
        prop_line_val = -pt
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "rl",
            "pick_team":     rl.get("pick_team", ""),
            "pick_line":     pick_line,
            "pick_side":     side,
            "pick_odds":     rl["pick_odds"],
            "pick_prob":     rl["pick_prob"],
            "pick_edge":     rl.get("edge", 0),
            "value_pick":    rl.get("value_bet", False),
            "prop_line":     prop_line_val,
        })

    # Totals
    t = g.get("totals")
    if t and not t.get("conflict"):
        direction = t.get("direction", "over")
        line      = t.get("total_line")
        label     = "Over" if direction == "over" else "Under"
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "totals",
            "pick_team":     f"{label} {line}",
            "pick_side":     direction,
            "pick_odds":     t["pick_odds"],
            "pick_prob":     t["pick_prob"],
            "pick_edge":     t.get("edge", 0),
            "value_pick":    t.get("value_bet", False),
            "prop_line":     float(line) if line is not None else None,
        })

    return legs

# moved from app.py:1171
def _unique_legs(pool: list, n: int) -> list:
    """Select up to n legs ensuring no two legs come from the same game."""
    legs: list       = []
    used_games: set  = set()
    for g in pool:
        gid = g.get("game_id", "")
        if gid and gid in used_games:
            continue
        legs.append(g)
        if gid:
            used_games.add(gid)
        if len(legs) >= n:
            break
    return legs

# moved from app.py:1187
def _generate_parlays(serialized: list, bankroll: float) -> dict:
    """
    Produce four parlay recommendations from ML, run-line, and totals picks.
    Only upcoming games are eligible; each game contributes at most one leg.
    """
    now_utc = datetime.now(timezone.utc)

    # Deduplicate games by game_id, keep only upcoming
    seen_ids: set = set()
    all_legs: list = []
    for g in serialized:
        gid = g.get("game_id", "")
        if gid in seen_ids:
            continue
        seen_ids.add(gid)
        try:
            ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if ct > now_utc:
                all_legs.extend(_expand_game_legs(g))
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    value   = [l for l in all_legs if l["value_pick"]]
    any_pos = [l for l in all_legs if l["pick_edge"] > 0]
    dogs    = [l for l in all_legs if l["pick_odds"] >= -150 and l["pick_edge"] > 0]

    # ── Safe: 2 highest-confidence value picks ────────────────────────────────
    safe_pool = sorted(value, key=lambda l: l["pick_prob"], reverse=True)
    safe_legs = _unique_legs(safe_pool, 2)

    # ── Value: top 3 by edge ──────────────────────────────────────────────────
    val_pool = sorted(value, key=lambda l: l["pick_edge"], reverse=True)
    val_legs = _unique_legs(val_pool, 3)

    # ── High Risk / High Reward: 3-4 underdog-leaning picks ──────────────────
    hr_base = dogs if len(dogs) >= 3 else sorted(any_pos,
                  key=lambda l: l["pick_odds"], reverse=True)
    hr_pool  = sorted(hr_base, key=lambda l: l["pick_edge"], reverse=True)
    hr_n     = 4 if len(dogs) >= 4 else 3
    hr_legs  = _unique_legs(hr_pool, hr_n)

    # ── Lottery: 5 picks, balanced edge + upside ─────────────────────────────
    lot_pool = [l for l in all_legs if l["pick_edge"] > -0.08]
    if len(lot_pool) < 5:
        lot_pool = all_legs[:]
    def _lot_score(l):
        upside = 0.4 if l["pick_odds"] > 0 else (0.2 if l["pick_odds"] >= -130 else 0.0)
        return l["pick_edge"] * 0.6 + upside
    lot_sorted = sorted(lot_pool, key=_lot_score, reverse=True)
    lot_legs   = _unique_legs(lot_sorted, 5)

    return {
        "safe": _compute_parlay(
            safe_legs, "Safe Play", "2 highest-confidence value picks",
            "🛡️", "blue", bankroll,
        ),
        "value": _compute_parlay(
            val_legs, "Value Parlay", "Top 3 picks by edge",
            "💎", "green", bankroll,
        ),
        "high_risk": _compute_parlay(
            hr_legs, "High Risk / High Reward", "Underdog-leaning picks with model edge",
            "🔥", "orange", bankroll,
        ),
        "lottery": _compute_parlay(
            lot_legs, "Lottery Ticket", "5 picks · tiny stake · max upside",
            "🎰", "purple", bankroll,
        ),
    }
