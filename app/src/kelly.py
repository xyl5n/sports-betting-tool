"""
Kelly Criterion bet sizing.
1 unit = 1% of the original starting bankroll.

Formula:
  b = decimal_odds - 1  (net profit per unit staked)
  p = model win probability
  q = 1 - p
  Full Kelly  = (b*p - q) / b
  Half Kelly  = Full Kelly / 2   (used for user/confirmed bets)

Reductions applied in order:
  1. Half Kelly if is_user_bet
  2. Upset factor reduction (score >= 7 → ×0.75, score >= 4 → ×0.90)
  3. Moderate/split confidence reduction (×0.50)
  4. Hard cap at 2U (2% of starting bankroll) — applies to ALL bets.
     The cap is a risk-management ceiling, not a flat stake.

Confidence tiers:
  strong   – all models unanimous → full Kelly sizing
  moderate – 2-of-3 majority (NN present) → ×0.50 Kelly
  split    – 2-model split with gap > 10pp → ×0.50 Kelly (half-Kelly allowed bet)
  low      – 2-model split with gap ≤ 10pp → no bet

Dollar precision:
  Model bets (is_user_bet=False) → rounded to the cent  ($7.50)
  User bets  (is_user_bet=True)  → rounded to nearest dollar ($8)
"""
from typing import Optional


# ── Named constants ───────────────────────────────────────────────────────────
# These values are also referenced in src/daily_picks.py (MIN_EDGE),
# src/upset.py (UPSET_HIGH/LOW), and templates/index.html (chaos thresholds).
MIN_EDGE: float       = 0.03   # minimum edge gate (3%) for any bet to be placed
KELLY_HARD_CAP: float = 0.02   # 2U cap — maximum Kelly fraction (2% of starting bankroll)
UPSET_HIGH: int       = 7      # upset score ≥ UPSET_HIGH → ×0.75 Kelly reduction
UPSET_LOW:  int       = 4      # upset score ≥ UPSET_LOW  → ×0.90 Kelly reduction
UPSET_HIGH_MULT: float = 0.75  # Kelly multiplier for high upset scores
UPSET_LOW_MULT:  float = 0.90  # Kelly multiplier for moderate upset scores


def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1.0
    return 100 / abs(american) + 1.0


def implied_prob(american: int) -> float:
    return 1.0 / american_to_decimal(american)


def confidence_tier(xgb_prob: float, lr_prob: float, nn_prob=None) -> str:
    """
    DEPRECATED — kept for any external scripts still calling it. Live picks
    should use confidence_tier_from_prob() instead, which classifies a pick
    by its raw probability rather than by model agreement.

    Determine pick confidence from model agreement.
    strong   – all available models unanimous
    moderate – 2-of-3 majority (with NN present)
    split    – 2-model split with gap > 10pp → half-Kelly allowed bet
    low      – 2-model split with gap ≤ 10pp → no bet
    """
    xgb_home = xgb_prob >= 0.5
    lr_home  = lr_prob  >= 0.5

    if nn_prob is None:
        if xgb_home == lr_home:
            return "strong"
        gap = abs(xgb_prob - lr_prob)
        return "split" if gap > 0.10 else "low"

    nn_home    = nn_prob >= 0.5
    home_votes = sum([xgb_home, lr_home, nn_home])
    return "strong" if home_votes in (0, 3) else "moderate"


# ── Probability-based confidence tiering ──────────────────────────────────────
# Independent of model agreement, market odds, or edge. The tier is a
# function of the raw probability the model assigns to the picked outcome
# (P(team wins) for moneyline, P(team covers -1.5) for run line at -1.5,
# P(combined > line) for totals over).  Edge is reported separately.
CONFIDENCE_STRONG_MIN:   float = 0.62   # > 62%  → strong
CONFIDENCE_MODERATE_MIN: float = 0.52   # 52-62% → moderate; below → low


def confidence_tier_from_prob(pick_prob: float | None) -> str:
    """
    Return 'strong' | 'moderate' | 'low' based solely on the model's
    raw probability for the picked outcome.

        prob > 0.62   → 'strong'
        prob in [0.52, 0.62]  → 'moderate'
        prob < 0.52   → 'low'

    Edge and model-agreement information are deliberately not consulted —
    the tier reflects how confident the model is about the OUTCOME, and
    edge is a separately reported number that compares this confidence
    to the market's implied probability.
    """
    if pick_prob is None:
        return "low"
    p = float(pick_prob)
    if p > CONFIDENCE_STRONG_MIN:
        return "strong"
    if p >= CONFIDENCE_MODERATE_MIN:
        return "moderate"
    return "low"


