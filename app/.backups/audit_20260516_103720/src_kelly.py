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


def american_to_decimal(american: int) -> float:
    if american > 0:
        return american / 100 + 1.0
    return 100 / abs(american) + 1.0


def implied_prob(american: int) -> float:
    return 1.0 / american_to_decimal(american)


def confidence_tier(xgb_prob: float, lr_prob: float, nn_prob=None) -> str:
    """
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

    # Minimum edge gate (3% over market implied probability)
    edge = model_prob - (1.0 / decimal)
    if edge < 0.03 or confidence == "low":
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
    if upset_score >= 7:
        fraction *= 0.75
    elif upset_score >= 4:
        fraction *= 0.90

    # Step 3: Moderate/split confidence reduction
    if confidence in ("moderate", "split"):
        fraction /= 2.0

    # Step 4: Hard cap at 2U (2% of starting bankroll) — applies to all bets
    cap = 0.02
    fraction = min(fraction, cap)

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


# ── Backward-compat wrappers ──────────────────────────────────────────────────

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
