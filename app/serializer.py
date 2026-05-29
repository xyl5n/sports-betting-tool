"""serializer.py -- analysis-result serialization layer (PR #283).

Lifts the four-function cluster that converts raw analysis state into
the dict shape served to the UI and consumed by AI prompts:

    _serialize                 (MLB game -> serialized dict)
    _serialize_wnba            (WNBA game -> serialized dict)
    _apply_correlation_rules   (cross-bet correlation adjustments)
    _format_rl_shap            (run-line SHAP feature labels)

All four are pure data-shaping functions -- NO Flask coupling at all
(no jsonify, request, app.config, render_template).  The audit in
PR #283's "Phase 2 Audit" report (branch claude/audit-serializer-283)
documents this in detail.

Consumers (Flask routes in app.py and _build_chat_context /
_rerun_single_game) reach these via `from serializer import *`.

Direction:
    serializer.py -> state.py / utils.py / src.kelly   (one-way down)
    app.py        -> serializer.py                      (one-way down)
    scheduler.py  -> serializer.py                      (one-way down)
    serializer.py NEVER imports app.py or scheduler.py.
"""
from __future__ import annotations

import logging

import numpy as np

# src.kelly is required.  app.py historically wraps this in a try/except
# that exits the process on failure; here we let an ImportError surface
# naturally -- the module loader treats it the same way (process death)
# and avoids duplicating the STARTUP-print plumbing.
from src.kelly import size_bet, confidence_tier_from_prob

from state import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403

# Parallel reference to the same logger app.py and scheduler.py own.
# Python's logging module is a process-wide name-keyed registry, so
# getLogger("sports_betting") returns the same singleton in every
# module that calls it.  This is the same approved pattern used in
# scheduler.py since PR #279 (see migration_log.txt).
_logger = logging.getLogger("sports_betting")

__all__ = [
    "_serialize",
    "_serialize_wnba",
    "_apply_correlation_rules",
    "_format_rl_shap",
]

