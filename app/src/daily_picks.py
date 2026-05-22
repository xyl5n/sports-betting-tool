"""
daily_picks.py
==============
Cross-sport daily pick selection: top-10 game picks (pooled across all bet
markets) + top-5 prop picks from the props scoring pipeline.

Selection algorithm
-------------------
1. Daily reset  — remove non-confirmed open model bets from both ledgers
                  and restore their staked amounts to each ledger's model_bankroll.
2. Collect      — build candidate pools from ALL future MLB + WNBA games across
                  all markets (moneyline, run-line/spread, totals).
                  Only sanity filter: ML odds > -300.  No edge/conf/prob gates.
3. Score        — score = (pick_prob - 0.50) * 0.60 + edge * 0.40
4. Select top MAX_GAME_PICKS across ALL markets combined (no per-market cap).
                  Diversity sort guarantees ≥1 MLB + ≥1 WNBA when both qualify.
                  Deduplicates by (game_id, bet_type).
5. Props        — fetch today's props via the props model pipeline; select top
                  MAX_PROP_PICKS by confidence (≥55% + regression edge filter).
6. Size         — Half Kelly (is_user_bet=True) from each sport's own bankroll.
7. Log          — add_bet() to each sport's Ledger; save both; write
                  data/daily_picks.json and Supabase app_cache.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .kelly import confidence_tier_from_prob, size_bet

if TYPE_CHECKING:
    from .ledger import Ledger

_DAILY_PICKS_FILE    = Path("data/daily_picks.json")
_CACHE_KEY_DAILY_PICKS = "daily_picks"

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_EDGE        = 0.03   # 3 % minimum edge (diagnostic; not a hard gate)
MIN_PROB        = 0.52   # 52 % minimum model probability (informational)
DIVERSITY_EDGE  = 0.03   # edge threshold for per-sport diversity enforcement
DIVERSITY_CONF  = 0.55   # avg pick_prob threshold for diversity enforcement
MAX_GAME_PICKS  = 10     # top game picks across all markets combined
MAX_PROP_PICKS  = 5      # top prop picks by confidence

# Kept for backward compat — callers that read CATEGORIES / MAX_PER_CAT still work.
MAX_PER_CAT     = 5

# ── Category registry (used only for candidate collection; display is now
#    "game_picks" flat list in the new schema) ─────────────────────────────────
CATEGORY_CONFIG: list[dict] = [
    {"key": "moneyline",       "label": "Moneyline",         "bet_types": ("single",)},
    {"key": "run_line_spread", "label": "Run Line / Spread", "bet_types": ("run_line", "spread")},
    {"key": "totals",          "label": "Totals",            "bet_types": ("totals",)},
]
CATEGORIES = tuple(c["key"] for c in CATEGORY_CONFIG)


# ── Row shape normalization ──────────────────────────────────────────────────
# Analysis results arrive in two shapes:
#   * raw nested -- {"game": {...}, "prediction": {...}, "rl_pred": ..., ...}
#     produced freshly by build_for_game during /api/analyze
#   * flat passthrough -- {"home_team": ..., "pick_team": ..., "run_line": ...,
#     "totals": ..., ...} produced by _serialize and hydrated from the daily
#     snapshot on container boot
#
# _collect_mlb / _collect_wnba below crashed with KeyError('game') whenever
# they were handed a flat passthrough (post-restart repick path).  Rather
# than rewrite the picker to read flat keys everywhere, _row_as_nested
# returns a (game, prediction) pair regardless of input shape -- raw rows
# pass through untouched; flat rows get a synthesized minimal nested view.

def _american_to_prob(american) -> float:
    """American moneyline -> raw implied probability (0-1).  Returns
    0.5 for unparseable input so the implied-prob field doesn't poison
    edge math when odds are missing."""
    try:
        v = int(american)
    except (TypeError, ValueError):
        return 0.5
    if v > 0:
        return 100.0 / (v + 100.0)
    return abs(v) / (abs(v) + 100.0)


def _row_as_nested(r: dict) -> tuple[dict, dict] | None:
    """Return (game, prediction) for *r* regardless of whether it's a
    raw nested dict or a flat serialized passthrough.  None for
    malformed input -- the caller skips that row.
    """
    if isinstance(r.get("game"), dict) and isinstance(r.get("prediction"), dict):
        return r["game"], r["prediction"]

    # Flat passthrough -- synthesize the minimal sub-dicts the rest of
    # _collect_* reads.  Field names mirror what _serialize writes.
    home_team = r.get("home_team")
    away_team = r.get("away_team")
    if not (home_team and away_team):
        return None
    home_odds = r.get("home_odds")
    away_odds = r.get("away_odds")
    home_implied = r.get("home_implied_prob")
    if home_implied is None and home_odds is not None and away_odds is not None:
        ho = _american_to_prob(home_odds)
        ao = _american_to_prob(away_odds)
        home_implied = ho / (ho + ao) if (ho + ao) > 0 else 0.5
    if home_implied is None:
        home_implied = 0.5
    g = {
        "id":                r.get("game_id") or r.get("id"),
        "home_team":         home_team,
        "away_team":         away_team,
        "commence_time":     r.get("commence_time", ""),
        "h2h_home_odds":     home_odds,
        "h2h_away_odds":     away_odds,
        "home_implied_prob": float(home_implied),
    }
    # Derive home_win_prob from pick_team + pick_prob so the moneyline
    # branch in _collect_* doesn't get a guaranteed 50/50 split.  No
    # per-model breakdown is available from a flat row so xgb_prob /
    # lr_prob / nn_prob fall back to the same value -- model agreement
    # check downstream treats this as "models agree".
    pick_team = r.get("pick_team")
    pick_prob = r.get("pick_prob")
    if isinstance(pick_prob, (int, float)) and pick_team:
        hp = float(pick_prob) if pick_team == home_team else 1.0 - float(pick_prob)
    else:
        hp = 0.5
    pred = {
        "home_win_prob": hp,
        "xgb_prob":      hp,
        "lr_prob":       hp,
        "nn_prob":       hp,
    }
    return g, pred


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(pick_prob: float, edge: float) -> float:
    return (pick_prob - 0.50) * 0.60 + edge * 0.40


def _avg_prob(picks: list[dict]) -> float:
    if not picks:
        return 0.0
    return sum(p["pick_prob"] for p in picks) / len(picks)


# ── Ledger reset ──────────────────────────────────────────────────────────────

def _reset_model_bets(ledger: "Ledger") -> None:
    """
    Remove ALL non-confirmed open model bets from *ledger* and restore their
    staked amounts to model_bankroll.  Confirmed bets are untouched.
    """
    keep: list[dict] = []
    for b in ledger.data["open_bets"]:
        if b.get("confirmed"):
            keep.append(b)
        else:
            if not b.get("limit_reached", False):
                ledger.data["model_bankroll"] = round(
                    ledger.data["model_bankroll"] + b.get("model_amount", 0.0), 2
                )
    ledger.data["open_bets"] = keep


def reset_today_model_bets(ledger: "Ledger", today_str: str) -> int:
    """
    Remove only today's non-confirmed, unsettled model bets from *ledger*
    and restore their staked amounts to model_bankroll.

    Bets from previous days, confirmed bets, and settled bets (in history)
    are completely untouched.

    Returns the number of bets removed.
    """
    keep: list[dict] = []
    removed = 0
    for b in ledger.data["open_bets"]:
        placed_today = b.get("placed_at", "")[:10] == today_str
        is_model     = not b.get("confirmed", False)
        if placed_today and is_model:
            # Restore the stake
            if not b.get("limit_reached", False):
                ledger.data["model_bankroll"] = round(
                    ledger.data["model_bankroll"] + b.get("model_amount", 0.0), 2
                )
            removed += 1
        else:
            keep.append(b)
    ledger.data["open_bets"] = keep
    return removed


# ── Candidate collection ─────────────────────────────────────────────────────

def _collect_mlb(results: list[dict], now_utc: datetime, ledger: "Ledger") -> dict[str, list]:
    """Return candidate dicts keyed by CATEGORIES for MLB results."""
    cands: dict[str, list] = {c: [] for c in CATEGORIES}

    for r in results:
        # Tolerate both raw nested and flat passthrough rows -- see
        # _row_as_nested up top.  Malformed rows are silently skipped
        # rather than crashing the whole repick.
        normalized = _row_as_nested(r)
        if normalized is None:
            continue
        g, pred = normalized

        try:
            ct = datetime.fromisoformat(g.get("commence_time", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue

        upset_score = float((r.get("upset") or {}).get("score", 0.0))
        matchup     = f"{g['away_team']} @ {g['home_team']}"

        # ── Moneyline ─────────────────────────────────────────────────────────
        hp   = float(pred["home_win_prob"])
        mp   = float(g.get("home_implied_prob", 0.5))
        _xgb = float(pred.get("xgb_prob", hp))
        _lr  = float(pred.get("lr_prob",  hp))
        _nn_raw = pred.get("nn_prob")
        _nn  = float(_nn_raw) if _nn_raw is not None else None

        if hp >= 0.5:
            ml_side, ml_team = "home", g["home_team"]
            ml_odds = int(g.get("h2h_home_odds") or -110)
            ml_p    = hp
            pick_edge = hp - mp
        else:
            ml_side, ml_team = "away", g["away_team"]
            ml_odds = int(g.get("h2h_away_odds") or -110)
            ml_p    = 1.0 - hp
            pick_edge = (1.0 - hp) - (1.0 - mp)

        ml_conf = confidence_tier_from_prob(ml_p)

        if ml_odds > -300:
            cands["moneyline"].append({
                "sport": "mlb", "sport_label": "MLB",
                "game": g, "upset_score": upset_score,
                "side": ml_side, "team": ml_team,
                "odds": ml_odds, "pick_prob": ml_p,
                "edge": pick_edge, "confidence_tier": ml_conf,
                "xgb_prob": _xgb, "lr_prob": _lr, "nn_prob": _nn,
                "bet_type": "single", "prop_line": None,
                "score": _score(ml_p, pick_edge),
                "_ledger": ledger, "matchup": matchup,
            })

        # ── Run line ──────────────────────────────────────────────────────────
        rl = r.get("rl_pred")
        if rl:
            _rx  = float(rl.get("xgb_prob", 0.5))
            _rl2 = float(rl.get("lr_prob",  0.5))
            _rn_raw = rl.get("nn_prob")
            _rn  = float(_rn_raw) if _rn_raw is not None else None
            rl_edge = float(rl.get("edge", 0.0))
            rl_side = rl.get("side", "home")
            rl_team = rl.get("pick_team", g["home_team"] if rl_side == "home" else g["away_team"])
            rl_odds = int(rl.get("pick_odds", -110))
            rl_prob = float(rl.get("pick_prob", 0.5))
            rl_conf = confidence_tier_from_prob(rl_prob)
            _home_pt   = float(rl.get("run_line_point", -1.5))
            _pick_line = _home_pt if rl_side == "home" else -_home_pt
            cands["run_line_spread"].append({
                "sport": "mlb", "sport_label": "MLB",
                "game": g, "upset_score": upset_score,
                "side": rl_side, "team": rl_team,
                "odds": rl_odds, "pick_prob": rl_prob,
                "edge": rl_edge, "confidence_tier": rl_conf,
                "xgb_prob": _rx, "lr_prob": _rl2, "nn_prob": _rn,
                "bet_type": "run_line",
                "prop_line": round(_pick_line, 1),
                "score": _score(rl_prob, rl_edge),
                "_ledger": ledger, "matchup": matchup,
            })

        # ── Totals ────────────────────────────────────────────────────────────
        tp = r.get("totals_pred")
        if tp:
            tp_edge = float(tp.get("edge", 0.0))
            tp_dir  = tp.get("direction", "over")
            tp_line = tp.get("total_line", 8.5)
            tp_odds = int(tp.get("pick_odds", -110))
            tp_prob = float(tp.get("pick_prob", 0.5))
            tp_conf = confidence_tier_from_prob(tp_prob)
            cands["totals"].append({
                "sport": "mlb", "sport_label": "MLB",
                "game": g, "upset_score": 0.0,
                "side": tp_dir,
                "team": f"{tp_dir.title()} {tp_line}",
                "odds": tp_odds, "pick_prob": tp_prob,
                "edge": tp_edge, "confidence_tier": tp_conf,
                "xgb_prob": None, "lr_prob": None, "nn_prob": None,
                "bet_type": "totals",
                "prop_line": float(tp_line),
                "score": _score(tp_prob, tp_edge),
                "_ledger": ledger, "matchup": matchup,
            })

    return cands


def _collect_wnba(results: list[dict], now_utc: datetime, ledger: "Ledger") -> dict[str, list]:
    """Return candidate dicts keyed by CATEGORIES for WNBA results."""
    cands: dict[str, list] = {c: [] for c in CATEGORIES}

    for r in results:
        normalized = _row_as_nested(r)
        if normalized is None:
            continue
        g, pred = normalized

        try:
            ct = datetime.fromisoformat(g.get("commence_time", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if ct <= now_utc:
            continue

        matchup = f"{g['away_team']} @ {g['home_team']}"

        # ── Moneyline ─────────────────────────────────────────────────────────
        hp   = float(pred["home_win_prob"])
        mp   = float(g.get("home_implied_prob", 0.5))
        _xgb = float(pred.get("xgb_prob", hp))
        _lr  = float(pred.get("lr_prob",  hp))

        if hp >= 0.5:
            ml_side, ml_team = "home", g["home_team"]
            ml_odds = int(g.get("h2h_home_odds") or -110)
            ml_p    = hp
            pick_edge = hp - mp
        else:
            ml_side, ml_team = "away", g["away_team"]
            ml_odds = int(g.get("h2h_away_odds") or -110)
            ml_p    = 1.0 - hp
            pick_edge = (1.0 - hp) - (1.0 - mp)

        ml_conf = confidence_tier_from_prob(ml_p)

        if ml_odds > -300:
            cands["moneyline"].append({
                "sport": "wnba", "sport_label": "WNBA",
                "game": g, "upset_score": 0.0,
                "side": ml_side, "team": ml_team,
                "odds": ml_odds, "pick_prob": ml_p,
                "edge": pick_edge, "confidence_tier": ml_conf,
                "xgb_prob": _xgb, "lr_prob": _lr, "nn_prob": None,
                "bet_type": "single", "prop_line": None,
                "score": _score(ml_p, pick_edge),
                "_ledger": ledger, "matchup": matchup,
            })

        # ── Spread ────────────────────────────────────────────────────────────
        sp = r.get("spread_pred")
        if sp:
            sp_edge = float(sp.get("edge", 0.0))
            sp_side = sp.get("side", "home")
            sp_team = sp.get("pick_team", g["home_team"] if sp_side == "home" else g["away_team"])
            sp_odds = int(sp.get("pick_odds", -110))
            sp_prob = float(sp.get("pick_prob", 0.5))
            sp_conf = confidence_tier_from_prob(sp_prob)
            _sp_home_pt  = float(sp.get("spread_line", -5.5))
            _sp_pick_ln  = _sp_home_pt if sp_side == "home" else -_sp_home_pt
            cands["run_line_spread"].append({
                "sport": "wnba", "sport_label": "WNBA",
                "game": g, "upset_score": 0.0,
                "side": sp_side, "team": sp_team,
                "odds": sp_odds, "pick_prob": sp_prob,
                "edge": sp_edge, "confidence_tier": sp_conf,
                "xgb_prob": None, "lr_prob": None, "nn_prob": None,
                "bet_type": "spread",
                "prop_line": round(_sp_pick_ln, 1),
                "score": _score(sp_prob, sp_edge),
                "_ledger": ledger, "matchup": matchup,
            })

        # ── Totals ────────────────────────────────────────────────────────────
        tp = r.get("totals_pred")
        if tp:
            tp_edge = float(tp.get("edge", 0.0))
            tp_dir  = tp.get("direction", "over")
            tp_line = tp.get("total_line", 165.5)
            tp_odds = int(tp.get("pick_odds", -110))
            tp_prob = float(tp.get("pick_prob", 0.5))
            tp_conf = confidence_tier_from_prob(tp_prob)
            cands["totals"].append({
                "sport": "wnba", "sport_label": "WNBA",
                "game": g, "upset_score": 0.0,
                "side": tp_dir,
                "team": f"{tp_dir.title()} {tp_line}",
                "odds": tp_odds, "pick_prob": tp_prob,
                "edge": tp_edge, "confidence_tier": tp_conf,
                "xgb_prob": None, "lr_prob": None, "nn_prob": None,
                "bet_type": "totals",
                "prop_line": float(tp_line),
                "score": _score(tp_prob, tp_edge),
                "_ledger": ledger, "matchup": matchup,
            })

    return cands


# ── Diversity sort (shared by game-pick and guaranteed selectors) ─────────────

def _apply_diversity_sort(pool: list[dict]) -> list[dict]:
    """
    Sort pool by score; if both sports qualify for diversity, guarantee
    ≥ 1 pick from each sport at the front of the returned list.
    """
    if not pool:
        return []

    mlb_pool  = [p for p in pool if p["sport"] == "mlb"]
    wnba_pool = [p for p in pool if p["sport"] == "wnba"]

    def _qualifies(sport_pool: list[dict]) -> bool:
        if not sport_pool:
            return False
        best3 = sorted(sport_pool, key=lambda x: x["score"], reverse=True)[:3]
        return best3[0]["edge"] >= DIVERSITY_EDGE and _avg_prob(best3) >= DIVERSITY_CONF

    if _qualifies(mlb_pool) and _qualifies(wnba_pool):
        mlb_sorted  = sorted(mlb_pool,  key=lambda x: x["score"], reverse=True)
        wnba_sorted = sorted(wnba_pool, key=lambda x: x["score"], reverse=True)
        guaranteed  = [mlb_sorted[0], wnba_sorted[0]]
        rest_pool   = sorted(
            mlb_sorted[1:] + wnba_sorted[1:],
            key=lambda x: x["score"], reverse=True,
        )
        return guaranteed + rest_pool
    else:
        return sorted(pool, key=lambda x: x["score"], reverse=True)


def _diag_pool(label: str, pool: list[dict]) -> None:
    """Print a diagnostic breakdown of a pick pool showing how many pass each filter stage."""
    if not pool:
        print(f"  [picks diag] {label}: 0 candidates in pool")
        return

    total   = len(pool)
    t1_pool = [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] != "low"]
    t3_pool = [p for p in pool if 0 < p["edge"] < MIN_EDGE]
    t4_pool = [p for p in pool if p["edge"] <= 0]

    print(f"  [picks diag] {label}: {total} total candidates")
    print(f"    Tier 1 (edge>={MIN_EDGE:.0%}, conf!='low'): {len(t1_pool)}")
    print(f"    Tier 3 (0<edge<{MIN_EDGE:.0%}):             {len(t3_pool)}")
    print(f"    Tier 4 (edge<=0, below threshold):          {len(t4_pool)}")

    by_score = sorted(pool, key=lambda x: x["score"], reverse=True)
    for p in by_score[:12]:
        sport  = p.get("sport", "?").upper()
        team   = p.get("team", "?")
        bt     = p.get("bet_type", "?")
        edge   = p["edge"]
        prob   = p["pick_prob"]
        conf   = p["confidence_tier"]
        tier   = ("T1" if edge >= MIN_EDGE and conf != "low" else
                  "T2" if edge >= MIN_EDGE else
                  "T3" if edge > 0          else "T4")
        print(
            f"    [{tier}] {sport} {team:30s} [{bt:14s}]  "
            f"edge={edge:+.3f}  prob={prob:.3f}  conf={conf}"
        )


# ── Top-N selector for the combined cross-market pool ────────────────────────

def _select_top_n_combined(pool: list[dict], n: int, label: str = "") -> list[dict]:
    """
    Select the top *n* game picks from a combined cross-market pool ranked by
    score.  Applies diversity sort (≥1 MLB + ≥1 WNBA at front when both qualify).
    Deduplicates by (game_id, bet_type) to prevent the same leg appearing twice.
    """
    if label:
        _diag_pool(label, pool)

    sorted_pool = _apply_diversity_sort(pool)
    selected: list[dict] = []
    seen: set = set()
    for p in sorted_pool:
        key = (p["game"]["id"], p["bet_type"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(p)
        if len(selected) >= n:
            break

    if label:
        print(f"  [picks diag] {label}: selected {len(selected)}/{n} game picks")
    return selected


# Legacy per-category selectors kept for backward compat
# (select_daily_picks no longer calls these, but external code might).

def _select_top5_by_confidence(pool: list[dict], label: str = "") -> list[dict]:
    """Return up to MAX_PER_CAT picks ranked by confidence (legacy helper)."""
    if label:
        _diag_pool(label, pool)
    selected: list[dict] = []
    seen: set = set()
    for p in sorted(pool, key=lambda x: x["pick_prob"], reverse=True):
        key = (p["game"]["id"], p["bet_type"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(p)
        if len(selected) >= MAX_PER_CAT:
            break
    return selected


def _select_top5_guaranteed(pool: list[dict], label: str = "") -> list[dict]:
    """4-tier fallback selector that always fills MAX_PER_CAT slots (legacy helper)."""
    if label:
        _diag_pool(label, pool)
    selected: list[dict] = []
    used_keys: set = set()

    def _add(candidates: list[dict], mark_below: bool = False) -> None:
        for p in candidates:
            if len(selected) >= MAX_PER_CAT:
                break
            key = (p["game"]["id"], p["bet_type"])
            if key not in used_keys:
                used_keys.add(key)
                selected.append(dict(p, below_threshold=True) if mark_below else p)

    t1 = [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] != "low"]
    _add(_apply_diversity_sort(t1))
    if len(selected) < MAX_PER_CAT:
        t2 = sorted(
            [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] == "low"],
            key=lambda x: x["edge"], reverse=True,
        )
        _add(t2)
    if len(selected) < MAX_PER_CAT:
        t3 = sorted(
            [p for p in pool if 0 < p["edge"] < MIN_EDGE],
            key=lambda x: x["edge"], reverse=True,
        )
        _add(t3)
    if len(selected) < MAX_PER_CAT:
        t4 = sorted(
            [p for p in pool if p["edge"] <= 0],
            key=lambda x: x["edge"], reverse=True,
        )
        _add(t4, mark_below=True)
    return selected


# ── Props collection ─────────────────────────────────────────────────────────

def _collect_props(now_utc: datetime) -> list[dict]:
    """
    Fetch today's props, score each via the props model pipeline, and return
    the top MAX_PROP_PICKS candidates ranked by confidence.

    Scoring follows pages/props.py exactly:
      1. Score every API prop with predict(p), storing ALL results in by_pick
         keyed by (player, market, line).  "Pass" recommendations are NOT
         filtered here — they are stored like any other result so the
         over/under deduplication (keep highest confidence) works correctly.
      2. Filter to confidence >= 55% AND regression edge (predicted_value
         clears line by >= 0.5 units when a regressor is available).
      3. Sort by confidence descending, take top MAX_PROP_PICKS.

    The two bugs fixed vs the original version:
      - "Pass" was being skipped *before* storing in by_pick, which discarded
        both sides of a prop and produced an empty result set.
      - `side` was being read from pred.get("recommendation"), which can be
        "Pass"; it should come from p.get("side") (the API's own over/under
        label, which is never "Pass").

    Returns [] on any failure (missing API key, no data, model unavailable).
    """
    import sys as _sys
    _CONF_THRESHOLD = 0.55

    # ── Import guard ──────────────────────────────────────────────────────────
    try:
        from .props_client import get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS
        from .props_model  import predict
    except Exception as exc:
        print(f"PROPS-COLLECT: import failed: {exc}", file=_sys.stderr, flush=True)
        return []

    # ── Fetch today's cached props ─────────────────────────────────────────────
    try:
        payload     = get_client().get_today_props() or {}
        all_markets = payload.get("markets") or {}
    except Exception as exc:
        print(f"PROPS-COLLECT: get_today_props failed: {exc}", file=_sys.stderr, flush=True)
        return []

    all_bucket_markets = set(ALL_PITCHER_MARKETS) | set(ALL_BATTER_MARKETS)
    n_raw = sum(len(v or []) for k, v in all_markets.items() if k in all_bucket_markets)
    print(
        f"PROPS-COLLECT: fetched {len(all_markets)} markets, "
        f"{n_raw} props in target markets "
        f"({len(ALL_PITCHER_MARKETS)} pitcher + {len(ALL_BATTER_MARKETS)} batter market keys)",
        file=_sys.stderr, flush=True,
    )

    # ── Score + dedup ─────────────────────────────────────────────────────────
    # Store ALL results in by_pick keyed by (player, market, line).  Keep the
    # side with higher confidence.  "Pass" recommendations are NOT filtered
    # here — filtering happens after dedup via the confidence threshold below,
    # matching pages/props.py exactly.  (Filtering "Pass" inside the loop was
    # the original bug: it dropped both the Over and Under before they could
    # compete in the dedup, so by_pick ended up empty.)
    by_pick: dict[tuple, dict] = {}
    n_scored = n_predict_err = 0
    for market, props in all_markets.items():
        if market not in all_bucket_markets:
            continue
        for p in (props or []):
            try:
                pred = predict(p)
                n_scored += 1
            except Exception as exc:
                n_predict_err += 1
                print(
                    f"PROPS-COLLECT: predict() failed for "
                    f"{market}/{p.get('player_name')}: {exc}",
                    file=_sys.stderr, flush=True,
                )
                continue
            try:
                line_f = float(p.get("line"))
            except (TypeError, ValueError):
                continue

            key = (p.get("player_name", "?"), market, line_f)
            # Use p.get("side") — the raw API label ("Over" / "Under") —
            # NOT pred.get("recommendation"), which can be "Pass".
            side  = (p.get("side") or "Over").strip().title()
            score = float(pred.get("confidence") or 0.0)
            existing = by_pick.get(key)
            if existing is None or score > existing["confidence"]:
                by_pick[key] = {
                    "player":          p.get("player_name", "?"),
                    "market":          market,
                    "team":            (
                        p.get("home_team") or p.get("team") or
                        p.get("home_team_abbr") or ""
                    ),
                    "line":            line_f,
                    "side":            side,
                    "recommendation":  pred.get("recommendation"),
                    "best_odds":       p.get("best_odds"),
                    "confidence":      round(score, 4),
                    "edge":            round(float(pred.get("edge") or 0.0), 4),
                    "predicted_value": pred.get("predicted_value"),
                    "event_id":        p.get("event_id"),
                    "commence_time":   p.get("commence_time"),
                    "sport":           "mlb",
                }

    print(
        f"PROPS-COLLECT: scored {n_scored} props ({n_predict_err} errors), "
        f"{len(by_pick)} after dedup by (player, market, line)",
        file=_sys.stderr, flush=True,
    )

    # ── Filter: confidence threshold + regression edge ────────────────────────
    def _has_reg_edge(r: dict) -> bool:
        pv = r.get("predicted_value")
        if pv is None:
            return True   # no regressor — confidence alone is sufficient
        try:
            lf = float(r["line"])
            if (r.get("side") or "Over").strip().title() == "Over":
                return pv >= lf + 0.5
            return pv <= lf - 0.5
        except (TypeError, ValueError):
            return True

    n_pass_conf = sum(1 for r in by_pick.values() if r["confidence"] >= _CONF_THRESHOLD)
    rows = [
        r for r in by_pick.values()
        if r["confidence"] >= _CONF_THRESHOLD and _has_reg_edge(r)
    ]
    rows.sort(key=lambda r: -r["confidence"])
    print(
        f"PROPS-COLLECT: {n_pass_conf} pass confidence>={int(_CONF_THRESHOLD*100)}%, "
        f"{len(rows)} also pass regression-edge filter → "
        f"returning top {min(len(rows), MAX_PROP_PICKS)}",
        file=_sys.stderr, flush=True,
    )
    if rows:
        top = rows[0]
        print(
            f"PROPS-COLLECT: top pick: {top['player']} {top['market']} "
            f"{top['side']} {top['line']} conf={top['confidence']:.3f}",
            file=_sys.stderr, flush=True,
        )

    result: list[dict] = []
    for rank, r in enumerate(rows[:MAX_PROP_PICKS], 1):
        result.append({**r, "rank": rank})
    return result


# ── Supabase persistence helpers ─────────────────────────────────────────────

def _save_daily_picks_to_supabase(payload: dict) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_CACHE_KEY_DAILY_PICKS, None, today, payload)
    except Exception:                                                       # noqa: BLE001
        pass


def _load_daily_picks_from_supabase() -> dict | None:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_CACHE_KEY_DAILY_PICKS)
        if not isinstance(row, dict):
            return None
        data = row.get("data") or row
        if isinstance(data, dict) and "picks" in data:
            return data
    except Exception:                                                       # noqa: BLE001
        pass
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

def select_daily_picks(
    mlb_results:  list[dict],
    wnba_results: list[dict],
    mlb_ledger:   "Ledger",
    wnba_ledger:  "Ledger",
    now_utc:      datetime | None = None,
    today_only:   bool = False,
    selection_mode: str = "confidence",
) -> dict:
    """
    Select today's top-10 game picks (pooled across all bet markets) plus
    top-5 prop picks from the props pipeline.

    Returns a dict with shape:
      {
        "generated_at": "<iso>",
        "picks": {
          "game_picks": [...],   # up to MAX_GAME_PICKS (10)
          "prop_picks": [...],   # up to MAX_PROP_PICKS (5)
        }
      }

    Side-effects:
    - Resets non-confirmed model bets in both ledgers and restores their stakes.
      today_only=False (default): clears ALL-TIME non-confirmed model picks.
      today_only=True           : clears only today's picks (preserves prior days).
    - Logs the selected game picks to their sport-specific ledger with Half Kelly.
    - Saves both ledgers to disk.
    - Writes data/daily_picks.json and Supabase app_cache (key="daily_picks").
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # ── 1. Daily reset ────────────────────────────────────────────────────────
    if today_only:
        today_str = now_utc.strftime("%Y-%m-%d")
        reset_today_model_bets(mlb_ledger,  today_str)
        reset_today_model_bets(wnba_ledger, today_str)
    else:
        _reset_model_bets(mlb_ledger)
        _reset_model_bets(wnba_ledger)

    # ── 2. Collect candidates across all markets ──────────────────────────────
    mlb_cands  = _collect_mlb(mlb_results,   now_utc, mlb_ledger)
    wnba_cands = _collect_wnba(wnba_results, now_utc, wnba_ledger)

    # ── 3. Pool ALL candidates (all markets, both sports) into one list ───────
    # Dedup by (game_id, bet_type) — keep the entry with the higher score so
    # no leg appears twice.
    deduped: dict[tuple, dict] = {}
    for cat in CATEGORIES:
        for c in mlb_cands[cat] + wnba_cands[cat]:
            key = (c["game"]["id"], c["bet_type"])
            if key not in deduped or c["score"] > deduped[key]["score"]:
                deduped[key] = c
    all_candidates = list(deduped.values())

    # ── 4. Select top MAX_GAME_PICKS from the combined pool ───────────────────
    print(
        f"  [picks diag] ===== Daily picks ({len(all_candidates)} combined candidates) ====="
    )
    game_picks_raw = _select_top_n_combined(
        all_candidates, MAX_GAME_PICKS, label="COMBINED"
    )
    print("  [picks diag] ===========================================")

    # ── 5. Collect top prop picks ─────────────────────────────────────────────
    prop_picks_raw = _collect_props(now_utc)

    # ── 6. Size game bets (Half Kelly) and log to ledgers ─────────────────────
    result_game_picks: list[dict] = []

    for rank, pick in enumerate(game_picks_raw, 1):
        ledger    = pick.pop("_ledger")       # extract non-serialisable ref
        g         = pick["game"]
        sport     = pick["sport"]
        sport_key = "baseball_mlb" if sport == "mlb" else "basketball_wnba"

        br       = ledger.data["model_bankroll"]
        starting = ledger.data.get("model_starting_bankroll", 1000.0)

        _, amt, _, size_display = size_bet(
            pick["pick_prob"],
            pick["odds"],
            br,
            starting,
            pick.get("upset_score", 0.0),
            pick["confidence_tier"],
            is_user_bet=True,   # Half Kelly
        )

        if not ledger.has_bet(g["id"], pick["bet_type"]):
            ledger.add_bet(
                game=g,
                sport=sport,
                sport_key=sport_key,
                side=pick["side"],
                team=pick["team"],
                odds=pick["odds"],
                model_prob=pick["pick_prob"],
                edge=pick["edge"],
                model_amount=round(amt, 2),
                confirmed=False,
                confirmed_amount=0.0,
                bet_type=pick["bet_type"],
                confidence_tier=pick["confidence_tier"],
                prop_line=pick.get("prop_line"),
                xgb_prob=pick.get("xgb_prob"),
                lr_prob=pick.get("lr_prob"),
                nn_prob=pick.get("nn_prob"),
            )

        result_game_picks.append({
            "rank":            rank,
            "sport":           sport,
            "sport_label":     pick["sport_label"],
            "matchup":         pick["matchup"],
            "team":            pick["team"],
            "side":            pick["side"],
            "odds":            pick["odds"],
            "bet_type":        pick["bet_type"],
            "pick_prob":       round(pick["pick_prob"], 4),
            "edge":            round(pick["edge"], 4),
            "confidence_tier": pick["confidence_tier"],
            "below_threshold": pick.get("below_threshold", False),
            "score":           round(pick["score"], 4),
            "model_amount":    round(amt, 2),
            "size_display":    size_display,
            "game_id":         g["id"],
            "home_team":       g["home_team"],
            "away_team":       g["away_team"],
            "commence_time":   g.get("commence_time", ""),
            "prop_line":       pick.get("prop_line"),
        })

    # ── 7. Persist ────────────────────────────────────────────────────────────
    mlb_ledger.save()
    wnba_ledger.save()

    payload = {
        "generated_at": now_utc.isoformat(),
        "picks": {
            "game_picks": result_game_picks,
            "prop_picks": prop_picks_raw,
        },
    }
    _DAILY_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DAILY_PICKS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _save_daily_picks_to_supabase(payload)

    return payload


