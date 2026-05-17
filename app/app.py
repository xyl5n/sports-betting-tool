"""
Flask backend for the Sports Betting Analysis desktop app.
All existing src/ modules are reused unchanged — only the display layer changes
from Rich terminal output to JSON served to the PyWebView browser frontend.
"""
import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))

from src.cache import Cache
from src.daily_picks import select_daily_picks, load_daily_picks
from src.explainer import PredictionExplainer
from src.game_store import GameStore
from src.kelly import size_bet, american_to_decimal, confidence_tier
from src.ledger import Ledger
from src.model import BettingModel
from src.odds_client import OddsClient
from src.run_line_model import RunLineModel
from src.sports_config import SPORTS
from src.totals_model import TotalsModel
from src.upset import UpsetCalculator
from src.wnba_stats_client import WNBAStatsClient
from src.wnba_features import WNBAFeatureBuilder
from src.wnba_spread_model import WNBASpreadModel
from src.wnba_totals_model import WNBATotalsModel
from src.wnba_college_client import WNBACollegeClient
import anthropic as _anthropic

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# ── Global state (single-user desktop app) ────────────────────────────────────
_cache = Cache()
_ANALYSIS_TTL        = 900  # 15 minutes — skip API if last run was within this window
_ANALYSIS_CACHE_FILE      = Path("data/analysis_cache.json")
_WNBA_ANALYSIS_CACHE_FILE = Path("data/wnba_analysis_cache.json")
_PRE_GAME_ODDS_FILE       = Path("data/pre_game_odds.json")
_EXPLAIN_CACHE_FILE       = Path("data/explain_cache.json")
_AI_BREAKDOWN_CACHE_FILE  = Path("data/ai_breakdown_cache.json")
_ARCHIVE_PATH             = Path("data/bet_history_archive.json")

_ANALYST_SYSTEM_PROMPT = (
    "You are a professional sports analyst with 20 years of experience in MLB and WNBA "
    "betting markets. You have deep expertise in sabermetrics, advanced baseball statistics, "
    "basketball analytics, lineup construction, pitcher matchup analysis, and betting market "
    "inefficiencies. You form your own independent opinions based on the data presented to "
    "you and are not afraid to disagree with model predictions when your analysis suggests a "
    "different outcome. When you disagree with the model you clearly state your own pick and "
    "explain why you see the game differently. Your analysis is direct, confident, and "
    "specific and you never give vague or non-committal answers. You always consider factors "
    "like recent form, situational context, matchup history, and market line movement in "
    "addition to the statistical data provided. After giving your analysis always end with a "
    "clear recommendation of either: 'Agree with model', 'Disagree — my pick is X', or "
    "'Lean with caution' if you partially agree but see significant risk."
)
_upset_calc          = UpsetCalculator(cache=_cache)


# ── Anthropic helper ──────────────────────────────────────────────────────────

def _call_analyst(prompt: str, max_tokens: int = 600) -> str:
    """Call the Anthropic analyst model and return the raw response text.
    Raises ValueError if API key is missing; re-raises Anthropic errors as-is."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")
    client = _anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=_ANALYST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from a Claude response."""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _format_odds(odds_value) -> str:
    """Format an American odds value as a signed string like '+140' or '-200'."""
    if isinstance(odds_value, (int, float)):
        return f"{int(odds_value):+d}"
    return str(odds_value or "n/a")


def _load_archive_bets() -> list[dict]:
    """Load all settled bets from the permanent archive file."""
    if not _ARCHIVE_PATH.exists():
        return []
    try:
        raw = json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8"))
        return raw.get("bets", []) if isinstance(raw, dict) else raw
    except Exception:
        return []

_analysis_state: dict = {
    "sport":              None,
    "bankroll":           250.0,
    "results":            [],   # raw result dicts (game, prediction, shap, meta)
    "parlays":            {},
    "last_analyzed_at":   None, # datetime (UTC) of last full run
    "last_analysis_meta": {},   # games_loaded, cv/lr/nn accuracy, model_status
}

_wnba_analysis_state: dict = {
    "sport":              "wnba",
    "bankroll":           1000.0,
    "results":            [],
    "parlays":            {},
    "last_analyzed_at":   None,
    "last_analysis_meta": {},
}

_FEATURE_LABELS = {
    # NFL
    "net_scoring_diff":     "Net scoring margin",
    "ppg_diff":             "Points per game",
    "papg_diff":            "Points allowed/gm",
    "win_pct_diff":         "Win percentage",
    "home_away_split_diff": "Home/Away split",
    "last5_diff":           "Last-5 form",
    "home_implied_prob":    "Market win prob",
    "spread":               "Point spread",
    # MLB — team stats
    "net_run_diff":         "Net run margin",
    "rpg_diff":             "Runs per game",
    "rapg_diff":            "Runs allowed/gm",
    "last10_diff":          "Last-10 form",
    "hits_diff":            "Hits per game",
    "errors_diff":          "Errors (fielding)",
    "run_line":             "Run line",
    # MLB — starting pitcher
    "sp_era_diff":          "SP ERA advantage",
    "sp_whip_diff":         "SP WHIP advantage",
    "sp_k_rate_diff":       "SP strikeout rate",
    "home_sp_rest":         "Home SP rest days",
    "away_sp_rest":         "Away SP rest days",
    "sp_hand_adv":          "Pitcher handedness",
    # MLB — ballpark & weather
    "park_run_factor":      "Ballpark run factor",
    "wind_speed":           "Wind speed (mph)",
    "wind_direction":       "Wind direction (°)",
    # MLB — bullpen
    "bullpen_era_diff":     "Bullpen ERA advantage",
    "bullpen_fatigue_diff": "Bullpen fatigue edge",
    # MLB — lineup
    "lineup_confirmed":     "Lineup confirmed",
    # MLB — market
    "line_movement":        "Line movement",
    # Totals model features
    "combined_rpg":         "Combined runs/game",
    "combined_rapg":        "Combined runs allowed/gm",
    "combined_sp_era":      "Combined SP ERA",
    "home_sp_k_rate":       "Home SP K rate",
    "away_sp_k_rate":       "Away SP K rate",
    "combined_bullpen_era": "Combined bullpen ERA",
    "temperature":          "Temperature (°F)",
}


# ── Pre-game odds lock ────────────────────────────────────────────────────────
# Odds fields that get snapshotted before first pitch and restored for in-progress games.
_ODDS_FIELDS = (
    "h2h_home_odds", "h2h_away_odds",
    "home_implied_prob", "away_implied_prob",
    "run_line_home_odds", "run_line_away_odds", "run_line_point", "spread",
    "over_odds", "under_odds", "total_line",
)

def _load_pre_game_odds() -> dict:
    try:
        if _PRE_GAME_ODDS_FILE.exists():
            raw = json.loads(_PRE_GAME_ODDS_FILE.read_text(encoding="utf-8"))
            # Drop entries older than 3 days to keep the file small
            cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
            return {
                gid: snap for gid, snap in raw.items()
                if snap.get("commence_time", "")[:10] >= cutoff
            }
    except Exception:
        pass
    return {}

def _save_pre_game_odds(store: dict) -> None:
    try:
        Path("data").mkdir(exist_ok=True)
        _PRE_GAME_ODDS_FILE.write_text(json.dumps(store, default=str), encoding="utf-8")
    except Exception:
        pass

def _lock_in_pre_game_odds(games: list) -> list:
    """
    Snapshot market odds for every upcoming game.
    For games already in progress, substitute the stored pre-game odds so the
    model always evaluates the opening market — not live in-play prices.
    """
    now_utc = datetime.now(timezone.utc)
    store   = _load_pre_game_odds()
    updated = False
    result  = []

    for game in games:
        gid = game.get("id", "")
        try:
            ct = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        except Exception:
            result.append(game)
            continue

        if ct > now_utc:
            # Pre-game: refresh the snapshot with the latest market odds
            snap = {f: game.get(f) for f in _ODDS_FIELDS}
            snap["commence_time"] = game["commence_time"]
            store[gid] = snap
            updated = True
            result.append(game)
        else:
            # In-progress: restore pre-game odds over whatever the API sent
            if gid in store:
                snap = store[gid]
                game = {**game, **{f: snap[f] for f in _ODDS_FIELDS if f in snap}}
            result.append(game)

    if updated:
        _save_pre_game_odds(store)

    return result


# ── Serialization helpers ─────────────────────────────────────────────────────

def _py(obj):
    """Recursively convert numpy scalars / arrays to plain Python types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _py(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_py(v) for v in obj]
    return obj


def _serialize(r: dict, bankroll: float, sport: str = "mlb", starting_bankroll: float | None = None) -> dict:
    """Convert a raw analysis result to a JSON-safe dict for the frontend."""
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

    # ML confidence tier (drives label colour and sizing; "split" allows half-Kelly bet)
    ml_conf = confidence_tier(xgb_prob, lr_prob, nn_prob)

    # Adjust displayed probability (floor at 0.48)
    pick_prob_adj = max(0.48, pick_prob - conf_red)
    pick_edge_adj = pick_prob_adj - (market_prob if pick_side == "home" else 1.0 - market_prob)

    # Tier drives the value gate; adj prob < 0.52 always blocks the bet
    is_value = (
        ml_conf in ("strong", "moderate", "split") and
        pick_edge_adj >= 0.05 and
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

    # Starting pitcher details
    home_sp = meta.get("home_sp") or {}
    away_sp = meta.get("away_sp") or {}
    if home_sp:
        out["home_sp"] = {
            "era":    round(float(home_sp.get("era", 4.5)), 2),
            "whip":   round(float(home_sp.get("whip", 1.3)), 2),
            "k_rate": round(float(home_sp.get("k_rate", 0.215)) * 100, 1),
            "hand":   "LHP" if home_sp.get("hand") == 1 else "RHP",
            "rest":   int(home_sp.get("rest", 4)),
        }
    if away_sp:
        out["away_sp"] = {
            "era":    round(float(away_sp.get("era", 4.5)), 2),
            "whip":   round(float(away_sp.get("whip", 1.3)), 2),
            "k_rate": round(float(away_sp.get("k_rate", 0.215)) * 100, 1),
            "hand":   "LHP" if away_sp.get("hand") == 1 else "RHP",
            "rest":   int(away_sp.get("rest", 4)),
        }

    # Ballpark & weather
    park_run = meta.get("park_run_factor")
    if park_run is not None:
        out["park_run_factor"] = round(float(park_run), 3)
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
        rl_conf = confidence_tier(
            float(rl_pred.get("xgb_prob", 0.5)),
            float(rl_pred.get("lr_prob",  0.5)),
            float(rl_pred["nn_prob"]) if rl_pred.get("nn_prob") is not None else None,
        )
        rl_kelly = 0.0
        rl_is_value = (
            rl_pred.get("value_bet") and
            rl_conf in ("strong", "moderate", "split") and
            rl_prob_adj >= 0.52
        )
        if bankroll > 0 and rl_is_value:
            _, rl_kelly, _, _ = size_bet(
                rl_prob_adj, rl_pred["pick_odds"], bankroll, s_bankroll,
                upset_score, rl_conf, is_user_bet=True,
            )
            rl_kelly = round(rl_kelly, 2)
        rl_shap = rl_pred.get("shap")
        # Adjusted edge for run line
        rl_mkt_side = "home" if rl_pred["side"] == "home" else "away"
        rl_mkt_prob = (
            game.get("home_implied_prob", 0.5)
            if rl_mkt_side == "home"
            else 1.0 - game.get("home_implied_prob", 0.5)
        )
        rl_edge_adj = rl_prob_adj - rl_mkt_prob
        out["run_line"] = {
            "home_cover_prob":    round(rl_pred["home_cover_prob"], 4),
            "xgb_prob":           round(rl_pred["xgb_prob"], 4),
            "lr_prob":            round(rl_pred["lr_prob"], 4),
            "models_agree":       rl_pred["models_agree"],
            "conflict":           rl_pred["conflict"],
            "confidence_tier":    rl_conf,
            "side":               rl_pred["side"],
            "pick_team":          rl_pred["pick_team"],
            "pick_odds":          rl_pred["pick_odds"],
            "pick_prob":          rl_prob_adj,
            "edge":               round(rl_edge_adj, 4),
            "value_bet":          rl_is_value,
            "confidence":         round(rl_prob_adj, 4),
            "run_line_point":     rl_pred["run_line_point"],
            "run_line_home_odds": rl_pred["run_line_home_odds"],
            "run_line_away_odds": rl_pred["run_line_away_odds"],
            "bet_dollars":        rl_kelly,
            "shap": _format_rl_shap(rl_shap) if rl_shap else None,
        }
        # Validate: ML favorite must always carry -1.5; correct if API data is flipped
        _hml = out["home_odds"]
        _aml = out["away_odds"]
        _expected_pt = -1.5 if _hml < _aml else 1.5
        _actual_pt   = out["run_line"].get("run_line_point")
        if _actual_pt is not None and abs(float(_actual_pt) - _expected_pt) > 0.01:
            print(
                f"[RL Validation] {out['home_team']} vs {out['away_team']}: "
                f"run_line_point={_actual_pt} but home_ml={_hml} vs away_ml={_aml}, "
                f"expected {_expected_pt}. Auto-correcting."
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


# ── WNBA serialization ───────────────────────────────────────────────────────

def _serialize_wnba(r: dict, bankroll: float, starting_bankroll: float | None = None) -> dict:
    """Convert a raw WNBA analysis result to a JSON-safe dict for the frontend."""
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

    ml_conf       = confidence_tier(xgb_prob, lr_prob, None)
    pick_prob_adj = max(0.48, pick_prob)
    pick_edge_adj = pick_prob_adj - (market_prob if pick_side == "home" else 1.0 - market_prob)
    is_value      = (
        ml_conf in ("strong", "moderate", "split") and
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
        sp_conf     = confidence_tier(
            float(spread_pred.get("xgb_pred", 0) > 0),
            float(spread_pred.get("lr_pred",  0) > 0),
            None,
        )
        sp_conf = "strong" if spread_pred.get("models_agree") else "low"
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


def _save_wnba_analysis_cache(serialized, parlays, games_loaded, cv_acc, lr_cv_acc):
    try:
        Path("data").mkdir(exist_ok=True)
        payload = {
            "date":          datetime.now(timezone.utc).date().isoformat(),
            "analyzed_at":   datetime.now(timezone.utc).isoformat(),
            "sport":         "wnba",
            "games_loaded":  games_loaded,
            "cv_accuracy":   cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
            "results":       serialized,
            "parlays":       parlays,
        }
        _WNBA_ANALYSIS_CACHE_FILE.write_text(json.dumps(payload, default=str), encoding="utf-8")
    except Exception:
        pass


# ── Correlation validation ────────────────────────────────────────────────────

def _correlation_impl_prob(odds: int) -> float:
    """American odds → raw implied probability (no vig removal)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


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


