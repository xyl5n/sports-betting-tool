"""
Composite player-level features used by the XGBoost moneyline and run-line
models on top of the existing 24-feature team vector.

Three composites, all signed so that POSITIVE = better for the home team:

    pds_diff       (index 24, both ML and RL)
        Home pitcher's Pitcher Dominance Score minus the away pitcher's.
        Individual PDS combines (5 - ERA), (1.4 - WHIP), and K%, each
        z-shifted to a comparable scale and averaged with equal weight.

    lvs_diff       (index 25, both ML and RL)
        Away lineup's Lineup Vulnerability Score (avg OPS of top-5 batters
        against the home pitcher's hand) minus the equivalent home-lineup
        score against the away pitcher.  Positive = away lineup is the
        weaker matchup -> home favoured.

    blowout_prob   (index 26, RL only)
        Estimated probability of a 2+ run home margin, combining the two
        composites above with the team net-run-diff.  Sigmoid of a linear
        score so the output is bounded in (0, 1).

All inputs are taken at face value with light defensive clipping; no extra
fetching happens here -- callers pass the raw stat dicts.
"""
from __future__ import annotations

import math
from typing import Optional


# ── PDS helpers ──────────────────────────────────────────────────────────────

_PDS_ERA_ANCHOR  = 5.00   # ERAs below this score positive in the (5 - ERA) term
_PDS_WHIP_ANCHOR = 1.40   # WHIPs below this score positive in the (1.4 - WHIP) term
_PDS_K_ANCHOR    = 0.20   # league-ish K rate baseline


def pitcher_dominance(era: float, whip: float, k_rate: float) -> float:
    """
    Single-pitcher Pitcher Dominance Score.  Equal weighting of three
    z-shifted dimensions:
        ERA term  : (anchor - era)   scaled so 1 run / 9 ip == 1.0
        WHIP term : (anchor - whip)  scaled so 0.10 WHIP == 1.0
        K term    : (k_rate - anchor) scaled so 5 pp == 1.0
    Output is dimensionless; ~0 for league-average, positive for elite.
    """
    era_term  = (_PDS_ERA_ANCHOR  - era)        # 1.0 ERA below anchor -> +1.0
    whip_term = (_PDS_WHIP_ANCHOR - whip) * 10  # 0.10 WHIP below       -> +1.0
    k_term    = (k_rate - _PDS_K_ANCHOR) * 20    # 5 pp above            -> +1.0
    return (era_term + whip_term + k_term) / 3.0


def pds_diff(home_sp: dict, away_sp: dict) -> float:
    """Home PDS minus away PDS.  Positive = home pitcher is the better matchup."""
    return pitcher_dominance(
        home_sp.get("era", 4.50),
        home_sp.get("whip", 1.30),
        home_sp.get("k_rate", 0.215),
    ) - pitcher_dominance(
        away_sp.get("era", 4.50),
        away_sp.get("whip", 1.30),
        away_sp.get("k_rate", 0.215),
    )


# ── LVS helpers ──────────────────────────────────────────────────────────────

_LVS_ANCHOR_OPS = 0.710   # neutral OPS; deviations from this are the signal


def lineup_vulnerability(lvs_inputs: list[dict]) -> float:
    """
    Average OPS of up-to-5 hitter rows against the relevant pitcher hand.
    Each input row is {"ops": float, "k_rate": float}.  Falls back to the
    league anchor when fewer than 1 hitter is supplied.
    """
    if not lvs_inputs:
        return _LVS_ANCHOR_OPS
    return sum(r.get("ops", _LVS_ANCHOR_OPS) for r in lvs_inputs) / len(lvs_inputs)


def lvs_diff(
    away_lineup_vs_home_sp: list[dict],
    home_lineup_vs_away_sp: list[dict],
) -> float:
    """
    Signed lineup-vulnerability diff oriented so positive helps the home team.

        away lineup's OPS vs the home starter  >  home lineup's OPS vs away starter
        -> home pitcher is facing a tougher slate than the away pitcher
        -> WORSE for home; we flip the sign.

    Negative output = away lineup is easier to handle (good for home).
    Positive output = home lineup is easier to handle (good for away).

    We invert so positive = good for home:
        return  home_lineup_ops - away_lineup_ops
    """
    home_ops = lineup_vulnerability(home_lineup_vs_away_sp)
    away_ops = lineup_vulnerability(away_lineup_vs_home_sp)
    # away lineup easier (low OPS vs home SP) AND home lineup tougher (high OPS vs away SP)
    # both push the metric positive -> good for home.
    return (home_ops - away_ops)


# ── Blowout probability ──────────────────────────────────────────────────────

# Coefficients chosen so the linear score is on roughly the same scale as
# logit(P(home covers -1.5)).  Calibrated heuristically; the XGB model will
# learn to scale/clip from there during training.
_BO_W_PDS   = 0.45
_BO_W_LVS   = 0.65
_BO_W_NETRD = 0.18
_BO_BIAS    = -0.45   # base rate for "home wins by 2+" is ~35-40%


def _sigmoid(z: float) -> float:
    # Clamp to avoid overflow on extreme inputs.
    if z >  35: return 1.0
    if z < -35: return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def blowout_probability(
    pds_diff_val: float,
    lvs_diff_val: float,
    net_run_diff: float,
) -> float:
    """
    Estimate P(home wins by 2+ runs).  Inputs are signed so that positive
    each push toward a home blowout.
    """
    z = (_BO_BIAS
         + _BO_W_PDS   * pds_diff_val
         + _BO_W_LVS   * lvs_diff_val
         + _BO_W_NETRD * net_run_diff)
    return _sigmoid(z)


# ── Convenience wrapper used by both feature builders ────────────────────────

def compose_player_features(
    home_sp:                dict,
    away_sp:                dict,
    away_top5_vs_home_hand: list[dict],
    home_top5_vs_away_hand: list[dict],
    net_run_diff:           float,
) -> tuple[float, float, float]:
    """
    Return (pds_diff_val, lvs_diff_val, blowout_prob) given the raw inputs.
    Used as a single call site from both live and historical feature paths.
    """
    pds_v = pds_diff(home_sp, away_sp)
    lvs_v = lvs_diff(away_top5_vs_home_hand, home_top5_vs_away_hand)
    bo_v  = blowout_probability(pds_v, lvs_v, net_run_diff)
    return pds_v, lvs_v, bo_v