# moved from app.py:1182
def _serialize(r: dict, bankroll: float, sport: str = "mlb", starting_bankroll: float | None = None) -> dict:
    """Convert a raw analysis result to a JSON-safe dict for the frontend.

    Tolerates flat passthrough input: if *r* lacks the nested `game` +
    `prediction` keys but already carries the flat serialized shape
    (home_team / away_team / pick_team at the top level), return a copy
    of it as-is.  The two callers that hit this path are the cached-
    analyze branches (lines ~4153 and ~7285) which read directly from
    _analysis_state["results"]; those entries may already be flat after
    a snapshot hydration and re-serializing them crashed with
    KeyError('game').
    """
    if not isinstance(r.get("game"), dict):
        if r.get("home_team") and r.get("away_team"):
            return dict(r)
        # Not flat-shape either -- let the original KeyError propagate
        # so we don't silently swallow truly malformed input.
    game = r["game"]
    pred = r["prediction"]
    shap_data = r.get("shap")
    meta = r.get("meta") or {}
    rl_pred     = r.get("rl_pred")
    totals_pred = r.get("totals_pred")

    home_prob   = float(pred["home_win_prob"])
    xgb_prob    = float(pred.get("xgb_prob", home_prob))
    lr_prob     = float(pred.get("lr_prob",  home_prob))
    _nn         = pred.get("nn_prob")
    nn_prob     = float(_nn) if _nn is not None else None
    agree       = bool(pred.get("models_agree", True))
    market_prob = float(game["home_implied_prob"])
    home_edge   = home_prob - market_prob

    if home_prob >= 0.5:
        pick_side  = "home"
        pick_team  = game["home_team"]
        pick_odds  = int(game.get("h2h_home_odds") or -110)
        pick_prob  = home_prob
        pick_edge  = home_edge
    else:
        pick_side  = "away"
        pick_team  = game["away_team"]
        pick_odds  = int(game.get("h2h_away_odds") or -110)
        pick_prob  = 1.0 - home_prob
        pick_edge  = -home_edge

    # ── Upset factor adjustments ──────────────────────────────────────────────
    upset       = r.get("upset") or {}
    conf_red    = float(upset.get("confidence_reduction", 0.0))
    upset_score = float(upset.get("score", 0.0))
    s_bankroll  = starting_bankroll if starting_bankroll is not None else bankroll

    # Adjust displayed probability (floor at 0.48)
    pick_prob_adj = max(0.48, pick_prob - conf_red)
    pick_edge_adj = pick_prob_adj - (market_prob if pick_side == "home" else 1.0 - market_prob)

    # ML confidence tier — pure probability of the picked outcome, no edge
    # or model-agreement input.  Strong > 0.62, Moderate 0.52-0.62, Low < 0.52.
    ml_conf = confidence_tier_from_prob(pick_prob_adj)

    # Edge is computed independently and gates value separately from tier.
    # EV_MIN_EDGE (module-level) is the single source of truth -- changing
    # it here also updates the EV Scan label on the home page.
    is_value = (
        ml_conf in ("strong", "moderate") and
        pick_edge_adj >= EV_MIN_EDGE and
        pick_odds > -300 and
        pick_prob_adj >= 0.52
    )

    bet_dollars = bet_units = 0.0
    if bankroll > 0 and is_value:
        _, bet_dollars, bet_units, _ = size_bet(
            pick_prob_adj, pick_odds, bankroll, s_bankroll,
            upset_score, ml_conf, is_user_bet=True,
        )
        bet_dollars = round(bet_dollars, 2)
        bet_units   = round(bet_units, 1)

    out: dict = {
        "game_id":          game["id"],
        "home_team":        game["home_team"],
        "away_team":        game["away_team"],
        "commence_time":    game.get("commence_time", ""),
        "home_odds":        int(game.get("h2h_home_odds") or -110),
        "away_odds":        int(game.get("h2h_away_odds") or -110),
        "spread":           game.get("spread"),
        "home_implied_prob": market_prob,
        "home_win_prob":    home_prob,         # raw model output (unmodified)
        "xgb_prob":         xgb_prob,
        "lr_prob":          lr_prob,
        "nn_prob":          nn_prob,
        "models_agree":     agree,
        "conflict":         not agree,
        "pick_side":        pick_side,
        "pick_team":        pick_team,
        "pick_odds":        pick_odds,
        "pick_prob":        pick_prob_adj,     # upset-adjusted confidence
        "pick_edge":        pick_edge_adj,
        "confidence_tier":  ml_conf,
        "bet_dollars":      bet_dollars,
        "bet_units":        bet_units,
        "value_pick":       is_value,
        "upset_factor":     upset,
    }

    if shap_data:
        out["shap"] = {
            "base_value": float(shap_data["base_value"]),
            "source":     shap_data.get("source", ""),
            "values": [
                {
                    "feature":       v["feature"],
                    "label":         _FEATURE_LABELS.get(v["feature"], v["feature"]),
                    "shap_value":    float(v["shap_value"]),
                    "feature_value": float(v["feature_value"]),
                }
                for v in shap_data["shap_values"][:8]
            ],
        }

    h = meta.get("home_stats") or {}
    a = meta.get("away_stats") or {}
    if h:
        out["home_stats"] = {k: float(v) for k, v in h.items()
                             if isinstance(v, (int, float, np.floating, np.integer))}
    if a:
        out["away_stats"] = {k: float(v) for k, v in a.items()
                             if isinstance(v, (int, float, np.floating, np.integer))}

    # Starting pitcher details -- carry the full set of fields the
    # matchup page needs in one shot.  The pitcher_client pipeline
    # (statsapi.mlb.com season / homeAndAway / gameLog / people /
    # teams) populates every field below; missing values use neutral
    # defaults and the matchup page applies its own sanity bounds on
    # top before rendering.
    home_sp = meta.get("home_sp") or {}
    away_sp = meta.get("away_sp") or {}
    if home_sp:
        out["home_sp"] = {
            "era":         round(float(home_sp.get("era", 4.5)), 2),
            "whip":        round(float(home_sp.get("whip", 1.3)), 2),
            # k_rate stays as a fraction (0.214 = 21.4%).  The display
            # layer multiplies by 100 via {:.1%} -- the old `* 100`
            # here double-converted into "2140%".
            "k_rate":      round(float(home_sp.get("k_rate", 0.215)), 4),
            "k_per_9":     round(float(home_sp.get("k_per_9", 8.50)), 2),
            "bb9":         round(float(home_sp.get("bb9", 3.30)), 2),
            "era_home":    round(float(home_sp.get("era_home", home_sp.get("era", 4.5))), 2),
            "era_away":    round(float(home_sp.get("era_away", home_sp.get("era", 4.5))), 2),
            "last3_era":   round(float(home_sp.get("last3_era", home_sp.get("era", 4.5))), 2),
            "wins":        int(home_sp.get("wins")   or 0),
            "losses":      int(home_sp.get("losses") or 0),
            "hand":        "LHP" if home_sp.get("hand") == 1 else "RHP",
            "rest":        int(home_sp.get("rest", 4)),
            # Identity fields straight from pitcher_client's new
            # /people + /teams fetches.  Empty strings flag TBD on
            # the matchup page.
            "full_name":   str(home_sp.get("full_name") or "").strip(),
            "team_abbrev": str(home_sp.get("team_abbrev") or "").strip().upper(),
        }
    if away_sp:
        out["away_sp"] = {
            "era":         round(float(away_sp.get("era", 4.5)), 2),
            "whip":        round(float(away_sp.get("whip", 1.3)), 2),
            "k_rate":      round(float(away_sp.get("k_rate", 0.215)), 4),
            "k_per_9":     round(float(away_sp.get("k_per_9", 8.50)), 2),
            "bb9":         round(float(away_sp.get("bb9", 3.30)), 2),
            "era_home":    round(float(away_sp.get("era_home", away_sp.get("era", 4.5))), 2),
            "era_away":    round(float(away_sp.get("era_away", away_sp.get("era", 4.5))), 2),
            "last3_era":   round(float(away_sp.get("last3_era", away_sp.get("era", 4.5))), 2),
            "wins":        int(away_sp.get("wins")   or 0),
            "losses":      int(away_sp.get("losses") or 0),
            "hand":        "LHP" if away_sp.get("hand") == 1 else "RHP",
            "rest":        int(away_sp.get("rest", 4)),
            "full_name":   str(away_sp.get("full_name") or "").strip(),
            "team_abbrev": str(away_sp.get("team_abbrev") or "").strip().upper(),
        }

    # Ballpark & weather
    # park_run_factor is stored 1.000-base in src/park_factors.py
    # (1.000 = league average, used directly by the totals model as a
    # multiplier).  The matchup page wants the FanGraphs / pybaseball
    # convention: 100-base, where >100 = hitter-friendly and <100 =
    # pitcher-friendly.  Convert here so the model input is unchanged
    # but the displayed value reads like park factors anywhere else.
    park_run = meta.get("park_run_factor")
    if park_run is not None:
        try:
            out["park_run_factor"] = int(round(float(park_run) * 100))
        except (TypeError, ValueError):
            out["park_run_factor"] = 100
    # Home ballpark name -- pull from the static venue map keyed by
    # team name.  The matchup-detail Venue section uses this to label
    # the park-factor number (e.g. "Coors Field  142 (Hitter Friendly)").
    try:
        from src.park_factors import get_venue_name
        venue = get_venue_name(game.get("home_team", ""))
        if venue:
            out["venue_name"] = venue
    except Exception:                                                     # noqa: BLE001
        pass
    wx = meta.get("weather") or {}
    if wx:
        out["weather"] = {
            "wind_speed":    round(float(wx.get("wind_speed", 0)), 1),
            "wind_direction": round(float(wx.get("wind_direction", 0)), 0),
            "temperature":   round(float(wx.get("temperature", 72)), 1),
        }

    # Bullpen
    home_bp = meta.get("home_bp") or {}
    away_bp = meta.get("away_bp") or {}
    if home_bp:
        out["home_bp"] = {
            "era":     round(float(home_bp.get("era", 4.2)), 2),
            "fatigue": int(home_bp.get("fatigue", 2)),
        }
    if away_bp:
        out["away_bp"] = {
            "era":     round(float(away_bp.get("era", 4.2)), 2),
            "fatigue": int(away_bp.get("fatigue", 2)),
        }

    # Lineup & line movement
    if "lineup_confirmed" in meta:
        out["lineup_confirmed"] = bool(meta["lineup_confirmed"])
    if "line_movement" in meta:
        out["line_movement"] = round(float(meta["line_movement"]), 4)

    # ── Run line ──────────────────────────────────────────────────────────────
    if rl_pred is not None:
        rl_prob_adj = max(0.48, float(rl_pred["pick_prob"]) - conf_red)
        # RL tier from pick_prob only.  Composed with the conditional hurdle
        # in run_line_model.predict, rl_prob_adj is bounded above by the
        # moneyline pick_prob for the same home team, so the resulting tier
        # is bounded above by ml_conf when both pick HOME.
        rl_conf = confidence_tier_from_prob(rl_prob_adj)
        rl_shap = rl_pred.get("shap")
        rl_mkt_side = "home" if rl_pred["side"] == "home" else "away"
        rl_mkt_prob = (
            game.get("home_implied_prob", 0.5)
            if rl_mkt_side == "home"
            else 1.0 - game.get("home_implied_prob", 0.5)
        )
        rl_edge_adj = rl_prob_adj - rl_mkt_prob

        # ── Correlation consistency is now structural, not patched ───────────
        # The conditional run-line hurdle in run_line_model._train guarantees
        # P(cover -1.5) <= P(win outright) by construction (each sub-model
        # multiplied by the matching moneyline sub-model probability).
        # Combined with the prob-based confidence_tier_from_prob (monotonic
        # in pick_prob), this implies rl_conf <= ml_conf whenever both pick
        # the same HOME team — no downstream floor/cap needed.  The flags
        # remain in the response schema for backwards compatibility but are
        # always False since no repair is performed.
        ml_corr = False
        rl_corr = False

        # Re-derive RL sizing from rl_prob_adj
        rl_is_value = (
            rl_pred.get("value_bet") and
            rl_conf in ("strong", "moderate") and
            rl_prob_adj >= 0.52
        )
        rl_kelly = 0.0
        if bankroll > 0 and rl_is_value:
            _, rl_kelly, _, _ = size_bet(
                rl_prob_adj, rl_pred["pick_odds"], bankroll, s_bankroll,
                upset_score, rl_conf, is_user_bet=True,
            )
            rl_kelly = round(rl_kelly, 2)

        out["run_line"] = {
            "home_cover_prob":      round(rl_pred["home_cover_prob"], 4),
            "xgb_prob":             round(rl_pred["xgb_prob"], 4),
            "lr_prob":              round(rl_pred["lr_prob"], 4),
            "models_agree":         rl_pred["models_agree"],
            "conflict":             rl_pred["conflict"],
            "confidence_tier":      rl_conf,
            "side":                 rl_pred["side"],
            "pick_team":            rl_pred["pick_team"],
            "pick_odds":            rl_pred["pick_odds"],
            "pick_prob":            round(rl_prob_adj, 4),
            "edge":                 round(rl_edge_adj, 4),
            "value_bet":            rl_is_value,
            "confidence":           round(rl_prob_adj, 4),
            "run_line_point":       rl_pred["run_line_point"],
            "run_line_home_odds":   rl_pred["run_line_home_odds"],
            "run_line_away_odds":   rl_pred["run_line_away_odds"],
            "bet_dollars":          rl_kelly,
            "rl_correlated_with_ml": rl_corr,
            "shap": _format_rl_shap(rl_shap) if rl_shap else None,
        }
        # Validate: ML favorite must always carry -1.5; correct if API data is flipped
        _hml = out["home_odds"]
        _aml = out["away_odds"]
        _expected_pt = -1.5 if _hml < _aml else 1.5
        _actual_pt   = out["run_line"].get("run_line_point")
        if _actual_pt is not None and abs(float(_actual_pt) - _expected_pt) > 0.01:
            _logger.warning(
                "[RL Validation] %s vs %s: run_line_point=%s but home_ml=%s vs away_ml=%s, "
                "expected %s — auto-correcting.",
                out["home_team"], out["away_team"], _actual_pt, _hml, _aml, _expected_pt,
            )
            out["run_line"]["run_line_point"] = _expected_pt
            out["run_line"]["run_line_home_odds"], out["run_line"]["run_line_away_odds"] = (
                out["run_line"]["run_line_away_odds"],
                out["run_line"]["run_line_home_odds"],
            )

    # ── Totals ────────────────────────────────────────────────────────────────
    if totals_pred is not None:
        t_prob_adj = max(0.48, float(totals_pred["pick_prob"]) - conf_red)
        t_conf = "strong" if totals_pred["models_agree"] else "low"
        t_is_value = (
            totals_pred.get("value_bet") and
            t_conf == "strong" and
            t_prob_adj >= 0.52
        )
        t_kelly = 0.0
        if bankroll > 0 and t_is_value:
            _, t_kelly, _, _ = size_bet(
                t_prob_adj, totals_pred["pick_odds"], bankroll, s_bankroll,
                upset_score, t_conf, is_user_bet=True,
            )
            t_kelly = round(t_kelly, 2)
        out["totals"] = {
            "predicted_total":     totals_pred["predicted_total"],
            "raw_predicted_total": totals_pred.get("raw_predicted_total", totals_pred["predicted_total"]),
            "xgb_pred":            totals_pred["xgb_pred"],
            "lr_pred":             totals_pred["lr_pred"],
            "total_line":          totals_pred["total_line"],
            "direction":           totals_pred["direction"],
            "models_agree":        totals_pred["models_agree"],
            "conflict":            totals_pred["conflict"],
            "confidence_tier":     t_conf,
            "pick_odds":           totals_pred["pick_odds"],
            "pick_prob":           t_prob_adj,
            "edge":                round(float(totals_pred["edge"]) - conf_red, 4),
            "value_bet":           t_is_value,
            "confidence":          round(t_prob_adj, 4),
            "over_odds":           totals_pred.get("over_odds"),
            "under_odds":          totals_pred.get("under_odds"),
            "park_run_factor":     totals_pred.get("park_run_factor", 1.0),
            "bet_dollars":         t_kelly,
            "top_reasons":         totals_pred.get("top_reasons", []),
        }

    _apply_correlation_rules(out)
    return out

