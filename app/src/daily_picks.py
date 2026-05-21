"""
daily_picks.py
==============
Cross-sport daily pick selection: top-5 per bet category (Moneyline, Run Line/Spread, Totals)
drawn from the combined MLB + WNBA candidate pools.

Selection algorithm
-------------------
1. Daily reset  — remove ALL non-confirmed open model bets from both ledgers
                  and restore their staked amounts to each ledger's model_bankroll.
2. Collect      — build candidate pools from ALL future MLB + WNBA games.
                  Only sanity filter: ML odds > -300.  No edge/conf/prob gates.
3. Score        — score = (pick_prob - 0.50) * 0.60 + edge * 0.40
4. Select exactly 5 per category via 4-tier fallback:
                  Tier 1 — edge >= 3% AND conf != "low"  (diversity-aware, by score)
                  Tier 2 — edge >= 3% AND conf == "low"  (ranked by edge)
                  Tier 3 — 0 < edge < 3%, any conf       (ranked by edge)
                  Tier 4 — edge <= 0, marked below_threshold (least-negative first)
5. Size         — Half Kelly (is_user_bet=True) from each sport's own bankroll.
6. Log          — add_bet() to each sport's Ledger; save both; write daily_picks.json.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .kelly import confidence_tier_from_prob, size_bet

if TYPE_CHECKING:
    from .ledger import Ledger

_DAILY_PICKS_FILE = Path("data/daily_picks.json")

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_EDGE        = 0.03   # 3 % minimum edge to qualify
MIN_PROB        = 0.52   # 52 % minimum model probability
DIVERSITY_EDGE  = 0.03   # edge threshold for per-sport diversity enforcement
DIVERSITY_CONF  = 0.55   # avg pick_prob threshold for diversity enforcement
MAX_PER_CAT     = 5      # top picks per category (always filled — never left empty)

# ── Extensible category registry ─────────────────────────────────────────────
# To add a new bet category (NRFI, player props, first inning, etc.) append
# ONE entry here.  Everything downstream — selection, sizing, persistence,
# and the frontend — picks it up automatically without any other code changes.
#
# Fields
# ------
# key        str   — storage/JSON key (snake_case, must be unique)
# label      str   — human-readable display name
# bet_types  tuple — which bet_type values in candidate dicts belong here
CATEGORY_CONFIG: list[dict] = [
    {
        "key":       "moneyline",
        "label":     "Moneyline",
        "bet_types": ("single",),
    },
    {
        "key":       "run_line_spread",
        "label":     "Run Line / Spread",
        "bet_types": ("run_line", "spread"),
    },
    {
        "key":       "totals",
        "label":     "Totals",
        "bet_types": ("totals",),
    },
    # ── Future categories — add one dict here, no other changes needed ────────
    # {"key": "nrfi",         "label": "NRFI",            "bet_types": ("nrfi",)},
    # {"key": "first_inning", "label": "1st Inning",      "bet_types": ("first_inning",)},
    # {"key": "player_props", "label": "Player Props",    "bet_types": ("player_prop",)},
]

# Derived tuple — used wherever the old CATEGORIES constant was referenced.
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
        # Step 1 — pure confidence: probability of the picked outcome, then
        # tier from that prob (no odds, no edge, no model-agreement).
        # Step 2 — edge: model prob vs market implied prob, computed AFTER
        # the side is fixed.  The two are deliberately independent.
        hp   = float(pred["home_win_prob"])
        mp   = float(g.get("home_implied_prob", 0.5))
        _xgb = float(pred.get("xgb_prob", hp))
        _lr  = float(pred.get("lr_prob",  hp))
        _nn_raw = pred.get("nn_prob")
        _nn  = float(_nn_raw) if _nn_raw is not None else None

        # Always pick model-preferred side (home if model prob >= 0.5)
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

        if ml_odds > -300:   # sanity check: skip heavily juiced lines
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
            # Tier from pick_prob only — independent of edge or model-agreement
            rl_conf = confidence_tier_from_prob(rl_prob)
            # Signed pick-team line: home_pt is the home team's handicap (e.g. -1.5).
            # If we're picking the home side, the pick team gets home_pt.
            # If picking the away side, the pick team gets -home_pt.
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
                "prop_line": round(_pick_line, 1),   # signed pick-team line
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
            # Tier from pick_prob only — independent of edge or model-agreement
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
        # Tolerate both raw nested and flat passthrough rows.  See
        # _row_as_nested up top for the synth path.
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
        # Step 1 — pure confidence: pick the side, derive tier from pick_prob.
        # Step 2 — edge: model prob vs market implied prob. Independent.
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

        if ml_odds > -300:   # sanity check: skip heavily juiced lines
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
            # Tier from pick_prob only — independent of edge or model-agreement
            sp_conf = confidence_tier_from_prob(sp_prob)
            # Signed pick-team spread: spread_line is the home team's handicap.
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
                "prop_line": round(_sp_pick_ln, 1),  # signed pick-team spread
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
            # Tier from pick_prob only — independent of edge or model-agreement
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


# ── Guaranteed top-5 selection with 4-tier fallback ──────────────────────────

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

    total      = len(pool)
    t1_pool    = [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] != "low"]
    t2_pool    = [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] == "low"]
    t3_pool    = [p for p in pool if 0 < p["edge"] < MIN_EDGE]
    t4_pool    = [p for p in pool if p["edge"] <= 0]
    agree_pool = [p for p in pool if p.get("xgb_prob") is not None]  # ML-model picks only

    print(f"  [picks diag] {label}: {total} total candidates")
    print(f"    Tier 1 (edge>={MIN_EDGE:.0%}, conf!='low'): {len(t1_pool)}")
    print(f"    Tier 2 (edge>={MIN_EDGE:.0%}, conf=='low'): {len(t2_pool)}")
    print(f"    Tier 3 (0<edge<{MIN_EDGE:.0%}):             {len(t3_pool)}")
    print(f"    Tier 4 (edge<=0, below threshold):          {len(t4_pool)}")

    # Per-pick detail (up to 10) — edge, conf tier, pick_prob
    by_score = sorted(pool, key=lambda x: x["score"], reverse=True)
    for p in by_score[:10]:
        sport  = p.get("sport", "?").upper()
        team   = p.get("team", "?")
        edge   = p["edge"]
        prob   = p["pick_prob"]
        conf   = p["confidence_tier"]
        xp     = p.get("xgb_prob")
        lp     = p.get("lr_prob")
        np_    = p.get("nn_prob")
        xstr   = f"XGB={xp:.3f}" if xp is not None else ""
        lstr   = f"LR={lp:.3f}"  if lp is not None else ""
        nstr   = f"NN={np_:.3f}" if np_ is not None else ""
        models = "  ".join(s for s in [xstr, lstr, nstr] if s)
        tier   = ("T1" if edge >= MIN_EDGE and conf != "low" else
                  "T2" if edge >= MIN_EDGE else
                  "T3" if edge > 0          else "T4")
        print(
            f"    [{tier}] {sport} {team:30s}  "
            f"edge={edge:+.3f}  prob={prob:.3f}  conf={conf}  {models}"
        )


def _select_top5_by_confidence(pool: list[dict], label: str = "") -> list[dict]:
    """
    Return exactly MAX_PER_CAT picks ranked purely by pick_prob (confidence)
    descending.  No edge gate, no tier fallback — the model's most-confident
    bets win.  Deduplicates by (game_id, bet_type) so the same leg can't
    appear twice in one category.
    """
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

    if label:
        print(f"  [picks diag] {label}: selected {len(selected)}/{MAX_PER_CAT} by pure confidence")
    return selected


def _select_top5_guaranteed(pool: list[dict], label: str = "") -> list[dict]:
    """
    Always return exactly MAX_PER_CAT picks using a 4-tier fallback:

      Tier 1: edge >= MIN_EDGE AND conf != "low"   (diversity-aware, ranked by score)
      Tier 2: edge >= MIN_EDGE AND conf == "low"   (ranked by edge desc)
      Tier 3: 0 < edge < MIN_EDGE, any conf        (ranked by edge desc)
      Tier 4: edge <= 0, marked below_threshold    (ranked by edge desc; least negative first)

    Picks are deduplicated by (game_id, bet_type) across tiers.
    """
    if label:
        _diag_pool(label, pool)

    selected: list[dict] = []
    used_keys: set = set()   # (game_id, bet_type)

    def _add(candidates: list[dict], mark_below: bool = False) -> None:
        for p in candidates:
            if len(selected) >= MAX_PER_CAT:
                break
            key = (p["game"]["id"], p["bet_type"])
            if key not in used_keys:
                used_keys.add(key)
                selected.append(dict(p, below_threshold=True) if mark_below else p)

    # Tier 1 — qualifying picks with sport diversity
    t1 = [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] != "low"]
    _add(_apply_diversity_sort(t1))

    # Tier 2 — low-confidence but meets edge threshold
    if len(selected) < MAX_PER_CAT:
        t2 = sorted(
            [p for p in pool if p["edge"] >= MIN_EDGE and p["confidence_tier"] == "low"],
            key=lambda x: x["edge"], reverse=True,
        )
        _add(t2)

    # Tier 3 — positive edge below MIN_EDGE threshold, any confidence
    if len(selected) < MAX_PER_CAT:
        t3 = sorted(
            [p for p in pool if 0 < p["edge"] < MIN_EDGE],
            key=lambda x: x["edge"], reverse=True,
        )
        _add(t3)

    # Tier 4 — zero/negative edge; mark below_threshold
    if len(selected) < MAX_PER_CAT:
        t4 = sorted(
            [p for p in pool if p["edge"] <= 0],
            key=lambda x: x["edge"], reverse=True,  # least negative first
        )
        _add(t4, mark_below=True)

    if label:
        n_t1 = sum(1 for p in selected if p["edge"] >= MIN_EDGE and p["confidence_tier"] != "low" and not p.get("below_threshold"))
        n_t2 = sum(1 for p in selected if p["edge"] >= MIN_EDGE and p["confidence_tier"] == "low"  and not p.get("below_threshold"))
        n_t3 = sum(1 for p in selected if 0 < p["edge"] < MIN_EDGE and not p.get("below_threshold"))
        n_t4 = sum(1 for p in selected if p.get("below_threshold"))
        print(
            f"  [picks diag] {label}: selected {len(selected)}/{MAX_PER_CAT} "
            f"(T1={n_t1} T2={n_t2} T3={n_t3} T4/below={n_t4})"
        )

    return selected


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
    Select today's top-5 picks per category from MLB + WNBA combined pools.

    Returns a dict with shape:
      {
        "generated_at": "<iso>",
        "picks": {
          "moneyline":       [...],   # up to 5
          "run_line_spread": [...],   # up to 5
          "totals":          [...],   # up to 5
        }
      }

    Side-effects:
    - Resets non-confirmed model bets in both ledgers and restores their stakes.
      today_only=False (default): clears ALL-TIME non-confirmed model picks.
      today_only=True           : clears only today's picks (preserves prior days).
    - Logs the selected picks to their sport-specific ledger with Half Kelly.
    - Saves both ledgers to disk.
    - Writes data/daily_picks.json.
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

    # ── 2. Collect candidates ─────────────────────────────────────────────────
    mlb_cands  = _collect_mlb(mlb_results,   now_utc, mlb_ledger)
    wnba_cands = _collect_wnba(wnba_results, now_utc, wnba_ledger)

    # Merge per category and deduplicate by (game_id, bet_type) — keep highest score
    combined: dict[str, list] = {}
    for cat in CATEGORIES:
        merged = mlb_cands[cat] + wnba_cands[cat]
        deduped: dict[tuple, dict] = {}
        for c in merged:
            key = (c["game"]["id"], c["bet_type"])
            if key not in deduped or c["score"] > deduped[key]["score"]:
                deduped[key] = c
        combined[cat] = list(deduped.values())

    # ── 3-4. Score + select top-5 per category ────────────────────────────────
    print(f"  [picks diag] ===== Daily picks filter diagnostic (mode={selection_mode}) =====")
    _selector = (
        _select_top5_by_confidence
        if selection_mode == "confidence"
        else _select_top5_guaranteed
    )
    selected: dict[str, list] = {
        cat: _selector(pool, label=cat.upper())
        for cat, pool in combined.items()
    }
    print("  [picks diag] ===========================================")

    # ── 5-6. Size bets (Half Kelly) and log to ledgers ────────────────────────
    result_picks: dict[str, list] = {cat: [] for cat in CATEGORIES}

    for cat, picks in selected.items():
        for rank, pick in enumerate(picks, 1):
            ledger  = pick.pop("_ledger")       # extract non-serialisable ref
            g       = pick["game"]
            sport   = pick["sport"]
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

            result_picks[cat].append({
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

    # ── Persist ───────────────────────────────────────────────────────────────
    mlb_ledger.save()
    wnba_ledger.save()

    payload = {
        "generated_at": now_utc.isoformat(),
        "picks":        result_picks,
    }
    _DAILY_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DAILY_PICKS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return payload


# ── Read-only helper ──────────────────────────────────────────────────────────

def load_daily_picks() -> dict:
    """Return the most-recently saved daily picks, or an empty structure."""
    if _DAILY_PICKS_FILE.exists():
        try:
            return json.loads(_DAILY_PICKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "generated_at": None,
        "picks": {cat: [] for cat in CATEGORIES},
    }
