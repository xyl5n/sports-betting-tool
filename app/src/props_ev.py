"""
props_ev.py
===========
Expected-value percentage for a player-prop bet.

EV% answers: "for every dollar I stake at this line, how many cents do
I expect to keep on average if the model's win probability is right?"

    profit_per_dollar(american_odds):
        +N -> N / 100        (e.g. +120 -> 1.20)
        -N -> 100 / N        (e.g. -110 -> 0.909)
    EV%(win_prob, odds) = (win_prob * profit_per_dollar
                           - (1 - win_prob) * 1) * 100

Conventions
-----------
* ``win_prob`` is the model's confidence in the side it picked
  (clamped to [0.50, 1.00] elsewhere in the props pipeline).
* ``american_odds`` is the best-available American odds for the chosen
  side; ``best_odds`` on a scored pick dict.
* Returns ``None`` when either input is missing or unparseable, so
  callers can show an em-dash without guarding numerically.
"""
from __future__ import annotations

from typing import Optional


def calc_ev_pct(win_prob, american_odds) -> Optional[float]:
    """Return EV% (signed) for a unit-stake bet, or None on bad inputs.

    Positive = profitable in expectation, negative = bleed money.  A
    coin-flip (-110) bet needs ~52.38% to be break-even.
    """
    if win_prob is None or american_odds is None:
        return None
    try:
        wp = float(win_prob)
    except (TypeError, ValueError):
        return None
    try:
        o = int(american_odds)
    except (TypeError, ValueError):
        return None

    # Clamp confidence to [0, 1] so an extrapolated isotonic value past
    # the calibration boundary doesn't blow up the math.
    if wp < 0.0:
        wp = 0.0
    if wp > 1.0:
        wp = 1.0

    if o >= 100:
        profit_per_dollar = o / 100.0
    elif o <= -100:
        profit_per_dollar = 100.0 / abs(o)
    else:
        # American odds in (-100, +100) are non-standard; treat as
        # untradeable rather than guessing a payout.
        return None

    ev_per_dollar = wp * profit_per_dollar - (1.0 - wp)
    return round(ev_per_dollar * 100.0, 2)


def ev_color(ev_pct, theme) -> str:
    """Pick a theme color for an EV% chip.  Green positive, red
    negative, dim grey when EV is unknown."""
    if ev_pct is None:
        return theme.TEXT_DIM2
    try:
        v = float(ev_pct)
    except (TypeError, ValueError):
        return theme.TEXT_DIM2
    if v > 0.0:
        return theme.POS
    if v < 0.0:
        return theme.NEG
    return theme.TEXT_DIM


def ev_label(ev_pct) -> str:
    """Compact ``+12.4% EV`` / ``-3.1% EV`` / ``— EV`` label."""
    if ev_pct is None:
        return "— EV"
    try:
        v = float(ev_pct)
    except (TypeError, ValueError):
        return "— EV"
    sign = "+" if v > 0 else ("" if v == 0 else "-")
    return f"{sign}{abs(v):.1f}% EV"