# moved from app.py:1537
# ── WNBA serialization ───────────────────────────────────────────────────────

def _serialize_wnba(r: dict, bankroll: float, starting_bankroll: float | None = None) -> dict:
    """Convert a raw WNBA analysis result to a JSON-safe dict for the
    frontend.  Mirrors _serialize's flat-passthrough guard so the
    cached-analyze branch at ~line 7285 doesn't KeyError when
    _wnba_analysis_state["results"] was hydrated as flat rows."""
    if not isinstance(r.get("game"), dict):
        if r.get("home_team") and r.get("away_team"):
            return dict(r)
    game        = r["game"]
    pred        = r["prediction"]
    spread_pred = r.get("spread_pred")
    totals_pred = r.get("totals_pred")

    home_prob   = float(pred["home_win_prob"])
    xgb_prob    = float(pred.get("xgb_prob", home_prob))
    lr_prob     = float(pred.get("lr_prob",  home_prob))
    agree       = bool(pred.get("models_agree", True))
    market_prob = float(game["home_implied_prob"])
    home_edge   = home_prob - market_prob
    s_bankroll  = starting_bankroll if starting_bankroll is not None else bankroll

    if home_prob >= 0.5:
        pick_side  = "home";  pick_team = game["home_team"]
        pick_odds  = int(game.get("h2h_home_odds") or -110)
        pick_prob  = home_prob;  pick_edge = home_edge
    else:
        pick_side  = "away";  pick_team = game["away_team"]
        pick_odds  = int(game.get("h2h_away_odds") or -110)
        pick_prob  = 1.0 - home_prob;  pick_edge = -home_edge

    pick_prob_adj = max(0.48, pick_prob)
    pick_edge_adj = pick_prob_adj - (market_prob if pick_side == "home" else 1.0 - market_prob)
    # Tier from pick_prob only — independent of edge or model-agreement
    ml_conf       = confidence_tier_from_prob(pick_prob_adj)
    is_value      = (
        ml_conf in ("strong", "moderate") and
        pick_edge_adj >= 0.05 and pick_odds > -300 and pick_prob_adj >= 0.52
    )

    bet_dollars = bet_units = 0.0
    if bankroll > 0 and is_value:
        _, bet_dollars, bet_units, _ = size_bet(
            pick_prob_adj, pick_odds, bankroll, s_bankroll, 0.0, ml_conf, is_user_bet=True
        )

    out: dict = {
        "game_id":           game["id"],
        "home_team":         game["home_team"],
        "away_team":         game["away_team"],
        "commence_time":     game.get("commence_time", ""),
        "home_odds":         int(game.get("h2h_home_odds") or -110),
        "away_odds":         int(game.get("h2h_away_odds") or -110),
        "spread":            game.get("spread"),
        "home_implied_prob": market_prob,
        "home_win_prob":     home_prob,
        "xgb_prob":          xgb_prob,
        "lr_prob":           lr_prob,
        "nn_prob":           None,
        "models_agree":      agree,
        "conflict":          not agree,
        "pick_side":         pick_side,
        "pick_team":         pick_team,
        "pick_odds":         pick_odds,
        "pick_prob":         round(pick_prob_adj, 4),
        "pick_edge":         round(pick_edge_adj, 4),
        "confidence_tier":   ml_conf,
        "bet_dollars":       round(bet_dollars, 2),
        "bet_units":         round(bet_units, 1),
        "value_pick":        is_value,
        "upset_factor":      {},
        "sport":             "wnba",
    }

    # Include player/team meta
    meta = r.get("meta") or {}
    hp = meta.get("home_player") or {}
    ap = meta.get("away_player") or {}
    if hp.get("name"):
        out["home_player"] = {"name": hp["name"], "pts_pg": hp.get("pts_pg", 15.0)}
    if ap.get("name"):
        out["away_player"] = {"name": ap["name"], "pts_pg": ap.get("pts_pg", 15.0)}

    h2h = meta.get("h2h") or {}
    if h2h:
        out["h2h"] = h2h

    if meta.get("home_b2b"):
        out["home_b2b"] = True
    if meta.get("away_b2b"):
        out["away_b2b"] = True

    # Spread prediction
    if spread_pred is not None:
        sp_prob_adj = max(0.48, float(spread_pred["pick_prob"]))
        # Tier from pick_prob only — independent of edge or model-agreement
        sp_conf     = confidence_tier_from_prob(sp_prob_adj)
        sp_is_value = (
            spread_pred.get("value_bet") and sp_conf in ("strong",) and sp_prob_adj >= 0.52
        )
        sp_kelly = 0.0
        if bankroll > 0 and sp_is_value:
            _, sp_kelly, _, _ = size_bet(
                sp_prob_adj, spread_pred["pick_odds"], bankroll, s_bankroll,
                0.0, sp_conf, is_user_bet=True,
            )
        sp_mkt_prob = spread_pred.get("market_prob", 0.5)
        sp_edge_adj = sp_prob_adj - sp_mkt_prob
        out["spread_pick"] = {
            "predicted_margin":  spread_pred["predicted_margin"],
            "xgb_pred":          spread_pred["xgb_pred"],
            "lr_pred":           spread_pred["lr_pred"],
            "models_agree":      spread_pred["models_agree"],
            "conflict":          spread_pred["conflict"],
            "confidence_tier":   sp_conf,
            "side":              spread_pred["side"],
            "pick_team":         spread_pred["pick_team"],
            "pick_odds":         spread_pred["pick_odds"],
            "pick_prob":         round(sp_prob_adj, 4),
            "edge":              round(sp_edge_adj, 4),
            "value_bet":         sp_is_value,
            "confidence":        round(sp_prob_adj, 4),
            "spread_line":       spread_pred["spread_line"],
            "spread_home_odds":  spread_pred["spread_home_odds"],
            "spread_away_odds":  spread_pred["spread_away_odds"],
            "bet_dollars":       round(sp_kelly, 2),
        }

    # Totals prediction
    if totals_pred is not None:
        t_prob_adj  = max(0.48, float(totals_pred["pick_prob"]))
        t_conf      = "strong" if totals_pred.get("models_agree") else "low"
        t_is_value  = (
            totals_pred.get("value_bet") and t_conf == "strong" and t_prob_adj >= 0.52
        )
        t_kelly = 0.0
        if bankroll > 0 and t_is_value:
            _, t_kelly, _, _ = size_bet(
                t_prob_adj, totals_pred["pick_odds"], bankroll, s_bankroll,
                0.0, t_conf, is_user_bet=True,
            )
        t_edge_adj = t_prob_adj - totals_pred.get("market_prob", 0.5)
        out["totals"] = {
            "predicted_total":  totals_pred["predicted_total"],
            "xgb_pred":         totals_pred["xgb_pred"],
            "lr_pred":          totals_pred["lr_pred"],
            "total_line":       totals_pred["total_line"],
            "direction":        totals_pred["direction"],
            "models_agree":     totals_pred["models_agree"],
            "conflict":         totals_pred["conflict"],
            "confidence_tier":  t_conf,
            "pick_odds":        totals_pred["pick_odds"],
            "pick_prob":        round(t_prob_adj, 4),
            "edge":             round(t_edge_adj, 4),
            "value_bet":        t_is_value,
            "confidence":       round(t_prob_adj, 4),
            "over_odds":        totals_pred.get("over_odds"),
            "under_odds":       totals_pred.get("under_odds"),
            "bet_dollars":      round(t_kelly, 2),
        }

    return out