# ── Parlay generation ─────────────────────────────────────────────────────────
# Note: american_to_decimal() is imported from src.kelly — no local duplicate needed.

def _compute_parlay(legs: list, name: str, desc: str, emoji: str,
                    accent: str, bankroll: float) -> dict:
    """Build one parlay dict from a list of serialized game results."""
    if len(legs) < 2:
        return {"available": False, "name": name, "description": desc,
                "emoji": emoji, "accent": accent}

    combined_prob  = 1.0
    parlay_decimal = 1.0
    for g in legs:
        combined_prob  *= g["pick_prob"]
        parlay_decimal *= american_to_decimal(g["pick_odds"])

    if parlay_decimal >= 2.0:
        parlay_american = int((parlay_decimal - 1) * 100)
    else:
        parlay_american = int(-100 / (parlay_decimal - 1))

    # Positive-edge gate: model combined prob must beat implied parlay odds
    implied_prob = 1.0 / parlay_decimal if parlay_decimal > 0 else 1.0
    edge = combined_prob - implied_prob

    bet_dollars = bet_units = 0.0
    if bankroll > 0 and edge > 0:
        n = len(legs)
        # Reduction multipliers by leg count
        reduction = {2: 0.25, 3: 0.15, 4: 0.10, 5: 0.05}.get(n, 0.05)
        # Hard caps by leg count (fraction of bankroll)
        cap = {2: 0.02, 3: 0.015, 4: 0.01, 5: 0.005}.get(n, 0.005)

        b = parlay_decimal - 1.0
        p = combined_prob
        q = 1.0 - p
        full_kelly = (b * p - q) / b if b > 0 else 0.0

        if full_kelly > 0:
            fraction = full_kelly * reduction
            fraction = min(fraction, cap)
            raw_dollars = fraction * bankroll
            bet_dollars = round(raw_dollars, 2)
            starting = bankroll  # units relative to current bankroll
            unit_size = starting * 0.01
            bet_units = round(bet_dollars / unit_size, 1) if unit_size > 0 else 0.0

    return {
        "available":       True,
        "name":            name,
        "description":     desc,
        "emoji":           emoji,
        "accent":          accent,
        "legs":            legs,
        "combined_prob":   combined_prob,
        "parlay_decimal":  parlay_decimal,
        "parlay_american": parlay_american,
        "fair_mult":       1.0 / combined_prob if combined_prob > 0 else 0,
        "edge_pct":        round(edge * 100, 2),
        "bet_dollars":     bet_dollars,
        "bet_units":       bet_units,
        "n_legs":          len(legs),
    }


def _expand_game_legs(g: dict) -> list:
    """Expand one serialized game into individual ML / RL / totals leg dicts."""
    legs    = []
    game_id = g.get("game_id", "")
    home    = g.get("home_team", "")
    away    = g.get("away_team", "")
    ct      = g.get("commence_time", "")

    # Moneyline
    if g.get("pick_prob") is not None and g.get("pick_odds") is not None:
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "ml",
            "pick_team":     g["pick_team"],
            "pick_side":     g.get("pick_side", ""),
            "pick_odds":     g["pick_odds"],
            "pick_prob":     g["pick_prob"],
            "pick_edge":     g.get("pick_edge", 0),
            "value_pick":    g.get("value_pick", False),
            "prop_line":     None,
        })

    # Run line
    rl = g.get("run_line")
    if rl and not rl.get("conflict"):
        pt   = float(rl.get("run_line_point") or -1.5)
        side = rl.get("side", "home")
        # pick_line is the signed handicap for the chosen team (+1.5 or -1.5)
        pick_line = pt if side == "home" else -pt
        # prop_line for settlement: -run_line_point gives correct threshold for both sides
        prop_line_val = -pt
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "rl",
            "pick_team":     rl.get("pick_team", ""),
            "pick_line":     pick_line,
            "pick_side":     side,
            "pick_odds":     rl["pick_odds"],
            "pick_prob":     rl["pick_prob"],
            "pick_edge":     rl.get("edge", 0),
            "value_pick":    rl.get("value_bet", False),
            "prop_line":     prop_line_val,
        })

    # Totals
    t = g.get("totals")
    if t and not t.get("conflict"):
        direction = t.get("direction", "over")
        line      = t.get("total_line")
        label     = "Over" if direction == "over" else "Under"
        legs.append({
            "game_id":       game_id,
            "home_team":     home,
            "away_team":     away,
            "commence_time": ct,
            "bet_type":      "totals",
            "pick_team":     f"{label} {line}",
            "pick_side":     direction,
            "pick_odds":     t["pick_odds"],
            "pick_prob":     t["pick_prob"],
            "pick_edge":     t.get("edge", 0),
            "value_pick":    t.get("value_bet", False),
            "prop_line":     float(line) if line is not None else None,
        })

    return legs


def _unique_legs(pool: list, n: int) -> list:
    """Select up to n legs ensuring no two legs come from the same game."""
    legs: list       = []
    used_games: set  = set()
    for g in pool:
        gid = g.get("game_id", "")
        if gid and gid in used_games:
            continue
        legs.append(g)
        if gid:
            used_games.add(gid)
        if len(legs) >= n:
            break
    return legs


def _generate_parlays(serialized: list, bankroll: float) -> dict:
    """
    Produce four parlay recommendations from ML, run-line, and totals picks.
    Only upcoming games are eligible; each game contributes at most one leg.
    """
    now_utc = datetime.now(timezone.utc)

    # Deduplicate games by game_id, keep only upcoming
    seen_ids: set = set()
    all_legs: list = []
    for g in serialized:
        gid = g.get("game_id", "")
        if gid in seen_ids:
            continue
        seen_ids.add(gid)
        try:
            ct = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if ct > now_utc:
                all_legs.extend(_expand_game_legs(g))
        except Exception:
            pass

    value   = [l for l in all_legs if l["value_pick"]]
    any_pos = [l for l in all_legs if l["pick_edge"] > 0]
    dogs    = [l for l in all_legs if l["pick_odds"] >= -150 and l["pick_edge"] > 0]

    # ── Safe: 2 highest-confidence value picks ────────────────────────────────
    safe_pool = sorted(value, key=lambda l: l["pick_prob"], reverse=True)
    safe_legs = _unique_legs(safe_pool, 2)

    # ── Value: top 3 by edge ──────────────────────────────────────────────────
    val_pool = sorted(value, key=lambda l: l["pick_edge"], reverse=True)
    val_legs = _unique_legs(val_pool, 3)

    # ── High Risk / High Reward: 3-4 underdog-leaning picks ──────────────────
    hr_base = dogs if len(dogs) >= 3 else sorted(any_pos,
                  key=lambda l: l["pick_odds"], reverse=True)
    hr_pool  = sorted(hr_base, key=lambda l: l["pick_edge"], reverse=True)
    hr_n     = 4 if len(dogs) >= 4 else 3
    hr_legs  = _unique_legs(hr_pool, hr_n)

    # ── Lottery: 5 picks, balanced edge + upside ─────────────────────────────
    lot_pool = [l for l in all_legs if l["pick_edge"] > -0.08]
    if len(lot_pool) < 5:
        lot_pool = all_legs[:]
    def _lot_score(l):
        upside = 0.4 if l["pick_odds"] > 0 else (0.2 if l["pick_odds"] >= -130 else 0.0)
        return l["pick_edge"] * 0.6 + upside
    lot_sorted = sorted(lot_pool, key=_lot_score, reverse=True)
    lot_legs   = _unique_legs(lot_sorted, 5)

    return {
        "safe": _compute_parlay(
            safe_legs, "Safe Play", "2 highest-confidence value picks",
            "🛡️", "blue", bankroll,
        ),
        "value": _compute_parlay(
            val_legs, "Value Parlay", "Top 3 picks by edge",
            "💎", "green", bankroll,
        ),
        "high_risk": _compute_parlay(
            hr_legs, "High Risk / High Reward", "Underdog-leaning picks with model edge",
            "🔥", "orange", bankroll,
        ),
        "lottery": _compute_parlay(
            lot_legs, "Lottery Ticket", "5 picks · tiny stake · max upside",
            "🎰", "purple", bankroll,
        ),
    }


# ── Analysis disk cache ───────────────────────────────────────────────────────

def _save_analysis_cache(serialized: list, parlays: dict, sport: str,
                         games_loaded: int, cv_acc, lr_cv_acc, nn_val_acc) -> None:
    """Persist today's serialized analysis to disk for cross-session auto-load."""
    try:
        Path("data").mkdir(exist_ok=True)
        payload = {
            "date":            datetime.now(timezone.utc).date().isoformat(),
            "analyzed_at":     datetime.now(timezone.utc).isoformat(),
            "sport":           sport,
            "games_loaded":    games_loaded,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
            "results":         serialized,
            "parlays":         parlays,
        }
        _ANALYSIS_CACHE_FILE.write_text(
            json.dumps(payload, default=str), encoding="utf-8"
        )
    except Exception:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── MLB Stats API proxy ────────────────────────────────────────────────────────
# Fetches statsapi.mlb.com server-side so the browser never makes a cross-origin
# request.  QWebEngineView's CORS policy can silently block direct external fetches
# from an HTTP localhost origin; routing through Flask eliminates that entirely.
#
# Routes:
#   /api/mlb/schedule?date=YYYY-MM-DD              → schedule (1-hour cache)
#   /api/mlb/schedule?date=YYYY-MM-DD&hydrate=linescore → live scores (30-sec cache)

_MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"
# In-memory short-TTL cache for linescore data (avoids disk I/O on 60-s polling)
_linescore_mem: dict[str, tuple[float, dict]] = {}   # key → (timestamp, data)
_LINESCORE_TTL = 30   # seconds — live scores refresh this often


@app.route("/api/mlb/schedule", methods=["GET"])
def mlb_schedule_proxy():
    """
    Server-side proxy for the MLB Stats API schedule endpoint.
    Accepts the same query params the JavaScript previously sent directly:
      date=YYYY-MM-DD  (required)
      hydrate=linescore  (optional — triggers live-score TTL of 30 s)
    """
    import time as _time
    import urllib.request as _urlreq
    import urllib.error  as _urlerr

    date_str = request.args.get("date", "").strip()
    hydrate  = request.args.get("hydrate", "").strip()

    if not date_str:
        return jsonify({"dates": [], "error": "date param required"}), 400

    is_linescore = hydrate == "linescore"
    cache_key    = f"mlb_schedule_{date_str}_{hydrate}"

    # ── Short-TTL in-memory cache for linescore (live games) ──────────────────
    if is_linescore:
        entry = _linescore_mem.get(cache_key)
        if entry and (_time.time() - entry[0]) < _LINESCORE_TTL:
            return jsonify(entry[1])
    else:
        # Use the file-based cache (1-hour TTL) for plain schedule requests
        cached = _cache.get(cache_key, ttl=3600)
        if cached is not None:
            return jsonify(cached)

    # ── Fetch from MLB Stats API ───────────────────────────────────────────────
    url = f"{_MLB_STATS_BASE}/schedule?sportId=1&date={date_str}"
    if hydrate:
        url += f"&hydrate={hydrate}"

    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except _urlerr.URLError as exc:
        print(f"  [mlb proxy] URLError fetching {url}: {exc}")
        return jsonify({"dates": []}), 200
    except Exception as exc:
        print(f"  [mlb proxy] Error fetching {url}: {exc}")
        return jsonify({"dates": []}), 200

    # ── Store in appropriate cache ─────────────────────────────────────────────
    if is_linescore:
        _linescore_mem[cache_key] = (_time.time(), data)
    else:
        try:
            _cache.set(cache_key, data)
        except Exception:
            pass  # cache write failure is non-fatal

    return jsonify(data)


