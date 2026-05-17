"""
NN-only player-level feature widening.

The shared 24-col training matrix (X from the enriched historical cache and
training-time MLBFeatureBuilder.build_training_row) lacks the player-level
columns 24-29 that exist in sports_config.MLB_FEATURES.  This module widens
any base matrix into the full 30-col vector the NN trains and predicts on,
deriving columns where possible without any new API calls.

XGBoost and Logistic Regression continue to train on the original 24 cols
using the shared `scaler`.  The Neural Network alone uses these 30 cols
with its dedicated `nn_scaler`.

Real signal (derivable from base columns, no API calls):
    col 27  pitcher_dominance_diff   — composite of sp_era_diff, sp_whip_diff,
                                       sp_k_rate_diff in z-score space.
    col 29  blowout_prob             — logistic approximation from
                                       net_run_diff, sp_era_diff,
                                       bullpen_era_diff.

Neutral (zero) in historical training — real at predict time via _assemble:
    col 24  bb9_diff
    col 25  sp_split_era_diff
    col 26  sp_recent_form_diff
    col 28  lineup_vuln_diff
"""
from __future__ import annotations

import numpy as np

# Column indices in the base 24-col matrix (must match MLB_FEATURES order).
_IDX_NET_RUN_DIFF      = 0
_IDX_SP_ERA_DIFF       = 10
_IDX_SP_WHIP_DIFF      = 11
_IDX_SP_K_RATE_DIFF    = 12
_IDX_BULLPEN_ERA_DIFF  = 19

# League-scale denominators for z-score composite.  ERA std ~ 0.80,
# WHIP std ~ 0.15, K% std ~ 0.040 around the neutral baselines used
# elsewhere in the codebase (4.50 ERA, 1.30 WHIP, 21.5 % K rate).
_ERA_SCALE  = 0.80
_WHIP_SCALE = 0.15
_K_SCALE    = 0.040

NN_FEATURE_COUNT   = 30   # what the NN scaler and weights expect
BASE_FEATURE_COUNT = 24   # what XGB / LR / the cached dataset still use
EXTRAS_COUNT       = NN_FEATURE_COUNT - BASE_FEATURE_COUNT   # 6


def _pitcher_dominance_from_diffs(
    sp_era_diff:    np.ndarray,
    sp_whip_diff:   np.ndarray,
    sp_k_rate_diff: np.ndarray,
) -> np.ndarray:
    """
    Composite 'home pitcher dominance' minus 'away pitcher dominance'.

    sp_era_diff      = away_era  - home_era    (positive = home pitcher better)
    sp_whip_diff     = away_whip - home_whip   (positive = home pitcher better)
    sp_k_rate_diff   = home_k    - away_k      (positive = home pitcher better)

    Per-pitcher dominance := z(K%) - z(ERA) - z(WHIP).
    Diff (home - away) collapses to a sum of pre-signed diffs / scales:
        dom_diff = sp_k_rate_diff/K_SCALE
                 + sp_era_diff   /ERA_SCALE
                 + sp_whip_diff  /WHIP_SCALE
    """
    return (
        sp_k_rate_diff / _K_SCALE
        + sp_era_diff   / _ERA_SCALE
        + sp_whip_diff  / _WHIP_SCALE
    )


def _blowout_prob_from_diffs(
    net_run_diff:     np.ndarray,
    sp_era_diff:      np.ndarray,
    bullpen_era_diff: np.ndarray,
) -> np.ndarray:
    """Logistic approximation of the predict-time blowout_prob feature."""
    z = 0.6 * net_run_diff + 0.4 * sp_era_diff + 0.2 * bullpen_era_diff
    return 1.0 / (1.0 + np.exp(-0.5 * z))


def widen_to_nn_features(X_base: np.ndarray) -> np.ndarray:
    """
    Widen a base (n, 24) matrix to (n, 30) by appending six player-extras
    columns.  Pass-through if input already has 30 cols (live predict path
    via _assemble already builds the full vector).

    Pitcher Dominance and Blowout Probability are derived from the base
    matrix.  The four sparsely-trained columns (bb9_diff, sp_split_era_diff,
    sp_recent_form_diff, lineup_vuln_diff) are zero here — they pick up real
    values only at predict time when feature_vec already arrives 30-wide.
    """
    if X_base.ndim == 1:
        X_base = X_base.reshape(1, -1)
        squeeze = True
    else:
        squeeze = False

    n, d = X_base.shape
    if d == NN_FEATURE_COUNT:
        out = X_base.astype(np.float32, copy=False)
    elif d == BASE_FEATURE_COUNT:
        extras = np.zeros((n, EXTRAS_COUNT), dtype=np.float32)
        # extras column 3 → global col 27 (pitcher_dominance_diff)
        extras[:, 3] = _pitcher_dominance_from_diffs(
            X_base[:, _IDX_SP_ERA_DIFF],
            X_base[:, _IDX_SP_WHIP_DIFF],
            X_base[:, _IDX_SP_K_RATE_DIFF],
        )
        # extras column 5 → global col 29 (blowout_prob)
        extras[:, 5] = _blowout_prob_from_diffs(
            X_base[:, _IDX_NET_RUN_DIFF],
            X_base[:, _IDX_SP_ERA_DIFF],
            X_base[:, _IDX_BULLPEN_ERA_DIFF],
        )
        out = np.hstack([X_base.astype(np.float32, copy=False), extras])
    else:
        raise ValueError(
            f"NN feature widening expected {BASE_FEATURE_COUNT} or "
            f"{NN_FEATURE_COUNT} cols, got {d}."
        )

    return out[0] if squeeze else out