# moved from app.py:1765
def _apply_correlation_rules(out: dict) -> None:
    """
    Enforce logical consistency across ML / run-line / totals picks for one game.
    Mutates out in-place.  Adds:
      out["correlation_status"] — "correlated" | "adjusted" | "conflict"
      out["correlation_flags"]  — list of rule codes that fired
    """
    flags:    list[str] = []
    adjusted: bool      = False
    conflict: bool      = False

    ml_side   = out.get("pick_side")           # "home" | "away"
    ml_prob   = float(out.get("pick_prob", 0.5))
    ml_odds   = int(out.get("pick_odds", -110))
    rl        = out.get("run_line")
    totals    = out.get("totals")
    home_team = out.get("home_team", "")
    away_team = out.get("away_team", "")

    # ── Rule 1 — ML and RL must favor the same team ───────────────────────────
    # ML pick_side ("home"/"away") must match RL side ("home"/"away").
    # Whichever prediction has the lower confidence gets flipped to agree with
    # the stronger one.  If the corrected probability is still < 0.52 the
    # models genuinely contradict — mark as conflict.
    if rl and ml_side:
        rl_side = rl.get("side", "")
        if rl_side and rl_side != ml_side:
            rl_prob      = float(rl.get("pick_prob", 0.5))
            home_cov_raw = float(rl.get("home_cover_prob", 0.5))
            rl_home_odds = int(rl.get("run_line_home_odds") or -150)
            rl_away_odds = int(rl.get("run_line_away_odds") or 130)

            if ml_prob >= rl_prob:
                # ML wins — flip the RL pick to match ML's team
                if ml_side == "home":
                    new_p = max(0.50, home_cov_raw)
                    new_o = rl_home_odds
                    rl["side"]      = "home"
                    rl["pick_team"] = home_team
                else:
                    new_p = max(0.50, 1.0 - home_cov_raw)
                    new_o = rl_away_odds
                    rl["side"]      = "away"
                    rl["pick_team"] = away_team
                new_edge         = round(new_p - _correlation_impl_prob(new_o), 4)
                rl["pick_prob"]  = round(new_p, 4)
                rl["pick_odds"]  = new_o
                rl["edge"]       = new_edge
                rl["value_bet"]  = new_edge >= 0.05
                rl["confidence"] = round(new_p, 4)
                flags.append("rule1_rl_flipped")
                if new_p < 0.52:
                    conflict = True
            else:
                # RL wins — flip the ML pick to match RL's team
                if rl_side == "home":
                    new_side = "home";  new_team = home_team
                    new_p    = max(0.50, float(out.get("home_win_prob", 0.5)))
                    new_o    = int(out.get("home_odds") or -110)
                else:
                    new_side = "away";  new_team = away_team
                    new_p    = max(0.50, 1.0 - float(out.get("home_win_prob", 0.5)))
                    new_o    = int(out.get("away_odds") or -110)
                new_edge            = round(new_p - _correlation_impl_prob(new_o), 4)
                out["pick_side"]    = new_side
                out["pick_team"]    = new_team
                out["pick_prob"]    = round(new_p, 4)
                out["pick_odds"]    = new_o
                out["pick_edge"]    = new_edge
                out["value_pick"]   = new_edge >= 0.05 and new_o > -300
                flags.append("rule1_ml_flipped")
                if new_p < 0.52:
                    conflict = True

            adjusted = True

    # ── Rule 2 — Heavy ML favorite paired with an over: reduce over confidence ─
    # Dominant teams tend to win lower-scoring games; an over in this context
    # is directionally inconsistent.  Reduce totals pick_prob by 10 pp.
    if totals and ml_odds <= -150 and totals.get("direction") == "over":
        old_p = float(totals.get("pick_prob", 0.5))
        new_p = max(0.50, old_p - 0.10)
        totals["pick_prob"]  = round(new_p, 4)
        totals["confidence"] = round(new_p, 4)
        totals["edge"]       = round(float(totals.get("edge", 0.0)) - 0.10, 4)
        totals["value_bet"]  = float(totals["edge"]) >= 0.05
        flags.append("rule2_favorite_over")
        adjusted = True

    # ── Rule 3 — -1.5 run-line favorite + over on a tight total (< 8) ─────────
    # A -1.5 pick implies a multi-run win; a low total + over is directionally
    # inconsistent.  Reduce totals pick_prob by 10 pp (cumulative with Rule 2).
    if rl and totals:
        rl_pt_raw  = rl.get("run_line_point")
        rl_side_r3 = rl.get("side", "")
        total_line = totals.get("total_line")
        if rl_pt_raw is not None and total_line is not None:
            rl_pt_f = float(rl_pt_raw)
            # Picked team is the -1.5 favorite when rl is on home and pt < 0,
            # or rl is on away and pt > 0 (away is the -1.5 fav).
            is_minus15_pick = (
                (rl_side_r3 == "home" and rl_pt_f < 0) or
                (rl_side_r3 == "away" and rl_pt_f > 0)
            )
            if (
                is_minus15_pick
                and totals.get("direction") == "over"
                and float(total_line) < 8.0
            ):
                old_p = float(totals.get("pick_prob", 0.5))
                new_p = max(0.50, old_p - 0.10)
                totals["pick_prob"]  = round(new_p, 4)
                totals["confidence"] = round(new_p, 4)
                totals["edge"]       = round(float(totals.get("edge", 0.0)) - 0.10, 4)
                totals["value_bet"]  = float(totals["edge"]) >= 0.05
                flags.append("rule3_rl_tight_over")
                adjusted = True

    # ── Status ────────────────────────────────────────────────────────────────
    if conflict:
        status = "conflict"
    elif adjusted:
        status = "adjusted"
    else:
        status = "correlated"

    out["correlation_status"] = status
    out["correlation_flags"]  = flags

# moved from app.py:1895
def _format_rl_shap(shap_data: dict) -> dict:
    """Format run line SHAP data for frontend (top 6 features)."""
    if not shap_data:
        return {}
    return {
        "base_value": float(shap_data["base_value"]),
        "source":     shap_data.get("source", ""),
        "values": [
            {
                "feature":       v["feature"],
                "label":         _FEATURE_LABELS.get(v["feature"], v["feature"]),
                "shap_value":    float(v["shap_value"]),
                "feature_value": float(v["feature_value"]),
            }
            for v in shap_data["shap_values"][:6]
        ],
    }
