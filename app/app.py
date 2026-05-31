"""
Flask backend for the Sports Betting Analysis desktop app.
All existing src/ modules are reused unchanged — only the display layer changes
from Rich terminal output to JSON served to the PyWebView browser frontend.
"""
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.request as _urlreq
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Shared state: every mutable global, lock, cache singleton, and config
# constant lives in state.py.  Star-imported here so legacy bare-name
# references in this file keep working without per-site rewrites.
from state import *  # noqa: F401,F403

# Pure helpers (date/odds math, formatters, normalizers).  Star-imported
# so legacy bare-name references throughout this file keep working.
from utils import *  # noqa: F401,F403

# Scheduler-owned helpers (currently: _eprint + 2 small job functions).
# Star-imported so legacy bare-name references throughout this file keep
# working without per-site rewrites.  See scheduler.py header for why
# the other 7 jobs and the APScheduler bootstrap block still live here.
from scheduler import *  # noqa: F401,F403

# Serialization layer (analysis-result -> UI dict).  Star-imported so the
# Flask routes + _build_chat_context + _rerun_single_game keep working
# without per-site rewrites.  Pure data-shaping, no Flask coupling.
from serializer import *  # noqa: F401,F403

# AI prompt builders + analyst-call helpers (Phase A).  Star-imported so
# the Flask routes that build prompts keep working without per-site rewrites.
from ai_prompts import *  # noqa: F401,F403

# Parlay generation (Phase B).  Star-imported so the analyze route's
# parlay step keeps working without per-site rewrites.
from parlay import *  # noqa: F401,F403

# No-odds game prediction (Phase C).  Star-imported so the analyze +
# prefetch paths keep working without per-site rewrites.
from predictor import *  # noqa: F401,F403

# Boot-sequence helpers (Phase D).  Star-imported so the module-scope
# boot calls below keep resolving without per-site rewrites.  Definitions
# live in boot.py; the CALLS remain here at their original positions.
from boot import *  # noqa: F401,F403

print("STARTUP [1/6]: stdlib imports OK", flush=True, file=sys.stderr)

try:
    import numpy as np
    print("STARTUP [2/6]: numpy OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: numpy import failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except Exception as _e:
    print(f"STARTUP WARNING: python-dotenv not available: {_e}", flush=True, file=sys.stderr)
    load_dotenv = lambda: None  # noqa: E731

try:
    from flask import Flask, jsonify, render_template, request
    print("STARTUP [3/6]: Flask OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: Flask import failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

load_dotenv()
print("STARTUP: env loaded", flush=True, file=sys.stderr)




_validate_odds_api_key_on_boot()


logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.WARNING),
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
_logger = logging.getLogger("sports_betting")




class _StdoutToLogger:
    """Route every print() call through the logger at DEBUG level.

    A single redirect here silences all print() calls across app.py and every
    src/ module without touching those files.  Railway operators control
    verbosity via the LOG_LEVEL environment variable (default WARNING).
    """
    def write(self, msg: str) -> None:
        msg = msg.rstrip()
        if msg:
            _logger.debug("%s", msg)

    def flush(self) -> None:
        pass


sys.stdout = _StdoutToLogger()

sys.path.insert(0, str(Path(__file__).parent))

# ── src/ imports — each wrapped individually so one missing module can't kill  ──
# the entire startup.  Failures print to stderr (visible in Railway deploy logs)
# and fall back to a safe stub so Flask can still start and serve health checks.

print("STARTUP [4/6]: loading src/ modules...", flush=True, file=sys.stderr)

try:
    from src.cache import Cache
    print("STARTUP:   src.cache OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.cache failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)   # Cache is used everywhere; can't continue without it

try:
    from src.daily_picks import select_daily_picks, load_daily_picks
    print("STARTUP:   src.daily_picks OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.daily_picks failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

# Credential redactor — never raises; falls back to str() if the module
# isn't importable (degenerate dev case).  Every traceback / error
# message that lands in a log file, stderr, or a JSON response is run
# through this so an HTTPError that embeds `?apiKey=...` in its message
# can't leak the key.  See src/redact.py for the rules.
try:
    from src.redact import redact as _redact
except Exception:                                                         # noqa: BLE001
    def _redact(s):                                                       # type: ignore[no-redef]
        return "" if s is None else str(s)

# ensemble_store — graceful stub if unavailable
class _EnsembleStoreStub:
    """No-op stub used when src.ensemble_store fails to import."""
    def save(self, picks, sport): pass
    def get_picks(self, sport=None): return {}
    def load(self): return {}

try:
    import src.ensemble_store as ensemble_store
    print("STARTUP:   src.ensemble_store OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP WARNING: src.ensemble_store failed ({_e}) — using stub",
          flush=True, file=sys.stderr)
    ensemble_store = _EnsembleStoreStub()  # type: ignore[assignment]

# news_feed + home_stats — optional helpers used only by the server-rendered
# /home-v2 home page (Phase 1 of the NiceGUI → HTML/JS/CSS migration).  Both
# are pure (NiceGUI-free); guarded so a failed import never takes down Flask.
try:
    import src.news_feed as news_feed
    print("STARTUP:   src.news_feed OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP WARNING: src.news_feed failed ({_e})", flush=True, file=sys.stderr)
    news_feed = None  # type: ignore[assignment]

try:
    from pages import home_stats as _home_stats
    print("STARTUP:   pages.home_stats OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP WARNING: pages.home_stats failed ({_e})", flush=True, file=sys.stderr)
    _home_stats = None  # type: ignore[assignment]

try:
    from src.game_store import GameStore
    print("STARTUP:   src.game_store OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.game_store failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

try:
    from src.kelly import size_bet, american_to_decimal, confidence_tier_from_prob
    print("STARTUP:   src.kelly OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.kelly failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

try:
    from src.ledger import Ledger
    print("STARTUP:   src.ledger OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.ledger failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)



try:
    from src.odds_client import OddsClient
    print("STARTUP:   src.odds_client OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.odds_client failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

try:
    from src.sports_config import SPORTS
    print("STARTUP:   src.sports_config OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.sports_config failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

try:
    from src.upset import UpsetCalculator
    print("STARTUP:   src.upset OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP FATAL: src.upset failed: {_e}", flush=True, file=sys.stderr)
    sys.exit(1)

print("STARTUP [5/6]: all src/ modules loaded", flush=True, file=sys.stderr)
# Confirm the persistence backend so cross-process settlement/display sync can
# be verified in the Railway logs (json mode => local-file-only, which does NOT
# survive across worker processes / redeploys).
try:
    from src import db as _db_startup
    print(f"SUPABASE STATUS: {_db_startup.is_supabase()}", flush=True, file=sys.stderr)
    print(f"SUPABASE STATUS detail: {_db_startup.status()}", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"SUPABASE STATUS: unknown ({type(_e).__name__}: {_e})",
          flush=True, file=sys.stderr)
# Heavy analysis packages (xgboost, sklearn, shap, anthropic) are imported lazily
# inside each route handler so Flask starts and passes its health check in < 2 s.

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
print("STARTUP [6/6]: Flask app created — registering routes...", flush=True, file=sys.stderr)

_ANALYSIS_TTL        = 900  # 15 minutes — skip API if last run was within this window

# Step 2: single lock so concurrent requests (init + analyze) never race on the file.
import threading as _threading













def _supabase_cache_set_sync(key: str, sport: str | None, date: str,
                             data: dict, *, timeout: float = 3.0) -> bool:
    """Synchronous Supabase cache write with a short timeout.  Used by the
    AI daily counter where we genuinely need confirmation the write landed
    before responding to the user.  Returns True iff cache_set returned a
    truthy value within `timeout` seconds; False on timeout, error, or
    when Supabase isn't connected."""
    import concurrent.futures
    def _do() -> bool:
        try:
            from src import db as _db
            return bool(_db.cache_set(key, sport, date, data))
        except Exception as exc:                                          # noqa: BLE001
            print(f"SUPABASE cache_set_sync({key}) failed: {exc}",
                  flush=True, file=sys.stderr)
            return False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_do).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        print(f"SUPABASE cache_set_sync({key}) timed out after {timeout}s",
              flush=True, file=sys.stderr)
        return False
    except Exception as exc:                                              # noqa: BLE001
        print(f"SUPABASE cache_set_sync({key}) outer error: {exc}",
              flush=True, file=sys.stderr)
        return False


# ── AI daily counter (used by /api/ai/chat + /api/ai/usage) ─────────────────
# Tracks the number of Anthropic /api/ai/chat calls made TODAY in ET.
# Persisted in Supabase app_cache so the counter survives Railway
# restarts.  Falls back to an in-process dict when Supabase is offline so
# the chat still works (counter just resets on container restart).





def _ai_get_daily_count() -> int:
    """Return today's Anthropic-call count.  Reads Supabase first; falls
    back to the in-process counter if Supabase returns nothing."""
    today = _today_et()
    try:
        row = _supabase_cache_get(_ai_daily_counter_key())
        if isinstance(row, dict) and isinstance(row.get("count"), int):
            return int(row["count"])
    except Exception:                                                     # noqa: BLE001
        pass
    return int(_ai_counter_mem.get(today, 0))


def _ai_increment_daily_count() -> int:
    """Read-modify-write the daily counter.  Increments by 1 and persists
    sync to Supabase (when configured) so the next call sees the bump
    immediately.  Always updates the in-process fallback regardless."""
    today = _today_et()
    new   = _ai_get_daily_count() + 1
    _ai_counter_mem[today] = new
    _supabase_cache_set_sync(
        _ai_daily_counter_key(), None, today, {"count": new},
    )
    return new


def _ai_daily_limit() -> int:
    """Return the configured per-day chat-call cap.  Bounded to a sane
    range so a typo in settings can't make the limit absurd."""
    try:
        v = int(_load_model_settings().get("ai_daily_limit", 20))
    except (TypeError, ValueError):
        v = 20
    return max(1, min(v, 500))








def _read_analysis_timestamps() -> dict:
    """Read the timestamps file; return {} on any error.

    Three-tier read so the admin "Last analyzed" line stays accurate
    across Railway redeploys (which wipe the local file but leave the
    Supabase mirror intact):

      1. Local data/analysis_timestamps.json (fast path)
      2. Supabase app_cache row keyed "analysis_timestamps" (durable)
      3. Empty dict (cold boot, never analyzed)

    Writes through to the local file on a Supabase hit so subsequent
    reads in this worker take the fast path.
    """
    # Tier 1 -- local file
    try:
        data = json.loads(_ANALYSIS_TIMESTAMPS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except Exception:                                                     # noqa: BLE001
        pass

    # Tier 2 -- Supabase mirror.  _write_analysis_timestamp pushes to
    # this key on every successful analyze; pull it back when the local
    # file is missing.
    try:
        from src import db as _db
        if _db.is_supabase():
            row = _db.cache_get("analysis_timestamps")
            payload = None
            if isinstance(row, dict):
                payload = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(payload, dict) and payload:
                # Strip the wrapper fields cache_set writes around the
                # data so the returned shape matches the local file.
                clean = {
                    k: v for k, v in payload.items()
                    if isinstance(v, dict) and "analyzed_at" in v
                }
                if clean:
                    try:
                        Path("data").mkdir(exist_ok=True)
                        _ANALYSIS_TIMESTAMPS_FILE.write_text(
                            json.dumps(clean, indent=2), encoding="utf-8",
                        )
                        print(
                            f"TIMESTAMP-READ: restored "
                            f"{list(clean.keys())} from Supabase mirror",
                            flush=True, file=sys.stderr,
                        )
                    except Exception:                                      # noqa: BLE001
                        pass
                    return clean
    except Exception as exc:                                              # noqa: BLE001
        print(f"TIMESTAMP-READ: Supabase fallback failed: {exc}",
              flush=True, file=sys.stderr)

    return {}


def _write_analysis_timestamp(sport: str, ts: datetime) -> None:
    """Persist a single sport's analysis timestamp.  Writes the local
    timestamps file AND mirrors to Supabase app_cache so Railway
    redeploys don't lose the field.  Best-effort; never raises.

    Closes a gap where the admin panel's "Last analyzed" line went
    stale after a redeploy because this file was wiped but the
    snapshot/cache mirrors were either also wiped (analysis_cache)
    or write-once (snapshot, pre-fix).
    """
    try:
        Path("data").mkdir(exist_ok=True)
        data = _read_analysis_timestamps()
        data[sport] = {
            "analyzed_at": ts.isoformat(),
            "date":        ts.date().isoformat(),
        }
        _ANALYSIS_TIMESTAMPS_FILE.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        # Mirror to Supabase under a stable key so reset-aware admin
        # views can read it even after the local file is wiped.
        _supabase_cache_set(
            "analysis_timestamps", None, ts.date().isoformat(), data,
        )
        print(
            f"TIMESTAMP-WRITE: {sport.upper()}  analyzed_at={ts.isoformat()}  "
            f"local + Supabase synced",
            flush=True, file=sys.stderr,
        )
    except Exception as exc:                                              # noqa: BLE001
        print(f"TIMESTAMP write error (ignored): {exc}",
              flush=True, file=sys.stderr)
















def _clear_snapshot_sport(sport: str) -> None:
    """
    Remove a single sport's entry from today's snapshot so a fresh run can
    overwrite it.  Used by force_refresh.  Atomic write; never raises.
    """
    if not _SNAPSHOT_ENABLED:
        return
    with _snapshot_lock:
        try:
            if not _DAILY_SNAPSHOT_FILE.exists():
                return
            raw = _DAILY_SNAPSHOT_FILE.read_text(encoding="utf-8")
            snap = json.loads(raw) if raw.strip() else {}
            if sport not in snap:
                return
            del snap[sport]
            raw_out = json.dumps(snap, indent=2, default=str)
            _DAILY_SNAPSHOT_TMP.write_text(raw_out, encoding="utf-8")
            _DAILY_SNAPSHOT_TMP.replace(_DAILY_SNAPSHOT_FILE)
            # Issue 4: keep Supabase mirror in sync.
            try:
                _supabase_cache_set(
                    _CACHE_KEY_SNAPSHOT, None, snap.get("date") or _today_et(), snap,
                )
            except Exception:                                             # noqa: BLE001
                pass
        except Exception as _e:
            print(f"SNAPSHOT clear error (ignored): {_e}", flush=True, file=sys.stderr)
            try:
                _DAILY_SNAPSHOT_TMP.unlink(missing_ok=True)
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# System prompt for the AI Breakdown chat UI (pages/ai_breakdown.py).
# Distinct from _ANALYST_SYSTEM_PROMPT (which feeds the deeper
# /api/ai/breakdown report) so each surface can be tuned independently.
#
# IMPORTANT: keep this prompt instructing "no markdown" -- the NiceGUI
# chat surface renders text plainly; asterisks and pound signs would
# show through as raw characters.
_CHAT_SYSTEM_PROMPT = (
    "You are an experienced sports betting analyst who has done the homework — "
    "not a data reader. You have access to today's MLB and WNBA model predictions, "
    "confidence scores, edge percentages, SHAP factors, starting-pitcher stats and "
    "pitch mix (usage % and velocity by pitch type), team stats, and betting lines. "
    "Reason ACROSS all of these signals to form your own opinion rather than "
    "describing them one at a time. For any pick the user asks about, proactively "
    "identify what would make it FAIL and whether those risks are present today. "
    "Use the pitch-mix data to judge whether a matchup is mechanically favorable or "
    "unfavorable — for example, a pitcher who leans heavily on a pitch type the "
    "opposing hitters struggle against. When similar-player comparisons are present "
    "in the data, reference them. Be direct and willing to say plainly when a pick "
    "looks weak DESPITE high model confidence, and when you disagree with the model, "
    "state your own pick and why. Always include the confidence level and edge when "
    "referencing a pick, and end with a clear stance: 'Agree with model', "
    "'Disagree — my pick is X', or 'Lean with caution'. "
    "Answer only sports/betting questions; for anything unrelated reply exactly: "
    "I can only answer sports related questions about picks, players, teams, and "
    "betting data. Never use any formatting whatsoever: no markdown (no asterisks, "
    "underscores, pound signs, or backticks) and no HTML tags — plain text only "
    "with line breaks for separation, because formatting displays as raw symbols here."
)




# ── Anthropic helper ──────────────────────────────────────────────────────────













def _load_archive_bets() -> list[dict]:
    """Load all settled bets from the permanent archive file."""
    if not _ARCHIVE_PATH.exists():
        return []
    try:
        raw = json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8"))
        return raw.get("bets", []) if isinstance(raw, dict) else raw
    except Exception:
        return []







def _load_model_settings() -> dict:
    """Return current model-bets settings.  Always returns a complete dict.

    Supabase app_cache ('model_settings') is the source of truth (survives
    Railway redeploys); the local file is only a fallback for when Supabase
    is off or hasn't been written yet."""
    try:
        from src import db as _db
        if _db.is_supabase():
            row = _db.cache_get("model_settings")
            data = row.get("data") if isinstance(row, dict) else None
            if isinstance(data, dict) and data:
                return {**_MODEL_SETTINGS_DEFAULT, **data}
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_settings Supabase load failed: %s", exc)
    try:
        if _MODEL_SETTINGS_FILE.exists():
            raw = json.loads(_MODEL_SETTINGS_FILE.read_text(encoding="utf-8"))
            return {**_MODEL_SETTINGS_DEFAULT, **(raw or {})}
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_settings load failed: %s", exc)
    return dict(_MODEL_SETTINGS_DEFAULT)


def _save_model_settings(settings: dict) -> dict:
    """Persist settings (merged onto defaults) and return the saved snapshot.

    Per-key type coercion: any key whose default value is bool is coerced
    via bool(v); any key whose default is int is coerced via int(v) with a
    fallback to the default on parse failure.  Anything else is stored
    as-is.  Keeps existing boolean toggles working while allowing
    numeric settings (ai_daily_limit, etc.) to round-trip correctly.
    """
    merged: dict = {**_MODEL_SETTINGS_DEFAULT, **(settings or {})}
    coerced: dict = {}
    for k, v in merged.items():
        default = _MODEL_SETTINGS_DEFAULT.get(k)
        if isinstance(default, bool):
            coerced[k] = bool(v)
        elif isinstance(default, int):
            try:
                coerced[k] = int(v)
            except (TypeError, ValueError):
                coerced[k] = default
        else:
            coerced[k] = v
    _MODEL_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MODEL_SETTINGS_FILE.write_text(json.dumps(coerced, indent=2), encoding="utf-8")
    # Mirror to Supabase app_cache so the toggles survive Railway redeploys
    # (the local file is just a cache).  Best-effort.
    try:
        from src import db as _db
        if _db.is_supabase():
            _db.cache_set("model_settings", None,
                          datetime.now(timezone.utc).strftime("%Y-%m-%d"), coerced)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"_save_model_settings: Supabase cache_set failed: {exc}")
    return coerced




# Module-level scheduler reference (set at startup)
_sched = None




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
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}

def _save_pre_game_odds(store: dict) -> None:
    try:
        Path("data").mkdir(exist_ok=True)
        _PRE_GAME_ODDS_FILE.write_text(json.dumps(store, default=str), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

def _lock_in_pre_game_odds(games: list) -> list:
    """Pre-game odds lock.

    The model was trained on opening-market lines, so live in-play odds
    would feed it out-of-distribution data.  This function:

      1. For UPCOMING games (commence_time still in the future):
           - Uses the API's current odds as-is (those ARE pre-game lines).
           - Snapshots them into _PRE_GAME_ODDS_FILE so the next call -- after
             the game starts -- can restore the same numbers.
           - Sets g["_pregame_locked"] = True.

      2. For STARTED games (commence_time in the past):
           a. If we have a snapshot for this gamePk, restore those fields
              over whatever the API just returned.  Set _pregame_locked=True.
           b. If no snapshot exists, the API response is live in-play.
              Leave the fields alone but set _pregame_locked=False so
              downstream code can decide whether to predict on them.

    Each branch logs a single line so 'why are my odds different from
    the book right now' has an audit trail in Railway logs.
    """
    now_utc = datetime.now(timezone.utc)
    store   = _load_pre_game_odds()
    updated = False
    result  = []
    n_fresh = n_locked = n_live_no_snap = 0

    for game in games:
        gid = game.get("id", "")
        away = game.get("away_team", "?")
        home = game.get("home_team", "?")
        matchup = f"{away[:3]}@{home[:3]}"
        try:
            ct = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        except Exception:
            game["_pregame_locked"] = None
            result.append(game)
            continue

        if ct > now_utc:
            # Upcoming -- API odds are pre-game by definition.  Capture
            # them so we can restore later if the game starts before we
            # refresh again.
            snap = {f: game.get(f) for f in _ODDS_FIELDS}
            snap["commence_time"]   = game["commence_time"]
            snap["captured_at_utc"] = now_utc.isoformat()
            store[gid] = snap
            updated = True
            game["_pregame_locked"]  = True
            game["_odds_source"]     = "live_pre_game"
            n_fresh += 1
            _eprint(f"  [pre-game-lock] {matchup} {gid}: using FRESH pre-game "
                    f"odds from API (commence_time={game['commence_time']})")
            result.append(game)
        else:
            # Started -- prefer the snapshot.  If absent, fall back to the
            # live odds but flag them so downstream code knows.
            if gid in store:
                snap = store[gid]
                merged = {**game, **{f: snap[f] for f in _ODDS_FIELDS if f in snap}}
                merged["_pregame_locked"] = True
                merged["_odds_source"]    = "pre_game_snapshot"
                merged["_odds_captured_at"] = snap.get("captured_at_utc")
                _eprint(f"  [pre-game-lock] {matchup} {gid}: game STARTED "
                        f"(commence={game['commence_time']}); restored "
                        f"pre-game snapshot captured at {snap.get('captured_at_utc')}")
                n_locked += 1
                result.append(merged)
            else:
                game["_pregame_locked"] = False
                game["_odds_source"]    = "live_no_snapshot"
                _eprint(f"  [pre-game-lock] {matchup} {gid}: game STARTED "
                        f"and NO pre-game snapshot exists -- live in-play "
                        f"odds in payload, _pregame_locked=False.  Model "
                        f"prediction NOT safe; caller should skip.")
                n_live_no_snap += 1
                result.append(game)

    if updated:
        _save_pre_game_odds(store)

    _eprint(f"  [pre-game-lock] summary: fresh_pregame={n_fresh}  "
            f"restored_from_snapshot={n_locked}  "
            f"live_no_snapshot={n_live_no_snap}  total={len(result)}")
    return result








def _serialize_wnba_no_model(game: dict, reason: str | None) -> dict:
    """Build a JSON-safe serialized dict for a WNBA game the model could
    not predict (e.g. one of the teams has no training data because it's
    a 2026 expansion team like Toronto Tempo).

    Carries matchup + bookmaker odds + market-implied probabilities.  No
    model pick fields -- the `_no_model` flag tells the UI to render a
    NO MODEL PICK badge instead of fake prediction bet boxes.

    Result flows through the same caching + snapshot path as model-picked
    results, so the game still appears on the WNBA tab tonight and the
    user knows it's on -- they just can't get a model recommendation.
    """
    home_implied = float(game.get("home_implied_prob") or 0.5)
    away_implied = 1.0 - home_implied
    return {
        "_no_model":         True,
        "_no_model_reason":  reason or "Model could not produce a prediction for this matchup.",
        "game_id":           game.get("id") or "",   # Track button needs this
        "commence_time":     game.get("commence_time", ""),
        "home_team":         game.get("home_team", ""),
        "away_team":         game.get("away_team", ""),
        # Field names match _serialize_wnba's matchup-row contract
        "home_odds":         game.get("h2h_home_odds"),
        "away_odds":         game.get("h2h_away_odds"),
        # No model pick -- explicit Nones so MONEYLINE bet box shows '—'
        "pick_team":         None,
        "pick_prob":         None,
        "pick_edge":         None,
        "pick_odds":         None,
        "value_pick":        False,
        # Market-implied probabilities surfaced for transparency
        "market_home_prob":  round(home_implied, 4),
        "market_away_prob":  round(away_implied, 4),
        # No spread / totals model output either
        "spread_pick":       None,
        "totals":            None,
        "run_line":          None,
    }


def _save_wnba_analysis_cache(serialized, parlays, games_loaded, cv_acc, lr_cv_acc,
                              analyzed_at: datetime | None = None):
    try:
        _ts = analyzed_at or datetime.now(timezone.utc)
        Path("data").mkdir(exist_ok=True)
        payload = {
            "date":          _today_et(),  # ET date — correct even when analysis runs after 8 PM ET
            "analyzed_at":   _ts.isoformat(),
            "sport":         "wnba",
            "games_loaded":  games_loaded,
            "cv_accuracy":   cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
            "results":       serialized,
            "parlays":       parlays,
        }
        _WNBA_ANALYSIS_CACHE_FILE.write_text(json.dumps(payload, default=str), encoding="utf-8")
        # Issue 4: mirror to Supabase so the cache survives Railway redeploys.
        _supabase_cache_set(_CACHE_KEY_ANALYSIS_WNBA, "wnba", payload["date"], payload)
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
















# ── Analysis disk cache ───────────────────────────────────────────────────────

def _save_analysis_cache(serialized: list, parlays: dict, sport: str,
                         games_loaded: int, cv_acc, lr_cv_acc, nn_val_acc,
                         analyzed_at: datetime | None = None) -> None:
    """Persist today's serialized analysis to disk for cross-session auto-load."""
    try:
        _ts = analyzed_at or datetime.now(timezone.utc)
        Path("data").mkdir(exist_ok=True)
        payload = {
            "date":            _today_et(),  # ET date — correct even when analysis runs after 8 PM ET
            "analyzed_at":     _ts.isoformat(),
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
        # Issue 4: mirror to Supabase so the cache survives Railway redeploys.
        _supabase_cache_set(_CACHE_KEY_ANALYSIS_MLB, "mlb", payload["date"], payload)
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "OK", 200


# ── Home page ("/" + "/home-v2") — server-rendered (NiceGUI → HTML migration) ─
# ── Player-props page (Flask + Tailwind, PR #304) ───────────────────────────
# Replaces the NiceGUI props page (now at /props-legacy) with a static
# Tailwind template.  Pure rendering layer: it reads the SAME scored-props
# cache the NiceGUI page used (src.props_scored_cache.load_scored_props) and
# flattens each pick into a small, JSON-serialisable view-model the template
# + static/js/props.js can filter entirely client-side.  No model code,
# Supabase query, or scoring logic changes here.

# Market-key -> short display label.  Mirrors pages/props.py `_short_market`
# so the two pages stay visually consistent during the migration.
_PROPS_MARKET_LABELS = {
    "pitcher_strikeouts":   "Strikeouts",
    "pitcher_outs":         "Outs Recorded",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks":        "Walks Allowed",
    "pitcher_earned_runs":  "Earned Runs",
    "pitcher_record_a_win": "Win",
    "batter_hits":          "Hits",
    "batter_total_bases":   "Total Bases",
    "batter_home_runs":     "Home Runs",
    "batter_rbis":          "RBIs",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
    "batter_stolen_bases":  "Stolen Bases",
}


def _props_market_label(market):
    """Human-readable label for a market key (falls back to Title Case)."""
    if not market:
        return ""
    return _PROPS_MARKET_LABELS.get(market, str(market).replace("_", " ").title())


def _props_headshot_url(player_id):
    """MLB Stats API headshot URL.  The d_people:generic transform yields a
    generic silhouette for unknown/invalid ids, so this is always safe to
    emit; the template still keeps an emoji fallback via <img onerror>."""
    if not player_id:
        return ""
    return (
        "https://img.mlbstatic.com/mlb-photos/image/upload/"
        "d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
        "v1/people/{}/headshot/67/current".format(player_id)
    )


def _props_window_hit_rate(summary, window):
    """Hit rate (0..1) for a summary window like 'last_10' / 'last_20', or
    None when there are no games recorded for that window."""
    if not isinstance(summary, dict):
        return None
    hits = summary.get("{}_hits".format(window))
    games = summary.get("{}_games".format(window))
    try:
        hits = float(hits or 0)
        games = float(games or 0)
    except (TypeError, ValueError):
        return None
    if games <= 0:
        return None
    return hits / games


def _props_game_time(commence_time):
    """Format an ISO-8601 UTC commence_time as a short ET clock label
    ('7:05 PM'), or '' when missing/unparseable.  Display-only."""
    if not commence_time:
        return ""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        raw = str(commence_time).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(ZoneInfo("America/New_York"))
        return dt.strftime("%-I:%M %p")
    except Exception:                                                     # noqa: BLE001
        return ""


def _props_grade_composite(pick):
    """0..1 matchup-grade composite -- mirrors pages/props.py _prop_grade_composite
    so the filter-panel slider matches NiceGUI exactly (no data re-derivation,
    same weights: 0.50 confidence + 0.30 opp_rank + 0.20 ev_pct)."""
    try:
        conf = float(pick.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf_score = max(0.0, min(1.0, (conf - 0.50) / 0.45))
    try:
        rank = int(pick.get("opp_rank") or 15)
    except (TypeError, ValueError):
        rank = 15
    rank_score = max(0.0, min(1.0, (31 - rank) / 30.0))
    try:
        ev = float(pick.get("ev_pct") or 0.0)
    except (TypeError, ValueError):
        ev = 0.0
    ev_score = max(0.0, min(1.0, ev / 30.0))
    return 0.5 * conf_score + 0.3 * rank_score + 0.2 * ev_score


def _props_roi_str(roi):
    """Format a ROI% value as '+14.2%' / '-3.1%', or None when absent.
    Mirrors pages/props.py _roi_str so the X-Ray table's ROI sub-text
    is byte-identical between the NiceGUI and Tailwind views."""
    if roi is None:
        return None
    try:
        return "{:+.1f}%".format(float(roi))
    except (TypeError, ValueError):
        return None


def _props_window_hits_games(summary, window):
    """Raw (hits, games) for a summary window key like 'last_5' / 'last_10'.
    Returns (None, None) when the cache row doesn't carry that window."""
    if not isinstance(summary, dict):
        return None, None
    hits  = summary.get("{}_hits".format(window))
    games = summary.get("{}_games".format(window))
    try:
        hits_i  = int(hits  or 0)
        games_i = int(games or 0)
    except (TypeError, ValueError):
        return None, None
    if games_i <= 0:
        return None, None
    return hits_i, games_i


def _props_view_model(pick):
    """Flatten one scored-props pick into the flat dict the Tailwind card +
    client-side filters consume.  Every value is JSON-serialisable."""
    summary = pick.get("summary") if isinstance(pick, dict) else None
    summary = summary if isinstance(summary, dict) else {}

    try:
        confidence = float(pick.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        edge = float(pick.get("edge") or 0.0)
    except (TypeError, ValueError):
        edge = 0.0

    l10 = _props_window_hit_rate(summary, "last_10")
    # The scored summary carries hit-window counts for L5/L10/L20 but only a
    # season *average* (not a season hit count), so we surface the longest
    # available rolling window (L20) as the "season" hit-rate proxy.  Honest
    # best-effort given the cached shape; labelled "Season" in the card.
    season = _props_window_hit_rate(summary, "last_20")

    side = (pick.get("recommendation") or pick.get("side") or "Over")
    side = str(side).title()

    # ── Phase-2b filter fields ─────────────────────────────────────────────
    # All sourced from the raw pick dict the scored cache already produces;
    # no new data fetches.
    try:
        l10_hits = int((summary.get("last_10_hits") or 0))
    except (TypeError, ValueError):
        l10_hits = 0
    line_type = (pick.get("line_type") or "main").lower()
    home_team = (pick.get("home_team") or "").strip()
    away_team = (pick.get("away_team") or "").strip()
    event_id  = pick.get("event_id")
    if event_id:
        game_key = str(event_id)
    elif home_team or away_team:
        game_key = f"{away_team or '?'}@{home_team or '?'}"
    else:
        game_key = ""
    game_label = (f"{away_team or '?'} @ {home_team or '?'}"
                  if (home_team or away_team) else "")

    # ── Phase-2c track-button fields ───────────────────────────────────────
    # Server-side "already tracked" check survives page reloads (parity
    # with pages/props.py _track_button); the four payload fields below
    # are passed through to /api/props/track unchanged.
    try:
        from src import props_picks_tracker as _ppt_check
        tracked = bool(_ppt_check.is_tracked(
            pick.get("player"), pick.get("market"),
            pick.get("line"), side, event_id,
        ))
    except Exception:                                                      # noqa: BLE001
        tracked = False

    return {
        "sport":          str(pick.get("sport") or "MLB").upper(),
        "player":         pick.get("player") or "",
        "player_id":      pick.get("player_id"),
        "headshot":       _props_headshot_url(pick.get("player_id")),
        "position":       (pick.get("bucket") or "").title(),
        "team":           pick.get("team") or "",
        "matchup":        pick.get("team") or "",
        "market":         pick.get("market") or "",
        "stat_label":     _props_market_label(pick.get("market")),
        "line":           pick.get("line"),
        "side":           side,
        "confidence":     round(confidence, 4),
        "confidence_pct": round(confidence * 100, 1),
        "edge":           round(edge, 4),
        "edge_pct":       round(edge * 100, 1),
        "l10_hit_rate":   None if l10 is None else round(l10 * 100, 1),
        "season_hit_rate": None if season is None else round(season * 100, 1),
        "model":          pick.get("source") or "model",
        "game_time":      _props_game_time(pick.get("commence_time")),
        "commence_time":  pick.get("commence_time") or "",
        # ── filter-panel fields ──────────────────────────────────────────
        "l10_hits":       l10_hits,
        "line_type":      line_type,
        "game_key":       game_key,
        "game_label":     game_label,
        "prop_grade":     round(_props_grade_composite(pick), 4),
        # ── track-button fields ──────────────────────────────────────────
        "tracked":        tracked,
        "event_id":       event_id,
        "best_odds":      pick.get("best_odds"),
        "predicted_value": pick.get("predicted_value"),
        # ── X-Ray fields (Phase 2e) ───────────────────────────────────────
        # All from the raw pick / summary -- no new data, no recompute.  The
        # X-Ray table reads these directly; the list / by-game card views
        # ignore them.
        "ev_pct":         pick.get("ev_pct"),
        "l5_hits":        (lambda hg=_props_window_hits_games(summary, "last_5"):
                           hg[0])(),
        "l5_games":       (lambda hg=_props_window_hits_games(summary, "last_5"):
                           hg[1])(),
        "l5_roi":         _props_roi_str(summary.get("l5_roi")),
        "l10_games":      (lambda hg=_props_window_hits_games(summary, "last_10"):
                           hg[1])(),
        "l10_roi":        _props_roi_str(summary.get("l10_roi")),
        "season_avg":     summary.get("season_avg"),
        "szn_roi":        _props_roi_str(summary.get("szn_roi")),
    }


@app.route("/props")
def props_page():
    """Serve the new Flask + Tailwind player-props page.  Reads the scored
    props cache and passes a flat view-model list to templates/props.html as
    JSON; all filtering/sorting happens client-side in static/js/props.js."""
    try:
        from src.props_scored_cache import load_scored_props
        cache = load_scored_props() or {}
    except Exception as exc:                                              # noqa: BLE001
        print("PROPS PAGE: load_scored_props failed: {}".format(exc),
              flush=True, file=sys.stderr)
        cache = {}

    picks = cache.get("picks") or []
    props = [_props_view_model(p) for p in picks if isinstance(p, dict)]

    # Distinct stat-type labels present in today's slate, for the filter pills.
    seen = []
    for p in props:
        if p["stat_label"] and p["stat_label"] not in seen:
            seen.append(p["stat_label"])

    # Phase-2b filter-panel option sets, derived from today's slate.
    market_options: list[dict] = []
    market_seen: set = set()
    for p in props:
        m = p["market"]
        if m and m not in market_seen:
            market_seen.add(m)
            market_options.append({"key": m, "label": p["stat_label"] or m})
    market_options.sort(key=lambda d: d["label"])

    game_options: list[dict] = []
    game_seen: set = set()
    for p in props:
        gk = p["game_key"]
        if gk and gk not in game_seen:
            game_seen.add(gk)
            game_options.append({"key": gk, "label": p["game_label"] or gk})
    game_options.sort(key=lambda d: d["label"])

    return render_template(
        "props.html",
        props=props,
        stat_labels=seen,
        market_options=market_options,
        game_options=game_options,
        generated_at=cache.get("generated_at"),
        prop_date=cache.get("date"),
    )


# ── Home-page helpers (Phase-1 Flask port of pages/home.py) ──────────────────
# Mirrors the NiceGUI home page's chip / EV-scan / confidence-carousel sections
# only.  Games table, news, rotation, heatmap, model-performance are deferred
# to a Phase-2 follow-up.

from zoneinfo import ZoneInfo as _ZoneInfo
_ET = _ZoneInfo("America/New_York")

_HOME_EV_MIN_EDGE    = 0.03
_HOME_CONFIDENCE_MIN = 0.0001
_HOME_STARTED_TOKENS: frozenset[str] = frozenset(
    s.lower() for s in (
        "Final", "Live", "In Progress", "In_Progress",
        "Game Over", "Postponed", "Suspended", "Completed Early",
        "Final: Tied", "Manager Challenge", "Delayed",
        "Suspended: Rain", "Suspended Rain",
    )
)


def _home_has_started(g: dict) -> bool:
    """True when a serialized game row should be excluded from forward-looking lists."""
    status = (g.get("status") or g.get("game_status") or "").lower().strip()
    if status in _HOME_STARTED_TOKENS:
        return True
    ct = g.get("commence_time") or g.get("game_time") or ""
    if not ct:
        return False
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        return dt.astimezone(_ET) <= datetime.now(_ET)
    except Exception:                                                      # noqa: BLE001
        return False


def _home_filter_upcoming(games: list[dict]) -> list[dict]:
    """Keep only games that haven't started yet."""
    return [g for g in games if not _home_has_started(g)]


def _home_all_serialized_games() -> list[dict]:
    """Pull + flatten both sport caches into one list of serialized dicts.
    Mirrors _all_serialized_games in pages/home.py -- same passthrough guard,
    same _sport stamp, same per-row exception isolation."""
    out: list[dict] = []

    # MLB
    try:
        bankroll = float(_analysis_state.get("bankroll") or 250)
        mlb_ledger = None
        for r in (_analysis_state.get("results") or []):
            try:
                if "home_team" in r and "away_team" in r:
                    g = dict(r)
                else:
                    if mlb_ledger is None:
                        mlb_ledger = Ledger(
                            path="data/ledger.json",
                            starting_bankroll=bankroll,
                        )
                    s_bank = mlb_ledger.data.get("personal_starting_bankroll", bankroll)
                    g = _serialize(r, bankroll, "mlb", s_bank)
                g["_sport"] = "mlb"
                out.append(g)
            except Exception:                                              # noqa: BLE001
                continue
    except Exception:                                                      # noqa: BLE001
        pass

    # WNBA
    try:
        bankroll = float(_wnba_analysis_state.get("bankroll") or 1000)
        wnba_results = _wnba_analysis_state.get("results") or []
        if wnba_results:
            wnba_ledger = None
            for r in wnba_results:
                try:
                    if "home_team" in r and "away_team" in r:
                        g = dict(r)
                    else:
                        if wnba_ledger is None:
                            wnba_ledger = Ledger(
                                path="data/wnba_ledger.json",
                                starting_bankroll=bankroll,
                            )
                        s_bank = wnba_ledger.data.get("personal_starting_bankroll", bankroll)
                        g = _serialize_wnba(r, bankroll, s_bank)
                    g["_sport"] = "wnba"
                    out.append(g)
                except Exception:                                          # noqa: BLE001
                    continue
    except Exception:                                                      # noqa: BLE001
        pass

    return out


def _home_stub_games() -> list[dict]:
    """Today's schedule stubs (no analysis yet).  Returns [] if either sport
    already has analysis results -- the carousels cover the slate."""
    try:
        mlb_results  = _analysis_state.get("results") or []
        wnba_results = _wnba_analysis_state.get("results") or []
    except Exception:                                                      # noqa: BLE001
        mlb_results = wnba_results = []
    if mlb_results or wnba_results:
        return []

    games: list[dict] = []
    try:
        games += list(get_todays_schedule("mlb"))
    except Exception:                                                      # noqa: BLE001
        pass
    try:
        games += list(get_todays_schedule("wnba"))
    except Exception:                                                      # noqa: BLE001
        pass
    return games


def _home_fmt_game_time(iso) -> str:
    """ISO commence_time -> '7:05 PM ET'.  Returns 'TBD' on failure."""
    if not iso:
        return "TBD"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%-I:%M %p ET")
    except Exception:                                                      # noqa: BLE001
        return "TBD"


def _home_news_items() -> tuple[list[dict], str]:
    """ESPN news headlines for the home page.  Picks the dominant active
    sport (MLB primary, WNBA fallback only when MLB has no results and
    WNBA does), then calls news_feed.fetch (5-min in-process cache).
    Returns ``(items, sport)`` -- sport drives the badge color/label
    in the template."""
    try:
        wnba_active = bool((_wnba_analysis_state or {}).get("results"))
        mlb_active  = bool((_analysis_state or {}).get("results"))
    except Exception:                                                      # noqa: BLE001
        wnba_active = mlb_active = False
    sport = "wnba" if (wnba_active and not mlb_active) else "mlb"
    try:
        from src.news_feed import fetch as _nf_fetch
        items = _nf_fetch(sport, max_items=10) or []
    except Exception:                                                      # noqa: BLE001
        items = []
    return items, sport


# ── Games table (Phase 2c) ──────────────────────────────────────────────────
# Ported from pages/home.py's _section_games + helpers.  All formatting happens
# server-side so the template stays logic-free; sport toggle is client-side
# show/hide because both sports' data is already in memory (no extra fetch).

_HOME_MLB_ABBR: dict[str, str] = {
    "Arizona Diamondbacks": "ARI",  "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",     "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",          "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",       "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",      "Detroit Tigers": "DET",
    "Houston Astros": "HOU",        "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",         "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",       "New York Mets": "NYM",
    "New York Yankees": "NYY",      "Oakland Athletics": "OAK",
    "Athletics": "ATH",             "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",   "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",   "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",         "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}
_HOME_WNBA_ABBR: dict[str, str] = {
    "Atlanta Dream": "ATL",         "Chicago Sky": "CHI",
    "Connecticut Sun": "CONN",      "Dallas Wings": "DAL",
    "Indiana Fever": "IND",         "Las Vegas Aces": "LV",
    "Los Angeles Sparks": "LA",     "Minnesota Lynx": "MIN",
    "New York Liberty": "NY",       "Phoenix Mercury": "PHX",
    "Seattle Storm": "SEA",         "Washington Mystics": "WSH",
    "Golden State Valkyries": "GSV",
}

# 5-min in-process cache, keyed sport_YYYY-MM-DD (mirrors pages/home.py's
# _GAMES_CACHE so a hot page reload pays only one schedule fetch per TTL).
_HOME_GAMES_CACHE: dict[str, dict] = {}
_HOME_GAMES_CACHE_TTL = 300


def _g_abbr(name: str, sport: str) -> str:
    """Short team abbrev from the lookup tables; falls back to last word."""
    table = _HOME_MLB_ABBR if sport == "mlb" else _HOME_WNBA_ABBR
    abbr  = table.get((name or "").strip())
    if abbr:
        return abbr
    parts = (name or "").strip().split()
    return parts[-1][:4].upper() if parts else (name or "")[:4].upper()


def _g_ml(odds) -> str:
    """American moneyline odds -> '+130' / '-145' / ''."""
    if odds is None:
        return ""
    try:
        n = int(odds)
        return f"+{n}" if n > 0 else str(n)
    except (TypeError, ValueError):
        return ""


def _g_spread(spread, for_home: bool) -> str:
    """Spread -> '+1.5' / '-1.5'.  spread is the home team's line."""
    if spread is None:
        return ""
    try:
        v = float(spread)
        v = v if for_home else -v
        return f"{v:+.1f}"
    except (TypeError, ValueError):
        return ""


def _g_time(commence_time: str) -> str:
    """ISO UTC commence time -> 'H:MM AM/PM' (no zero-padded hour)."""
    if not commence_time:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        et = dt.astimezone(_ET)
        h  = et.hour % 12 or 12
        p  = "PM" if et.hour >= 12 else "AM"
        return f"{h}:{et.minute:02d} {p}"
    except Exception:                                                      # noqa: BLE001
        return ""


def _g_total(g: dict):
    """Extract O/U line from wherever it lives in a serialized game."""
    for getter in (
        lambda d: d.get("total_line"),
        lambda d: (d.get("totals") or {}).get("total_line"),
        lambda d: (d.get("totals") or {}).get("line"),
    ):
        try:
            v = getter(g)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _home_games_data(sport: str) -> dict:
    """Merge today's schedule (status + scores) with serialized odds; shape
    each row for the template.  Returns {"upcoming": [...], "final": [...]}
    with all formatted strings ready to render.  Cached 5 min per sport."""
    import time as _t
    from datetime import datetime
    today     = datetime.now(_ET).date().isoformat()
    cache_key = f"{sport}_{today}"
    entry     = _HOME_GAMES_CACHE.get(cache_key)
    if entry and (_t.monotonic() - entry["ts"]) < _HOME_GAMES_CACHE_TTL:
        return entry["data"]

    try:
        schedule: list[dict] = list(get_todays_schedule(sport) or [])
    except Exception:                                                      # noqa: BLE001
        schedule = []

    odds_map: dict[tuple, dict] = {}
    try:
        for g in _home_all_serialized_games():
            if (g.get("_sport") or "").lower() == sport.lower():
                key = ((g.get("home_team") or "").strip(),
                       (g.get("away_team") or "").strip())
                odds_map[key] = g
    except Exception:                                                      # noqa: BLE001
        pass

    upcoming: list[dict] = []
    final:    list[dict] = []

    for game in schedule:
        home   = (game.get("home_team") or "").strip()
        away   = (game.get("away_team") or "").strip()
        status = (game.get("status")       or "Preview").strip()
        coded  = (game.get("coded_status") or "").upper()
        odds   = odds_map.get((home, away), {})

        # Pre-format everything so the template is logic-free.
        away_ml_s = _g_ml(odds.get("away_odds"))
        home_ml_s = _g_ml(odds.get("home_odds"))
        away_sp_s = _g_spread(odds.get("spread"), for_home=False)
        home_sp_s = _g_spread(odds.get("spread"), for_home=True)
        time_str  = _g_time(game.get("commence_time", ""))
        total     = _g_total(odds)
        total_str = f"O/U {total:.1f}" if total is not None else ""

        home_sc = game.get("home_score")
        away_sc = game.get("away_score")
        is_final = (
            coded in ("F", "O")
            or status.lower() in ("final", "game over", "completed")
        )

        if is_final and home_sc is not None:
            final.append({
                "home_abbr":  _g_abbr(home, sport),
                "away_abbr":  _g_abbr(away, sport),
                "home_score": home_sc,
                "away_score": away_sc if away_sc is not None else 0,
                "home_wins":  (away_sc is not None and home_sc > away_sc),
                "away_wins":  (away_sc is not None and away_sc > home_sc),
                "commence_time": game.get("commence_time", ""),
            })
        else:
            upcoming.append({
                "home_abbr":  _g_abbr(home, sport),
                "away_abbr":  _g_abbr(away, sport),
                "home_ml":    home_ml_s,
                "away_ml":    away_ml_s,
                "home_spread": home_sp_s,
                "away_spread": away_sp_s,
                "time_str":   time_str,
                "total_str":  total_str,
                "commence_time": game.get("commence_time", ""),
            })

    upcoming.sort(key=lambda g: g.get("commence_time") or "")
    final.sort(key=lambda g: g.get("commence_time") or "", reverse=True)

    data = {"upcoming": upcoming, "final": final}
    _HOME_GAMES_CACHE[cache_key] = {"ts": _t.monotonic(), "data": data}
    return data


def _home_games_block() -> dict:
    """Both sports + default-sport pick (MLB primary, WNBA fallback when
    MLB has no analysis results and WNBA does -- same logic as the News
    section).  Template renders both, JS toggle shows the active one."""
    try:
        wnba_active = bool((_wnba_analysis_state or {}).get("results"))
        mlb_active  = bool((_analysis_state or {}).get("results"))
    except Exception:                                                      # noqa: BLE001
        wnba_active = mlb_active = False
    default_sport = "wnba" if (wnba_active and not mlb_active) else "mlb"
    return {
        "default_sport": default_sport,
        "mlb":           _home_games_data("mlb"),
        "wnba":          _home_games_data("wnba"),
    }


# ── Team Rotation chart (Phase 2d) ──────────────────────────────────────────
# Mechanical port of pages/home.py _rotation_chart_opts.  Builds the ECharts
# scatter-plot options dict the front-end echarts.init() consumes.  Colors are
# remapped to the Flask palette (#22c55e/#ef4444/#f59e0b/#94a3b8/#2a3040 ...)
# so the chart looks coherent with the rest of the new home page.

_HOME_ROT_AXIS_NAMES = {
    "ml":  "Win %",
    "ats": "ATS Cover % (run-line proxy)",
    "ou":  "Over %",
}
_HOME_ROT_METRIC_SHORT = {"ml": "ML", "ats": "ATS", "ou": "O/U"}


def _home_rotation_opts(points: list[dict], metric: str, sport: str) -> dict:
    """Build ECharts scatter-plot opts.  Pure-Python — returned dict goes out
    as JSON and is consumed by chart.setOption(opts, true) on the client."""
    wnba_proxy = (
        sport == "wnba" and metric in ("ats", "ou")
        and any(pt.get("ml_proxy") for pt in points)
    )
    axis = _HOME_ROT_AXIS_NAMES["ml" if wnba_proxy else metric]
    suffix = " (ML proxy)" if wnba_proxy else ""
    x_name = f"Recent 14d — {axis}{suffix}"
    y_name = f"Season — {axis}{suffix}"
    ml_lbl = _HOME_ROT_METRIC_SHORT["ml" if wnba_proxy else metric]

    scatter_data: list[dict] = []
    for pt in points:
        x100 = round(pt["x"] * 100, 1)
        y100 = round(pt["y"] * 100, 1)

        # Quadrant colour (Flask palette: c-pos / c-neg / c-warn / blue)
        if x100 >= 50 and y100 >= 50:
            color = "#22c55e"          # Leading — emerald
        elif x100 < 50 and y100 >= 50:
            color = "#3b82f6"          # Improving — blue
        elif x100 >= 50 and y100 < 50:
            color = "#f59e0b"          # Weakening — amber
        else:
            color = "#ef4444"          # Lagging — rose

        szn_g = pt["szn_w"] + pt["szn_l"]
        rec_g = pt["rec_w"] + pt["rec_l"]
        tooltip_html = (
            f"<b style='font-size:13px'>{pt['name']}</b><br/>"
            f"Season {ml_lbl}: {pt['szn_w']}-{pt['szn_l']}"
            + (f" ({round(y100)}%)" if szn_g else "")
            + f"<br/>Recent L14: {pt['rec_w']}-{pt['rec_l']}"
            + (f" ({round(x100)}%)" if rec_g else "")
        )

        scatter_data.append({
            "value":     [x100, y100],
            "name":      pt["abbr"],
            "itemStyle": {
                "color":       color,
                "borderColor": "rgba(0,0,0,0.2)",
                "borderWidth": 1,
            },
            "tooltip":   {"formatter": tooltip_html},
        })

    _zones = [
        [
            {"name": "Leading",   "xAxis": 50, "yAxis": 50,
             "itemStyle": {"color": "rgba(34,197,94,0.09)"},
             "label": {"position": "insideTopRight",
                       "color": "rgba(34,197,94,0.45)",
                       "fontSize": 11, "fontWeight": "700", "fontStyle": "italic"}},
            {"xAxis": 100, "yAxis": 100},
        ],
        [
            {"name": "Improving", "xAxis": 0, "yAxis": 50,
             "itemStyle": {"color": "rgba(59,130,246,0.07)"},
             "label": {"position": "insideTopLeft",
                       "color": "rgba(59,130,246,0.45)",
                       "fontSize": 11, "fontWeight": "700", "fontStyle": "italic"}},
            {"xAxis": 50, "yAxis": 100},
        ],
        [
            {"name": "Weakening", "xAxis": 50, "yAxis": 0,
             "itemStyle": {"color": "rgba(245,158,11,0.08)"},
             "label": {"position": "insideBottomRight",
                       "color": "rgba(245,158,11,0.45)",
                       "fontSize": 11, "fontWeight": "700", "fontStyle": "italic"}},
            {"xAxis": 100, "yAxis": 50},
        ],
        [
            {"name": "Lagging",   "xAxis": 0, "yAxis": 0,
             "itemStyle": {"color": "rgba(239,68,68,0.08)"},
             "label": {"position": "insideBottomLeft",
                       "color": "rgba(239,68,68,0.45)",
                       "fontSize": 11, "fontWeight": "700", "fontStyle": "italic"}},
            {"xAxis": 50, "yAxis": 50},
        ],
    ]

    _axis_common = {
        "type": "value",
        "min": 0, "max": 100,
        "splitLine": {"show": False},
        "axisLine":  {"lineStyle": {"color": "#2a3040"}},
        "axisTick":  {"show": False},
        "axisLabel": {"color": "#94a3b8", "fontSize": 9,
                      "formatter": "{value}%"},
    }

    return {
        "backgroundColor": "transparent",
        "grid": {"left": "54px", "right": "24px", "top": "20px", "bottom": "48px"},
        "xAxis": {
            **_axis_common,
            "name": x_name, "nameLocation": "middle", "nameGap": 28,
            "nameTextStyle": {"color": "#94a3b8", "fontSize": 10},
        },
        "yAxis": {
            **_axis_common,
            "name": y_name, "nameLocation": "middle", "nameGap": 42,
            "nameTextStyle": {"color": "#94a3b8", "fontSize": 10},
        },
        "tooltip": {
            "trigger":         "item",
            "backgroundColor": "#1e2436",
            "borderColor":     "#2a3040",
            "textStyle":       {"color": "#e2e8f0", "fontSize": 12},
            "padding":         [8, 12],
        },
        "series": [
            {
                "type":   "scatter",
                "data":   [],
                "silent": True,
                "markArea": {"silent": True, "label": {"show": True}, "data": _zones},
                "markLine": {
                    "silent": True, "symbol": "none",
                    "lineStyle": {"color": "#2a3040", "width": 1, "type": "solid"},
                    "label": {"show": False},
                    "data": [
                        [{"xAxis": 50, "yAxis": 0}, {"xAxis": 50, "yAxis": 100}],
                        [{"xAxis": 0, "yAxis": 50}, {"xAxis": 100, "yAxis": 50}],
                    ],
                },
            },
            {
                "type":       "scatter",
                "symbolSize": 28,
                "data":       scatter_data,
                "label": {
                    "show": True, "position": "inside", "formatter": "{b}",
                    "color": "#ffffff", "fontSize": 8, "fontWeight": "700",
                    "textShadowBlur": 3,
                    "textShadowColor": "rgba(0,0,0,0.9)",
                },
                "emphasis": {
                    "scale": True,
                    "itemStyle": {"shadowBlur": 10,
                                  "shadowColor": "rgba(0,0,0,0.4)"},
                },
            },
        ],
    }


def _home_rotation_data(sport: str = "mlb", metric: str = "ml") -> dict:
    """Fetch quadrant points and shape the ECharts opts for one sport+metric.

    Returns {"sport", "metric", "count", "opts", "empty"} so the same dict
    type covers both the inline initial render and the /api/home/rotation
    JSON response.  team_rotation_cache already TTL-caches the underlying
    network fetch (1 hr); we don't wrap it again here."""
    sport  = (sport  or "mlb").lower()
    metric = (metric or "ml").lower()
    if sport  not in ("mlb", "wnba"):
        sport = "mlb"
    if metric not in ("ml", "ats", "ou"):
        metric = "ml"

    try:
        from src.team_rotation_cache import get_rotation_data
        points = get_rotation_data(sport=sport, metric=metric) or []
    except Exception:                                                      # noqa: BLE001
        points = []

    opts = _home_rotation_opts(points, metric, sport) if points else None
    return {
        "sport":  sport,
        "metric": metric,
        "count":  len(points),
        "opts":   opts,
        "empty":  not points,
    }


def _home_rotation_block() -> dict:
    """Initial-render combo for the rotation section.  Defaults to MLB+ML;
    falls back to WNBA when MLB has no analysis results and WNBA does (same
    auto-default rule the News and Games sections use)."""
    try:
        wnba_active = bool((_wnba_analysis_state or {}).get("results"))
        mlb_active  = bool((_analysis_state       or {}).get("results"))
    except Exception:                                                      # noqa: BLE001
        wnba_active = mlb_active = False
    default_sport = "wnba" if (wnba_active and not mlb_active) else "mlb"
    return _home_rotation_data(sport=default_sport, metric="ml")


def _home_heatmap_rows(sport: str = "mlb", metric: str = "ml") -> list[dict]:
    """Season Heatmap rows for the home page -- shape pages/home.py's
    _heatmap_table_html into a list the Jinja template can iterate.
    Phase-2a ships default (MLB + ML); metric/sport toggles deferred."""
    try:
        from src.team_rotation_cache import get_rotation_data as _grd
        raw = _grd(sport=sport, metric=metric) or []
    except Exception:                                                      # noqa: BLE001
        return []
    points = sorted(
        [p for p in raw
         if int(p.get("szn_w") or 0) + int(p.get("szn_l") or 0) > 0],
        key=lambda p: -float(p.get("y") or 0),
    )
    def _color(pct100: float) -> str:
        if pct100 >= 60: return "pos"
        if pct100 >= 50: return "warn"
        return "neg"
    out: list[dict] = []
    for rank, pt in enumerate(points, start=1):
        pct100 = float(pt.get("y") or 0) * 100
        out.append({
            "rank":      rank,
            "name":      pt.get("name") or "",
            "wl":        f"{int(pt.get('szn_w') or 0)}-{int(pt.get('szn_l') or 0)}",
            "pct_str":   f"{pct100:.1f}%",
            "bar_color": _color(pct100),
            "bar_width": f"{min(pct100, 100):.1f}%",
        })
    return out


def _home_view_model() -> dict:
    """Shape all data for templates/home.html.  Returns a single dict that
    render_template unpacks directly -- no logic in the template."""
    import pages.home_stats as hs

    # ── Chips ────────────────────────────────────────────────────────────────
    try:
        settings = _load_model_settings()
    except Exception:                                                      # noqa: BLE001
        settings = {}
    show_overall = bool(settings.get("show_overall_chip", True))

    try:
        overall = hs.overall_record(None)
    except Exception:                                                      # noqa: BLE001
        overall = {"wins": 0, "losses": 0, "pct": None}
    try:
        props = hs.props_record(None)
    except Exception:                                                      # noqa: BLE001
        props = {"wins": 0, "losses": 0, "pct": None}
    try:
        best_model = hs.best_classifier(None)
    except Exception:                                                      # noqa: BLE001
        best_model = None
    try:
        best_bet = hs.best_bet_type(None)
    except Exception:                                                      # noqa: BLE001
        best_bet = None

    def _pct_s(d: dict | None) -> str:
        if not d:
            return "—"
        p = d.get("pct")
        return f"{p * 100:.0f}%" if p is not None else "—"

    def _color(pct) -> str:
        if pct is None:
            return "dim"
        p = float(pct) * 100
        if p > 55:
            return "pos"
        if p < 45:
            return "neg"
        return "warn"

    chips = []
    if show_overall:
        chips.append({
            "label":  "GAME MODELS",
            "main":   f"{overall['wins']}-{overall['losses']}",
            "suffix": _pct_s(overall),
            "color":  _color(overall.get("pct")),
        })
    chips.append({
        "label":  "PROPS MODELS",
        "main":   f"{props.get('wins', 0)}-{props.get('losses', 0)}",
        "suffix": _pct_s(props),
        "color":  _color(props.get("pct")),
    })
    if best_model:
        chips.append({
            "label":  "BEST GAME MODEL",
            "main":   best_model["model"],
            "suffix": f"{best_model['pct'] * 100:.0f}%",
            "color":  _color(best_model["pct"]),
        })
    else:
        chips.append({"label": "BEST GAME MODEL", "main": "—",
                      "suffix": "Insufficient data", "color": "dim"})
    if best_bet:
        chips.append({
            "label":  "BEST PROP MODEL",
            "main":   best_bet["label"],
            "suffix": f"{best_bet['wins']}-{best_bet['losses']}  {best_bet['pct'] * 100:.0f}%",
            "color":  _color(best_bet["pct"]),
        })
    else:
        chips.append({"label": "BEST PROP MODEL", "main": "—",
                      "suffix": "Insufficient data", "color": "dim"})

    # ── Today's games stub ───────────────────────────────────────────────────
    stubs = []
    for g in _home_stub_games():
        away = (g.get("away_team") or "").strip() or "TBD"
        home = (g.get("home_team") or "").strip() or "TBD"
        is_live = bool(
            g.get("is_live")
            or g.get("status") == "Live"
            or (g.get("coded_status") or "") == "I"
        )
        stubs.append({
            "away":       away,
            "home":       home,
            "is_live":    is_live,
            "away_score": g.get("away_score"),
            "home_score": g.get("home_score"),
            "game_time":  _home_fmt_game_time(g.get("commence_time")),
        })

    # ── EV scan ──────────────────────────────────────────────────────────────
    ev_min       = float(getattr(sys.modules[__name__], "EV_MIN_EDGE", _HOME_EV_MIN_EDGE))
    all_games    = _home_all_serialized_games()
    upcoming     = _home_filter_upcoming(all_games)
    all_value    = hs.enumerate_value_picks(upcoming, min_edge=0.0)
    ev_rows_raw  = [r for r in all_value if float(r.get("edge") or 0) >= ev_min]
    ev_rows_raw.sort(key=lambda r: float(r.get("edge") or 0), reverse=True)

    ev_empty_reason = None
    if not ev_rows_raw:
        if not all_games:
            ev_empty_reason = "Analysis pipeline hasn't run yet today — visit Admin to trigger a run."
        elif not upcoming:
            ev_empty_reason = "Today's games have already started — picks will refresh tonight."
        elif all_value:
            ev_empty_reason = (f"{len(all_value)} value pick(s) today, but none reach the "
                               f"edge ≥ {ev_min:.0%} cutoff.")
        else:
            ev_empty_reason = f"No picks with edge ≥ {ev_min:.0%} found in today's slate."

    def _shape_card(r: dict) -> dict:
        edge_pct = float(r.get("edge") or 0) * 100
        return {
            "matchup":   r["matchup"],
            "pick":      r["pick"],
            "edge_s":    f"+{edge_pct:.1f}% Edge",
            "edge_pct":  edge_pct,
            "prob_pct":  float(r.get("prob") or 0) * 100,
            "sport":     r.get("sport", "mlb"),
            "game_id":   r.get("game_id"),
            "away_full": r.get("away_full", ""),
            "home_full": r.get("home_full", ""),
        }

    ev_rows = [_shape_card(r) for r in ev_rows_raw]

    # ── Confidence carousel ──────────────────────────────────────────────────
    conf_rows_raw = hs.enumerate_value_picks(upcoming, min_edge=_HOME_CONFIDENCE_MIN)
    conf_rows_raw.sort(key=lambda r: float(r.get("prob") or 0), reverse=True)
    conf_rows_raw = conf_rows_raw[:10]

    conf_empty_reason = None
    if not conf_rows_raw:
        if not all_games:
            conf_empty_reason = "Analysis pipeline hasn't run yet today."
        elif not upcoming:
            conf_empty_reason = "Today's games have already started."
        else:
            conf_empty_reason = "No positive-edge picks yet."

    conf_rows = [_shape_card(r) for r in conf_rows_raw]

    # ── News (Phase-2b: MLB primary, WNBA only when MLB inactive) ───────────
    news_items, news_sport = _home_news_items()
    news_tag_label = {"mlb": "MLB", "wnba": "WNBA"}.get(news_sport, news_sport.upper())

    # ── Games table (Phase-2c: both sports pre-rendered + client toggle) ────
    try:
        games = _home_games_block()
    except Exception:                                                      # noqa: BLE001
        games = {"default_sport": "mlb",
                 "mlb":  {"upcoming": [], "final": []},
                 "wnba": {"upcoming": [], "final": []}}

    # ── Team Rotation (Phase-2d: default combo inline, toggles via /api) ────
    try:
        rotation = _home_rotation_block()
    except Exception:                                                      # noqa: BLE001
        rotation = {"sport": "mlb", "metric": "ml", "count": 0,
                    "opts": None, "empty": True}

    # ── Season Heatmap (Phase-2a: default MLB + ML, no toggles) ─────────────
    heatmap_rows = _home_heatmap_rows(sport="mlb", metric="ml")

    # ── Model Performance (Phase-2b: ensemble combined, finished picks) ─────
    try:
        import pages.home_stats as _hs_mp
        perf = _hs_mp.model_performance(None)
    except Exception:                                                      # noqa: BLE001
        perf = {"wins": 0, "losses": 0, "pct": None}
    perf_pct      = perf.get("pct")
    perf_pct_str  = f"{perf_pct * 100:.1f}%" if perf_pct is not None else "—"
    perf_color    = _color(perf_pct)
    perf_record   = f"{perf.get('wins', 0)}-{perf.get('losses', 0)}"

    return {
        "chips":              chips,
        "stubs":              stubs,
        "ev_rows":            ev_rows,
        "ev_min_pct":         f"{ev_min:.0%}",
        "ev_count":           len(ev_rows),
        "ev_empty_reason":    ev_empty_reason,
        "conf_rows":          conf_rows,
        "conf_empty_reason":  conf_empty_reason,
        "news_items":         news_items,
        "news_sport":         news_sport,
        "news_tag_label":     news_tag_label,
        "news_count":         len(news_items),
        "heatmap_rows":       heatmap_rows,
        "heatmap_count":      len(heatmap_rows),
        "perf_pct_str":       perf_pct_str,
        "perf_record":        perf_record,
        "perf_color":         perf_color,
        "games":              games,
        "rotation":           rotation,
    }


@app.route("/")
def home():
    """Phase-1 Flask home page.  Mirrors pages/home.py's chip + EV-scan +
    confidence-carousel sections via templates/home.html.  Graceful fallback
    on any view-model error so the page always renders."""
    import sys, traceback
    print("[HOME] route hit", flush=True, file=sys.stderr)
    try:
        vm = _home_view_model()
        print(
            f"[HOME] vm ok -- chips={len(vm.get('chips', []))} "
            f"stubs={len(vm.get('stubs', []))} "
            f"ev_rows={len(vm.get('ev_rows', []))} "
            f"conf_rows={len(vm.get('conf_rows', []))} "
            f"ev_empty={vm.get('ev_empty_reason')!r}",
            flush=True, file=sys.stderr,
        )
    except Exception:                                                      # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        vm = {"chips": [], "stubs": [], "ev_rows": [], "ev_min_pct": "3%",
              "ev_count": 0, "ev_empty_reason": "Error loading data.",
              "conf_rows": [], "conf_empty_reason": "Error loading data.",
              "news_items": [], "news_sport": "mlb", "news_tag_label": "MLB",
              "news_count": 0,
              "heatmap_rows": [], "heatmap_count": 0,
              "perf_pct_str": "—", "perf_record": "0-0", "perf_color": "dim",
              "games": {"default_sport": "mlb",
                        "mlb":  {"upcoming": [], "final": []},
                        "wnba": {"upcoming": [], "final": []}},
              "rotation": {"sport": "mlb", "metric": "ml", "count": 0,
                           "opts": None, "empty": True}}
    return render_template("home.html", **vm)


@app.route("/api/home/rotation", methods=["GET"])
def api_home_rotation():
    """JSON endpoint for the home Team Rotation chart's sport/metric toggle.

    Returns the same shape as _home_rotation_block() so the client just calls
    chart.setOption(payload.opts, true) on success, or swaps in the empty
    state when payload.empty is true."""
    sport  = (request.args.get("sport")  or "mlb").lower()
    metric = (request.args.get("metric") or "ml").lower()
    return jsonify(_home_rotation_data(sport=sport, metric=metric))


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
        _logger.warning("mlb proxy URLError: %s", exc)
        return jsonify({"dates": []}), 200
    except Exception as exc:
        _logger.warning("mlb proxy error: %s", exc)
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











@app.route("/api/wnba/schedule", methods=["GET"])
def wnba_schedule_proxy():
    """
    Server-side proxy for ESPN's WNBA scoreboard.  Same contract as the MLB
    proxy: date=YYYY-MM-DD (required), hydrate=linescore (optional → 30 s TTL).
    The frontend reads it through the same JSON envelope shape as MLB.
    """
    import time as _time
    import urllib.request as _urlreq
    import urllib.error  as _urlerr

    date_str = request.args.get("date", "").strip()
    hydrate  = request.args.get("hydrate", "").strip()

    if not date_str:
        return jsonify({"dates": [], "error": "date param required"}), 400

    # ESPN expects YYYYMMDD without dashes
    espn_date = date_str.replace("-", "")
    is_linescore = hydrate == "linescore"
    cache_key    = f"wnba_schedule_{date_str}_{hydrate}"

    if is_linescore:
        entry = _wnba_linescore_mem.get(cache_key)
        if entry and (_time.time() - entry[0]) < _LINESCORE_TTL:
            return jsonify(entry[1])
    else:
        cached = _cache.get(cache_key, ttl=3600)
        if cached is not None:
            return jsonify(cached)

    url = f"{_ESPN_WNBA_BASE}/scoreboard?dates={espn_date}"
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except _urlerr.URLError as exc:
        _logger.warning("wnba proxy URLError: %s", exc)
        return jsonify({"dates": []}), 200
    except Exception as exc:
        _logger.warning("wnba proxy error: %s", exc)
        return jsonify({"dates": []}), 200

    data = _normalize_espn_wnba_scoreboard(raw)

    if is_linescore:
        _wnba_linescore_mem[cache_key] = (_time.time(), data)
    else:
        try:
            _cache.set(cache_key, data)
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    return jsonify(data)




















def _picks_index_for_today(sport: str) -> tuple[dict[str, dict], dict[tuple[str, str, str], dict]]:
    """When the user navigates to today's date the freshest model picks
    live in the in-process _analysis_state.  Returns two indexes:

      by_id     -- {game_id: serialized}  (Odds API id; legacy lookup)
      by_match  -- {(home_key, away_key, et_date): serialized}
                   (team-name + ET-date composite key)

    The schedule endpoint uses MLB Stats API gamePk ids (e.g.
    "824274") and the Odds API uses opaque ids like
    "427339d860a9d7bf7b2075fa02850c56", so a pure id-based join always
    misses.  The composite key lets schedule rows find their matching
    analysis result without depending on either API's id scheme.
    """
    state = _wnba_analysis_state if sport == "wnba" else _analysis_state
    by_id: dict[str, dict] = {}
    by_match: dict[tuple[str, str, str], dict] = {}
    for r in (state.get("results") or []):
        gid = (r.get("game") or {}).get("id") or r.get("game_id") or r.get("id")
        if gid:
            by_id[str(gid)] = r
        # Team-name + ET-date composite key.  Pull from r["game"] for
        # raw analysis dicts and from the flat keys for serialized
        # passthrough rows (snapshot hydration path).
        game = r.get("game") or {}
        home = game.get("home_team") or r.get("home_team") or ""
        away = game.get("away_team") or r.get("away_team") or ""
        ct   = game.get("commence_time") or r.get("commence_time") or ""
        et_d = _game_et_date(ct) or ""
        if home and away and et_d:
            by_match[(_team_key(home), _team_key(away), et_d)] = r
    return by_id, by_match


def _picks_index_for_historical(sport: str, date_str: str) -> dict[str, dict]:
    """For past dates the picks live in the ledger history as settled
    bets.  Build {game_id: list[history_row]} so multiple bet_types on
    the same game survive the join."""
    out: dict[str, list[dict]] = {}
    path = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
    try:
        led = Ledger(path=path, starting_bankroll=1000.0)
    except Exception:                                                     # noqa: BLE001
        return {}
    for h in (led.data.get("history") or []):
        gid = str(h.get("game_id") or "")
        ct  = (h.get("commence_time") or "")[:10]
        if not gid or ct != date_str:
            continue
        out.setdefault(gid, []).append(h)
    return {k: {"history_rows": v} for k, v in out.items()}








def _sched_live_fields(g: dict) -> dict:
    """Subset of a normalized schedule game describing its live state +
    score.  Stashed on each card row as ``_sched`` so the game card can
    show a live indicator + score straight from the schedule data, even
    when the separate live-score cache misses (e.g. team-name key
    mismatch).  No extra API call -- these values are already in the
    schedule response."""
    return {
        "is_live":        g.get("is_live"),
        "status":         g.get("status"),
        "coded_status":   g.get("coded_status"),
        "home_score":     g.get("home_score"),
        "away_score":     g.get("away_score"),
        "inning_ordinal": g.get("inning_ordinal"),
        "is_top_inning":  g.get("is_top_inning"),
        "balls":          g.get("balls"),
        "strikes":        g.get("strikes"),
        "outs":           g.get("outs"),
    }


@app.route("/api/schedule/<sport>", methods=["GET"])
def schedule_for_date(sport: str):
    """Full slate for one sport on one ET date.

    Query params:
      date  YYYY-MM-DD  defaults to today_et
    """
    sport = (sport or "mlb").lower()
    if sport not in ("mlb", "wnba"):
        return jsonify({"error": f"unknown sport: {sport}"}), 400
    date_str = (request.args.get("date") or _today_et()).strip()

    games = _fetch_raw_schedule(sport, date_str)

    # Join with picks based on whether date is past / present / future.
    is_today = (date_str == _today_et())
    if is_today:
        # Two indexes returned: legacy by-id (Odds API ids), and the
        # team-name + ET-date composite that bridges MLB statsapi /
        # ESPN ids to Odds API ids.
        picks_by_id, picks_by_match = _picks_index_for_today(sport)
        picks_hist: dict[str, dict] = {}
    else:
        picks_by_id = {}
        picks_by_match = {}
        picks_hist = _picks_index_for_historical(sport, date_str)

    # Join hit counters -- logged once after the loop so the deploy log
    # makes it obvious whether the schedule -> analysis merge actually
    # found anything.  When `matched_by_match > 0 and matched_by_id ==
    # 0` the ID-only path was failing as suspected (Odds API ids vs
    # MLB statsapi gamePks).
    matched_by_id = 0
    matched_by_match = 0
    fell_through_no_odds = 0

    # For today, serialize so the frontend sees the same flat dict shape
    # /api/analyze emits; for historical, leave history_rows inline so
    # the UI can render WIN/LOST badges from the result + model_pnl.
    out_games: list[dict] = []
    for g in games:
        gid = str(g.get("id") or "")
        pick_entry = None
        match_source = ""
        if is_today:
            pick_entry = picks_by_id.get(gid)
            if pick_entry is not None:
                matched_by_id += 1
                match_source = "id"
            else:
                # Composite-key fallback: team names + ET commence date.
                key = (
                    _team_key(g.get("home_team")),
                    _team_key(g.get("away_team")),
                    _game_et_date(g.get("commence_time", "")) or "",
                )
                pick_entry = picks_by_match.get(key)
                if pick_entry is not None:
                    matched_by_match += 1
                    match_source = "team_date"
        else:
            pick_entry = picks_hist.get(gid)
            if pick_entry is not None:
                match_source = "id_hist"

        if is_today and pick_entry:
            try:
                bankroll = float((
                    _wnba_analysis_state if sport == "wnba"
                    else _analysis_state
                ).get("bankroll") or 1000.0)
                if sport == "mlb":
                    serialized = _serialize(pick_entry, bankroll, "mlb", bankroll)
                else:
                    serialized = _serialize_wnba(pick_entry, bankroll, bankroll)
                # Carry the schedule status + final score so the UI's
                # live-score lookups still work for in-progress / finished
                # games on the same date.
                serialized["_status"] = g.get("status")
                serialized["_sched"] = _sched_live_fields(g)
                serialized["_has_odds"] = True
                # _data_source lets game_card.render log which path each
                # card took -- "analysis_id" (legacy id match) or
                # "analysis_team_date" (composite match) tells us the
                # ID-mismatch bug is fixed in production.
                serialized["_data_source"] = f"analysis_{match_source}"
                # Force the schedule's stable id onto the serialized
                # row so MLB statsapi gamePks survive the join.  The
                # detail page already looks up by both keys, but pinning
                # the schedule id here keeps Track + live-score lookups
                # consistent across the slate and the detail view.
                serialized["_schedule_id"] = gid
                out_games.append(serialized)
                continue
            except Exception:                                             # noqa: BLE001
                pass
        fell_through_no_odds += 1

        # No model pick available for this game on this date.  Emit
        # a sparse row + try to attach model-only predictions so the
        # UI can render Predicted Winner / Run Line Prediction /
        # Projected Total instead of the bare "No Odds Available"
        # notice.  Predictions are skipped for past dates -- the
        # outcome is already known -- and for future dates where
        # the model would just be guessing without any market
        # signal anyway (the predictor needs current-season team
        # stats which the GameStore only loads for dates in range).
        row = {
            "id":            gid,
            "game_id":       gid,
            "home_team":     g.get("home_team"),
            "away_team":     g.get("away_team"),
            "commence_time": g.get("commence_time"),
            "_status":       g.get("status"),
            "_sched":        _sched_live_fields(g),
            "_no_odds":      True,
            "_data_source":  "schedule_stub",
        }
        if g.get("home_score") is not None and g.get("away_score") is not None:
            row["_final_score"] = {
                "home": g["home_score"], "away": g["away_score"],
            }
        if pick_entry and pick_entry.get("history_rows"):
            row["_settled_rows"] = pick_entry["history_rows"]
        # Only attempt the no-odds predict for today + future (no point
        # for past completed games -- the score is the answer).
        # Order: cached pre-prediction (from midnight prefetch) first,
        # then on-demand live predict if the cache misses.  Writes the
        # on-demand result back to the cache so subsequent requests hit
        # the fast path.
        if date_str >= _today_et():
            cached_preds_for_date = _read_no_odds_predictions(sport, date_str)
            cached_pred = cached_preds_for_date.get(gid) if cached_preds_for_date else None
            if cached_pred:
                row["_model_prediction"] = cached_pred
                row["_data_source"] = "no_odds_cached_prediction"
            else:
                try:
                    model_pred = _predict_no_odds_game(sport, g)
                    if model_pred is not None:
                        row["_model_prediction"] = model_pred
                        row["_data_source"] = "no_odds_live_prediction"
                        # Write back to cache so the next request hits
                        # the fast path (uses fresh cached_preds_for_date
                        # so we don't double-read on a busy endpoint).
                        cached_preds_for_date[gid] = model_pred
                        _write_no_odds_predictions(sport, date_str, cached_preds_for_date)
                except Exception as exc:                                       # noqa: BLE001
                    _eprint(f"schedule {sport} {gid}: no-odds predict skipped: {exc}")
        out_games.append(row)

    if is_today:
        _eprint(
            f"SCHEDULE JOIN [{sport.upper()}] date={date_str} "
            f"schedule_games={len(games)} "
            f"matched_by_id={matched_by_id} "
            f"matched_by_team_date={matched_by_match} "
            f"fell_through_no_odds={fell_through_no_odds}"
        )

    return jsonify({
        "sport":    sport,
        "date":     date_str,
        "is_today": is_today,
        "games":    out_games,
    })


def _prefetch_schedules_next_n_days(n: int = 7) -> dict:
    """Warm the schedule cache for today + the next n-1 days across
    both sports.  Used by the on-demand schedule endpoint for forward
    date navigation so user moves through the date nav hit the cache
    instead of a live API call.  (The nightly cycle's JOB 3 prefetches
    TODAY only via _fetch_raw_schedule -- see _run_job3_games_prefetch.)

    Returns a summary dict for logging.  Best-effort: per-date / per-
    sport errors are caught and counted, never propagated."""
    from datetime import date as _date, timedelta as _td

    start = _date.fromisoformat(_today_et())
    summary: dict[str, dict] = {"mlb": {}, "wnba": {}}
    for sport in ("mlb", "wnba"):
        for i in range(n):
            d = (start + _td(days=i)).isoformat()
            try:
                games = _fetch_raw_schedule(sport, d)
                summary[sport][d] = len(games)
            except Exception as exc:                                      # noqa: BLE001
                summary[sport][d] = f"ERR: {type(exc).__name__}"
    return summary

















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
        return _redact(f"CRASH in _today_et_str(): {exc}\n{traceback.format_exc()}")

    try:
        return _run_live_score_debug_inner(label, date_str, lines, sep, L)
    except Exception as exc:
        tb = _redact(traceback.format_exc())
        _exc_msg = _redact(str(exc))
        _debug_print(f"[live-debug] CRASH: {_exc_msg}\n{tb}")
        lines.append(f"\nCRASH: {_exc_msg}\n{tb}")
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
        tb = _redact(traceback.format_exc())
        _debug_print(f"[debug-endpoint] CRASHED: {_redact(str(exc))}\n{tb}")
        return jsonify({
            "report": f"ERROR: {_redact(str(exc))}\n\nTraceback:\n{tb}",
            "log_file": str(_DEBUG_LOG.resolve()),
            "error": True,
        })


@app.route("/api/snapshot", methods=["GET"])
def get_snapshot():
    """Return today's locked pre-game snapshot, or {exists: false} if none."""
    snap = _read_daily_snapshot()
    if not _snapshot_is_today(snap):
        return jsonify({"exists": False})
    return jsonify({"exists": True, **snap})


@app.route("/api/meta-consensus/run", methods=["POST"])
def run_meta_consensus_endpoint():
    """Manually trigger the Meta-Consensus job (compound-beta batched review)
    without waiting for the 8:30 AM schedule.  Returns the consensus summary."""
    try:
        res = _run_meta_consensus_job()
        status = 200 if "error" not in res else 500
        return jsonify(res), status
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/api/init", methods=["GET"])
def init_analysis():
    """Return today's cached analysis for auto-load on startup. No API calls."""
    try:
        # Snapshot takes priority — it's the permanent pre-game record for today.
        _snap = _read_daily_snapshot()
        if _snapshot_is_today(_snap) and _snap.get("mlb"):
            _sp = _snap["mlb"]
            _at = _sp.get("analyzed_at")
            if _at and _analysis_state.get("last_analyzed_at") is None:
                try:
                    _analysis_state["last_analyzed_at"] = datetime.fromisoformat(_at)
                except Exception as _exc:
                    logging.warning("Suppressed exception in %s: %s", __name__, _exc)
            return jsonify({
                "has_predictions": True,
                "snapshot":        True,
                "analyzed_at":     _at,
                "sport":           "mlb",
                "games_loaded":    _sp.get("games_loaded", 0),
                "cv_accuracy":     _sp.get("cv_accuracy"),
                "lr_cv_accuracy":  _sp.get("lr_cv_accuracy"),
                "nn_val_accuracy": _sp.get("nn_val_accuracy"),
                "results":         _sp.get("results", []),
                "parlays":         _sp.get("parlays", {}),
            })

        # Always read the lightweight timestamp file so we can show the real
        # last-run time even when the analysis cache is stale or absent.
        _ts_store  = _read_analysis_timestamps()
        _mlb_stamp = _ts_store.get("mlb", {})
        _saved_at  = _mlb_stamp.get("analyzed_at")   # ISO string | None

        # Restore in-memory state from timestamp file when app just restarted.
        if _saved_at and _analysis_state.get("last_analyzed_at") is None:
            try:
                _analysis_state["last_analyzed_at"] = datetime.fromisoformat(_saved_at)
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        if not _ANALYSIS_CACHE_FILE.exists():
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})
        payload = json.loads(_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
        today   = _today_et()
        if payload.get("date") != today:
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})

        # Cache is current — authoritative timestamp comes from the cache payload.
        _at = payload.get("analyzed_at") or _saved_at
        if _at and _analysis_state.get("last_analyzed_at") is None:
            try:
                _analysis_state["last_analyzed_at"] = datetime.fromisoformat(_at)
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        _results = _filter_stale_games(payload.get("results", []))
        return jsonify({
            "has_predictions": bool(_results),
            "analyzed_at":     _at,
            "sport":           payload.get("sport", "mlb"),
            "games_loaded":    payload.get("games_loaded", 0),
            "cv_accuracy":     payload.get("cv_accuracy"),
            "lr_cv_accuracy":  payload.get("lr_cv_accuracy"),
            "nn_val_accuracy": payload.get("nn_val_accuracy"),
            "results":         _results,
            "parlays":         payload.get("parlays", {}),
        })
    except Exception:
        return jsonify({"has_predictions": False})


def _run_daily_picks_selection() -> None:
    """
    Run cross-sport daily picks selection.  Prefers today's ensemble picks
    (ensemble_picks_today.json) as the authoritative source; falls back to
    the in-memory analysis state only when the ensemble file has no data yet.

    Called at the end of each /api/analyze and /api/wnba/analyze route so
    picks always reflect the most-recent data from whichever sport was last
    analyzed.
    """
    # Always use in-memory results so _collect_mlb gets the raw nested structure
    # (ensemble_store returns serialized flat dicts which lack r["game"] etc.)
    settings = _load_model_settings()
    mlb_results  = (_analysis_state.get("results")      or []) if settings["mlb_enabled"]  else []
    wnba_results = (_wnba_analysis_state.get("results") or []) if settings["wnba_enabled"] else []
    if not mlb_results and not wnba_results:
        return
    try:
        mlb_ledger  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba_ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        # today_only=True so a disabled sport's prior-day picks aren't wiped.
        select_daily_picks(
            mlb_results, wnba_results, mlb_ledger, wnba_ledger,
            today_only=True, selection_mode="confidence",
        )
    except Exception as exc:
        # Daily picks selection should never crash the main analyze route
        _logger.warning("daily_picks selection failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
#  Background-worker plumbing for /api/analyze + /api/wnba/analyze
#
#  Decouples the long-running analyze pipeline (30-90 s) from the calling
#  HTTP / WebSocket lifetime.  The /start endpoints spawn a daemon thread
#  that invokes the existing analyze routes via Flask's in-process test
#  client and writes per-step progress into a module-level dict.  The UI
#  polls /api/analyze/status every 5 s instead of waiting on the slow
#  POST response.
#
#  Why threads (not the APScheduler executor):
#   - the analyze code already runs synchronously and pulls in heavy
#     ML imports per call; a thread per analyze is the minimum change
#   - daemon=True means the thread won't block container shutdown
#   - one analyze per sport at a time (re-clicking Run while a job is
#     in flight returns HTTP 409 with the current status payload)
# ─────────────────────────────────────────────────────────────────────────────

import threading as _threading_for_analyze   # local alias to avoid name shadow



def _get_analysis_progress(sport: str) -> dict:
    """Thread-safe read of one sport's progress row."""
    with _analysis_progress_lock:
        return dict(_analysis_progress.get(sport, {}))


def _set_analysis_progress(sport: str, **kwargs) -> None:
    """Thread-safe partial update of one sport's progress row."""
    with _analysis_progress_lock:
        row = _analysis_progress.setdefault(sport, {})
        row.update(kwargs)


def _record_analysis_step(sport: str, label: str) -> None:
    """Called from each route's _step() closure to mirror its log line
    into the polling payload.  Auto-increments step_num and trims label
    to keep the JSON small.  No-op if sport isn't a recognized key."""
    if sport not in _analysis_progress:
        return
    with _analysis_progress_lock:
        row = _analysis_progress[sport]
        if not row.get("running"):
            return  # only count steps while the worker is active
        row["step_num"]  = int(row.get("step_num") or 0) + 1
        row["step"]      = str(label)[:200]


def _run_analysis_worker(sport: str, body: dict) -> None:
    """Daemon-thread entry point: runs one sport's analyze via the Flask
    test client and writes status updates throughout.

    Every code path here is logged to stderr because the analyze pipeline
    is long, the thread isn't attached to a request, and the only way to
    see where it died (if it dies) is the Railway log stream.  The
    outermost handler catches BaseException so SystemExit / KeyboardInterrupt
    don't slip through silently — the thread MUST always either set
    completed_at + error or print a traceback to stderr before exiting."""
    import time as _time
    import traceback as _traceback

    print(
        f"BACKGROUND THREAD STARTED [{sport.upper()}] "
        f"thread={_threading_for_analyze.current_thread().name}",
        flush=True, file=sys.stderr,
    )

    try:
        _set_analysis_progress(
            sport,
            running=True, step="Starting analysis...",
            step_num=0, total_steps=8,
            started_at=_time.time(), completed_at=None,
            error=None, n_games=None,
        )
        print(f"WORKER [{sport.upper()}] progress dict initialized",
              flush=True, file=sys.stderr)

        path = "/api/wnba/analyze" if sport == "wnba" else "/api/analyze"

        # Push an app context for the whole worker run.  test_client.post()
        # pushes its own request context per-call, but if any upstream
        # code touches current_app / g outside the request scope (e.g.
        # logging extensions, scheduler hooks, db.session.* helpers) it
        # would NoneType-explode in a bare thread.  app_context covers
        # that.
        print(f"WORKER [{sport.upper()}] entering app_context",
              flush=True, file=sys.stderr)
        with app.app_context():
            print(f"WORKER [{sport.upper()}] app_context entered; "
                  f"building test client",
                  flush=True, file=sys.stderr)
            client = app.test_client()
            print(f"WORKER [{sport.upper()}] POSTing {path} ...",
                  flush=True, file=sys.stderr)
            resp = client.post(path, json=body or {})
            print(f"WORKER [{sport.upper()}] POST {path} -> "
                  f"HTTP {resp.status_code}",
                  flush=True, file=sys.stderr)
            try:
                data = resp.get_json(force=True, silent=True) or {}
            except Exception as exc_json:                                 # noqa: BLE001
                print(f"WORKER [{sport.upper()}] get_json failed: "
                      f"{type(exc_json).__name__}: {exc_json}",
                      flush=True, file=sys.stderr)
                data = {}

            if resp.status_code >= 400:
                err = data.get("error") or f"HTTP {resp.status_code}"
                print(f"WORKER [{sport.upper()}] analyze returned error: {err}",
                      flush=True, file=sys.stderr)
                _set_analysis_progress(
                    sport, running=False, step=f"Failed: {err}",
                    completed_at=_time.time(), error=err, n_games=0,
                )
                return

            n = len(data.get("results") or [])
            print(f"WORKER [{sport.upper()}] analyze complete -- {n} games",
                  flush=True, file=sys.stderr)
            _set_analysis_progress(
                sport, running=False,
                step=f"Complete -- {n} games analyzed",
                completed_at=_time.time(), error=None, n_games=n,
            )
            print(f"WORKER [{sport.upper()}] progress dict finalized; "
                  f"thread exiting cleanly",
                  flush=True, file=sys.stderr)

    except BaseException as exc:                                          # noqa: BLE001
        # Catches Exception, SystemExit, KeyboardInterrupt, GeneratorExit
        # — anything that could otherwise kill the thread silently.
        tb = _traceback.format_exc()
        print(
            f"WORKER [{sport.upper()}] FATAL "
            f"{type(exc).__name__}: {exc}\n{tb}",
            flush=True, file=sys.stderr,
        )
        try:
            _set_analysis_progress(
                sport, running=False,
                step=f"Failed: {type(exc).__name__}",
                completed_at=_time.time(),
                error=f"{type(exc).__name__}: {exc}", n_games=0,
            )
        except BaseException as exc_inner:                                # noqa: BLE001
            # Even the progress dict write could in theory fail — log
            # but don't re-raise.
            print(
                f"WORKER [{sport.upper()}] could not record fatal: "
                f"{type(exc_inner).__name__}: {exc_inner}",
                flush=True, file=sys.stderr,
            )
        # SystemExit etc are intentionally swallowed here so the thread
        # doesn't propagate them; the only caller is Thread.run which
        # would just log them and exit anyway.


def _start_analysis_thread(sport: str, body: dict) -> dict:
    """Spawn a worker thread for the given sport, refusing if one is
    already in flight.  Returns the payload the /start endpoint sends
    back to the UI.

    Uses daemon=False so a slow analyze in flight at shutdown gets a
    chance to finish (uvicorn's graceful-shutdown window).  The thread
    is observable on Railway as 'analyze-<sport>' in any thread dump."""
    current = _get_analysis_progress(sport)
    if current.get("running"):
        print(f"START_THREAD [{sport.upper()}] refused -- already running",
              flush=True, file=sys.stderr)
        return {
            "success": False, "started": False,
            "error":   f"{sport.upper()} analysis already running.",
            "status":  current,
            "http_status": 409,
        }

    print(f"START_THREAD [{sport.upper()}] constructing Thread...",
          flush=True, file=sys.stderr)
    th = _threading_for_analyze.Thread(
        target=_run_analysis_worker,
        args=(sport, body),
        name=f"analyze-{sport}",
        daemon=False,
    )
    print(f"START_THREAD [{sport.upper()}] calling .start() "
          f"(daemon={th.daemon})",
          flush=True, file=sys.stderr)
    th.start()
    print(f"START_THREAD [{sport.upper()}] started; "
          f"is_alive={th.is_alive()}",
          flush=True, file=sys.stderr)
    return {
        "success": True, "started": True,
        "sport":   sport,
        "status":  _get_analysis_progress(sport),
        "http_status": 202,
    }


@app.route("/api/analyze/start", methods=["POST"])
def analyze_start_mlb():
    """Spawn a background MLB analyze; return immediately."""
    body = request.get_json() or {}
    resp = _start_analysis_thread("mlb", body)
    http_status = resp.pop("http_status", 202)
    return jsonify(resp), http_status


@app.route("/api/wnba/analyze/start", methods=["POST"])
def analyze_start_wnba():
    """Spawn a background WNBA analyze; return immediately."""
    body = request.get_json() or {}
    resp = _start_analysis_thread("wnba", body)
    http_status = resp.pop("http_status", 202)
    return jsonify(resp), http_status


@app.route("/api/analyze/status", methods=["GET"])
def analyze_status():
    """Polled by the UI every ~5 s.  Returns one sport's current
    progress dict.  Cheap, no upstream traffic."""
    sport = (request.args.get("sport") or "mlb").strip().lower()
    if sport not in _analysis_progress:
        return jsonify({"error": f"unknown sport: {sport}"}), 400
    row = _get_analysis_progress(sport)
    # Derive a small "elapsed_sec" so the UI doesn't have to do the math
    # client-side.  Useful for "Running for 42s" subtitles.
    import time as _time
    started_at   = row.get("started_at")
    completed_at = row.get("completed_at")
    if started_at:
        end = completed_at or _time.time()
        row["elapsed_sec"] = max(0, int(end - started_at))
    else:
        row["elapsed_sec"] = 0
    return jsonify(row)


@app.route("/api/analyze/completions", methods=["GET"])
def analyze_completions():
    """Returns the progress dict for *both* sports in one call.
    Historically powered the cross-page completion watcher; the UI
    no longer polls it (admin buttons now run analyze synchronously
    and force ui.navigate.reload() on success), but the endpoint is
    kept for external callers / future tooling.

    Cheap, no upstream traffic -- just reads the in-process
    _analysis_progress dict under its lock.

    Payload shape:
      {
        "mlb":  {running, step, completed_at, n_games, error, ...},
        "wnba": {running, step, completed_at, n_games, error, ...},
        "now":  <unix_ts>,
      }

    The UI compares each sport's `completed_at` against a per-tab
    "last seen" marker in app.storage.tab to decide whether to fire
    the success notification on this tick.
    """
    import time as _time
    payload: dict[str, object] = {}
    for sport in ("mlb", "wnba"):
        row = _get_analysis_progress(sport)
        started_at   = row.get("started_at")
        completed_at = row.get("completed_at")
        if started_at:
            end = completed_at or _time.time()
            row["elapsed_sec"] = max(0, int(end - started_at))
        else:
            row["elapsed_sec"] = 0
        payload[sport] = row
    payload["now"] = _time.time()
    return jsonify(payload)


def _track_line_mlb(matchup: str, prediction: dict, rl_pred: dict | None,
                    totals_pred: dict | None) -> None:
    """Emit one [TRACK] stderr line per game summarizing what each
    per-classifier recorder saw.  The recorders themselves live inside
    model.predict / rl_model.predict / totals_model.predict and fire on
    every game regardless of value-pick / top-5 selection -- this line
    is the visible proof they did so for THIS particular game.

    Format:
      [TRACK] Yankees @ Red Sox |
        ML  xgb=0.732 lr=0.681 nn=0.711 |
        RL  xgb=0.612 lr=0.583 nn=0.624 |
        TOT xgb=OVER 0.561 lr=OVER 0.547 nn=OVER 0.583
    """
    def _f(v) -> str:
        try:
            return f"{float(v):.3f}"
        except (TypeError, ValueError):
            return "?"

    ml = prediction or {}
    ml_part = (
        f"ML  xgb={_f(ml.get('xgb_prob'))} "
        f"lr={_f(ml.get('lr_prob'))} "
        f"nn={_f(ml.get('nn_prob'))}"
    )

    if rl_pred:
        rl_part = (
            f"RL  xgb={_f(rl_pred.get('xgb_prob'))} "
            f"lr={_f(rl_pred.get('lr_prob'))} "
            f"nn={_f(rl_pred.get('nn_prob'))}"
        )
    else:
        rl_part = "RL  (not run)"

    if totals_pred:
        line = totals_pred.get("market_line") or totals_pred.get("total_line")
        def _dir(v) -> str:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return "?"
            if line is None:
                return "?"
            return "OVER " + _f(v) if f > float(line) else "UNDER " + _f(v)
        tot_part = (
            f"TOT xgb={_dir(totals_pred.get('xgb_predicted_total'))} "
            f"lr={_dir(totals_pred.get('lr_predicted_total'))} "
            f"nn={_dir(totals_pred.get('nn_predicted_total'))}"
        )
    else:
        tot_part = "TOT (not run)"

    _eprint(f"[TRACK] {matchup} | {ml_part} | {rl_part} | {tot_part}")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """Run the full analysis pipeline (mirrors main.py Steps 1-4 + 6)."""
    data       = request.get_json() or {}
    sport      = data.get("sport", "mlb")
    bankroll   = float(data.get("bankroll", 250))
    season     = int(data.get("season", _SEASON))
    games_lim  = int(data.get("games", 0))

    odds_key   = _ODDS_API_KEY
    sports_key = _API_SPORTS_KEY

    if not odds_key or odds_key == "your_odds_api_key_here":
        return jsonify({"error": "ODDS_API_KEY not configured in .env"}), 400
    if not sports_key or sports_key == "your_api_sports_key_here":
        return jsonify({"error": "API_SPORTS_KEY not configured in .env"}), 400

    # Step 5: health check — printed to stderr before any logic runs so even a
    # fast crash shows *something* in the logs.
    print(f"ANALYZE [{sport.upper()}] health-check: route entered, force_refresh={data.get('force_refresh')}, snapshot_enabled={_SNAPSHOT_ENABLED}", flush=True, file=sys.stderr)

    # ── Cache control params — parsed early so force_refresh can bypass snapshot ─
    force_refresh = bool(data.get("force_refresh", False))

    # ── Snapshot guard — return locked picks immediately if snapshot exists ─────
    # Bypassed when force_refresh=True (explicit re-run request).  In that case
    # the snapshot entry for this sport is cleared so a fresh one gets written
    # at the end of the new run.
    if force_refresh:
        _clear_snapshot_sport(sport)   # atomic, locked, never raises
    _asnap = _read_daily_snapshot()
    if not force_refresh and _snapshot_is_today(_asnap) and _asnap.get(sport):
        _asp = _asnap[sport]
        _analysis_state["bankroll"] = bankroll
        if _analysis_state.get("last_analyzed_at") is None:
            try:
                _analysis_state["last_analyzed_at"] = datetime.fromisoformat(
                    _asp.get("analyzed_at", "")
                )
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)
        return jsonify({
            "success":         True,
            "cached":          True,
            "snapshot":        True,
            "sport":           sport,
            "bankroll":        bankroll,
            "analyzed_at":     _asp.get("analyzed_at"),
            "results":         _asp.get("results", []),
            "parlays":         _asp.get("parlays", {}),
            "games_loaded":    _asp.get("games_loaded", 0),
            "cv_accuracy":     _asp.get("cv_accuracy"),
            "lr_cv_accuracy":  _asp.get("lr_cv_accuracy"),
            "nn_val_accuracy": _asp.get("nn_val_accuracy"),
            "model_status":    _asp.get("model_status", "snapshot"),
        })

    # Auto-settle any completed open bets before running fresh analysis
    try:
        _settle_ledger = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        _oc_settle     = OddsClient(odds_key, _cache)
        _sport_cfg     = SPORTS.get(sport, SPORTS["mlb"])
        _settle_ledger.settle(_oc_settle, _sport_cfg.odds_key)
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    # ── Cache control params from frontend ───────────────────────────────────
    # force_refresh=True  → always hit the API, ignore any cached results
    # use_cached=True     → return existing in-memory results without any API call,
    #                       even if the TTL has expired (user chose "Use Cached Data")
    # (force_refresh was already parsed above the snapshot guard — kept here for
    #  use_cached which wasn't needed earlier)
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
        _cached_ts = _analysis_state.get("last_analyzed_at")
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
            "analyzed_at":     _cached_ts.isoformat() if _cached_ts else None,
            "results":         serialized,
            "parlays":         parlays,
        })

    if sport not in SPORTS:
        print(f"ANALYZE FATAL: unknown sport {sport!r}", flush=True, file=sys.stderr)
        return jsonify({"error": f"Unknown sport: {sport}"}), 400
    sport_cfg = SPORTS[sport]

    # ── Step checkpoint helper — prints to stderr so errors appear in Railway logs ──
    def _step(label: str) -> None:
        print(f"ANALYZE [{sport.upper()}] {label}", flush=True, file=sys.stderr)
        # Mirror into the polling payload for /api/analyze/status.  No-op
        # when sport isn't a tracked key or the worker isn't running.
        _record_analysis_step(sport, label)

    try:
        _step("importing model modules")
        from src.model import BettingModel
        from src.run_line_model import RunLineModel
        from src.totals_model import TotalsModel
        from src.explainer import PredictionExplainer

        # Step 1 — season data
        _step("Step 1: loading season stats from GameStore")
        store = GameStore(
            api_key=sports_key,
            base_url=sport_cfg.api_sports_base,
            league_id=sport_cfg.league_id,
            sport_tag=sport,
            cache=_cache,
        )
        n_completed = store.load(season)
        _step(f"Step 1 done: {n_completed} completed games loaded")

        # Step 2 — feature builder (MLB only; WNBA uses its own dedicated builder)
        _step("Step 2: building MLBFeatureBuilder")
        from src.mlb_features import MLBFeatureBuilder
        fb = MLBFeatureBuilder(store)

        # Step 3 — models (moneyline + run line + totals for MLB)
        _step("Step 3: training / loading moneyline model")
        model  = BettingModel(sport_cfg)
        status = model.train_or_load(
            stats_client=store, feature_builder=fb,
            season=season, force_retrain=False,
        )
        cv_acc     = float(model.cv_accuracy)      if model.cv_accuracy      else None
        lr_cv_acc  = float(model.lr_cv_accuracy)  if model.lr_cv_accuracy  else None
        nn_val_acc = float(model.nn_val_accuracy) if model.nn_val_accuracy else None
        _step(f"Step 3 moneyline done: status={status!r}")

        rl_model = totals_model = None
        if sport == "mlb":
            _step("Step 3b: training / loading run-line model")
            rl_model = RunLineModel()
            rl_status = rl_model.train_or_load(store, fb, season)
            _logger.info("run_line model: %s", rl_status)
            _step(f"Step 3b done: run_line status={rl_status!r}")

            _step("Step 3c: training / loading totals model")
            totals_model = TotalsModel()
            tot_status = totals_model.train_or_load(store, fb, season)
            _logger.info("totals model: %s", tot_status)
            _step(f"Step 3c done: totals status={tot_status!r}")

        # Step 4 — odds (baseball_mlb only)
        _step(f"Step 4: fetching odds from Odds API  sport_key={sport_cfg.odds_key!r}")
        odds_client = OddsClient(odds_key, _cache)
        # ODDS FETCH STARTING / COMPLETE: top-level markers (grep'able
        # in Railway logs) bracketing the get_odds call so it's
        # trivially visible from the deploy log whether the call ran
        # at all and how many games came back -- the user's symptom
        # was every game showing no_odds=True even though analysis
        # supposedly completed.
        _eprint(f"ODDS FETCH STARTING [MLB] sport_key={sport_cfg.odds_key!r} "
                f"force_refresh={force_refresh}")
        # force_refresh threads from the /api/analyze request body all the
        # way down to bypass the daily Supabase cache when the user explicitly
        # asks for a fresh fetch from the admin Force Refresh button.
        games_pre_filter = odds_client.get_odds(
            sport_key=sport_cfg.odds_key, force_refresh=force_refresh,
        )
        _eprint(f"ODDS FETCH COMPLETE [MLB] returned {len(games_pre_filter)} "
                f"parsed games (pre stale-date filter)")
        _step(f"Step 4: get_odds returned {len(games_pre_filter)} parsed games "
              f"(before stale-date filter)")
        # Drop yesterday's games before any processing.  `_filter_stale_games`
        # converts each game's commence_time UTC -> ET via zoneinfo and keeps
        # only games whose ET date >= today's ET date.  If you see a big drop
        # here, check the first game's commence_time vs the today_et logged.
        today_et = _today_et()
        kept_dates: dict[str, int] = {}
        dropped_dates: dict[str, int] = {}
        # Per-game audit trail.  For each game log the raw UTC commence_time,
        # the parsed UTC + ET datetimes, the resulting ET date string, and
        # whether the stale-date filter is going to keep or drop it.  If a
        # user reports "all my games got dropped", they paste this block
        # and we can see exactly which games on which days got dropped why.
        for i, g in enumerate(games_pre_filter):
            ct = g.get("commence_time", "")
            d  = _game_et_date(ct) or "<unparsable>"
            verdict = "KEEP" if d >= today_et else "DROP"
            (kept_dates if verdict == "KEEP" else dropped_dates)[d] = \
                (kept_dates if verdict == "KEEP" else dropped_dates).get(d, 0) + 1
            try:
                from zoneinfo import ZoneInfo as _Z
                _utc_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                _et_dt  = _utc_dt.astimezone(_Z("America/New_York"))
                _step(
                    f"Step 4: game[{i:2d}] "
                    f"{(g.get('away_team') or '?')[:3]}@{(g.get('home_team') or '?')[:3]}  "
                    f"raw={ct}  utc={_utc_dt.isoformat()}  et={_et_dt.isoformat()}  "
                    f"et_date={d}  vs today_et={today_et}  -> {verdict}"
                )
            except Exception as _e:                                       # noqa: BLE001
                _step(f"Step 4: game[{i:2d}] raw={ct!r} -- could not parse: {_e}")
        games = _filter_stale_games(games_pre_filter)
        _step(f"Step 4: stale-date filter  today_et={today_et}  "
              f"kept_dates={kept_dates}  dropped_dates={dropped_dates}")
        _step(f"Step 4 done: {len(games)} games with odds")

        # Freeze pre-game odds: for started games, restore market odds from before first pitch
        _step("Step 4b: locking pre-game odds")
        games = _lock_in_pre_game_odds(games)
        # Per spec: never feed the model live in-play odds.  A game whose
        # _lock_in_pre_game_odds path couldn't find a pre-game snapshot
        # AND has already started carries _pregame_locked=False; drop
        # those from the prediction set with a single log line.
        _unsafe = [g for g in games if g.get("_pregame_locked") is False]
        if _unsafe:
            _step(f"Step 4: dropping {len(_unsafe)} game(s) with live in-play "
                  f"odds and no pre-game snapshot -- not safe for model")
            games = [g for g in games if g.get("_pregame_locked") is not False]

        if not games:
            _step("Step 4: no games found — returning empty result")
            return jsonify({
                "success": True, "no_games": True, "results": [],
                "model_status": status,
                "cv_accuracy": cv_acc, "lr_cv_accuracy": lr_cv_acc, "nn_val_accuracy": nn_val_acc,
                "games_loaded": n_completed, "sport": sport, "bankroll": bankroll,
            })

        if games_lim > 0:
            games = games[:games_lim]

        # Step 5 — load model weights then predict + explain
        _step(f"Step 5: running predictions on {len(games)} games")
        _wt_ledger    = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        model_weights = _wt_ledger.get_model_weights()

        explainer    = PredictionExplainer(sport_cfg)
        rl_explainer = PredictionExplainer(sport_cfg) if rl_model else None
        results = []
        for _gi, game in enumerate(games):
            _matchup = f"{game.get('away_team','?')} @ {game.get('home_team','?')}"
            _step(f"  game {_gi+1}/{len(games)}: {_matchup}")
            try:
                built = fb.build_for_game(game)
                if built is None:
                    _step(f"  game {_gi+1}: build_for_game returned None -- skipping")
                    continue
                feature_vec, meta = built

                _step(f"  game {_gi+1}: moneyline predict")
                prediction  = model.predict(feature_vec, weights=model_weights, game_meta=game)
                shap_result = explainer.explain(
                    feature_vec, model=model.get_raw_model(),
                    scaler=model.get_scaler(), is_trained=model.is_trained,
                    xgb_cols=model.get_xgb_cols(), xgb_names=model.get_xgb_names(),
                )

                # Run line prediction (MLB only).
                rl_pred = None
                if rl_model and rl_model.is_trained:
                    _step(f"  game {_gi+1}: run-line predict")
                    try:
                        rl_pred = rl_model.predict(
                            feature_vec, game,
                            weights=model_weights,
                            ml_prob_home    = prediction.get("xgb_prob"),
                            ml_lr_prob_home = prediction.get("lr_prob"),
                            ml_nn_prob_home = prediction.get("nn_prob"),
                        )
                        if rl_pred and rl_explainer:
                            rl_shap = rl_explainer.explain(
                                feature_vec, model=rl_model.get_raw_model(),
                                scaler=rl_model.get_scaler(), is_trained=rl_model.is_trained,
                                xgb_cols=rl_model.get_xgb_cols(), xgb_names=rl_model.get_xgb_names(),
                            )
                            rl_pred["shap"] = rl_shap
                    except Exception as _rle:
                        _eprint(f"ANALYZE [MLB] run_line.predict failed for {_matchup}: "
                                f"{type(_rle).__name__}: {_rle}")
                        rl_pred = None

                # Totals prediction
                totals_pred = None
                if totals_model and totals_model.is_trained and game.get("total_line") is not None:
                    _step(f"  game {_gi+1}: totals predict")
                    try:
                        totals_vec = fb.build_totals_from_meta(meta)
                        if totals_vec is not None:
                            totals_pred = totals_model.predict(totals_vec, game, weights=model_weights)
                    except Exception as _te:
                        _eprint(f"ANALYZE [MLB] totals.predict failed for {_matchup}: "
                                f"{type(_te).__name__}: {_te}")
                        totals_pred = None

                # Per-game tracking confirmation -- proves all three per-classifier
                # recorders fired (inside model.predict / rl_model.predict /
                # totals_model.predict) for EVERY game in the slate, not just
                # the top-5 daily picks.  If a tracker silently failed, the
                # corresponding field reads "?" instead of a probability.
                _track_line_mlb(_matchup, prediction, rl_pred, totals_pred)

                results.append({
                    "game":        game,
                    "prediction":  prediction,
                    "shap":        shap_result,
                    "meta":        meta,
                    "rl_pred":     rl_pred,
                    "totals_pred": totals_pred,
                })
            except Exception as _ge:
                _eprint(f"ANALYZE [MLB] prediction loop crashed on game {_gi+1} ({_matchup}): "
                        f"{type(_ge).__name__}: {_ge}")
                _eprint(traceback.format_exc())
                # Skip this game but continue with the rest

        _step(f"Step 5 done: {len(results)} games predicted")

        # Compute upset factor for each game (MLB only; cached 1h per team)
        if sport == "mlb":
            _step("Step 5b: computing upset factors")
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
        _step("Step 6: daily picks selection")
        _run_daily_picks_selection()

        # Step 6b — immediate settlement of any freshly-recorded picks
        # whose games are already Final.  Runs ungated by ET game hours
        # so a late-night analyze run still settles same-day instead of
        # waiting until the 30-min scheduler fires (which is gated to
        # 11 AM-2 AM ET).  Logs each settled pick to stderr; bankroll +
        # confidence-tier history + per-model trackers all update via
        # Ledger.settle()'s existing fan-out.
        _step("Step 6b: immediate settlement check")
        try:
            _settle_freshly_recorded_picks()
        except Exception as exc:                                          # noqa: BLE001
            _eprint(
                f"IMMEDIATE-SETTLE: hook failed (analyze continues): "
                f"{type(exc).__name__}: {exc}"
            )

        # Reload ledger to get current personal_starting_bankroll for serialization
        _step("Step 7: serializing results")
        _ledger_for_serial  = Ledger(path="data/ledger.json", starting_bankroll=bankroll)
        personal_starting   = _ledger_for_serial.data.get("personal_starting_bankroll", bankroll)

        # Wrap _serialize per-game so one bad game can't kill the whole batch.
        # Each failure is logged and the game is skipped rather than crashing.
        serialized = []
        for _si, _r in enumerate(results):
            try:
                serialized.append(_serialize(_r, bankroll, sport, personal_starting))
            except Exception as _se:
                _eprint(f"ANALYZE [MLB] _serialize failed on game {_si}: "
                        f"{type(_se).__name__}: {_se}")
                _eprint(traceback.format_exc())

        _step(f"Step 7 done: {len(serialized)}/{len(results)} games serialized")

        try:
            parlays = _generate_parlays(serialized, bankroll)
        except Exception as _pe:
            _eprint(f"ANALYZE [MLB] _generate_parlays failed: {type(_pe).__name__}: {_pe}")
            parlays = {}

        _ts = datetime.now(timezone.utc)
        _analysis_state["parlays"]            = parlays
        _analysis_state["last_analyzed_at"]   = _ts
        _analysis_state["last_analysis_meta"] = {
            "games_loaded":    n_completed,
            "model_status":    status,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
        }

        _step("Step 8: saving cache and snapshot")
        _save_analysis_cache(serialized, parlays, sport, n_completed,
                             cv_acc, lr_cv_acc, nn_val_acc, analyzed_at=_ts)
        _write_analysis_timestamp("mlb", _ts)
        # Dedicated per-sport Supabase key -- written synchronously so
        # Railway log confirms it fired and the admin "Last analyzed" line
        # survives a redeploy even if the shared analysis_timestamps key
        # had a silent async write failure.
        try:
            from zoneinfo import ZoneInfo as _ZI_ts
            _ts_et_mlb = datetime.now(_ZI_ts("America/New_York")).isoformat()
        except Exception:
            _ts_et_mlb = _ts.isoformat()
        try:
            from src import db as _db_ts
            _db_ts.cache_set(
                "last_analyzed_at_mlb", "mlb", _ts.date().isoformat(),
                {"ts": _ts_et_mlb},
            )
            print(
                f"ANALYSIS-TIMESTAMP: updated mlb last_analyzed_at to {_ts_et_mlb}",
                flush=True, file=sys.stderr,
            )
        except Exception as _tse:
            print(
                f"ANALYSIS-TIMESTAMP: dedicated mlb key write failed (ignored): {_tse}",
                flush=True, file=sys.stderr,
            )
        _write_daily_snapshot(sport, {
            "results":         serialized,
            "parlays":         parlays,
            "games_loaded":    n_completed,
            "cv_accuracy":     cv_acc,
            "lr_cv_accuracy":  lr_cv_acc,
            "nn_val_accuracy": nn_val_acc,
            "model_status":    status,
        }, _ts)
        try:
            ensemble_store.save(serialized, "mlb")
        except Exception as _es:
            _eprint(f"ANALYZE [MLB] ensemble_store.save failed: {type(_es).__name__}: {_es}")
        _step(f"DONE: {len(serialized)} games serialized and saved")

        # ANALYZE COMPLETE summary -- counts games WITH odds (have a
        # pick_team) vs games that fell into the no-odds bucket.  Makes
        # it instantly obvious from Railway logs whether the Odds API
        # actually returned lines or every game ended up no-odds (which
        # is what the user was hitting).
        _with_odds = sum(1 for r in serialized if r.get("pick_team"))
        _no_odds   = len(serialized) - _with_odds
        _eprint(
            f"ANALYZE COMPLETE [MLB]: {len(serialized)} games total -- "
            f"{_with_odds} with odds, {_no_odds} no_odds"
        )

        # Tier 2 player-props refresh -- once per analyze run, gated to
        # the MLB analyze path because props_client is MLB-only.  Same
        # fire-and-forget pattern as the existing daily-picks selector
        # so a props fetch failure can't take down the main analyze
        # response.
        try:
            from src.props_client import run_tier_2_refresh as _props_tier_2
            _props_tier_2()
        except Exception as _pe:                                          # noqa: BLE001
            _eprint(f"ANALYZE [MLB] tier-2 props fetch failed (non-fatal): "
                    f"{type(_pe).__name__}: {_pe}")

        # Force a fresh re-read of today's snapshot back into the
        # in-memory _analysis_state.  The route already mutates that
        # dict in-process (line ~4279), so this is mostly a belt-and-
        # suspenders for routes that read from the disk snapshot (the
        # NiceGUI renderer calls backend.hydrate_state on every page
        # render).  Without it, the next schedule join would still
        # work, but a stale daily_snapshot.json could shadow the
        # fresh _analysis_state if the renderer ran a snapshot reload
        # mid-request.
        try:
            hydrate_state()
        except Exception as _he:                                          # noqa: BLE001
            _eprint(f"ANALYZE [MLB] post-analyze hydrate_state failed "
                    f"(non-fatal): {type(_he).__name__}: {_he}")

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
            "analyzed_at":     _ts.isoformat(),
            "results":         serialized,
            "parlays":         parlays,
            "bankroll":        bankroll,
        })

    except Exception as exc:
        # Daily Odds-API quota hit -- return a clean 429 with the counter
        # snapshot the UI uses to render the "limit reached" banner.
        # Class-name check avoids a hard import dance with src.odds_client.
        if type(exc).__name__ == "OddsApiLimitExceeded":
            try:
                from src.odds_client import odds_usage
                u = odds_usage()
            except Exception:                                             # noqa: BLE001
                u = {"count": 0, "effective_limit": 500, "limit_reached": True}
            _eprint(f"ANALYZE [{sport.upper()}] BLOCKED: Odds API daily "
                    f"limit reached ({u['count']}/{u['effective_limit']})")
            return jsonify({
                "success":       False,
                "limit_reached": True,
                "error":         (
                    f"Daily Odds API limit of {u['effective_limit']} reached, "
                    f"additional pulls require manual approval."
                ),
                "calls_today":   u["count"],
                "limit":         u["effective_limit"],
            }), 429

        # Log type+message FIRST with _eprint so it survives even if traceback
        # formatting or jsonify later fails (e.g. UnicodeEncodeError on Windows).
        # Every payload that leaves this block is run through _redact so an
        # HTTPError that embeds `?apiKey=...` can't leak into Railway logs or
        # the JSON response body.
        _exc_type = type(exc).__name__
        _exc_msg  = _redact(str(exc))
        _eprint(f"\nANALYZE [{sport.upper()}] CRASHED")
        _eprint(f"  type:    {_exc_type}")
        _eprint(f"  message: {_exc_msg}")
        try:
            _tb = _redact(traceback.format_exc())
            _eprint(f"  traceback:\n{_tb}")
        except Exception:
            _tb = f"{_exc_type}: {_exc_msg}"
        try:
            return jsonify({"error": _exc_msg, "detail": _tb, "exc_type": _exc_type}), 500
        except Exception as _je:
            _eprint(f"  jsonify also failed: {_redact(str(_je))}")
            return (
                f'{{"error": "{_exc_type}: {_exc_msg}"}}'
            ), 500, {"Content-Type": "application/json"}


@app.route("/api/refresh_models", methods=["POST"])
def refresh_models():
    """Retrain all ML models on cached data and rerun predictions. No odds/stats API calls."""
    from src.model import BettingModel
    from src.run_line_model import RunLineModel
    from src.totals_model import TotalsModel
    from src.explainer import PredictionExplainer
    data     = request.get_json() or {}
    sport    = data.get("sport", _analysis_state.get("sport", "mlb"))
    bankroll = float(data.get("bankroll", _analysis_state.get("bankroll", 250)))
    season   = int(data.get("season", _SEASON))

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
            api_key=_API_SPORTS_KEY,
            base_url=sport_cfg.api_sports_base,
            league_id=sport_cfg.league_id,
            sport_tag=sport,
            cache=_cache,
        )
        n_completed = store.load(season)

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

            prediction  = model.predict(feature_vec, weights=model_weights, game_meta=game)
            shap_result = explainer.explain(
                feature_vec, model=model.get_raw_model(),
                scaler=model.get_scaler(), is_trained=model.is_trained,
                xgb_cols=model.get_xgb_cols(), xgb_names=model.get_xgb_names(),
            )

            rl_pred = None
            if rl_model and rl_model.is_trained:
                # RL XGB AND LR are both conditional P(margin>=2 | home wins);
                # pass each one's moneyline counterpart so the joint
                # probabilities respect P_rl <= P_ml per classifier.
                rl_pred = rl_model.predict(
                    feature_vec, game,
                    weights=model_weights,
                    ml_prob_home    = prediction.get("xgb_prob"),
                    ml_lr_prob_home = prediction.get("lr_prob"),
                    ml_nn_prob_home = prediction.get("nn_prob"),
                )
                if rl_pred and rl_explainer:
                    rl_shap = rl_explainer.explain(
                        feature_vec, model=rl_model.get_raw_model(),
                        scaler=rl_model.get_scaler(), is_trained=rl_model.is_trained,
                        xgb_cols=rl_model.get_xgb_cols(), xgb_names=rl_model.get_xgb_names(),
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
        return jsonify({"error": _redact(str(exc)), "detail": _redact(traceback.format_exc())}), 500


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

        # Tier from picked-outcome probability (pure confidence, no agreement).
        _ml_hp = float(pred.get("home_win_prob", 0.5))
        ml_conf = confidence_tier_from_prob(_ml_hp if _ml_hp >= 0.5 else 1.0 - _ml_hp)

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
                "confidence_tier":   confidence_tier_from_prob(
                    float(rl.get("pick_prob", 0.5))
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


@app.route("/api/ensemble_picks", methods=["GET"])
def get_ensemble_picks():
    """Return today's ensemble picks for all sports (single source of truth)."""
    sport = request.args.get("sport")  # optional filter: "mlb" or "wnba"
    try:
        if sport:
            picks = ensemble_store.get_picks(sport)
            return jsonify({"sport": sport, "picks": picks, "count": len(picks)})
        data = ensemble_store.load()
        mlb_picks  = data["picks"].get("mlb",  [])
        wnba_picks = data["picks"].get("wnba", [])
        return jsonify({
            "date":  data.get("date", ""),
            "mlb":   {"picks": mlb_picks,  "count": len(mlb_picks)},
            "wnba":  {"picks": wnba_picks, "count": len(wnba_picks)},
        })
    except Exception as exc:
        return jsonify({"error": _redact(str(exc))}), 500


@app.route("/api/model_performance", methods=["GET"])
def get_model_performance():
    """
    Return per-model accuracy stats for the Model Performance Comparison table.
    Reads from the unified bet history (closed bets with xgb_prob/lr_prob/nn_prob)
    and computes individual model correct-call rates for XGB, LR, and NN.
    """
    try:
        from src.ledger import Ledger as _Ledger

        # Gather closed bets from both sport ledgers
        def _closed(path: str) -> list[dict]:
            try:
                return _Ledger(path=path, starting_bankroll=1000.0).data.get("history", [])
            except Exception:
                return []

        history = _closed("data/ledger.json") + _closed("data/wnba_ledger.json")

        # Also pull from the archive file if it exists
        if _ARCHIVE_PATH.exists():
            try:
                arc = json.loads(_ARCHIVE_PATH.read_text(encoding="utf-8"))
                history += arc if isinstance(arc, list) else []
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        def _model_correct(model_prob: float | None, bet_side: str, result: str) -> bool | None:
            """
            Return True/False if the model correctly called the winner.
            Returns None when model_prob is missing (skip from accuracy calc).
            result is "win" or "loss" from the ledger entry's perspective
            (i.e. relative to the *bet* side, not always the home team).
            """
            if model_prob is None:
                return None
            model_picks_home = model_prob >= 0.5
            bet_on_home = bet_side == "home"
            # bet won → the bet side won
            if result == "win":
                home_won = bet_on_home
            elif result == "loss":
                home_won = not bet_on_home
            else:
                return None
            return model_picks_home == home_won

        BET_TYPE_MAP = {
            "moneyline":       "Moneyline",
            "run_line_spread": "Run Line / Spread",
            "totals":          "Totals",
        }

        stats: dict = {
            model: {
                "overall": {"correct": 0, "total": 0},
                "by_type": {k: {"correct": 0, "total": 0} for k in BET_TYPE_MAP},
            }
            for model in ("xgb", "lr", "nn")
        }

        for bet in history:
            result = bet.get("result", "")
            if result not in ("win", "loss"):
                continue
            side     = bet.get("side", "home")
            bet_type = bet.get("bet_type", "moneyline")
            if bet_type not in BET_TYPE_MAP:
                bet_type = "moneyline"

            for model_key in ("xgb", "lr", "nn"):
                prob_field = f"{model_key}_prob"
                prob = bet.get(prob_field)
                if prob is None:
                    continue
                correct = _model_correct(float(prob), side, result)
                if correct is None:
                    continue
                stats[model_key]["overall"]["total"]   += 1
                stats[model_key]["overall"]["correct"] += int(correct)
                stats[model_key]["by_type"][bet_type]["total"]   += 1
                stats[model_key]["by_type"][bet_type]["correct"] += int(correct)

        def _pct(c: int, t: int) -> float | None:
            return round(c / t * 100, 1) if t > 0 else None

        result_out: dict = {}
        for model_key, data in stats.items():
            ov = data["overall"]
            by_type: dict = {}
            for bt, bt_data in data["by_type"].items():
                by_type[bt] = {
                    "correct":    bt_data["correct"],
                    "total":      bt_data["total"],
                    "win_pct":    _pct(bt_data["correct"], bt_data["total"]),
                    "label":      BET_TYPE_MAP[bt],
                }
            result_out[model_key] = {
                "label":    {"xgb": "XGBoost", "lr": "Logistic Regression", "nn": "Neural Net"}[model_key],
                "overall":  {
                    "correct": ov["correct"],
                    "total":   ov["total"],
                    "win_pct": _pct(ov["correct"], ov["total"]),
                },
                "by_type":  by_type,
            }

        # Recommend the model with the highest overall win_pct (min 10 bets)
        best_model = None
        best_pct   = 0.0
        for mk, md in result_out.items():
            wp = md["overall"]["win_pct"]
            total = md["overall"]["total"]
            if wp is not None and total >= 10 and wp > best_pct:
                best_pct   = wp
                best_model = mk

        return jsonify({
            "models":     result_out,
            "best_model": best_model,
            "total_bets": len(history),
        })

    except Exception as exc:
        return jsonify({"error": _redact(str(exc)), "detail": _redact(traceback.format_exc())}), 500


@app.route("/api/retrain_status", methods=["GET"])
def get_retrain_status():
    """
    Return the nightly retrain log and scheduler metadata.

    Response shape:
      {
        "runs":              [...],  # newest-first list of run entries
        "last_success":      str | null,
        "next_run":          str | null,  # ISO datetime of next scheduled fire
        "scheduler_running": bool,
      }
    """
    try:
        return jsonify(nightly_retrain.get_log())
    except Exception as exc:
        return jsonify({"error": _redact(str(exc))}), 500


@app.route("/api/auto_analysis_status", methods=["GET"])
def get_auto_analysis_status():
    """Next scheduled auto-analysis times + last run summary."""
    try:
        next_morning = next_noon = None
        if _sched is not None and _sched.running:
            for job_id in ("auto_analysis_morning", "auto_analysis_noon"):
                job = _sched.get_job(job_id)
                if job and job.next_run_time:
                    iso = job.next_run_time.isoformat()
                    if job_id == "auto_analysis_morning":
                        next_morning = iso
                    else:
                        next_noon = iso
        # Pick whichever fires soonest as "next_run"
        candidates = [t for t in [next_morning, next_noon] if t]
        next_run = min(candidates) if candidates else None
        with _auto_analysis_lock:
            state = dict(_auto_analysis_state)
        return jsonify({
            "scheduler_running": _sched is not None and _sched.running,
            "next_morning_run":  next_morning,
            "next_noon_run":     next_noon,
            "next_run":          next_run,
            **state,
        })
    except Exception as exc:
        return jsonify({"error": _redact(str(exc))}), 500


@app.route("/api/auto_settlement_status", methods=["GET"])
def get_auto_settlement_status():
    """Last settlement run metadata + next scheduled fire time."""
    try:
        next_run = None
        if _sched is not None and _sched.running:
            job = _sched.get_job("auto_settlement")
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()
        with _auto_settlement_lock:
            state = dict(_auto_settlement_state)
        return jsonify({"next_run": next_run, "scheduler_running": _sched is not None and _sched.running, **state})
    except Exception as exc:
        return jsonify({"error": _redact(str(exc))}), 500


@app.route("/api/retrain_now", methods=["POST"])
def trigger_retrain_now():
    """
    Manually trigger the nightly retrain job immediately (admin use / testing).
    Runs in the background so the HTTP response returns right away.
    """
    try:
        fn = getattr(nightly_retrain, "run_nightly_retrain", None)
        if fn is None or isinstance(nightly_retrain, _NightlyRetrainStub):
            return jsonify({"error": "Nightly retrain module not available (APScheduler missing)."}), 503
        t = threading.Thread(target=fn, daemon=True, name="manual_retrain")
        t.start()
        return jsonify({"success": True, "message": "Retrain job started in background."})
    except Exception as exc:
        return jsonify({"error": _redact(str(exc))}), 500


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
    odds_key = _ODDS_API_KEY
    if odds_key and odds_key != "your_odds_api_key_here":
        oc = OddsClient(odds_key, _cache)
        try:
            settled.extend(ledger.settle(oc, sport_cfg.odds_key))
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)
        try:
            settled.extend(wledger.settle(oc, "basketball_wnba"))
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    summary = ledger.get_summary()

    # ── All model history from BOTH sports (for model tab W/L record) ─────────
    # MLB "bet_type" uses: "single" (ML), "run_line" (RL), "totals"
    # WNBA "bet_type" uses: "single" (ML), "spread",         "totals"
    _all_model_hist = ledger.data["history"] + wledger.data["history"]

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
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


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
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/wipe_ledger", methods=["POST"])
def admin_wipe_ledger():
    """
    Per-sport ledger wipe used by the Admin sub-page.
    Body: {"sport": "mlb" | "wnba" | "both"}.

    Clears open_bets + history for the chosen sport(s) and resets both
    bankrolls to their starting values, across THREE storage layers:

      Local JSON         data/ledger.json + data/wnba_ledger.json
      Supabase           bets table (per sport) + bankroll table row
      In-memory          _analysis_state / _wnba_analysis_state parlays

    Model files / analysis caches / API settings are untouched -- this
    is the ledger wipe, not the picks wipe.  For the picks wipe use
    /api/admin/model/reset.

    Returns the same per-layer audit shape the Reset Model Picks route
    uses so the inline status message on /admin reads consistently.
    """
    print("[ADMIN-ROUTE] /api/admin/wipe_ledger invoked  body="
          f"{request.json!r}", flush=True, file=sys.stderr)
    audit: list[str] = []
    try:
        sport = (request.json or {}).get("sport", "").strip().lower()
        if sport not in ("mlb", "wnba", "both"):
            return jsonify({"success": False,
                            "error": "sport must be 'mlb', 'wnba', or 'both'"}), 400

        sports = ["mlb", "wnba"] if sport == "both" else [sport]
        paths  = {
            "mlb":  Path("data/ledger.json"),
            "wnba": Path("data/wnba_ledger.json"),
        }
        wiped: list[str] = []
        per_sport_counts: dict[str, dict] = {}

        # ── Layer 1: local JSON file ─────────────────────────────────────
        for s in sports:
            path = paths[s]
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:                                              # noqa: BLE001
                data = {}
            n_open  = len(data.get("open_bets") or [])
            n_hist  = len(data.get("history") or [])
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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
            wiped.append(s)
            per_sport_counts[s] = {
                "open_bets_dropped": n_open,
                "history_dropped":   n_hist,
                "model_start":       model_start,
                "personal_start":    personal_start,
            }
            audit.append(
                f"local {s} ledger.json: dropped {n_open} open + {n_hist} history "
                f"row(s); bankrolls -> model=${model_start:.2f}, "
                f"personal=${personal_start:.2f}"
            )
            _eprint(
                f"WIPE-LEDGER [{s.upper()}] local file cleared: "
                f"{n_open} open + {n_hist} history bets dropped, "
                f"model_bankroll=${model_start:.2f}, "
                f"personal_bankroll=${personal_start:.2f}"
            )

        # ── Layer 2: Supabase bets + bankroll per sport ──────────────────
        supabase_bets_by_sport: dict[str, int] = {}
        try:
            from src import db as _db
            if _db.is_supabase():
                for s in sports:
                    # Delete EVERY bet for this sport (both confirmed and
                    # unconfirmed) -- this is the total ledger wipe.
                    try:
                        n_bets = int(_db.delete_bets_bulk(sport=s) or 0)
                    except Exception as bx:                                # noqa: BLE001
                        n_bets = 0
                        audit.append(
                            f"supabase bets {s}: error "
                            f"{type(bx).__name__}: {bx}"
                        )
                        _eprint(
                            f"WIPE-LEDGER [{s.upper()}] supabase bets FAILED: "
                            f"{type(bx).__name__}: {bx}"
                        )
                    supabase_bets_by_sport[s] = n_bets
                    if n_bets >= 0:
                        audit.append(
                            f"supabase bets {s}: deleted {n_bets} row(s)"
                        )
                        _eprint(
                            f"WIPE-LEDGER [{s.upper()}] supabase bets table: "
                            f"deleted {n_bets} row(s)"
                        )

                    # Reset the bankroll row to starting values.  Without
                    # this Supabase still reports the pre-wipe balance.
                    try:
                        d = per_sport_counts.get(s, {})
                        ms = float(d.get("model_start", 1000.0))
                        ps = float(d.get("personal_start", 1000.0))
                        _db.upsert_bankroll(s, {
                            "model_bankroll":             ms,
                            "model_starting_bankroll":    ms,
                            "personal_bankroll":          ps,
                            "personal_starting_bankroll": ps,
                        })
                        audit.append(
                            f"supabase bankroll {s}: reset to "
                            f"model=${ms:.2f}, personal=${ps:.2f}"
                        )
                        _eprint(
                            f"WIPE-LEDGER [{s.upper()}] supabase bankroll: "
                            f"reset to model=${ms:.2f}, personal=${ps:.2f}"
                        )
                    except Exception as br:                                # noqa: BLE001
                        audit.append(
                            f"supabase bankroll {s}: error "
                            f"{type(br).__name__}: {br}"
                        )
                        _eprint(
                            f"WIPE-LEDGER [{s.upper()}] supabase bankroll "
                            f"FAILED: {type(br).__name__}: {br}"
                        )
            else:
                audit.append("supabase: OFF -- bets + bankroll skipped")
                _eprint(
                    "WIPE-LEDGER supabase: OFF -- bets + bankroll sync skipped"
                )
        except Exception as sb_exc:                                       # noqa: BLE001
            audit.append(
                f"supabase block: error {type(sb_exc).__name__}: {sb_exc}"
            )
            _eprint(
                f"WIPE-LEDGER supabase block FAILED: "
                f"{type(sb_exc).__name__}: {sb_exc}"
            )

        # ── Layer 2b: authoritative supa_ledger pools (model + personal) ─
        # These (*_bankroll_pool + *_ledger_bets) are what My Bets + the
        # Model bankroll card actually read; the old `bets`/`bankroll`
        # tables above are legacy.  The pools are combined across sports, so
        # a single-sport wipe can only scope the bets — the combined balance
        # is left intact unless this is a full ("both") wipe, which rebases
        # each pool to its starting value (matching the local-file reset).
        try:
            from src import supa_ledger as _sl
            if _sl.db.is_supabase():
                if sport == "both":
                    _sl.model().reset()
                    _sl.personal().reset()
                    audit.append("supabase pools: model + personal reset to "
                                 "starting, all ledger_bets cleared")
                    _eprint("WIPE-LEDGER supabase pools: model + personal "
                            "reset to starting, all ledger_bets cleared")
                else:
                    n_m = _sl.model().clear_bets(sport=sport)
                    n_p = _sl.personal().clear_bets(sport=sport)
                    audit.append(
                        f"supabase ledger_bets ({sport}): cleared {n_m} model "
                        f"+ {n_p} personal row(s); combined bankroll pools left "
                        f"intact (single-sport wipe can't partially rebase)"
                    )
                    _eprint(f"WIPE-LEDGER supabase ledger_bets [{sport}]: "
                            f"cleared {n_m} model + {n_p} personal row(s)")
            else:
                audit.append("supabase pools: OFF -- skipped")
        except Exception as pool_exc:                                     # noqa: BLE001
            audit.append(f"supabase pools: error "
                         f"{type(pool_exc).__name__}: {pool_exc}")
            _eprint(f"WIPE-LEDGER supabase pools FAILED: "
                    f"{type(pool_exc).__name__}: {pool_exc}")

        # ── Layer 3: in-memory state -- parlays only.  Results stay so
        # the slate / matchup pages still render today's analyzed games;
        # the wipe is about BETS, not picks. ─────────────────────────────
        try:
            if "mlb" in sports:
                _analysis_state["parlays"] = {}
                audit.append("in-memory: cleared _analysis_state['parlays']")
                _eprint("WIPE-LEDGER in-memory: cleared _analysis_state['parlays']")
            if "wnba" in sports:
                _wnba_analysis_state["parlays"] = {}
                audit.append("in-memory: cleared _wnba_analysis_state['parlays']")
                _eprint(
                    "WIPE-LEDGER in-memory: cleared _wnba_analysis_state['parlays']"
                )
        except Exception as mem_exc:                                       # noqa: BLE001
            audit.append(
                f"in-memory clear FAILED: "
                f"{type(mem_exc).__name__}: {mem_exc}"
            )
            _eprint(
                f"WIPE-LEDGER in-memory FAILED: "
                f"{type(mem_exc).__name__}: {mem_exc}"
            )

        message = "; ".join(audit)
        _eprint(f"WIPE-LEDGER COMPLETE [{sport}]: {message}")
        return jsonify({
            "success":                True,
            "wiped":                  wiped,
            "per_sport_counts":       per_sport_counts,
            "supabase_bets_by_sport": supabase_bets_by_sport,
            "message":                message,
            "audit_log":              audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _eprint(
            f"WIPE-LEDGER FATAL: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


# ────────────────────────────────────────────────────────────────────────────
#  Granular reset endpoints used by the Admin -> Data Reset section.  Each
#  one wipes EXACTLY the slice named in its route -- the surrounding state
#  is preserved.  Power-user controls; the UI gates each call behind a
#  confirm dialog that quotes what the call deletes.
#
#  Schema reminder (per src/ledger.py):
#    history       list of settled bets; each has `confirmed: bool` and
#                  `confidence_tier: 'strong'|'moderate'|'low'|None`.
#                  confirmed=True  => personal (user-tracked) bet
#                  confirmed=False => pure model bet
#    open_bets     same shape as history but unsettled.
#    model_bankroll / model_starting_bankroll        $ figures
#    personal_bankroll / personal_starting_bankroll  $ figures
# ────────────────────────────────────────────────────────────────────────────

_RESET_PATHS = ("data/ledger.json", "data/wnba_ledger.json")


def _reset_each_ledger(mutator) -> dict:
    """Helper: open each per-sport ledger, run `mutator(ledger.data)`, save.

    `mutator` mutates in place; this wrapper drives the per-sport iteration
    and returns a {sport: count} summary the route hands back to the UI.
    Errors are caught per-sport so one bad file doesn't abort the other.
    """
    summary: dict[str, int] = {}
    for path in _RESET_PATHS:
        sport = "wnba" if "wnba" in path else "mlb"
        try:
            led   = Ledger(path=path, starting_bankroll=1000.0)
            count = mutator(led.data)
            led.save()
            summary[sport] = int(count or 0)
        except Exception as exc:                                          # noqa: BLE001
            _logger.warning("reset(%s) failed: %s", path, exc)
            summary[sport] = -1
    return summary




def _delete_file(path: Path) -> bool:
    """Delete a file if present.  Returns True iff the file was removed."""
    try:
        if path.exists():
            path.unlink()
            return True
        return False
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("delete_file(%s) failed: %s", path, exc)
        return False


def _audit_log(label: str, lines: list[str]) -> None:
    """Emit a structured stderr report of every file + Supabase
    table the reset endpoint touched.  One header line + one indented
    line per item; explicit ZERO line so the user can tell the
    endpoint ran and just found nothing to clear."""
    _eprint(f"RESET[{label}]: confirmation log")
    if not lines:
        _eprint(f"  RESET[{label}]: (nothing was found to clear)")
        return
    for ln in lines:
        _eprint(f"  RESET[{label}]: {ln}")


@app.route("/api/admin/reset/model_record", methods=["POST"])
def admin_reset_model_record():
    """Reset Model Record -- wipe the model's entire tracked pick history.

      Supabase (canonical store)
        - model_picks table: every row deleted (both sports, every
          model, pending + finished) so the home + props record /
          win-percentage cards return to 0-0.  This replaces the old
          truncation of .cache/xgb_picks_history.json,
          .cache/lr_picks_history.json, data/nn_picks_history.json and
          .cache/props_picks_history.json -- the model record now lives
          only in Supabase.
        - records + model_history tables: legacy mirrors, also cleared.

      Local JSON (bankroll-side bookkeeping, not the model record)
        - data/ledger.json + data/wnba_ledger.json: drop history rows
          with confirmed=False (the model's own settled bets)
        - data/ensemble_picks_today.json / data/daily_picks.json
          deleted (regenerated by next analyze run)
    """
    audit: list[str] = []
    try:
        def _mut(data: dict) -> int:
            hist = data.get("history") or []
            kept = [h for h in hist if h.get("confirmed")]
            removed = len(hist) - len(kept)
            data["history"] = kept
            return removed
        removed_summary = _reset_each_ledger(_mut)
        for sport, n in removed_summary.items():
            audit.append(f"ledger {sport}: dropped {n} model history row(s)")

        # model_picks is now the single source of truth for the model
        # record, so wipe it here instead of truncating the legacy
        # per-classifier picks-history JSON files.
        try:
            from src import db as _db
            if _db.is_supabase():
                n_mp = _db.delete_model_picks()
                audit.append(f"supabase model_picks: deleted {n_mp} row(s) "
                             f"(all sports / models / statuses)")
                _eprint(f"RESET[model_record] supabase model_picks: "
                        f"deleted {n_mp} row(s)")
            else:
                audit.append("supabase model_picks: (Supabase off, skipped)")
        except Exception as mp_exc:                                       # noqa: BLE001
            audit.append(f"supabase model_picks: error "
                         f"{type(mp_exc).__name__}: {mp_exc}")
            _eprint(f"RESET[model_record] supabase model_picks FAILED: "
                    f"{type(mp_exc).__name__}: {mp_exc}")

        if _delete_file(_ENSEMBLE_PICKS_FILE):
            audit.append(f"file: deleted {_ENSEMBLE_PICKS_FILE}")
        else:
            audit.append(f"file: {_ENSEMBLE_PICKS_FILE} (not present, skipped)")

        # data/daily_picks.json -- previously left behind, which is why
        # the Model tab's TODAY'S MODEL PICKS section kept showing stale
        # picks after a Reset Model Record click.  Clear it explicitly.
        if _delete_file(_DAILY_PICKS_FILE):
            audit.append(f"file: deleted {_DAILY_PICKS_FILE}")
        else:
            audit.append(f"file: {_DAILY_PICKS_FILE} (not present, skipped)")

        # data/bet_history_archive.json -- permanent archive that
        # Ledger.settle appends to.  Reset Model Record is the
        # "wipe model records" button, so the archive's model entries
        # are also in-scope.  We keep personal-confirmed entries by
        # filtering rather than deleting the file.
        try:
            if _BET_HISTORY_ARCHIVE.exists():
                raw  = json.loads(_BET_HISTORY_ARCHIVE.read_text(encoding="utf-8"))
                bets = raw.get("bets") if isinstance(raw, dict) else raw
                if isinstance(bets, list):
                    before = len(bets)
                    kept   = [b for b in bets if b.get("confirmed")]
                    after  = len(kept)
                    _BET_HISTORY_ARCHIVE.write_text(
                        json.dumps({"bets": kept}, indent=2),
                        encoding="utf-8",
                    )
                    audit.append(
                        f"file: {_BET_HISTORY_ARCHIVE} -- pruned {before - after} "
                        f"model row(s), kept {after} confirmed row(s)"
                    )
        except Exception as exc:                                          # noqa: BLE001
            audit.append(f"file: {_BET_HISTORY_ARCHIVE} prune error: {exc}")

        # In-memory state -- the analyze pipeline writes results +
        # last_analysis_meta into _analysis_state / _wnba_analysis_state.
        # Without clearing these the home + sport pages render against
        # the previous run's data until the next analyze fires.
        for _state, _label in (
            (_analysis_state,      "mlb"),
            (_wnba_analysis_state, "wnba"),
        ):
            _state["results"]            = []
            _state["last_analysis_meta"] = {}
            audit.append(
                f"in-memory: cleared _analysis_state[{_label}].results + "
                f"last_analysis_meta"
            )

        # Reload ensemble_store so get_picks() returns an empty list rather
        # than the pre-delete in-memory snapshot.
        try:
            ensemble_store.load()
            audit.append("ensemble_store reloaded")
        except Exception:                                                  # noqa: BLE001
            pass

        try:
            from src import db as _db
            if _db.is_supabase():
                n_rows = _db.delete_records(sport=None)
                audit.append(f"supabase records: deleted {n_rows} row(s)")
                _eprint(f"RESET[model_record] supabase records: deleted {n_rows} row(s)")

                # Supabase model_history table -- the actual mirror of
                # the xgb / lr / nn picks history files.  delete_model_history
                # with no filters drops every row across all classifiers /
                # sports, matching the "wipe all model record" intent.
                try:
                    n_mh = _db.delete_model_history()
                    audit.append(
                        f"supabase model_history: deleted {n_mh} row(s) "
                        f"(xgb + lr + nn picks history)"
                    )
                    _eprint(
                        f"RESET[model_record] supabase model_history: "
                        f"deleted {n_mh} row(s) across xgb / lr / nn"
                    )
                except Exception as mh_exc:                                # noqa: BLE001
                    audit.append(
                        f"supabase model_history: error "
                        f"{type(mh_exc).__name__}: {mh_exc}"
                    )
                    _eprint(
                        f"RESET[model_record] supabase model_history FAILED: "
                        f"{type(mh_exc).__name__}: {mh_exc}"
                    )

                # Also delete Supabase bets table rows with confirmed=false
                # so the Supabase view of model history is wiped too --
                # otherwise list_bets(confirmed=False) would still return
                # the pre-reset model bets, which any future export /
                # diagnostic call would surface as stale data.
                n_bets = _db.delete_bets_bulk(confirmed=False)
                audit.append(
                    f"supabase bets (confirmed=false): deleted {n_bets} row(s)"
                )
                _eprint(
                    f"RESET[model_record] supabase bets (confirmed=false): "
                    f"deleted {n_bets} row(s)"
                )

                # app_cache wildcard delete for any picks-history-style
                # key the rest of the pipeline may have written.
                # cache_delete_keys_like is the helper added in PR #95;
                # this call adds picks_history / model_history matches
                # on top of the picks/snapshot/analysis sweep used by
                # /api/admin/model/reset.
                try:
                    n_cache, matched_keys = _db.cache_delete_keys_like(
                        ["picks_history", "model_history"],
                    )
                    audit.append(
                        f"supabase app_cache (picks_history/model_history): "
                        f"deleted {n_cache} row(s)"
                    )
                    _eprint(
                        f"RESET[model_record] supabase app_cache pattern delete: "
                        f"removed {n_cache} row(s)"
                        + (f" -- keys: {', '.join(matched_keys[:10])}"
                           + (" ..." if len(matched_keys) > 10 else "")
                           if matched_keys else "")
                    )
                except Exception as kc_exc:                                # noqa: BLE001
                    audit.append(
                        f"supabase app_cache pattern delete: error "
                        f"{type(kc_exc).__name__}: {kc_exc}"
                    )
                    _eprint(
                        f"RESET[model_record] supabase app_cache pattern "
                        f"delete FAILED: {type(kc_exc).__name__}: {kc_exc}"
                    )
            else:
                audit.append("supabase records: (Supabase off, skipped)")
                _eprint("RESET[model_record] supabase: OFF -- skipped all "
                        "supabase-side deletes")
        except Exception as exc:                                          # noqa: BLE001
            audit.append(f"supabase records: error {type(exc).__name__}: {exc}")
            _eprint(
                f"RESET[model_record] supabase block FAILED: "
                f"{type(exc).__name__}: {exc}"
            )

        _audit_log("model_record", audit)
        return jsonify({
            "success":   True,
            "removed":   removed_summary,
            "audit_log": audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _audit_log("model_record", audit + [f"FATAL: {exc}"])
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/model_bankroll", methods=["POST"])
def admin_reset_model_bankroll():
    """Reset Model Bankroll -- the bankroll dollars + every unconfirmed
    bet across local + Supabase:

      Local JSON
        - model_bankroll <- model_starting_bankroll on each per-sport ledger
        - drop confirmed=False open_bets so the bankroll dollars match
          what's actually exposed in the market

      Supabase
        - bets table: delete every row with confirmed=false
        - bankroll table: re-upsert each sport's row to the starting
          balance with 0 open exposure
    """
    audit: list[str] = []
    try:
        def _mut(data: dict) -> int:
            start = float(data.get("model_starting_bankroll", 1000.0))
            data["model_bankroll"] = start
            opens = data.get("open_bets") or []
            kept  = [b for b in opens if b.get("confirmed")]
            removed = len(opens) - len(kept)
            data["open_bets"] = kept
            return removed
        removed_summary = _reset_each_ledger(_mut)
        for sport, n in removed_summary.items():
            audit.append(f"ledger {sport}: bankroll -> starting, dropped {n} model open_bet(s)")

        try:
            from src import db as _db
            if _db.is_supabase():
                n_bets = _db.delete_bets_bulk(confirmed=False)
                audit.append(f"supabase bets: deleted {n_bets} unconfirmed row(s)")
                for sport in ("mlb", "wnba"):
                    try:
                        led = Ledger(
                            path=f"data/{'wnba_ledger' if sport == 'wnba' else 'ledger'}.json",
                            starting_bankroll=1000.0,
                        )
                        start = float(led.data.get("model_starting_bankroll", 1000.0))
                        _db.delete_bankroll(sport)
                        _db.upsert_bankroll(sport, {
                            "model_bankroll":          start,
                            "model_starting_bankroll": start,
                            "personal_bankroll":       float(led.data.get(
                                "personal_bankroll",
                                led.data.get("personal_starting_bankroll", 1000.0))),
                            "personal_starting_bankroll": float(
                                led.data.get("personal_starting_bankroll", 1000.0)),
                        })
                        audit.append(f"supabase bankroll {sport}: reset to ${start:.2f}")
                    except Exception as bx:                                # noqa: BLE001
                        audit.append(f"supabase bankroll {sport}: error {bx}")
            else:
                audit.append("supabase bets/bankroll: (Supabase off, skipped)")
        except Exception as exc:                                          # noqa: BLE001
            audit.append(f"supabase bets/bankroll: error {type(exc).__name__}: {exc}")

        # Authoritative supa_ledger MODEL pool -- rebase to starting + clear
        # its frozen bets so the Model bankroll card actually resets (the
        # `bankroll`/`bets` writes above are legacy tables the card no longer
        # reads).  Personal pool untouched.
        try:
            from src import supa_ledger as _sl
            if _sl.db.is_supabase():
                _sl.model().reset()
                audit.append("supabase model pool: reset to starting + "
                             "model_ledger_bets cleared")
                _eprint("RESET[model_bankroll] supabase model pool: "
                        "reset to starting + bets cleared")
            else:
                audit.append("supabase model pool: (Supabase off, skipped)")
        except Exception as pexc:                                         # noqa: BLE001
            audit.append(f"supabase model pool: error "
                         f"{type(pexc).__name__}: {pexc}")

        _audit_log("model_bankroll", audit)
        return jsonify({
            "success":           True,
            "removed_open_bets": removed_summary,
            "audit_log":         audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _audit_log("model_bankroll", audit + [f"FATAL: {exc}"])
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/confidence_record", methods=["POST"])
def admin_reset_confidence_record():
    """Reset Confidence Record -- clear the confidence_tier field on
    every settled-history row across both ledgers.

      Local JSON
        - data/ledger.json + data/wnba_ledger.json: confidence_tier
          set to None on history rows that had a tier.  Open bets
          keep their tier so the next settlement still records it
          (this reset is about historical record, not future picks).

      Supabase
        - bets table rows mirror the ledger; the next call to
          upsert_bets_bulk (after any settle / reset) syncs the
          cleared field.  No separate Supabase mutation here -- the
          confidence_tier card reads from the local ledger and the
          ledger save fans out on next write.
    """
    audit: list[str] = []
    try:
        def _mut(data: dict) -> int:
            cleared = 0
            for h in (data.get("history") or []):
                if h.get("confidence_tier") is not None:
                    h["confidence_tier"] = None
                    cleared += 1
            return cleared
        cleared_summary = _reset_each_ledger(_mut)
        for sport, n in cleared_summary.items():
            audit.append(
                f"ledger {sport}: cleared confidence_tier on {n} history row(s)"
            )
        audit.append(
            "supabase: (confidence_tier propagates via the next ledger upsert; "
            "the card reads from local history)"
        )
        # Persist a 'cleared' marker to Supabase app_cache so the reset
        # survives a Railway redeploy (same app_cache pattern explorer
        # cache_save uses).  Best-effort.
        try:
            from src import db as _db
            if _db.is_supabase():
                _db.cache_set(
                    "confidence_record", None,
                    datetime.now(timezone.utc).strftime("%Y-%m-%d"), {},
                )
                audit.append("supabase app_cache 'confidence_record': cleared")
                _eprint("RESET[confidence_record] supabase app_cache "
                        "'confidence_record': cleared")
            else:
                audit.append("supabase app_cache: (Supabase off, skipped)")
        except Exception as cexc:                                         # noqa: BLE001
            audit.append(f"supabase app_cache 'confidence_record': error "
                         f"{type(cexc).__name__}: {cexc}")
        _audit_log("confidence_record", audit)
        return jsonify({
            "success":   True,
            "cleared":   cleared_summary,
            "audit_log": audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _audit_log("confidence_record", audit + [f"FATAL: {exc}"])
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/my_bets_record", methods=["POST"])
def admin_reset_my_bets_record():
    """Reset My Bets Record -- comprehensive personal-side wipe:

      Local JSON
        - data/ledger.json + data/wnba_ledger.json:
            history list: drop every row with confirmed=True
            open_bets:    drop every row with confirmed=True
            personal_bankroll <- personal_starting_bankroll

      Supabase
        - bets table: delete every row with confirmed=true
        - bankroll table: re-upsert personal_bankroll to starting on
          each sport's row (model side preserved as-is)
    """
    audit: list[str] = []
    try:
        def _mut(data: dict) -> dict:
            # Tuple-shaped count so the per-sport summary captures
            # both history + open_bets removals.
            hist = data.get("history") or []
            kept_hist = [h for h in hist if not h.get("confirmed")]
            removed_hist = len(hist) - len(kept_hist)
            data["history"] = kept_hist

            opens = data.get("open_bets") or []
            kept_open = [b for b in opens if not b.get("confirmed")]
            removed_open = len(opens) - len(kept_open)
            data["open_bets"] = kept_open

            start = float(data.get("personal_starting_bankroll", 1000.0))
            data["personal_bankroll"] = start

            return {
                "history":      removed_hist,
                "open_bets":    removed_open,
                "bankroll_to": start,
            }

        # _reset_each_ledger returns {sport: int} -- adapt by stashing
        # the per-sport detail dict in a closure so the audit log gets
        # full granularity even though the public summary stays an int.
        detail: dict[str, dict] = {}
        def _mut_int(data: dict) -> int:
            d = _mut(data)
            sport = "wnba" if data.get("sport") == "wnba" else "mlb"
            detail[sport] = d
            return d["history"] + d["open_bets"]
        summary = _reset_each_ledger(_mut_int)
        for sport, n in summary.items():
            d = detail.get(sport, {})
            audit.append(
                f"ledger {sport}: dropped {d.get('history', 0)} confirmed "
                f"history + {d.get('open_bets', 0)} confirmed open_bet(s), "
                f"personal_bankroll -> ${d.get('bankroll_to', 0):.2f}"
            )

        # Prune personal-confirmed rows from the permanent archive too.
        # Mirrors what Reset Model Record does for the model side --
        # without this, the archive keeps the user's old confirmed bets
        # forever and any future export / diagnostic that walks the
        # archive would surface them as still-present.
        try:
            if _BET_HISTORY_ARCHIVE.exists():
                raw  = json.loads(_BET_HISTORY_ARCHIVE.read_text(encoding="utf-8"))
                bets = raw.get("bets") if isinstance(raw, dict) else raw
                if isinstance(bets, list):
                    before = len(bets)
                    kept   = [b for b in bets if not b.get("confirmed")]
                    after  = len(kept)
                    _BET_HISTORY_ARCHIVE.write_text(
                        json.dumps({"bets": kept}, indent=2),
                        encoding="utf-8",
                    )
                    audit.append(
                        f"file: {_BET_HISTORY_ARCHIVE} -- pruned {before - after} "
                        f"confirmed row(s), kept {after} model row(s)"
                    )
        except Exception as exc:                                          # noqa: BLE001
            audit.append(f"file: {_BET_HISTORY_ARCHIVE} prune error: {exc}")

        try:
            from src import db as _db
            if _db.is_supabase():
                n_bets = _db.delete_bets_bulk(confirmed=True)
                audit.append(f"supabase bets: deleted {n_bets} confirmed row(s)")
                for sport in ("mlb", "wnba"):
                    try:
                        led = Ledger(
                            path=f"data/{'wnba_ledger' if sport == 'wnba' else 'ledger'}.json",
                            starting_bankroll=1000.0,
                        )
                        start = float(led.data.get("personal_starting_bankroll", 1000.0))
                        _db.upsert_bankroll(sport, {
                            "model_bankroll":          float(led.data.get(
                                "model_bankroll",
                                led.data.get("model_starting_bankroll", 1000.0))),
                            "model_starting_bankroll": float(
                                led.data.get("model_starting_bankroll", 1000.0)),
                            "personal_bankroll":          start,
                            "personal_starting_bankroll": start,
                        })
                        audit.append(
                            f"supabase bankroll {sport}: personal_bankroll -> ${start:.2f}"
                        )
                    except Exception as bx:                                # noqa: BLE001
                        audit.append(f"supabase bankroll {sport}: error {bx}")
            else:
                audit.append("supabase bets/bankroll: (Supabase off, skipped)")
        except Exception as exc:                                          # noqa: BLE001
            audit.append(f"supabase bets/bankroll: error {type(exc).__name__}: {exc}")

        # Authoritative supa_ledger PERSONAL pool -- rebase to starting +
        # clear its frozen bets so the My Bets bankroll card actually resets
        # (the `bets`/`bankroll` writes above are legacy tables it no longer
        # reads).  Model pool untouched.
        try:
            from src import supa_ledger as _sl
            if _sl.db.is_supabase():
                _sl.personal().reset()
                audit.append("supabase personal pool: reset to starting + "
                             "personal_ledger_bets cleared")
                _eprint("RESET[my_bets_record] supabase personal pool: "
                        "reset to starting + bets cleared")
            else:
                audit.append("supabase personal pool: (Supabase off, skipped)")
        except Exception as pexc:                                         # noqa: BLE001
            audit.append(f"supabase personal pool: error "
                         f"{type(pexc).__name__}: {pexc}")

        _audit_log("my_bets_record", audit)
        return jsonify({
            "success":   True,
            "removed":   summary,
            "audit_log": audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _audit_log("my_bets_record", audit + [f"FATAL: {exc}"])
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


# ────────────────────────────────────────────────────────────────────────────
#  Odds API quota -- read + manual-allowance grant.
#
#  /api/odds/usage  (GET)   read-only snapshot for the UI to render the
#                            counter chip + the limit-reached banner
#  /api/admin/odds/approve_additional (POST) bump today's allowance by
#                            +50 (the bonus_step in odds_client).  Each
#                            click of the Admin button calls this once.
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/odds/usage", methods=["GET"])
def odds_usage_endpoint():
    """Return today's Odds API call count + effective limit.  Cheap, no
    upstream traffic -- the UI hits this on page load and after every
    analyze response."""
    try:
        from src.odds_client import odds_usage
        u = odds_usage()
        return jsonify({
            "success":         True,
            "count":           u["count"],
            "base_limit":      u["base_limit"],
            "extra_allowance": u["extra_allowance"],
            "effective_limit": u["effective_limit"],
            "remaining":       u["remaining"],
            "limit_reached":   u["limit_reached"],
            "date_et":         u["date_et"],
        })
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({
            "success":         False,
            "error":           _redact(str(exc)),
            "count":           0,
            "base_limit":      500,
            "extra_allowance": 0,
            "effective_limit": 500,
            "remaining":       500,
            "limit_reached":   False,
        }), 200


def _odds_health_for_sport(sport: str) -> dict:
    """Bundle freshness + games-with-odds + last-analyzed timestamp for
    one sport into a single dict.  Used by both /api/odds/cache_status
    (extra fields appended) and the admin "Odds Status" indicator.

    games_with_odds  -- count of in-memory results that carry a
                         pick_team (i.e. The Odds API returned a line
                         the model could attach a pick to).  When this
                         is zero on a day with a non-empty schedule,
                         analyze ran but the Odds API came back empty
                         OR the response landed somewhere the renderer
                         can't see.
    last_analyzed_at -- ISO string from _analysis_state, or None when
                         analyze hasn't run since the last reset.
    """
    sport = sport.lower()
    state = _wnba_analysis_state if sport == "wnba" else _analysis_state
    results = state.get("results") or []
    with_odds = sum(1 for r in results if r.get("pick_team"))
    last = state.get("last_analyzed_at")
    return {
        "games_total":      len(results),
        "games_with_odds":  with_odds,
        "games_no_odds":    len(results) - with_odds,
        "last_analyzed_at": last.isoformat() if hasattr(last, "isoformat") else (last or None),
    }


@app.route("/api/odds/cache_status", methods=["GET"])
def odds_cache_status_endpoint():
    """Return whether the 15-min Odds API cache has fresh data for the
    given sport, PLUS per-sport game counts + last-analyzed timestamp.

    Used by the admin Run buttons (for the "Pull fresh odds?"
    confirmation dialog) AND by the admin Odds Status indicator
    (for the games-with-odds + last-updated display).

    Query:
      sport=mlb|wnba|both   (default 'both' -> returns both sports)

    Returns (per sport):
      {fresh, ttl_sec, sport_key,
       games_total, games_with_odds, games_no_odds, last_analyzed_at}
    """
    try:
        from src.odds_client import cache_status as _cache_status
        sport = (request.args.get("sport") or "both").strip().lower()
        sport_keys = {
            "mlb":  "baseball_mlb",
            "wnba": "basketball_wnba",
        }
        def _bundle(s: str) -> dict:
            return {**_cache_status(_cache, sport_keys[s]), **_odds_health_for_sport(s)}

        if sport in sport_keys:
            return jsonify(_bundle(sport))
        return jsonify({
            "mlb":  _bundle("mlb"),
            "wnba": _bundle("wnba"),
        })
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({
            "error":         _redact(str(exc)),
            "mlb":  {"fresh": False, "ttl_sec": 900, "sport_key": "baseball_mlb"},
            "wnba": {"fresh": False, "ttl_sec": 900, "sport_key": "basketball_wnba"},
        }), 200


@app.route("/api/admin/odds/approve_additional", methods=["POST"])
def odds_approve_additional():
    """Add +50 to today's Odds API allowance so blocked auto-runs can
    proceed.  Each click adds exactly _ODDS_BONUS_STEP (50 by default --
    see src/odds_client._ODDS_BONUS_STEP).  Idempotent only across a
    single ET day -- the counter resets to 0 at midnight ET, and the
    extra_allowance with it (since they live in the same dated row)."""
    try:
        from src.odds_client import odds_grant_additional
        u = odds_grant_additional()
        return jsonify({
            "success":         True,
            "count":           u["count"],
            "base_limit":      u["base_limit"],
            "extra_allowance": u["extra_allowance"],
            "effective_limit": u["effective_limit"],
            "remaining":       u["remaining"],
            "limit_reached":   u["limit_reached"],
        })
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/status", methods=["GET"])
def admin_status():
    """Single endpoint the Admin sub-page polls for header status fields.

    Picks the freshest `last_analyzed_at` available per sport from THREE
    sources in priority order:

      1. _analysis_state["last_analyzed_at"] -- in-memory, stamped by
         the analyze route in the SAME request that just finished.
         Always wins because nothing else can be fresher.
      2. _read_analysis_timestamps() -- local file (with Supabase
         fallback baked in; see the helper).
      3. None -- never analyzed in this container's lifetime.

    The in-memory path is the bug fix: the admin label used to read
    only the local file, so right after `Run MLB Analysis` clicked the
    timestamp didn't move because the file write happens just before
    the response returns and the admin poller raced ahead.  Live state
    is updated atomically inside the route well before the file write.
    """
    try:
        # Tier 1 -- live in-memory state.  Wins because nothing on disk
        # can be fresher than what the analyze route just stamped.
        def _to_iso(value) -> str | None:
            if value is None:
                return None
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return str(value) or None

        mlb_ts  = _to_iso(_analysis_state.get("last_analyzed_at"))
        wnba_ts = _to_iso(_wnba_analysis_state.get("last_analyzed_at"))

        # Tier 2a -- dedicated per-sport Supabase keys written synchronously
        # by each analyze route.  These are more reliable than the shared
        # analysis_timestamps key because they use a direct _db.cache_set
        # call (not a background thread) and survive a Railway redeploy.
        if mlb_ts is None or wnba_ts is None:
            try:
                from src import db as _db_status
                if _db_status.is_supabase():
                    if mlb_ts is None:
                        _row = _db_status.cache_get("last_analyzed_at_mlb")
                        if isinstance(_row, dict):
                            _data = _row.get("data") or _row
                            mlb_ts = (_data.get("ts") if isinstance(_data, dict) else None)
                    if wnba_ts is None:
                        _row = _db_status.cache_get("last_analyzed_at_wnba")
                        if isinstance(_row, dict):
                            _data = _row.get("data") or _row
                            wnba_ts = (_data.get("ts") if isinstance(_data, dict) else None)
            except Exception:                                               # noqa: BLE001
                pass

        # Tier 2b -- local file (with Supabase fallback inside the helper).
        # Consulted last; covers deployments that predate the dedicated keys.
        if mlb_ts is None or wnba_ts is None:
            ts_store = _read_analysis_timestamps()
            if mlb_ts is None:
                mlb_ts  = (ts_store.get("mlb")  or {}).get("analyzed_at")
            if wnba_ts is None:
                wnba_ts = (ts_store.get("wnba") or {}).get("analyzed_at")

        try:
            from src import db as _db
            db_status = _db.status()
        except Exception:                                                 # noqa: BLE001
            db_status = {"mode": "json"}
        return jsonify({
            "mlb_analyzed_at":  mlb_ts,
            "wnba_analyzed_at": wnba_ts,
            "db":               db_status,
        })
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


# ── /admin Flask port (diagnostics + perf + sharpapi + page) ────────────────

@app.route("/api/admin/diagnostics", methods=["GET"])
def admin_diagnostics():
    """Read-only probe of every data source the UI depends on.  Wraps
    src.admin_diagnostics.run_diagnostics; returns {results: [{label,
    status, detail}, ...]} where status is ok|warn|err|info.  No mutations,
    no quota burn (the Odds API probe uses the free /v4/sports endpoint)."""
    try:
        from src import admin_diagnostics as _diag
        return jsonify({"results": _diag.run_diagnostics(sys.modules[__name__])})
    except Exception as exc:                                              # noqa: BLE001
        import traceback as _tb
        _eprint(f"ADMIN-DIAG: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": _redact(str(exc)), "results": []}), 500


@app.route("/api/admin/probe_sharpapi", methods=["POST"])
def admin_probe_sharpapi():
    """One-shot SharpAPI endpoint + auth-style probe.  Same {results: [...]}
    shape as /api/admin/diagnostics so the front-end renders both into one
    panel.  Gated on SHARPAPI_KEY inside the helper."""
    try:
        from src import admin_diagnostics as _diag
        return jsonify({"results": _diag.probe_sharpapi()})
    except Exception as exc:                                              # noqa: BLE001
        import traceback as _tb
        _eprint(f"ADMIN-SHARPAPI: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": _redact(str(exc)), "results": []}), 500


@app.route("/api/admin/model/performance", methods=["POST"])
def admin_model_performance():
    """Per-model W/L table for the admin Model Performance section.

    Body: {preset: "all"|"today"|"yesterday"|"7d"|"30d"} OR
          {since: "YYYY-MM-DD", until: "YYYY-MM-DD"} for a custom range.
    Returns {rows: [...], updated_at} from src.model_picks.performance."""
    try:
        from src import model_picks as _mp
        body  = request.get_json(silent=True) or {}
        since = body.get("since")
        until = body.get("until")
        if not (since or until):
            preset = (body.get("preset") or "all").lower()
            if preset != "all":
                since, until = _mp.date_range(preset)
        data = _mp.performance(since, until)
        return jsonify(_py(data))
    except Exception as exc:                                              # noqa: BLE001
        import traceback as _tb
        _eprint(f"ADMIN-PERF: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": _redact(str(exc)), "rows": [], "updated_at": ""}), 500


def _admin_view_model() -> dict:
    """Pre-fetch only what the JSON island needs: current settings (for the
    toggles + AI limit input) and the status row (last analyzed + DB mode).
    Everything else on the page is fetched on user action, so the view model
    stays tiny -- same approach as _mybets_view_model."""
    try:
        settings = _load_model_settings()
    except Exception:                                                      # noqa: BLE001
        settings = dict(_MODEL_SETTINGS_DEFAULT)

    status: dict = {"mlb_analyzed_at": None, "wnba_analyzed_at": None,
                    "db": {"mode": "json"}}
    try:
        rv = app.test_client().get("/api/admin/status")
        if rv.status_code < 400:
            status = rv.get_json(force=True, silent=True) or status
    except Exception:                                                      # noqa: BLE001
        pass

    return {
        "settings": {
            "mlb_enabled":       bool(settings.get("mlb_enabled", True)),
            "wnba_enabled":      bool(settings.get("wnba_enabled", False)),
            "show_overall_chip": bool(settings.get("show_overall_chip", True)),
            "ai_daily_limit":    int(settings.get("ai_daily_limit", 20) or 20),
        },
        "status": status,
    }


@app.route("/admin")
def admin_page():
    """Tailwind Admin page.  Hydrates settings + status from a JSON island;
    everything else (analysis runs, resets, explorer, diagnostics) is fetched
    on user action via SBT.apiPost.  No page-level poll -- only AI analysis
    has a per-action 2 s poll, handled client-side."""
    import traceback as _tb
    print("[ADMIN] route hit", flush=True, file=sys.stderr)
    try:
        vm = _admin_view_model()
    except Exception:                                                      # noqa: BLE001
        _tb.print_exc(file=sys.stderr)
        vm = {
            "settings": {"mlb_enabled": True, "wnba_enabled": False,
                         "show_overall_chip": True, "ai_daily_limit": 20},
            "status": {"mlb_analyzed_at": None, "wnba_analyzed_at": None,
                       "db": {"mode": "json"}},
        }
    return render_template("admin.html", init_data=vm)


# ────────────────────────────────────────────────────────────────────────────
#  Model-bets admin endpoints
#  These power the MODEL BETS section in the Admin sub-page.  All four
#  operate on the model side of the ledger only (confirmed=False bets) — the
#  user's personal_bankroll is never touched.
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/model/settings", methods=["GET"])
def admin_model_settings_get():
    """Current per-sport auto-pick toggles."""
    try:
        return jsonify({"success": True, "settings": _load_model_settings()})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/model/settings", methods=["POST"])
def admin_model_settings_post():
    """Update model-bets settings.  Body may carry any subset of the known
    settings keys (mlb_enabled, wnba_enabled, show_overall_chip,
    ai_daily_limit); each provided key is merged and persisted with the
    correct type coercion handled by _save_model_settings.

    Previously this only merged mlb_enabled / wnba_enabled, silently
    dropping show_overall_chip and ai_daily_limit even though the defaults
    + save path support them -- so the Home 'Overall' chip toggle and the
    AI daily-limit input appeared to save but never persisted.  Generalised
    to merge any key present in the settings defaults (no signature change,
    same response shape)."""
    try:
        body = request.json or {}
        current = _load_model_settings()
        # Merge any recognised settings key the caller sent.  _save_model_settings
        # coerces bool/int per the default's type, so we just pass values through.
        for key in _MODEL_SETTINGS_DEFAULT:
            if key in body:
                current[key] = body[key]
        saved = _save_model_settings(current)
        return jsonify({"success": True, "settings": saved})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/model/reset", methods=["POST"])
def admin_model_reset():
    """
    Wipe today's non-confirmed model picks across every storage layer.

    Clears, in this order:
      1. open model bets (non-confirmed) for each requested sport's
         ledger, refunding their stakes to model_bankroll
      2. Supabase `bets` table -- delete every row with confirmed=False
         for each requested sport so the next ledger load can't
         resurrect them
      3. data/ensemble_picks_today.json -- rewritten as the empty
         structure dated today
      4. data/daily_snapshot.json -- deleted
      5. data/analysis_cache.json + data/wnba_analysis_cache.json --
         deleted so the next analyze run writes fresh state
      6. Supabase app_cache rows for the three canonical keys
         (daily_snapshot, analysis_cache:mlb, analysis_cache:wnba)
      7. Supabase app_cache rows with keys containing "picks",
         "snapshot", or "analysis" -- catches stray rows the
         canonical-key pass would miss
      8. In-memory _analysis_state / _wnba_analysis_state results +
         parlays so the UI immediately reflects zero picks without
         a page refresh

    Body: {sport: "mlb"|"wnba"|"both"}.  The cross-sport storage
    layers (files + Supabase app_cache + in-memory state) always get
    cleared regardless of the body -- only the per-sport ledger +
    Supabase `bets` clear is sport-scoped.

    Returns a detailed summary the admin toast surfaces verbatim,
    plus per-layer counts in the JSON payload for the UI / diagnostics.
    """
    print("[ADMIN-ROUTE] /api/admin/model/reset invoked  body="
          f"{request.json!r}", flush=True, file=sys.stderr)
    try:
        from src.daily_picks import reset_today_model_bets
        sport = (request.json or {}).get("sport", "").strip().lower()
        if sport not in ("mlb", "wnba", "both"):
            return jsonify({"success": False,
                            "error": "sport must be 'mlb', 'wnba', or 'both'"}), 400
        sports = ["mlb", "wnba"] if sport == "both" else [sport]
        paths = {"mlb": "data/ledger.json", "wnba": "data/wnba_ledger.json"}
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        audit: list[str] = []
        removed_by_sport: dict[str, int] = {}
        supabase_bets_by_sport: dict[str, int] = {}
        total_removed = 0

        # ── 1) Per-sport ledger reset (local JSON) ────────────────────────
        for s in sports:
            try:
                led = Ledger(path=paths[s], starting_bankroll=1000.0)
                n_open_before = len(led.data.get("open_bets") or [])
                n = reset_today_model_bets(led, today_str)
                led.save()
                removed_by_sport[s] = n
                total_removed += n
                audit.append(
                    f"{s.upper()} ledger cleared {n} open bet(s) "
                    f"(of {n_open_before} total open)"
                )
                _eprint(
                    f"RESET-MODEL [{s.upper()}]: removed {n} open bets "
                    f"(from {n_open_before} total open in local ledger)"
                )
            except Exception as ledger_exc:                                # noqa: BLE001
                removed_by_sport[s] = 0
                audit.append(f"{s.upper()} ledger reset FAILED: "
                             f"{type(ledger_exc).__name__}: {ledger_exc}")
                _eprint(
                    f"RESET-MODEL [{s.upper()}] LEDGER FAILED: "
                    f"{type(ledger_exc).__name__}: {ledger_exc}\n"
                    f"{traceback.format_exc()}"
                )

        # ── 2) Supabase `bets` table -- delete model (non-confirmed) ─────
        # Without this the next Ledger.load() pulls the stale model bets
        # back from Supabase and the picks reappear on the home page.
        for s in sports:
            try:
                from src import db as _db
                n_sb = int(_db.delete_bets_bulk(sport=s, confirmed=False) or 0)
                supabase_bets_by_sport[s] = n_sb
                total_removed += n_sb
                audit.append(
                    f"{s.upper()} Supabase bets table: {n_sb} model row(s) deleted"
                )
                _eprint(
                    f"RESET-MODEL [{s.upper()}]: Supabase bets table cleared "
                    f"({n_sb} non-confirmed row(s) deleted)"
                )
            except Exception as sb_exc:                                    # noqa: BLE001
                supabase_bets_by_sport[s] = 0
                audit.append(
                    f"{s.upper()} Supabase bets delete FAILED: "
                    f"{type(sb_exc).__name__}: {sb_exc}"
                )
                _eprint(
                    f"RESET-MODEL [{s.upper()}] Supabase bets FAILED: "
                    f"{type(sb_exc).__name__}: {sb_exc}"
                )

        # ── 2b) Supabase model_picks -- delete this sport's pending picks ─
        # model_picks is the canonical pick-tracking store; "reset today's
        # picks" must clear the open (pending) rows for the sport so the
        # tracked picks disappear too -- not just the display snapshot.
        # Finished rows are left intact (settled history is preserved).
        supabase_model_picks_by_sport: dict[str, int] = {}
        for s in sports:
            try:
                from src import db as _db
                n_mp = int(_db.delete_model_picks(sport=s, status="pending") or 0)
                supabase_model_picks_by_sport[s] = n_mp
                total_removed += n_mp
                audit.append(
                    f"{s.upper()} Supabase model_picks: {n_mp} pending row(s) deleted"
                )
                _eprint(
                    f"RESET-MODEL [{s.upper()}]: Supabase model_picks cleared "
                    f"({n_mp} pending row(s) deleted)"
                )
            except Exception as mp_exc:                                    # noqa: BLE001
                supabase_model_picks_by_sport[s] = 0
                audit.append(
                    f"{s.upper()} Supabase model_picks delete FAILED: "
                    f"{type(mp_exc).__name__}: {mp_exc}"
                )
                _eprint(
                    f"RESET-MODEL [{s.upper()}] Supabase model_picks FAILED: "
                    f"{type(mp_exc).__name__}: {mp_exc}"
                )

        # ── 3) ensemble_picks_today.json -> fresh empty for today ────────
        ensemble_cleared = 0
        try:
            existing = {}
            try:
                if _ENSEMBLE_PICKS_FILE.exists():
                    existing = json.loads(
                        _ENSEMBLE_PICKS_FILE.read_text(encoding="utf-8")
                    )
            except Exception:                                              # noqa: BLE001
                existing = {}
            for s in ("mlb", "wnba"):
                ensemble_cleared += len(
                    ((existing.get("picks") or {}).get(s) or [])
                )
            today_et_str = _today_et()
            _ENSEMBLE_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ENSEMBLE_PICKS_FILE.write_text(
                json.dumps(
                    {"date": today_et_str, "picks": {"mlb": [], "wnba": []}},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            total_removed += ensemble_cleared
            audit.append(
                f"ensemble picks wiped ({ensemble_cleared} pick(s) removed)"
            )
            _eprint(
                f"RESET-MODEL: ensemble_picks_today.json reset for "
                f"{today_et_str}  (was {ensemble_cleared} pick(s))"
            )
        except Exception as ens_exc:                                       # noqa: BLE001
            audit.append(
                f"ensemble picks wipe FAILED: "
                f"{type(ens_exc).__name__}: {ens_exc}"
            )
            _eprint(
                f"RESET-MODEL ENSEMBLE FAILED: "
                f"{type(ens_exc).__name__}: {ens_exc}\n"
                f"{traceback.format_exc()}"
            )

        # ── 4 + 5) Local cache + snapshot file deletes ───────────────────
        # Same loop because the failure handling is identical -- each
        # file gets one audit line whether it existed or not.
        local_files = [
            ("daily_snapshot.json",       _DAILY_SNAPSHOT_FILE),
            ("analysis_cache.json",       _ANALYSIS_CACHE_FILE),
            ("wnba_analysis_cache.json",  _WNBA_ANALYSIS_CACHE_FILE),
            ("daily_picks.json",          _DAILY_PICKS_FILE),
        ]
        local_files_deleted = 0
        for label, path in local_files:
            try:
                if path.exists():
                    path.unlink(missing_ok=True)
                    local_files_deleted += 1
                    audit.append(f"{label} deleted")
                    _eprint(f"RESET-MODEL: deleted local file {path}")
                else:
                    audit.append(f"{label} already absent")
            except Exception as file_exc:                                  # noqa: BLE001
                audit.append(
                    f"{label} delete FAILED: "
                    f"{type(file_exc).__name__}: {file_exc}"
                )
                _eprint(
                    f"RESET-MODEL {label} DELETE FAILED: "
                    f"{type(file_exc).__name__}: {file_exc}"
                )

        # ── 6) Supabase app_cache: canonical keys ────────────────────────
        # Order matters -- daily_snapshot is what hydrate_state restores
        # from on a fresh worker, so wipe it first.
        supabase_canonical_deleted = 0
        canonical_keys = [
            _CACHE_KEY_SNAPSHOT,
            _CACHE_KEY_ANALYSIS_MLB,
            _CACHE_KEY_ANALYSIS_WNBA,
        ]
        for ckey in canonical_keys:
            try:
                from src import db as _db
                ok = bool(_db.cache_delete(ckey))
                if ok:
                    supabase_canonical_deleted += 1
                    _eprint(f"RESET-MODEL: Supabase app_cache row '{ckey}' deleted")
                else:
                    _eprint(
                        f"RESET-MODEL: Supabase app_cache row '{ckey}' "
                        f"not deleted (offline or missing)"
                    )
            except Exception as sb_exc:                                    # noqa: BLE001
                _eprint(
                    f"RESET-MODEL Supabase delete({ckey}) failed: "
                    f"{type(sb_exc).__name__}: {sb_exc}"
                )
        audit.append(
            f"Supabase canonical app_cache rows deleted: {supabase_canonical_deleted}"
        )

        # ── 7) Supabase app_cache: pattern delete ────────────────────────
        # Catches anything that contains "picks", "snapshot", or
        # "analysis" in the key -- future code paths can add new rows
        # without us needing to update this reset endpoint.
        supabase_pattern_deleted = 0
        supabase_pattern_keys: list[str] = []
        try:
            from src import db as _db
            n, keys = _db.cache_delete_keys_like(["picks", "snapshot", "analysis"])
            supabase_pattern_deleted = int(n or 0)
            supabase_pattern_keys = list(keys or [])
            audit.append(
                f"Supabase pattern-match rows deleted: "
                f"{supabase_pattern_deleted}"
            )
            if supabase_pattern_keys:
                _eprint(
                    f"RESET-MODEL: Supabase pattern delete removed "
                    f"{supabase_pattern_deleted} row(s): "
                    f"{', '.join(supabase_pattern_keys[:20])}"
                    + (" ..." if len(supabase_pattern_keys) > 20 else "")
                )
            else:
                _eprint(
                    f"RESET-MODEL: Supabase pattern delete found 0 matches "
                    f"for picks / snapshot / analysis"
                )
        except Exception as sb_exc:                                        # noqa: BLE001
            audit.append(
                f"Supabase pattern delete FAILED: "
                f"{type(sb_exc).__name__}: {sb_exc}"
            )
            _eprint(
                f"RESET-MODEL Supabase pattern delete FAILED: "
                f"{type(sb_exc).__name__}: {sb_exc}"
            )

        supabase_deleted = supabase_canonical_deleted + supabase_pattern_deleted
        total_removed += supabase_deleted

        # ── 8) In-memory state -- so the UI reflects zero picks now ──────
        try:
            _analysis_state["results"] = []
            _analysis_state["parlays"] = {}
            _analysis_state["last_analyzed_at"] = None
            _wnba_analysis_state["results"] = []
            _wnba_analysis_state["parlays"] = {}
            _wnba_analysis_state["last_analyzed_at"] = None
            # ensemble_store keeps its own in-memory cache; reload it
            # so the next get_picks() call sees the freshly-empty file.
            try:
                ensemble_store.load()
            except Exception:                                              # noqa: BLE001
                pass
            audit.append("in-memory analysis state cleared")
            _eprint(
                "RESET-MODEL: in-memory _analysis_state + "
                "_wnba_analysis_state results / parlays / last_analyzed_at "
                "cleared; ensemble_store reloaded"
            )
        except Exception as mem_exc:                                       # noqa: BLE001
            audit.append(
                f"in-memory clear FAILED: "
                f"{type(mem_exc).__name__}: {mem_exc}"
            )

        message = ", ".join(audit)
        _eprint(f"RESET-MODEL COMPLETE: {message}  total_removed={total_removed}")
        return jsonify({
            "success":                    True,
            "removed":                    removed_by_sport,
            "supabase_bets_by_sport":     supabase_bets_by_sport,
            "supabase_model_picks_by_sport": supabase_model_picks_by_sport,
            "ensemble_cleared":           ensemble_cleared,
            "local_files_deleted":        local_files_deleted,
            "supabase_canonical_deleted": supabase_canonical_deleted,
            "supabase_pattern_deleted":   supabase_pattern_deleted,
            "supabase_pattern_keys":      supabase_pattern_keys,
            "supabase_deleted":           supabase_deleted,
            "total_removed":              total_removed,
            "message":                    message,
            "audit":                      audit,
        })
    except Exception as exc:                                              # noqa: BLE001
        _eprint(
            f"RESET-MODEL FAILED: {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
        return jsonify({"success": False,
                        "error": _redact(str(exc)),
                        "detail": _redact(traceback.format_exc())}), 500


# ══════════════════════════════════════════════════════════════════════
#  Admin "Supabase Data Explorer" -- read/inspect/edit app_cache + ledger
# ══════════════════════════════════════════════════════════════════════

def _explorer_row_size(data) -> int:
    """Byte size of an app_cache row's data payload (best-effort)."""
    try:
        return len(json.dumps(data, default=str).encode("utf-8"))
    except Exception:                                                       # noqa: BLE001
        return 0


@app.route("/api/admin/explorer/cache_keys", methods=["POST"])
def admin_explorer_cache_keys():
    """List every app_cache key with sport/date/size/updated_at (no blobs)."""
    try:
        from src import db as _db
        if not _db.is_supabase():
            return jsonify({"success": True, "supabase": False, "keys": []})
        rows = _db.cache_list_all()
        out = [{
            "key":        r.get("key"),
            "sport":      r.get("sport"),
            "date":       r.get("date"),
            "updated_at": r.get("updated_at"),
            "size":       _explorer_row_size(r.get("data")),
        } for r in rows]
        out.sort(key=lambda x: (x.get("key") or ""))
        return jsonify({"success": True, "supabase": True, "keys": out})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/cache_value", methods=["POST"])
def admin_explorer_cache_value():
    """Raw stored value (the `data` payload) for one app_cache key."""
    key = (request.get_json() or {}).get("key")
    if not key:
        return jsonify({"success": False, "error": "key required"}), 400
    try:
        from src import db as _db
        row = _db.cache_get(key)
        if row is None:
            return jsonify({"success": False, "error": "key not found"}), 404
        value = row.get("data") if isinstance(row, dict) else row
        return jsonify({"success": True, "key": key,
                        "sport": row.get("sport"), "date": row.get("date"),
                        "updated_at": row.get("updated_at"),
                        "value": _py(value)})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/cache_save", methods=["POST"])
def admin_explorer_cache_save():
    """Raw-editor save: overwrite one app_cache key's JSON value.  Preserves
    the existing row's sport/date when present."""
    body = request.get_json() or {}
    key  = body.get("key")
    raw  = body.get("value")
    if not key:
        return jsonify({"success": False, "error": "key required"}), 400
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": f"invalid JSON: {exc}"}), 400
    if not isinstance(parsed, (dict, list)):
        return jsonify({"success": False, "error": "value must be a JSON object or array"}), 400
    try:
        from src import db as _db
        existing = _db.cache_get(key) or {}
        sport = existing.get("sport")
        date  = existing.get("date") or _today_et()
        ok = _db.cache_set(key, sport, date, parsed)
        return jsonify({"success": bool(ok), "key": key})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/cache_delete", methods=["POST"])
def admin_explorer_cache_delete():
    """Delete one app_cache key (models, props dates, any key)."""
    key = (request.get_json() or {}).get("key")
    if not key:
        return jsonify({"success": False, "error": "key required"}), 400
    try:
        from src import db as _db
        ok = _db.cache_delete(key)
        return jsonify({"success": bool(ok), "key": key})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/models", methods=["POST"])
def admin_explorer_models():
    """List joblib/model artifacts stored in app_cache with key, size,
    sha256 and last-updated.  Models are stored base64 in data['b64']
    (date column == 'models')."""
    try:
        import base64 as _b64
        import hashlib as _hl
        from src import db as _db
        if not _db.is_supabase():
            return jsonify({"success": True, "supabase": False, "models": []})
        out = []
        for r in _db.cache_list_all():
            key  = r.get("key") or ""
            data = r.get("data") or {}
            is_model = (r.get("date") == "models") or key.startswith("props_model") \
                or (isinstance(data, dict) and "b64" in data)
            if not is_model:
                continue
            size, sha = 0, None
            try:
                b64 = data.get("b64") if isinstance(data, dict) else None
                if b64:
                    blob = _b64.b64decode(b64)
                    size = len(blob)
                    sha  = _hl.sha256(blob).hexdigest()
                else:
                    size = _explorer_row_size(data)
            except Exception:                                               # noqa: BLE001
                size = _explorer_row_size(data)
            out.append({"key": key, "size": size, "sha256": sha,
                        "updated_at": r.get("updated_at")})
        out.sort(key=lambda x: x.get("key") or "")
        return jsonify({"success": True, "supabase": True, "models": out})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/props_cache", methods=["POST"])
def admin_explorer_props_cache():
    """Props cache summary: today's market/prop counts + last write, plus
    every date-stamped props_* app_cache row (for stale-key deletion)."""
    try:
        from src import db as _db
        today = {"date": None, "markets": 0, "total": 0, "generated_at": None}
        try:
            from src.props_scored_cache import load_scored_props
            payload = load_scored_props() or {}
            picks = payload.get("picks") or []
            today = {
                "date":         payload.get("date"),
                "markets":      len({p.get("market") for p in picks if p.get("market")}),
                "total":        len(picks),
                "generated_at": payload.get("generated_at"),
            }
        except Exception:                                                   # noqa: BLE001
            pass
        rows = []
        if _db.is_supabase():
            for r in _db.cache_list_all():
                key = (r.get("key") or "")
                if "props" in key and "mlb" in key:
                    rows.append({"key": key, "date": r.get("date"),
                                 "updated_at": r.get("updated_at"),
                                 "size": _explorer_row_size(r.get("data"))})
            rows.sort(key=lambda x: x.get("key") or "")
        return jsonify({"success": True, "supabase": _db.is_supabase(),
                        "today": today, "rows": rows})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


def _explorer_bet_brief(b: dict) -> dict:
    return {
        "id":          b.get("id"),
        "sport":       b.get("sport"),
        "team":        b.get("bet_team") or b.get("parlay_name"),
        "bet_type":    b.get("bet_type"),
        "odds":        b.get("american_odds"),
        "amount":      b.get("confirmed_amount"),
        "result":      b.get("result"),
        "model_pnl":   b.get("model_pnl"),
        "confirmed_pnl": b.get("confirmed_pnl"),
        "placed_at":   b.get("placed_at"),
        "settled_at":  b.get("settled_at"),
    }


@app.route("/api/admin/explorer/picks", methods=["POST"])
def admin_explorer_picks():
    """Ledger + props snapshot: bankrolls, counts, P/L, and bet lists for
    per-row mark/delete controls."""
    try:
        ledgers = {}
        open_bets, settled_bets = [], []
        for sport, path in (("mlb", "data/ledger.json"), ("wnba", "data/wnba_ledger.json")):
            try:
                led = Ledger(path=path, starting_bankroll=1000.0)
                s = led.get_summary()
                ledgers[sport] = {
                    "model_bankroll":    s.get("model_bankroll"),
                    "personal_bankroll": s.get("personal_bankroll"),
                    "open_bets":         s.get("open_bets"),
                    "settled_bets":      len(led.data.get("history") or []),
                    "model_pnl":         s.get("model_pnl"),
                    "confirmed_pnl":     s.get("confirmed_pnl"),
                }
                for b in (led.data.get("open_bets") or []):
                    open_bets.append(_explorer_bet_brief({**b, "sport": b.get("sport") or sport}))
                for b in (led.data.get("history") or [])[-100:]:
                    settled_bets.append(_explorer_bet_brief({**b, "sport": b.get("sport") or sport}))
            except Exception:                                               # noqa: BLE001
                ledgers[sport] = {}
        props = {"record": {}, "picks": []}
        try:
            from src import props_picks_tracker as _ppt
            _ppt.reload()
            props["record"] = _ppt.get_record()
            for p in _ppt.get_all()[:200]:
                props["picks"].append({
                    "id":         p.get("id"),
                    "player":     p.get("player"),
                    "market":     p.get("market"),
                    "line":       p.get("line"),
                    "side":       p.get("side"),
                    "odds":       p.get("odds"),
                    "result":     p.get("result") or "pending",
                    "model_pnl":  p.get("model_pnl"),
                })
        except Exception:                                                   # noqa: BLE001
            pass
        return jsonify({"success": True, "ledgers": ledgers,
                        "open_bets": _py(open_bets),
                        "settled_bets": _py(settled_bets),
                        "props": _py(props)})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/mark_bet", methods=["POST"])
def admin_explorer_mark_bet():
    """Mark a game bet or prop pick won / loss(lost) / push(void) / pending."""
    body   = request.get_json() or {}
    kind   = (body.get("kind") or "game").lower()
    bet_id = body.get("id")
    result = (body.get("result") or "").lower()
    if not bet_id:
        return jsonify({"success": False, "error": "id required"}), 400
    if result not in ("win", "won", "loss", "lost", "push", "void", "pending"):
        return jsonify({"success": False, "error": "bad result"}), 400
    try:
        if kind == "prop":
            from src import props_picks_tracker as _ppt
            _ppt.reload()
            updated = _ppt.set_result(bet_id, result)
            return (jsonify({"success": True, "bet": _py(updated)}) if updated
                    else (jsonify({"success": False, "error": "pick not found"}), 404))
        sport = (body.get("sport") or "mlb").lower()
        path  = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
        led   = Ledger(path=path, starting_bankroll=1000.0)
        g_result = {"won": "win", "lost": "loss", "void": "push"}.get(result, result)
        updated = led.set_result(bet_id, g_result)

        # Mirror the manual grade into the authoritative supa_ledger pool so
        # the My Bets / Model bankroll cards (which read supa_ledger, not the
        # local file) reflect it.  Personal pool for a confirmed bet, else the
        # model pool.  Best-effort + idempotent (settle() no-ops a bet that
        # isn't still active, and silently does nothing if the id isn't in the
        # pool because the two stores use different id schemes).
        try:
            from src import supa_ledger as _sl
            if (_sl.db.is_supabase() and updated
                    and g_result in ("win", "loss", "push", "void")):
                _pool = _sl.personal() if (updated or {}).get("confirmed") else _sl.model()
                _match = next((b for b in _pool.active_bets()
                               if b.get("bet_id") == bet_id), None)
                if _match is not None:
                    _pool.settle(_match, g_result)
        except Exception as _se:                                          # noqa: BLE001
            _eprint(f"MARK-BET supa_ledger mirror failed: {_se}")

        return (jsonify({"success": True, "bet": _py(updated)}) if updated
                else (jsonify({"success": False, "error": "bet not found"}), 404))
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


def _explorer_last_settlement() -> str | None:
    """Most recent settled_at across both ledgers + props (or admin override)."""
    latest = None
    try:
        from src import db as _db
        ov = _db.cache_get("admin_last_settlement") if _db.is_supabase() else None
        if isinstance(ov, dict):
            v = (ov.get("data") or {}).get("value") if isinstance(ov.get("data"), dict) else None
            if v:
                return v
    except Exception:                                                       # noqa: BLE001
        pass
    cands: list[str] = []
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = Ledger(path=path, starting_bankroll=1000.0)
            cands += [b.get("settled_at") for b in (led.data.get("history") or []) if b.get("settled_at")]
        except Exception:                                                   # noqa: BLE001
            pass
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        cands += [p.get("settled_at") for p in _ppt.get_all() if p.get("settled_at")]
    except Exception:                                                       # noqa: BLE001
        pass
    cands = [c for c in cands if c]
    return max(cands) if cands else latest


@app.route("/api/admin/explorer/timestamps", methods=["POST"])
def admin_explorer_timestamps():
    """Last analyzed (mlb/wnba), last props refresh, last settlement."""
    try:
        ts = _read_analysis_timestamps() or {}
        props_refresh = None
        try:
            from src.props_scored_cache import load_scored_props
            props_refresh = (load_scored_props() or {}).get("generated_at")
        except Exception:                                                   # noqa: BLE001
            pass
        return jsonify({"success": True,
                        "mlb":  (ts.get("mlb")  or {}).get("analyzed_at"),
                        "wnba": (ts.get("wnba") or {}).get("analyzed_at"),
                        "props_refresh": props_refresh,
                        "settlement":    _explorer_last_settlement()})
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/explorer/set_timestamp", methods=["POST"])
def admin_explorer_set_timestamp():
    """Override a timestamp.  field ∈ {mlb, wnba, props_refresh, settlement}."""
    body  = request.get_json() or {}
    field = (body.get("field") or "").lower()
    value = (body.get("value") or "").strip()
    if not value:
        return jsonify({"success": False, "error": "value required"}), 400
    try:
        # Normalise to an aware datetime / ISO string.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        iso = dt.isoformat()
    except Exception:                                                       # noqa: BLE001
        return jsonify({"success": False, "error": "value must be ISO-8601"}), 400
    try:
        from src import db as _db
        if field in ("mlb", "wnba"):
            _write_analysis_timestamp(field, dt)
            state = _analysis_state if field == "mlb" else _wnba_analysis_state
            try:
                state["last_analyzed_at"] = dt
            except Exception:                                               # noqa: BLE001
                pass
            return jsonify({"success": True, "field": field, "value": iso})
        if field == "props_refresh":
            try:
                from src.props_scored_cache import load_scored_props
                payload = load_scored_props() or {}
                payload["generated_at"] = iso
                key = f"props_scored_mlb_{payload.get('date') or _today_et()}"
                _db.cache_set(key, "mlb", payload.get("date") or _today_et(), payload)
            except Exception as exc:                                        # noqa: BLE001
                return jsonify({"success": False, "error": str(exc)}), 500
            return jsonify({"success": True, "field": field, "value": iso})
        if field == "settlement":
            _db.cache_set("admin_last_settlement", None, _today_et(), {"value": iso})
            return jsonify({"success": True, "field": field, "value": iso})
        return jsonify({"success": False, "error": "unknown field"}), 400
    except Exception as exc:                                                # noqa: BLE001
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/admin/model/repick", methods=["POST"])
def admin_model_repick():
    """
    Wipe today's model picks for enabled sports, then re-pick the top 5 by
    confidence per category using the cached analysis results.  Returns the
    new picks payload.  Body (optional): {sport: "mlb"|"wnba"|"both"} —
    defaults to "both" but only enabled sports actually get repicked.
    """
    try:
        settings = _load_model_settings()
        sport_filter = (request.json or {}).get("sport", "both").strip().lower()
        if sport_filter not in ("mlb", "wnba", "both"):
            return jsonify({"success": False, "error": "sport must be 'mlb', 'wnba', or 'both'"}), 400

        want_mlb  = settings["mlb_enabled"]  and sport_filter in ("mlb",  "both")
        want_wnba = settings["wnba_enabled"] and sport_filter in ("wnba", "both")

        mlb_results  = (_analysis_state.get("results")      or []) if want_mlb  else []
        wnba_results = (_wnba_analysis_state.get("results") or []) if want_wnba else []

        if not mlb_results and not wnba_results:
            return jsonify({
                "success": False,
                "error":   "No cached analysis available for the enabled sports — run analysis first.",
            }), 400

        mlb_ledger  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba_ledger = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        payload = select_daily_picks(
            mlb_results, wnba_results, mlb_ledger, wnba_ledger,
            today_only=True, selection_mode="confidence",
        )
        return jsonify({"success": True, "picks": _py(payload)})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc)), "detail": _redact(traceback.format_exc())}), 500


@app.route("/api/reset-sport", methods=["POST"])
def reset_sport():
    """
    Clear today's analysis snapshot and timestamp for a single sport so a
    fresh analysis run can overwrite it.  Does NOT touch bet history or bankrolls.

    Body JSON: { "sport": "mlb" | "wnba" }
    """
    try:
        sport = (request.json or {}).get("sport", "").strip().lower()
        if sport not in ("mlb", "wnba"):
            return jsonify({"success": False, "error": "sport must be 'mlb' or 'wnba'"}), 400

        # 1. Clear snapshot entry so _write_daily_snapshot write-once guard is lifted
        _clear_snapshot_sport(sport)

        # 2. Clear analysis timestamp for this sport
        try:
            ts_data = _read_analysis_timestamps()
            if sport in ts_data:
                del ts_data[sport]
                Path("data").mkdir(exist_ok=True)
                _ANALYSIS_TIMESTAMPS_FILE.write_text(
                    json.dumps(ts_data, indent=2), encoding="utf-8"
                )
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        # 3. Clear in-memory analysis state so auto-status endpoints reflect the reset
        if sport == "mlb":
            _analysis_state["last_analyzed_at"] = None
        else:
            _wnba_analysis_state["last_analyzed_at"] = None

        _eprint(f"RESET-SPORT: {sport.upper()} snapshot + timestamp cleared by user")
        return jsonify({"success": True, "sport": sport,
                        "message": f"{sport.upper()} analysis data cleared."})
    except Exception as exc:
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


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
        return jsonify({"success": False, "error": _redact(str(exc)),
                        "detail": _redact(traceback.format_exc())}), 500








# ── Daily budget helpers (FIX 2/3) ───────────────────────────────────────────

def _personal_bankroll_now() -> float:
    """Current personal bankroll -- the single source of truth for all bet
    sizing + budget math.  Read from the MLB ledger (which restores from
    Supabase on boot)."""
    try:
        _l = Ledger(path="data/ledger.json", starting_bankroll=1000.0)
        return float(_l.data.get("personal_bankroll")
                     or _l.data.get("personal_starting_bankroll") or 0.0)
    except Exception:                                                      # noqa: BLE001
        return 0.0




def _personal_spent_today() -> float:
    """Sum of confirmed personal-side stakes on open bets placed today (ET)
    across both sport ledgers -- the running daily total for budget gating."""
    today = _today_et()
    total = 0.0
    for _p in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            _l = Ledger(path=_p, starting_bankroll=1000.0)
            total += _l._daily_exposure(today, confirmed_only=True)
        except Exception:                                                  # noqa: BLE001
            pass
    return total


def _budget_status(new_amount: float) -> dict:
    """Snapshot for gating a new *new_amount* stake against today's budget
    (20% of the current personal bankroll).  ``over`` = adding it would
    exceed the remaining daily budget."""
    from src.ledger import compute_daily_budget
    budget = compute_daily_budget(_personal_bankroll_now())["total"]
    spent = _personal_spent_today()
    remaining = max(0.0, budget - spent)
    over = (spent + float(new_amount or 0.0)) > budget + 0.001
    return {"budget": round(budget, 2), "spent": round(spent, 2),
            "remaining": round(remaining, 2), "over": bool(over)}


@app.route("/api/ledger/confirm/<game_id>", methods=["POST"])
def confirm_bet(game_id: str):
    """Mark a model-tracked bet as user-confirmed, or add it fresh if missing."""
    data     = request.get_json() or {}
    bankroll = float(data.get("bankroll", _analysis_state["bankroll"] or 250))
    force    = bool(data.get("force"))
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
            gate = _budget_status(conf_amt)
            if gate["over"] and not force:
                return jsonify({"over_budget": True, "amount": conf_amt, **gate})
            bet["confirmed"]        = True
            bet["confirmed_amount"] = conf_amt
            # Immediately deduct confirmed stake from personal bankroll
            if conf_amt > 0:
                ledger.data["personal_bankroll"] = round(
                    ledger.data["personal_bankroll"] - conf_amt, 2
                )
            ledger.save()
            return jsonify({"success": True, "confirmed_amount": conf_amt})

    # Not yet in ledger — pull from analysis cache and add as full bet.
    # _find_analysis_row handles both the raw nested shape AND the flat
    # serialized passthrough shape (snapshot hydration path) so we
    # never crash with KeyError('game') here.
    raw = _find_analysis_row(_analysis_state, game_id)
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

    # Tier from the picked-outcome probability (model_p), not model agreement.
    ml_conf = confidence_tier_from_prob(model_p)
    model_amt, conf_amt = ledger.kelly_amounts(model_p, odds)
    gate = _budget_status(conf_amt)
    if gate["over"] and not force:
        return jsonify({"over_budget": True, "amount": round(conf_amt, 2), **gate})
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


@app.route("/api/wnba/ledger/confirm/<game_id>", methods=["POST"])
def confirm_bet_wnba(game_id: str):
    """WNBA mirror of /api/ledger/confirm/<game_id>.

    The MLB version above is hardcoded to data/ledger.json + _analysis_state.
    The NiceGUI Track button needs an equivalent that writes to
    data/wnba_ledger.json + reads from _wnba_analysis_state.  Same shape of
    response so the front-end uses one call pattern across sports.
    """
    data     = request.get_json() or {}
    bankroll = float(data.get("bankroll", _wnba_analysis_state["bankroll"] or 1000))
    force    = bool(data.get("force"))
    sport_cfg = SPORTS["wnba"]
    ledger   = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)

    for bet in ledger.data["open_bets"]:
        if bet["game_id"] == game_id and bet.get("bet_type", "single") == "single":
            if bet["confirmed"]:
                return jsonify({"error": "Already confirmed"}), 409
            _, conf_amt = ledger.kelly_amounts(bet["model_prob"], bet["american_odds"])
            conf_amt = round(conf_amt, 2)
            gate = _budget_status(conf_amt)
            if gate["over"] and not force:
                return jsonify({"over_budget": True, "amount": conf_amt, **gate})
            bet["confirmed"]        = True
            bet["confirmed_amount"] = conf_amt
            if conf_amt > 0:
                ledger.data["personal_bankroll"] = round(
                    ledger.data["personal_bankroll"] - conf_amt, 2
                )
            ledger.save()
            return jsonify({"success": True, "confirmed_amount": conf_amt})

    raw = _find_analysis_row(_wnba_analysis_state, game_id)
    if raw is None:
        return jsonify({"error": "Game not found in current WNBA analysis"}), 404

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

    ml_conf = confidence_tier_from_prob(model_p)
    model_amt, conf_amt = ledger.kelly_amounts(model_p, odds)
    gate = _budget_status(conf_amt)
    if gate["over"] and not force:
        return jsonify({"over_budget": True, "amount": round(conf_amt, 2), **gate})
    ledger.add_bet(
        game=g, sport="wnba", sport_key=sport_cfg.odds_key,
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
        raw = _find_analysis_row(_analysis_state, game_id)
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
    force     = bool(data.get("force"))
    sport     = _analysis_state.get("sport") or "mlb"
    sport_cfg = SPORTS[sport]

    raw = _find_analysis_row(_analysis_state, game_id)
    if raw is None:
        return jsonify({"error": "Game not found in current analysis"}), 404

    g = raw.get("game") or {}
    prop_line = None
    if bet_type == "run_line":
        # Raw nested rows carry rl_pred; snapshot-hydrated flat rows carry
        # the serialized "run_line" dict (same field names).  Fall back so
        # tracking works on either shape instead of 404-ing on flat rows.
        pred = raw.get("rl_pred") or raw.get("run_line")
        if not pred:
            return jsonify({
                "success":     False,
                "unavailable": True,
                "message":     "Run line prediction not available for this game",
            }), 200
        side        = pred.get("side")
        team        = pred.get("pick_team")
        odds        = pred.get("pick_odds")
        model_p     = pred.get("pick_prob")
        edge        = abs(pred.get("edge") or 0.0)
        label       = "run_line"
        prop_line   = -float(pred.get("run_line_point", -1.5))  # settlement threshold = -run_line_point
    elif bet_type == "totals":
        pred = raw.get("totals_pred") or raw.get("totals")
        if not pred:
            return jsonify({
                "success":     False,
                "unavailable": True,
                "message":     "Totals prediction not available for this game",
            }), 200
        side        = pred.get("direction")   # "over" or "under"
        team        = f"{(pred.get('direction') or '').title()} {pred.get('total_line')}"
        odds        = pred.get("pick_odds")
        model_p     = pred.get("pick_prob")
        edge        = abs(pred.get("edge") or 0.0)
        label       = "totals"
        prop_line   = float(pred.get("total_line"))
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

    gate = _budget_status(conf_dollars)
    if gate["over"] and not force:
        return jsonify({"over_budget": True, "amount": conf_dollars, **gate})

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


@app.route("/api/props/track", methods=["POST"])
def track_prop_pick():
    """Track a player-prop pick from the Props page.

    Body (JSON):
        player          str   — player full name
        market          str   — e.g. "pitcher_strikeouts"
        line            float — the prop line
        side            str   — "Over" or "Under"
        odds            int   — American odds (e.g. -115)
        confidence      float — model confidence 0..1
        predicted_value float — model's numeric prediction (may be null)
        team            str   — team label string
        event_id        str   — Odds API event id (may be null)
        commence_time   str   — ISO-8601 game start (may be null)

    Returns {"success": true, "id": "<uuid>"} or an error dict.
    """
    data = request.get_json() or {}

    player  = (data.get("player") or "").strip()
    market  = (data.get("market") or "").strip()
    side    = (data.get("side")   or "Over").strip().title()
    if not player or not market:
        return jsonify({"error": "player and market are required"}), 400

    try:
        line = float(data["line"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "line must be a number"}), 400

    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()   # always read the freshest state before writing

        event_id = data.get("event_id")
        pick_id = _ppt.record_prop_pick(
            player          = player,
            market          = market,
            line            = line,
            side            = side,
            odds            = data.get("odds"),
            confidence      = float(data.get("confidence") or 0),
            predicted_value = data.get("predicted_value"),
            team            = data.get("team") or "",
            event_id        = event_id,
            commence_time   = data.get("commence_time"),
        )
        if pick_id is None:
            return jsonify({"error": "This pick is already tracked"}), 409
        # Return the recommended dollar stake so the track toast can show it
        # (e.g. "Tracked: ... ($10.00)") like the game-pick toasts do.
        return jsonify({"success": True, "id": pick_id,
                        "amount": float(_ppt.flat_stake())})
    except Exception as exc:                                                # noqa: BLE001
        import traceback as _tb
        _eprint(f"PROPS-TRACK: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/props/settle_open", methods=["POST"])
def settle_open_props():
    """Attempt to auto-settle all pending props picks whose games have
    finished.  Safe to call repeatedly — picks with no game-log row yet
    stay pending.  Returns the settle summary from props_picks_tracker.
    """
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        summary = _ppt.settle_pending()
        return jsonify({"success": True, **summary})
    except Exception as exc:                                                # noqa: BLE001
        import traceback as _tb
        _eprint(f"PROPS-SETTLE: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


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
    # Persist the real balance to the Supabase personal pool -- this is the
    # source of truth the My Bets bankroll card + daily limit read from, so
    # without this write the admin value never actually shows up.  Refresh
    # the daily limit immediately so it tracks the new bankroll.
    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            _sl.personal().set_bankroll(new_br)
            _sl.personal().refresh_daily_limit()
    except Exception as _be:                                               # noqa: BLE001
        _eprint(f"SET-BANKROLL personal pool write failed: {_be}")
    # Editing the bankroll immediately recalculates the daily budget (20% of
    # the new bankroll) + per-bet floor/ceiling so the My Bets banner and the
    # budget gate reflect the change right away (FIX 3).
    budget = _persist_daily_budget(new_br)
    return jsonify({"success": True, "bankroll": new_br, "budget": budget})


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
    # Persist to the Supabase model pool (the source of truth the Model
    # bankroll card reads from).
    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            _sl.model().set_bankroll(new_br)
    except Exception as _be:                                               # noqa: BLE001
        _eprint(f"SET-MODEL-BANKROLL pool write failed: {_be}")
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


# ── My Bets: per-card remove / edit + manual add ───────────────────────────────

def _mybets_add_options() -> dict:
    """Everything the Add-Bet flow needs for autocomplete + confidence
    prefill: today's games (with per-bet-type model picks) and today's
    scored props.  Built from the in-memory analysis state + scored-props
    cache — no external API call."""
    try:
        hydrate_state()
    except Exception:                                                       # noqa: BLE001
        pass

    games: list[dict] = []
    for sport, state in (("mlb", _analysis_state), ("wnba", _wnba_analysis_state)):
        for r in (state.get("results") or []):
            if not isinstance(r, dict):
                continue
            g   = r.get("game") if isinstance(r.get("game"), dict) else {}
            gid = r.get("game_id") or r.get("id") or g.get("id")
            home = r.get("home_team") or g.get("home_team")
            away = r.get("away_team") or g.get("away_team")
            if not (gid and home and away):
                continue
            pred = r.get("prediction") if isinstance(r.get("prediction"), dict) else {}
            hwp  = r.get("home_win_prob")
            if hwp is None:
                hwp = pred.get("home_win_prob")
            rl  = (r.get("run_line") if isinstance(r.get("run_line"), dict)
                   else (r.get("rl_pred") if isinstance(r.get("rl_pred"), dict) else None))
            tot = (r.get("totals") if isinstance(r.get("totals"), dict)
                   else (r.get("totals_pred") if isinstance(r.get("totals_pred"), dict) else None))
            game_obj = {
                "game_id":       str(gid),
                "sport":         sport,
                "home_team":     home,
                "away_team":     away,
                "commence_time": r.get("commence_time") or g.get("commence_time") or "",
                "home_odds":     r.get("home_odds") or g.get("h2h_home_odds"),
                "away_odds":     r.get("away_odds") or g.get("h2h_away_odds"),
                "home_win_prob": hwp,
                "pick_team":     r.get("pick_team"),
                "pick_prob":     r.get("pick_prob"),
            }
            if rl:
                game_obj["run_line"] = {
                    "pick_team":      rl.get("pick_team"),
                    "pick_prob":      rl.get("pick_prob"),
                    "pick_odds":      rl.get("pick_odds"),
                    "run_line_point": rl.get("run_line_point"),
                }
            if tot:
                game_obj["totals"] = {
                    "direction":  tot.get("direction"),
                    "total_line": tot.get("total_line"),
                    "pick_prob":  tot.get("pick_prob"),
                    "over_odds":  tot.get("over_odds"),
                    "under_odds": tot.get("under_odds"),
                    "pick_odds":  tot.get("pick_odds"),
                }
            games.append(game_obj)

    props: list[dict] = []
    try:
        from src.props_scored_cache import load_scored_props
        for p in (load_scored_props().get("picks") or []):
            props.append({
                "player":          p.get("player"),
                "team":            p.get("team"),
                "market":          p.get("market"),
                "line":            p.get("line"),
                "side":            p.get("side") or p.get("recommendation") or "Over",
                "confidence":      p.get("confidence"),
                "predicted_value": p.get("predicted_value"),
                "best_odds":       p.get("best_odds"),
                "over_odds":       p.get("over_odds"),
                "under_odds":      p.get("under_odds"),
                "event_id":        p.get("event_id"),
                "commence_time":   p.get("commence_time"),
                "home_team":       p.get("home_team"),
                "away_team":       p.get("away_team"),
            })
    except Exception:                                                       # noqa: BLE001
        pass

    return {"games": games, "props": props}


@app.route("/api/mybets/add_options", methods=["POST"])
def mybets_add_options():
    """Return today's games + props for the Add-Bet autocomplete."""
    return jsonify(_py(_mybets_add_options()))


@app.route("/api/mybets/remove", methods=["POST"])
def mybets_remove():
    """Remove one tracked bet (game or prop) from the local file + Supabase."""
    data   = request.get_json() or {}
    kind   = (data.get("kind") or "game").lower()
    bet_id = data.get("id")
    if not bet_id:
        return jsonify({"error": "id required"}), 400

    if kind == "prop":
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        ok = _ppt.remove_pick(bet_id)
        return (jsonify({"success": True}) if ok
                else (jsonify({"error": "Pick not found"}), 404))

    sport  = (data.get("sport") or "mlb").lower()
    path   = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
    ledger = Ledger(path=path, starting_bankroll=1000.0)
    removed = ledger.remove_bet(bet_id)
    if removed is None:
        return jsonify({"error": "Bet not found"}), 404
    try:
        from src import db as _db
        _db.delete_bet(bet_id)
    except Exception:                                                       # noqa: BLE001
        pass
    return jsonify({"success": True})


@app.route("/api/mybets/edit", methods=["POST"])
def mybets_edit():
    """Edit fields (odds / line / actual_payout / notes) on one tracked bet."""
    data   = request.get_json() or {}
    kind   = (data.get("kind") or "game").lower()
    bet_id = data.get("id")
    if not bet_id:
        return jsonify({"error": "id required"}), 400
    fields = {k: data.get(k) for k in ("odds", "line", "amount", "actual_payout",
                                       "notes", "confidence")
              if data.get(k) is not None}

    if kind == "prop":
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        updated = _ppt.update_pick(bet_id, **fields)
        return (jsonify({"success": True, "bet": _py(updated)}) if updated
                else (jsonify({"error": "Pick not found"}), 404))

    sport  = (data.get("sport") or "mlb").lower()
    path   = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
    ledger = Ledger(path=path, starting_bankroll=1000.0)
    updated = ledger.update_bet(bet_id, **fields)
    if updated is None:
        return jsonify({"error": "Bet not found"}), 404
    return jsonify({"success": True, "bet": _py(updated)})


@app.route("/api/mybets/add", methods=["POST"])
def mybets_add():
    """Manually add a bet (game or prop) with result=pending."""
    import uuid as _uuid
    from src.kelly import american_to_decimal, tracked_bet_kelly

    data     = request.get_json() or {}
    kind     = (data.get("kind") or "game").lower()
    bankroll = float(data.get("bankroll") or 0)

    if kind == "prop":
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        try:
            line = float(data["line"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "line must be a number"}), 400
        player = (data.get("player") or "").strip()
        market = (data.get("market") or "").strip()
        if not player or not market:
            return jsonify({"error": "player and market are required"}), 400
        pid = _ppt.add_manual_pick(
            player=player, market=market, line=line,
            side=(data.get("side") or "Over").strip().title(),
            odds=data.get("odds"),
            confidence=float(data.get("confidence") or 0),
            predicted_value=data.get("predicted_value"),
            team=data.get("team") or "",
            event_id=data.get("event_id"),
            commence_time=data.get("commence_time"),
            notes=data.get("notes"),
        )
        if pid is None:
            return jsonify({"error": "This pick is already tracked"}), 409
        # Stake into the rebuilt personal ledger (frozen, Supabase-only).
        try:
            from src import supa_ledger as _sl
            if _sl.db.is_supabase():
                pled  = _sl.personal()
                conf  = float(data.get("confidence") or 0)
                from src.kelly import tracked_bet_kelly as _tbk
                p_stake, _ = _tbk(conf, data.get("odds"), pled.bankroll()) \
                    if (0.0 < conf < 1.0 and data.get("odds")) else (0.0, None)
                if p_stake and p_stake > 0:
                    pled.place(bet_id=str(pid), sport="mlb", bet_type=market,
                               selection=(data.get("side") or "Over"),
                               odds=data.get("odds"), stake=p_stake, kind="prop",
                               game_id=str(data.get("event_id") or pid),
                               player_name=player, meta={"line": line})
        except Exception as _ple:                                          # noqa: BLE001
            _eprint(f"MYBETS-ADD prop personal-ledger place failed: {_ple}")
        return jsonify({"success": True, "id": pid})

    # ── Game bet ────────────────────────────────────────────────────────────
    sport     = (data.get("sport") or "mlb").lower()
    sport_cfg = SPORTS.get(sport) or SPORTS["mlb"]
    bet_type  = (data.get("bet_type") or "ml").lower()   # ml | run_line | total
    team      = (data.get("team") or "").strip()
    home_team = (data.get("home_team") or "").strip()
    away_team = (data.get("away_team") or "").strip()
    try:
        odds = int(data.get("odds"))
    except (TypeError, ValueError):
        return jsonify({"error": "odds must be an integer"}), 400
    prob = float(data.get("confidence") or 0)
    if not (0.0 < prob < 1.0):
        return jsonify({"error": "confidence must be between 0 and 1"}), 400

    edge = prob - (1.0 / american_to_decimal(odds))
    stake, _flag = tracked_bet_kelly(prob, odds, bankroll)

    path   = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
    ledger = Ledger(path=path, starting_bankroll=1000.0)
    game = {
        "id":            data.get("game_id") or str(_uuid.uuid4()),
        "home_team":     home_team,
        "away_team":     away_team,
        "commence_time": data.get("commence_time") or "",
    }

    label, side, prop_line, bet_team = "single", "", None, team
    if bet_type == "run_line":
        label = "spread" if sport == "wnba" else "run_line"
        side  = "home" if team == home_team else "away"
        try:
            prop_line = -float(data.get("line"))
        except (TypeError, ValueError):
            prop_line = None
    elif bet_type == "total":
        label = "totals"
        side  = (data.get("side") or "over").lower()
        try:
            tl = float(data.get("line"))
            prop_line = tl
            bet_team  = f"{side.title()} {tl:g}"
        except (TypeError, ValueError):
            prop_line = None
    else:  # moneyline
        side = "home" if team == home_team else "away"

    new_id = ledger.add_bet(
        game=game, sport=sport, sport_key=sport_cfg.odds_key,
        side=side, team=bet_team, odds=odds, model_prob=prob, edge=edge,
        model_amount=0.0, confirmed=True, confirmed_amount=stake,
        bet_type=label, prop_line=prop_line, confidence_tier="manual",
    )
    if data.get("notes"):
        b = next((x for x in ledger.data["open_bets"] if x.get("id") == new_id), None)
        if b is not None:
            b["notes"] = str(data["notes"])
    ledger.save()

    # Stake into the rebuilt personal ledger (frozen at placement,
    # Supabase-only).  Sized off the personal pool's CURRENT bankroll; once
    # placed the stake never recalculates (bankroll edits can't change it).
    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            pled = _sl.personal()
            p_stake, _ = tracked_bet_kelly(prob, odds, pled.bankroll())
            if not p_stake or p_stake <= 0:
                p_stake = stake
            if p_stake and p_stake > 0:
                pled.place(bet_id=str(new_id), sport=sport, bet_type=label,
                           selection=bet_team, odds=odds, stake=p_stake,
                           kind="game", game_id=game["id"],
                           meta={"line": prop_line, "side": side,
                                 "home_team": home_team, "away_team": away_team})
    except Exception as _ple:                                              # noqa: BLE001
        _eprint(f"MYBETS-ADD game personal-ledger place failed: {_ple}")
    return jsonify({"success": True, "id": new_id, "stake": stake})


# ── /mybets Flask port (PR #339) ────────────────────────────────────────────
# Helpers + view model + GET routes for the Tailwind My Bets page.  Backend
# endpoints (/api/mybets/add_options, /add, /edit, /remove) above this block
# are already wired and consumed by mybets.js -- no new POST routes here.

_MB_MARKET_LABEL: dict[str, str] = {
    "pitcher_strikeouts":   "Ks",
    "pitcher_outs":         "Outs",
    "pitcher_hits_allowed": "H Allow",
    "pitcher_walks":        "BB Allow",
    "pitcher_earned_runs":  "ER",
    "batter_hits":          "Hits",
    "batter_total_bases":   "Total Bases",
    "batter_home_runs":     "Home Runs",
    "batter_rbis":          "RBIs",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
}

_MB_TYPE_LABEL: dict[str, str] = {
    "single":   "Moneyline",
    "run_line": "Run Line",
    "spread":   "Spread",
    "totals":   "Total",
}

_MB_TYPE_SHORT: dict[str, str] = {
    "single": "ML", "run_line": "RL", "spread": "SPD", "totals": "TOT",
}


def _mb_odds_str(o) -> str:
    if not isinstance(o, (int, float)):
        return "—"
    n = int(o)
    return f"+{n}" if n > 0 else str(n)


def _mb_bet_line_value(b: dict):
    """Bettor-facing line for a game bet (None for ML).  Run line/spread are
    stored negated (settlement threshold); totals stored as-is."""
    bt = (b.get("bet_type") or "single").lower()
    pl = b.get("prop_line")
    if pl is None:
        return None
    try:
        return -float(pl) if bt in ("run_line", "spread") else float(pl)
    except (TypeError, ValueError):
        return None


def _mb_bet_line_str(b: dict) -> str:
    """Signed handicap for RL / spread display (empty for ML / totals)."""
    bt = (b.get("bet_type") or "single").lower()
    if bt not in ("run_line", "spread"):
        return ""
    v = _mb_bet_line_value(b)
    if v is None:
        return ""
    return f"{v:+g}"


def _mb_confidence_pct(b: dict):
    p = b.get("model_prob")
    if not isinstance(p, (int, float)):
        return None
    return int(round(float(p) * 100))


def _mb_placed_date(b: dict) -> str:
    iso = b.get("placed_at") or b.get("commence_time") or ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%b %-d")
    except Exception:                                                      # noqa: BLE001
        return ""


def _mb_game_datetime_str(b: dict) -> str:
    """Game date + start time in ET, e.g. 'May 25 · 6:11 PM ET'."""
    iso = b.get("commence_time")
    if iso:
        try:
            from datetime import datetime
            dt = (datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                  .astimezone(_ET))
            return dt.strftime("%b %-d · %-I:%M %p ET")
        except Exception:                                                  # noqa: BLE001
            pass
    return _mb_placed_date(b)


def _mb_matchup_str(b: dict) -> str:
    away = b.get("away_team") or ""
    home = b.get("home_team") or ""
    if away and home:
        return f"{away} @ {home}"
    return b.get("game") or ""


def _mb_shape_game_bet(b: dict, sport: str, settled: bool) -> dict:
    """Pre-format one game bet (open or settled) for the unified list.
    Mirrors pages/mybets.py _game_bet_row's render logic."""
    bet_type = (b.get("bet_type") or "single").lower()
    team     = b.get("bet_team") or b.get("parlay_name") or "—"
    line_s   = _mb_bet_line_str(b)
    odds_s   = _mb_odds_str(b.get("american_odds"))
    conf     = _mb_confidence_pct(b)
    amount   = float(b.get("confirmed_amount") or 0)
    pnl      = float(b.get("confirmed_pnl")    or 0) if settled else 0.0
    result   = (b.get("result") or "").lower()

    short = _MB_TYPE_SHORT.get(bet_type, "")
    badge = f"{sport.upper()} · {short}" if short else sport.upper()

    if bet_type == "single":
        pick = f"{team} ML"
    elif bet_type in ("run_line", "spread") and line_s:
        pick = f"{team} {line_s}"
    else:
        pick = team

    sub_parts: list[str] = []
    matchup = _mb_matchup_str(b)
    if matchup:
        sub_parts.append(matchup)
    if conf is not None:
        sub_parts.append(f"{conf}% confidence")
    if odds_s != "—":
        sub_parts.append(odds_s)

    # Money + result colour mirrors NiceGUI's branching.
    if settled and result == "win":
        money_text, money_color, pick_color = f"+${pnl:.2f}", "pos", "pos"
    elif settled and result == "loss":
        money_text, money_color, pick_color = f"-${amount:.2f}", "neg", "neg"
    elif settled and result == "push":
        money_text, money_color, pick_color = "$0.00", "dim", "text"
    else:
        money_text, money_color, pick_color = f"${amount:.2f}", "text", "text"
    status_text  = result.upper() if settled and result else "PENDING"
    status_color = {"win": "pos", "loss": "neg", "push": "warn",
                    "void": "dim"}.get(result, "dim")
    border_color = (status_color if settled and result in ("win", "loss", "push")
                    else "border")

    return {
        "kind":            "game",
        "sport":           sport,
        "id":              b.get("id"),
        "bet_type":        bet_type,
        "badge":           badge,
        "pick":            pick,
        "pick_color":      pick_color,
        "sub_line":        "  ·  ".join(sub_parts),
        "game_dt":         _mb_game_datetime_str(b),
        "money_text":      money_text,
        "money_color":     money_color,
        "status_text":     status_text,
        "status_color":    status_color,
        "border_color":    border_color,
        "settled":         settled,
        # Raw fields needed by the edit panel:
        "odds":            b.get("american_odds"),
        "line":            _mb_bet_line_value(b),
        "has_line":        bet_type != "single",
        "amount":          amount,
        "actual_payout":   b.get("actual_payout"),
        "confidence_pct":  conf,
        # Sort keys (used by the snapshot poll; not surfaced to the template):
        "commence_time":   b.get("commence_time") or "",
        "settled_at":      b.get("settled_at") or "",
    }


def _mb_shape_prop_bet(b: dict, settled: bool) -> dict:
    """Pre-format one prop bet (open or settled) for the unified list.
    Mirrors pages/mybets.py _prop_bet_row."""
    side    = (b.get("side") or "Over").strip().title()
    player  = b.get("player") or "—"
    raw_mkt = b.get("market", "")
    market  = _MB_MARKET_LABEL.get(raw_mkt, (raw_mkt or "").replace("_", " ").title())
    line    = b.get("line")
    line_s  = f"{float(line):.1f}" if line is not None else "—"
    conf    = b.get("confidence")
    pv      = b.get("predicted_value")
    odds    = b.get("odds")
    odds_s  = (f"+{odds}" if (isinstance(odds, int) and odds > 0) else
               (str(odds) if isinstance(odds, int) else None))
    matchup = b.get("game") or b.get("team") or ""
    game_dt = _mb_game_datetime_str(b)
    result  = (b.get("result") or "").lower()

    pick_line = " ".join(s for s in (player, side, line_s, market) if s).strip()
    l2 = [x for x in (
        matchup,
        (f"{conf * 100:.0f}% confidence" if isinstance(conf, (int, float)) else None),
        (f"{float(pv):.1f} projected" if pv is not None else None),
    ) if x]
    l3 = [x for x in (odds_s, game_dt) if x]

    # Money: pending shows stake + potential, settled shows pnl.
    money_lines: list[dict] = []
    try:
        from src.props_picks_tracker import _FLAT_STAKE, _payout_multiplier
        stake     = float(_FLAT_STAKE)
        potential = round(_FLAT_STAKE * _payout_multiplier(odds), 2)
        mpnl      = float(b.get("model_pnl") or 0.0)
        if settled and result in ("won", "win"):
            money_lines = [{"text": f"+${mpnl:.2f}",       "color": "pos"}]
        elif settled and result in ("lost", "loss"):
            money_lines = [{"text": f"-${abs(mpnl):.2f}",  "color": "neg"}]
        elif settled and result == "void":
            money_lines = [{"text": "$0.00",               "color": "dim"}]
        else:
            money_lines = [
                {"text": f"${stake:.2f}",                  "color": "text"},
                {"text": f"to win ${potential:.2f}",       "color": "dim"},
            ]
    except Exception:                                                      # noqa: BLE001
        money_lines = []

    if settled and result in ("won", "win"):
        pick_color, status_color = "pos", "pos"
    elif settled and result in ("lost", "loss"):
        pick_color, status_color = "neg", "neg"
    elif settled and result == "void":
        pick_color, status_color = "text", "warn"
    else:
        pick_color, status_color = "text", "dim"
    status_text  = result.upper() if settled and result else "PENDING"
    border_color = (status_color
                    if settled and result in ("won", "win", "lost", "loss", "void")
                    else "border")

    return {
        "kind":           "prop",
        "sport":          "mlb",
        "id":             b.get("id"),
        "market":         raw_mkt,
        "badge":          "MLB · PROP",
        "pick":           pick_line,
        "pick_color":     pick_color,
        "sub_line":       "  ·  ".join(l2),
        "extra_line":     "  ·  ".join(l3),
        "money_lines":    money_lines,
        "status_text":    status_text,
        "status_color":   status_color,
        "border_color":   border_color,
        "settled":        settled,
        "odds":           b.get("odds"),
        "line":           b.get("line"),
        "has_line":       True,
        "confidence_pct": (int(round(float(conf) * 100))
                           if isinstance(conf, (int, float)) else None),
        "actual_payout":  b.get("actual_payout"),
        "commence_time":  b.get("commence_time") or "",
        "settled_at":     b.get("settled_at") or b.get("recorded_at") or "",
    }


def _mb_bankroll_block() -> dict:
    """START / CURRENT / P/L / AT RISK + Today's Budget.  Source of truth is
    src.supa_ledger.personal() when available; falls back to the local Ledger
    files (same logic as pages/mybets.py _personal_bankroll)."""
    start = current = pnl = at_risk = 0.0
    budget_total = max_per_bet = remaining = 0.0
    mlb = wnba = None
    supa = None

    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            supa = _sl.personal()
    except Exception:                                                      # noqa: BLE001
        supa = None

    if supa is not None:
        try:
            start   = float(supa.starting())
            current = float(supa.bankroll())
            pnl     = current - start
            at_risk = float(sum(float(b.get("stake") or 0) for b in supa.active_bets()))
            limit   = supa.daily_limit() or {}
            budget_total = float(limit.get("total")        or 0.0)
            max_per_bet  = float(limit.get("max_per_bet")  or 0.0)
            remaining    = float(limit.get("remaining")    or 0.0)
        except Exception:                                                  # noqa: BLE001
            supa = None

    if supa is None:
        try:
            mlb  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
            wnba = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
            s = mlb.get_summary()
            start   = float(s.get("personal_starting_bankroll", 1000))
            current = float(s.get("personal_bankroll", start))
            pnl     = current - start
            open_confirmed = (
                [b for b in (mlb.data.get("open_bets")  or []) if b.get("confirmed")]
                + [b for b in (wnba.data.get("open_bets") or []) if b.get("confirmed")]
            )
            at_risk = sum(float(b.get("confirmed_amount") or 0) for b in open_confirmed)
            from src.ledger import compute_daily_budget
            budget = compute_daily_budget(current) or {}
            budget_total = float(budget.get("total")       or 0.0)
            max_per_bet  = float(budget.get("max_per_bet") or 0.0)
            try:
                today_et = datetime.now(_ET).date().isoformat()
                spent = (mlb._daily_exposure(today_et, confirmed_only=True)
                         + wnba._daily_exposure(today_et, confirmed_only=True))
            except Exception:                                              # noqa: BLE001
                spent = 0.0
            remaining = max(0.0, budget_total - float(spent))
        except Exception:                                                  # noqa: BLE001
            start = current = 1000.0
            pnl = at_risk = budget_total = max_per_bet = remaining = 0.0

    return {
        "start":           start,
        "current":         current,
        "pnl":             pnl,
        "pnl_sign":        "+" if pnl >= 0 else "−",
        "pnl_abs":         abs(pnl),
        "pnl_color":       "pos" if pnl >= 0 else "neg",
        "at_risk":         at_risk,
        "budget_total":    budget_total,
        "budget_max":      max_per_bet,
        "budget_remaining": remaining,
        "remaining_color": "pos" if remaining > 0 else "neg",
    }


def _mb_unified_bets() -> tuple[list[dict], list[dict]]:
    """All confirmed game bets (MLB + WNBA) + all prop bets, shaped for the
    unified list.  Open bets sorted soonest-first; settled most-recent first.
    History capped at 50 prop bets like NiceGUI."""
    open_items: list[dict] = []
    settled_items: list[dict] = []

    for sport in ("mlb", "wnba"):
        try:
            path = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
            ledger = Ledger(path=path, starting_bankroll=1000.0)
        except Exception:                                                  # noqa: BLE001
            continue
        for b in (ledger.data.get("open_bets") or []):
            if not b.get("confirmed"):
                continue
            open_items.append(_mb_shape_game_bet(b, sport, settled=False))
        for b in (ledger.data.get("history") or []):
            if not b.get("confirmed"):
                continue
            settled_items.append(_mb_shape_game_bet(b, sport, settled=True))

    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        p_open  = _ppt.get_open()    or []
        p_hist  = _ppt.get_history() or []
    except Exception:                                                      # noqa: BLE001
        p_open, p_hist = [], []

    for b in p_open:
        open_items.append(_mb_shape_prop_bet(b, settled=False))
    for b in p_hist[:50]:
        settled_items.append(_mb_shape_prop_bet(b, settled=True))

    open_items.sort(key=lambda it: it.get("commence_time") or "9999-99-99")
    settled_items.sort(key=lambda it: it.get("settled_at") or "", reverse=True)
    return open_items, settled_items


def _mb_recommendations() -> tuple[list[dict], list[dict]]:
    """Today's untracked model picks (game + prop), sorted by confidence DESC.
    Mirrors pages/mybets.py _build_recommendations + _build_prop_recommendations."""
    try:
        hydrate_state()
    except Exception:                                                      # noqa: BLE001
        pass
    from components import track_button as _tb
    from components import live_score as _ls
    _backend = sys.modules[__name__]

    game_recs: list[dict] = []
    for sport, state in (("mlb", _analysis_state), ("wnba", _wnba_analysis_state)):
        for g in (state.get("results") or []):
            if g.get("_no_model") or g.get("_no_odds"):
                continue
            gid = g.get("id") or g.get("game_id")
            if not gid:
                continue
            try:
                started = _ls.game_has_started(
                    _backend,
                    commence_time=g.get("commence_time"),
                    home_team=g.get("home_team"),
                    away_team=g.get("away_team"),
                    sport=sport,
                )
            except Exception:                                              # noqa: BLE001
                started = False
            if started:
                continue
            try:
                tracked = _tb.tracked_bet_types(_backend, gid, sport)
            except Exception:                                              # noqa: BLE001
                tracked = set()
            matchup = (f"{g.get('away_team', '')} @ "
                       f"{g.get('home_team', '')}").strip(" @")

            # Moneyline (both sports).
            if g.get("pick_team") and "single" not in tracked:
                game_recs.append(_mb_shape_game_rec(
                    sport=sport, gid=gid, bet_type="ml",
                    team=g.get("pick_team"), line="",
                    odds=g.get("pick_odds"), conf=g.get("pick_prob"),
                    matchup=matchup, type_label="Moneyline",
                ))
            if sport != "mlb":
                continue   # RL / totals tracking is MLB-only
            rl = g.get("run_line") or {}
            if rl.get("pick_team") and "run_line" not in tracked:
                pt = rl.get("run_line_point")
                line_s = f"{float(pt):+g}" if isinstance(pt, (int, float)) else ""
                game_recs.append(_mb_shape_game_rec(
                    sport=sport, gid=gid, bet_type="rl",
                    team=rl.get("pick_team"), line=line_s,
                    odds=rl.get("pick_odds"), conf=rl.get("pick_prob"),
                    matchup=matchup, type_label="Run Line",
                ))
            tot = g.get("totals") or {}
            if tot.get("total_line") and "totals" not in tracked:
                direction = (tot.get("direction") or "over").title()
                game_recs.append(_mb_shape_game_rec(
                    sport=sport, gid=gid, bet_type="total",
                    team=f"{direction} {tot.get('total_line')}", line="",
                    odds=tot.get("pick_odds"), conf=tot.get("pick_prob"),
                    matchup=matchup, type_label="Total",
                ))
    game_recs.sort(key=lambda p: -float(p.get("conf_raw") or 0.0))

    prop_recs: list[dict] = []
    try:
        from src.props_scored_cache import load_scored_props
        from src import props_picks_tracker as _ppt
        picks = (load_scored_props() or {}).get("picks") or []

        def _key(d):
            return (
                d.get("player"),
                d.get("market"),
                round(float(d.get("line") or 0), 2),
                (d.get("side") or "").strip().title(),
            )
        try:
            open_keys = {_key(p) for p in _ppt.get_open()}
        except Exception:                                                  # noqa: BLE001
            open_keys = set()

        for r in picks:
            if _key(r) in open_keys:
                continue
            try:
                started = _ls.game_has_started(
                    _backend,
                    commence_time=r.get("commence_time"),
                    home_team=r.get("home_team"),
                    away_team=r.get("away_team"),
                    sport="mlb",
                )
            except Exception:                                              # noqa: BLE001
                started = False
            if started:
                continue
            prop_recs.append(_mb_shape_prop_rec(r))
    except Exception:                                                      # noqa: BLE001
        pass
    prop_recs.sort(key=lambda p: -float(p.get("conf_raw") or 0.0))
    return game_recs, prop_recs


def _mb_shape_game_rec(*, sport, gid, bet_type, team, line, odds, conf,
                       matchup, type_label) -> dict:
    """One game-pick recommendation row.  Pre-builds the track URL + body so
    mybets.js doesn't need per-bet-type branching at click time (matches the
    /props page's pattern of server-side payload shaping)."""
    conf_s = (f"{int(round(float(conf) * 100))}%"
              if isinstance(conf, (int, float)) else "—")
    odds_s = _mb_odds_str(odds)
    detail = type_label + (f" {line}" if line else "")
    if odds_s != "—":
        detail += f" ({odds_s})"
    if bet_type == "ml":
        path = (f"/api/ledger/confirm/{gid}" if sport == "mlb"
                else f"/api/wnba/ledger/confirm/{gid}")
        body = {}                       # bankroll added client-side
    else:
        path = "/api/ledger/track_prop"
        body = {
            "game_id":  gid,
            "bet_type": "run_line" if bet_type == "rl" else "totals",
        }
    return {
        "kind":       "game",
        "sport":      sport,
        "game_id":    gid,
        "bet_type":   bet_type,
        "team":       team or "—",
        "type_label": type_label,
        "detail":     detail,
        "conf_str":   conf_s,
        "conf_raw":   conf,
        "matchup":    matchup,
        "track_url":  path,
        "track_body": body,
    }


def _mb_shape_prop_rec(r: dict) -> dict:
    """One prop-pick recommendation row.  Pre-built /api/props/track payload
    so the client just forwards it via SBT.apiPost."""
    conf = r.get("confidence")
    conf_s = (f"{int(round(float(conf) * 100))}%"
              if isinstance(conf, (int, float)) else "—")
    odds_s = _mb_odds_str(r.get("best_odds"))
    market = (r.get("market") or "").replace("_", " ").title()
    side   = (r.get("side") or "").title()
    line   = r.get("line")
    detail = f"{side} {line} {market}".strip()
    if odds_s != "—":
        detail += f" ({odds_s})"
    matchup = (f"{r.get('away_team', '')} @ "
               f"{r.get('home_team', '')}").strip(" @")
    return {
        "kind":       "prop",
        "player":     r.get("player") or "—",
        "detail":     detail,
        "conf_str":   conf_s,
        "conf_raw":   conf,
        "matchup":    matchup,
        "track_url":  "/api/props/track",
        "track_body": {
            "player":          r.get("player", ""),
            "market":          r.get("market", ""),
            "line":            r.get("line"),
            "side":            r.get("side", "Over"),
            "odds":            r.get("best_odds"),
            "confidence":      r.get("confidence"),
            "predicted_value": r.get("predicted_value"),
            "team":            r.get("team", ""),
            "event_id":        r.get("event_id"),
            "commence_time":   r.get("commence_time"),
        },
    }


def _mybets_view_model() -> dict:
    """Single dict consumed by both the initial /mybets render and the 60 s
    snapshot poll.  Every shape decision happens here so mybets.html stays
    logic-free and mybets.js just renders strings/numbers it receives."""
    bankroll = _mb_bankroll_block()
    open_bets, settled_bets = _mb_unified_bets()
    rec_games, rec_props = _mb_recommendations()
    return {
        "bankroll":            bankroll,
        "open_bets":           open_bets,
        "settled_bets":        settled_bets,
        "open_count":          len(open_bets),
        "settled_count":       len(settled_bets),
        "rec_games":           rec_games,
        "rec_props":           rec_props,
        "rec_total":           len(rec_games) + len(rec_props),
    }


@app.route("/mybets")
def mybets_page():
    """Tailwind My Bets page.  Hydrates from a JSON island; 60 s
    SBT.apiPost('/api/mybets/snapshot') keeps the bankroll + lists fresh
    as the background settle job lands results."""
    import traceback as _tb
    print("[MYBETS] route hit", flush=True, file=sys.stderr)
    try:
        vm = _mybets_view_model()
        print(
            f"[MYBETS] vm ok -- open={vm.get('open_count')} "
            f"settled={vm.get('settled_count')} recs={vm.get('rec_total')}",
            flush=True, file=sys.stderr,
        )
    except Exception:                                                      # noqa: BLE001
        _tb.print_exc(file=sys.stderr)
        vm = {
            "bankroll": {"start": 0, "current": 0, "pnl": 0, "pnl_sign": "+",
                         "pnl_abs": 0, "pnl_color": "dim", "at_risk": 0,
                         "budget_total": 0, "budget_max": 0,
                         "budget_remaining": 0, "remaining_color": "dim"},
            "open_bets": [], "settled_bets": [],
            "open_count": 0, "settled_count": 0,
            "rec_games": [], "rec_props": [], "rec_total": 0,
        }
    return render_template("mybets.html", **vm)


@app.route("/api/mybets/snapshot", methods=["GET"])
def mybets_snapshot():
    """Same view-model dict the /mybets route renders, returned as JSON for
    the page's 60 s poll.  Smaller wire payload than re-rendering HTML and
    avoids template diffing on the client."""
    try:
        return jsonify(_py(_mybets_view_model()))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MYBETS-SNAPSHOT: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


# ── /modelbets Flask port (read-only model dashboard) ───────────────────────
# Mirrors pages/model.py: model bankroll hero, record-by-bet-type, today's
# model picks (game + prop), per-classifier accuracy.  Read-only -- no
# mutations, so no SBT.apiPost; the page just renders the view model + polls
# /api/modelbets/snapshot every 60 s (same cadence as NiceGUI's ui.timer).

_MB_CATS = (
    ("moneyline",       "Moneyline"),
    ("run_line_spread", "Run Line / Spread"),
    ("totals",          "Totals"),
)


def _modelbets_bankroll() -> dict:
    """Model bankroll hero: start / current / P&L + record + at-risk.
    Source-of-truth ladder matches pages/model.py _bankroll_card."""
    start = current = at_risk = 0.0
    supa = None
    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            supa = _sl.model()
    except Exception:                                                      # noqa: BLE001
        supa = None

    if supa is not None:
        try:
            start   = float(supa.starting())
            current = float(supa.bankroll())
            at_risk = float(sum(float(b.get("stake") or 0) for b in supa.active_bets()))
        except Exception:                                                  # noqa: BLE001
            supa = None
    if supa is None:
        try:
            mlb  = Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
            wnba = Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
            start   = float(mlb.data.get("model_starting_bankroll", 1000.0))
            current = float(mlb.data.get("model_bankroll", start))
            at_risk = sum(
                float(b.get("model_amount") or 0)
                for ld in (mlb, wnba)
                for b in (ld.data.get("open_bets") or [])
                if not b.get("confirmed") and not b.get("limit_reached")
            )
        except Exception:                                                  # noqa: BLE001
            start = current = 1000.0
            at_risk = 0.0

    pnl = current - start
    # W/L from the model_picks combined store (same source as the home GAME
    # MODELS chip), not the ledger -- keeps the numbers agreeing.
    w = l = 0
    try:
        from pages import home_stats as hs
        ov = hs.tracker_records()["overall"]
        w = int(ov.get("wins") or 0)
        l = int(ov.get("losses") or 0)
    except Exception:                                                      # noqa: BLE001
        pass
    total = w + l
    record_pct = f"{(w / total * 100):.1f}%" if total else "—"

    return {
        "start":      start,
        "current":    current,
        "pnl":        pnl,
        "pnl_sign":   "+" if pnl >= 0 else "−",
        "pnl_abs":    abs(pnl),
        "pnl_color":  "pos" if pnl >= 0 else "neg",
        "at_risk":    at_risk,
        "record_w":   w,
        "record_l":   l,
        "record_pct": record_pct,
    }


def _modelbets_type_records() -> list[dict]:
    """Per-bet-type W/L from home_stats.tracker_records()['by_bet_type']."""
    rows: list[dict] = []
    try:
        from pages import home_stats as hs
        by_cat = hs.tracker_records()["by_bet_type"]
    except Exception:                                                      # noqa: BLE001
        by_cat = {}
    for key, label in _MB_CATS:
        c = by_cat.get(key) or {"wins": 0, "losses": 0}
        w = int(c.get("wins") or 0)
        l = int(c.get("losses") or 0)
        total = w + l
        rows.append({
            "label": label, "wins": w, "losses": l,
            "pct": f"{(w / total * 100):.1f}%" if total else "—",
        })
    return rows


def _modelbets_result_index() -> dict:
    """{(game_id, bet_type): history_row} across both ledgers, so each daily
    pick can show its settled result + P&L.  Mirrors model.py
    _build_result_index."""
    out: dict = {}
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = Ledger(path=path, starting_bankroll=1000.0)
        except Exception:                                                  # noqa: BLE001
            continue
        for h in (led.data.get("history") or []):
            gid = h.get("game_id")
            bt  = h.get("bet_type") or "single"
            if gid:
                out[(str(gid), str(bt))] = h
    return out


def _modelbets_shape_game_pick(p: dict, result_index: dict) -> dict:
    """Shape one daily game pick into a ready-to-render row, coloured by its
    settled result.  Mirrors pages/model.py _pick_row."""
    bt = (p.get("bet_type") or "single").lower()
    if bt in ("run_line", "spread"):
        cat_key, aliases = "run_line_spread", ("run_line", "spread")
    elif bt == "totals":
        cat_key, aliases = "totals", ("totals",)
    else:
        cat_key, aliases = "moneyline", ("single",)

    line = p.get("prop_line")
    line_s = ""
    if cat_key == "run_line_spread" and line is not None:
        try:
            line_s = f" {float(line):+g}"
        except Exception:                                                  # noqa: BLE001
            line_s = ""

    gid  = str(p.get("game_id") or p.get("id") or "")
    hist = None
    if gid and result_index:
        for a in aliases:
            hist = result_index.get((gid, a))
            if hist is not None:
                break
    result = ((hist or {}).get("result") or "").lower() if hist else ""

    amt   = p.get("model_amount")
    stake = float((hist.get("model_amount") if hist else (amt or 0)) or 0.0)
    pnl   = float((hist or {}).get("model_pnl") or 0)
    if result == "win":
        team_color, amount_color, amount_text = "pos", "pos", f"+${pnl:.2f}"
    elif result == "loss":
        team_color, amount_color, amount_text = "neg", "neg", f"-${stake:.2f}"
    elif result == "push":
        team_color, amount_color, amount_text = "text", "dim", "$0.00"
    else:
        team_color, amount_color = "text", "text"
        amount_text = f"${float(amt):.0f}" if amt is not None else "—"

    odds = p.get("odds")
    odds_s = (f"+{int(odds)}" if isinstance(odds, (int, float)) and odds > 0
              else (f"{int(odds)}" if isinstance(odds, (int, float)) else "—"))
    return {
        "rank":         p.get("rank", "·"),
        "team":         (p.get("team") or "—") + line_s,
        "sport":        (p.get("sport_label") or p.get("sport") or "").upper(),
        "prob":         round(float(p.get("pick_prob") or 0) * 100),
        "odds_s":       odds_s,
        "amount_text":  amount_text,
        "team_color":   team_color,
        "amount_color": amount_color,
        "below_threshold": bool(p.get("below_threshold")),
    }


def _modelbets_shape_prop_pick(p: dict) -> dict:
    """Shape one daily prop pick.  Mirrors pages/model.py _prop_pick_row."""
    line = p.get("line")
    try:
        line_s = f"{float(line):g}"
    except (TypeError, ValueError):
        line_s = "—"
    pv = p.get("predicted_value")
    try:
        pv_s = f"{float(pv):.2f}"
    except (TypeError, ValueError):
        pv_s = "—"
    odds = p.get("best_odds")
    odds_s = (f"+{int(odds)}" if isinstance(odds, (int, float)) and odds > 0
              else (f"{int(odds)}" if isinstance(odds, (int, float)) else ""))
    side = (p.get("side") or "Over").strip().title()
    return {
        "rank":       p.get("rank", "·"),
        "player":     p.get("player") or "—",
        "market":     (p.get("market") or "").replace("_", " ").title(),
        "side":       side,
        "side_color": "primary" if side == "Over" else "dim",
        "line_s":     line_s,
        "pv_s":       pv_s,
        "conf":       round(float(p.get("confidence") or 0) * 100),
        "odds_s":     odds_s,
    }


def _modelbets_picks() -> dict:
    """Today's model picks: game + prop, shaped + result-coloured."""
    try:
        daily = load_daily_picks() or {}
        picks = daily.get("picks") or {}
    except Exception:                                                      # noqa: BLE001
        picks = {}
    ridx = _modelbets_result_index()
    game = [_modelbets_shape_game_pick(p, ridx) for p in (picks.get("game_picks") or [])]
    prop = [_modelbets_shape_prop_pick(p) for p in (picks.get("prop_picks") or [])]
    return {"game_picks": game, "prop_picks": prop,
            "game_count": len(game), "prop_count": len(prop)}


def _modelbets_classifiers() -> list[dict]:
    """Per-classifier (xgb/lr/nn) accuracy with best/worst tinting.
    Mirrors pages/model.py _classifier_card."""
    labels = {"xgb": "XGBoost", "lr": "Logistic Regression", "nn": "Neural Net"}
    models = ("xgb", "lr", "nn")
    try:
        from pages import home_stats as hs
        tallies = hs.classifier_accuracy_from_trackers()
    except Exception:                                                      # noqa: BLE001
        tallies = {m: {"overall": [0, 0]} for m in models}

    overall = {m: (tallies.get(m, {}).get("overall") or [0, 0]) for m in models}
    qualified = [(m, overall[m][0] / overall[m][1]) for m in models
                 if overall[m][1] >= 10]
    best  = max(qualified, key=lambda r: r[1])[0] if qualified else None
    worst = (min(qualified, key=lambda r: r[1])[0]
             if len(qualified) >= 2 else None)

    out: list[dict] = []
    for m in models:
        correct, total = overall[m][0], overall[m][1]
        pct = (correct / total * 100) if total else None
        by_cat = []
        for key, label in _MB_CATS:
            cc = (tallies.get(m, {}).get(key) or [0, 0])
            ct, tt = cc[0], cc[1]
            by_cat.append({
                "label": label, "correct": ct, "total": tt,
                "pct": f"{(ct / tt * 100):.0f}%" if tt else "—",
            })
        out.append({
            "model":    m,
            "label":    labels[m],
            "correct":  correct,
            "total":    total,
            "pct":      "—" if pct is None else f"{pct:.1f}%",
            "is_best":  m == best,
            "is_worst": m == worst,
            "by_cat":   by_cat,
        })
    return out


def _modelbets_view_model() -> dict:
    """Single dict for both the /modelbets render and the 60 s snapshot poll.
    Read-only -- every card is computed server-side from the model ledger +
    model_picks trackers + daily picks file."""
    return {
        "bankroll":     _modelbets_bankroll(),
        "type_records": _modelbets_type_records(),
        "picks":        _modelbets_picks(),
        "classifiers":  _modelbets_classifiers(),
    }


def _modelbets_fallback_vm() -> dict:
    return {
        "bankroll": {"start": 0, "current": 0, "pnl": 0, "pnl_sign": "+",
                     "pnl_abs": 0, "pnl_color": "dim", "at_risk": 0,
                     "record_w": 0, "record_l": 0, "record_pct": "—"},
        "type_records": [], "picks": {"game_picks": [], "prop_picks": [],
                                      "game_count": 0, "prop_count": 0},
        "classifiers": [],
    }


@app.route("/modelbets")
def modelbets_page():
    """Tailwind Model Bets page -- read-only dashboard mirroring pages/model.py.
    Hydrates from a JSON island; 60 s /api/modelbets/snapshot poll keeps the
    bankroll + records fresh as the settle job lands results."""
    import traceback as _tb
    print("[MODELBETS] route hit", flush=True, file=sys.stderr)
    try:
        vm = _modelbets_view_model()
    except Exception:                                                      # noqa: BLE001
        _tb.print_exc(file=sys.stderr)
        vm = _modelbets_fallback_vm()
    return render_template("modelbets.html", init_data=vm)


@app.route("/api/modelbets/snapshot", methods=["GET"])
def modelbets_snapshot():
    """Same view-model dict the /modelbets route renders, as JSON for the
    60 s poll."""
    try:
        return jsonify(_py(_modelbets_view_model()))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MODELBETS-SNAPSHOT: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify(_py(_modelbets_fallback_vm())), 500


# ── /model-history Flask port (single-model pick history, read-only) ────────
# Mirrors pages/model_history.py: date-browsable pick list + W-L-V record for
# one (sport, model) store from the Supabase model_picks table.  Read-only --
# no mutations, no auto-poll (user-driven via preset pills + date input).
# Closes the inbound links from the /modelbets (#341) and /admin (#340)
# Model Performance tables, which both point rows at /model-history/{sport}/{model}.

_MH_PRESETS = (("today", "Today"), ("yesterday", "Yesterday"),
               ("7d", "Last 7 Days"), ("30d", "Last 30 Days"))


def _mh_fmt_dt(iso) -> str:
    """ISO UTC -> 'MM-DD HH:MM' ET.  Mirrors model_history._fmt_dt."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso)[:16]


def _mh_shape_pick(p: dict) -> dict:
    """Shape one pick row.  Pending picks marked Pending (dim); finished rows
    coloured win=pos / loss=neg / void=warn / else dim -- carries the NiceGUI
    coloring exactly, including voids living in the picks list as finished
    rows with result='void'."""
    status = (p.get("status") or "pending").lower()
    result = (p.get("result") or "").lower()
    if status != "finished":
        result_text, result_color = "Pending", "dim"
    else:
        result_color = {"win": "pos", "loss": "neg", "void": "warn"}.get(result, "dim")
        result_text  = result.upper() or "—"
    conf = p.get("confidence")
    conf_s = f"{float(conf) * 100:.0f}%" if isinstance(conf, (int, float)) else "—"
    line = p.get("line")
    line_s = f"{float(line):g}" if isinstance(line, (int, float)) else "—"
    return {
        "made":         _mh_fmt_dt(p.get("created_at")),
        "who":          p.get("player_name") or p.get("game_id") or "—",
        "bet":          p.get("bet_type") or "",
        "side":         p.get("pick_side") or "—",
        "line_s":       line_s,
        "conf_s":       conf_s,
        "status":       status,
        "result_text":  result_text,
        "result_color": result_color,
    }


def _model_history_view_model(sport: str, model: str,
                              preset: str | None = None,
                              date: str | None = None) -> dict:
    """Shape one (sport, model) history view for a timeframe.  Either `date`
    (single ET day) or `preset` drives the range; default preset 'today'.
    Read-only -- reads src.model_picks.history."""
    from src import model_picks as mp
    sport = (sport or "mlb").lower()
    model = (model or "combined").lower()

    if date:
        start = end = date
        label = date
        active = {"mode": "date", "date": date, "preset": None}
    else:
        preset = (preset or "today").lower()
        if preset not in dict(_MH_PRESETS):
            preset = "today"
        start, end = mp.date_range(preset)
        label = dict(_MH_PRESETS)[preset]
        active = {"mode": "preset", "preset": preset, "date": None}

    try:
        data = mp.history(sport, model, start, end)
    except Exception as exc:                                               # noqa: BLE001
        _eprint(f"MODEL-HISTORY history() failed: {type(exc).__name__}: {exc}")
        data = {"record": {}, "picks": []}

    rec   = data.get("record") or {}
    picks = data.get("picks") or []
    w = int(rec.get("wins") or 0)
    l = int(rec.get("losses") or 0)
    v = int(rec.get("voids") or 0)
    pct = rec.get("pct")
    pct_s = f"{pct * 100:.1f}%" if isinstance(pct, (int, float)) else "—"
    rec_color = ("pos" if (pct or 0) >= 0.55 else
                 "neg" if (isinstance(pct, (int, float)) and pct < 0.50) else "dim")
    total = len(picks)
    # NiceGUI count-line semantics carried over verbatim: "finished" = W/L
    # only (voids excluded), pending = total - (w + l + v).
    finished_wl = w + l
    pending = total - (w + l + v)

    return {
        "sport":       sport,
        "model":       model,
        "label":       label,
        "active":      active,
        "presets":     [{"key": k, "label": lbl} for k, lbl in _MH_PRESETS],
        "record": {
            "wins": w, "losses": l, "voids": v,
            "pct_s": pct_s, "rec_color": rec_color,
            "void_s": f" · {v}V" if v else "",
        },
        "counts": {"total": total, "finished": finished_wl, "pending": pending},
        "picks":  [_mh_shape_pick(p) for p in picks],
    }


@app.route("/model-history/<sport>/<model>")
def model_history_page(sport, model):
    """Tailwind single-model pick-history page -- read-only, date-browsable.
    Initial render defaults to Today; preset/date changes re-fetch
    /api/model-history/<sport>/<model> client-side."""
    import traceback as _tb
    print(f"[MODEL-HISTORY] route hit sport={sport} model={model}",
          flush=True, file=sys.stderr)
    try:
        vm = _model_history_view_model(sport, model, preset="today")
    except Exception:                                                      # noqa: BLE001
        _tb.print_exc(file=sys.stderr)
        vm = {"sport": (sport or "mlb").lower(), "model": (model or "combined").lower(),
              "label": "Today", "active": {"mode": "preset", "preset": "today", "date": None},
              "presets": [{"key": k, "label": l} for k, l in _MH_PRESETS],
              "record": {"wins": 0, "losses": 0, "voids": 0, "pct_s": "—",
                         "rec_color": "dim", "void_s": ""},
              "counts": {"total": 0, "finished": 0, "pending": 0}, "picks": []}
    return render_template("model_history.html", init_data=vm)


@app.route("/api/model-history/<sport>/<model>", methods=["GET"])
def model_history_data(sport, model):
    """JSON view model for a timeframe change.  Query: ?preset=<key> OR
    ?date=YYYY-MM-DD.  GET because it's a pure read keyed by path + query
    (bookmarkable: a specific model + day is a meaningful URL)."""
    try:
        preset = request.args.get("preset")
        date   = request.args.get("date")
        return jsonify(_py(_model_history_view_model(sport, model,
                                                     preset=preset, date=date)))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MODEL-HISTORY-DATA: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc), "picks": [],
                        "record": {}, "counts": {}}), 500


# ── /matchup (game_detail) Flask port ───────────────────────────────────────
# Ports pages/game_detail.py (1,555 lines NiceGUI) to Flask + Tailwind.  The
# 3-strategy game resolver lives in src/game_detail_data.py (extracted so the
# Flask port + the legacy NiceGUI page can share one implementation).  Two
# route aliases serve the same handler -- the original NiceGUI page declared
# both, and every inbound link uses /matchup/...

from src import game_detail_data                                          # noqa: E402


def _gd_pick_blocks(raw: dict, ser: dict, sport: str) -> list[dict]:
    """Shape the model-picks rows: ML always; RL/Spread when present;
    Totals when total_line is set.  Includes the top-3 SHAP factors so the
    template renders the 'TOP FACTORS' list without any client logic.
    Mirrors pages/game_detail.py:_section_model_picks lines 551-607."""
    is_mlb = sport == "mlb"
    out: list[dict] = []

    def _edge_color(edge_pct: float) -> str:
        return "pos" if edge_pct >= 0 else "neg"

    def _shape_shap(shap: list) -> list[dict]:
        rows = []
        for s in (shap or [])[:3]:
            label = s.get("label") or s.get("feature", "factor")
            try:
                val = float(s.get("shap_value") or 0)
            except (TypeError, ValueError):
                val = 0.0
            rows.append({
                "label": label,
                "arrow": "↑" if val > 0 else ("↓" if val < 0 else "·"),
                "color": "pos" if val > 0 else ("neg" if val < 0 else "dim"),
            })
        return rows

    def _row(bet_type, label, pick, prob, edge, odds, kelly, agree, shap_src,
             track_url, track_body):
        prob_pct = float(prob or 0) * 100
        edge_pct = float(edge or 0) * 100
        return {
            "bet_type":   bet_type,
            "label":      label,
            "pick":       pick,
            "prob_pct":   round(prob_pct),
            "edge_pct":   round(edge_pct, 1),
            "edge_sign":  "+" if edge_pct >= 0 else "",
            "edge_color": _edge_color(edge_pct),
            "odds_s":     game_detail_data.odds_str(odds),
            "kelly_s":    (f"½K  ${float(kelly):.0f}"
                           if isinstance(kelly, (int, float)) and kelly > 0
                           else "½K  —"),
            "agree":      bool(agree),
            "shap":       _shape_shap(shap_src),
            "track_url":  track_url,
            "track_body": track_body,
        }

    # Moneyline -- always present in serialized output.
    if ser.get("pick_team"):
        track_url = (f"/api/ledger/confirm/{ser.get('game_id') or ''}" if is_mlb
                     else f"/api/wnba/ledger/confirm/{ser.get('game_id') or ''}")
        out.append(_row(
            "moneyline", "Moneyline", ser.get("pick_team", "—"),
            ser.get("pick_prob"), ser.get("pick_edge"), ser.get("pick_odds"),
            ser.get("bet_dollars"), ser.get("models_agree", True),
            (raw.get("prediction") or {}).get("shap") or [],
            track_url, {},   # bankroll added client-side
        ))

    # Run Line (MLB) / Spread (WNBA)
    rl = ser.get("run_line") or ser.get("spread_pick")
    if rl and rl.get("pick_team"):
        bt = "run_line" if is_mlb else "spread"
        line = rl.get("run_line_point") if is_mlb else rl.get("spread_line")
        line_str = f" {float(line):+g}" if isinstance(line, (int, float)) else ""
        out.append(_row(
            bt, "Run Line" if is_mlb else "Spread",
            f"{rl.get('pick_team', '')}{line_str}".strip(),
            rl.get("pick_prob"), rl.get("edge"), rl.get("pick_odds"),
            rl.get("bet_dollars"), rl.get("models_agree", True),
            (raw.get("rl_pred") or raw.get("spread_pred") or {}).get("shap") or [],
            "/api/ledger/track_prop",
            {"game_id": ser.get("game_id"), "bet_type": "run_line"},
        ))

    # Totals
    tot = ser.get("totals") or {}
    if tot and tot.get("total_line") is not None:
        direction = (tot.get("direction") or "over").title()
        out.append(_row(
            "totals", "Totals", f"{direction} {tot.get('total_line')}",
            tot.get("pick_prob"), tot.get("edge"),
            tot.get("over_odds") if direction == "Over" else tot.get("under_odds"),
            tot.get("bet_dollars"), tot.get("models_agree", True),
            (raw.get("totals_pred") or {}).get("shap") or [],
            "/api/ledger/track_prop",
            {"game_id": ser.get("game_id"), "bet_type": "totals"},
        ))
    return out


def _gd_header_block(raw: dict, ser: dict, sport: str) -> dict:
    """Shape the header card: meta row + odds folded into the matchup box.
    Mirrors _section_header lines 358-473.  Live-score state is computed
    here but not the live-score block itself (that's read off the same
    live_score helper, gracefully degraded when missing)."""
    game = raw.get("game") or {}
    away_full = game.get("away_team") or ser.get("away_team") or "—"
    home_full = game.get("home_team") or ser.get("home_team") or "—"
    when = game_detail_data.fmt_when(
        game.get("commence_time") or ser.get("commence_time", ""))
    if isinstance(game.get("venue"), dict):
        venue = (game.get("venue") or {}).get("name") or "—"
    else:
        venue = (game.get("venue_name") or ser.get("venue_name")
                 or ser.get("venue") or "—")
    upset = raw.get("upset") or {}
    sgn = upset.get("series_game_number")
    series_ctx = f"Game {sgn} of series" if sgn else None

    # Live score lookup -- gracefully no-op when offline / off-hours.
    live_state = "scheduled"
    live = None
    try:
        from components import live_score as _ls
        live = _ls.lookup(sport, game_id=(game.get("id") or ""),
                          away_team=away_full, home_team=home_full)
        live_state = _ls.state_of(live)
    except Exception:                                                      # noqa: BLE001
        live = None

    away_ml = game_detail_data.odds_str(ser.get("away_odds"))
    home_ml = game_detail_data.odds_str(ser.get("home_odds"))
    rl_blob = ser.get("run_line") or ser.get("spread_pick") or {}
    _pt = (rl_blob.get("run_line_point") if sport == "mlb"
           else rl_blob.get("spread_line"))
    away_rl = home_rl = None
    if isinstance(_pt, (int, float)):
        _h = game_detail_data.odds_str(
            rl_blob.get("run_line_home_odds") or rl_blob.get("pick_odds"))
        _a = game_detail_data.odds_str(
            rl_blob.get("run_line_away_odds") or rl_blob.get("pick_odds"))
        home_rl = f"{float(_pt):+g} {_h}".strip()
        away_rl = f"{(-float(_pt)):+g} {_a}".strip()
    tot = ser.get("totals") or {}
    total_line = tot.get("total_line")
    proj_total = tot.get("predicted_total")

    return {
        "sport":       sport,
        "away_team":   away_full,
        "home_team":   home_full,
        "when":        when,
        "venue":       venue if venue and venue != "—" else None,
        "series_ctx":  series_ctx,
        "live_state":  live_state,                # scheduled | live | final
        "away_ml":     away_ml,
        "home_ml":     home_ml,
        "away_rl":     away_rl,
        "home_rl":     home_rl,
        "total_line":  total_line,
        "proj_total":  (float(proj_total) if isinstance(proj_total, (int, float))
                        else None),
    }


def _gd_venue_block(ser: dict, sport: str) -> dict | None:
    """MLB-only venue card: ballpark + run factor + Hitter/Pitcher Friendly
    tag.  Mirrors _section_venue."""
    if sport != "mlb":
        return None
    park = ser.get("park_run_factor")
    venue = ser.get("venue_name") or "—"
    try:
        pv = int(round(float(park))) if park is not None else None
    except (TypeError, ValueError):
        pv = None
    if pv is None:
        tag, tag_color = "—", "dim"
    elif pv > 105:
        tag, tag_color = "Hitter Friendly", "pos"
    elif pv < 95:
        tag, tag_color = "Pitcher Friendly", "neg"
    else:
        tag, tag_color = "Neutral", "dim"
    return {"venue": venue, "run_factor": pv, "tag": tag, "tag_color": tag_color}


def _gd_game_context_block(ser: dict) -> list[dict]:
    """Weather + line movement rows.  Mirrors _section_game_context.
    Umpire data line preserved verbatim ('Coming soon')."""
    wx = ser.get("weather") or {}
    line_move = (ser.get("meta") or {}).get("line_movement")
    rows: list[dict] = []
    if wx:
        temp = wx.get("temperature")
        wind = wx.get("wind_speed")
        wdir = wx.get("wind_direction")
        bits = []
        if isinstance(temp, (int, float)): bits.append(f"{temp:.0f}°F")
        if isinstance(wind, (int, float)): bits.append(f"wind {wind:.0f} mph")
        if wdir: bits.append(f"({wdir})")
        rows.append({"label": "Weather",
                     "value": " ".join(bits) if bits else "—"})
    else:
        rows.append({"label": "Weather", "value": "—"})
    if isinstance(line_move, (int, float)) and line_move:
        sign = "+" if line_move > 0 else ""
        rows.append({"label": "Line movement (vs opening)",
                     "value": f"{sign}{line_move:.2f}"})
    else:
        rows.append({"label": "Line movement (vs opening)", "value": "—"})
    rows.append({"label": "Umpire data", "value": "Coming soon"})
    return rows


def _gd_upset_block(ser: dict) -> dict:
    """Chaos score + components (each 0..100% with a coloured bar).
    Mirrors _section_upset_factor.  Returns {available, ...} so the
    template can show the 'No upset-factor data' empty state."""
    upset = ser.get("upset_factor") or {}
    score = upset.get("score")
    if score is None:
        return {"available": False}
    pretty = {
        "run_scoring_var":      "Run scoring volatility",
        "pitching_var":         "Pitching volatility",
        "streak":               "Recent streak swing",
        "underdog_win_rate":    "Underdog upset rate this season",
        "blown_lead_rate":      "Blown leads recently",
        "h2h_divergence":       "Head-to-head divergence",
        "bullpen_volatility":   "Bullpen volatility",
        "pitcher_consistency":  "Starting pitcher inconsistency",
        "series_game":          "Series-game effect",
    }
    components = upset.get("components") or {}
    rows: list[dict] = []
    for key, label in pretty.items():
        if key not in components:
            continue
        try:
            v = float(components[key])
        except (TypeError, ValueError):
            v = 0.0
        pct = max(0, min(100, int(round(v * 100))))
        col = "neg" if pct > 65 else ("warn" if pct > 35 else "pos")
        rows.append({"label": label, "pct": pct, "color": col})
    return {"available": True, "score": int(score), "components": rows}


def _game_detail_view_model(sport: str, game_id: str) -> dict:
    """Core sections rendered immediately (header / picks / venue / game
    context / upset).  Pitching, lineups, team context, and AI analysis
    are lazy-loaded client-side via separate GET endpoints.  Returns
    {found: false} for unknown game ids."""
    sport = (sport or "mlb").lower()
    _backend = sys.modules[__name__]
    raw, ser = game_detail_data.resolve_game(_backend, sport, game_id)
    if not ser:
        return {"found": False, "sport": sport, "game_id": game_id}

    raw_safe = raw or ser   # schedule stubs come back as raw=None
    picks = _gd_pick_blocks(raw_safe, ser, sport) if raw else []

    return {
        "found":         True,
        "sport":         sport,
        "game_id":       game_id,
        "has_raw":       bool(raw),
        "header":        _gd_header_block(raw_safe, ser, sport),
        "picks":         picks,
        "picks_empty":   not bool(raw),
        "venue":         _gd_venue_block(ser, sport),
        "game_context":  _gd_game_context_block(ser),
        "upset":         _gd_upset_block(ser),
        "home_team":     ser.get("home_team") or "",
        "away_team":     ser.get("away_team") or "",
        "commence_time": ser.get("commence_time") or "",
    }


def _matchup_render(sport: str, game_id: str):
    """Shared handler for both route aliases (/matchup + /game)."""
    import traceback as _tb
    print(f"[MATCHUP] route hit sport={sport} game_id={game_id}",
          flush=True, file=sys.stderr)
    try:
        try:
            hydrate_state()
        except Exception:                                                  # noqa: BLE001
            pass
        vm = _game_detail_view_model(sport, game_id)
    except Exception:                                                      # noqa: BLE001
        _tb.print_exc(file=sys.stderr)
        vm = {"found": False, "sport": sport, "game_id": game_id}
    return render_template("game_detail.html", init_data=vm)


@app.route("/matchup/<sport>/<game_id>")
def matchup_page(sport, game_id):
    """Tailwind game-detail page.  Alias of /game/<sport>/<game_id>."""
    return _matchup_render(sport, game_id)


@app.route("/game/<sport>/<game_id>")
def game_detail_page_alias(sport, game_id):
    """Legacy alias preserved from NiceGUI -- pages/game_detail.py declared
    both /matchup and /game pointing at the same handler."""
    return _matchup_render(sport, game_id)


# ── 4 lazy GET endpoints (AI / pitching / lineups / team context) ──────────
# Each mirrors a `ui.timer(0.05, once=True)` lazy load in the NiceGUI
# page, so first paint never blocks on a slow upstream call.

@app.route("/api/matchup/<sport>/<game_id>/ai", methods=["GET"])
def matchup_ai(sport, game_id):
    """AI analysis (3 tabbed takes: moneyline / run_line / run_total).
    Wraps ai_summaries.get_game_bet_analysis -- cached per game per day
    by that helper."""
    try:
        _, ser = game_detail_data.resolve_game(sys.modules[__name__], sport, game_id)
        if not ser:
            return jsonify({"error": "game not found"}), 404
        from src import ai_summaries as _ais
        data = _ais.get_game_bet_analysis(sport, ser) or {}
        return jsonify(_py(data))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MATCHUP-AI: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/matchup/<sport>/<game_id>/pitching", methods=["GET"])
def matchup_pitching(sport, game_id):
    """MLB starting-pitcher cards.  WNBA returns empty so the client
    renders the 'Starting five coming soon' placeholder."""
    try:
        if sport.lower() != "mlb":
            return jsonify({"away": {}, "home": {}, "sport": "wnba"})
        _, ser = game_detail_data.resolve_game(sys.modules[__name__], sport, game_id)
        if not ser:
            return jsonify({"error": "game not found"}), 404
        home_team = (ser.get("home_team") or "").strip()
        away_team = (ser.get("away_team") or "").strip()
        commence  = ser.get("commence_time") or ""
        game_date = ""
        if commence:
            try:
                dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
                game_date = dt.astimezone(_ET).date().isoformat()
            except Exception:                                              # noqa: BLE001
                pass
        if not game_date:
            game_date = datetime.now(_ET).date().isoformat()
        if not (home_team and away_team):
            return jsonify({"away": {}, "home": {}})
        from src.pitcher_client import get_pitcher_client
        data = get_pitcher_client().get_starters_for_game(
            home_team, away_team, game_date, commence_time=commence) or {}
        return jsonify(_py({
            "away":      data.get("away") or {},
            "home":      data.get("home") or {},
            "away_team": away_team,
            "home_team": home_team,
        }))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MATCHUP-PITCHING: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/matchup/<sport>/<game_id>/lineups", methods=["GET"])
def matchup_lineups(sport, game_id):
    """Confirmed batting lineups via matchup_context.get_matchup_lineups
    (MLB only; WNBA returns empty)."""
    try:
        if sport.lower() != "mlb":
            return jsonify({})
        _, ser = game_detail_data.resolve_game(sys.modules[__name__], sport, game_id)
        if not ser:
            return jsonify({"error": "game not found"}), 404
        home_team = (ser.get("home_team") or "").strip()
        away_team = (ser.get("away_team") or "").strip()
        commence  = ser.get("commence_time") or ""
        try:
            game_date = (datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
                         .astimezone(_ET).date().isoformat())
        except Exception:                                                  # noqa: BLE001
            game_date = datetime.now(_ET).date().isoformat()
        # Pitcher handedness comes from the same pitcher_client call used by
        # the pitching endpoint -- request both so the lineup split-vs-hand
        # calculation has the right opposing-hand value.
        try:
            from src.pitcher_client import get_pitcher_client
            sp = get_pitcher_client().get_starters_for_game(
                home_team, away_team, game_date, commence_time=commence) or {}
        except Exception:                                                  # noqa: BLE001
            sp = {}
        home_sp_hand = (sp.get("home") or {}).get("hand")
        away_sp_hand = (sp.get("away") or {}).get("hand")
        from src.matchup_context import get_matchup_lineups
        data = get_matchup_lineups(
            home_team, away_team, game_date, home_sp_hand, away_sp_hand) or {}
        return jsonify(_py(data))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MATCHUP-LINEUPS: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


@app.route("/api/matchup/<sport>/<game_id>/team-context", methods=["GET"])
def matchup_team_context(sport, game_id):
    """Team-context card: last-10, streak, home/away splits, head-to-head."""
    try:
        _, ser = game_detail_data.resolve_game(sys.modules[__name__], sport, game_id)
        if not ser:
            return jsonify({"error": "game not found"}), 404
        home_team = (ser.get("home_team") or "").strip()
        away_team = (ser.get("away_team") or "").strip()
        commence  = ser.get("commence_time") or ""
        try:
            game_date = (datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
                         .astimezone(_ET).date().isoformat())
        except Exception:                                                  # noqa: BLE001
            game_date = datetime.now(_ET).date().isoformat()
        from src.matchup_context import get_team_context
        data = get_team_context(home_team, away_team, game_date) or {}
        return jsonify(_py({**data, "home_team": home_team, "away_team": away_team}))
    except Exception as exc:                                               # noqa: BLE001
        import traceback as _tb
        _eprint(f"MATCHUP-TEAM-CTX: {type(exc).__name__}: {exc}\n{_tb.format_exc()}")
        return jsonify({"error": str(exc)}), 500


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
        return jsonify({"error": _redact(str(exc))}), 500

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
        return jsonify({"error": _redact(str(exc))}), 500

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
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    return jsonify(parsed)


@app.route("/api/ai/chat", methods=["POST"])
def ai_chat():
    """Handle a single chat turn with the AI sports analyst (NiceGUI chat).

    Body:
      message:          str                     -- the user's new message
      history:          [{role, content}, ...]  -- prior turns this session
      include_context:  bool (default True)     -- whether to load today's
                                                   game data into the
                                                   system prompt for this
                                                   call.  The UI sends True
                                                   on the first message of
                                                   a session and False on
                                                   every subsequent message
                                                   to minimize token cost.

    Response:
      response:         str                     -- the analyst's reply
      calls_today:      int                     -- count AFTER this call
      daily_limit:      int                     -- configured cap
      limit_reached:    bool                    -- whether the next call
                                                   would be blocked

    Hard daily cap: if calls_today >= daily_limit, returns 429 with
    {error, calls_today, daily_limit, limit_reached: True} and does NOT
    consume an Anthropic call.
    """
    data            = request.get_json() or {}
    message         = (data.get("message") or "").strip()
    history         = data.get("history") or []
    include_context = bool(data.get("include_context", True))

    if not message:
        return jsonify({"error": "No message provided"}), 400

    # Daily limit -- check BEFORE making the upstream call.
    limit  = _ai_daily_limit()
    so_far = _ai_get_daily_count()
    if so_far >= limit:
        return jsonify({
            "error":         "Daily AI limit reached, resets at midnight.",
            "calls_today":   so_far,
            "daily_limit":   limit,
            "limit_reached": True,
        }), 429

    # Build the system prompt.  Mandatory portion is always present; the
    # context block is only appended when include_context=True (first
    # message of the session per spec).
    system = _CHAT_SYSTEM_PROMPT
    if include_context:
        try:
            mlb_results  = _analysis_state.get("results") or []
            mlb_bankroll = float(_analysis_state.get("bankroll") or 250)
            mlb_ctx = _build_chat_context(mlb_results, mlb_bankroll, "mlb")
        except Exception:                                                 # noqa: BLE001
            mlb_ctx = ""
        try:
            wnba_results  = _wnba_analysis_state.get("results") or []
            wnba_bankroll = float(_wnba_analysis_state.get("bankroll") or 1000)
            wnba_ctx = _build_chat_context(wnba_results, wnba_bankroll, "wnba")
        except Exception:                                                 # noqa: BLE001
            wnba_ctx = ""
        ctx_parts = [c for c in (mlb_ctx, wnba_ctx) if c]
        if ctx_parts:
            system = f"{system}\n\n" + "\n\n".join(ctx_parts)

    messages = list(history) + [{"role": "user", "content": message}]

    try:
        # _call_analyst_chat already appends its extra_context to the system
        # prompt -- we've already done that ourselves so pass the system
        # text via the `extra_context` param while we leave the prompt-
        # composition in our hands.  Specifically: replace
        # _ANALYST_SYSTEM_PROMPT here with the chat-specific one.
        import anthropic as _anthropic
        api_key = _ANTHROPIC_API_KEY
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in env / .env")
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=system,
            messages=messages,
        )
        response = msg.content[0].text.strip()
    except Exception as exc:
        # Don't increment the counter on failure -- only successful
        # Anthropic calls consume daily quota.
        return jsonify({"error": _redact(str(exc))}), 500

    new_count = _ai_increment_daily_count()
    return jsonify({
        "response":      response,
        "calls_today":   new_count,
        "daily_limit":   limit,
        "limit_reached": new_count >= limit,
    })


@app.route("/api/ai/usage", methods=["GET"])
def ai_usage():
    """Cheap GET the UI calls on page load to seed the counter chip + Send
    button enabled-state.  Does NOT make any Anthropic call -- safe per
    the spec's 'never make any Anthropic API call automatically on page
    load' constraint."""
    try:
        count = _ai_get_daily_count()
        limit = _ai_daily_limit()
        return jsonify({
            "calls_today":   count,
            "daily_limit":   limit,
            "limit_reached": count >= limit,
        })
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({
            "calls_today":   0,
            "daily_limit":   20,
            "limit_reached": False,
            "error":         _redact(str(exc)),
        }), 200


@app.route("/api/ai/pick_analysis", methods=["POST"])
def ai_pick_analysis():
    """Focused 3-4 sentence analysis of one specific model pick.

    Body:
      game_id:   str  -- the analysis cache key (Odds API id)
      bet_type:  str  -- "moneyline" | "run_line" | "spread" | "totals"
      sport:     str  -- "mlb" | "wnba"

    The handler looks the game up in _analysis_state / _wnba_analysis_state
    and assembles a tight system prompt containing only what's needed to
    reason about THIS pick (matchup, pick team, prob, edge, odds, top
    SHAP factors, model agreement).  Output is exactly the 3-4 sentence
    plain-text analysis -- the chat-style markdown ban applies.

    Counts toward the same daily AI cap as /api/ai/chat.  Returns 429
    when the cap is hit, same payload shape.
    """
    data = request.get_json() or {}
    game_id  = (data.get("game_id") or "").strip()
    bet_type = (data.get("bet_type") or "moneyline").strip().lower()
    sport    = (data.get("sport") or "mlb").strip().lower()
    if not game_id:
        return jsonify({"error": "game_id required"}), 400

    # Daily cap check (same as /api/ai/chat).
    limit  = _ai_daily_limit()
    so_far = _ai_get_daily_count()
    if so_far >= limit:
        return jsonify({
            "error":         "Daily AI limit reached, resets at midnight.",
            "calls_today":   so_far,
            "daily_limit":   limit,
            "limit_reached": True,
        }), 429

    # Locate the raw analysis result (carries prediction + shap + meta).
    # _find_analysis_row tolerates both nested and flat shapes.
    state = _wnba_analysis_state if sport == "wnba" else _analysis_state
    raw = _find_analysis_row(state, game_id)
    if raw is None:
        return jsonify({
            "error": f"Game {game_id!r} not found in {sport.upper()} analysis cache.",
        }), 404

    # Build the focused per-pick context.  Strict ~600 token cap so the
    # call is cheap; the system prompt below limits the response to
    # 3-4 sentences which keeps the OUTPUT cap low too.
    try:
        ctx = _build_pick_analysis_context(raw, bet_type, sport)
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"error": f"context build failed: {_redact(str(exc))}"}), 500

    system = (
        "You are a sharp professional sports analyst with this complete "
        "data card in front of you.  Write 4 to 6 plain-text sentences "
        "(no markdown, no asterisks, no bullet points, no headers) that:\n"
        "  1. Reference both pitchers by name + team abbreviation and "
        "cite at least one specific number from each (ERA, K/9, BB/9, "
        "Home/Away ERA, or Last 3 ERA).\n"
        "  2. State which model -- XGB, LR, or NN -- is most confident "
        "and call out if the three disagree.\n"
        "  3. Explain WHY the model favors one side using the SHAP "
        "factors (cite the actual feature name + direction).\n"
        "  4. Translate the edge over market into plain English (sharp "
        "vs marginal vs trap) and reference the moneyline / run line / "
        "totals lines as appropriate to the bet type asked about.\n"
        "  5. Give a concrete recommendation: bet it at the Kelly size, "
        "fade it, or pass.  No hedging language like 'might' or "
        "'could be' -- be opinionated.\n"
        "Always ground claims in the numbers below.  Never invent stats.\n\n"
        + ctx
    )

    try:
        import anthropic as _anthropic
        api_key = _ANTHROPIC_API_KEY
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in env / .env")
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=system,
            messages=[{
                "role":    "user",
                "content": f"Analyze the {bet_type} pick for this game.",
            }],
        )
        analysis = msg.content[0].text.strip()
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"error": _redact(str(exc))}), 500

    new_count = _ai_increment_daily_count()
    return jsonify({
        "analysis":      analysis,
        "calls_today":   new_count,
        "daily_limit":   limit,
        "limit_reached": new_count >= limit,
    })












# ── WNBA analysis endpoint ────────────────────────────────────────────────────

@app.route("/api/wnba/analyze", methods=["POST"])
def analyze_wnba():
    """Full WNBA analysis pipeline: team stats + odds + ensemble predictions."""
    from src.model import BettingModel
    from src.wnba_stats_client import WNBAStatsClient
    from src.wnba_features import WNBAFeatureBuilder
    from src.wnba_spread_model import WNBASpreadModel
    from src.wnba_totals_model import WNBATotalsModel
    from src.wnba_college_client import WNBACollegeClient
    data       = request.get_json() or {}
    bankroll   = float(data.get("bankroll", _wnba_analysis_state.get("bankroll", 1000)))
    season     = int(data.get("season", 2025))
    force_refresh = bool(data.get("force_refresh", False))
    use_cached    = bool(data.get("use_cached", False))

    odds_key   = _ODDS_API_KEY
    sports_key = _API_SPORTS_KEY  # optional for WNBA (ESPN used instead)

    if not odds_key or odds_key == "your_odds_api_key_here":
        return jsonify({"error": "ODDS_API_KEY not configured in .env"}), 400

    print(f"ANALYZE [WNBA] health-check: route entered, force_refresh={force_refresh}, snapshot_enabled={_SNAPSHOT_ENABLED}", flush=True, file=sys.stderr)

    # ── Snapshot guard ────────────────────────────────────────────────────────
    if force_refresh:
        _clear_snapshot_sport("wnba")   # atomic, locked, never raises
    _wsnap2 = _read_daily_snapshot()
    if not force_refresh and _snapshot_is_today(_wsnap2) and _wsnap2.get("wnba"):
        _wsp2 = _wsnap2["wnba"]
        _wnba_analysis_state["bankroll"] = bankroll
        if _wnba_analysis_state.get("last_analyzed_at") is None:
            try:
                _wnba_analysis_state["last_analyzed_at"] = datetime.fromisoformat(
                    _wsp2.get("analyzed_at", "")
                )
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)
        return jsonify({
            "success":        True,
            "cached":         True,
            "snapshot":       True,
            "sport":          "wnba",
            "bankroll":       bankroll,
            "analyzed_at":    _wsp2.get("analyzed_at"),
            "results":        _wsp2.get("results", []),
            "parlays":        _wsp2.get("parlays", {}),
            "games_loaded":   _wsp2.get("games_loaded", 0),
            "cv_accuracy":    _wsp2.get("cv_accuracy"),
            "lr_cv_accuracy": _wsp2.get("lr_cv_accuracy"),
            "model_status":   _wsp2.get("model_status", "snapshot"),
        })

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
        _wnba_cached_ts = _wnba_analysis_state.get("last_analyzed_at")
        return jsonify({
            "success": True, "cached": True, "sport": "wnba", "bankroll": bankroll,
            "games_loaded":   meta.get("games_loaded", 0),
            "model_status":   meta.get("model_status", ""),
            "cv_accuracy":    meta.get("cv_accuracy"),
            "lr_cv_accuracy": meta.get("lr_cv_accuracy"),
            "analyzed_at":    _wnba_cached_ts.isoformat() if _wnba_cached_ts else None,
            "results": serialized, "parlays": parlays,
        })

    # Auto-settle any completed WNBA bets first
    try:
        _oc_settle = OddsClient(odds_key, _cache)
        _wl = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
        _wl.settle(_oc_settle, "basketball_wnba")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

    # ── Per-step checkpoint helper (mirrors MLB analyze)
    # Prints to stderr so each step shows up in Railway's deploy log
    # even before any exception fires.  Makes a 500 immediately
    # bisectable to a step.
    def _step(label: str) -> None:
        print(f"ANALYZE [WNBA] {label}", flush=True, file=sys.stderr)
        # Mirror into the /api/analyze/status polling payload (no-op
        # when no worker thread is active).
        _record_analysis_step("wnba", label)

    try:
        _step("importing model modules + config")
        from src.sports_config import WNBA
        wnba_cfg = WNBA

        # Step 1 — load WNBA season data from ESPN free API
        _step("Step 1: loading WNBA season stats")
        wnba_client = WNBAStatsClient(api_key=sports_key, cache=_cache)
        n_completed = wnba_client.load(season)
        _step(f"Step 1 done: {n_completed} completed games loaded")

        # Step 2 — feature builder
        _step("Step 2: building WNBAFeatureBuilder")
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
                # Log diagnostic summary for any team with non-zero adjustment
                n_adjusted = sum(1 for a in college_adjs.values() if abs(a) > 0.01)
                if n_adjusted:
                    _logger.info("college adjustments: %d team(s)", n_adjusted)
        except Exception as _college_err:
            _logger.warning("college adjustment skipped: %s", _college_err)

        # Step 3 — models
        _step("Step 3: training / loading moneyline model")
        ml_model = BettingModel(wnba_cfg)
        status   = ml_model.train_or_load(
            stats_client=wnba_client, feature_builder=fb,
            season=season, force_retrain=False,
        )
        cv_acc    = float(ml_model.cv_accuracy)     if ml_model.cv_accuracy    else None
        lr_cv_acc = float(ml_model.lr_cv_accuracy)  if ml_model.lr_cv_accuracy else None
        _step(f"Step 3 moneyline done: status={status!r}")

        _step("Step 3b: training / loading spread model")
        spread_model = WNBASpreadModel()
        sp_status = spread_model.train_or_load(wnba_client, fb, season)
        _logger.info("wnba spread model: %s", sp_status)
        _step(f"Step 3b done: spread status={sp_status!r}")

        _step("Step 3c: training / loading totals model")
        totals_model = WNBATotalsModel()
        tot_status = totals_model.train_or_load(wnba_client, fb, season)
        _logger.info("wnba totals model: %s", tot_status)
        _step(f"Step 3c done: totals status={tot_status!r}")

        # Step 4 — odds from The Odds API
        _step("Step 4: fetching odds from Odds API  sport_key='basketball_wnba'")
        odds_client = OddsClient(odds_key, _cache)
        _eprint(f"ODDS FETCH STARTING [WNBA] sport_key='basketball_wnba' "
                f"force_refresh={force_refresh}")
        games_pre_filter = odds_client.get_odds(
            sport_key="basketball_wnba", force_refresh=force_refresh,
        )
        _eprint(f"ODDS FETCH COMPLETE [WNBA] returned {len(games_pre_filter)} "
                f"parsed games (pre stale-date filter)")
        _step(f"Step 4: get_odds returned {len(games_pre_filter)} parsed games "
              f"(before stale-date filter)")
        today_et = _today_et()
        kept_dates: dict[str, int] = {}
        dropped_dates: dict[str, int] = {}
        # Per-game audit trail.  For each game log the raw UTC commence_time,
        # the parsed UTC + ET datetimes, the resulting ET date string, and
        # whether the stale-date filter is going to keep or drop it.  If a
        # user reports "all my games got dropped", they paste this block
        # and we can see exactly which games on which days got dropped why.
        for i, g in enumerate(games_pre_filter):
            ct = g.get("commence_time", "")
            d  = _game_et_date(ct) or "<unparsable>"
            verdict = "KEEP" if d >= today_et else "DROP"
            (kept_dates if verdict == "KEEP" else dropped_dates)[d] = \
                (kept_dates if verdict == "KEEP" else dropped_dates).get(d, 0) + 1
            try:
                from zoneinfo import ZoneInfo as _Z
                _utc_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                _et_dt  = _utc_dt.astimezone(_Z("America/New_York"))
                _step(
                    f"Step 4: game[{i:2d}] "
                    f"{(g.get('away_team') or '?')[:3]}@{(g.get('home_team') or '?')[:3]}  "
                    f"raw={ct}  utc={_utc_dt.isoformat()}  et={_et_dt.isoformat()}  "
                    f"et_date={d}  vs today_et={today_et}  -> {verdict}"
                )
            except Exception as _e:                                       # noqa: BLE001
                _step(f"Step 4: game[{i:2d}] raw={ct!r} -- could not parse: {_e}")
        games = _filter_stale_games(games_pre_filter)
        _step(f"Step 4: stale-date filter  today_et={today_et}  "
              f"kept_dates={kept_dates}  dropped_dates={dropped_dates}")
        _step(f"Step 4 done: {len(games)} games with odds")
        _step(f"Step 4b: after stale-game filter: {len(games)} games")
        games       = _lock_in_pre_game_odds(games)
        _step("Step 4c: pre-game odds locked")

        if not games:
            _step("Step 4: no games found — returning empty result")
            return jsonify({
                "success": True, "no_games": True, "results": [],
                "model_status": status, "cv_accuracy": cv_acc,
                "lr_cv_accuracy": lr_cv_acc, "games_loaded": n_completed,
                "sport": "wnba", "bankroll": bankroll,
            })

        # Step 5 — predict per-game with isolation
        _step(f"Step 5: running predictions on {len(games)} games")
        results = []
        # Track per-game outcomes so a "1 game in, 0 games out" failure mode
        # produces a structured summary in logs AND in the API response.  The
        # UI can render a "skipped: N" badge so the user knows a game was
        # dropped (e.g. unknown team) rather than mysteriously empty.
        skipped: list[dict] = []
        for _gi, game in enumerate(games):
            _home = game.get("home_team", "?")
            _away = game.get("away_team", "?")
            _matchup = f"{_away} @ {_home}"
            _step(f"  game {_gi+1}/{len(games)}: {_matchup}  "
                  f"(home={_home!r}, away={_away!r}, "
                  f"commence_time={game.get('commence_time', '?')!r})")
            try:
                built = fb.build_for_game(game)
                if built is None:
                    reason = (
                        f"build_for_game returned None -- WNBAFeatureBuilder "
                        f"could not produce features.  Most likely cause: "
                        f"one of the teams ({_home!r}, {_away!r}) has no "
                        f"historical games in the training set yet (e.g. a "
                        f"2026 expansion team).  Game NOT included in results."
                    )
                    _step(f"  game {_gi+1}: {reason}")
                    skipped.append({
                        "matchup":       _matchup,
                        "home_team":     _home,
                        "away_team":     _away,
                        "commence_time": game.get("commence_time"),
                        "reason":        "build_for_game returned None",
                        "detail":        reason,
                        # Stash the raw game dict so we can build a
                        # market-odds-only stub for the UI's no-model card.
                        "game":          game,
                    })
                    continue
                feature_vec, meta = built

                _step(f"  game {_gi+1}: moneyline predict")
                prediction = ml_model.predict(feature_vec, game_meta=game)

                spread_pred = None
                if spread_model.is_trained:
                    _step(f"  game {_gi+1}: spread predict")
                    try:
                        spread_pred = spread_model.predict(feature_vec, game)
                    except Exception as _sp_err:
                        _eprint(f"ANALYZE [WNBA] spread.predict failed for {_matchup}: "
                                f"{type(_sp_err).__name__}: {_sp_err}")
                        spread_pred = None

                totals_pred = None
                totals_vec  = fb.build_totals_from_meta(meta)
                if totals_model.is_trained and game.get("total_line") is not None and totals_vec is not None:
                    _step(f"  game {_gi+1}: totals predict")
                    try:
                        totals_pred = totals_model.predict(totals_vec, game)
                    except Exception as _t_err:
                        _eprint(f"ANALYZE [WNBA] totals.predict failed for {_matchup}: "
                                f"{type(_t_err).__name__}: {_t_err}")
                        totals_pred = None

                # Same per-game [TRACK] confirmation as the MLB loop --
                # proves the per-classifier recorders fired for every
                # WNBA game.  spread_pred takes the RL slot.
                _track_line_mlb(_matchup, prediction, spread_pred, totals_pred)

                results.append({
                    "game":        game,
                    "prediction":  prediction,
                    "meta":        meta,
                    "spread_pred": spread_pred,
                    "totals_pred": totals_pred,
                })
            except Exception as _g_err:
                _eprint(f"ANALYZE [WNBA] prediction loop crashed on game {_gi+1} ({_matchup}): "
                        f"{type(_g_err).__name__}: {_g_err}")
                _eprint(traceback.format_exc())
                skipped.append({
                    "matchup":       _matchup,
                    "home_team":     _home,
                    "away_team":     _away,
                    "commence_time": game.get("commence_time"),
                    "reason":        f"{type(_g_err).__name__}",
                    "detail":        str(_g_err),
                    "game":          game,
                })
                # Skip this game but continue with the rest

        _step(f"Step 5 done: {len(results)} games predicted, {len(skipped)} skipped")
        if skipped:
            for _sk in skipped:
                _step(f"  skipped: {_sk['matchup']}  reason={_sk['reason']}")
        # Stash the skipped list on the analysis state so the API response +
        # /admin diagnostics can surface it.  The home / sports UI already
        # reads from _wnba_analysis_state, so adding a sibling key here is a
        # zero-risk extension.
        _wnba_analysis_state["skipped"] = skipped

        _wnba_analysis_state["results"]  = results
        _wnba_analysis_state["bankroll"] = bankroll

        # Step 6 — cross-sport daily picks selection (top-5 per category, Half Kelly)
        _step("Step 6: daily picks selection")
        _run_daily_picks_selection()

        # Step 6b — immediate settlement of any freshly-recorded picks
        # whose games are already Final.  See the MLB analyze route for
        # the full rationale; this mirror runs the same hook so a WNBA
        # analyze fired late on a game day also closes out same-day.
        _step("Step 6b: immediate settlement check")
        try:
            _settle_freshly_recorded_picks()
        except Exception as exc:                                          # noqa: BLE001
            _eprint(
                f"IMMEDIATE-SETTLE: hook failed (analyze continues): "
                f"{type(exc).__name__}: {exc}"
            )

        # Reload wnba ledger to get current personal_starting_bankroll for serialization
        _step("Step 7: serializing results")
        _wledger_serial = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
        s_br            = _wledger_serial.data.get("personal_starting_bankroll", bankroll)

        # Wrap _serialize_wnba per-game so one bad game can't kill the batch.
        serialized = []
        for _si, _r in enumerate(results):
            try:
                serialized.append(_serialize_wnba(_r, bankroll, s_br))
            except Exception as _se:
                _eprint(f"ANALYZE [WNBA] _serialize_wnba failed on game {_si}: "
                        f"{type(_se).__name__}: {_se}")
                _eprint(traceback.format_exc())

        # Option A: also serialize the games the model couldn't predict so
        # they appear on the WNBA tab as "NO MODEL PICK" cards with the
        # market odds visible.  Without this the user sees an empty board
        # and has no clue Toronto Tempo @ Phoenix Mercury is even on tonight.
        _skipped_for_ui = _wnba_analysis_state.get("skipped") or []
        for _sk in _skipped_for_ui:
            _game = _sk.get("game")
            if not _game:
                continue
            try:
                stub = _serialize_wnba_no_model(
                    _game,
                    reason=(
                        f"No model pick: {_sk.get('detail') or _sk.get('reason') or '—'}"
                    ),
                )
                serialized.append(stub)
            except Exception as _sn:                                       # noqa: BLE001
                _eprint(f"ANALYZE [WNBA] _serialize_wnba_no_model failed for "
                        f"{_sk.get('matchup', '?')}: {type(_sn).__name__}: {_sn}")
        _step(f"Step 7 done: {len(serialized)}/{len(results)} games serialized "
              f"({len(_skipped_for_ui)} no-model stubs appended)")

        try:
            parlays = _generate_parlays(serialized, bankroll)
        except Exception as _pe:
            _eprint(f"ANALYZE [WNBA] _generate_parlays failed: {type(_pe).__name__}: {_pe}")
            parlays = {}

        _ts = datetime.now(timezone.utc)
        _wnba_analysis_state["parlays"]            = parlays
        _wnba_analysis_state["last_analyzed_at"]   = _ts
        _wnba_analysis_state["last_analysis_meta"] = {
            "games_loaded":  n_completed,
            "model_status":  status,
            "cv_accuracy":   cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
        }
        _step("Step 8: saving cache and snapshot")
        _save_wnba_analysis_cache(serialized, parlays, n_completed, cv_acc, lr_cv_acc,
                                  analyzed_at=_ts)
        _write_analysis_timestamp("wnba", _ts)
        # Dedicated per-sport Supabase key -- same rationale as the MLB block.
        try:
            from zoneinfo import ZoneInfo as _ZI_ts
            _ts_et_wnba = datetime.now(_ZI_ts("America/New_York")).isoformat()
        except Exception:
            _ts_et_wnba = _ts.isoformat()
        try:
            from src import db as _db_ts
            _db_ts.cache_set(
                "last_analyzed_at_wnba", "wnba", _ts.date().isoformat(),
                {"ts": _ts_et_wnba},
            )
            print(
                f"ANALYSIS-TIMESTAMP: updated wnba last_analyzed_at to {_ts_et_wnba}",
                flush=True, file=sys.stderr,
            )
        except Exception as _tse:
            print(
                f"ANALYSIS-TIMESTAMP: dedicated wnba key write failed (ignored): {_tse}",
                flush=True, file=sys.stderr,
            )
        _write_daily_snapshot("wnba", {
            "results":        serialized,
            "parlays":        parlays,
            "games_loaded":   n_completed,
            "cv_accuracy":    cv_acc,
            "lr_cv_accuracy": lr_cv_acc,
            "model_status":   status,
        }, _ts)
        try:
            ensemble_store.save(serialized, "wnba")
        except Exception as _es:
            _eprint(f"ANALYZE [WNBA] ensemble_store.save failed: {type(_es).__name__}: {_es}")
        _step(f"DONE: {len(serialized)} games serialized and saved")

        # ANALYZE COMPLETE summary -- same shape as the MLB log above
        # so a single grep "ANALYZE COMPLETE" finds both sports.
        _with_odds = sum(1 for r in serialized if r.get("pick_team"))
        _no_odds   = len(serialized) - _with_odds
        _eprint(
            f"ANALYZE COMPLETE [WNBA]: {len(serialized)} games total -- "
            f"{_with_odds} with odds, {_no_odds} no_odds"
        )

        # Belt-and-suspenders re-hydrate -- see MLB analyze route for
        # the rationale.  Keeps the disk snapshot + in-memory state
        # consistent before the next /api/schedule call lands.
        try:
            hydrate_state()
        except Exception as _he:                                          # noqa: BLE001
            _eprint(f"ANALYZE [WNBA] post-analyze hydrate_state failed "
                    f"(non-fatal): {type(_he).__name__}: {_he}")

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
            "analyzed_at":    _ts.isoformat(),
            "results":        serialized,
            "parlays":        parlays,
            # New: games dropped during prediction (e.g. unknown teams like
            # 2026 expansion teams missing from training data).  Empty list
            # when nothing was skipped.  Surfaced so the UI / admin can
            # render a "skipped: N" message instead of just an empty board.
            "skipped":        _wnba_analysis_state.get("skipped", []),
        })

    except Exception as exc:
        # Daily Odds-API quota hit -- clean 429 response mirroring the MLB
        # handler above so the UI's toast / banner logic stays uniform
        # across sports.
        if type(exc).__name__ == "OddsApiLimitExceeded":
            try:
                from src.odds_client import odds_usage
                u = odds_usage()
            except Exception:                                             # noqa: BLE001
                u = {"count": 0, "effective_limit": 500, "limit_reached": True}
            _eprint(f"ANALYZE [WNBA] BLOCKED: Odds API daily "
                    f"limit reached ({u['count']}/{u['effective_limit']})")
            return jsonify({
                "success":       False,
                "limit_reached": True,
                "error":         (
                    f"Daily Odds API limit of {u['effective_limit']} reached, "
                    f"additional pulls require manual approval."
                ),
                "calls_today":   u["count"],
                "limit":         u["effective_limit"],
            }), 429

        # Mirror the MLB crash handler -- log type + message FIRST via _eprint
        # so it survives even if traceback formatting or jsonify later fails
        # (e.g. UnicodeEncodeError on Windows).  Every payload is run through
        # _redact so credentials embedded in HTTPError URLs (e.g. ?apiKey=...)
        # can't leak into Railway logs or the JSON response body.
        _exc_type = type(exc).__name__
        _exc_msg  = _redact(str(exc))
        _eprint(f"\nANALYZE [WNBA] CRASHED")
        _eprint(f"  type:    {_exc_type}")
        _eprint(f"  message: {_exc_msg}")
        try:
            _tb = _redact(traceback.format_exc())
            _eprint(f"  traceback:\n{_tb}")
        except Exception:
            _tb = f"{_exc_type}: {_exc_msg}"
        try:
            return jsonify({"error": _exc_msg, "detail": _tb, "exc_type": _exc_type}), 500
        except Exception as _je:
            _eprint(f"  jsonify also failed: {_redact(str(_je))}")
            return (
                f'{{"error": "{_exc_type}: {_exc_msg}"}}'
            ), 500, {"Content-Type": "application/json"}


@app.route("/api/wnba/init", methods=["GET"])
def init_wnba():
    """Return today's cached WNBA analysis for auto-load on startup."""
    try:
        # Snapshot takes priority — permanent pre-game record for today.
        _wsnap = _read_daily_snapshot()
        if _snapshot_is_today(_wsnap) and _wsnap.get("wnba"):
            _wsp = _wsnap["wnba"]
            _wat = _wsp.get("analyzed_at")
            if _wat and _wnba_analysis_state.get("last_analyzed_at") is None:
                try:
                    _wnba_analysis_state["last_analyzed_at"] = datetime.fromisoformat(_wat)
                except Exception as _exc:
                    logging.warning("Suppressed exception in %s: %s", __name__, _exc)
            return jsonify({
                "has_predictions": True,
                "snapshot":        True,
                "analyzed_at":     _wat,
                "sport":           "wnba",
                "games_loaded":    _wsp.get("games_loaded", 0),
                "cv_accuracy":     _wsp.get("cv_accuracy"),
                "lr_cv_accuracy":  _wsp.get("lr_cv_accuracy"),
                "results":         _wsp.get("results", []),
                "parlays":         _wsp.get("parlays", {}),
            })

        today = _today_et()  # ET date — was incorrectly comparing against UTC date

        _ts_store   = _read_analysis_timestamps()
        _wnba_stamp = _ts_store.get("wnba", {})
        _saved_at   = _wnba_stamp.get("analyzed_at")

        if _saved_at and _wnba_analysis_state.get("last_analyzed_at") is None:
            try:
                _wnba_analysis_state["last_analyzed_at"] = datetime.fromisoformat(_saved_at)
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        if not _WNBA_ANALYSIS_CACHE_FILE.exists():
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})
        payload = json.loads(_WNBA_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("date") != today:
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})

        _at = payload.get("analyzed_at") or _saved_at
        if _at and _wnba_analysis_state.get("last_analyzed_at") is None:
            try:
                _wnba_analysis_state["last_analyzed_at"] = datetime.fromisoformat(_at)
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        _wresults = _filter_stale_games(payload.get("results", []))
        return jsonify({
            "has_predictions": bool(_wresults),
            "analyzed_at":     _at,
            "sport":           "wnba",
            "games_loaded":    payload.get("games_loaded", 0),
            "cv_accuracy":     payload.get("cv_accuracy"),
            "lr_cv_accuracy":  payload.get("lr_cv_accuracy"),
            "results":         _wresults,
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
    odds_key = _ODDS_API_KEY
    if odds_key and odds_key != "your_odds_api_key_here":
        try:
            oc      = OddsClient(odds_key, _cache)
            settled = ledger.settle(oc, "basketball_wnba")
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

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


# ── Auto-analysis helpers ─────────────────────────────────────────────────────

def _write_auto_analysis_log(entry: dict) -> None:
    """Append a run entry to data/auto_analysis_log.json (newest-first, cap 60). Never raises."""
    try:
        _AUTO_ANALYSIS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = json.loads(_AUTO_ANALYSIS_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = {"runs": [], "last_success": None}
        runs = raw.get("runs", [])
        runs.insert(0, entry)
        runs = runs[:60]
        if entry.get("status") == "success":
            raw["last_success"] = entry.get("started_at")
        raw["runs"] = runs
        tmp = _AUTO_ANALYSIS_LOG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, default=str), encoding="utf-8")
        tmp.replace(_AUTO_ANALYSIS_LOG_FILE)
    except Exception as _exc:
        _eprint(f"AUTO-ANALYSIS: _write_auto_analysis_log error: {_exc}")




def _run_auto_analysis_job(label: str, is_retry: bool = False) -> None:
    """Run MLB + WNBA analysis via the Flask test client. Called by APScheduler."""
    _eprint(f"AUTO-ANALYSIS [{label}]: starting (is_retry={is_retry})")
    started_at = datetime.now(timezone.utc)

    # Read bankrolls
    try:
        mlb_bankroll = Ledger(path="data/ledger.json", starting_bankroll=250).data.get("bankroll", 250)
    except Exception:
        mlb_bankroll = 250

    try:
        wnba_bankroll = Ledger(path="data/wnba_ledger.json", starting_bankroll=250).data.get("bankroll", 250)
    except Exception:
        wnba_bankroll = 250

    season = _SEASON

    results: dict = {}
    mlb_results: list = []
    wnba_results: list = []

    # ── MLB ───────────────────────────────────────────────────────────────────
    mlb_games = mlb_picks = 0
    mlb_error: str | None = None
    try:
        with app.test_client() as _client:
            resp = _client.post(
                "/api/analyze",
                json={"sport": "mlb", "bankroll": mlb_bankroll, "season": season, "force_refresh": True},
                content_type="application/json",
            )
        if resp.status_code == 200:
            data = resp.get_json() or {}
            mlb_results = data.get("results", []) or []
            mlb_games = len(mlb_results)
            mlb_picks = sum(1 for r in mlb_results if r.get("value_pick"))
        else:
            mlb_error = f"HTTP {resp.status_code}"
    except Exception as _exc:
        mlb_error = str(_exc)
        _eprint(f"AUTO-ANALYSIS [{label}]: MLB error: {traceback.format_exc()}")

    results["MLB"] = {"games": mlb_games, "picks": mlb_picks, "error": mlb_error}

    # ── WNBA ──────────────────────────────────────────────────────────────────
    wnba_games = wnba_picks = 0
    wnba_error: str | None = None
    try:
        with app.test_client() as _client:
            resp = _client.post(
                "/api/wnba/analyze",
                json={"bankroll": wnba_bankroll, "season": season, "force_refresh": True},
                content_type="application/json",
            )
        if resp.status_code == 200:
            data = resp.get_json() or {}
            wnba_results = data.get("results", []) or []
            wnba_games = len(wnba_results)
            wnba_picks = sum(1 for r in wnba_results if r.get("value_pick"))
        else:
            wnba_error = f"HTTP {resp.status_code}"
    except Exception as _exc:
        wnba_error = str(_exc)
        _eprint(f"AUTO-ANALYSIS [{label}]: WNBA error: {traceback.format_exc()}")

    results["WNBA"] = {"games": wnba_games, "picks": wnba_picks, "error": wnba_error}

    # ── Summarise ─────────────────────────────────────────────────────────────
    finished_at = datetime.now(timezone.utc)
    duration_s  = round((finished_at - started_at).total_seconds(), 1)
    mlb_ok  = mlb_error is None
    wnba_ok = wnba_error is None
    overall_ok = mlb_ok and wnba_ok
    if overall_ok:
        status = "success"
    elif mlb_ok or wnba_ok:
        status = "partial"
    else:
        status = "error"

    _eprint(
        f"AUTO-ANALYSIS [{label}] DONE in {duration_s}s"
        f" | MLB: {mlb_games} games / {mlb_picks} picks"
        f" | WNBA: {wnba_games} games / {wnba_picks} picks"
        f" | {status.upper()}"
    )

    with _auto_analysis_lock:
        _auto_analysis_state.update({
            "last_label":    label,
            "last_started":  started_at.isoformat(),
            "last_finished": finished_at.isoformat(),
            "last_duration": duration_s,
            "last_status":   status,
            "last_results":  results,
        })

    _write_auto_analysis_log({
        "label":       label,
        "is_retry":    is_retry,
        "started_at":  started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s":  duration_s,
        "status":      status,
        "results":     results,
    })

    # ── AI summaries: game summaries first (this call), props after ──────────
    # Fire-and-forget background queue so it never blocks the scheduler.  Game
    # summaries complete before prop summaries begin (run_summary_queue order).
    if mlb_ok or wnba_ok:
        try:
            from src import ai_summaries
            game_results = (
                [("mlb", r) for r in mlb_results]
                + [("wnba", r) for r in wnba_results]
            )
            # Run the queue on our own daemon thread (same as launch_summary_queue,
            # still non-blocking) so we can rebuild Top Plays the moment the queue
            # finishes and the AI verdict tiers exist.  Without this the morning
            # slate's picks all fail the Top Plays ai_tier gate (no verdict yet)
            # and /top-picks shows "No picks available yet" until a later cycle.
            # The ai_tier gate + build_rankings logic are unchanged -- we only add
            # this post-summary trigger.
            def _summaries_then_top_plays(_results=game_results, _label=label):
                try:
                    ai_summaries.run_summary_queue(
                        game_results=_results, do_games=True, do_props=True)
                except Exception as _qe:                                   # noqa: BLE001
                    _eprint(f"AUTO-ANALYSIS [{_label}]: summary queue error: {_qe}")
                try:
                    from src.top_picks import build_rankings
                    _res = build_rankings(sys.modules[__name__]) or {}
                    _eprint(f"AUTO-ANALYSIS [{_label}]: Top Plays rebuilt after "
                            f"AI summaries -- {_res.get('count', 0)} play(s)")
                except Exception as _be:                                   # noqa: BLE001
                    _eprint(f"AUTO-ANALYSIS [{_label}]: Top Plays rebuild failed: "
                            f"{type(_be).__name__}: {_be}")
            threading.Thread(target=_summaries_then_top_plays, daemon=True).start()
        except Exception as _se:                                          # noqa: BLE001
            _eprint(f"AUTO-ANALYSIS [{label}]: summary launch failed: {_se}")

    # Model-pick tracking.  The 8 AM wave logs each model's picks as pending.
    # The noon wave re-checks: for games that haven't started it replaces an
    # 8 AM pending pick only when the noon pick is strictly better; started
    # games are locked.  Any other wave (retry) just logs.
    try:
        if (label or "").lower() == "noon":
            _noon_reconcile_model_picks()
        else:
            _log_model_picks()
    except Exception as _mpe:                                             # noqa: BLE001
        _eprint(f"AUTO-ANALYSIS [{label}]: model-pick log failed: {_mpe}")

    # Stake today's model picks into the combined-$1000 model ledger.
    # Idempotent per game/bet_type, so the 8 AM wave places them and the
    # noon wave only adds genuinely new picks; after noon no analysis wave
    # runs, so the day's stakes are LOCKED.  Stakes freeze at placement.
    try:
        from src.daily_picks import load_daily_picks
        from src import ledger_integration as _li
        _placed = _li.place_model_daily_picks(load_daily_picks())
        if _placed.get("games") or _placed.get("props"):
            _eprint(f"AUTO-ANALYSIS [{label}]: model ledger staked "
                    f"{_placed['games']} game + {_placed['props']} prop bet(s)")
    except Exception as _lpe:                                             # noqa: BLE001
        _eprint(f"AUTO-ANALYSIS [{label}]: model-ledger placement failed: {_lpe}")

    if not overall_ok and not is_retry:
        _schedule_auto_retry(label)












def get_todays_schedule(sport: str) -> list[dict]:
    """Public accessor for the home/sports pages: today's prefetched
    schedule (matchup + start time) for *sport*.  Reads the schedule
    cache populated by JOB 3 (or any earlier on-demand fetch).  Never
    triggers analysis or odds calls beyond the cheap schedule fetch.
    Returns [] on any failure."""
    try:
        return _fetch_raw_schedule(sport.lower(), _today_et()) or []
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"get_todays_schedule({sport}) failed: {_exc}")
        return []


def _schedule_auto_retry(label: str) -> None:
    """Schedule a 15-minute retry of the auto-analysis job after a partial/error."""
    global _sched
    try:
        if _sched is None or not _sched.running:
            _eprint(f"AUTO-ANALYSIS [{label}]: cannot schedule retry — scheduler not running")
            return
        retry_time = datetime.now(timezone.utc) + timedelta(minutes=15)
        _sched.add_job(
            _run_auto_analysis_job,
            "date",
            run_date=retry_time,
            id=f"auto_analysis_retry_{label}_{int(retry_time.timestamp())}",
            kwargs={"label": label, "is_retry": True},
            replace_existing=True,
            misfire_grace_time=900,
        )
        _eprint(f"AUTO-ANALYSIS [{label}]: retry scheduled for {retry_time.isoformat()}")
    except Exception as _exc:
        _eprint(f"AUTO-ANALYSIS [{label}]: _schedule_auto_retry error: {_exc}")







def _settle_freshly_recorded_picks() -> dict:
    """Settle any open bets whose games are ALREADY Final, regardless of
    the 11 AM-2 AM ET game-hours gate that _run_auto_settlement_job uses.

    Called from /api/analyze and /api/wnba/analyze right after
    _run_daily_picks_selection() writes today's picks to the ledger.
    Closes the gap where running analyze late at night (or the morning
    after) would record picks for games that finished hours ago, then
    leave them sitting open until the next scheduler tick (potentially
    the following day if it's currently outside the gate window).

    Settles BOTH ledgers because select_daily_picks() may have written
    to either sport's ledger regardless of which sport's analyze route
    invoked us (the picks-selection function is cross-sport).

    Logs each immediately-settled pick to stderr with matchup, bet
    side, result (W/L/PUSH), and P&L so the path is observable in
    Railway logs:

      IMMEDIATE-SETTLE [MLB]: settled 2 freshly-recorded pick(s)
        IMMEDIATE-SETTLE [MLB]: Yankees @ Red Sox | ML Yankees -> WIN  | model P&L: $+45.20
        IMMEDIATE-SETTLE [MLB]: Mets @ Cubs       | RL Mets    -> LOSS | model P&L: $-25.00

    Returns a summary dict the caller can include in /api/analyze's
    JSON response if needed.  No-op when ODDS_API_KEY is missing
    (matches _run_auto_settlement_job's behavior).

    Side effects propagate automatically through Ledger.settle():
      - history list grows -> Confidence Performance card updates
      - model_bankroll / personal_bankroll adjusted in place
      - _settle_model_trackers() updates xgb/lr/nn picks histories
      - _append_to_archive() writes to data/bet_history_archive.json
    """
    odds_key = _ODDS_API_KEY
    if not odds_key or odds_key == "your_odds_api_key_here":
        _eprint("IMMEDIATE-SETTLE: skipped (ODDS_API_KEY not configured)")
        return {"mlb_settled": 0, "wnba_settled": 0, "settled": []}

    oc = OddsClient(odds_key, _cache)
    settled_all: list = []

    _BET_TYPE_SHORT = {"single": "ML", "run_line": "RL", "spread": "SPR", "totals": "TOT"}

    for label, path, sport_key, cache_key in (
        ("MLB",  "data/ledger.json",      "baseball_mlb",   "scores_baseball_mlb_3"),
        ("WNBA", "data/wnba_ledger.json", "basketball_wnba", "scores_basketball_wnba_3"),
    ):
        try:
            led = Ledger(path=path, starting_bankroll=250)
            open_for_sport = [
                b for b in (led.data.get("open_bets") or [])
                if b.get("sport_key") == sport_key
            ]
            if not open_for_sport:
                continue
            # Invalidate the scores cache so settle sees the freshest
            # completion state -- a game that finished 10 minutes ago
            # may still be missing from the 3-day rolling cache.
            _cache.invalidate(cache_key)
            new = led.settle(oc, sport_key)
            if not new:
                _eprint(
                    f"IMMEDIATE-SETTLE [{label}]: 0 of {len(open_for_sport)} "
                    f"open pick(s) ready to settle (games still in progress / pre-game)"
                )
                continue
            settled_all.extend(new)
            _eprint(
                f"IMMEDIATE-SETTLE [{label}]: settled {len(new)} "
                f"freshly-recorded pick(s)"
            )
            for s in new:
                bts    = _BET_TYPE_SHORT.get(s.get("bet_type", "single"), "ML")
                result = s.get("result", "?").upper()
                team   = s.get("bet_team", "?")
                away   = s.get("away_team", "?")
                home   = s.get("home_team", "?")
                pnl    = s.get("model_pnl", 0.0)
                _eprint(
                    f"  IMMEDIATE-SETTLE [{label}]: "
                    f"{away} @ {home} | {bts} {team} -> {result} "
                    f"| model P&L: ${pnl:+.2f}"
                )
        except Exception as exc:                                              # noqa: BLE001
            _eprint(
                f"IMMEDIATE-SETTLE [{label}]: error: "
                f"{type(exc).__name__}: {exc}"
            )

    return {
        "mlb_settled":  sum(
            1 for s in settled_all if s.get("sport_key") == "baseball_mlb"
        ),
        "wnba_settled": sum(
            1 for s in settled_all if s.get("sport_key") == "basketball_wnba"
        ),
        "settled": settled_all,
    }








def _noon_reconcile_model_picks() -> None:
    """Noon re-check (PART 2/2).  Re-runs every model's analysis and, for
    games that have NOT started, replaces an 8 AM pending pick only when the
    noon pick is strictly better.  Started games (live_score's detector,
    incl. warmup/pre-game) are locked: never replaced or removed.  Logs a
    per-(sport, model) summary of kept / replaced / locked."""
    try:
        import sys as _sys
        from src import model_picks as _mp
        from components import live_score as _ls
        backend_mod = _sys.modules[__name__]

        def _started(sport, d):
            try:
                return _ls.game_has_started(
                    backend_mod,
                    commence_time=d.get("commence_time"),
                    home_team=d.get("home_team"),
                    away_team=d.get("away_team"),
                    sport=sport,
                )
            except Exception:                                              # noqa: BLE001
                return False

        summary = _mp.reconcile_noon(backend_mod, _started)
        for (sp, m), v in sorted(summary.items()):
            _eprint(f"NOON-RECHECK {sp}/{m}: kept={v['kept']} "
                    f"replaced={v['replaced']} locked={v['locked']}")
    except Exception as exc:                                               # noqa: BLE001
        _eprint(f"NOON-RECHECK failed: {type(exc).__name__}: {exc}")




























def _detect_pitching_changes() -> int:
    """Compare today's MLB probable starters to the last cycle's; log + count
    any change.  One free MLB Stats API call per cycle (hydrate probablePitcher)."""
    url = (f"{_MLB_STATS_BASE}/schedule?sportId=1&date={_today_et()}"
           f"&hydrate=probablePitcher")
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"CYCLE pitching-change fetch failed: {exc}")
        return 0
    changes = 0
    for date_block in (data.get("dates") or []):
        for g in (date_block.get("games") or []):
            gpk = str(g.get("gamePk") or "")
            if not gpk:
                continue
            teams = g.get("teams") or {}
            ap = (((teams.get("away") or {}).get("probablePitcher") or {})
                  .get("fullName") or "TBD")
            hp = (((teams.get("home") or {}).get("probablePitcher") or {})
                  .get("fullName") or "TBD")
            cur = f"{ap}|{hp}"
            prev = _last_probables.get(gpk)
            if prev is not None and prev != cur and "TBD" not in cur:
                changes += 1
                _eprint(f"CYCLE pitching change game={gpk}: '{prev}' -> '{cur}'")
            _last_probables[gpk] = cur
    return changes


def _refresh_game_odds_detect_moves() -> dict:
    """Step 2: re-fetch current ML/RL/Total lines for today's games and flag
    significant moves (ML > 5 cents either side, totals line > 0.5).  Returns
    {'games','ml_moves','total_moves'}.  Best-effort -- never raises."""
    res = {"games": 0, "ml_moves": 0, "total_moves": 0}
    odds_key = _ODDS_API_KEY
    if not odds_key or odds_key == "your_odds_api_key_here":
        return res
    try:
        oc = OddsClient(odds_key, _cache)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"CYCLE step2 OddsClient init failed: {exc}")
        return res
    for sport_key in ("baseball_mlb", "basketball_wnba"):
        try:
            games = oc.get_odds(sport_key, force_refresh=True) or []
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE step2 {sport_key} odds error: {exc}")
            continue
        for g in games:
            gid = str(g.get("id") or "")
            if not gid:
                continue
            res["games"] += 1
            cur = {
                "ml_home": g.get("h2h_home_odds"),
                "ml_away": g.get("h2h_away_odds"),
                "total":   g.get("total_line"),
            }
            prev = _last_seen_lines.get(gid)
            if prev:
                ml_moved = False
                for side in ("ml_home", "ml_away"):
                    a, b = prev.get(side), cur.get(side)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                            and abs(b - a) > 5:
                        ml_moved = True
                if ml_moved:
                    res["ml_moves"] += 1
                    _eprint(
                        f"CYCLE line move ML {g.get('away_team')} @ {g.get('home_team')}: "
                        f"{prev.get('ml_away')}/{prev.get('ml_home')} -> "
                        f"{cur.get('ml_away')}/{cur.get('ml_home')}"
                    )
                ta, tb = prev.get("total"), cur.get("total")
                if isinstance(ta, (int, float)) and isinstance(tb, (int, float)) \
                        and abs(tb - ta) > 0.5:
                    res["total_moves"] += 1
                    _eprint(
                        f"CYCLE line move TOTAL {g.get('away_team')} @ {g.get('home_team')}: "
                        f"{ta} -> {tb}"
                    )
            _last_seen_lines[gid] = cur
    return res






















@app.route("/api/admin/settle_now", methods=["POST"])
def admin_settle_now():
    """Force the auto-settlement job to run immediately, bypassing the
    11 AM-2 AM ET game-hours gate.  Powers the "Force Settlement"
    admin button.  Returns the same summary dict the scheduler logs.
    """
    print("[ADMIN-ROUTE] /api/admin/settle_now invoked",
          flush=True, file=sys.stderr)
    try:
        result = _run_auto_settlement_job(force=True) or {}
        _eprint(
            f"FORCE-SETTLE complete: settled={result.get('settled', 0)}  "
            f"wins={result.get('wins', 0)}  losses={result.get('losses', 0)}  "
            f"voided={result.get('voided', 0)}"
        )
        return jsonify({"success": True, **result})
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"FORCE-SETTLE FAILED: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}")
        return jsonify({"success": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/api/admin/props/refresh_now", methods=["POST"])
def admin_props_refresh_now():
    """Run the same Tier-1 refresh + scoring pass that auto_props_refresh
    fires every 15 minutes, on demand.

    Synchronous from Flask's perspective (which is what the test-client
    pattern in pages/admin.py expects).  The admin button wraps the
    call in ``asyncio.to_thread`` so NiceGUI's event loop stays
    responsive while it runs, even on slates with thousands of props.

    Returns a summary dict the admin button uses to render the
    ``Done -- X picks above threshold`` status line.
    """
    import time as _time
    started = _time.monotonic()
    print("[ADMIN-ROUTE] /api/admin/props/refresh_now invoked",
          flush=True, file=sys.stderr)
    try:
        from src.props_client import run_tier_1_refresh
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"FORCE-PROPS import failed: {type(exc).__name__}: {exc}")
        return jsonify({"success": False,
                        "error": f"import failed: {exc}"}), 500
    try:
        # run_tier_1_refresh() runs the raw-line fetch AND the scoring
        # pass (the scheduler hook added in the props-scored-cache PR),
        # so by the time it returns the cache is already updated.
        run_tier_1_refresh()
        payload    = load_scored_props() or {}
        picks      = payload.get("picks") or []
        summary    = payload.get("summary") or {}
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        result = {
            "success":      True,
            "kept":         len(picks),
            "scored":       int(summary.get("scored") or 0),
            "deduped":      int(summary.get("deduped") or 0),
            "predict_err":  int(summary.get("predict_err") or 0),
            "generated_at": payload.get("generated_at"),
            "elapsed_ms":   elapsed_ms,
        }
        _eprint(
            f"FORCE-PROPS complete  kept={result['kept']}  "
            f"scored={result['scored']}  deduped={result['deduped']}  "
            f"elapsed={elapsed_ms}ms"
        )
        return jsonify(result)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"FORCE-PROPS FAILED: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}")
        return jsonify({"success": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 500


@app.route("/api/admin/props/repull", methods=["POST"])
def admin_props_repull():
    """Full FRESH props re-pull for the 'MLB Props' admin button.

    Re-hits the Odds API for the full ALL_MODEL_MARKETS set (all 11
    model-backed markets) and re-scores every prop from scratch, OVERWRITING
    whatever is cached rather than merging -- so a stale pre-PR#207 cache
    (old/partial market set) is fully replaced.  Uses the existing
    props_model scoring pipeline; builds no new model.  Synchronous (the
    admin button wraps it in asyncio.to_thread)."""
    import time as _time
    started = _time.monotonic()
    print("[ADMIN-ROUTE] /api/admin/props/repull invoked",
          flush=True, file=sys.stderr)
    try:
        from src.props_client import run_full_props_repull
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"REPULL import failed: {type(exc).__name__}: {exc}")
        return jsonify({"success": False, "error": f"import failed: {exc}"}), 500
    try:
        run_full_props_repull()
        payload    = load_scored_props() or {}
        picks      = payload.get("picks") or []
        summary    = payload.get("summary") or {}
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        result = {
            "success":      True,
            "kept":         len(picks),
            "scored":       int(summary.get("scored") or 0),
            "deduped":      int(summary.get("deduped") or 0),
            "predict_err":  int(summary.get("predict_err") or 0),
            "generated_at": payload.get("generated_at"),
            "elapsed_ms":   elapsed_ms,
        }
        _eprint(
            f"REPULL complete  kept={result['kept']}  scored={result['scored']}  "
            f"deduped={result['deduped']}  elapsed={elapsed_ms}ms"
        )
        return jsonify(result)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"REPULL FAILED: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return jsonify({"success": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 500




def _run_ai_analysis_job(force: bool = False) -> None:
    """Background worker for the admin 'Run AI Analysis' button.  Generates
    every missing Groq summary/breakdown sequentially, updating
    _ai_run_state for the live progress poll.  Releases _ai_run_lock when
    done.

    force=True ('Force AI Refresh') bypasses change-detection: every game
    verdict, prop summary, and player breakdown is regenerated + overwritten
    regardless of whether it was already cached."""
    import time as _t
    started = _t.monotonic()
    try:
        from src import ai_summaries
        # Game picks (both sports) that carry a model pick.
        game_results = []
        for sport, state in (("mlb", _analysis_state), ("wnba", _wnba_analysis_state)):
            for r in (state.get("results") or []):
                if isinstance(r, dict) and r.get("pick_team") and (r.get("game_id") or r.get("id")):
                    game_results.append((sport, r))
        # Scored props, highest confidence first.
        try:
            from src.props_scored_cache import load_scored_props
            props = list((load_scored_props() or {}).get("picks") or [])
        except Exception:                                                 # noqa: BLE001
            props = []
        props.sort(key=lambda p: -float(p.get("confidence") or 0.0))
        # One breakdown per player (their highest-confidence pick).
        player_items: list = []
        seen_players: set = set()
        for p in props:
            pl = p.get("player")
            if pl and pl not in seen_players:
                seen_players.add(pl)
                player_items.append(p)

        _ai_run_state.update({
            "total": len(game_results) + len(props) + len(player_items),
            "done": 0, "games_generated": 0, "props_generated": 0,
            "breakdowns_generated": 0, "skipped": 0, "failed": 0,
        })

        def _bump(status: str, gen_key: str) -> None:
            if status == "generated":
                _ai_run_state[gen_key] += 1
                _t.sleep(_AI_RUN_DELAY)        # pace only real Groq calls
            elif status == "cached":
                _ai_run_state["skipped"] += 1
            else:
                _ai_run_state["failed"] += 1
            _ai_run_state["done"] += 1

        # Phase 0 — pre-populate ai_game_bets_* rows (mirrors what
        # _generate_games does in the 15-min cycle).  Without this the admin
        # button only wrote summary rows, so a manual "Run AI Analysis" could
        # never fix a "AI analysis unavailable" game-detail page.  Reuses the
        # already-built game_results (and the already-imported ai_summaries);
        # _generate_games' own `if not db.cache_get(_bets_key)` guard keeps it
        # idempotent, so this is cheap when the rows already exist.
        _ai_run_state["phase"] = "bets analysis"
        try:
            if game_results:
                ai_summaries._generate_games(game_results)
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"ADMIN bets pre-pop error: {type(exc).__name__}: {exc}")

        # Phase 1 — game summaries
        _ai_run_state["phase"] = "game summaries"
        for sport, g in game_results:
            _bump(ai_summaries.ensure_game_summary(sport, g, force=force),
                  "games_generated")

        # Phase 2 — prop summaries (desc confidence)
        _ai_run_state["phase"] = "prop summaries"
        for r in props:
            _bump(ai_summaries.ensure_prop_summary(r, force=force),
                  "props_generated")

        # Phase 3 — player breakdowns
        _ai_run_state["phase"] = "player breakdowns"
        for p in player_items:
            from src import player_ai_breakdown
            _bump(player_ai_breakdown.generate_for_pick(p, force=force),
                  "breakdowns_generated")

        elapsed = round(_t.monotonic() - started, 1)
        summary = {
            "games_generated":      _ai_run_state["games_generated"],
            "props_generated":      _ai_run_state["props_generated"],
            "breakdowns_generated": _ai_run_state["breakdowns_generated"],
            "skipped":              _ai_run_state["skipped"],
            "failed":               _ai_run_state["failed"],
            "elapsed":              elapsed,
        }
        _ai_run_state["summary"]     = summary
        _ai_run_state["elapsed"]     = elapsed
        _ai_run_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _eprint(
            "AI-ANALYSIS COMPLETE in %ss | game summaries: %d generated | "
            "prop summaries: %d generated | player breakdowns: %d generated | "
            "%d already cached/skipped | %d failed" % (
                elapsed, summary["games_generated"], summary["props_generated"],
                summary["breakdowns_generated"], summary["skipped"], summary["failed"],
            )
        )
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"AI-ANALYSIS FAILED: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
    finally:
        _ai_run_state["running"] = False
        try:
            _ai_run_lock.release()
        except Exception:                                                 # noqa: BLE001
            pass


@app.route("/api/admin/ai_analysis/run", methods=["POST"])
def admin_ai_analysis_run():
    """Kick off the on-demand AI summary/breakdown generation on a daemon
    thread.  Returns immediately; the admin page polls /status for progress.
    Guarded so it can't be double-started.

    POST body {"force": true} ('Force AI Refresh') regenerates everything,
    bypassing the cached-skip; otherwise only missing items are generated."""
    force = bool((request.get_json(silent=True) or {}).get("force"))
    if not _ai_run_lock.acquire(blocking=False):
        return jsonify({"success": True, "already_running": True})
    _ai_run_state.update({
        "running": True, "phase": "starting", "done": 0, "total": 0,
        "games_generated": 0, "props_generated": 0, "breakdowns_generated": 0,
        "skipped": 0, "failed": 0, "summary": None, "elapsed": None,
        "finished_at": None, "forced": force,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    _eprint(f"[ADMIN-ROUTE] /api/admin/ai_analysis/run invoked (force={force}) — "
            f"starting AI analysis")
    try:
        threading.Thread(target=_run_ai_analysis_job,
                         kwargs={"force": force}, daemon=True).start()
    except Exception as exc:                                              # noqa: BLE001
        _ai_run_state["running"] = False
        try:
            _ai_run_lock.release()
        except Exception:                                                 # noqa: BLE001
            pass
        return jsonify({"success": False, "error": str(exc)}), 500
    return jsonify({"success": True, "started": True})


@app.route("/api/admin/ai_analysis/status", methods=["GET"])
def admin_ai_analysis_status():
    """Live progress for the on-demand AI analysis run."""
    return jsonify({"success": True, **_ai_run_state})



# ── APScheduler bootstrap ────────────────────────────────────────────────
# Inline block moved to scheduler.init() in PR #282 -- the first and
# only approved rewrite in the decomposition series.  See migration_log.
_werkzeug_main = os.environ.get("WERKZEUG_RUN_MAIN", "false") == "true"
_sched = init(app, _werkzeug_main)

# ── Persistent-cache startup steps (Issues 1 + 2 + 4) ───────────────────────
# 1. Ensure data/ exists before any file op (Railway can drop it on redeploy)
# 2. Purge any cache file / Supabase row whose date != today
# 3. Restore today's cache rows from Supabase to disk when local files are
#    missing (the common case right after a Railway redeploy)
_purge_stale_caches_on_boot()
_restore_caches_from_supabase_on_boot()



_model_cache_boot_inventory()


# ─────────────────────────────────────────────────────────────────────────────
# Boot Health Report -- one-glance OK/FAIL per subsystem, printed last so
# the user's eye lands here when scrolling Railway logs.  All checks are
# read-only (no mutations, no remote calls beyond a tiny db status read);
# safe to run on every boot.
# ─────────────────────────────────────────────────────────────────────────────





_boot_health_report()


# ─────────────────────────────────────────────────────────────────────────────
# Boot-time no-odds prediction warmup
# ─────────────────────────────────────────────────────────────────────────────
#
# Midnight reset pre-computes a full slate of no-odds predictions and
# persists them to Supabase under the "no_odds" date sentinel.  Two cases
# leave the cache empty and the user sees blank prediction cards on
# launch:
#
#   1.  Fresh Railway deploy that happens between two midnight runs --
#       the container started AFTER the last midnight job and won't see
#       a fresh prefetch until the next 00:00 ET.
#   2.  Supabase outage during the last midnight run -- the cache row
#       never landed.
#
# This boot hook fixes both: spawn a daemon thread that, after the rest
# of boot finishes, checks today's cache and triggers a prefetch when
# it's missing.  Runs in the background so we don't block uvicorn
# accepting the first HTTP request -- the predictions land 30-90s
# later, by which time most users haven't navigated past /home yet.



_boot_predictions_warmup()

# Props subsystem boot sync.  Two pieces:
#   1. Restore the per-day props payload from Supabase if the local
#      .cache/props_mlb_YYYY-MM-DD.json was wiped by a Railway redeploy.
#   2. Restore the pitcher / batter props joblib models from Supabase
#      same way -- the runtime predictor falls back to a market-only
#      heuristic when the joblibs are missing, so this is best-effort.
try:
    from src.props_client import restore_from_supabase_if_missing as _props_boot
    _props_boot_did = _props_boot()
    print(
        f"STARTUP: props boot sync -- "
        f"{'restored from Supabase' if _props_boot_did else 'local cache OK'}",
        flush=True, file=sys.stderr,
    )
except Exception as _pe:
    print(f"STARTUP WARNING: props boot sync failed: {_pe}",
          flush=True, file=sys.stderr)
try:
    from src.props_model import restore_models_from_supabase as _props_models_boot
    _props_models_status = _props_models_boot()
    print(
        f"STARTUP: props joblib restore -- {_props_models_status}",
        flush=True, file=sys.stderr,
    )
except Exception as _pe:
    print(f"STARTUP WARNING: props joblib restore failed: {_pe}",
          flush=True, file=sys.stderr)

# Analysis-timestamps boot restore.  data/analysis_timestamps.json
# powers the admin "Last analyzed" line; Railway redeploys wipe the
# file so without this restore the admin label would show a "—" until
# the next analyze run.
#
# Two-tier seed strategy:
#   1. Dedicated per-sport Supabase keys (last_analyzed_at_mlb /
#      last_analyzed_at_wnba) written synchronously by each analyze
#      route.  These are the most reliable source.
#   2. Shared analysis_timestamps key (legacy) as a fallback for
#      deployments that predate the dedicated keys.
try:
    _ts_boot_direct: dict[str, str | None] = {"mlb": None, "wnba": None}
    try:
        from src import db as _db_boot
        if _db_boot.is_supabase():
            for _bk, _bsport in (("last_analyzed_at_mlb", "mlb"),
                                  ("last_analyzed_at_wnba", "wnba")):
                _brow = _db_boot.cache_get(_bk)
                if isinstance(_brow, dict):
                    _bdata = _brow.get("data") or _brow
                    _bts   = _bdata.get("ts") if isinstance(_bdata, dict) else None
                    if _bts:
                        _ts_boot_direct[_bsport] = _bts
    except Exception as _dbe:
        print(f"STARTUP: dedicated key read failed (ignored): {_dbe}",
              flush=True, file=sys.stderr)

    # Fall back to shared key for any sport the dedicated key didn't cover
    _ts_boot = _read_analysis_timestamps()
    print(
        f"STARTUP: timestamps boot restore -- "
        f"mlb_dedicated={_ts_boot_direct.get('mlb') or 'none'}  "
        f"mlb_shared={(_ts_boot.get('mlb') or {}).get('analyzed_at') or 'none'}  "
        f"wnba_dedicated={_ts_boot_direct.get('wnba') or 'none'}  "
        f"wnba_shared={(_ts_boot.get('wnba') or {}).get('analyzed_at') or 'none'}",
        flush=True, file=sys.stderr,
    )
    # Seed the in-memory state: prefer dedicated key, fall back to shared.
    for _sp_key, _state in (
        ("mlb",  _analysis_state),
        ("wnba", _wnba_analysis_state),
    ):
        _saved = _ts_boot_direct.get(_sp_key) or (_ts_boot.get(_sp_key) or {}).get("analyzed_at")
        if _saved and _state.get("last_analyzed_at") is None:
            try:
                _state["last_analyzed_at"] = datetime.fromisoformat(_saved)
                print(
                    f"STARTUP: in-memory last_analyzed_at[{_sp_key}] "
                    f"seeded: {_saved}",
                    flush=True, file=sys.stderr,
                )
            except Exception as _se:                                       # noqa: BLE001
                print(f"STARTUP: in-memory seed[{_sp_key}] parse failed: {_se}",
                      flush=True, file=sys.stderr)
except Exception as _te:
    print(f"STARTUP WARNING: timestamps boot restore failed: {_te}",
          flush=True, file=sys.stderr)

print("STARTUP: app ready", flush=True, file=sys.stderr)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