# ── Live-score debug system ────────────────────────────────────────────────────
# Writes to stdout AND data/debug_live.log so output is readable whether
# the user runs via 'python desktop.pyw' (terminal) or via launch.bat (log file).

_DEBUG_LOG = Path("data/debug_live.log")

def _debug_print(msg: str) -> None:
    """Print to stdout and append to log file with timestamp."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _today_et_str() -> str:
    """Return today's date in America/New_York as YYYY-MM-DD."""
    try:
        # zoneinfo is stdlib in Python 3.9+
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        # fallback: UTC offset -5 (close enough for date purposes)
        return (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d")


def _fetch_mlb_linescore_raw(date_str: str) -> dict:
    """
    Direct fetch from MLB Stats API (bypasses in-memory cache).
    Returns {gamePk: game_dict} for every game on date_str.
    """
    import urllib.request as _urlreq
    import urllib.error  as _urlerr
    url = (f"{_MLB_STATS_BASE}/schedule"
           f"?sportId=1&date={date_str}&hydrate=linescore")
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        live_map: dict = {}
        for d in data.get("dates", []):
            for g in d.get("games", []):
                live_map[int(g["gamePk"])] = g
        return live_map
    except _urlerr.URLError as exc:
        _debug_print(f"[live-debug] URLError fetching MLB Stats: {exc}")
        return {}
    except Exception as exc:
        _debug_print(f"[live-debug] Error fetching MLB Stats: {exc}")
        return {}


def _run_live_score_debug(label: str = "auto") -> str:
    """
    Fetch today's MLB live scores, match against stored results, and print a
    full diagnostic report.  Returns the report as a plain-text string.
    """
    lines: list[str] = []
    sep = "=" * 64

    def L(msg: str = "") -> None:
        lines.append(msg)
        _debug_print(msg)

    try:
        date_str = _today_et_str()
    except Exception as exc:
        return f"CRASH in _today_et_str(): {exc}\n{traceback.format_exc()}"

    try:
        return _run_live_score_debug_inner(label, date_str, lines, sep, L)
    except Exception as exc:
        tb = traceback.format_exc()
        _debug_print(f"[live-debug] CRASH: {exc}\n{tb}")
        lines.append(f"\nCRASH: {exc}\n{tb}")
        return "\n".join(lines)


def _run_live_score_debug_inner(label: str, date_str: str,
                                 lines: list, sep: str, L) -> str:
    # ── Get stored results (raw analysis state) ────────────────────────────
    raw_results: list[dict] = _analysis_state.get("results") or []

    # Build flat list: [{game_id, away_team, home_team}]
    flat: list[dict] = []
    for r in raw_results:
        g = r.get("game") if isinstance(r, dict) else None
        if not g:
            # Already-serialised result (e.g. loaded via init endpoint)
            g = r if isinstance(r, dict) else {}
        flat.append({
            "game_id":   g.get("id") or g.get("game_id") or "?",
            "away_team": g.get("away_team") or "?",
            "home_team": g.get("home_team") or "?",
        })

    # Fall back to analysis cache file if state is empty
    if not flat and _ANALYSIS_CACHE_FILE.exists():
        try:
            payload = json.loads(_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
            for r in payload.get("results", []):
                g = r.get("game") or r
                flat.append({
                    "game_id":   g.get("id") or g.get("game_id") or "?",
                    "away_team": g.get("away_team") or "?",
                    "home_team": g.get("home_team") or "?",
                })
            if flat:
                L(f"  (using cache file — {len(flat)} results)")
        except Exception as exc:
            L(f"  cache file read error: {exc}")

    L(sep)
    L(f"LIVE SCORE DEBUG  trigger={label}  date={date_str}  "
      f"results_in_state={len(raw_results)}  flat={len(flat)}")
    L(sep)

    if not flat:
        L("  NO results available — run analysis first.")
        L(sep)
        return "\n".join(lines)

    # ── Fetch live scores ──────────────────────────────────────────────────
    L(f"  Fetching: {_MLB_STATS_BASE}/schedule?sportId=1&date={date_str}&hydrate=linescore")
    live_map = _fetch_mlb_linescore_raw(date_str)
    L(f"  MLB Stats API returned {len(live_map)} game(s)")

    if not live_map:
        L("  WARNING: empty response — wrong date, network issue, or API down.")
        L(sep)
        return "\n".join(lines)

    # ── Print every game returned by the API ──────────────────────────────
    L("")
    L("  Games from MLB Stats API:")
    state_counts: dict[str, int] = {}
    for pk, game in sorted(live_map.items()):
        status = game.get("status", {})
        state  = status.get("abstractGameState", "?")
        detail = status.get("detailedState", "")
        away_n = game["teams"]["away"]["team"]["name"]
        home_n = game["teams"]["home"]["team"]["name"]
        ls     = game.get("linescore") or {}
        score  = ""
        if ls and state == "Live":
            ar  = ls.get("teams", {}).get("away", {}).get("runs", "?")
            hr  = ls.get("teams", {}).get("home", {}).get("runs", "?")
            inn = ls.get("currentInningOrdinal", "")
            half = "▲" if ls.get("isTopInning") else "▼"
            b   = ls.get("balls", 0)
            s   = ls.get("strikes", 0)
            o   = ls.get("outs", 0)
            score = f"  {ar}-{hr} {half}{inn} B{b}S{s}O{o}"
        elif ls and state == "Final":
            ar = ls.get("teams", {}).get("away", {}).get("runs", "?")
            hr = ls.get("teams", {}).get("home", {}).get("runs", "?")
            score = f"  Final {ar}-{hr}"
        L(f"    pk={pk:<8} [{state:<8}] ({detail:<20}) {away_n} @ {home_n}{score}")
        state_counts[state] = state_counts.get(state, 0) + 1

    L("")
    L(f"  State summary: " +
      "  ".join(f"{s}={n}" for s, n in sorted(state_counts.items())))

    # ── Build name→pk lookup ───────────────────────────────────────────────
    # normalise the same way as JS enrichResultsFromSchedule
    _NORM = {"Oakland Athletics": "Athletics"}
    def _norm(n: str) -> str:
        return _NORM.get(n, n).strip().lower()

    name_map: dict[str, int] = {}
    for pk, game in live_map.items():
        name_map[_norm(game["teams"]["away"]["team"]["name"])] = pk
        name_map[_norm(game["teams"]["home"]["team"]["name"])] = pk

    # ── Match results → live_map ───────────────────────────────────────────
    L("")
    L("  Matching stored results to live_map:")
    match_ok = match_miss = 0
    for r in flat:
        away_n = r["away_team"]
        home_n = r["home_team"]
        gid    = r["game_id"]

        pk_away = name_map.get(_norm(away_n))
        pk_home = name_map.get(_norm(home_n))
        matched_pk = pk_away or pk_home

        # Check if BOTH teams resolve to the SAME gamePk (true match)
        if pk_away and pk_home and pk_away == pk_home:
            game   = live_map[matched_pk]
            state  = game.get("status", {}).get("abstractGameState", "?")
            result = f"✓ MATCH  pk={matched_pk}  state={state}"
            match_ok += 1
        elif matched_pk:
            result = (f"⚠ PARTIAL MATCH  pk={matched_pk}  "
                      f"(away_found={pk_away is not None}, home_found={pk_home is not None})")
            match_ok += 1
        else:
            result = (f"✗ NO MATCH  "
                      f"away='{away_n}' → norm='{_norm(away_n)}'  "
                      f"home='{home_n}' → norm='{_norm(home_n)}'")
            match_miss += 1

        L(f"    [{str(gid)[:16]}...]  {away_n} @ {home_n}  →  {result}")

    L("")
    L(f"  Match result: {match_ok} matched, {match_miss} unmatched out of {len(flat)}")
    if match_miss > 0:
        L("")
        L("  FIX NEEDED: add unmatched team names to MLB_NAME_NORM in index.html")
        L("  and to _NORM dict in app.py _run_live_score_debug()")

    L(sep)
    return "\n".join(lines)


# ── Background debug thread: runs every 60 s alongside live score polling ─────
def _live_debug_loop() -> None:
    """Daemon thread: wait 15 s for startup, then log every 60 s."""
    time.sleep(15)          # let Flask fully start before first run
    while True:
        try:
            if _analysis_state.get("results"):
                _run_live_score_debug("auto-60s")
        except Exception as exc:
            _debug_print(f"[live-debug] background error: {exc}")
        time.sleep(60)


_debug_thread = threading.Thread(target=_live_debug_loop, daemon=True, name="live-debug")
_debug_thread.start()


@app.route("/api/debug/live-scores", methods=["GET"])
def debug_live_scores():
    """On-demand live score diagnostic — called by the Debug button in the UI."""
    try:
        report = _run_live_score_debug("manual-button")
        return jsonify({"report": report, "log_file": str(_DEBUG_LOG.resolve())})
    except Exception as exc:
        tb = traceback.format_exc()
        _debug_print(f"[debug-endpoint] CRASHED: {exc}\n{tb}")
        return jsonify({
            "report": f"ERROR: {exc}\n\nTraceback:\n{tb}",
            "log_file": str(_DEBUG_LOG.resolve()),
            "error": True,
        })


@app.route("/api/init", methods=["GET"])
def init_analysis():
    """Return today's cached analysis for auto-load on startup. No API calls."""
    try:
        if not _ANALYSIS_CACHE_FILE.exists():
            return jsonify({"has_predictions": False})
        payload = json.loads(_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
        today   = datetime.now(timezone.utc).date().isoformat()
        if payload.get("date") != today:
            return jsonify({"has_predictions": False})
        return jsonify({
            "has_predictions": True,
            "analyzed_at":     payload.get("analyzed_at"),
            "sport":           payload.get("sport", "mlb"),
            "games_loaded":    payload.get("games_loaded", 0),
            "cv_accuracy":     payload.get("cv_accuracy"),
            "lr_cv_accuracy":  payload.get("lr_cv_accuracy"),
            "nn_val_accuracy": payload.get("nn_val_accuracy"),
            "results":         payload.get("results", []),
            "parlays":         payload.get("parlays", {}),
        })
    except Exception:
        return jsonify({"has_predictions": False})


def _run_daily_picks_selection() -> None:
    """
    Run cross-sport daily picks selection using the latest cached results from
    both MLB and WNBA analyses.  Called at the end of each /api/analyze and
    /api/wnba/analyze route so picks always reflect the most-recent data from
    whichever sport was last analyzed.
    """
    mlb_results  = _analysis_state.get("results")  or []
    wnba_results = _wnba_analysis_state.get("results") or []
    if not mlb_results and not wnba_results:
        return
    try:
        mlb_ledger  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba_ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        select_daily_picks(mlb_results, wnba_results, mlb_ledger, wnba_ledger)
    except Exception as exc:
        # Daily picks selection should never crash the main analyze route
        import traceback as _tb
        print(f"[daily_picks] Warning: selection failed — {exc}\n{_tb.format_exc()}")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Run the full analysis pipeline (mirrors main.py Steps 1-4 + 6)."""
    data       = request.get_json() or {}
    sport      = data.get("sport", "mlb")
    bankroll   = float(data.get("bankroll", 250))
    season     = int(data.get("season", int(os.getenv("NFL_SEASON", 2024))))
    games_lim  = int(data.get("games", 0))

    odds_key   = os.getenv("ODDS_API_KEY", "")
    sports_key = os.getenv("API_SPORTS_KEY", "")

    if not odds_key or odds_key == "your_odds_api_key_here":
        return jsonify({"error": "ODDS_API_KEY not configured in .env"}), 400
    if not sports_key or sports_key == "your_api_sports_key_here":
        return jsonify({"error": "API_SPORTS_KEY not configured in .env"}), 400

    # Auto-settle any completed open bets before running fresh analysis
    try:
        _settle_ledger = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        _oc_settle     = OddsClient(odds_key, _cache)
        _sport_cfg     = SPORTS.get(sport, SPORTS["mlb"])
        _settle_ledger.settle(_oc_settle, _sport_cfg.odds_key)
    except Exception:
        pass

    # ── Cache control params from frontend ───────────────────────────────────
    # force_refresh=True  → always hit the API, ignore any cached results
    # use_cached=True     → return existing in-memory results without any API call,
    #                       even if the TTL has expired (user chose "Use Cached Data")
    force_refresh = bool(data.get("force_refresh", False))
    use_cached    = bool(data.get("use_cached",    False))
    _last         = _analysis_state.get("last_analyzed_at")
    _has_results  = (
        _analysis_state.get("sport") == sport
        and bool(_analysis_state.get("results"))
    )

    if (
        not force_refresh
        and _has_results
        and (
            use_cached
            or (
                _last is not None
                and (datetime.now(timezone.utc) - _last).total_seconds() < _ANALYSIS_TTL
            )
        )
    ):
        _ledger_cache   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        _s_bankroll     = _ledger_cache.data.get("personal_starting_bankroll", bankroll)
        serialized = [_serialize(r, bankroll, sport, _s_bankroll) for r in _analysis_state["results"]]
        parlays    = _generate_parlays(serialized, bankroll)
        meta       = _analysis_state.get("last_analysis_meta", {})
        _analysis_state["parlays"]  = parlays
        _analysis_state["bankroll"] = bankroll
        return jsonify({
            "success":         True,
            "cached":          True,
            "sport":           sport,
            "bankroll":        bankroll,
            "games_loaded":    meta.get("games_loaded", 0),
            "model_status":    meta.get("model_status", ""),
            "cv_accuracy":     meta.get("cv_accuracy"),
            "lr_cv_accuracy":  meta.get("lr_cv_accuracy"),
            "nn_val_accuracy": meta.get("nn_val_accuracy"),
            "results":         serialized,
            "parlays":         parlays,
        })

    sport_cfg = SPORTS[sport]

    try:
        # Step 1 — season data
        store = GameStore(
            api_key=sports_key,
            base_url=sport_cfg.api_sports_base,
            league_id=sport_cfg.league_id,
            sport_tag=sport,
            cache=_cache,
        )
        n_completed = store.load(season)

        # Step 2 — feature builder
        if sport == "nfl":
            from src.features import FeatureBuilder
            fb = FeatureBuilder(store)
        else:
            from src.mlb_features import MLBFeatureBuilder
            fb = MLBFeatureBuilder(store)

        # Step 3 — models (moneyline + run line + totals for MLB)
        model  = BettingModel(sport_cfg)
        status = model.train_or_load(
            stats_client=store, feature_builder=fb,
            season=season, force_retrain=False,
        )
        cv_acc     = float(model.cv_accuracy)      if model.cv_accuracy      else None
        lr_cv_acc  = float(model.lr_cv_accuracy)  if model.lr_cv_accuracy  else None
        nn_val_acc = float(model.nn_val_accuracy) if model.nn_val_accuracy else None

        rl_model = totals_model = None
        if sport == "mlb":
            rl_model = RunLineModel()
            rl_status = rl_model.train_or_load(store, fb, season)
            print(f"  {rl_status}")
            totals_model = TotalsModel()
            tot_status = totals_model.train_or_load(store, fb, season)
            print(f"  {tot_status}")

        # Step 4 — odds
        odds_client = OddsClient(odds_key, _cache)
        games = odds_client.get_nfl_odds(sport_key=sport_cfg.odds_key)

        # Freeze pre-game odds: for started games, restore market odds from before first pitch
        games = _lock_in_pre_game_odds(games)

        if not games:
            return jsonify({
                "success": True, "no_games": True, "results": [],
                "model_status": status,
                "cv_accuracy": cv_acc, "lr_cv_accuracy": lr_cv_acc, "nn_val_accuracy": nn_val_acc,
                "games_loaded": n_completed, "sport": sport, "bankroll": bankroll,
            })

        if games_lim > 0:
            games = games[:games_lim]

        # Step 5 — load model weights then predict + explain
        _wt_ledger    = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        model_weights = _wt_ledger.get_model_weights()

        explainer    = PredictionExplainer(sport_cfg)
        rl_explainer = PredictionExplainer(sport_cfg) if rl_model else None
        results = []
        for game in games:
            built = fb.build_for_game(game)
            if built is None:
                continue
            feature_vec, meta = built
            prediction  = model.predict(feature_vec, weights=model_weights)
            shap_result = explainer.explain(
                feature_vec, model=model.get_raw_model(),
                scaler=model.get_scaler(), is_trained=model.is_trained,
            )
            # Run line prediction (MLB only)
            rl_pred = None
            if rl_model and rl_model.is_trained:
                rl_pred = rl_model.predict(feature_vec, game, weights=model_weights)
                if rl_pred and rl_explainer:
                    rl_shap = rl_explainer.explain(
                        feature_vec, model=rl_model.get_raw_model(),
                        scaler=rl_model.get_scaler(), is_trained=rl_model.is_trained,
                    )
                    rl_pred["shap"] = rl_shap

            # Totals prediction (MLB only, requires O/U line from odds API)
            totals_pred = None
            if totals_model and totals_model.is_trained and game.get("total_line") is not None:
                totals_vec = fb.build_totals_from_meta(meta)
                if totals_vec is not None:
                    totals_pred = totals_model.predict(totals_vec, game, weights=model_weights)

            results.append({
                "game":        game,
                "prediction":  prediction,
                "shap":        shap_result,
                "meta":        meta,
                "rl_pred":     rl_pred,
                "totals_pred": totals_pred,
            })

        # Compute upset factor for each game (MLB only; cached 1h per team)
        if sport == "mlb":
            _upset_calc.season = season
            for r in results:
                g = r["game"]
                game_date = g.get("commence_time", "")[:10]
                try:
                    r["upset"] = _upset_calc.compute(
                        g["home_team"], g["away_team"], game_date
                    )
                except Exception:
                    r["upset"] = {}

        # Cache raw results for bet-tracking endpoints
        _analysis_state["sport"]    = sport
        _analysis_state["bankroll"] = bankroll
        _analysis_state["results"]  = results
        _analysis_state["parlays"]  = {}  # reset until computed below

        # Step 6 — cross-sport daily picks selection (top-5 per category, Half Kelly)
        _run_daily_picks_selection()

        # Reload ledger to get current personal_starting_bankroll for serialization
        _ledger_for_serial  = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        personal_starting   = _ledger_for_serial.data.get("personal_starting_bankroll", bankroll)

        serialized = [_serialize(r, bankroll, sport, personal_starting) for r in results]
        parlays    = _generate_parlays(serialized, bankroll)
        _analysis_state["parlays"]            = parlays
        _analysis_state["last_analyzed_at"]   = datetime.now(timezone.utc)
        _analysis_state["last_analysis_meta"] = {
            "games_loaded":    n_completed,
            "model_status":    status,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
        }
        _save_analysis_cache(serialized, parlays, sport, n_completed,
                             cv_acc, lr_cv_acc, nn_val_acc)
        return jsonify({
            "success":         True,
            "cached":          False,
            "sport":           sport,
            "season":          season,
            "games_loaded":    n_completed,
            "model_status":    status,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
            "results":         serialized,
            "parlays":         parlays,
            "bankroll":        bankroll,
        })

    except Exception as exc:
        return jsonify({"error": str(exc), "detail": traceback.format_exc()}), 500


@app.route("/api/refresh_models", methods=["POST"])
def refresh_models():
    """Retrain all ML models on cached data and rerun predictions. No odds/stats API calls."""
    data     = request.get_json() or {}
    sport    = data.get("sport", _analysis_state.get("sport", "mlb"))
    bankroll = float(data.get("bankroll", _analysis_state.get("bankroll", 250)))
    season   = int(data.get("season", int(os.getenv("NFL_SEASON", 2024))))

    if not _analysis_state.get("results"):
        # Fall back to disk cache so the button works after an app restart
        try:
            payload = json.loads(_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
            cached_results = payload.get("results", [])
            if not cached_results:
                return jsonify({"error": "No game data found. Run Analysis first."}), 400
            # Rebuild raw result stubs so the prediction loop has game dicts to work from
            _analysis_state["results"] = [{"game": r["game"], "prediction": {}, "shap": None,
                                            "meta": None, "rl_pred": None, "totals_pred": None}
                                           for r in cached_results if r.get("game")]
            _analysis_state["sport"]   = payload.get("sport", sport)
        except Exception:
            return jsonify({"error": "No game data in memory. Run Analysis first."}), 400

    existing_results = _analysis_state["results"]
    sport_cfg = SPORTS[sport]

    try:
        # Load store from disk cache — no API call if 24 h cache is still valid
        store = GameStore(
            api_key=os.getenv("API_SPORTS_KEY", ""),
            base_url=sport_cfg.api_sports_base,
            league_id=sport_cfg.league_id,
            sport_tag=sport,
            cache=_cache,
        )
        n_completed = store.load(season)

        if sport == "nfl":
            from src.features import FeatureBuilder
            fb = FeatureBuilder(store)
        else:
            from src.mlb_features import MLBFeatureBuilder
            fb = MLBFeatureBuilder(store)

        # Force-retrain all models
        model  = BettingModel(sport_cfg)
        status = model.train_or_load(store, fb, season, force_retrain=True)
        cv_acc     = float(model.cv_accuracy)      if model.cv_accuracy      else None
        lr_cv_acc  = float(model.lr_cv_accuracy)  if model.lr_cv_accuracy  else None
        nn_val_acc = float(model.nn_val_accuracy) if model.nn_val_accuracy else None

        rl_model = totals_model = None
        if sport == "mlb":
            rl_model = RunLineModel()
            rl_model.train_or_load(store, fb, season, force_retrain=True)
            totals_model = TotalsModel()
            totals_model.train_or_load(store, fb, season, force_retrain=True)

        # Re-run predictions on the same games — no odds API call
        _wt_ledger2   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        model_weights = _wt_ledger2.get_model_weights()

        explainer    = PredictionExplainer(sport_cfg)
        rl_explainer = PredictionExplainer(sport_cfg) if rl_model else None
        results = []
        for r in existing_results:
            game = r["game"]
            built = fb.build_for_game(game)
            if built is None:
                continue
            feature_vec, meta = built

            prediction  = model.predict(feature_vec, weights=model_weights)
            shap_result = explainer.explain(
                feature_vec, model=model.get_raw_model(),
                scaler=model.get_scaler(), is_trained=model.is_trained,
            )

            rl_pred = None
            if rl_model and rl_model.is_trained:
                rl_pred = rl_model.predict(feature_vec, game, weights=model_weights)
                if rl_pred and rl_explainer:
                    rl_shap = rl_explainer.explain(
                        feature_vec, model=rl_model.get_raw_model(),
                        scaler=rl_model.get_scaler(), is_trained=rl_model.is_trained,
                    )
                    rl_pred["shap"] = rl_shap

            totals_pred = None
            if totals_model and totals_model.is_trained and game.get("total_line") is not None:
                totals_vec = fb.build_totals_from_meta(meta)
                if totals_vec is not None:
                    totals_pred = totals_model.predict(totals_vec, game, weights=model_weights)

            results.append({
                "game": game, "prediction": prediction,
                "shap": shap_result, "meta": meta,
                "rl_pred": rl_pred, "totals_pred": totals_pred,
            })

        # Recompute upset factors
        if sport == "mlb":
            _upset_calc.season = season
            for r in results:
                g = r["game"]
                game_date = g.get("commence_time", "")[:10]
                try:
                    r["upset"] = _upset_calc.compute(g["home_team"], g["away_team"], game_date)
                except Exception:
                    r["upset"] = {}

        # Update in-memory state
        _analysis_state["results"]  = results
        _analysis_state["bankroll"] = bankroll

        ledger     = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        s_bankroll = ledger.data.get("personal_starting_bankroll", bankroll)
        serialized = [_serialize(r, bankroll, sport, s_bankroll) for r in results]
        parlays    = _generate_parlays(serialized, bankroll)
        _analysis_state["parlays"] = parlays

        _save_analysis_cache(serialized, parlays, sport, n_completed, cv_acc, lr_cv_acc, nn_val_acc)

        return jsonify({
            "success":         True,
            "cached":          False,
            "sport":           sport,
            "bankroll":        bankroll,
            "games_loaded":    n_completed,
            "model_status":    status,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
            "results":         serialized,
            "parlays":         parlays,
        })

    except Exception as exc:
        return jsonify({"error": str(exc), "detail": traceback.format_exc()}), 500


@app.route("/api/model-detail", methods=["GET"])
def model_detail():
    """Hidden developer endpoint — raw individual model outputs for all games.
    Returns XGB/LR/NN raw probabilities, disagreements, ensemble decisions,
    and the model weights currently in use.
    Not linked from the UI; for debugging only.
    """
    results = _analysis_state.get("results", [])
    if not results:
        return jsonify({"error": "No analysis data in memory. Run Analysis first."}), 404

    sport = _analysis_state.get("sport", "mlb")

    # Current model weights from settled bet history
    _md_ledger    = Ledger(path="data/ledger.json", starting_bankroll=250)
    model_weights = _md_ledger.get_model_weights()

    out = []
    for r in results:
        g    = r.get("game", {})
        pred = r.get("prediction", {}) or {}
        rl   = r.get("rl_pred")   or {}
        tot  = r.get("totals_pred") or {}

        ml_conf = confidence_tier(
            float(pred.get("xgb_prob", 0.5)),
            float(pred.get("lr_prob",  0.5)),
            float(pred["nn_prob"]) if pred.get("nn_prob") is not None else None,
        )

        entry = {
            "game_id":    g.get("id"),
            "home_team":  g.get("home_team"),
            "away_team":  g.get("away_team"),
            "commence_time": g.get("commence_time"),
            "moneyline": {
                "xgb_prob":          pred.get("xgb_prob"),
                "lr_prob":           pred.get("lr_prob"),
                "nn_prob":           pred.get("nn_prob"),
                "effective_weights": pred.get("effective_weights"),
                "ensemble_prob":     pred.get("home_win_prob"),
                "models_agree":      pred.get("models_agree"),
                "confidence_tier":   ml_conf,
            },
            "run_line": {
                "xgb_prob":          rl.get("xgb_prob"),
                "lr_prob":           rl.get("lr_prob"),
                "nn_prob":           rl.get("nn_prob"),
                "effective_weights": rl.get("effective_weights"),
                "home_cover_prob":   rl.get("home_cover_prob"),
                "models_agree":      rl.get("models_agree"),
                "confidence_tier":   confidence_tier(
                    float(rl.get("xgb_prob", 0.5)),
                    float(rl.get("lr_prob",  0.5)),
                    float(rl["nn_prob"]) if rl.get("nn_prob") is not None else None,
                ) if rl else None,
            } if rl else None,
            "totals": {
                "xgb_pred":          tot.get("xgb_pred"),
                "lr_pred":           tot.get("lr_pred"),
                "nn_pred":           tot.get("nn_pred"),
                "effective_weights": tot.get("effective_weights"),
                "predicted_total":   tot.get("predicted_total"),
                "total_line":        tot.get("total_line"),
                "models_agree":      tot.get("models_agree"),
            } if tot else None,
        }
        out.append(entry)

    return jsonify({
        "sport":         sport,
        "game_count":    len(out),
        "model_weights": model_weights,
        "note":          "Raw individual model outputs — for debugging only, not shown in UI.",
        "games":         out,
    })


@app.route("/api/ledger", methods=["GET"])
def get_ledger():
    """Return unified ledger summary (MLB + WNBA combined), open bets, and history."""
    bankroll   = float(request.args.get("bankroll", _analysis_state["bankroll"] or 250))
    sport      = request.args.get("sport", _analysis_state["sport"] or "mlb")
    sport_cfg  = SPORTS.get(sport, SPORTS["mlb"])
    ledger     = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
    wledger    = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)

    # Attempt to auto-settle MLB and WNBA games via Odds API (one shared client)
    settled: list = []
    odds_key = os.getenv("ODDS_API_KEY", "")
    if odds_key and odds_key != "your_odds_api_key_here":
        oc = OddsClient(odds_key, _cache)
        try:
            settled.extend(ledger.settle(oc, sport_cfg.odds_key))
        except Exception:
            pass
        try:
            settled.extend(wledger.settle(oc, "basketball_wnba"))
        except Exception:
            pass

    summary = ledger.get_summary()

    # ── All model history from BOTH sports (for model tab W/L record) ─────────
    # MLB "bet_type" uses: "single" (ML), "run_line" (RL), "totals"
    # WNBA "bet_type" uses: "single" (ML), "spread",         "totals"
    _all_model_hist = ledger.data["history"] + wledger.data["history"]

    # Combined model W/L record and P&L across ALL 15 daily picks (both sports)
    model_wins_all   = sum(1 for h in _all_model_hist if h["result"] == "win")
    model_losses_all = sum(1 for h in _all_model_hist if h["result"] == "loss")
    model_pnl_all    = round(sum(h.get("model_pnl", 0) for h in _all_model_hist), 2)

    # ── Merge WNBA confirmed bets into the unified My Bets view ──────────────
    # open_bets: all MLB open bets + all WNBA open bets (deduped)
    all_open = ledger.data["open_bets"] + [
        b for b in wledger.data["open_bets"]
        if b not in ledger.data["open_bets"]
    ]

    # confirmed open bets across both sports (for My Bets tab display)
    confirmed_open = [b for b in all_open if b.get("confirmed")]

    # history: merge MLB + WNBA confirmed history, sort by placed_at descending
    wnba_conf_hist = [b for b in wledger.data["history"] if b.get("confirmed")]
    mlb_conf_hist  = [b for b in ledger.data["history"]  if b.get("confirmed")]
    combined_conf_hist = sorted(
        mlb_conf_hist + wnba_conf_hist,
        key=lambda b: b.get("placed_at", ""),
        reverse=True,
    )

    # Combined confirmed W/L record and P&L across both sports
    conf_wins   = sum(1 for h in combined_conf_hist if h["result"] == "win")
    conf_losses = sum(1 for h in combined_conf_hist if h["result"] == "loss")
    conf_pnl    = round(sum(h.get("confirmed_pnl", 0) for h in combined_conf_hist), 2)

    # ── Permanent archive — drives all-time W/L records ──────────────────────
    _archive_bets = _load_archive_bets()

    archive_model_wins   = sum(1 for h in _archive_bets if h.get("result") == "win")
    archive_model_losses = sum(1 for h in _archive_bets if h.get("result") == "loss")
    archive_model_pnl    = round(sum(h.get("model_pnl", 0) for h in _archive_bets), 2)

    # Build a unified summary — patch in cross-sport model AND confirmed figures
    # model_record and model_pnl now reflect the full permanent archive
    unified_summary = dict(summary)
    unified_summary["model_record"]     = (archive_model_wins, archive_model_losses)
    unified_summary["model_pnl"]        = archive_model_pnl
    unified_summary["confirmed_record"] = (conf_wins, conf_losses)
    unified_summary["confirmed_pnl"]    = conf_pnl

    # ── Per-type all-time records — also from archive ─────────────────────────
    # Categories: moneyline ("single"), run_line_spread ("run_line"/"spread"), totals
    _full_hist = _all_model_hist  # kept for _conf_rec (per-confidence breakdown)

    CAT_ALIASES = [
        ("moneyline",       ["single"]),
        ("run_line_spread", ["run_line", "spread"]),
        ("totals",          ["totals"]),
    ]

    def _type_rec(hist, conf):
        """
        Return per-category all-time W/L from the permanent archive.
        `hist` parameter is ignored (kept for call-site compatibility).
        Keys: "moneyline", "run_line_spread", "totals"
        """
        out = {}
        for cat_key, aliases in CAT_ALIASES:
            sub = [h for h in _archive_bets if h.get("bet_type", "single") in aliases]
            if conf is not None:
                sub = [h for h in sub if bool(h.get("confirmed")) == conf]
            out[cat_key] = [
                sum(1 for h in sub if h.get("result") == "win"),
                sum(1 for h in sub if h.get("result") == "loss"),
            ]
        return out

    def _conf_rec(hist, confirmed_only):
        out = {}
        for tier in ("strong", "moderate", "low"):
            sub = [h for h in hist if h.get("confidence_tier", "strong") == tier]
            if confirmed_only:
                sub = [h for h in sub if h.get("confirmed")]
            out[tier] = [
                sum(1 for h in sub if h["result"] == "win"),
                sum(1 for h in sub if h["result"] == "loss"),
            ]
        return out

    # Combined model history (both sports), most recent 120 entries, for today+yesterday display
    combined_model_hist = sorted(
        _all_model_hist,
        key=lambda h: h.get("placed_at", ""),
        reverse=True,
    )[:120]

    return jsonify({
        "summary":           _py(unified_summary),
        "open_bets":         _py(all_open),
        "confirmed_open":    _py(confirmed_open),
        "confirmed_history": _py(combined_conf_hist[:50]),
        "history":           _py(combined_model_hist),
        "settled_now":       _py(settled),
        "type_records": {
            "model":     _type_rec(_full_hist, None),
            "confirmed": _type_rec(_full_hist, True),
        },
        "conf_records": {
            "model":     _conf_rec(_full_hist, False),
            "confirmed": _conf_rec(_full_hist, True),
        },
        "daily_picks":  _py(load_daily_picks()),
    })


@app.route("/api/daily-picks", methods=["GET"])
def get_daily_picks():
    """Return the most-recently saved cross-sport daily picks."""
    return jsonify(_py(load_daily_picks()))


@app.route("/api/clipboard", methods=["POST"])
def write_clipboard():
    """
    Write text to the OS clipboard from Python.
    This is far more reliable inside QWebEngineView than the browser
    Clipboard API, which requires HTTPS or explicit permissions in some
    Chromium builds.
    """
    import subprocess
    text = (request.get_json(force=True) or {}).get("text", "")
    try:
        if os.name == "nt":
            # Windows: pipe UTF-16-LE to clip.exe (handles full Unicode)
            subprocess.run(
                ["clip"],
                input=text.encode("utf-16-le"),
                check=True,
                timeout=5,
            )
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           check=True, timeout=5)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), check=True, timeout=5)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/reset-all", methods=["POST"])
def reset_all():
    """
    Hard-reset all bet tracking data.
    - Clears open_bets and history in both ledgers.
    - Resets model_bankroll to model_starting_bankroll for each ledger.
    - Resets personal_bankroll to personal_starting_bankroll for each ledger.
    - Wipes daily_picks.json.
    Model files, analysis caches, and API settings are untouched.
    """
    try:
        _LEDGER_PATHS = [
            Path("data/ledger.json"),
            Path("data/wnba_ledger.json"),
        ]
        for path in _LEDGER_PATHS:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                # Preserve each bankroll's own starting value independently
                model_start    = float(data.get("model_starting_bankroll",    1000.0))
                personal_start = float(data.get("personal_starting_bankroll", 1000.0))
                clean = {
                    "model_starting_bankroll":    model_start,
                    "model_bankroll":             model_start,
                    "personal_starting_bankroll": personal_start,
                    "personal_bankroll":          personal_start,
                    "open_bets":                  [],
                    "history":                    [],
                }
                path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
            else:
                # Create fresh file with independent defaults
                clean = {
                    "model_starting_bankroll":    1000.0,
                    "model_bankroll":             1000.0,
                    "personal_starting_bankroll": 1000.0,
                    "personal_bankroll":          1000.0,
                    "open_bets":                  [],
                    "history":                    [],
                }
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(clean, indent=2), encoding="utf-8")

        # Wipe daily picks
        picks_path = Path("data/daily_picks.json")
        empty_picks = {
            "generated_at": None,
            "picks": {"moneyline": [], "run_line_spread": [], "totals": []},
        }
        picks_path.parent.mkdir(parents=True, exist_ok=True)
        picks_path.write_text(json.dumps(empty_picks, indent=2), encoding="utf-8")

        return jsonify({"success": True, "message": "All bet history cleared and records reset to 0-0."})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/archive", methods=["GET"])
def get_archive():
    """
    Return filtered bets from the permanent bet_history_archive.json.
    Query params:
      sport      — "mlb" | "wnba" | "" (all)
      bet_type   — "moneyline" | "run_line_spread" | "totals" | "" (all)
      result     — "win" | "loss" | "push" | "" (all)
      date_from  — YYYY-MM-DD (ET, inclusive)
      date_to    — YYYY-MM-DD (ET, inclusive)
      page       — 1-based page number (default 1)
      page_size  — records per page (default 50, max 200)
    """
    bets = _load_archive_bets()

    sport    = request.args.get("sport", "").strip().lower()
    bet_type = request.args.get("bet_type", "").strip().lower()
    result   = request.args.get("result", "").strip().lower()
    date_from = request.args.get("date_from", "").strip()
    date_to   = request.args.get("date_to",   "").strip()

    # Sport filter
    if sport:
        bets = [b for b in bets if (b.get("sport") or "mlb").lower() == sport]

    # Bet type filter — "moneyline" matches bet_type=="single",
    # "run_line_spread" matches "run_line" or "spread", "totals" matches "totals"
    if bet_type == "moneyline":
        bets = [b for b in bets if b.get("bet_type", "single") == "single"]
    elif bet_type == "run_line_spread":
        bets = [b for b in bets if b.get("bet_type", "single") in ("run_line", "spread")]
    elif bet_type == "totals":
        bets = [b for b in bets if b.get("bet_type") == "totals"]

    # Result filter
    if result in ("win", "loss", "push"):
        bets = [b for b in bets if b.get("result") == result]

    # Date filters — compare against placed_at (ISO UTC → ET date string for filtering)
    def _et_date(iso: str) -> str:
        """Convert ISO UTC timestamp to ET date string YYYY-MM-DD."""
        try:
            from datetime import timezone, timedelta
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            et = dt.astimezone(timezone(timedelta(hours=-5)))   # EST; close enough
            return et.strftime("%Y-%m-%d")
        except Exception:
            return iso[:10] if iso else ""

    if date_from:
        bets = [b for b in bets if _et_date(b.get("placed_at", "")) >= date_from]
    if date_to:
        bets = [b for b in bets if _et_date(b.get("placed_at", "")) <= date_to]

    # Sort newest first
    bets = sorted(bets, key=lambda b: b.get("placed_at", ""), reverse=True)

    # Pagination
    try:
        page      = max(1, int(request.args.get("page", 1)))
        page_size = min(200, max(1, int(request.args.get("page_size", 50))))
    except Exception:
        page = 1; page_size = 50

    total   = len(bets)
    start   = (page - 1) * page_size
    end     = start + page_size
    page_bets = bets[start:end]

    return jsonify({
        "bets":      _py(page_bets),
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     (total + page_size - 1) // page_size if page_size else 1,
    })


@app.route("/api/refresh-picks", methods=["POST"])
def refresh_picks():
    """
    Clear today's unsettled non-confirmed model picks from both ledgers, then
    immediately reselect the top-5 per category using cached analysis results.
    No new API calls — works entirely from _analysis_state / _wnba_analysis_state.
    Returns the refreshed daily picks + updated ledger summaries.
    """
    try:
        mlb_results  = _analysis_state.get("results")  or []
        wnba_results = _wnba_analysis_state.get("results") or []

        if not mlb_results and not wnba_results:
            return jsonify({
                "success": False,
                "error": "No cached analysis data. Run MLB or WNBA analysis first.",
            }), 400

        mlb_ledger  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba_ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)

        # Count all pending model picks that will be cleared and refunded
        mlb_removed  = sum(1 for b in mlb_ledger.data["open_bets"]
                           if not b.get("confirmed"))
        wnba_removed = sum(1 for b in wnba_ledger.data["open_bets"]
                           if not b.get("confirmed"))

        # Full reset — clears ALL non-confirmed model picks and restores bankroll,
        # then reselects top-5 per category from scratch
        daily = select_daily_picks(mlb_results, wnba_results, mlb_ledger, wnba_ledger)

        # Step 3: build ledger summaries for immediate UI update
        mlb_summary  = mlb_ledger.get_summary()
        wnba_summary = wnba_ledger.get_summary()

        return jsonify({
            "success":      True,
            "daily_picks":  _py(daily),
            "mlb_removed":  mlb_removed,
            "wnba_removed": wnba_removed,
            "mlb_summary":  _py(mlb_summary),
            "wnba_summary": _py(wnba_summary),
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc),
                        "detail": traceback.format_exc()}), 500


@app.route("/api/ledger/confirm/<game_id>", methods=["POST"])
def confirm_bet(game_id: str):
    """Mark a model-tracked bet as user-confirmed, or add it fresh if missing."""
    data     = request.get_json() or {}
    bankroll = float(data.get("bankroll", _analysis_state["bankroll"] or 250))
    sport    = _analysis_state.get("sport") or "mlb"
    sport_cfg = SPORTS[sport]
    ledger   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)

    # Check if a moneyline model bet already exists — if so, promote it to confirmed
    for bet in ledger.data["open_bets"]:
        if bet["game_id"] == game_id and bet.get("bet_type", "single") == "single":
            if bet["confirmed"]:
                return jsonify({"error": "Already confirmed"}), 409
            _, conf_amt = ledger.kelly_amounts(bet["model_prob"], bet["american_odds"])
            conf_amt = round(conf_amt, 2)
            bet["confirmed"]        = True
            bet["confirmed_amount"] = conf_amt
            # Immediately deduct confirmed stake from personal bankroll
            if conf_amt > 0:
                ledger.data["personal_bankroll"] = round(
                    ledger.data["personal_bankroll"] - conf_amt, 2
                )
            ledger.save()
            return jsonify({"success": True, "confirmed_amount": conf_amt})

    # Not yet in ledger — pull from analysis cache and add as full bet
    raw = next((r for r in _analysis_state["results"] if r["game"]["id"] == game_id), None)
    if raw is None:
        return jsonify({"error": "Game not found in current analysis"}), 404

    g  = raw["game"]
    hp = float(raw["prediction"]["home_win_prob"])
    mp = float(g["home_implied_prob"])
    he = hp - mp

    if hp >= 0.5:
        side, team = "home", g["home_team"]
        odds = int(g.get("h2h_home_odds") or -110)
        model_p, edge = hp, he
    else:
        side, team = "away", g["away_team"]
        odds = int(g.get("h2h_away_odds") or -110)
        model_p, edge = 1 - hp, -he

    pred_full = raw["prediction"]
    ml_conf = confidence_tier(
        float(pred_full.get("xgb_prob", model_p)),
        float(pred_full.get("lr_prob",  model_p)),
        float(pred_full["nn_prob"]) if pred_full.get("nn_prob") is not None else None,
    )
    model_amt, conf_amt = ledger.kelly_amounts(model_p, odds)
    ledger.add_bet(
        game=g, sport=sport, sport_key=sport_cfg.odds_key,
        side=side, team=team, odds=odds,
        model_prob=model_p, edge=edge,
        model_amount=model_amt,
        confirmed=True, confirmed_amount=conf_amt,
        confidence_tier=ml_conf,
    )
    ledger.save()
    return jsonify({"success": True, "team": team,
                    "odds": odds, "confirmed_amount": conf_amt})


@app.route("/api/ledger/parlay", methods=["POST"])
def log_parlay():
    """Record all legs of a parlay as a grouped confirmed bet."""
    data       = request.get_json() or {}
    bankroll   = float(data.get("bankroll", _analysis_state["bankroll"] or 250))
    parlay_key = data.get("parlay_id")   # "safe" | "value" | "high_risk" | "lottery"

    sport     = _analysis_state.get("sport") or "mlb"
    sport_cfg = SPORTS[sport]

    parlay = _analysis_state.get("parlays", {}).get(parlay_key)
    if not parlay or not parlay.get("available"):
        return jsonify({"error": "Parlay not found or not available — run analysis first"}), 404

    legs = parlay.get("legs", [])
    if len(legs) < 2:
        return jsonify({"error": "Parlay must have at least 2 legs"}), 400

    bet_dollars  = float(parlay.get("bet_dollars", 0))
    parlay_name  = parlay.get("name", parlay_key)
    new_parlay_id = str(uuid.uuid4())

    ledger = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
    legs_tracked = 0

    for leg in legs:
        game_id = leg["game_id"]
        raw = next((r for r in _analysis_state["results"] if r["game"]["id"] == game_id), None)
        if raw is None:
            continue

        g = raw["game"]
        existing = next((b for b in ledger.data["open_bets"] if b["game_id"] == game_id), None)
        if existing:
            existing["confirmed"]        = True
            existing["confirmed_amount"] = round(bet_dollars, 2)
            existing["bet_type"]         = "parlay"
            existing["parlay_id"]        = new_parlay_id
            existing["parlay_name"]      = parlay_name
        else:
            ledger.add_bet(
                game=g, sport=sport, sport_key=sport_cfg.odds_key,
                side=leg["pick_side"], team=leg["pick_team"],
                odds=leg["pick_odds"],
                model_prob=leg["pick_prob"], edge=abs(leg["pick_edge"]),
                model_amount=0.0,
                confirmed=True, confirmed_amount=bet_dollars,
                bet_type="parlay", parlay_id=new_parlay_id, parlay_name=parlay_name,
                prop_line=leg.get("prop_line"),
            )
        legs_tracked += 1

    if legs_tracked == 0:
        return jsonify({"error": "No legs could be tracked — run analysis first"}), 400

    ledger.save()
    return jsonify({"success": True, "legs_tracked": legs_tracked, "parlay_id": new_parlay_id})


@app.route("/api/ledger/track_prop", methods=["POST"])
def track_prop():
    """Track a run line or totals bet (side bets added from the dashboard)."""
    data      = request.get_json() or {}
    game_id   = data.get("game_id")
    bet_type  = data.get("bet_type", "run_line")   # "run_line" or "totals"
    bankroll  = float(data.get("bankroll", _analysis_state["bankroll"] or 250))
    sport     = _analysis_state.get("sport") or "mlb"
    sport_cfg = SPORTS[sport]

    raw = next((r for r in _analysis_state["results"] if r["game"]["id"] == game_id), None)
    if raw is None:
        return jsonify({"error": "Game not found in current analysis"}), 404

    g = raw["game"]
    prop_line = None
    if bet_type == "run_line":
        pred = raw.get("rl_pred")
        if not pred:
            return jsonify({"error": "No run line prediction for this game"}), 404
        side        = pred["side"]
        team        = pred["pick_team"]
        odds        = pred["pick_odds"]
        model_p     = pred["pick_prob"]
        edge        = abs(pred["edge"])
        label       = "run_line"
        prop_line   = -float(pred.get("run_line_point", -1.5))  # settlement threshold = -run_line_point
    elif bet_type == "totals":
        pred = raw.get("totals_pred")
        if not pred:
            return jsonify({"error": "No totals prediction for this game"}), 404
        side        = pred["direction"]   # "over" or "under"
        team        = f"{pred['direction'].title()} {pred['total_line']}"
        odds        = pred["pick_odds"]
        model_p     = pred["pick_prob"]
        edge        = abs(pred["edge"])
        label       = "totals"
        prop_line   = float(pred["total_line"])
    else:
        return jsonify({"error": f"Unknown bet_type: {bet_type}"}), 400

    _ledger_tmp   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
    model_dollars, conf_dollars = _ledger_tmp.kelly_amounts(model_p, odds)
    model_dollars = round(model_dollars, 2)
    conf_dollars  = round(conf_dollars,  2)

    prop_conf = "strong" if pred.get("models_agree", True) else "low"
    ledger = Ledger(path="data/ledger.json", starting_bankroll=bankroll)

    # Deduplication guard: prevent tracking the same game+bet_type twice
    if ledger.has_bet(game_id, label):
        return jsonify({"error": f"Bet already tracked for this game ({label})"}), 409

    ledger.add_bet(
        game=g, sport=sport, sport_key=sport_cfg.odds_key,
        side=side, team=team, odds=odds,
        model_prob=model_p, edge=edge,
        model_amount=model_dollars,
        confirmed=True, confirmed_amount=conf_dollars,
        bet_type=label, prop_line=prop_line,
        confidence_tier=prop_conf,
    )
    ledger.save()
    return jsonify({
        "success":          True,
        "team":             team,
        "odds":             odds,
        "confirmed_amount": conf_dollars,
    })


@app.route("/api/ledger/settle_manual/<bet_id>", methods=["POST"])
def settle_manual(bet_id: str):
    """Manually settle a bet: result must be 'win', 'loss', or 'push'."""
    data     = request.get_json() or {}
    result   = data.get("result", "").lower()
    if result not in ("win", "loss", "push"):
        return jsonify({"error": "result must be win, loss, or push"}), 400
    bankroll = float(data.get("bankroll", _analysis_state["bankroll"] or 250))
    ledger   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
    settled  = ledger.settle_manual(bet_id, result)
    if settled is None:
        return jsonify({"error": "Bet not found"}), 404
    return jsonify({"success": True, "settled": _py(settled)})


@app.route("/api/ledger/set_bankroll", methods=["POST"])
def set_bankroll():
    """Update ONLY the personal (user-confirmed) bankroll on both MLB and WNBA ledgers.
    Never touches model_bankroll or model_starting_bankroll.
    Does not affect open_bets or history."""
    body   = request.get_json(force=True) or {}
    new_br = float(body.get("bankroll", 0))
    if new_br <= 0:
        return jsonify({"error": "Bankroll must be greater than 0"}), 400
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        ledger = Ledger(path=path, starting_bankroll=1000.0)
        # Snapshot model fields — MUST be preserved no matter what state the file is in
        saved_model_bankroll = ledger.data.get("model_bankroll",          1000.0)
        saved_model_starting = ledger.data.get("model_starting_bankroll", 1000.0)
        # Update only personal fields
        ledger.data["personal_starting_bankroll"] = new_br
        ledger.data["personal_bankroll"]          = new_br
        # Explicitly restore model fields (bulletproof guarantee)
        ledger.data["model_bankroll"]          = saved_model_bankroll
        ledger.data["model_starting_bankroll"] = saved_model_starting
        ledger.save()
    _analysis_state["bankroll"]      = new_br
    _wnba_analysis_state["bankroll"] = new_br
    return jsonify({"success": True, "bankroll": new_br})


@app.route("/api/ledger/set_model_bankroll", methods=["POST"])
def set_model_bankroll():
    """Update ONLY the model bankroll on both MLB and WNBA ledgers.
    Never touches personal_bankroll or personal_starting_bankroll.
    Does not affect open_bets or history."""
    body   = request.get_json(force=True) or {}
    new_br = float(body.get("bankroll", 0))
    if new_br <= 0:
        return jsonify({"error": "Bankroll must be greater than 0"}), 400
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        ledger = Ledger(path=path, starting_bankroll=1000.0)
        # Snapshot personal fields — MUST be preserved no matter what state the file is in
        saved_personal_bankroll = ledger.data.get("personal_bankroll",          ledger._starting)
        saved_personal_starting = ledger.data.get("personal_starting_bankroll", ledger._starting)
        # Update only model fields
        ledger.data["model_starting_bankroll"] = new_br
        ledger.data["model_bankroll"]          = new_br
        # Explicitly restore personal fields (bulletproof guarantee)
        ledger.data["personal_bankroll"]          = saved_personal_bankroll
        ledger.data["personal_starting_bankroll"] = saved_personal_starting
        ledger.save()
    return jsonify({"success": True, "bankroll": new_br})


@app.route("/api/ledger/bet/<bet_id>", methods=["DELETE"])
def remove_bet(bet_id: str):
    """Remove an open bet and return its stake to the available balance."""
    bankroll = float(request.args.get("bankroll", _analysis_state["bankroll"] or 250))
    ledger   = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
    removed  = next((b for b in ledger.data["open_bets"] if b["id"] == bet_id), None)
    if removed is None:
        return jsonify({"error": "Bet not found"}), 404
    # Return the stake to the available balance (undo the deduction made at placement)
    if not removed.get("limit_reached"):
        model_amt = removed.get("model_amount", 0.0)
        if model_amt > 0:
            ledger.data["model_bankroll"] = round(
                ledger.data["model_bankroll"] + model_amt, 2
            )
        if removed.get("confirmed"):
            conf_amt = removed.get("confirmed_amount", 0.0)
            if conf_amt > 0:
                ledger.data["personal_bankroll"] = round(
                    ledger.data["personal_bankroll"] + conf_amt, 2
                )
    ledger.data["open_bets"] = [b for b in ledger.data["open_bets"] if b["id"] != bet_id]
    ledger.save()
    return jsonify({"success": True})


def _build_explain_prompt(d: dict) -> str:
    bet_type = d.get("bet_type", "ml")
    home     = d.get("home_team", "Home")
    away     = d.get("away_team", "Away")
    home_sp  = d.get("home_sp") or {}
    away_sp  = d.get("away_sp") or {}
    uf       = d.get("upset_factor") or {}
    shap     = d.get("shap_features") or []

    odds_val = d.get("pick_odds")
    odds_str = (f"{odds_val:+d}" if isinstance(odds_val, int)
                else f"{int(odds_val):+d}" if odds_val is not None else "n/a")

    edge     = d.get("pick_edge") or 0
    edge_str = f"{edge * 100:+.1f}%"

    if bet_type == "ml":
        pick_desc = f"{d.get('pick_team')} moneyline at {odds_str}"
        conf_desc = (f"XGBoost {d.get('xgb_prob', 0)*100:.1f}% / "
                     f"LR {d.get('lr_prob', 0)*100:.1f}%")
    elif bet_type == "run_line":
        home_pt  = float(d.get("run_line_point") or -1.5)
        side     = d.get("pick_side", "home")
        team     = d.get("pick_team") or (home if side == "home" else away)
        pick_pt  = home_pt if side == "home" else -home_pt
        pt_str   = f"+{abs(pick_pt)}" if pick_pt > 0 else f"{pick_pt}"
        pick_desc = f"{team} {pt_str} run line at {odds_str}"
        conf_desc = (f"XGBoost {d.get('xgb_prob', 0)*100:.1f}% / "
                     f"LR {d.get('lr_prob', 0)*100:.1f}%")
    else:  # totals
        pf = d.get("park_factor", 1.0) or 1.0
        pick_desc = (f"{(d.get('direction') or 'over').upper()} "
                     f"{d.get('total_line')} at {odds_str}")
        conf_desc = (f"Predicted total: {d.get('predicted_total')} runs "
                     f"(XGB {d.get('xgb_pred')}, LR {d.get('lr_pred')}) · "
                     f"Park factor {pf:.2f}×")

    shap_lines = "\n".join(
        f"  - {f.get('label', f.get('feature', '?'))}: {f.get('shap_value', 0):+.3f}"
        for f in shap[:3]
    )
    shap_block = f"Top model features:\n{shap_lines}" if shap_lines else ""

    sp_lines = []
    h_name = d.get("home_sp_name") or home
    a_name = d.get("away_sp_name") or away
    if home_sp:
        sp_lines.append(
            f"  {h_name} ({home_sp.get('hand','RHP')}): "
            f"ERA {home_sp.get('era','?')}  WHIP {home_sp.get('whip','?')}  "
            f"K% {home_sp.get('k_rate','?')}  {home_sp.get('rest','?')}d rest"
        )
    if away_sp:
        sp_lines.append(
            f"  {a_name} ({away_sp.get('hand','RHP')}): "
            f"ERA {away_sp.get('era','?')}  WHIP {away_sp.get('whip','?')}  "
            f"K% {away_sp.get('k_rate','?')}  {away_sp.get('rest','?')}d rest"
        )
    sp_block = ("Starting pitchers:\n" + "\n".join(sp_lines)) if sp_lines else ""

    uf_parts = []
    if uf.get("score") is not None:
        uf_parts.append(f"Chaos/upset score: {uf['score']}/10")
    if uf.get("confidence_reduction"):
        uf_parts.append(
            f"confidence reduced {round(uf['confidence_reduction']*100)}pp, "
            f"stake −{round(uf.get('kelly_reduction', 0)*100)}%"
        )
    uf_block = " · ".join(uf_parts)

    bd, bu = d.get("bet_dollars") or 0, d.get("bet_units") or 0
    kelly_block = f"Recommended stake: ${bd:.0f} ({bu:.1f}U)" if bd and bd > 0 else ""

    sections = [s for s in [shap_block, sp_block, uf_block, kelly_block] if s]

    prompt = (
        f"Analyze this betting pick and give your expert opinion in 3–4 sentences. "
        f"Cover: why the model favors this side, the key factors driving the edge, "
        f"the main risk, and your own independent assessment of this pick. "
        f"Be specific and direct. Do not use bullet points or headers. "
        f"Do not repeat the raw numbers verbatim — synthesize them into insight. "
        f"End with exactly one line formatted as: "
        f"ANALYST VERDICT: followed by one of these three options: "
        f"'Agree with model', 'Disagree — my pick is [team/side]', or 'Lean with caution'.\n\n"
        f"Game: {away} @ {home}\n"
        f"Pick: {pick_desc}\n"
        f"Model confidence: {conf_desc}\n"
        f"Edge vs market: {edge_str}\n"
    )
    if sections:
        prompt += "\n" + "\n".join(sections)

    return prompt.strip()


def _load_explain_cache() -> dict:
    if _EXPLAIN_CACHE_FILE.exists():
        try:
            with open(_EXPLAIN_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_explain_cache(cache: dict) -> None:
    _EXPLAIN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_EXPLAIN_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _prune_explain_cache(cache: dict) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    return {k: v for k, v in cache.items() if v.get("game_date", "") >= today}


@app.route("/api/explain_cache", methods=["GET"])
def get_explain_cache():
    """Return all non-stale cached explanations keyed by game_id:bet_type."""
    pruned = _prune_explain_cache(_load_explain_cache())
    return jsonify({k: v["explanation"] for k, v in pruned.items()})


@app.route("/api/explain_pick", methods=["POST"])
def explain_pick():
    data = request.get_json() or {}
    try:
        explanation = _call_analyst(_build_explain_prompt(data), max_tokens=600)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Parse "ANALYST VERDICT: …" line — marker is already uppercase so only
    # uppercase the source text once for the search.
    verdict      = ""
    analyst_pick = ""
    _VERDICT_MARKER = "ANALYST VERDICT:"
    vi = explanation.upper().find(_VERDICT_MARKER)
    if vi != -1:
        verdict_line = explanation[vi + len(_VERDICT_MARKER):].split("\n")[0].strip()
        verdict      = verdict_line
        if "disagree" in verdict_line.lower():
            after_is = verdict_line.lower().find(" is ")
            if after_is != -1:
                analyst_pick = verdict_line[after_is + 4:].strip()

    # Persist to disk — keyed by game_id:bet_type, stored with game date for pruning
    game_id   = data.get("game_id", "")
    bet_type  = data.get("bet_type", "ml")
    game_date = data.get("game_date", "")
    if game_id:
        cache = _prune_explain_cache(_load_explain_cache())
        cache[f"{game_id}:{bet_type}"] = {
            "explanation":  explanation,
            "verdict":      verdict,
            "analyst_pick": analyst_pick,
            "game_date":    game_date,
            "created_at":   datetime.now(timezone.utc).isoformat(),
        }
        _save_explain_cache(cache)

    return jsonify({"explanation": explanation, "verdict": verdict, "analyst_pick": analyst_pick})


def _build_breakdown_prompt(serialized: list) -> str:
    """Build the AI breakdown prompt from serialized game results."""
    if not serialized:
        return ""

    games_text = []
    for g in serialized[:14]:  # cap at 14 games
        away = g.get("away_team", "Away")
        home = g.get("home_team", "Home")

        # ML pick
        pick_team  = g.get("pick_team", "")
        pick_odds  = g.get("pick_odds")
        odds_str   = _format_odds(pick_odds)
        ml_conf    = g.get("ml_confidence") or g.get("xgb_prob") or 0
        edge       = g.get("pick_edge") or 0
        conflict   = g.get("conflict", False)

        # Run line
        rl_pick   = g.get("run_line_pick_team", "")
        rl_point  = g.get("run_line_point", -1.5)

        # Totals
        total_dir  = (g.get("direction") or "").upper()
        total_line = g.get("total_line", "")
        pred_total = g.get("predicted_total", "")

        # Starting pitchers
        h_sp_name = g.get("home_sp_name", "")
        a_sp_name = g.get("away_sp_name", "")
        h_sp      = g.get("home_sp") or {}
        a_sp      = g.get("away_sp") or {}

        # Upset factor
        uf_score = (g.get("upset_factor") or {}).get("score", "n/a")

        lines = [f"Game: {away} @ {home}"]
        if conflict:
            lines.append("ML: SKIP — models conflict")
        else:
            lines.append(
                f"ML pick: {pick_team} {odds_str} | "
                f"Confidence: {ml_conf * 100:.1f}% | Edge: {edge * 100:+.1f}%"
            )
        if rl_pick:
            rl_side = "home" if g.get("run_line_side") == "home" else "away"
            pt_str  = f"{rl_point:+.1f}" if rl_side == "home" else f"{-rl_point:+.1f}"
            lines.append(f"Run line: {rl_pick} {pt_str}")
        if total_dir and total_line:
            lines.append(
                f"Totals: {total_dir} {total_line}"
                + (f" (model pred: {pred_total})" if pred_total else "")
            )
        sp_parts = []
        if a_sp_name:
            sp_parts.append(f"{a_sp_name} ERA {a_sp.get('era', '?')} WHIP {a_sp.get('whip', '?')}")
        if h_sp_name:
            sp_parts.append(f"{h_sp_name} ERA {h_sp.get('era', '?')} WHIP {h_sp.get('whip', '?')}")
        if sp_parts:
            lines.append("SPs: " + " vs ".join(sp_parts))
        lines.append(f"Chaos/upset factor: {uf_score}/10")

        games_text.append("\n".join(lines))

    all_games = "\n\n".join(games_text)

    return (
        f"Here is today's MLB slate with model predictions. Provide:\n"
        f"1. A brief 2-sentence analysis for each game\n"
        f"2. Your top 3-5 best bet recommendations across all games\n"
        f"3. One strong 2-team parlay and one 3-team parlay\n\n"
        f"Today's games:\n{all_games}\n\n"
        f"Respond ONLY with valid JSON (no markdown fences, no extra text):\n"
        f'{{"games":[{{"matchup":"Away @ Home","analysis":"2 sentence analysis"}}],'
        f'"best_bets":[{{"pick":"Team ML / Over X / Team RL","reason":"Why this is top value"}}],'
        f'"parlays":{{"2-team":[{{"legs":["Pick 1","Pick 2"],"note":"Why they pair well"}}],'
        f'"3-team":[{{"legs":["Pick 1","Pick 2","Pick 3"],"note":"Why this parlay works"}}]}}}}'
    )


@app.route("/api/ai/breakdown", methods=["POST"])
def ai_breakdown():
    """Generate a full AI analyst breakdown for today's slate."""
    results  = _analysis_state.get("results", [])
    bankroll = float(_analysis_state.get("bankroll", 250))
    sport    = _analysis_state.get("sport", "mlb")

    if not results:
        return jsonify({"error": "No analysis data available. Run analysis first."}), 400

    # Serialize results for the prompt (same format the frontend uses)
    try:
        ledger     = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        s_bankroll = ledger.data.get("personal_starting_bankroll", bankroll)
        serialized = [_serialize(r, bankroll, sport, s_bankroll) for r in results]
    except Exception:
        serialized = [
            {"away_team": r.get("game", {}).get("away_team", ""),
             "home_team": r.get("game", {}).get("home_team", "")}
            for r in results
        ]

    prompt = _build_breakdown_prompt(serialized)
    if not prompt:
        return jsonify({"error": "Could not build analysis prompt."}), 400

    try:
        raw_text = _call_analyst(prompt, max_tokens=2000)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # Parse JSON; strip accidental markdown fences first
    try:
        parsed = json.loads(_strip_markdown_fences(raw_text))
    except Exception:
        return jsonify({"raw": raw_text, "games": [], "best_bets": [], "parlays": {}})

    # Cache to disk (best-effort — data/ already exists from analysis run)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        _AI_BREAKDOWN_CACHE_FILE.write_text(
            json.dumps({"date": today, "data": parsed}, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    return jsonify(parsed)


# ── WNBA analysis endpoint ────────────────────────────────────────────────────

@app.route("/api/wnba/analyze", methods=["POST"])
def analyze_wnba():
    """Full WNBA analysis pipeline: team stats + odds + ensemble predictions."""
    data       = request.get_json() or {}
    bankroll   = float(data.get("bankroll", _wnba_analysis_state.get("bankroll", 1000)))
    season     = int(data.get("season", 2025))
    force_refresh = bool(data.get("force_refresh", False))
    use_cached    = bool(data.get("use_cached", False))

    odds_key   = os.getenv("ODDS_API_KEY", "")
    sports_key = os.getenv("API_SPORTS_KEY", "")  # optional for WNBA (ESPN used instead)

    if not odds_key or odds_key == "your_odds_api_key_here":
        return jsonify({"error": "ODDS_API_KEY not configured in .env"}), 400

    # Cache control
    _last     = _wnba_analysis_state.get("last_analyzed_at")
    _has_res  = bool(_wnba_analysis_state.get("results"))
    if (not force_refresh and _has_res and (
            use_cached or (
                _last is not None and
                (datetime.now(timezone.utc) - _last).total_seconds() < _ANALYSIS_TTL
            )
    )):
        wnba_ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
        s_br  = wnba_ledger.data.get("personal_starting_bankroll", bankroll)
        serialized = [_serialize_wnba(r, bankroll, s_br)
                      for r in _wnba_analysis_state["results"]]
        parlays = _generate_parlays(serialized, bankroll)
        _wnba_analysis_state["parlays"]  = parlays
        _wnba_analysis_state["bankroll"] = bankroll
        meta = _wnba_analysis_state.get("last_analysis_meta", {})
        return jsonify({
            "success": True, "cached": True, "sport": "wnba", "bankroll": bankroll,
            "games_loaded":   meta.get("games_loaded", 0),
            "model_status":   meta.get("model_status", ""),
            "cv_accuracy":    meta.get("cv_accuracy"),
            "lr_cv_accuracy": meta.get("lr_cv_accuracy"),
            "results": serialized, "parlays": parlays,
        })

    # Auto-settle any completed WNBA bets first
    try:
        _oc_settle = OddsClient(odds_key, _cache)
        _wl = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
        _wl.settle(_oc_settle, "basketball_wnba")
    except Exception:
        pass

    try:
        from src.sports_config import WNBA
        wnba_cfg = WNBA

        # Step 1 — load WNBA season data from ESPN free API
        wnba_client = WNBAStatsClient(api_key=sports_key, cache=_cache)
        n_completed = wnba_client.load(season)

        # Step 2 — feature builder
        fb = WNBAFeatureBuilder(wnba_client)

        # Step 2b — college-performance adjustments for rookies / 2nd-year players
        #   Fetches ESPN WNBA rosters + sportsdataverse WBB stats; cached 24 h.
        #   Results injected into fb so build_for_game() populates college_adj_diff.
        try:
            college_client = WNBACollegeClient(cache=_cache)
            all_team_ids = wnba_client.all_team_ids()
            if all_team_ids:
                college_adjs = college_client.get_college_adjustments(all_team_ids, season)
                college_diag = {tid: college_client.get_diagnostics(tid) for tid in all_team_ids}
                fb.set_college_adjustments(college_adjs, college_diag)
                # Print diagnostic summary for any team with non-zero adjustment
                n_adjusted = sum(1 for a in college_adjs.values() if abs(a) > 0.01)
                if n_adjusted:
                    print(f"  [college] {n_adjusted} team(s) with college adjustments applied:")
                    for tid, adj in sorted(college_adjs.items(), key=lambda x: abs(x[1]), reverse=True):
                        if abs(adj) > 0.01:
                            diag_rows = college_diag.get(tid, [])
                            found_players = [d for d in diag_rows if d.get("found")]
                            print(f"    team_id={tid} adj={adj:+.3f} ({len(found_players)} young players with college data)")
                            for d in found_players:
                                print(f"      {d['name']} ({d['exp_years']}yr) "
                                      f"college={d['college'] or 'N/A'}  "
                                      f"ppg={d['ppg']:.1f} fg%={d['fg_pct']:.3f} "
                                      f"rpg={d['rpg']:.1f} apg={d['apg']:.1f}  "
                                      f"adj={d['adj']:+.3f}")
        except Exception as _college_err:
            print(f"  [college] College adjustment skipped: {_college_err}")

        # Step 3 — models
        ml_model = BettingModel(wnba_cfg)
        status   = ml_model.train_or_load(
            stats_client=wnba_client, feature_builder=fb,
            season=season, force_retrain=False,
        )
        cv_acc    = float(ml_model.cv_accuracy)     if ml_model.cv_accuracy    else None
        lr_cv_acc = float(ml_model.lr_cv_accuracy)  if ml_model.lr_cv_accuracy else None

        spread_model = WNBASpreadModel()
        sp_status = spread_model.train_or_load(wnba_client, fb, season)
        print(f"  {sp_status}")

        totals_model = WNBATotalsModel()
        tot_status = totals_model.train_or_load(wnba_client, fb, season)
        print(f"  {tot_status}")

        # Step 4 — odds from The Odds API
        odds_client = OddsClient(odds_key, _cache)
        games       = odds_client.get_nfl_odds(sport_key="basketball_wnba")
        games       = _lock_in_pre_game_odds(games)

        if not games:
            return jsonify({
                "success": True, "no_games": True, "results": [],
                "model_status": status, "cv_accuracy": cv_acc,
                "lr_cv_accuracy": lr_cv_acc, "games_loaded": n_completed,
                "sport": "wnba", "bankroll": bankroll,
            })

        # Step 5 — predict
        results = []
        for game in games:
            built = fb.build_for_game(game)
            if built is None:
                continue
            feature_vec, meta = built

            prediction   = ml_model.predict(feature_vec)
            spread_pred  = spread_model.predict(feature_vec, game) if spread_model.is_trained else None
            totals_vec   = fb.build_totals_from_meta(meta)
            totals_pred  = None
            if totals_model.is_trained and game.get("total_line") is not None and totals_vec is not None:
                totals_pred = totals_model.predict(totals_vec, game)

            results.append({
                "game":        game,
                "prediction":  prediction,
                "meta":        meta,
                "spread_pred": spread_pred,
                "totals_pred": totals_pred,
            })

        _wnba_analysis_state["results"]  = results
        _wnba_analysis_state["bankroll"] = bankroll

        # Step 6 — cross-sport daily picks selection (top-5 per category, Half Kelly)
        _run_daily_picks_selection()

        # Reload wnba ledger to get current personal_starting_bankroll for serialization
        _wledger_serial = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
        s_br            = _wledger_serial.data.get("personal_starting_bankroll", bankroll)

        serialized = [_serialize_wnba(r, bankroll, s_br) for r in results]
        parlays    = _generate_parlays(serialized, bankroll)
        _wnba_analysis_state["parlays"]            = parlays
        _wnba_analysis_state["last_analyzed_at"]   = datetime.now(timezone.utc)
        _wnba_analysis_state["last_analysis_meta"] = {
            "games_loaded":  n_completed,
            "model_status":  status,
            "cv_accuracy":   cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
        }
        _save_wnba_analysis_cache(serialized, parlays, n_completed, cv_acc, lr_cv_acc)

        return jsonify({
            "success":        True,
            "cached":         False,
            "sport":          "wnba",
            "season":         season,
            "bankroll":       bankroll,
            "games_loaded":   n_completed,
            "model_status":   status,
            "cv_accuracy":    cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
            "results":        serialized,
            "parlays":        parlays,
        })

    except Exception as exc:
        return jsonify({"error": str(exc), "detail": traceback.format_exc()}), 500


@app.route("/api/wnba/init", methods=["GET"])
def init_wnba():
    """Return today's cached WNBA analysis for auto-load on startup."""
    try:
        if not _WNBA_ANALYSIS_CACHE_FILE.exists():
            return jsonify({"has_predictions": False})
        payload = json.loads(_WNBA_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
        today   = datetime.now(timezone.utc).date().isoformat()
        if payload.get("date") != today:
            return jsonify({"has_predictions": False})
        return jsonify({
            "has_predictions": True,
            "analyzed_at":     payload.get("analyzed_at"),
            "sport":           "wnba",
            "games_loaded":    payload.get("games_loaded", 0),
            "cv_accuracy":     payload.get("cv_accuracy"),
            "lr_cv_accuracy":  payload.get("lr_cv_accuracy"),
            "results":         payload.get("results", []),
            "parlays":         payload.get("parlays", {}),
        })
    except Exception:
        return jsonify({"has_predictions": False})


@app.route("/api/wnba/ledger", methods=["GET"])
def get_wnba_ledger():
    """Return WNBA ledger summary, open bets, and history."""
    bankroll = float(request.args.get("bankroll", _wnba_analysis_state.get("bankroll") or 1000))
    ledger   = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)

    settled: list = []
    odds_key = os.getenv("ODDS_API_KEY", "")
    if odds_key and odds_key != "your_odds_api_key_here":
        try:
            oc      = OddsClient(odds_key, _cache)
            settled = ledger.settle(oc, "basketball_wnba")
        except Exception:
            pass

    summary = ledger.get_summary()

    _full_hist = ledger.data["history"]
    def _wnba_type_rec(hist):
        out = {}
        for bt in ("single", "spread", "totals"):
            sub = [h for h in hist if h.get("bet_type", "single") == bt]
            out[bt] = [
                sum(1 for h in sub if h["result"] == "win"),
                sum(1 for h in sub if h["result"] == "loss"),
            ]
        return out

    return jsonify({
        "summary":      _py(summary),
        "open_bets":    _py(ledger.data["open_bets"]),
        "history":      _py(ledger.data["history"][-30:]),
        "settled_now":  _py(settled),
        "type_records": {"model": _wnba_type_rec(_full_hist)},
    })


@app.route("/api/wnba/ledger/set_bankroll", methods=["POST"])
def set_wnba_bankroll():
    """Update ONLY the personal bankroll on the WNBA ledger.
    Never touches model_bankroll or model_starting_bankroll."""
    body   = request.get_json(force=True) or {}
    new_br = float(body.get("bankroll", 0))
    if new_br <= 0:
        return jsonify({"error": "Bankroll must be greater than 0"}), 400
    ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
    # Snapshot model fields — MUST be preserved no matter what state the file is in
    saved_model_bankroll = ledger.data.get("model_bankroll",          1000.0)
    saved_model_starting = ledger.data.get("model_starting_bankroll", 1000.0)
    # Update only personal fields
    ledger.data["personal_starting_bankroll"] = new_br
    ledger.data["personal_bankroll"]          = new_br
    # Explicitly restore model fields (bulletproof guarantee)
    ledger.data["model_bankroll"]          = saved_model_bankroll
    ledger.data["model_starting_bankroll"] = saved_model_starting
    ledger.save()
    _wnba_analysis_state["bankroll"] = new_br
    return jsonify({"success": True, "bankroll": new_br})


@app.route("/api/wnba/ledger/settle_manual/<bet_id>", methods=["POST"])
def settle_wnba_manual(bet_id: str):
    data    = request.get_json() or {}
    result  = data.get("result", "").lower()
    if result not in ("win", "loss", "push"):
        return jsonify({"error": "result must be win, loss, or push"}), 400
    bankroll = float(data.get("bankroll", _wnba_analysis_state.get("bankroll") or 1000))
    ledger   = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
    settled  = ledger.settle_manual(bet_id, result)
    if settled is None:
        return jsonify({"error": "Bet not found"}), 404
    return jsonify({"success": True, "settled": _py(settled)})


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