# ── Read-only helper ──────────────────────────────────────────────────────────

def load_daily_picks() -> dict:
    """Return the most-recently saved daily picks, preferring Supabase over
    the local file.  Normalizes old-schema picks (per-category keys) to the
    new flat-list schema on the fly so callers always see game_picks /
    prop_picks.
    """
    _EMPTY = {"generated_at": None, "picks": {"game_picks": [], "prop_picks": []}}

    # Try Supabase first so picks survive Railway redeploys.
    payload = _load_daily_picks_from_supabase()

    # Fall back to local file.
    if not payload and _DAILY_PICKS_FILE.exists():
        try:
            payload = json.loads(_DAILY_PICKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            payload = None

    if not payload:
        return _EMPTY

    picks = payload.get("picks") or {}

    # ── Normalize old schema (moneyline / run_line_spread / totals keys) ──────
    # Produced by versions of this module before the redesign.  Flatten into
    # game_picks so model.py and admin.py see the same structure regardless of
    # when the file was last written.
    if "game_picks" not in picks:
        old_games: list[dict] = []
        for cat in ("moneyline", "run_line_spread", "totals"):
            old_games.extend(picks.get(cat) or [])
        old_games.sort(key=lambda p: p.get("score", 0), reverse=True)
        payload = dict(payload, picks={
            "game_picks": old_games[:MAX_GAME_PICKS],
            "prop_picks": picks.get("prop_picks") or [],
        })

    return payload
