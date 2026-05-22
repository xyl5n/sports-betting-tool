"""
props_line_classifier.py
========================
Classify each (player, line) pair within a prop market as either
``main`` (the standard book line, priced close to even money) or
``alt`` (a sportsbook alternate line set far from balanced juice).

Why this lives in its own module
--------------------------------
Both ``props_scored_cache.score_today_props`` (slate-wide scoring) and
``player_profile_client.get_today_props_for_player`` (per-player live
re-score) need the same classification logic, and they sit on
opposite sides of an existing import edge.  A standalone tiny module
with no other src/ deps lets both import it without creating an
import cycle.

The page UI ("Show Alt Lines" toggle in pages/props.py) consumes the
``line_type`` field this classifier stamps onto each scored entry.
"""
from __future__ import annotations

from typing import Optional


# A line is "main-eligible" when BOTH the over and under best_odds
# sit inside this American-odds range -- the canonical book pair is
# something like (-120, +100) or (-110, -110); we widen a touch to
# tolerate inventory-heavy props (RBIs, HRs) that load extra juice.
# Anything outside this band is treated as an alt.
MAIN_ODDS_RANGE: tuple[int, int] = (-180, 140)


def _odds_distance_from_even(odds) -> Optional[float]:
    """Return how far American *odds* sit from even money (-100/+100).

    Returns ``None`` when odds is missing or unparseable.  -100 and
    +100 are both treated as distance 0 because they are equally
    "even"; -150 returns 50, +200 returns 100, and the tiny
    (-100, +100) zone returns 0 (sportsbooks rarely price there but
    the math should still degrade smoothly).
    """
    if odds is None:
        return None
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return None
    if o >= 100:
        return float(o - 100)
    if o <= -100:
        return float(abs(o) - 100)
    return 0.0


def _in_main_range(odds) -> bool:
    if odds is None:
        return False
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return False
    lo, hi = MAIN_ODDS_RANGE
    return lo <= o <= hi


def classify_lines_for_market(raw_props: list[dict]) -> dict[tuple, dict]:
    """Classify every (player, line) appearing in *raw_props*.

    Args:
        raw_props: One bucket from ``props_client.get_today_props()`` --
            a list of dicts that have at least
            ``player_name`` / ``line`` / ``side`` / ``best_odds``.

    Returns:
        Mapping ``(player_name, float(line))`` → dict with keys:

        - ``line_type``  : ``"main"`` | ``"alt"``
        - ``is_primary`` : True iff this line is the single chosen
          representative for that player in this market.  Exactly one
          line per player gets ``is_primary=True``; ties broken by
          smallest combined distance from even money.
        - ``over_odds``  : Best over-side American odds, or None.
        - ``under_odds`` : Best under-side American odds, or None.
        - ``balance``    : Combined distance from even money; lower
          numbers mean closer to a balanced -110/-110 line.

    Selection rule:
        1. Group rows by ``(player_name, line)``; gather both sides'
           best_odds (when present).
        2. A line is *main-eligible* iff BOTH sides' best_odds sit
           inside ``MAIN_ODDS_RANGE``.  These are the standard book
           lines you'd see on the lobby page.
        3. Per player, among main-eligible lines pick the one with
           the smallest combined distance from even money; that line
           is ``line_type="main"`` and ``is_primary=True``.  All
           other lines for the same player+market are
           ``line_type="alt"``.
        4. If no main-eligible line exists, the closest-to-balanced
           alt is still flagged ``is_primary=True`` so the player
           appears on the slate, but ``line_type`` stays ``"alt"``
           (the log + UI surface this so the user knows there was no
           main market for this prop).
    """
    # Stage 1 -- collect both sides' odds per (player, line)
    by_player_line: dict[tuple, dict] = {}
    for p in raw_props or []:
        player = (p.get("player_name") or "").strip()
        if not player:
            continue
        try:
            line = float(p.get("line"))
        except (TypeError, ValueError):
            continue
        side = (p.get("side") or "").strip().lower()
        key = (player, line)
        bucket = by_player_line.setdefault(key, {})
        if side == "over":
            bucket["over"] = p.get("best_odds")
        elif side == "under":
            bucket["under"] = p.get("best_odds")

    # Stage 2 -- score each (player, line) and group per player
    per_player: dict[str, list[tuple[float, float, bool, Optional[int], Optional[int]]]] = {}
    for (player, line), sides in by_player_line.items():
        o = sides.get("over")
        u = sides.get("under")
        eligible = _in_main_range(o) and _in_main_range(u)
        d_o = _odds_distance_from_even(o)
        d_u = _odds_distance_from_even(u)
        # 1-sided lines get a heavy penalty so 2-sided main lines win.
        d_o = 9999.0 if d_o is None else d_o
        d_u = 9999.0 if d_u is None else d_u
        balance = d_o + d_u
        per_player.setdefault(player, []).append((line, balance, eligible, o, u))

    # Stage 3 -- pick the primary line for each player, classify the rest
    out: dict[tuple, dict] = {}
    for player, rows in per_player.items():
        eligibles = [r for r in rows if r[2]]
        if eligibles:
            primary_line = min(eligibles, key=lambda r: r[1])[0]
            any_main = True
        else:
            primary_line = min(rows, key=lambda r: r[1])[0] if rows else None
            any_main = False
        for line, balance, eligible, over_o, under_o in rows:
            is_primary = (line == primary_line)
            # Even the primary row stays "alt" when there is no main-
            # eligible line; that's what tells the UI to show the
            # "no main market" annotation.
            line_type = "main" if (eligible and is_primary and any_main) else "alt"
            out[(player, line)] = {
                "line_type":  line_type,
                "is_primary": is_primary,
                "over_odds":  over_o,
                "under_odds": under_o,
                "balance":    balance,
            }
    return out


def line_type_for(
    classifications: dict[tuple, dict],
    player_name: str,
    line,
) -> str:
    """Convenience lookup -- returns the ``line_type`` for a given
    (player, line) or ``"alt"`` when unknown.  Both sides of the
    primary-vs-alt boundary care about the same default."""
    try:
        line_f = float(line)
    except (TypeError, ValueError):
        return "alt"
    entry = classifications.get((player_name or "", line_f))
    if not entry:
        return "alt"
    return entry.get("line_type", "alt")


def is_primary_for(
    classifications: dict[tuple, dict],
    player_name: str,
    line,
) -> bool:
    """Convenience lookup mirroring ``line_type_for``."""
    try:
        line_f = float(line)
    except (TypeError, ValueError):
        return False
    entry = classifications.get((player_name or "", line_f))
    if not entry:
        return False
    return bool(entry.get("is_primary"))
