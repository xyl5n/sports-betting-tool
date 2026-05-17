"""
Recency weighting utilities for MLB model training.

Multi-season sample weight scheme
-----------------------------------
  Current season (2026) : 60 % of total training influence
  Previous season (2025) : 25 %
  Historical (≤ 2024)    : 15 %

Dynamic boost
-------------
If a team's current-season win-rate differs from the previous season by more
than DYNAMIC_THRESHOLD (15 pp), the current-season rows that involve that
team receive a boosted weight (W_CURRENT_BOOSTED = 0.75) instead of the base
W_CURRENT = 0.60.  The boost is applied per-row and does NOT re-normalise the
old / previous-season buckets — the models handle unnormalised sample weights
correctly.

Usage in _train()
-----------------
    n_old, n_prev, n_curr = ...
    boost_mask = _build_boost_mask(game_team_ids, high_change_team_ids)
    weights    = compute_sample_weights(n_old, n_prev, n_curr, boost_mask)
    model.fit(X_scaled, y, sample_weight=weights)
"""
from __future__ import annotations

from typing import Optional
import numpy as np

# ── Season weight constants ────────────────────────────────────────────────────
W_CURRENT         = 0.60   # current season (2026)
W_PREV            = 0.25   # previous season (2025)
W_OLD             = 0.15   # 2024 and older
W_CURRENT_BOOSTED = 0.75   # current-season weight for high-change teams

DYNAMIC_THRESHOLD = 0.15   # 15 percentage-point delta triggers the boost
PREV_SEASON       = 2025   # the "previous" season (vs. current 2026)


# ── Core weighting function ────────────────────────────────────────────────────

def compute_sample_weights(
    n_old:     int,
    n_prev:    int,
    n_current: int,
    current_boost_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Return a float32 weight array of length (n_old + n_prev + n_current).

    Row layout: [old rows (n_old) | prev rows (n_prev) | current rows (n_current)]

    Base per-row weight = (group_fraction / group_count) * n_total, so that
    each group's *total* weight equals its target fraction of n_total.
    This is the standard unnormalised sample-weight convention for sklearn /
    XGBoost (they use ratios, not absolute values).

    current_boost_mask : bool array of length n_current.  True → row involves a
        team with a >15pp win-rate change; that row gets W_CURRENT_BOOSTED
        instead of W_CURRENT per-row weight.  Neighbouring groups are unaffected.
    """
    n_total = n_old + n_prev + n_current
    if n_total == 0:
        return np.array([], dtype=np.float32)

    weights = np.ones(n_total, dtype=np.float32)

    if n_old > 0:
        weights[:n_old] = (W_OLD  / n_old)     * n_total
    if n_prev > 0:
        weights[n_old : n_old + n_prev] = (W_PREV / n_prev) * n_total
    if n_current > 0:
        w_base  = (W_CURRENT         / n_current) * n_total
        w_boost = (W_CURRENT_BOOSTED / n_current) * n_total
        weights[n_old + n_prev :] = w_base

        if current_boost_mask is not None:
            if len(current_boost_mask) != n_current:
                raise ValueError(
                    f"current_boost_mask length ({len(current_boost_mask)}) "
                    f"!= n_current ({n_current})"
                )
            boost_idxs = n_old + n_prev + np.where(current_boost_mask)[0]
            if len(boost_idxs):
                weights[boost_idxs] = w_boost

    return weights


# ── Team-change detection ──────────────────────────────────────────────────────

def find_high_change_teams(
    store_current,
    store_prev,
    threshold: float = DYNAMIC_THRESHOLD,
) -> dict[int, float]:
    """
    Compare per-team win% between *store_current* and *store_prev* (both
    GameStore instances that have been loaded with their respective seasons).

    Returns {team_id: delta} for every team whose |delta| > threshold.
    delta > 0  means the team improved; delta < 0 means it declined.
    """
    changes: dict[int, float] = {}
    for team_id in store_current.all_team_ids():
        cur = store_current.get_team_stats(team_id)
        prv = store_prev.get_team_stats(team_id)
        if cur is None or prv is None:
            continue
        # Require at least 10 games in each season to reduce noise
        if cur.get("games_played", 0) < 10 or prv.get("games_played", 0) < 10:
            continue
        delta = cur["win_pct"] - prv["win_pct"]
        if abs(delta) > threshold:
            changes[team_id] = round(delta, 4)
    return changes


def build_boost_mask(
    game_team_pairs: list[tuple[int, int]],
    high_change_ids: set[int],
) -> np.ndarray:
    """
    Build a boolean mask of length len(game_team_pairs).
    True at index i if either team in game i is in high_change_ids.
    """
    if not high_change_ids:
        return np.zeros(len(game_team_pairs), dtype=bool)
    return np.array(
        [h in high_change_ids or a in high_change_ids
         for h, a in game_team_pairs],
        dtype=bool,
    )


# ── Summary formatting ─────────────────────────────────────────────────────────

def format_weight_shift_summary(
    changes:       dict[int, float],
    store_current,
    store_prev,
    top_n:         int = 10,
) -> str:
    """
    Build a human-readable summary of teams with the largest weight shifts.

    Parameters
    ----------
    changes       : {team_id: delta} from find_high_change_teams()
    store_current : GameStore loaded with 2026 season
    store_prev    : GameStore loaded with 2025 season
    top_n         : how many teams to show

    Returns
    -------
    Multi-line string suitable for printing to stdout.
    """
    if not changes:
        return (
            "  No teams exceeded the 15pp win-rate threshold.\n"
            "  All games use the standard 60 / 25 / 15 % weighting."
        )

    # Sort by absolute delta descending
    sorted_teams = sorted(changes.items(), key=lambda kv: abs(kv[1]), reverse=True)

    lines = [
        "",
        "  +- Dynamic Weight Shifts (> 15 pp change, current season weight -> 75%) -+",
        f"  {'Team':<28}  {'Prev Win%':>9}  {'Curr Win%':>9}  {'Delta':>8}  {'Direction':>10}",
        "  " + "-" * 68,
    ]

    for team_id, delta in sorted_teams[:top_n]:
        # Resolve team name
        team_obj = store_current._team_by_id.get(team_id, {})
        name     = team_obj.get("name", f"ID:{team_id}")

        cur_stats = store_current.get_team_stats(team_id)
        prv_stats = store_prev.get_team_stats(team_id)

        cur_pct = cur_stats["win_pct"] if cur_stats else float("nan")
        prv_pct = prv_stats["win_pct"] if prv_stats else float("nan")

        direction = "^ Improving" if delta > 0 else "v Declining"
        lines.append(
            f"  {name:<28}  {prv_pct:>8.1%}   {cur_pct:>8.1%}  "
            f"{delta:>+7.1%}  {direction}"
        )

    if len(sorted_teams) > top_n:
        lines.append(f"  ... and {len(sorted_teams) - top_n} more teams")

    lines.append("  +" + "-" * 69 + "+")
    lines.append(
        f"\n  {len(changes)} team(s) use 75 % current-season weight "
        f"(vs. 60 % default).\n"
        f"  Historical rows for those teams retain the standard 25 / 15 % split."
    )
    return "\n".join(lines)
