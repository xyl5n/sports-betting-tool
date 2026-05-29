"""state.py -- shared mutable state, caches, locks, and config constants.

All module-level definitions in this file were lifted out of app.py
unchanged.  They live here so the same dict / lock / cache instance is
visible to every importer (`from state import *`) without app.py
needing to be the source of truth for its own state.

DO NOT add functions or logic to this file -- it is a data module by
contract.  Side-effectful helpers stay in app.py.

Reassignment caveat: because consumers use `from state import *`, any
`name = ...` at module scope here is captured by reference once at
import time.  Mutate the object (dict[key] = x, list.append, lock
methods); never rebind the name in another module.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

# Aliases preserve the `import threading as _X` names that app.py used at the
# original definition sites; the lock initializers below reference these.
_threading = threading
_threading_for_analyze = threading

# Cache instance is constructed by src.cache; importing here lets state.py
# own the singleton without app.py touching it.
from src.cache import Cache

# Explicit re-export list: `from state import *` skips underscore-prefixed
# names by default, but every moved item starts with one underscore (they
# were "module-private" in app.py).  Listing them in __all__ overrides that
# rule so consumers see exactly the names that used to live in app.py.
__all__ = [
    "_LOG_LEVEL", "_ODDS_API_KEY", "_API_SPORTS_KEY", "_ANTHROPIC_API_KEY",
    "_SEASON", "_cache", "_ANALYSIS_CACHE_FILE", "_WNBA_ANALYSIS_CACHE_FILE",
    "_PRE_GAME_ODDS_FILE", "_EXPLAIN_CACHE_FILE", "_AI_BREAKDOWN_CACHE_FILE",
    "_ANALYSIS_TIMESTAMPS_FILE", "_snapshot_lock", "_SNAPSHOT_ENABLED",
    "_CACHE_KEY_SNAPSHOT", "_CACHE_KEY_ANALYSIS_MLB", "_CACHE_KEY_ANALYSIS_WNBA",
    "EV_MIN_EDGE", "_analysis_state", "_wnba_analysis_state",
    "_auto_analysis_lock", "_auto_analysis_state", "_AUTO_ANALYSIS_LOG_FILE",
    "_MODEL_SETTINGS_FILE", "_MODEL_SETTINGS_DEFAULT", "_auto_settlement_lock",
    "_refresh_cycle_lock", "_FEATURE_LABELS", "_ODDS_FIELDS",
    "_MLB_STATS_BASE", "_LINESCORE_TTL", "_ESPN_WNBA_BASE",
    "_QUARTER_ORDINAL", "_DEBUG_LOG", "_analysis_progress_lock",
    "_PICKS_HISTORY_FILES", "_ENSEMBLE_PICKS_FILE", "_BET_HISTORY_ARCHIVE",
    "_MLB_TEAM_NORM", "_MODEL_PICK_STAT", "_SETTLE_GAMELOG_TTL",
    "_STATSAPI_BRIDGE_TTL", "_AI_RUN_DELAY",
    "_DAILY_SNAPSHOT_FILE", "_DAILY_SNAPSHOT_TMP",
    "_STATSAPI_BRIDGE_CACHE",
    "_auto_settlement_state", "_SETTLE_GAMELOG_MEMO",
    "_DAILY_PICKS_FILE",
]

# moved from app.py:138
# NOTE: _validate_sharpapi_key_on_boot + _probe_sharpapi_leagues_on_boot
# used to live here.  Both removed because:
#   - SharpAPI is no longer used as a fallback (odds_client.OddsClient now
#     treats The Odds API as the sole source -- see PR 'remove sharpapi
#     fallback')
#   - The startup probe + cred-check were adding network latency and log
#     noise without adding value
# SHARPAPI_KEY is left in env / .env.example in case we re-enable later;
# no code touches it on this code path.


# NOTE: _bust_daily_odds_cache_on_boot used to live here and was tied to
# the old "1 Odds API call per sport per day" Supabase cache (see PR #37
# and PR #40).  The quota model has moved to a per-day request counter
# (see src/odds_client._odds_check_limit) with a 500-call ceiling, so
# the daily-cache + boot-bust combo is no longer relevant.  Removing
# the bust + the cache layer in one swoop.

# ── Logging ───────────────────────────────────────────────────────────────────
# LOG_LEVEL controls verbosity for Railway (set in Railway environment vars):
#   WARNING  — only errors/warnings printed; safe for Railway's 500-line/sec cap (default)
#   INFO     — adds one summary line per analysis run ("MLB analysis complete: N games")
#   DEBUG    — full print() output restored; for local development only
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()

# moved from app.py:153
# ── External API credentials & runtime config ────────────────────────────────
# Read every env var once at boot instead of calling os.getenv on every
# request.  Route handlers reference these constants directly; if Railway
# rotates a key the process restarts and picks up the new value.  Keep the
# existing "" / 2025 defaults so behavior matches the prior per-site reads.
_ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# moved from app.py:154
_API_SPORTS_KEY = os.environ.get("API_SPORTS_KEY", "")

# moved from app.py:155
_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# moved from app.py:156
_SEASON = int(os.environ.get("SEASON", "2025"))

# moved from app.py:305
# ── Global state (single-user desktop app) ────────────────────────────────────
_cache = Cache()

# moved from app.py:307
_ANALYSIS_CACHE_FILE      = Path("data/analysis_cache.json")

# moved from app.py:308
_WNBA_ANALYSIS_CACHE_FILE = Path("data/wnba_analysis_cache.json")

# moved from app.py:309
_PRE_GAME_ODDS_FILE       = Path("data/pre_game_odds.json")

# moved from app.py:310
_EXPLAIN_CACHE_FILE       = Path("data/explain_cache.json")

# moved from app.py:311
_AI_BREAKDOWN_CACHE_FILE  = Path("data/ai_breakdown_cache.json")

# moved from app.py:315
# Lightweight timestamp file — survives container restarts without reading the
# full results payloads.  Shape: {"mlb": {"analyzed_at": "<iso>", "date": "YYYY-MM-DD"}, "wnba": {...}}
_ANALYSIS_TIMESTAMPS_FILE = Path("data/analysis_timestamps.json")

# moved from app.py:321
_snapshot_lock = _threading.Lock()

# moved from app.py:324
# Step 3: master kill-switch.  Set env var SNAPSHOT_ENABLED=0 to bypass entirely.
_SNAPSHOT_ENABLED = os.environ.get("SNAPSHOT_ENABLED", "1").strip() not in ("0", "false", "False", "FALSE")

# moved from app.py:340
# ─────────────────────────────────────────────────────────────────────────────
#  Step 4: persistent-cache layer.  Snapshot + analysis caches mirror to
#  Supabase (table `app_cache`, see src/db.py) so they survive Railway
#  container restarts and redeployments.  Local files remain the primary
#  read surface; this layer is the persistence sidecar.
#
#  Wrappers below tolerate every failure mode (Supabase off, table missing,
#  network error) silently so file-based ops keep working when Supabase is
#  unavailable.
# ─────────────────────────────────────────────────────────────────────────────

# Keys used in the app_cache table.  Single source of truth so write +
# restore + delete all agree.
_CACHE_KEY_SNAPSHOT     = "daily_snapshot"

# moved from app.py:341
_CACHE_KEY_ANALYSIS_MLB  = "analysis_cache:mlb"

# moved from app.py:342
_CACHE_KEY_ANALYSIS_WNBA = "analysis_cache:wnba"

# moved from app.py:1213
# ── EV / value-pick threshold ──────────────────────────────────────────────────
# Minimum pick_edge for a game to receive value_pick=True in _serialize()
# and to appear in the EV Scan section on the home page.  Exposed as a
# module-level constant so the display label always stays in sync with the
# actual gate, and the threshold can be tuned from one place without a
# grep-and-replace across multiple files.
EV_MIN_EDGE: float = 0.03

# moved from app.py:1215
_analysis_state: dict = {
    "sport":              None,
    "bankroll":           250.0,
    "results":            [],   # raw result dicts (game, prediction, shap, meta)
    "parlays":            {},
    "last_analyzed_at":   None, # datetime (UTC) of last full run
    "last_analysis_meta": {},   # games_loaded, cv/lr/nn accuracy, model_status
}

# moved from app.py:1224
_wnba_analysis_state: dict = {
    "sport":              "wnba",
    "bankroll":           1000.0,
    "results":            [],
    "parlays":            {},
    "last_analyzed_at":   None,
    "last_analysis_meta": {},
}

# moved from app.py:1234
# ── Auto-analysis scheduler state ─────────────────────────────────────────────
_auto_analysis_lock  = threading.Lock()

# moved from app.py:1235
_auto_analysis_state: dict = {
    "last_label":    None,
    "last_started":  None,
    "last_finished": None,
    "last_duration": None,
    "last_status":   None,   # "success" | "partial" | "error" | None
    "last_results":  {},     # {"MLB": {...}, "WNBA": {...}}
}

# moved from app.py:1243
_AUTO_ANALYSIS_LOG_FILE = Path("data/auto_analysis_log.json")

# moved from app.py:1249
# ── Model-bets settings (per-sport toggle for auto-pick) ─────────────────────
# The Admin sub-page exposes a switch per sport so the user can disable a
# sport from the model's auto-pick pool.  Default: MLB on, WNBA off.  Persisted
# as a tiny JSON file so the choice survives restarts.
_MODEL_SETTINGS_FILE = Path("data/model_settings.json")

# moved from app.py:1250
_MODEL_SETTINGS_DEFAULT = {
    "mlb_enabled":         True,
    "wnba_enabled":        False,
    # Home-page top-bar "overall win rate" chip toggle.  When False the
    # chip is hidden and the two remaining chips (best model + best bet
    # type) stretch to fill the row.  See pages/home.py + pages/admin.py.
    "show_overall_chip":   True,
    # Per-day cap on /api/ai/chat Anthropic calls.  Counted in Supabase
    # app_cache under key "ai_calls:<YYYY-MM-DD ET>".  When the count
    # hits this number, the chat endpoint returns 429 and the UI
    # disables Send.  Stored as int -- the save path below preserves
    # int type for any default that is non-bool.
    "ai_daily_limit":      20,
}

# moved from app.py:1326
# ── Auto-settlement scheduler state ───────────────────────────────────────────
_auto_settlement_lock  = threading.Lock()

# moved from app.py:1338
# ── Consolidated 15-minute refresh-cycle state ────────────────────────────────
# The auto_props_refresh job now runs one coordinated pass (schedule+scores →
# game odds → prop lines → re-score → settlement → AI summaries).
_refresh_cycle_lock  = threading.Lock()   # non-blocking guard against overlap

# moved from app.py:1360
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

# moved from app.py:1409
# ── Pre-game odds lock ────────────────────────────────────────────────────────
# Odds fields that get snapshotted before first pitch and restored for in-progress games.
_ODDS_FIELDS = (
    "h2h_home_odds", "h2h_away_odds",
    "home_implied_prob", "away_implied_prob",
    "run_line_home_odds", "run_line_away_odds", "run_line_point", "spread",
    "over_odds", "under_odds", "total_line",
)

# moved from app.py:2557
# ── MLB Stats API proxy ────────────────────────────────────────────────────────
# Fetches statsapi.mlb.com server-side so the browser never makes a cross-origin
# request.  QWebEngineView's CORS policy can silently block direct external fetches
# from an HTTP localhost origin; routing through Flask eliminates that entirely.
#
# Routes:
#   /api/mlb/schedule?date=YYYY-MM-DD              → schedule (1-hour cache)
#   /api/mlb/schedule?date=YYYY-MM-DD&hydrate=linescore → live scores (30-sec cache)

_MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"

# moved from app.py:2560
_LINESCORE_TTL = 30   # seconds — live scores refresh this often

# moved from app.py:2637
# ── WNBA schedule + live scores proxy ──────────────────────────────────────────
# Mirrors the MLB endpoint above but talks to ESPN's public scoreboard
#   https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard
# which exposes live state (status.type.state ∈ {pre, in, post}), the current
# period (1-4 for regulation, 5+ for overtime), a displayClock string, and
# per-team scores.  stats.wnba.com would work too but is bot-protected and
# rate-limits aggressively — ESPN is reliable and unauthenticated.
#
# The response is reshaped to mirror the MLB Stats API structure
#   { dates: [{ games: [{ gamePk, teams.{home,away}.team.name,
#                         status.abstractGameState, linescore.* }] }] }
# so the frontend can reuse the same _applyLiveMap / _findLiveByTeamName logic
# (with a thin WNBA-flavoured wrapper for the period / quarter labelling).

_ESPN_WNBA_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba"

# moved from app.py:2640
_QUARTER_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}

# moved from app.py:3662
# ── Live-score debug system ────────────────────────────────────────────────────
# Writes to stdout AND data/debug_live.log so output is readable whether
# the user runs via 'python desktop.pyw' (terminal) or via launch.bat (log file).

_DEBUG_LOG = Path("data/debug_live.log")

# moved from app.py:4068
_analysis_progress_lock = _threading_for_analyze.Lock()

# moved from app.py:5903
_PICKS_HISTORY_FILES = (
    Path(".cache/xgb_picks_history.json"),
    Path(".cache/lr_picks_history.json"),
    Path("data/nn_picks_history.json"),
    Path(".cache/props_picks_history.json"),
)

# moved from app.py:5909
_ENSEMBLE_PICKS_FILE = Path("data/ensemble_picks_today.json")

# moved from app.py:5911
_BET_HISTORY_ARCHIVE = Path("data/bet_history_archive.json")
# moved from app.py:288
_DAILY_SNAPSHOT_FILE      = Path("data/daily_snapshot.json")
# moved from app.py:289
_DAILY_SNAPSHOT_TMP       = Path("data/daily_snapshot.json.tmp")


# moved from app.py:10435
# ── Auto-settlement helpers ───────────────────────────────────────────────────
_MLB_TEAM_NORM = {
    "Oakland Athletics": "Athletics",
    "Arizona Diamondbacks": "Diamondbacks",
    "Tampa Bay Rays": "Rays",
}

# moved from app.py:10751
_MODEL_PICK_STAT = {
    "pitcher_strikeouts": "K", "pitcher_earned_runs": "ER",
    "pitcher_hits_allowed": "H", "pitcher_walks": "BB", "pitcher_outs": "outs",
    "batter_hits": "H", "batter_total_bases": "TB", "batter_home_runs": "HR",
    "batter_rbis": "RBI", "batter_runs_scored": "R", "batter_walks": "BB",
    "batter_strikeouts": "SO",
}

# moved from app.py:10765
_SETTLE_GAMELOG_TTL = 120.0

# moved from app.py:10860
_STATSAPI_BRIDGE_TTL = 3600.0         # 1 hour -- avoids re-fetching a date's

# moved from app.py:12125
_AI_RUN_DELAY = 0.15   # 150 ms between Groq calls (free-tier friendly)

# moved from app.py:10239
_STATSAPI_BRIDGE_CACHE: dict = {}     # et_date_iso -> (ts, {norm_team: game_info})

# moved from app.py:1078
_auto_settlement_state: dict = {
    "last_ran_at":  None,   # ISO UTC
    "last_settled": 0,
    "last_wins":    0,
    "last_losses":  0,
    "last_voided":  0,
}

# moved from app.py:9917
# Per-pass gamelog memo for settlement: (player_id, is_pitcher) -> (ts, games).
# Settlement force-refreshes gamelogs (see below); a pitcher with three pending
# prop markets would otherwise fire three identical statsapi calls in one pass.
# Short TTL so a later cycle (15 min on) still picks up newly-finished games.
_SETTLE_GAMELOG_MEMO: dict = {}

# moved from app.py:5403
_DAILY_PICKS_FILE    = Path("data/daily_picks.json")
