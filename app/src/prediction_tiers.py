"""
Pure-confidence tier helpers and edge calculation, kept structurally separate.

The prediction pipeline is split into two independent steps:

  Step 1 -- CONFIDENCE
    The model predicts the probability of an outcome (home wins, home covers
    -1.5, score goes over the line) from team / pitcher / situation features
    only.  Zero reference to the betting market.  This is the model's
    independent opinion.

  Step 2 -- EDGE
    Edge = model_prob - implied_market_prob.  Computed AFTER confidence,
    using only confidence and market inputs.  Edge never feeds back into
    confidence, and confidence never references edge.

This module exposes the small set of pure helpers that both steps share:
  confidence_tier(p)           -- maps a probability to Strong / Moderate / Low
  compute_edge(prob, implied)  -- pure subtraction
  implied_from_american(odds)  -- vig-included implied probability
  devig_two_way(p1, p2)        -- convert vig-included to vig-free probabilities

The thresholds (52% / 62%) are the canonical confidence tiers; all callers
must use these and not maintain their own buckets.
"""
from __future__ import annotations

CONFIDENCE_TIER_STRONG_THRESHOLD: float   = 0.62
CONFIDENCE_TIER_MODERATE_THRESHOLD: float = 0.52

TIER_STRONG:   str = "Strong"
TIER_MODERATE: str = "Moderate"
TIER_LOW:      str = "Low"


def confidence_tier(p: float) -> str:
    """
    Classify a confidence probability into the Strong / Moderate / Low buckets.

    Accepts any probability in [0, 1].  Internally compares against the
    "pick probability" max(p, 1 - p) so callers may pass either the raw
    P(home) or P(over) without flipping the sign on under/away picks.

    Returns:
      Strong    if max(p, 1-p) >= 0.62
      Moderate  if 0.52 <= max(p, 1-p) < 0.62
      Low       otherwise
    """
    p = float(p)
    p_pick = p if p >= 0.5 else 1.0 - p
    if p_pick >= CONFIDENCE_TIER_STRONG_THRESHOLD:
        return TIER_STRONG
    if p_pick >= CONFIDENCE_TIER_MODERATE_THRESHOLD:
        return TIER_MODERATE
    return TIER_LOW


def compute_edge(model_prob: float, implied_prob: float) -> float:
    """
    Edge = model probability - market-implied probability.

    Both inputs should be the probability of the SAME side (the side the
    bettor would back).  Positive edge means the model thinks the bet is
    more likely to hit than the market does.

    Edge is computed from confidence + market inputs only.  Confidence is
    never adjusted by edge.
    """
    return float(model_prob) - float(implied_prob)


def implied_from_american(odds: float) -> float:
    """
    Vig-included implied probability from American odds.
    -110 -> 0.524,  +120 -> 0.455,  +200 -> 0.333.
    """
    o = float(odds)
    if o >= 0:
        return 100.0 / (o + 100.0)
    return -o / (-o + 100.0)


def devig_two_way(p1: float, p2: float) -> tuple[float, float]:
    """
    Strip the bookmaker's overround from a two-way market.

    p1, p2 are vig-included implied probabilities for two opposing sides.
    Returns (p1', p2') with p1' + p2' == 1.
    """
    s = float(p1) + float(p2)
    if s <= 0:
        return 0.5, 0.5
    return float(p1) / s, float(p2) / s