def tracked_bet_kelly(prob, american_odds, bankroll) -> tuple[float, Optional[str]]:
    """Recommended stake for a tracked bet using the textbook half-Kelly
    formula, sized off the *current* bankroll.

        b    = decimal_odds - 1
        p    = model win probability,  q = 1 - p
        full = (b*p - q) / b
        half = full / 2
        $    = half * bankroll

    Returns ``(dollars, flag)``:
      * positive edge -> (dollars, None), with a $1 floor when the raw
        amount rounds below a dollar (so a real edge never displays $0).
      * no edge (half-Kelly <= 0) -> (0.0, "no_edge").
      * bad inputs -> (0.0, "invalid").

    Unlike :func:`size_bet`, this applies NO confidence-tier / upset /
    hard-cap reductions and NO minimum-edge gate -- every tracked bet is
    sized by the same formula off the same bankroll, so two bets can no
    longer disagree on method (the $2-vs-$0 bug)."""
    try:
        p  = float(prob)
        bk = float(bankroll)
        odds = int(american_odds)
    except (TypeError, ValueError):
        return 0.0, "invalid"
    if not (0.0 < p < 1.0) or bk <= 0:
        return 0.0, "invalid"
    b = american_to_decimal(odds) - 1.0
    if b <= 0:
        return 0.0, "invalid"
    q = 1.0 - p
    half = ((b * p - q) / b) / 2.0
    if half <= 0:
        return 0.0, "no_edge"
    dollars = half * bk
    if dollars < 1.0:
        return 1.0, None          # positive edge but rounds to $0 -> $1 floor
    return float(round(dollars)), None


def size_bet(
    model_prob: float,
    american_odds: int,
    bankroll: float,
    starting_bankroll: float,
    upset_score: float = 0.0,
    confidence: str = "strong",
    is_user_bet: bool = False,
) -> tuple[float, float, float, str]:
    """
    Kelly bet sizing. Returns (fraction, dollars, units, display_str).
    Model bets use Full Kelly; user/confirmed bets use Half Kelly.
    Moderate/split tiers reduce the fraction by ×0.50 before the 2U cap.
    """
    decimal = american_to_decimal(american_odds)

    # Minimum edge gate — must beat market implied prob by at least MIN_EDGE
    edge = model_prob - (1.0 / decimal)
    if edge < MIN_EDGE or confidence == "low":
        return 0.0, 0.0, 0.0, "No Bet"

    # Core Kelly formula: f* = (b*p - q) / b
    b = decimal - 1.0
    p = model_prob
    q = 1.0 - p
    full_kelly = (b * p - q) / b
    if full_kelly <= 0.0:
        return 0.0, 0.0, 0.0, "No Bet"

    # Step 1: Half Kelly for user bets
    fraction = full_kelly / 2.0 if is_user_bet else full_kelly

    # Step 2: Upset factor reduction
    if upset_score >= UPSET_HIGH:
        fraction *= UPSET_HIGH_MULT
    elif upset_score >= UPSET_LOW:
        fraction *= UPSET_LOW_MULT

    # Step 3: Moderate/split confidence reduction
    if confidence in ("moderate", "split"):
        fraction /= 2.0

    # Step 4: Hard cap at KELLY_HARD_CAP (2% of starting bankroll) — applies to all bets
    fraction = min(fraction, KELLY_HARD_CAP)

    # Dollar amount:
    #   model bets → cent precision so individual edge differences are visible
    #   user bets  → nearest dollar (cleaner for manual tracking)
    raw_dollars = fraction * bankroll
    if is_user_bet:
        dollars = float(round(raw_dollars))
        fmt = f"${dollars:.0f}"
    else:
        dollars = round(raw_dollars, 2)
        fmt = f"${dollars:.2f}"

    if dollars <= 0:
        return 0.0, 0.0, 0.0, "No Bet"

    fraction = dollars / bankroll

    # Units: 1U = 1% of starting bankroll
    unit_size = starting_bankroll * 0.01
    units = round(dollars / unit_size, 1) if unit_size > 0 else 0.0

    return fraction, dollars, units, f"{fmt} ({units}U)"


# ── Backward-compat wrappers (DEPRECATED — not called by app.py) ─────────────
# Kept only to avoid import errors if called from external scripts.
# Use size_bet() directly for all new code.

def full_kelly_size(
    model_prob: float,
    american_odds: int,
    bankroll: float,
    max_fraction: float = 0.05,
) -> tuple[float, float, float]:
    """Returns (fraction, dollars, units). Uses full Kelly (model defaults)."""
    frac, dollars, units, _ = size_bet(
        model_prob, american_odds, bankroll, bankroll, is_user_bet=False
    )
    frac    = min(frac, max_fraction)
    dollars = round(frac * bankroll, 2)
    units   = round(frac * 100, 1)
    return frac, dollars, units


def half_kelly_size(
    model_prob: float,
    american_odds: int,
    bankroll: float,
    max_fraction: float = 0.05,
) -> tuple[float, float, float]:
    """Returns (fraction, dollars, units). Uses half Kelly (user defaults)."""
    frac, dollars, units, _ = size_bet(
        model_prob, american_odds, bankroll, bankroll, is_user_bet=True
    )
    frac    = min(frac, max_fraction)
    dollars = round(frac * bankroll, 2)
    units   = round(frac * 100, 1)
    return frac, dollars, units
