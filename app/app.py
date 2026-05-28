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


# ── Startup credential check ────────────────────────────────────────────────
# Print a scannable banner about ODDS_API_KEY at startup so misconfigurations
# surface in Railway logs immediately (rather than 70 seconds later when an
# analyze call crashes with 401).  Catches:
#   - missing env var
#   - empty string
#   - placeholder text from .env.example
#   - leading / trailing whitespace
#   - typo'd env var names (enumerates every *_KEY / *ODDS* env var so a
#     mistake like THE_ODDS_API_KEY / oddsapikey / Odds_Api_Key is visible
#     in one glance)
# Pure diagnostic -- no assertions, no exits.  Logs go to stderr so they
# appear in Railway's deploy log alongside other STARTUP: lines.
def _validate_odds_api_key_on_boot() -> None:
    print("STARTUP: validating Odds API credentials...",
          flush=True, file=sys.stderr)

    key_raw = os.environ.get("ODDS_API_KEY")

    if key_raw is None:
        print("STARTUP CRED-CHECK [ODDS_API_KEY]: NOT SET.  "
              "Add it in Railway -> service -> Variables tab.  "
              "Until then every analyze call will fail with 401.",
              flush=True, file=sys.stderr)
    else:
        key = key_raw.strip()
        problems = []
        if not key:
            problems.append("value is empty after strip()")
        if key == "your_odds_api_key_here":
            problems.append("value is still the .env.example placeholder text")
        if key != key_raw:
            problems.append(
                f"value has surrounding whitespace "
                f"(raw_len={len(key_raw)}, stripped_len={len(key)}) -- "
                f"trim it in Railway Variables")
        if " " in key:
            problems.append("value contains an embedded space")

        if problems:
            print(f"STARTUP CRED-CHECK [ODDS_API_KEY]: PROBLEMS -- "
                  f"{'; '.join(problems)}",
                  flush=True, file=sys.stderr)
        else:
            # Print first/last 4 chars + length so the user can compare
            # against what they expect.  Never prints the full key.
            print(f"STARTUP CRED-CHECK [ODDS_API_KEY]: present, "
                  f"len={len(key)}, prefix={key[:4]!r}, suffix={key[-4:]!r}",
                  flush=True, file=sys.stderr)

    # Enumerate other env vars that LOOK like API keys, so a Railway typo
    # (e.g. setting THE_ODDS_API_KEY instead of ODDS_API_KEY) becomes
    # obvious from the log line alone.  Only var NAMES are printed --
    # never values.
    suspect_names = sorted(
        k for k in os.environ
        if ("ODDS" in k.upper() or k.upper().endswith("_KEY"))
        and k != "ODDS_API_KEY"
    )
    if suspect_names:
        print(f"STARTUP CRED-CHECK [other *ODDS* / *_KEY vars present]: "
              f"{', '.join(suspect_names)}",
              flush=True, file=sys.stderr)
    else:
        print("STARTUP CRED-CHECK [other *ODDS* / *_KEY vars]: none",
              flush=True, file=sys.stderr)


_validate_odds_api_key_on_boot()


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

# nightly_retrain — graceful stub if APScheduler is absent or any import fails
class _NightlyRetrainStub:
    """No-op stub used when src.nightly_retrain fails to import."""
    def start(self, **kw): return None
    def get_log(self):
        return {"runs": [], "last_success": None,
                "next_run": None, "scheduler_running": False,
                "error": "nightly_retrain module unavailable"}
    def run_nightly_retrain(self): pass

try:
    import src.nightly_retrain as nightly_retrain
    print("STARTUP:   src.nightly_retrain OK", flush=True, file=sys.stderr)
except Exception as _e:
    print(f"STARTUP WARNING: src.nightly_retrain failed ({_e}) — scheduler disabled",
          flush=True, file=sys.stderr)
    nightly_retrain = _NightlyRetrainStub()  # type: ignore[assignment]

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

# ── Global state (single-user desktop app) ────────────────────────────────────
_cache = Cache()
_ANALYSIS_TTL        = 900  # 15 minutes — skip API if last run was within this window
_ANALYSIS_CACHE_FILE      = Path("data/analysis_cache.json")
_WNBA_ANALYSIS_CACHE_FILE = Path("data/wnba_analysis_cache.json")
_PRE_GAME_ODDS_FILE       = Path("data/pre_game_odds.json")
_EXPLAIN_CACHE_FILE       = Path("data/explain_cache.json")
_AI_BREAKDOWN_CACHE_FILE  = Path("data/ai_breakdown_cache.json")
_ARCHIVE_PATH             = Path("data/bet_history_archive.json")
# Lightweight timestamp file — survives container restarts without reading the
# full results payloads.  Shape: {"mlb": {"analyzed_at": "<iso>", "date": "YYYY-MM-DD"}, "wnba": {...}}
_ANALYSIS_TIMESTAMPS_FILE = Path("data/analysis_timestamps.json")
_DAILY_SNAPSHOT_FILE      = Path("data/daily_snapshot.json")
_DAILY_SNAPSHOT_TMP       = Path("data/daily_snapshot.json.tmp")

# Step 2: single lock so concurrent requests (init + analyze) never race on the file.
import threading as _threading
_snapshot_lock = _threading.Lock()

# Step 3: master kill-switch.  Set env var SNAPSHOT_ENABLED=0 to bypass entirely.
_SNAPSHOT_ENABLED = os.environ.get("SNAPSHOT_ENABLED", "1").strip() not in ("0", "false", "False", "FALSE")


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
_CACHE_KEY_ANALYSIS_MLB  = "analysis_cache:mlb"
_CACHE_KEY_ANALYSIS_WNBA = "analysis_cache:wnba"


def _ensure_data_dir() -> None:
    """Make sure data/ exists before any file op.  Railway's filesystem can
    drop directories between deployments, so re-creating at every import is
    cheaper than trying to detect when it's gone."""
    try:
        Path("data").mkdir(parents=True, exist_ok=True)
    except Exception as exc:                                              # noqa: BLE001
        print(f"STARTUP WARNING: could not create data/: {exc}",
              flush=True, file=sys.stderr)


def _supabase_cache_get(key: str) -> dict | None:
    """Synchronous read with a hard timeout.  Returns None on timeout or any
    error so the caller can fall back to a local file without delay."""
    import concurrent.futures
    def _do():
        from src import db as _db
        row = _db.cache_get(key)
        return (row or {}).get("data") if row else None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_do).result(timeout=5.0)
    except concurrent.futures.TimeoutError:
        print(f"SUPABASE cache_get({key}) timed out after 5s",
              flush=True, file=sys.stderr)
        return None
    except Exception as exc:                                              # noqa: BLE001
        print(f"SUPABASE cache_get({key}) failed: {exc}",
              flush=True, file=sys.stderr)
        return None


def _supabase_cache_set(key: str, sport: str | None, date: str, data: dict) -> None:
    """Fire-and-forget mirror to Supabase.  Returns immediately; the write
    happens in a daemon thread so a slow / hung Supabase NEVER blocks the
    analyze path.  Local-file write is the source of truth -- this is just
    a best-effort backup for cross-redeploy persistence."""
    def _do():
        try:
            from src import db as _db
            _db.cache_set(key, sport, date, data)
        except Exception as exc:                                          # noqa: BLE001
            print(f"SUPABASE cache_set({key}) failed: {exc}",
                  flush=True, file=sys.stderr)
    try:
        import threading as _th
        _th.Thread(target=_do, name=f"sb-cache-set-{key}", daemon=True).start()
    except Exception as exc:                                              # noqa: BLE001
        # If we can't even spawn a thread, log and move on -- analysis
        # never waits.
        print(f"SUPABASE cache_set({key}) thread spawn failed: {exc}",
              flush=True, file=sys.stderr)


def _supabase_cache_delete(key: str) -> None:
    """Fire-and-forget delete.  Same safety guarantee as _supabase_cache_set
    -- a slow Supabase never blocks the caller."""
    def _do():
        try:
            from src import db as _db
            _db.cache_delete(key)
        except Exception as exc:                                          # noqa: BLE001
            print(f"SUPABASE cache_delete({key}) failed: {exc}",
                  flush=True, file=sys.stderr)
    try:
        import threading as _th
        _th.Thread(target=_do, name=f"sb-cache-del-{key}", daemon=True).start()
    except Exception as exc:                                              # noqa: BLE001
        print(f"SUPABASE cache_delete({key}) thread spawn failed: {exc}",
              flush=True, file=sys.stderr)


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

_ai_counter_mem: dict[str, int] = {}     # in-process fallback when Supabase is off


def _ai_daily_counter_key() -> str:
    return f"ai_calls:{_today_et()}"


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


def _purge_stale_caches_on_boot() -> None:
    """Issue 1 + Issue 2 fix.  Runs once at module-load time.

    1. Ensures data/ exists (so subsequent writes don't ENOENT).
    2. For each cache file, if its `date` field doesn't match today's ET
       date, deletes the file.  Stops a snapshot from a prior day from
       leaking into today's UI.
    3. Asks Supabase to drop any app_cache rows whose date != today.
    """
    _ensure_data_dir()
    try:
        today = _today_et()
    except Exception:                                                     # noqa: BLE001
        # _today_et is defined later in the module -- skip cleanup if we're
        # somehow imported before that point.  The midnight-reset job is
        # the backstop in that case.
        return

    # Track whether the daily snapshot got purged -- if so we also nuke
    # today's odds cache below.  Stale snapshot is a reliable signal
    # that "the last analyze run was on a previous ET day", which means
    # any in-memory / on-disk odds payload from that run is also stale
    # and should NOT be reused by the next analyze.
    snapshot_was_stale = False

    purge_paths = (
        _ANALYSIS_CACHE_FILE, _WNBA_ANALYSIS_CACHE_FILE,
        _DAILY_SNAPSHOT_FILE, _ENSEMBLE_PICKS_FILE,
    )
    for path in purge_paths:
        try:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                path.unlink(missing_ok=True)
                continue
            payload = json.loads(raw)
            file_date = payload.get("date")
            if file_date and file_date != today:
                print(f"STARTUP: dropping stale {path.name} "
                      f"(date={file_date}, today={today})",
                      flush=True, file=sys.stderr)
                path.unlink(missing_ok=True)
                if path == _DAILY_SNAPSHOT_FILE:
                    snapshot_was_stale = True
                # Rewrite empty ensemble file immediately so the boot
                # health report's "Ensemble picks file" check doesn't
                # flag it FAIL until the next analyze runs.
                if path == _ENSEMBLE_PICKS_FILE:
                    try:
                        _ENSEMBLE_PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
                        _ENSEMBLE_PICKS_FILE.write_text(
                            json.dumps(
                                {"date": today, "picks": {"mlb": [], "wnba": []}},
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        print(
                            f"STARTUP: created empty {_ENSEMBLE_PICKS_FILE.name} "
                            f"for {today} (next analyze will populate)",
                            flush=True, file=sys.stderr,
                        )
                    except Exception as exc:                              # noqa: BLE001
                        print(
                            f"STARTUP WARNING: could not create empty "
                            f"{_ENSEMBLE_PICKS_FILE.name}: {exc}",
                            flush=True, file=sys.stderr,
                        )
        except Exception as exc:                                          # noqa: BLE001
            # Don't fail boot over a corrupt cache file -- just nuke it
            # and let the next analysis run recreate it.
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"STARTUP WARNING: removed unreadable {path.name}: {exc}",
                  flush=True, file=sys.stderr)

    # If the daily snapshot was from a prior ET day, the in-process
    # _cache layer also holds stale odds payloads from that run.
    # Wipe the odds-cache keys so the next analyze hits The Odds API
    # for fresh lines instead of recycling yesterday's.  Same matchup
    # patterns the nightly full-clear job (_run_job2_full_clear) uses,
    # so the behavior matches a real nightly cron firing.
    if snapshot_was_stale:
        try:
            # Glob the .cache/ dir for every "odds_*.json" file -- the
            # real cache keys include commence_from + commence_to UTC
            # timestamps (see src.odds_client._odds_cache_key) so the
            # short-string invalidate() call we used to make here never
            # matched anything.  Wiping by prefix is what actually
            # clears yesterday's odds before the next analyze fires.
            from pathlib import Path as _P
            _wiped = 0
            for _f in _P(".cache").glob("odds_*.json"):
                try:
                    _f.unlink()
                    _wiped += 1
                except Exception:                                          # noqa: BLE001
                    pass
            print(
                f"STARTUP: snapshot was stale -- wiped {_wiped} odds "
                f"cache file(s) so the next analyze fetches fresh lines",
                flush=True, file=sys.stderr,
            )
        except Exception as exc:                                          # noqa: BLE001
            print(f"STARTUP WARNING: odds-cache clear failed: {exc}",
                  flush=True, file=sys.stderr)

    # Best-effort Supabase cleanup -- silent if Supabase isn't connected.
    # Hard 5s timeout so a slow Supabase never blocks module import.
    try:
        import concurrent.futures
        from src import db as _db
        def _purge():
            return _db.cache_delete_stale(today)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            try:
                n = ex.submit(_purge).result(timeout=5.0)
            except concurrent.futures.TimeoutError:
                print("STARTUP WARNING: Supabase stale-purge timed out after 5s "
                      "(continuing -- local-file caches were already purged)",
                      flush=True, file=sys.stderr)
                n = 0
        if n:
            print(f"STARTUP: dropped {n} stale Supabase app_cache rows",
                  flush=True, file=sys.stderr)
    except Exception as exc:                                              # noqa: BLE001
        print(f"STARTUP WARNING: Supabase stale-purge failed: {exc}",
              flush=True, file=sys.stderr)


def _restore_caches_from_supabase_on_boot() -> None:
    """Issue 4 fix.  When the container restarts and local files are gone
    (or stale-purged above), pull today's cache rows from Supabase and
    write them back to disk so the rest of the code -- which reads local
    files -- works transparently."""
    try:
        from src import db as _db
        if not _db.is_supabase():
            return
        today = _today_et()
    except Exception:                                                     # noqa: BLE001
        return

    pairs = [
        (_CACHE_KEY_SNAPSHOT,      _DAILY_SNAPSHOT_FILE),
        (_CACHE_KEY_ANALYSIS_MLB,  _ANALYSIS_CACHE_FILE),
        (_CACHE_KEY_ANALYSIS_WNBA, _WNBA_ANALYSIS_CACHE_FILE),
    ]
    restored: list[str] = []
    for key, path in pairs:
        if path.exists():
            continue                                                      # local file is the source of truth
        row = _supabase_cache_get(key)
        if not row:
            continue
        if row.get("date") != today:
            continue                                                      # stale -- already deleted by _purge
        try:
            _ensure_data_dir()
            path.write_text(json.dumps(row, default=str), encoding="utf-8")
            restored.append(path.name)
        except Exception as exc:                                          # noqa: BLE001
            print(f"STARTUP WARNING: could not restore {path.name} from Supabase: {exc}",
                  flush=True, file=sys.stderr)
    if restored:
        print(f"STARTUP: restored from Supabase -> {', '.join(restored)}",
              flush=True, file=sys.stderr)


def _eprint(*args, **kwargs) -> None:
    """Safe stderr print that never raises.

    Encodes with UTF-8 + errors='replace' so box-drawing chars, emoji, and any
    non-cp1252 characters can't crash the crash-handler on Windows terminals.
    Runs every message through the credential redactor so an HTTPError that
    embeds `?apiKey=...` in its message can't leak the key into Railway logs.
    Falls back to a no-op if stderr itself is unavailable.
    """
    try:
        msg = " ".join(_redact(a) for a in args) + kwargs.get("end", "\n")
        buf = getattr(sys.stderr, "buffer", None)
        if buf is not None:
            buf.write(msg.encode("utf-8", errors="replace"))
            buf.flush()
        else:
            sys.stderr.write(msg)
            sys.stderr.flush()
    except Exception:
        pass  # last resort — never let logging kill the app


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


def _today_et() -> str:
    """Return today's date string in US/Eastern (handles DST automatically)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        # Fallback for environments without zoneinfo: approximate with UTC-4 (EDT)
        return datetime.now(timezone(timedelta(hours=-4))).date().isoformat()


def _game_et_date(commence_time: str) -> str:
    """Return YYYY-MM-DD in ET for a game's commence_time ISO string."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return ""


def _filter_stale_games(games: list) -> list:
    """Drop games whose ET date is strictly before today (yesterday's leftovers)."""
    today = _today_et()
    return [g for g in games if _game_et_date(g.get("commence_time", "")) >= today]


def _read_daily_snapshot() -> dict:
    """Read daily snapshot file; return {} on any error.  Thread-safe."""
    if not _SNAPSHOT_ENABLED:
        return {}
    with _snapshot_lock:
        try:
            if not _DAILY_SNAPSHOT_FILE.exists():
                return {}
            raw = _DAILY_SNAPSHOT_FILE.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
        except Exception as _e:
            print(f"SNAPSHOT read error (ignored): {_e}", flush=True, file=sys.stderr)
            return {}


def _snapshot_is_today(snap: dict) -> bool:
    """True if snapshot's date equals today in Eastern time."""
    if not _SNAPSHOT_ENABLED:
        return False
    try:
        return bool(snap) and snap.get("date") == _today_et()
    except Exception:
        return False


def hydrate_state() -> tuple[int, int]:
    """Re-read today's analysis from disk and seed the in-memory
    _analysis_state / _wnba_analysis_state dicts.

    Call this at the start of every page render so:
      - cold containers (post-deploy) immediately have today's picks
      - any path that wrote to the cache files (scheduler, manual
        Run, external tool) is visible to the UI on the next page
        load WITHOUT requiring app restart
      - the in-memory dicts always reflect whichever cache file is
        newest on disk -- no stale-Python-state-vs-served-render skew

    Idempotent + safe to call concurrently.  Source-of-truth order:
      1. data/daily_snapshot.json (atomic write-once per ET day)
      2. data/analysis_cache.json / data/wnba_analysis_cache.json
         (legacy per-sport caches, written by /api/analyze)
      3. nothing -- leave state as-is, return zeros

    Returns (mlb_count, wnba_count) for caller logging."""

    try:
        snap = _read_daily_snapshot()
        is_today = _snapshot_is_today(snap)
    except Exception as exc:                                              # noqa: BLE001
        print(f"hydrate_state: snapshot read failed: {exc}",
              flush=True, file=sys.stderr)
        snap, is_today = {}, False

    import json as _json
    from pathlib import Path as _Path

    def _seed(state_dict, sport_key: str, cache_path: str) -> int:
        sp = (snap.get(sport_key) or {}) if is_today else {}
        results = sp.get("results")
        analyzed_at = sp.get("analyzed_at")

        if not results:
            try:
                p = _Path(cache_path)
                if p.exists():
                    payload = _json.loads(p.read_text(encoding="utf-8"))
                    if payload.get("date") == _today_et():
                        results = _filter_stale_games(
                            payload.get("results") or []
                        )
                        analyzed_at = payload.get("analyzed_at") or analyzed_at
            except Exception as exc:                                      # noqa: BLE001
                print(f"hydrate_state: {sport_key} cache read failed: {exc}",
                      flush=True, file=sys.stderr)

        if not results:
            return 0

        # Replace the list -- crucial that we assign a fresh list rather
        # than mutate in place, so any UI render that captured the old
        # results reference sees an empty view and the page's own
        # state_dict["results"] read on next render gets the new list.
        state_dict["results"] = list(results)
        if analyzed_at:
            try:
                state_dict["last_analyzed_at"] = datetime.fromisoformat(analyzed_at)
            except Exception:                                             # noqa: BLE001
                pass
        return len(results)

    mlb_n  = _seed(_analysis_state,      "mlb",  "data/analysis_cache.json")
    wnba_n = _seed(_wnba_analysis_state, "wnba", "data/wnba_analysis_cache.json")
    return mlb_n, wnba_n


def _write_daily_snapshot(sport: str, payload: dict, ts: datetime) -> None:
    """
    Persist sport's analysis into the daily snapshot file.

    Always overwrites the sport entry's `analyzed_at` timestamp + payload
    so a force-refresh / manual re-analyze actually updates what the
    admin panel reads.  (The previous write-once-per-day guard left the
    snapshot stuck at the first analyze's timestamp, which is why the
    Admin "Last analyzed" display went stale after re-runs.)  The
    analyze route's own snapshot guard at /api/analyze still prevents
    the heavy pipeline from running redundantly within a day, so a
    second run only happens with explicit force_refresh=True -- a
    deliberate user-intent signal that the snapshot SHOULD update.

    Uses an atomic temp-file + rename so the file is never partially
    written.  Thread-safe via _snapshot_lock.
    """
    if not _SNAPSHOT_ENABLED:
        return
    with _snapshot_lock:
        try:
            Path("data").mkdir(exist_ok=True)
            today = _today_et()
            # Read current state without re-acquiring the lock (already held)
            snap: dict = {}
            try:
                if _DAILY_SNAPSHOT_FILE.exists():
                    raw = _DAILY_SNAPSHOT_FILE.read_text(encoding="utf-8")
                    if raw.strip():
                        snap = json.loads(raw)
            except Exception:
                snap = {}
            # Fresh day -> start clean
            if snap.get("date") != today:
                snap = {"date": today}
            # Always overwrite this sport's entry with the freshest data +
            # the freshest analyzed_at timestamp.  The other sport (if
            # present) is preserved untouched.
            snap[sport] = {"analyzed_at": ts.isoformat(), **payload}
            # Step 4: atomic write -- temp file then rename so the live file is
            # never in a partially-written state.
            raw_out = json.dumps(snap, indent=2, default=str)
            _DAILY_SNAPSHOT_TMP.write_text(raw_out, encoding="utf-8")
            _DAILY_SNAPSHOT_TMP.replace(_DAILY_SNAPSHOT_FILE)
            # Mirror to Supabase app_cache so the snapshot survives Railway
            # redeploys.  Best-effort -- silent on failure.
            _supabase_cache_set(_CACHE_KEY_SNAPSHOT, None, today, snap)
            _eprint(
                f"SNAPSHOT [{sport.upper()}]: updated analyzed_at="
                f"{ts.isoformat()} -> local + Supabase"
            )
        except Exception as _e:
            print(f"SNAPSHOT write error (ignored): {_e}", flush=True, file=sys.stderr)
            try:
                _DAILY_SNAPSHOT_TMP.unlink(missing_ok=True)
            except Exception:
                pass


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
            except Exception:
                pass


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
    """Call the Anthropic analyst model with a single user prompt."""
    import anthropic as _anthropic
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


def _call_analyst_chat(extra_context: str, messages: list, max_tokens: int = 800) -> str:
    """Call the Anthropic analyst model with a multi-turn conversation.

    extra_context is appended to the system prompt so the analyst has today's
    game data in every reply.  messages is the full history including the latest
    user message, in [{role, content}] form.
    """
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")
    system = _ANALYST_SYSTEM_PROMPT
    if extra_context:
        system += f"\n\n{extra_context}"
    client = _anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return msg.content[0].text.strip()


def _sp_pitch_mix_text(name: str) -> str:
    """Compact pitch-mix string for a starter (cached); '' if unavailable."""
    try:
        from src import ai_context as _aic
        mix = _aic.pitch_mix(_aic.resolve_player_id(name or ""))
        txt = _aic.pitch_mix_text(mix)
        return txt.replace("Arsenal: ", "").rstrip(".") if txt else ""
    except Exception:                                                      # noqa: BLE001
        return ""


def _build_chat_context(results: list, bankroll: float, sport: str) -> str:
    """Build a compact text summary of today's games for the chat system prompt."""
    if not results:
        return "No games have been analyzed yet for today. Tell the user to run analysis first."

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

    lines = [f"TODAY'S {sport.upper()} SLATE — {len(serialized)} GAMES\n"]

    for g in serialized[:16]:
        away = g.get("away_team", "Away")
        home = g.get("home_team", "Home")

        pick_team = g.get("pick_team", "")
        pick_odds = g.get("pick_odds")
        ml_conf   = g.get("ml_confidence") or g.get("xgb_prob") or 0
        edge      = g.get("pick_edge") or 0
        conflict  = g.get("conflict", False)

        rl_pick  = g.get("run_line_pick_team", "")
        rl_point = g.get("run_line_point", -1.5)
        rl_side  = g.get("run_line_side", "")
        rl_odds  = g.get("run_line_pick_odds")

        total_dir  = (g.get("direction") or "").upper()
        total_line = g.get("total_line", "")

        h_sp_name = g.get("home_sp_name", "")
        a_sp_name = g.get("away_sp_name", "")
        h_sp      = g.get("home_sp") or {}
        a_sp      = g.get("away_sp") or {}

        shap_vals = ((g.get("shap") or {}).get("values") or [])[:3]
        uf_score  = (g.get("upset_factor") or {}).get("score", "n/a")

        parts = [f"{away} @ {home}:"]
        if conflict:
            parts.append("  ML: SKIP (models conflict)")
        elif pick_team:
            parts.append(
                f"  ML: {pick_team} {_format_odds(pick_odds)} | "
                f"{ml_conf * 100:.1f}% conf | {edge * 100:+.1f}% edge"
            )
        if rl_pick:
            pt_str = f"{rl_point:+.1f}" if rl_side == "home" else f"{-rl_point:+.1f}"
            parts.append(f"  RL: {rl_pick} {pt_str} {_format_odds(rl_odds)}")
        if total_dir and total_line:
            parts.append(f"  Total: {total_dir} {total_line}")
        sp_parts = []
        for _nm, _sp in ((a_sp_name, a_sp), (h_sp_name, h_sp)):
            if not _nm:
                continue
            _line = (f"{_nm} ERA:{_sp.get('era', '?')} WHIP:{_sp.get('whip', '?')} "
                     f"K9:{_sp.get('k_per_9', '?')}")
            _mix = _sp_pitch_mix_text(_nm)
            if _mix:
                _line += f" [{_mix}]"
            sp_parts.append(_line)
        if sp_parts:
            parts.append(f"  SPs: {' vs '.join(sp_parts)}")
        if shap_vals:
            top = ", ".join(v.get("label") or v.get("feature", "") for v in shap_vals)
            parts.append(f"  Key factors: {top}")
        parts.append(f"  Upset risk: {uf_score}/10")

        lines.append("\n".join(parts))

    return "\n\n".join(lines)


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

# ── EV / value-pick threshold ──────────────────────────────────────────────────
# Minimum pick_edge for a game to receive value_pick=True in _serialize()
# and to appear in the EV Scan section on the home page.  Exposed as a
# module-level constant so the display label always stays in sync with the
# actual gate, and the threshold can be tuned from one place without a
# grep-and-replace across multiple files.
EV_MIN_EDGE: float = 0.03

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

# ── Auto-analysis scheduler state ─────────────────────────────────────────────
_auto_analysis_lock  = threading.Lock()
_auto_analysis_state: dict = {
    "last_label":    None,
    "last_started":  None,
    "last_finished": None,
    "last_duration": None,
    "last_status":   None,   # "success" | "partial" | "error" | None
    "last_results":  {},     # {"MLB": {...}, "WNBA": {...}}
}
_AUTO_ANALYSIS_LOG_FILE = Path("data/auto_analysis_log.json")

# ── Model-bets settings (per-sport toggle for auto-pick) ─────────────────────
# The Admin sub-page exposes a switch per sport so the user can disable a
# sport from the model's auto-pick pool.  Default: MLB on, WNBA off.  Persisted
# as a tiny JSON file so the choice survives restarts.
_MODEL_SETTINGS_FILE = Path("data/model_settings.json")
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

# ── Auto-settlement scheduler state ───────────────────────────────────────────
_auto_settlement_lock  = threading.Lock()
_auto_settlement_state: dict = {
    "last_ran_at":  None,   # ISO UTC
    "last_settled": 0,
    "last_wins":    0,
    "last_losses":  0,
    "last_voided":  0,
}

# ── Consolidated 15-minute refresh-cycle state ────────────────────────────────
# The auto_props_refresh job now runs one coordinated pass (schedule+scores →
# game odds → prop lines → re-score → settlement → AI summaries).
_refresh_cycle_lock  = threading.Lock()   # non-blocking guard against overlap
_refresh_cycle_state: dict = {
    "last_ran_at":   None,   # ISO UTC
    "last_duration": None,   # seconds
    "last_summary":  None,   # dict of per-step counts
}
# Last-seen game lines (per Odds-API game id) for movement detection, and last
# probable starters (per gamePk) for pitching-change detection.  Process-local.
_last_seen_lines: dict[str, dict] = {}
_last_probables:  dict[str, str]  = {}

# Per-cycle change-detection state (process-local).  Compared each 15-min tick.
#   _last_game_state: gid -> {sport, ml_home, ml_away, total, rl_point,
#                             pitchers, lineup, wind, temp}
#   _last_prop_state: "player|market" -> {line, side, recommendation,
#                             predicted_value, confidence}
_last_game_state: dict[str, dict] = {}
_last_prop_state: dict[str, dict] = {}

# Module-level scheduler reference (set at startup)
_sched = None

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
    except Exception:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "OK", 200


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
_wnba_linescore_mem: dict[str, tuple[float, dict]] = {}

_QUARTER_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _espn_state_to_mlb_state(state: str, completed: bool) -> str:
    """Map ESPN status.type.state → MLB abstractGameState vocabulary."""
    if completed or state == "post":
        return "Final"
    if state == "in":
        return "Live"
    return "Preview"


def _wnba_period_ordinal(period: int) -> str:
    """1..4 → '1st'..'4th'; 5+ → 'OT', 'OT2', etc.  Matches MLB's currentInningOrdinal role."""
    if not period:
        return ""
    if period in _QUARTER_ORDINAL:
        return _QUARTER_ORDINAL[period]
    return "OT" if period == 5 else f"OT{period - 4}"


def _normalize_espn_wnba_scoreboard(raw: dict) -> dict:
    """ESPN scoreboard payload → MLB-shaped {dates:[{games:[...]}]} envelope."""
    games: list[dict] = []
    for ev in (raw.get("events") or []):
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = (comp.get("status") or {})
        st_type = (status.get("type") or {})
        state    = st_type.get("state", "pre")
        complete = bool(st_type.get("completed", False))
        period   = int(status.get("period") or 0)
        clock    = status.get("displayClock", "")
        detail   = st_type.get("shortDetail") or st_type.get("description") or ""

        home_team = away_team = None
        home_score = away_score = None
        for c in (comp.get("competitors") or []):
            team_name = ((c.get("team") or {}).get("displayName")
                         or (c.get("team") or {}).get("name") or "")
            try:
                score_val = int(c.get("score")) if c.get("score") not in (None, "") else None
            except (TypeError, ValueError):
                score_val = None
            if c.get("homeAway") == "home":
                home_team, home_score = team_name, score_val
            elif c.get("homeAway") == "away":
                away_team, away_score = team_name, score_val
        if not home_team or not away_team:
            continue

        try:
            game_pk = int(ev.get("id"))
        except (TypeError, ValueError):
            game_pk = 0

        abstract = _espn_state_to_mlb_state(state, complete)
        ordinal  = _wnba_period_ordinal(period) if abstract == "Live" else ""

        games.append({
            "gamePk":   game_pk,
            "gameDate": ev.get("date"),
            "teams": {
                "home": {"team": {"name": home_team}},
                "away": {"team": {"name": away_team}},
            },
            "status": {
                "abstractGameState": abstract,
                "detailedState":     detail or abstract,
                "codedGameState":    state,
            },
            "linescore": {
                "currentInning":         period,
                "currentInningOrdinal":  ordinal,
                "displayClock":          clock,
                "isLive":                abstract == "Live",
                "teams": {
                    "home": {"runs": home_score if home_score is not None else 0},
                    "away": {"runs": away_score if away_score is not None else 0},
                },
            },
        })
    return {"dates": [{"games": games}]} if games else {"dates": []}


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
        except Exception:
            pass

    return jsonify(data)


# ── Full schedule view: arbitrary date, all games (with or without odds) ─────
# Used by pages/sport.py's date-nav UI.  Returns a normalized envelope
# joining the schedule fetch (MLB Stats API / ESPN scoreboard) with any
# model picks the analyze pipeline produced for the same game.
#
# Cache strategy:
#   - Local Cache  (file-backed, in-memory): 1-hour TTL same as the
#     existing per-sport schedule proxies.
#   - Supabase app_cache: 30-day TTL via the "schedule:<sport>:<date>"
#     key.  The "date" column on app_cache is set to the literal
#     "schedule" string (not the YYYY-MM-DD) so cache_delete_stale
#     (which prunes rows where date != today_et) leaves these alone.
#     Past-date schedules persist indefinitely so historical browsing
#     stays available across Railway restarts.
#
# Picks join:
#   - When date == today_et:  pull from in-memory _analysis_state /
#     _wnba_analysis_state which carries the freshest model picks.
#   - When date < today_et:   join against ledger history (settled
#     bets) so the game card can show the result + P/L.
#   - When date > today_et:   no picks (future-dated -- no analysis
#     has run yet).

def _et_date_of(iso: str) -> str:
    """Return the ET calendar date (YYYY-MM-DD) of an ISO timestamp,
    or '' on failure.  Used to group schedule games by the day they're
    actually played in Eastern time."""
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo as _ZI
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(_ZI("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return str(iso)[:10]


def _schedule_is_postponed(e: dict) -> bool:
    ds = (e.get("detailed_status") or "").lower()
    return "postpon" in ds or (e.get("coded_status") or "") in ("D", "DR", "PR")


def _schedule_priority(e: dict) -> int:
    """Higher = the entry we'd rather keep when two represent the same
    game.  Postponed twins lose to a live/final/rescheduled entry."""
    if _schedule_is_postponed(e):
        return 0
    if e.get("is_live"):
        return 4
    st = (e.get("status") or "")
    ds = (e.get("detailed_status") or "").lower()
    if st == "Final" or "final" in ds:
        return 3
    if e.get("rescheduled_from"):
        return 2
    return 1


def _dedup_schedule_games(rows: list[dict]) -> list[dict]:
    """Collapse duplicate schedule rows for the same game.

    Handles the postponed-and-rescheduled case (BUG: a game PPD'd from
    5/22 to 5/23 comes back under both date blocks): when a non-
    postponed entry exists for a matchup, every postponed entry for
    that same matchup is dropped, so only the rescheduled game on its
    new date survives.  Then collapses exact gamePk dupes and same-
    matchup-same-ET-date dupes, keeping the highest-priority entry.
    Two legitimately separate games (same teams on different days of a
    series) stay distinct because their ET dates differ.
    """
    def mk(e: dict) -> tuple:
        return ((e.get("away_team") or "").strip().lower(),
                (e.get("home_team") or "").strip().lower())

    # Drop postponed twins of a matchup that also has a non-postponed entry.
    non_ppd_matchups = {mk(e) for e in rows if not _schedule_is_postponed(e)}
    rows = [e for e in rows
            if not (_schedule_is_postponed(e) and mk(e) in non_ppd_matchups)]

    # Pass 1: collapse exact gamePk dupes.
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for e in rows:
        gid = e.get("id")
        if not gid:
            no_id.append(e)
            continue
        if gid not in by_id or _schedule_priority(e) > _schedule_priority(by_id[gid]):
            by_id[gid] = e
    stage = list(by_id.values()) + no_id

    # Pass 2: collapse same matchup + same ET calendar date.
    by_md: dict[tuple, dict] = {}
    for e in stage:
        key = (mk(e), _et_date_of(e.get("commence_time")))
        if key not in by_md or _schedule_priority(e) > _schedule_priority(by_md[key]):
            by_md[key] = e
    return list(by_md.values())


def _normalize_mlb_schedule(raw: dict) -> list[dict]:
    """Convert a raw MLB Stats API schedule response into the same
    flat list the UI consumes for The Odds API games.  Each game gets
    a stable id, home/away team names, commence_time ISO string, and
    status flags.

    Captures codedGameState + rescheduledFrom so the dedup pass can
    drop postponed-and-rescheduled twins, and an ``is_live`` flag so
    the UI can show a live score instead of the scheduled tip time.
    """
    out: list[dict] = []
    for date_block in raw.get("dates") or []:
        for g in date_block.get("games") or []:
            teams = g.get("teams") or {}
            home  = (teams.get("home") or {}).get("team") or {}
            away  = (teams.get("away") or {}).get("team") or {}
            status = g.get("status") or {}
            ls     = g.get("linescore") or {}
            home_runs = ((ls.get("teams") or {}).get("home") or {}).get("runs")
            away_runs = ((ls.get("teams") or {}).get("away") or {}).get("runs")
            abstract = status.get("abstractGameState") or "Preview"
            coded    = status.get("codedGameState") or ""
            out.append({
                "id":              str(g.get("gamePk") or ""),
                "home_team":       home.get("name") or "",
                "away_team":       away.get("name") or "",
                "commence_time":   g.get("gameDate") or "",
                "status":          abstract,
                "coded_status":    coded,
                "detailed_status": status.get("detailedState") or "",
                "rescheduled_from": g.get("rescheduledFrom") or g.get("rescheduledFromDate") or "",
                "is_live":         (abstract == "Live") or (coded == "I"),
                "home_score":      home_runs,
                "away_score":      away_runs,
                # Live in-game detail (linescore hydrate) so a card can show
                # the inning/count without a second API call.
                "inning_ordinal":  ls.get("currentInningOrdinal") or "",
                "is_top_inning":   bool(ls.get("isTopInning")),
                "balls":           ls.get("balls"),
                "strikes":         ls.get("strikes"),
                "outs":            ls.get("outs"),
            })
    return _dedup_schedule_games(out)


def _normalize_wnba_schedule(raw: dict) -> list[dict]:
    """Same as _normalize_mlb_schedule but pulls from the
    already-reshaped ESPN envelope written by
    _normalize_espn_wnba_scoreboard."""
    out: list[dict] = []
    for date_block in raw.get("dates") or []:
        for g in date_block.get("games") or []:
            teams = g.get("teams") or {}
            home  = (teams.get("home") or {}).get("team") or {}
            away  = (teams.get("away") or {}).get("team") or {}
            status = g.get("status") or {}
            ls     = g.get("linescore") or {}
            home_pts = ((ls.get("teams") or {}).get("home") or {}).get("runs")
            away_pts = ((ls.get("teams") or {}).get("away") or {}).get("runs")
            abstract = status.get("abstractGameState") or "Preview"
            coded    = status.get("codedGameState") or ""
            out.append({
                "id":              str(g.get("gamePk") or ""),
                "home_team":       home.get("name") or "",
                "away_team":       away.get("name") or "",
                "commence_time":   g.get("gameDate") or "",
                "status":          abstract,
                "coded_status":    coded,
                "detailed_status": status.get("detailedState") or "",
                "rescheduled_from": g.get("rescheduledFrom") or g.get("rescheduledFromDate") or "",
                "is_live":         (abstract == "Live") or (coded == "I"),
                "home_score":      home_pts,
                "away_score":      away_pts,
            })
    return _dedup_schedule_games(out)


def _schedule_cache_key(sport: str, date_str: str) -> str:
    return f"schedule:{sport}:{date_str}"


def _fetch_raw_schedule(sport: str, date_str: str) -> list[dict]:
    """Live-fetch + normalize one sport's schedule for one ET date.

    Falls back through three layers:
      1. Local Cache (1 h TTL) -- short-circuit for repeat hits in
         the same Railway boot.
      2. Supabase app_cache (no TTL via the "schedule" date sentinel)
         -- restored after Railway redeploys.
      3. Live fetch from MLB Stats / ESPN -- last resort.

    Returns the list of normalized game dicts (possibly empty)."""
    import time as _time
    import urllib.request as _urlreq
    import urllib.error  as _urlerr

    sport = sport.lower()
    local_key = f"normalized_schedule_{sport}_{date_str}"

    # 1) Local 1-hour memory cache
    cached_local = _cache.get(local_key, ttl=3600)
    if cached_local is not None:
        return cached_local

    # 2) Supabase app_cache
    try:
        from src import db as _db
        row = _db.cache_get(_schedule_cache_key(sport, date_str))
        if row and isinstance(row.get("data"), dict):
            games = row["data"].get("games") or []
            if isinstance(games, list):
                _cache.set(local_key, games)
                return games
    except Exception:                                                     # noqa: BLE001
        pass

    # 3) Live fetch
    if sport == "mlb":
        url = f"{_MLB_STATS_BASE}/schedule?sportId=1&date={date_str}&hydrate=linescore"
    elif sport == "wnba":
        espn_date = date_str.replace("-", "")
        url = f"{_ESPN_WNBA_BASE}/scoreboard?dates={espn_date}"
    else:
        return []

    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (_urlerr.URLError, Exception) as exc:                          # noqa: BLE001
        _logger.warning("schedule fetch %s %s failed: %s", sport, date_str, exc)
        return []

    if sport == "mlb":
        games = _normalize_mlb_schedule(raw)
    else:
        games = _normalize_wnba_schedule(_normalize_espn_wnba_scoreboard(raw))

    # Write back both caches.  Supabase write uses the "schedule" date
    # sentinel so cache_delete_stale won't purge it on the daily reset.
    try:
        _cache.set(local_key, games)
    except Exception:                                                     # noqa: BLE001
        pass
    try:
        from src import db as _db
        if _db.is_supabase():
            _db.cache_set(
                _schedule_cache_key(sport, date_str),
                sport, "schedule",
                {"date": date_str, "games": games},
            )
    except Exception:                                                     # noqa: BLE001
        pass
    return games


def _team_key(name: str) -> str:
    """Normalize a team name for cross-API matching.  MLB Stats API and
    The Odds API both return the official full name ("Los Angeles
    Dodgers") so a lowercase + whitespace squash is enough in 99% of
    cases.  Returns "" for falsy input so two unknown teams don't
    collide on the empty string."""
    if not name:
        return ""
    return " ".join(str(name).lower().split())


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


# ── No-odds predictions: model output for games The Odds API hasn't priced ───
# Schedule rows flagged with _no_odds normally render as a "No Odds Available"
# placeholder.  We can do better: the trained model only needs team identity
# (+ optional spread/implied prob, both of which default to neutral values) to
# emit ML / RL / totals predictions.  This block lazy-loads the predictor stack
# on first request and reuses it across subsequent calls.  First request after
# a Railway deploy is slow (GameStore.load() touches the API to hydrate the
# season window); subsequent requests within the same boot are cheap.

_no_odds_predictor: dict[str, object] = {"mlb": None, "wnba": None}
# Negative-cache slot so a failed first-load doesn't retry on every render.
_no_odds_predictor_failed: dict[str, bool] = {"mlb": False, "wnba": False}


def _ensure_no_odds_predictor(sport: str):
    """Return (fb, ml_model, rl_or_spread_model, totals_model) for the
    sport, lazy-loading from cached joblib snapshots if not yet built.
    Returns None on any failure (cached so we don't retry every render).
    """
    sport = sport.lower()
    if _no_odds_predictor.get(sport) is not None:
        return _no_odds_predictor[sport]
    if _no_odds_predictor_failed.get(sport):
        return None

    # WNBA: API-Sports requires a paid 2025+ plan, so GameStore.load
    # now falls back to sportsdataverse (Python port of the R wehoop
    # package) and then ESPN's keyless scoreboard endpoint -- both
    # implemented in src/game_store.py.  No special-cased short-circuit
    # here anymore: the load call below will succeed via the fallback
    # chain on free hosts and log which source was used.

    try:
        cfg = SPORTS.get(sport)
        if cfg is None:
            _no_odds_predictor_failed[sport] = True
            return None

        season = int(os.getenv("SEASON", 2025))
        sports_key = os.getenv("API_SPORTS_KEY", "")

        store = GameStore(
            api_key=sports_key,
            base_url=cfg.api_sports_base,
            league_id=cfg.league_id,
            sport_tag=sport,
            cache=_cache,
        )
        _eprint(
            f"NO-ODDS PREDICT [{sport.upper()}]: loading season={season} "
            f"via GameStore (API-Sports primary; WNBA falls back to "
            f"sportsdataverse / ESPN automatically)"
        )
        try:
            n_loaded = store.load(season)
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: GameStore.load failed: {exc}")
            _eprint(traceback.format_exc())
            _no_odds_predictor_failed[sport] = True
            return None
        # If every fallback returned [] we still continue (the cached
        # joblib models may load fine and produce predictions for live
        # games whose teams happen to be in the saved team index), but
        # log the source-exhausted state so the deploy log makes the
        # cause obvious when no predictions come back.
        if n_loaded == 0:
            _eprint(
                f"NO-ODDS PREDICT [{sport.upper()}]: GameStore.load "
                f"returned 0 games -- all data sources exhausted; "
                f"predictor will rely on cached joblib weights only"
            )
        else:
            _eprint(
                f"NO-ODDS PREDICT [{sport.upper()}]: loaded {n_loaded} "
                f"completed games for season {season}"
            )

        if sport == "mlb":
            from src.mlb_features import MLBFeatureBuilder
            fb = MLBFeatureBuilder(store)
        else:
            from src.wnba_features import WNBAFeatureBuilder
            fb = WNBAFeatureBuilder(store)

        # BettingModel + RunLineModel + TotalsModel are imported lazily
        # inside the existing analyze routes (lines ~3941 + ~4305).  We
        # need the same imports here -- the warmup thread + on-demand
        # predict path run BEFORE those routes have ever executed, so
        # without these the call site below raises NameError.
        from src.model           import BettingModel
        from src.run_line_model  import RunLineModel
        from src.totals_model    import TotalsModel

        ml_model = BettingModel(cfg)
        ml_model.train_or_load(stats_client=store, feature_builder=fb,
                               season=season, force_retrain=False)

        rl_model = totals_model = None
        if sport == "mlb":
            try:
                rl_model = RunLineModel()
                rl_model.train_or_load(store, fb, season)
            except Exception as exc:                                      # noqa: BLE001
                _eprint(f"NO-ODDS PREDICT [MLB]: RL load failed: {exc}")
            try:
                totals_model = TotalsModel()
                totals_model.train_or_load(store, fb, season)
            except Exception as exc:                                      # noqa: BLE001
                _eprint(f"NO-ODDS PREDICT [MLB]: totals load failed: {exc}")
        else:
            try:
                from src.wnba_spread_model import WNBASpreadModel
                rl_model = WNBASpreadModel()
                rl_model.train_or_load(store, fb, season)
            except Exception as exc:                                      # noqa: BLE001
                _eprint(f"NO-ODDS PREDICT [WNBA]: spread load failed: {exc}")
            try:
                from src.wnba_totals_model import WNBATotalsModel
                totals_model = WNBATotalsModel()
                totals_model.train_or_load(store, fb, season)
            except Exception as exc:                                      # noqa: BLE001
                _eprint(f"NO-ODDS PREDICT [WNBA]: totals load failed: {exc}")

        _no_odds_predictor[sport] = (fb, ml_model, rl_model, totals_model)
        _eprint(
            f"NO-ODDS PREDICT [{sport.upper()}]: predictor stack ready "
            f"(ml={ml_model.is_trained}, rl={(rl_model.is_trained if rl_model else False)}, "
            f"totals={(totals_model.is_trained if totals_model else False)})"
        )
        return _no_odds_predictor[sport]
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: setup failed: {exc}")
        _no_odds_predictor_failed[sport] = True
        return None


def _predict_no_odds_game(sport: str, g: dict) -> dict | None:
    """Run the model on one no-odds game.  Returns:
        {ml_prob_home, ml_prob_away, rl_pick, rl_prob, rl_line,
         totals_pred, totals_direction, totals_baseline}
    or None when prediction fails (frontend falls back to the
    "No Odds Available" notice in that case).

    No edge / Kelly sizing -- there's no market to compare against.
    Result is NOT recorded in any tracker file since there's nothing
    to settle (no odds means no bet was ever placed)."""
    sport = sport.lower()
    _matchup = f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
    pred = _ensure_no_odds_predictor(sport)
    if pred is None:
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- predictor unavailable")
        return None
    fb, ml_model, rl_model, totals_model = pred
    if not ml_model or not getattr(ml_model, "is_trained", False):
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- ML model not trained")
        return None

    # Inject a neutral implied prob + zero spread so build_for_game
    # doesn't penalize the no-odds game vs ones that came in with
    # market signal.  The model's xgb_input_kind="market_free" head
    # ignores these anyway; this just stops downstream code from
    # KeyError'ing on the optional fields.
    g_for_features = dict(g)
    g_for_features.setdefault("home_implied_prob", 0.5)
    g_for_features.setdefault("spread", 0.0)
    try:
        built = fb.build_for_game(g_for_features)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"build_for_game raised {type(exc).__name__}: {exc}")
        return None
    if built is None:
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"build_for_game returned None (team unresolved or no team stats)")
        return None
    feature_vec, meta = built

    try:
        ml = ml_model.predict(feature_vec, weights=None, game_meta=g_for_features)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"ml predict raised {type(exc).__name__}: {exc}")
        return None
    home_prob = float(ml.get("home_win_prob") or 0.5)

    out = {
        "ml_prob_home": round(home_prob, 4),
        "ml_prob_away": round(1.0 - home_prob, 4),
    }

    # Run line / spread -- neutral line so the model picks its favored side
    # against a market-free baseline.  MLB: -1.5 (the standard run line).
    # WNBA: -2.5 (a typical small spread).  The probability returned is
    # P(home covers); pick_team flips for the underdog branch.
    if rl_model and getattr(rl_model, "is_trained", False):
        try:
            g_rl = dict(g_for_features)
            if sport == "mlb":
                g_rl.setdefault("run_line_point", -1.5)
            else:
                g_rl.setdefault("spread", -2.5)
            rl = rl_model.predict(
                feature_vec, g_rl, weights=None,
                ml_prob_home    = ml.get("xgb_prob"),
                ml_lr_prob_home = ml.get("lr_prob"),
                ml_nn_prob_home = ml.get("nn_prob"),
            )
            if rl:
                out["rl_pick_team"] = rl.get("pick_team") or (
                    g.get("home_team") if rl.get("side") == "home" else g.get("away_team")
                )
                out["rl_pick_side"] = rl.get("side")
                out["rl_prob"]      = round(float(rl.get("pick_prob") or 0.0), 4)
                out["rl_line"]      = -1.5 if sport == "mlb" else -2.5
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: RL predict skipped: {exc}")

    # Totals -- pass a baseline line so the totals model emits a direction
    # (the actual displayed value is the projected raw total).
    if totals_model and getattr(totals_model, "is_trained", False):
        try:
            g_tot = dict(g_for_features)
            baseline = 9.0 if sport == "mlb" else 160.0
            g_tot.setdefault("total_line", baseline)
            g_tot.setdefault("over_odds",  -110)
            g_tot.setdefault("under_odds", -110)
            totals_vec = fb.build_totals_from_meta(meta) if hasattr(fb, "build_totals_from_meta") else None
            if totals_vec is not None:
                tot = totals_model.predict(totals_vec, g_tot, weights=None)
                if tot:
                    out["totals_projected"] = float(tot.get("predicted_total") or 0)
                    out["totals_direction"] = (tot.get("direction") or "").lower()
                    out["totals_baseline"]  = baseline
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: totals predict skipped: {exc}")

    return out


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


# ── No-odds predictions cache ────────────────────────────────────────────────
# Per-game model predictions for the no-odds path, persisted in Supabase
# app_cache so they survive Railway restarts AND so the schedule endpoint
# can serve them without re-running the (slow) GameStore + model load on
# every request.  Midnight reset pre-populates this for the new ET day's
# entire slate; the schedule endpoint also writes back on-demand for any
# game it predicts that isn't in the cache yet.

def _no_odds_predictions_cache_key(sport: str, date_str: str) -> str:
    return f"no_odds_predictions:{sport}:{date_str}"


def _read_no_odds_predictions(sport: str, date_str: str) -> dict[str, dict]:
    """Return the cached {game_id: prediction} dict for one date.
    Empty dict if not cached / read fails."""
    try:
        from src import db as _db
        row = _db.cache_get(_no_odds_predictions_cache_key(sport, date_str))
        if row and isinstance(row.get("data"), dict):
            preds = row["data"].get("predictions")
            if isinstance(preds, dict):
                return preds
    except Exception:                                                     # noqa: BLE001
        pass
    return {}


def _write_no_odds_predictions(sport: str, date_str: str,
                                preds: dict[str, dict]) -> bool:
    """Persist {game_id: prediction} for one date to Supabase.

    Uses the "no_odds" date sentinel so cache_delete_stale (which
    prunes rows where date != today_et) leaves this row alone --
    future-date predictions stay valid as the calendar advances."""
    try:
        from src import db as _db
        if not _db.is_supabase():
            return False
        return bool(_db.cache_set(
            _no_odds_predictions_cache_key(sport, date_str),
            sport, "no_odds",
            {"date": date_str, "predictions": preds},
        ))
    except Exception:                                                     # noqa: BLE001
        return False


def _prefetch_no_odds_predictions(sport: str, date_str: str) -> dict:
    """Run _predict_no_odds_game on every game in the sport's schedule
    for date_str, then persist the {game_id: prediction} map.

    Called on-demand by the schedule endpoint (and previously by the
    midnight reset).  NOTE: the nightly cycle's JOB 3 deliberately does
    NOT call this -- the 3 AM prefetch is schedule-only (no model
    scoring); the real predictions come from the 8 AM analysis run.
    When invoked, it leaves predictions ready for every scheduled game
    so a page load doesn't wait on the GameStore + model load before
    seeing cards with Predicted Winner / Run Line / Projected Total.

    Per-game failures are skipped (logged with the matchup).  An
    empty schedule returns {} without writing to Supabase."""
    games = _fetch_raw_schedule(sport, date_str)
    if not games:
        return {}

    out: dict[str, dict] = {}
    skipped = 0
    for g in games:
        gid = str(g.get("id") or "")
        if not gid:
            skipped += 1
            continue
        try:
            pred = _predict_no_odds_game(sport, g)
        except Exception as exc:                                          # noqa: BLE001
            _eprint(
                f"NO-ODDS PREFETCH [{sport.upper()}] {g.get('away_team','?')} "
                f"@ {g.get('home_team','?')}: {type(exc).__name__}: {exc}"
            )
            skipped += 1
            continue
        if pred is None:
            skipped += 1
            continue
        out[gid] = pred

    if out:
        wrote = _write_no_odds_predictions(sport, date_str, out)
        _eprint(
            f"NO-ODDS PREFETCH [{sport.upper()}] {date_str}: "
            f"predicted {len(out)} game(s), skipped {skipped}, "
            f"supabase_write={wrote}"
        )
    else:
        _eprint(
            f"NO-ODDS PREFETCH [{sport.upper()}] {date_str}: "
            f"0 predictions (skipped {skipped}; predictor may not "
            f"be loadable yet)"
        )
    return out


# ── Live-score debug system ────────────────────────────────────────────────────
# Writes to stdout AND data/debug_live.log so output is readable whether
# the user runs via 'python desktop.pyw' (terminal) or via launch.bat (log file).

_DEBUG_LOG = Path("data/debug_live.log")

def _debug_print(msg: str) -> None:
    """Print to stdout and append to log file with timestamp.  Messages
    are redacted so a leaked URL or env-var secret can't end up in the
    debug log file or stdout."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {_redact(msg)}"
    _logger.debug("%s", line)
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
                except Exception:
                    pass
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
            except Exception:
                pass

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
            except Exception:
                pass

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

_analysis_progress: dict[str, dict] = {
    "mlb":  {
        "running": False, "step": "", "step_num": 0, "total_steps": 8,
        "started_at": None, "completed_at": None,
        "error": None, "n_games": None,
    },
    "wnba": {
        "running": False, "step": "", "step_num": 0, "total_steps": 8,
        "started_at": None, "completed_at": None,
        "error": None, "n_games": None,
    },
}
_analysis_progress_lock = _threading_for_analyze.Lock()


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
    season     = int(data.get("season", int(os.getenv("SEASON", 2025))))
    games_lim  = int(data.get("games", 0))

    odds_key   = os.getenv("ODDS_API_KEY", "")
    sports_key = os.getenv("API_SPORTS_KEY", "")

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
            except Exception:
                pass
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
    except Exception:
        pass

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
    season   = int(data.get("season", int(os.getenv("SEASON", 2025))))

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
            except Exception:
                pass

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


_PICKS_HISTORY_FILES = (
    Path(".cache/xgb_picks_history.json"),
    Path(".cache/lr_picks_history.json"),
    Path("data/nn_picks_history.json"),
    Path(".cache/props_picks_history.json"),
)
_ENSEMBLE_PICKS_FILE = Path("data/ensemble_picks_today.json")
_DAILY_PICKS_FILE    = Path("data/daily_picks.json")
_BET_HISTORY_ARCHIVE = Path("data/bet_history_archive.json")


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
    """Update per-sport auto-pick toggles.  Body: {mlb_enabled, wnba_enabled}."""
    try:
        body = request.json or {}
        # Merge so the caller can send a single field at a time
        current = _load_model_settings()
        if "mlb_enabled"  in body: current["mlb_enabled"]  = bool(body["mlb_enabled"])
        if "wnba_enabled" in body: current["wnba_enabled"] = bool(body["wnba_enabled"])
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
        except Exception:
            pass

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


def _match_result_id(r: dict, game_id: str) -> bool:
    """True when *r* identifies the analysis result for *game_id*,
    regardless of whether it's a raw nested dict (r["game"]["id"]) or
    a flat serialized passthrough (r["game_id"] / r["id"] /
    r["_schedule_id"]).  Centralized so every /api/ledger/* +
    /api/ai/pick_analysis route can match the same way -- the bare
    r["game"]["id"] form raised KeyError("game") whenever results were
    hydrated from the daily snapshot's flat shape.
    """
    if not isinstance(r, dict):
        return False
    g_id = (r.get("game") or {}).get("id") if isinstance(r.get("game"), dict) else None
    return (
        g_id == game_id
        or r.get("game_id") == game_id
        or r.get("id") == game_id
        or r.get("_schedule_id") == game_id
    )


def _find_analysis_row(state: dict, game_id: str) -> dict | None:
    """Locate the analysis row for *game_id* in *state* and return it
    normalized to the nested shape downstream routes expect.

    When the matched row is already nested (has r["game"] and
    r["prediction"]) we return it untouched.  When it's a flat
    serialized passthrough (snapshot hydration path) we synthesize
    minimal `game` and `prediction` sub-dicts from the flat fields so
    code that does `raw["game"]["home_team"]` keeps working.  Without
    this, every /api/ledger/* call on a snapshot-hydrated worker
    crashed with KeyError('game').
    """
    results = (state or {}).get("results") or []
    raw = next((r for r in results if _match_result_id(r, game_id)), None)
    if raw is None:
        return None
    # Already in the nested raw shape -- pass through untouched.
    if isinstance(raw.get("game"), dict) and isinstance(raw.get("prediction"), dict):
        return raw
    # Flat passthrough: rebuild the minimal nested view from top-level
    # serialized fields so the rest of the route can continue.  Copy
    # rather than mutate so we don't poison the in-memory cache for
    # other readers.
    out = dict(raw)
    if not isinstance(out.get("game"), dict):
        # Re-derive home_implied_prob from the away_odds + home_odds
        # pair when we have them; the route uses it for edge math.
        home_odds = raw.get("home_odds")
        away_odds = raw.get("away_odds")
        implied = raw.get("home_implied_prob")
        if implied is None and isinstance(home_odds, (int, float)) \
                and isinstance(away_odds, (int, float)):
            try:
                ho = _american_to_prob(int(home_odds))
                ao = _american_to_prob(int(away_odds))
                if ho + ao > 0:
                    implied = ho / (ho + ao)
            except Exception:                                              # noqa: BLE001
                implied = None
        out["game"] = {
            "id":                raw.get("game_id") or raw.get("id"),
            "home_team":         raw.get("home_team"),
            "away_team":         raw.get("away_team"),
            "commence_time":     raw.get("commence_time"),
            "h2h_home_odds":     home_odds,
            "h2h_away_odds":     away_odds,
            "home_implied_prob": implied if implied is not None else 0.5,
            "total_line":        (raw.get("totals") or {}).get("total_line"),
        }
    if not isinstance(out.get("prediction"), dict):
        # Best-effort: derive home_win_prob from the moneyline pick
        # fields the serializer left at the top level.
        pick_team  = raw.get("pick_team")
        pick_prob  = raw.get("pick_prob")
        home_team  = raw.get("home_team")
        if isinstance(pick_prob, (int, float)) and pick_team and home_team:
            picked_home = pick_team == home_team
            home_win = float(pick_prob) if picked_home else 1.0 - float(pick_prob)
        else:
            home_win = 0.5
        out["prediction"] = {"home_win_prob": home_win}
    return out


def _american_to_prob(american: int) -> float:
    """American moneyline -> raw implied probability (0-1).  Local mirror
    of odds_client._american_to_prob so the helper above doesn't need
    to import the larger module."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


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


def _persist_daily_budget(bankroll: float) -> dict:
    """Recompute the daily budget off *bankroll* and persist it for today
    (ET) so the My Bets banner + budget gate reflect a bankroll change
    immediately.  Returns the budget dict."""
    from src.ledger import compute_daily_budget
    budget = compute_daily_budget(bankroll)
    try:
        from src import db as _db
        if _db.is_supabase():
            _db.cache_set("daily_budget", None, _today_et(), budget)
    except Exception as _e:                                                # noqa: BLE001
        _eprint(f"daily budget persist failed: {_e}")
    return budget


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

    pred_full = raw["prediction"]
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
    except Exception:
        pass

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
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
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
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
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


def _fmt_odds(o) -> str:
    """+150 / -110 style.  '?' when missing / unparseable."""
    if o is None or o == "":
        return "?"
    try:
        n = int(o)
    except (TypeError, ValueError):
        return str(o)
    return f"+{n}" if n > 0 else str(n)


def _fmt_pct(p) -> str:
    try:
        return f"{float(p) * 100:.1f}%"
    except (TypeError, ValueError):
        return "?"


def _pitcher_block_for_ai(sp: dict, side: str) -> str:
    """One pitcher's stat lines for the AI context.  Tolerant of the
    pitcher_client output shape (full_name / team_abbrev / era / whip /
    k_per_9 / bb9 / era_home / era_away / last3_era / wins / losses /
    rest / hand).  Missing fields render '?'."""
    if not isinstance(sp, dict) or not sp:
        return f"{side} SP: (no probable starter)"
    def _f(key, fmt: str) -> str:
        v = sp.get(key)
        if v is None or v == "":
            return "?"
        try:
            return fmt.format(float(v))
        except (TypeError, ValueError):
            return str(v)
    name = (sp.get("full_name") or "TBD").strip()
    team = (sp.get("team_abbrev") or "?").strip().upper()
    hand = sp.get("hand")
    hand_s = (
        "LHP" if hand == 1 or str(hand).upper() == "LHP"
        else "RHP" if hand == 0 or str(hand).upper() == "RHP"
        else "?"
    )
    wins   = int(sp.get("wins")   or 0)
    losses = int(sp.get("losses") or 0)
    record = f"{wins}-{losses}" if (wins or losses) else "?"
    return (
        f"{side} SP: {name} ({team}, {hand_s}, {record})  "
        f"ERA {_f('era', '{:.2f}')}  "
        f"WHIP {_f('whip', '{:.2f}')}  "
        f"K/9 {_f('k_per_9', '{:.1f}')}  "
        f"BB/9 {_f('bb9', '{:.1f}')}  "
        f"Home ERA {_f('era_home', '{:.2f}')}  "
        f"Away ERA {_f('era_away', '{:.2f}')}  "
        f"Last 3 ERA {_f('last3_era', '{:.2f}')}  "
        f"Rest {_f('rest', '{:.0f}')}d"
    )


def _resolve_pitcher_data_for_ai(raw: dict, sport: str) -> tuple[dict, dict]:
    """Best-effort pitcher dict resolution for the AI payload.
    Preference order: raw meta -> serialized passthrough top-level ->
    direct pitcher_client fetch (snapshot-hydrated path, MLB only).
    Returns (home_sp, away_sp) -- empty dicts when nothing resolves."""
    meta = raw.get("meta") or {}
    home_sp = meta.get("home_sp") or raw.get("home_sp") or {}
    away_sp = meta.get("away_sp") or raw.get("away_sp") or {}
    if (home_sp and away_sp) or sport != "mlb":
        return home_sp, away_sp
    # Fall back to pitcher_client direct fetch -- same path the matchup
    # page uses (PR #88) for snapshot-hydrated rows that lack meta.
    game = raw.get("game") or {}
    home = game.get("home_team") or raw.get("home_team") or ""
    away = game.get("away_team") or raw.get("away_team") or ""
    commence = game.get("commence_time") or raw.get("commence_time") or ""
    if not (home and away):
        return home_sp, away_sp
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        game_date = ""
        if commence:
            try:
                dt = _dt.fromisoformat(str(commence).replace("Z", "+00:00"))
                game_date = dt.astimezone(_Z("America/New_York")).date().isoformat()
            except Exception:                                              # noqa: BLE001
                pass
        from src.pitcher_client import get_pitcher_client
        data = get_pitcher_client().get_starters_for_game(
            home, away, game_date, commence_time=commence)
        return (
            (data or {}).get("home") or home_sp,
            (data or {}).get("away") or away_sp,
        )
    except Exception:                                                     # noqa: BLE001
        return home_sp, away_sp


def _build_pick_analysis_context(raw: dict, bet_type: str, sport: str) -> str:
    """Build the rich per-pick context fed to the Analyze button.

    The AI now sees a full data card: matchup + lines, both pitchers'
    stats and splits, all three model picks (ML / RL or Spread / Totals),
    per-model probabilities (XGB / LR / NN), the requested bet's edge
    + Kelly size + top SHAP factors with their actual numeric values,
    and the upset risk score.  Designed to support specific, opinionated
    output rather than generic "the model favors the home team" copy.
    """
    game = raw.get("game") or {}
    pred = raw.get("prediction") or {}
    meta = raw.get("meta") or {}
    away = game.get("away_team", "Away")
    home = game.get("home_team", "Home")
    commence_time = game.get("commence_time", "") or raw.get("commence_time", "")

    # ── Market lines (h2h moneyline + run line / spread + totals) ─────
    ml_home_odds = game.get("h2h_home_odds")
    ml_away_odds = game.get("h2h_away_odds")
    rl_pred      = raw.get("rl_pred") or raw.get("spread_pred") or {}
    totals_pred  = raw.get("totals_pred") or {}
    rl_line      = rl_pred.get("run_line_point") or rl_pred.get("spread_line")
    totals_line  = totals_pred.get("total_line")

    # ── All three picks (ML / RL or Spread / Totals) with confidences ─
    hp = float(pred.get("home_win_prob") or 0.5)
    market_p = float(game.get("home_implied_prob") or 0.5)
    if hp >= 0.5:
        ml_pick_team, ml_pick_prob, ml_edge = home, hp, hp - market_p
        ml_pick_odds = ml_home_odds
    else:
        ml_pick_team, ml_pick_prob, ml_edge = away, 1 - hp, (1 - hp) - (1 - market_p)
        ml_pick_odds = ml_away_odds

    rl_pick_team = rl_pred.get("pick_team") or "?"
    rl_pick_prob = float(rl_pred.get("pick_prob") or 0)
    rl_edge      = float(rl_pred.get("edge") or 0)
    rl_pick_odds = rl_pred.get("pick_odds")

    tot_dir   = (totals_pred.get("direction") or "over").title()
    tot_pick  = f"{tot_dir} {totals_line}" if totals_line is not None else "?"
    tot_prob  = float(totals_pred.get("pick_prob") or 0)
    tot_edge  = float(totals_pred.get("edge") or 0)
    tot_odds  = (
        totals_pred.get("over_odds") if tot_dir.lower() == "over"
        else totals_pred.get("under_odds")
    )

    # ── Per-model probabilities (XGB / LR / NN) ───────────────────────
    xgb_p = pred.get("xgb_prob")
    lr_p  = pred.get("lr_prob")
    nn_p  = pred.get("nn_prob")
    models_agree = bool(pred.get("models_agree", True))

    # ── Which bet did the user click Analyze on?  Mark it + pull SHAP ─
    if bet_type in ("moneyline", "single", "ml"):
        focus_label = "Moneyline"
        focus_pick  = ml_pick_team
        focus_prob  = ml_pick_prob
        focus_edge  = ml_edge
        focus_odds  = ml_pick_odds
        focus_shap  = pred.get("shap") or []
    elif bet_type in ("run_line", "spread"):
        focus_label = "Run Line" if sport == "mlb" else "Spread"
        line_s = f" {float(rl_line):+g}" if isinstance(rl_line, (int, float)) else ""
        focus_pick = f"{rl_pick_team}{line_s}"
        focus_prob = rl_pick_prob
        focus_edge = rl_edge
        focus_odds = rl_pick_odds
        focus_shap = rl_pred.get("shap") or []
    elif bet_type == "totals":
        focus_label = "Totals"
        focus_pick  = tot_pick
        focus_prob  = tot_prob
        focus_edge  = tot_edge
        focus_odds  = tot_odds
        focus_shap  = totals_pred.get("shap") or []
    else:
        focus_label = bet_type.title()
        focus_pick  = "?"
        focus_prob  = 0.0
        focus_edge  = 0.0
        focus_odds  = None
        focus_shap  = []

    # ── SHAP top 5 with actual numeric values ─────────────────────────
    shap_lines: list[str] = []
    for s in (focus_shap or [])[:5]:
        try:
            label = (
                s.get("label")
                or _FEATURE_LABELS.get(s.get("feature", ""), s.get("feature", "factor"))
            )
            shap_val = float(s.get("shap_value") or 0)
            direction = "+" if shap_val >= 0 else ""
            shap_lines.append(
                f"  - {label}: {direction}{shap_val:.3f} "
                f"({'supports' if shap_val >= 0 else 'argues against'} the pick)"
            )
        except Exception:                                                 # noqa: BLE001
            continue
    shap_block = "\n".join(shap_lines) if shap_lines else "  (none recorded)"

    # ── Kelly / bet sizing -- pull from whichever shape carried it ────
    kelly = (
        meta.get("model_amount")
        or raw.get("bet_dollars")
        or (rl_pred.get("bet_dollars") if bet_type in ("run_line", "spread") else None)
        or (totals_pred.get("bet_dollars") if bet_type == "totals" else None)
    )
    kelly_s = f"${float(kelly):.2f}" if isinstance(kelly, (int, float)) else "?"

    # ── Upset risk ────────────────────────────────────────────────────
    upset = raw.get("upset") or {}
    upset_score = upset.get("score")
    upset_s = f"{float(upset_score):.0f}/10" if isinstance(upset_score, (int, float)) else "?"

    # ── Pitchers (MLB only; WNBA falls through to "no SP data") ───────
    if sport == "mlb":
        home_sp, away_sp = _resolve_pitcher_data_for_ai(raw, sport)
        pitching_block = (
            f"{_pitcher_block_for_ai(away_sp, 'AWAY')}\n"
            f"{_pitcher_block_for_ai(home_sp, 'HOME')}"
        )
    else:
        pitching_block = "(WNBA -- no starting pitcher data)"

    rl_line_s = f"{float(rl_line):+g}" if isinstance(rl_line, (int, float)) else "?"

    return (
        f"=== MATCHUP ===\n"
        f"Sport: {sport.upper()}\n"
        f"Game: {away} @ {home}\n"
        f"Start: {commence_time or '?'}\n"
        f"\n=== STARTING PITCHERS ===\n"
        f"{pitching_block}\n"
        f"\n=== MARKET LINES ===\n"
        f"Moneyline: {away} {_fmt_odds(ml_away_odds)} / {home} {_fmt_odds(ml_home_odds)}\n"
        f"Run Line: home {rl_line_s} at {_fmt_odds(rl_pred.get('run_line_home_odds') or rl_pred.get('pick_odds'))}, "
        f"away {(-float(rl_line)) if isinstance(rl_line, (int, float)) else '?'} "
        f"at {_fmt_odds(rl_pred.get('run_line_away_odds') or rl_pred.get('pick_odds'))}\n"
        f"Totals: O {totals_line if totals_line is not None else '?'} "
        f"at {_fmt_odds(totals_pred.get('over_odds'))} / "
        f"U {totals_line if totals_line is not None else '?'} "
        f"at {_fmt_odds(totals_pred.get('under_odds'))}\n"
        f"\n=== ALL MODEL PICKS ===\n"
        f"Moneyline: {ml_pick_team} @ {_fmt_odds(ml_pick_odds)}  "
        f"conf={_fmt_pct(ml_pick_prob)}  edge={ml_edge * 100:+.1f}%\n"
        f"Run Line/Spread: {rl_pick_team} {rl_line_s} @ {_fmt_odds(rl_pick_odds)}  "
        f"conf={_fmt_pct(rl_pick_prob)}  edge={rl_edge * 100:+.1f}%\n"
        f"Totals: {tot_pick} @ {_fmt_odds(tot_odds)}  "
        f"conf={_fmt_pct(tot_prob)}  edge={tot_edge * 100:+.1f}%\n"
        f"\n=== PER-MODEL HOME WIN PROBABILITY ===\n"
        f"XGB: {_fmt_pct(xgb_p)}   LR: {_fmt_pct(lr_p)}   NN: {_fmt_pct(nn_p)}\n"
        f"Models agree: {'YES' if models_agree else 'NO -- ensemble split'}\n"
        f"\n=== FOCUS BET (user clicked Analyze on this) ===\n"
        f"Type: {focus_label}\n"
        f"Pick: {focus_pick} @ {_fmt_odds(focus_odds)}\n"
        f"Confidence: {_fmt_pct(focus_prob)}\n"
        f"Edge over market: {focus_edge * 100:+.1f}%\n"
        f"Half-Kelly bet size: {kelly_s}\n"
        f"\n=== TOP SHAP FACTORS (with values) ===\n"
        f"{shap_block}\n"
        f"\n=== UPSET / CHAOS SCORE ===\n"
        f"{upset_s} (higher = more unpredictable matchup)"
    )


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

    odds_key   = os.getenv("ODDS_API_KEY", "")
    sports_key = os.getenv("API_SPORTS_KEY", "")  # optional for WNBA (ESPN used instead)

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
            except Exception:
                pass
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
    except Exception:
        pass

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
                except Exception:
                    pass
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
            except Exception:
                pass

        if not _WNBA_ANALYSIS_CACHE_FILE.exists():
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})
        payload = json.loads(_WNBA_ANALYSIS_CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("date") != today:
            return jsonify({"has_predictions": False, "analyzed_at": _saved_at})

        _at = payload.get("analyzed_at") or _saved_at
        if _at and _wnba_analysis_state.get("last_analyzed_at") is None:
            try:
                _wnba_analysis_state["last_analyzed_at"] = datetime.fromisoformat(_at)
            except Exception:
                pass

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


def _run_meta_consensus_job() -> dict:
    """APScheduler 8:30 AM ET job: one batched compound-beta review of today's
    scored props -> meta_consensus_today cache.  Best-effort; never raises."""
    try:
        from services import meta_consensus
        res = meta_consensus.run_meta_consensus()
        _eprint(
            f"META-CONSENSUS: done -- parsed={res.get('parsed', 0)}/"
            f"{res.get('prop_count', 0)} (model={res.get('model')})"
        )
        return res
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"META-CONSENSUS: job failed: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}")
        return {"error": f"{type(exc).__name__}: {exc}"}


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

    season = int(os.getenv("SEASON", 2025))

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


def _run_personal_daily_limit_refresh() -> None:
    """4 AM ET: take a fresh My Bets daily-limit snapshot off the current
    personal bankroll that morning (higher if the bankroll grew, lower if
    it shrank).  Sizes NEW bets only -- never an already-placed stake."""
    try:
        from src import supa_ledger as _sl
        limit = _sl.personal().refresh_daily_limit()
        _eprint(f"DAILY-LIMIT [personal]: refreshed to ${limit:.2f} "
                f"(20% of current bankroll)")
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"DAILY-LIMIT refresh failed: {type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  Nightly three-job cycle (replaces the old single midnight_reset):
#
#    JOB 1  1:00 AM ET  final settlement   -- last-chance settle pass
#    JOB 2  2:00 AM ET  full clear         -- wipe the day to a clean slate
#    JOB 3  3:00 AM ET  games prefetch     -- schedule-only stub cards
#
#  The 8 AM auto-analysis job then runs as normal against the freshly
#  prefetched schedule.
# ─────────────────────────────────────────────────────────────────────────────

def _run_job1_final_settlement() -> None:
    """JOB 1 (1:00 AM ET) -- final settlement pass.

    Runs the full settlement job in forced mode so it bypasses the
    game-hours gate and catches anything the day's 30-minute
    auto-settlement runs missed: open game picks, open prop picks,
    postponed-game voids.  Each settle updates the model bankroll +
    W/L records + Supabase via the existing Ledger / props_ledger
    settle paths.

    Logs a one-line summary: game picks settled, prop picks settled,
    and total realized model P/L for the day.
    """
    _eprint("NIGHTLY JOB 1 (final settlement): starting forced settlement pass")
    try:
        result = _run_auto_settlement_job(force=True) or {}
        _eprint(
            "NIGHTLY JOB 1 (final settlement): COMPLETE -- "
            f"game_picks_settled={result.get('settled', 0)} "
            f"({result.get('wins', 0)}W/{result.get('losses', 0)}L, "
            f"{result.get('voided', 0)} voided)  "
            f"prop_picks_settled={result.get('props_settled', 0)}  "
            f"total_model_pnl=${result.get('total_pnl', 0.0):+.2f} "
            f"(game ${result.get('game_pnl', 0.0):+.2f} / "
            f"props ${result.get('props_pnl', 0.0):+.2f})"
        )
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"NIGHTLY JOB 1 (final settlement): FAILED: "
                f"{type(_exc).__name__}: {_exc}\n{traceback.format_exc()}")


def _run_job2_full_clear() -> None:
    """JOB 2 (2:00 AM ET) -- clear the website completely.

    Resets every layer that holds today's picks / props / analysis so
    the site looks like a brand-new day with nothing loaded:

      * data/ensemble_picks_today.json, data/daily_picks.json (disk)
      * analysis caches + daily snapshot + timestamps (disk)
      * in-memory MLB + WNBA analysis state
      * the props scored cache (local + Supabase)
      * the raw props-lines cache (local + Supabase)
      * Supabase app_cache rows for snapshot / analysis / daily_picks

    Deliberately does NOT prefetch schedules or run any model scoring
    -- that's JOB 3's responsibility, an hour later, so the clear is a
    clean, fast, side-effect-free wipe.
    """
    _eprint("NIGHTLY JOB 2 (full clear): clearing all game + prop data")
    try:
        # ── Disk caches ──────────────────────────────────────────────────
        for _path in (_ANALYSIS_CACHE_FILE, _WNBA_ANALYSIS_CACHE_FILE):
            try:
                _path.unlink(missing_ok=True)
            except Exception:                                             # noqa: BLE001
                pass
        with _snapshot_lock:
            try:
                _DAILY_SNAPSHOT_FILE.unlink(missing_ok=True)
                _DAILY_SNAPSHOT_TMP.unlink(missing_ok=True)
            except Exception:                                             # noqa: BLE001
                pass
        try:
            _ANALYSIS_TIMESTAMPS_FILE.unlink(missing_ok=True)
        except Exception:                                                 # noqa: BLE001
            pass
        for _picks_path in (_ENSEMBLE_PICKS_FILE, _DAILY_PICKS_FILE):
            try:
                _picks_path.unlink(missing_ok=True)
            except Exception:                                             # noqa: BLE001
                pass

        # ── In-memory state ──────────────────────────────────────────────
        try:
            ensemble_store.load()
        except Exception:                                                 # noqa: BLE001
            pass
        _analysis_state["results"]            = []
        _analysis_state["parlays"]            = {}
        _analysis_state["last_analyzed_at"]   = None
        _analysis_state["last_analysis_meta"] = {}
        _wnba_analysis_state["results"]            = []
        _wnba_analysis_state["parlays"]            = {}
        _wnba_analysis_state["last_analyzed_at"]   = None
        _wnba_analysis_state["last_analysis_meta"] = {}

        # ── Odds cache files ─────────────────────────────────────────────
        try:
            from pathlib import Path as _P
            _odds_wiped = 0
            for _f in _P(".cache").glob("odds_*.json"):
                try:
                    _f.unlink()
                    _odds_wiped += 1
                except Exception:                                          # noqa: BLE001
                    pass
            _eprint(f"NIGHTLY JOB 2 (full clear): wiped {_odds_wiped} odds cache file(s)")
        except Exception as _e:                                            # noqa: BLE001
            _eprint(f"NIGHTLY JOB 2 (full clear): odds cache wipe error: {_e}")

        # ── Props caches (scored + raw lines) ────────────────────────────
        try:
            from src.props_scored_cache import clear_scored_props
            _ps = clear_scored_props()
            _eprint(f"NIGHTLY JOB 2 (full clear): props scored cache cleared {_ps}")
        except Exception as _e:                                            # noqa: BLE001
            _eprint(f"NIGHTLY JOB 2 (full clear): props scored clear error: {_e}")
        try:
            from pathlib import Path as _P
            _props_raw_wiped = 0
            for _f in _P(".cache").glob("props_mlb_*.json"):
                try:
                    _f.unlink()
                    _props_raw_wiped += 1
                except Exception:                                          # noqa: BLE001
                    pass
            # Supabase raw-props row for today
            try:
                from src import db as _db
                if _db.is_supabase():
                    _db.cache_delete(f"props_mlb_{_today_et()}")
            except Exception:                                              # noqa: BLE001
                pass
            _eprint(f"NIGHTLY JOB 2 (full clear): wiped {_props_raw_wiped} raw-props cache file(s)")
        except Exception as _e:                                            # noqa: BLE001
            _eprint(f"NIGHTLY JOB 2 (full clear): raw-props clear error: {_e}")

        # ── Supabase app_cache rows ──────────────────────────────────────
        for _ckey in (_CACHE_KEY_SNAPSHOT, _CACHE_KEY_ANALYSIS_MLB, _CACHE_KEY_ANALYSIS_WNBA):
            _supabase_cache_delete(_ckey)
        try:
            from src import db as _db
            if _db.is_supabase():
                _db.cache_delete("daily_picks")
        except Exception:                                                  # noqa: BLE001
            pass

        # ── Daily bet budget (FIX 4) ─────────────────────────────────────
        # Compute tomorrow's conservative budget off the current personal
        # bankroll and persist it to Supabase so the My Bets page shows a
        # fixed budget for the whole ET day (recomputed here every 2 AM).
        try:
            from src.ledger import Ledger as _BudgetLedger, compute_daily_budget
            _bl = _BudgetLedger(path="data/ledger.json", starting_bankroll=1000.0)
            _bankroll = float(
                _bl.data.get("personal_bankroll")
                or _bl.data.get("personal_starting_bankroll")
                or 0.0
            )
            _budget = compute_daily_budget(_bankroll)
            from src import db as _db
            if _db.is_supabase():
                _db.cache_set("daily_budget", None, _today_et(), _budget)
            _eprint(f"NIGHTLY JOB 2 (full clear): daily budget {_budget}")
        except Exception as _e:                                            # noqa: BLE001
            _eprint(f"NIGHTLY JOB 2 (full clear): daily budget error: {_e}")

        _eprint("NIGHTLY JOB 2 (full clear): COMPLETE -- site is a clean slate")
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"NIGHTLY JOB 2 (full clear): FAILED: "
                f"{type(_exc).__name__}: {_exc}\n{traceback.format_exc()}")


def _run_job3_games_prefetch() -> None:
    """JOB 3 (3:00 AM ET) -- prefetch today's game schedule only.

    Fetches just the matchup + start time for today's games (both
    sports) into the schedule cache so the home + sports pages can
    render stub cards labelled "Analysis pending" when the user wakes
    up.  Deliberately NO odds, NO model scoring, NO no-odds
    predictions -- this is the cheap "something to look at" pass.

    The 8 AM auto-analysis job runs the real, expensive analysis
    afterwards.
    """
    _eprint("NIGHTLY JOB 3 (games prefetch): fetching today's schedule (no odds, no scoring)")
    try:
        today_str = _today_et()
        for _sport in ("mlb", "wnba"):
            try:
                games = _fetch_raw_schedule(_sport, today_str)
                _eprint(
                    f"NIGHTLY JOB 3 (games prefetch): {_sport} -> "
                    f"{len(games)} game(s) cached for {today_str}"
                )
            except Exception as _e:                                       # noqa: BLE001
                _eprint(
                    f"NIGHTLY JOB 3 (games prefetch): {_sport} FAILED: "
                    f"{type(_e).__name__}: {_e}"
                )
        _eprint("NIGHTLY JOB 3 (games prefetch): COMPLETE -- stub cards ready for the morning")
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"NIGHTLY JOB 3 (games prefetch): FAILED: "
                f"{type(_exc).__name__}: {_exc}\n{traceback.format_exc()}")

    # Recompute prop-market player-similarity clusters from the rolling
    # snapshots (refreshed by the nightly training run earlier).  Cheap
    # numpy distance work over the cached snapshots -- no API calls.
    try:
        from src.player_similarity import recompute_clusters
        res = recompute_clusters()
        _eprint(f"NIGHTLY JOB 3 (similarity): recompute -> {res.get('summary') or res}")
    except Exception as _se:                                              # noqa: BLE001
        _eprint(f"NIGHTLY JOB 3 (similarity): FAILED: "
                f"{type(_se).__name__}: {_se}")


def _run_overnight_ai_gen() -> None:
    """3:30 AM ET -- two-pass overnight AI pre-generation so every pick has a
    breakdown before the user wakes.  Runs right after JOB 3 (3 AM prefetch).

    Pass 1: game breakdowns on 70B (V3) + ALL props on 8B (V2).
    Pass 2: top agreeing, high-confidence props re-run on 70B (V3).
    Budget-aware cascading + spacing live in src/groq_models.  Daytime only
    re-runs on line movement after this."""
    _eprint("OVERNIGHT AI: starting two-pass pre-generation (V3 games + V2 props, "
            "then V3 top props)")
    try:
        hydrate_state()
    except Exception as _he:                                              # noqa: BLE001
        _eprint(f"OVERNIGHT AI: hydrate failed: {_he}")
    try:
        from src import ai_summaries
        game_results = (
            [("mlb",  r) for r in (_analysis_state.get("results") or [])]
            + [("wnba", r) for r in (_wnba_analysis_state.get("results") or [])]
        )
        summary = ai_summaries.run_overnight_generation(game_results)
        _eprint(f"OVERNIGHT AI: done -- {summary}")
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"OVERNIGHT AI: FAILED: {type(_exc).__name__}: {_exc}\n"
                f"{traceback.format_exc()}")


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


# ── Auto-settlement helpers ───────────────────────────────────────────────────
_MLB_TEAM_NORM = {
    "Oakland Athletics": "Athletics",
    "Arizona Diamondbacks": "Diamondbacks",
    "Tampa Bay Rays": "Rays",
}

def _norm_team_name(name: str) -> str:
    return _MLB_TEAM_NORM.get(name, name).strip().lower()


def _void_postponed_mlb_bets() -> list:
    """
    Check MLB Stats API for postponed games today. For each open MLB bet
    matching a postponed game, void it (return stake, result='void').
    Returns list of voided bet entries.
    """
    voided: list = []
    try:
        date_str = _today_et_str()
        live_map = _fetch_mlb_linescore_raw(date_str)
    except Exception as _e:
        _eprint(f"AUTO-SETTLE: could not fetch MLB linescore for postponed check: {_e}")
        return voided

    # Collect postponed matchups as normalised (away, home) tuples
    postponed: list = []
    for _pk, _g in live_map.items():
        try:
            detail = _g.get("status", {}).get("detailedState", "")
            if detail == "Postponed":
                away = _g["teams"]["away"]["team"]["name"]
                home = _g["teams"]["home"]["team"]["name"]
                postponed.append((_norm_team_name(away), _norm_team_name(home)))
        except Exception:
            continue

    if not postponed:
        return voided

    # Load MLB ledger and void matching open bets
    try:
        _ldr = Ledger(path="data/ledger.json", starting_bankroll=250)
    except Exception as _e:
        _eprint(f"AUTO-SETTLE: could not load MLB ledger for void: {_e}")
        return voided

    remaining: list = []
    changed = False
    for bet in _ldr.data.get("open_bets", []):
        b_away = _norm_team_name(bet.get("away_team", ""))
        b_home = _norm_team_name(bet.get("home_team", ""))
        is_postponed = any(
            (b_away in pa or pa in b_away) and (b_home in ph or ph in b_home)
            for pa, ph in postponed
        )
        if is_postponed:
            # Return stake to both bankrolls
            model_amt = bet.get("model_amount", 0.0)
            conf_amt  = bet.get("confirmed_amount", 0.0)
            limit_hit = bet.get("limit_reached", False)
            if not limit_hit:
                if model_amt > 0:
                    _ldr.data["model_bankroll"] = round(
                        _ldr.data["model_bankroll"] + model_amt, 2)
                if bet.get("confirmed") and conf_amt > 0:
                    _ldr.data["personal_bankroll"] = round(
                        _ldr.data["personal_bankroll"] + conf_amt, 2)
            voided_entry = {
                **bet,
                "result":        "void",
                "model_pnl":     0.0,
                "confirmed_pnl": 0.0,
                "settled_at":    datetime.now(timezone.utc).isoformat(),
                "void_reason":   "postponed",
            }
            _ldr.data.setdefault("history", []).append(voided_entry)
            voided.append(voided_entry)
            changed = True
        else:
            remaining.append(bet)

    if changed:
        _ldr.data["open_bets"] = remaining
        _ldr.save()
    return voided


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
    odds_key = os.getenv("ODDS_API_KEY", "")
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


def _completed_games_from_scores(scores: list) -> list[dict]:
    """Normalize Odds API score rows into {id, home_team, away_team,
    home_score, away_score, total_runs, game_date} for the tracker grader.
    Only completed games with both scores are returned."""
    out: list[dict] = []
    for s in (scores or []):
        if not isinstance(s, dict) or not s.get("completed"):
            continue
        gid = str(s.get("id") or "")
        ht, at = s.get("home_team"), s.get("away_team")
        hs = as_ = None
        for nm in (s.get("scores") or []):
            if not isinstance(nm, dict):
                continue
            try:
                sc = int(nm.get("score"))
            except (TypeError, ValueError):
                continue
            if nm.get("name") == ht:
                hs = sc
            elif nm.get("name") == at:
                as_ = sc
        if hs is None or as_ is None:
            continue
        out.append({
            "id":         gid,
            "home_team":  ht,
            "away_team":  at,
            "home_score": hs,
            "away_score": as_,
            "total_runs": hs + as_,
            "game_date":  (s.get("commence_time") or "")[:10],
        })
    return out


def _grade_model_trackers(oc, sport_keys: list[str], scores_by_sport=None) -> dict:
    """Grade all pending XGB/LR/NN tracker picks against completed games.
    Returns {'xgb': n, 'lr': n, 'nn': n} newly graded.

    *scores_by_sport* may be a {sport_key: [score rows]} map pre-fetched
    earlier in the same cycle; when present it's reused instead of calling
    get_scores again (avoids a duplicate Odds API call)."""
    graded = {"xgb": 0, "lr": 0, "nn": 0}
    for sk in sport_keys:
        if scores_by_sport is not None and sk in scores_by_sport:
            scores = scores_by_sport.get(sk) or []
        else:
            try:
                scores = oc.get_scores(sport_key=sk, days_from=3) or []
            except Exception:                                               # noqa: BLE001
                continue
        games = _completed_games_from_scores(scores)
        if not games:
            continue
        try:
            from src import xgb_picks_tracker as _xgb
            graded["xgb"] += _xgb.settle_picks(games)
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE xgb error: {_e}")
        try:
            from src import lr_picks_tracker as _lr
            for g in games:
                graded["lr"] += _lr.settle_lr_pick(g["id"], g["home_score"], g["away_score"])
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE lr error: {_e}")
        try:
            from src import nn_picks as _nn
            graded["nn"] += _nn.settle_completed_games(games)
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE nn error: {_e}")
    return graded


def _log_model_picks() -> None:
    """Log every individual model's current picks (+ ensemble + consensus)
    to the model_picks table.  Deduped per model/game/day, so safe to call
    on every analysis run and every 15-minute cycle (PART 1/2)."""
    try:
        from src import model_picks as _mp
        _mp.log_games(_analysis_state.get("results") or [], "mlb")
        _mp.log_games(_wnba_analysis_state.get("results") or [], "wnba")
        from src.props_scored_cache import load_scored_props
        _scored = (load_scored_props() or {}).get("picks") or []
        _mp.log_props(_scored)
        # Forward-only research history: freeze each scored prop's AI model +
        # edge + odds now (while the per-day caches are still warm) so the
        # /research leaderboard can attribute settled results later.
        try:
            from src import research_store as _rst
            _rst.record(_scored)
        except Exception as _rx:                                            # noqa: BLE001
            _eprint(f"RESEARCH-STORE: record failed: {type(_rx).__name__}: {_rx}")
    except Exception as exc:                                               # noqa: BLE001
        _eprint(f"MODEL-PICKS: log failed: {type(exc).__name__}: {exc}")


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


_MODEL_PICK_STAT = {
    "pitcher_strikeouts": "K", "pitcher_earned_runs": "ER",
    "pitcher_hits_allowed": "H", "pitcher_walks": "BB", "pitcher_outs": "outs",
    "batter_hits": "H", "batter_total_bases": "TB", "batter_home_runs": "HR",
    "batter_rbis": "RBI", "batter_runs_scored": "R", "batter_walks": "BB",
    "batter_strikeouts": "SO",
}


# Per-pass gamelog memo for settlement: (player_id, is_pitcher) -> (ts, games).
# Settlement force-refreshes gamelogs (see below); a pitcher with three pending
# prop markets would otherwise fire three identical statsapi calls in one pass.
# Short TTL so a later cycle (15 min on) still picks up newly-finished games.
_SETTLE_GAMELOG_MEMO: dict = {}
_SETTLE_GAMELOG_TTL = 120.0

# Per-cycle budget for the verbose STAT-LOOKUP diagnostic.  A settlement pass
# can call the lookup hundreds of times (one per pending prop), so we log only
# the first few per pass -- enough to confirm in Railway logs WHAT season/date
# is queried and what comes back, without flooding.  Reset before each settle.
_STAT_LOOKUP_LOG_BUDGET = 0


def _reset_stat_lookup_log_budget(n: int = 15) -> None:
    global _STAT_LOOKUP_LOG_BUDGET
    _STAT_LOOKUP_LOG_BUDGET = int(n)


def _model_pick_stat_lookup(player: str, market: str, pick_date: str = None):
    """Actual stat for a player's completed game, for settling prop picks.

    *pick_date* (YYYY-MM-DD ET, the slate the pick belongs to) makes the
    lookup grade a SPECIFIC past game rather than only "today":

      * season is taken from pick_date's year -- the old hardcoded
        _CURRENT_SEASON (2025) never returned 2026 gamelogs, so every 2026
        prop stayed pending.
      * the accepted game date is pick_date itself, so a backlog of picks
        from earlier days settles against each pick's own game.

    When *pick_date* is omitted (the ledger / Top-Plays same-day callers) the
    original today-window behaviour is kept: force-refresh the gamelog (so the
    just-finished game is present, not a stale morning cache) and accept today,
    plus yesterday only during the 12 AM-2 AM ET post-midnight settlement tail.
    """
    try:
        import time as _time
        from datetime import timedelta as _td
        from src.player_profile_client import (
            search_player_by_name, get_player_gamelog, gamelog_stat_value,
            _CURRENT_SEASON,
        )
        pid = search_player_by_name(player)
        if not pid:
            _stat_lookup_log(player, market, None, None, "player-not-found")
            return None
        is_pitcher = (market or "").startswith("pitcher_")

        if pick_date:
            try:
                season = int(str(pick_date)[:4])
            except (TypeError, ValueError):
                season = _CURRENT_SEASON
            acceptable = {str(pick_date)[:10]}
        else:
            season = _CURRENT_SEASON
            now_et = datetime.now(timezone(timedelta(hours=-4)))
            acceptable = {now_et.date().isoformat()}
            if now_et.hour <= 2:        # post-midnight settlement tail
                acceptable.add((now_et.date() - _td(days=1)).isoformat())

        memo_key = (int(pid), is_pitcher, season)
        cached = _SETTLE_GAMELOG_MEMO.get(memo_key)
        now = _time.monotonic()
        if cached is not None and (now - cached[0]) < _SETTLE_GAMELOG_TTL:
            games = cached[1]
        else:
            games = get_player_gamelog(
                int(pid), season,
                is_pitcher=is_pitcher, force_refresh=True,
            ) or []
            _SETTLE_GAMELOG_MEMO[memo_key] = (now, games)

        cand = [x for x in games if (x.get("date") or "")[:10] in acceptable]
        if not cand:
            _stat_lookup_log(player, market, season, acceptable,
                             f"no-game ({len(games)} in season)")
            return None
        g = max(cand, key=lambda x: (x.get("date") or ""))
        actual = gamelog_stat_value(g, _MODEL_PICK_STAT.get(market, market))
        _stat_lookup_log(player, market, season, acceptable,
                         f"game {(g.get('date') or '')[:10]} -> {actual}")
        return actual
    except Exception:                                                      # noqa: BLE001
        return None


def _stat_lookup_log(player, market, season, acceptable, outcome) -> None:
    """Budgeted one-liner so the settlement pass shows what the stat lookup
    queried (season + accepted date) and returned -- verifiable in prod logs."""
    global _STAT_LOOKUP_LOG_BUDGET
    if _STAT_LOOKUP_LOG_BUDGET <= 0:
        return
    _STAT_LOOKUP_LOG_BUDGET -= 1
    want = ",".join(sorted(acceptable)) if acceptable else "-"
    _eprint(f"MODEL-PICKS: STAT-LOOKUP {player!r} {market} "
            f"season={season} want={want} -> {outcome}")

_STATSAPI_BRIDGE_CACHE: dict = {}     # et_date_iso -> (ts, {norm_team: game_info})
_STATSAPI_BRIDGE_TTL = 3600.0         # 1 hour -- avoids re-fetching a date's
                                      # schedule on repeated Force Settlement.


def _statsapi_norm_team(name) -> str:
    """Lowercase + strip non-alphanumerics so Odds API and statsapi team
    names land on the same key ('LA Dodgers' == 'Los Angeles Dodgers')."""
    if not name:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _statsapi_pick_et_date(iso):
    """ET calendar date (YYYY-MM-DD) for a model_picks created_at timestamp
    (stored UTC) -- the game is the ET day the pick was logged.  None on
    parse failure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=-4))).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return None


def _statsapi_date_window(base_iso):
    """[base-1, base, base+1] ET date strings, tolerating UTC/ET rollover and
    games logged the morning before a late start."""
    try:
        from datetime import date as _date
        b = _date.fromisoformat(base_iso)
        return [(b + timedelta(days=o)).isoformat() for o in (0, -1, 1)]
    except Exception:                                                     # noqa: BLE001
        return [base_iso]


def _statsapi_schedule_index(date_iso: str) -> dict:
    """Final MLB games for one ET date from the free statsapi.mlb.com schedule
    (hydrate=linescore), indexed by normalised team name ->
    {home_team, away_team, home_score, away_score, gamePk}.  Cached 1h.
    No API key, no day-window limit."""
    now = time.time()
    hit = _STATSAPI_BRIDGE_CACHE.get(date_iso)
    if hit and (now - hit[0]) < _STATSAPI_BRIDGE_TTL:
        return hit[1]
    idx: dict = {}
    url = (f"{_MLB_STATS_BASE}/schedule?sportId=1&date={date_iso}"
           f"&hydrate=linescore")
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "sports-betting-ai/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"SETTLE-STATSAPI: schedule fetch failed for {date_iso}: "
                f"{type(exc).__name__}: {exc}")
        _STATSAPI_BRIDGE_CACHE[date_iso] = (now, {})
        return {}
    for d in (data.get("dates") or []):
        for g in (d.get("games") or []):
            if ((g.get("status") or {}).get("abstractGameState")) != "Final":
                continue
            teams = g.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            ht = (home.get("team") or {}).get("name")
            at = (away.get("team") or {}).get("name")
            hs, as_ = home.get("score"), away.get("score")
            if ht is None or at is None or hs is None or as_ is None:
                continue
            info = {"home_team": ht, "away_team": at,
                    "home_score": int(hs), "away_score": int(as_),
                    "gamePk": g.get("gamePk")}
            idx[_statsapi_norm_team(ht)] = info
            idx[_statsapi_norm_team(at)] = info
    _STATSAPI_BRIDGE_CACHE[date_iso] = (now, idx)
    return idx


def _fetch_mlb_statsapi_scores(game_picks: list) -> dict:
    """Resolve final scores for stale GAME model_picks rows from the free
    statsapi.mlb.com schedule -- the fallback for picks whose game is older
    than the Odds API /scores 3-day window.

    model_picks stores ``game_id`` = the Odds API event id (32-char hex), which
    statsapi does NOT understand (it keys games by a 6-digit gamePk), so a bare
    id cannot be turned into a gamePk.  We bridge via the data the pick row DOES
    carry: the picked TEAM (``pick_side``) plus the ET day it was logged
    (``created_at``).  That date's statsapi schedule yields the gamePk, both
    team names and the final score; we match the pick's team and return the
    score keyed by the ORIGINAL Odds API ``game_id`` so settle()'s
    ``final_scores.get(pick["game_id"])`` lookup hits.

    *game_picks* must be GAME rows (no ``player_name``) -- props are graded via
    the stat lookup, not scores, so the caller filters them out.  Returns
    ``{odds_api_game_id: {home_team, away_team, home_score, away_score}}`` with
    team names taken from the picks' own ``pick_side`` strings where they map,
    so _grade_game's exact-name comparison succeeds.
    """
    # Group rows by Odds API game_id; gather the (Odds API) team names the
    # picks used + a representative logged date.
    by_gid: dict = {}
    for pick in (game_picks or []):
        gid = str(pick.get("game_id") or "").strip()
        if not gid:
            continue
        grp = by_gid.setdefault(gid, {"teams": set(), "date": None})
        side = (pick.get("pick_side") or "").strip()
        if side and side.upper() not in ("OVER", "UNDER"):  # ml/rl carry a team
            grp["teams"].add(side)
        if grp["date"] is None:
            grp["date"] = _statsapi_pick_et_date(pick.get("created_at"))

    out: dict = {}
    for gid, grp in by_gid.items():
        teams, base_date = grp["teams"], grp["date"]
        if not teams or not base_date:
            continue                          # totals-only group / no date -> skip
        info = match_team = None
        for date_iso in _statsapi_date_window(base_date):
            idx = _statsapi_schedule_index(date_iso)
            for tm in teams:
                hit = idx.get(_statsapi_norm_team(tm))
                if hit:
                    info, match_team = hit, tm
                    break
            if info:
                break
        if not info:
            continue
        # Use the picks' own (Odds API) team strings where they map back to the
        # statsapi game, so _grade_game's `side == ht/at` succeeds; fall back to
        # the statsapi names for the side the picks never referenced.
        norm_to_pick = {_statsapi_norm_team(tm): tm for tm in teams}
        home_name = norm_to_pick.get(_statsapi_norm_team(info["home_team"]),
                                     info["home_team"])
        away_name = norm_to_pick.get(_statsapi_norm_team(info["away_team"]),
                                     info["away_team"])
        out[gid] = {
            "home_team":  home_name,
            "away_team":  away_name,
            "home_score": info["home_score"],
            "away_score": info["away_score"],
        }
        _eprint(
            f"SETTLE-STATSAPI: resolved game_id={gid} via team={match_team!r} "
            f"date={base_date} -> gamePk={info.get('gamePk')} "
            f"{away_name} {info['away_score']} @ {home_name} {info['home_score']}"
        )
    return out


def _final_scores_from(scores_by_sport: dict) -> dict:
    """Build {game_id: {home_team, away_team, home_score, away_score}} from
    completed Odds API score rows, for model_picks game settlement."""
    out: dict = {}
    for rows in (scores_by_sport or {}).values():
        for row in (rows or []):
            if not row.get("completed"):
                continue
            gid = str(row.get("id") or "")
            if not gid:
                continue
            ht, at = row.get("home_team"), row.get("away_team")
            sc = {s.get("name"): s.get("score") for s in (row.get("scores") or [])}
            try:
                hs = int(sc.get(ht))
                as_ = int(sc.get(at))
            except (TypeError, ValueError):
                continue
            out[gid] = {"home_team": ht, "away_team": at,
                        "home_score": hs, "away_score": as_}
    return out


def _run_auto_settlement_job(force: bool = False) -> dict:
    """
    APScheduler callback: every 30 min.
    Gated to 11 AM–2 AM ET (game hours) unless `force=True`. Settles
    completed bets via Odds API scores; voids postponed MLB games via
    MLB Stats API. Logs summary to stderr.

    Returns a summary dict {settled, wins, losses, voided, skipped} so
    /api/admin/settle_now can surface counts in the admin toast.  The
    scheduler ignores the return.
    """
    # ── Gate: only run during game hours (11 AM through 2 AM ET) ────────────
    try:
        from zoneinfo import ZoneInfo as _ZoneInfo
        _et = _ZoneInfo("America/New_York")
    except Exception:
        from datetime import timezone as _dtz, timedelta as _dtd
        _et = _dtz(timedelta(hours=-5))
    now_et   = datetime.now(_et)
    et_hour  = now_et.hour
    # Settlement window 12 PM-1 AM ET (noon-11:59 PM, plus 00:00-01:59 for late
    # west-coast finishers).  Matches the cycle's in_settlement_window + the
    # Top Plays gate so all stores settle on the same hours.
    in_window = (et_hour >= 12) or (et_hour <= 1)
    if not in_window and not force:
        return {"settled": 0, "wins": 0, "losses": 0,
                "voided": 0, "skipped": "out of game-hours window"}

    odds_key = os.getenv("ODDS_API_KEY", "")
    if not odds_key or odds_key == "your_odds_api_key_here":
        return

    _eprint(f"AUTO-SETTLE: checking at {now_et.strftime('%H:%M ET')}")
    oc = OddsClient(odds_key, _cache)

    settled: list = []

       # ── Fetch scores ONCE per sport for this pass ──────────────────────────────
    # The same score rows feed ledger settlement AND the bulk tracker grader
    # below; fetching here (and passing the result down) guarantees a single
    # Odds API scores call per sport per cycle instead of one per consumer.
    scores_by_sport: dict[str, list] = {}
    for _sk, _ck in (("baseball_mlb",    "scores_baseball_mlb_3"),
                     ("basketball_wnba", "scores_basketball_wnba_3")):
        try:
            # Invalidate the stale cache so we always see fresh completions.
            _cache.invalidate(_ck)
            scores_by_sport[_sk] = oc.get_scores(sport_key=_sk, days_from=3) or []
        except Exception as _sx:                                            # noqa: BLE001
            _eprint(f"AUTO-SETTLE: get_scores({_sk}) failed: "
                    f"{type(_sx).__name__}: {_sx}")
            scores_by_sport[_sk] = []

    # ── Augment with the free MLB Stats API for picks older than the Odds
    # API /scores 3-day window (stale rows that would otherwise never settle).
    # Forced passes only (cycle / JOB1 / admin) so routine ticks don't add
    # statsapi calls.  Synthetic rows are keyed by the ORIGINAL Odds API id so
    # _final_scores_from + settle() match them exactly like real score rows.
    if force:
        try:
            from src import db as _db
            _pending_rows = _db.model_picks_list(sport="mlb", status="pending")
            # Odds API score rows are keyed by "id"; skip games already covered
            # by the live (3-day) Odds API fetch above.
            _covered = {
                str(r.get("id")) for r in scores_by_sport.get("baseball_mlb", [])
                if r.get("completed") and r.get("id")
            }
            # GAME rows only (props settle via the stat lookup, not scores), and
            # only games the Odds API window didn't already cover.
            _stale_games = [
                r for r in (_pending_rows or [])
                if not r.get("player_name")
                and str(r.get("game_id") or "") not in _covered
            ]
            if _stale_games:
                _eprint(
                    f"SETTLE-STATSAPI: {len(_stale_games)} stale game pick(s) "
                    f"outside the Odds API window -- resolving via statsapi"
                )
                _statsapi_scores = _fetch_mlb_statsapi_scores(_stale_games)
                _eprint(f"SETTLE-STATSAPI: resolved {len(_statsapi_scores)} game(s)")
                _synthetic = [
                    {
                        "id": _gid, "completed": True,
                        "home_team": _sc["home_team"], "away_team": _sc["away_team"],
                        "scores": [
                            {"name": _sc["home_team"], "score": str(_sc["home_score"])},
                            {"name": _sc["away_team"], "score": str(_sc["away_score"])},
                        ],
                    }
                    for _gid, _sc in _statsapi_scores.items()
                ]
                if _synthetic:
                    scores_by_sport["baseball_mlb"] = (
                        scores_by_sport.get("baseball_mlb", []) + _synthetic
                    )
        except Exception as _sax:                                         # noqa: BLE001
            _eprint(f"SETTLE-STATSAPI: augment failed: {type(_sax).__name__}: {_sax}")

    # ── Open both ledgers up front so we can log bankroll before/after ─────────
    def _open_ledger(_path):
        try:
            return Ledger(path=_path, starting_bankroll=250)
        except Exception as _le:                                            # noqa: BLE001
            _eprint(f"AUTO-SETTLE: open {_path} failed: "
                    f"{type(_le).__name__}: {_le}")
            return None

    def _bankrolls(_ldr):
        if _ldr is None:
            return (0.0, 0.0)
        return (float(_ldr.data.get("model_bankroll") or 0.0),
                float(_ldr.data.get("personal_bankroll") or 0.0))

    _mlb_ldr  = _open_ledger("data/ledger.json")
    _wnba_ldr = _open_ledger("data/wnba_ledger.json")

    mlb_model_before,  mlb_pers_before  = _bankrolls(_mlb_ldr)
    wnba_model_before, wnba_pers_before = _bankrolls(_wnba_ldr)

    # ── MLB ───────────────────────────────────────────────────────────────────
    try:
        if _mlb_ldr is not None and _mlb_ldr.data.get("open_bets"):
            settled.extend(_mlb_ldr.settle(
                oc, "baseball_mlb", scores=scores_by_sport.get("baseball_mlb")))
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: MLB error: {type(_exc).__name__}: {_exc}")

    # ── WNBA ──────────────────────────────────────────────────────────────────
    try:
        if _wnba_ldr is not None and _wnba_ldr.data.get("open_bets"):
            settled.extend(_wnba_ldr.settle(
                oc, "basketball_wnba", scores=scores_by_sport.get("basketball_wnba")))
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: WNBA error: {type(_exc).__name__}: {_exc}")

    # ── Postponed games (MLB Stats API) ───────────────────────────────────────
    voided: list = []
    try:
        voided = _void_postponed_mlb_bets()
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: postponed check error: {type(_exc).__name__}: {_exc}")

    # ── Props picks (tracked from the Props page → props_picks_history) ──────
    # New props tracker (src.props_picks_tracker) replaces the old
    # props_ledger; it settles pending picks against the player's actual
    # box-score stat and books model P/L into the props bankroll.
    props_summary = {"settled": 0, "won": 0, "lost": 0, "void": 0,
                     "pnl": 0.0, "bankroll": 0.0, "still_pending": 0}
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        props_summary = _ppt.settle_pending()
        if props_summary.get("settled"):
            _eprint(
                f"PROPS-SETTLE: settled {props_summary['settled']} prop pick(s) — "
                f"{props_summary['won']}W / {props_summary['lost']}L / "
                f"{props_summary['void']}V | "
                f"P/L ${props_summary['pnl']:+.2f} | "
                f"bankroll ${props_summary['bankroll']:.2f} | "
                f"{props_summary['still_pending']} still pending"
            )
        else:
            _eprint(
                f"PROPS-SETTLE: no props newly settled "
                f"({props_summary.get('still_pending', 0)} still pending)"
            )
    except Exception as _pe:
        _eprint(f"PROPS-SETTLE: error: {type(_pe).__name__}: {_pe}")
    props_settled = props_summary.get("settled", 0)

    # ── FIX 2: bulk-grade the per-model trackers (XGB/LR/NN) ──────────────────
    # The per-bet ledger hook only grades trackers for games we actually
    # placed bets on.  Grade EVERY analyzed pick whose game just completed by
    # pulling the same Odds API scores and matching by game_id (XGB/LR) and
    # matchup+date (NN).  Runs on every settlement pass (auto + nightly force).
    graded = {"xgb": 0, "lr": 0, "nn": 0}
    try:
        graded = _grade_model_trackers(
            oc, ["baseball_mlb", "basketball_wnba"],
            scores_by_sport=scores_by_sport,
        )
        if any(graded.values()):
            _eprint(f"TRACKER-GRADE: newly graded xgb={graded['xgb']} "
                    f"lr={graded['lr']} nn={graded['nn']}")
    except Exception as _ge:
        _eprint(f"TRACKER-GRADE: error: {type(_ge).__name__}: {_ge}")

    # ── Terminal summary ──────────────────────────────────────────────────────
    wins   = sum(1 for s in settled if s.get("result") == "win")
    losses = sum(1 for s in settled if s.get("result") == "loss")

    if settled:
        _eprint(
            f"AUTO-SETTLE: settled {len(settled)} bet(s) — "
            f"{wins}W / {losses}L"
        )
        _BET_TYPE_SHORT = {"single": "ML", "run_line": "RL", "spread": "SPR", "totals": "TOT"}
        for s in settled:
            bts    = _BET_TYPE_SHORT.get(s.get("bet_type", "single"), "ML")
            result = s.get("result", "?").upper()
            team   = s.get("bet_team", "?")
            away   = s.get("away_team", "?")
            home   = s.get("home_team", "?")
            pnl    = s.get("model_pnl", 0.0)
            _eprint(f"  {away} @ {home} | {bts} {team} → {result} | model P&L: ${pnl:+.2f}")
    if voided:
        _eprint(f"AUTO-SETTLE: voided {len(voided)} postponed bet(s) (stakes returned)")
        for v in voided:
            _eprint(f"  {v.get('away_team','?')} @ {v.get('home_team','?')} — POSTPONED, stake returned")
    if not settled and not voided:
        _eprint("AUTO-SETTLE: no newly settled bets")

    # ── Total model P/L across everything settled this pass ─────────────────
    # game-pick P/L is on each settled game bet's "model_pnl"; prop P/L
    # comes from the props_picks_tracker settle summary.  Summing both
    # gives the day's realized model P/L for the JOB 1 summary line.
    game_pnl  = sum(float(s.get("model_pnl") or 0.0) for s in settled)
    props_pnl = float(props_summary.get("pnl") or 0.0)
    total_pnl = game_pnl + props_pnl

    # ── Pass summary: per-tracker settled counts + bankroll before/after ──────
    # ledger.settle() mutated the same instances we opened above, so reading
    # their bankrolls now reflects the post-settlement balances.  Personal
    # bankroll is unified across sports and lives in data/ledger.json.
    mlb_model_after,  mlb_pers_after  = _bankrolls(_mlb_ldr)
    wnba_model_after, _               = _bankrolls(_wnba_ldr)
    model_before = mlb_model_before + wnba_model_before
    model_after  = mlb_model_after  + wnba_model_after
    _eprint(
        "SETTLE-SUMMARY: game picks settled — "
        f"xgb={graded.get('xgb', 0)} lr={graded.get('lr', 0)} "
        f"nn={graded.get('nn', 0)} | ledger bets {len(settled)} "
        f"({wins}W/{losses}L) | props settled {props_settled} | "
        f"model bankroll ${model_before:.2f} -> ${model_after:.2f} | "
        f"personal bankroll ${mlb_pers_before:.2f} -> ${mlb_pers_after:.2f}"
    )

    # Per-store settled tallies + error tags, surfaced in the cycle summary
    # line below so all four systems can be verified at a glance in the logs.
    _settle_errors: list[str] = []
    model_picks_settled = 0
    top_plays_settled = 0

    # ── Per-model pick logging + settlement (PART 1-3) ────────────────────────
    # Log the current picks (deduped) then settle today's pending ones against
    # the final scores fetched above + each player's actual stat line.
    try:
        from src import model_picks as _mp
        _log_model_picks()
        _reset_stat_lookup_log_budget(15)
        _summary = _mp.settle(
            final_scores=_final_scores_from(scores_by_sport),
            stat_lookup=_model_pick_stat_lookup,
        )
        model_picks_settled = sum(int(n) for n in (_summary or {}).values())
        if _summary:
            _eprint("MODEL-PICKS settled: "
                    + ", ".join(f"{m}={n}" for m, n in sorted(_summary.items())))
        # PART 7 — per-store status so every store can be verified in the logs.
        for _line in _mp.store_summary_counts():
            _eprint(f"SETTLE-SUMMARY store {_line}")
    except Exception as _mpx:                                              # noqa: BLE001
        _settle_errors.append(f"model_picks:{type(_mpx).__name__}")
        _eprint(f"MODEL-PICKS: settle pass failed: {type(_mpx).__name__}: {_mpx}")

    # ── Settle the /research model-analytics history ──────────────────────────
    # Same player-stat lookup as model_picks; grades each pending research row
    # and freezes its units P/L so the leaderboard's ROI/Win% reflect reality.
    try:
        from src import research_store as _rst
        _rsum = _rst.settle(stat_lookup=_model_pick_stat_lookup)
        if _rsum.get("settled"):
            _eprint(f"RESEARCH-STORE: settled {_rsum['settled']} "
                    f"({_rsum['wins']}W/{_rsum['losses']}L/{_rsum['voids']}V)")
    except Exception as _rx:                                               # noqa: BLE001
        _settle_errors.append(f"research_store:{type(_rx).__name__}")
        _eprint(f"RESEARCH-STORE: settle pass failed: {type(_rx).__name__}: {_rx}")

    # ── Settle the rebuilt Supabase ledgers (Model + My Bets) ─────────────────
    # Grades each active staked bet against the same final scores / player
    # stats and applies the frozen-stake bankroll movement once: WIN returns
    # stake+profit, LOSS leaves the stake out, PUSH/VOID returns the stake.
    try:
        from src import ledger_integration as _li
        _lsum = _li.settle_open_ledger_bets(
            final_scores=_final_scores_from(scores_by_sport),
            stat_lookup=_model_pick_stat_lookup,
        )
        for _sys, _s in (_lsum or {}).items():
            if _s.get("settled"):
                _eprint(f"LEDGER-SETTLE [{_sys}]: settled {_s['settled']} "
                        f"({_s['wins']}W/{_s['losses']}L/{_s['pushes']}P)")
    except Exception as _lx:                                               # noqa: BLE001
        _settle_errors.append(f"ledger_integration:{type(_lx).__name__}")
        _eprint(f"LEDGER-SETTLE failed: {type(_lx).__name__}: {_lx}")

    # ── FIX 3: Supabase supa_ledger is the single authoritative model bankroll.
    # ledger_integration above just moved it; mirror that value back into the
    # ledger.json cache so the file's own settle math (run earlier this pass)
    # can't leave the fallback display out of sync with Supabase.
    try:
        from src import supa_ledger as _sl
        if _sl.db.is_supabase():
            _supa_model_bal = float(_sl.model().bankroll())
            for _ldr in (_mlb_ldr, _wnba_ldr):
                if _ldr is not None:
                    _ldr.sync_model_bankroll(_supa_model_bal)
            _eprint("MODEL-BANKROLL: synced ledger.json cache to Supabase "
                    f"authoritative ${_supa_model_bal:.2f}")
    except Exception as _bx:                                               # noqa: BLE001
        _settle_errors.append(f"bankroll_sync:{type(_bx).__name__}")
        _eprint(f"MODEL-BANKROLL sync failed: {type(_bx).__name__}: {_bx}")

    # ── Settle the standalone Top Plays scorecard (12 PM–1 AM ET) ─────────────
    # Its own store, separate from model_picks + the ledgers.  Same final
    # scores / stat lookup; on a win it adds profit in units off the frozen
    # odds, on a loss it subtracts the staked units (no balance is moved).
    if et_hour >= 12 or et_hour <= 1:
        try:
            from src import top_plays_tracker as _tpt
            _tps = _tpt.settle(
                final_scores=_final_scores_from(scores_by_sport),
                stat_lookup=_model_pick_stat_lookup,
            )
            top_plays_settled = int(_tps.get("settled") or 0)
            if _tps.get("settled"):
                _eprint(f"TOP-PLAYS settled {_tps['settled']} "
                        f"({_tps['wins']}W/{_tps['losses']}L/{_tps['pushes']}P)")
        except Exception as _tx:                                           # noqa: BLE001
            _settle_errors.append(f"top_plays:{type(_tx).__name__}")
            _eprint(f"TOP-PLAYS settle failed: {type(_tx).__name__}: {_tx}")

    # ── Recalculate the daily budget off the post-settlement bankroll ─────────
    # A win adds profit / a loss removes the stake from the personal bankroll,
    # so the budget (20% of bankroll), floor (1%) and ceiling (5%) must track
    # the new total immediately (FIX 3).
    if settled or voided:
        try:
            _persist_daily_budget(mlb_pers_after)
        except Exception as _be:                                           # noqa: BLE001
            _eprint(f"AUTO-SETTLE: budget recalc failed: {_be}")

    # ── Consolidated settlement summary -- one line covering all four systems
    # so a Railway log grep confirms each is firing (or shows which errored).
    _err_txt = ("none" if not _settle_errors
                else f"{len(_settle_errors)} ({', '.join(_settle_errors)})")
    _eprint(
        "SETTLE-CYCLE-SUMMARY: "
        f"game_bets={len(settled)} ({wins}W/{losses}L) | "
        f"model_picks={model_picks_settled} | "
        f"props={int(props_summary.get('settled') or 0)} | "
        f"top_plays={top_plays_settled} | voided={len(voided)} | "
        f"errors={_err_txt}"
    )

    # ── Update state ──────────────────────────────────────────────────────────
    with _auto_settlement_lock:
        _auto_settlement_state.update({
            "last_ran_at":  datetime.now(timezone.utc).isoformat(),
            "last_settled": len(settled),
            "last_wins":    wins,
            "last_losses":  losses,
            "last_voided":  len(voided),
        })

    return {
        "settled":       len(settled),
        "wins":          wins,
        "losses":        losses,
        "voided":        len(voided),
        "model_picks_settled": model_picks_settled,
        "props_settled": int(props_summary.get("settled") or 0),
        "props_won":     int(props_summary.get("won") or 0),
        "props_lost":    int(props_summary.get("lost") or 0),
        "props_bankroll": float(props_summary.get("bankroll") or 0.0),
        "top_plays_settled": top_plays_settled,
        "settle_errors": list(_settle_errors),
        "game_pnl":      round(game_pnl, 2),
        "props_pnl":     round(props_pnl, 2),
        "total_pnl":     round(total_pnl, 2),
        "forced":        force,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Consolidated 15-minute refresh cycle (auto_props_refresh job)
#
#  One coordinated pass, in order:
#    1. MLB/WNBA schedule + live scores (+ Supabase) and pitching-change detect
#    2. Game odds re-fetch + significant line-movement flagging
#    3. Player prop lines re-fetch          (run_tier_1_refresh)
#    4. Re-score props                      (run_tier_1_refresh)
#    5. Settlement check                    (_run_auto_settlement_job; replaces
#                                            the old 30-min auto_settlement job)
#    6. Groq summary queue                  (launched by run_tier_1_refresh)
# ══════════════════════════════════════════════════════════════════════════════

def _refresh_schedule_and_scores() -> dict:
    """Step 1: refresh today's schedule (scores/status, written to Supabase by
    _fetch_raw_schedule) and repopulate the live-score cache.  Pitching-change
    detection now lives in _detect_game_changes (step 2).  Best-effort."""
    import sys as _sys
    out = {"sports": 0}
    for sport in ("mlb", "wnba"):
        try:
            _fetch_raw_schedule(sport, _today_et())
            out["sports"] += 1
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE step1 {sport} schedule error: {exc}")
        try:
            from components import live_score as _ls
            _ls.fetch_live(_sys.modules.get(__name__), sport)
        except Exception:                                                 # noqa: BLE001
            pass
    return out


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
    odds_key = os.getenv("ODDS_API_KEY", "")
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


def _norm_team(name) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _team_pair(away, home) -> str:
    return f"{_norm_team(away)}|{_norm_team(home)}"


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _probables_by_pair() -> dict:
    """{team_pair -> 'away_pitcher|home_pitcher'} for today's MLB games."""
    url = (f"{_MLB_STATS_BASE}/schedule?sportId=1&date={_today_et()}"
           f"&hydrate=probablePitcher")
    out: dict = {}
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"CYCLE probables fetch failed: {exc}")
        return out
    for date_block in (data.get("dates") or []):
        for g in (date_block.get("games") or []):
            teams = g.get("teams") or {}
            an = (((teams.get("away") or {}).get("team") or {}).get("name") or "")
            hn = (((teams.get("home") or {}).get("team") or {}).get("name") or "")
            ap = (((teams.get("away") or {}).get("probablePitcher") or {}).get("fullName") or "TBD")
            hp = (((teams.get("home") or {}).get("probablePitcher") or {}).get("fullName") or "TBD")
            if an and hn:
                out[_team_pair(an, hn)] = f"{ap}|{hp}"
    return out


def _detect_game_changes() -> dict:
    """Compare this cycle's odds + probables + lineup-confirmed + weather to
    last cycle's per game; flag significant changes (ML > 10c, total > 0.5,
    run line > 0.5, pitcher change, lineup-confirmed flip, wind > 5 mph or
    temp > 10 F).  Lineup-roster swaps and precipitation aren't in the data
    feeds (only a confirmed flag + wind/temp), so those sub-signals are
    detected to the extent the existing data allows.  Returns the changeset +
    the fresh per-game odds dicts (for re-runs)."""
    out = {
        "games_with_changes": 0, "reasons": {}, "odds_by_id": {},
        "sport_by_id": {}, "pitcher_changed_pairs": set(),
        "counts": {"ml": 0, "total": 0, "rl": 0, "pitcher": 0,
                   "lineup": 0, "weather": 0},
    }
    odds_key = os.getenv("ODDS_API_KEY", "")
    if not odds_key or odds_key == "your_odds_api_key_here":
        return out
    try:
        oc = OddsClient(odds_key, _cache)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"CYCLE detect-changes OddsClient init failed: {exc}")
        return out

    pair_pitchers = {}
    try:
        pair_pitchers = _probables_by_pair()
    except Exception:                                                     # noqa: BLE001
        pair_pitchers = {}

    _lineup_fn = _weather_fn = None
    try:
        from src.lineup_client import get_lineup_client
        _lineup_fn = get_lineup_client().is_lineup_confirmed
    except Exception:                                                     # noqa: BLE001
        _lineup_fn = None
    try:
        from src.weather_client import get_game_weather as _weather_fn
    except Exception:                                                     # noqa: BLE001
        _weather_fn = None

    for sport_key, sport in (("baseball_mlb", "mlb"), ("basketball_wnba", "wnba")):
        try:
            games = oc.get_odds(sport_key, force_refresh=True) or []
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE detect-changes {sport_key} odds error: {exc}")
            continue
        for g in games:
            gid = str(g.get("id") or "")
            if not gid:
                continue
            home, away = g.get("home_team"), g.get("away_team")
            pair = _team_pair(away, home)
            cur = {
                "sport":   sport,
                "ml_home": g.get("h2h_home_odds"),
                "ml_away": g.get("h2h_away_odds"),
                "total":   g.get("total_line"),
                "rl_point": g.get("run_line_point"),
            }
            if sport == "mlb":
                cur["pitchers"] = pair_pitchers.get(pair)
                if _lineup_fn:
                    try:
                        cur["lineup"] = _lineup_fn(home, away, _today_et())
                    except Exception:                                     # noqa: BLE001
                        cur["lineup"] = None
                if _weather_fn:
                    try:
                        wx = _weather_fn(home, g.get("commence_time") or "") or {}
                        cur["wind"] = wx.get("wind_speed")
                        cur["temp"] = wx.get("temperature")
                    except Exception:                                     # noqa: BLE001
                        cur["wind"] = cur["temp"] = None

            prev = _last_game_state.get(gid)
            reasons: list[str] = []
            if prev:
                for s, lbl in (("ml_home", "ML home"), ("ml_away", "ML away")):
                    a, b = prev.get(s), cur.get(s)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(b - a) > 10:
                        reasons.append(f"{lbl} {a}->{b}")
                        out["counts"]["ml"] += 1
                        break
                a, b = prev.get("total"), cur.get("total")
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(b - a) > 0.5:
                    reasons.append(f"total {a}->{b}")
                    out["counts"]["total"] += 1
                a, b = prev.get("rl_point"), cur.get("rl_point")
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(b - a) > 0.5:
                    reasons.append(f"run line {a}->{b}")
                    out["counts"]["rl"] += 1
                pa, pb = prev.get("pitchers"), cur.get("pitchers")
                if pa and pb and pa != pb and "TBD" not in (pb or ""):
                    reasons.append(f"pitcher change {pa} -> {pb}")
                    out["counts"]["pitcher"] += 1
                    out["pitcher_changed_pairs"].add(pair)
                la, lb = prev.get("lineup"), cur.get("lineup")
                if isinstance(la, (int, float)) and isinstance(lb, (int, float)) and la != lb:
                    reasons.append(f"lineup confirmed {la}->{lb}")
                    out["counts"]["lineup"] += 1
                wa, wb = prev.get("wind"), cur.get("wind")
                ta, tb = prev.get("temp"), cur.get("temp")
                if (isinstance(wa, (int, float)) and isinstance(wb, (int, float)) and abs(wb - wa) > 5) \
                        or (isinstance(ta, (int, float)) and isinstance(tb, (int, float)) and abs(tb - ta) > 10):
                    reasons.append("weather change")
                    out["counts"]["weather"] += 1
            _last_game_state[gid] = cur
            if reasons:
                out["games_with_changes"] += 1
                out["reasons"][gid] = reasons
                out["odds_by_id"][gid] = g
                out["sport_by_id"][gid] = sport
                _eprint(f"CYCLE game change [{sport}] {away} @ {home}: {', '.join(reasons)}")
    return out


def _update_result_in_state(sport: str, gid: str, serialized: dict) -> None:
    state = _analysis_state if sport == "mlb" else _wnba_analysis_state
    results = state.get("results") or []
    for i, r in enumerate(results):
        if _match_result_id(r, gid):
            results[i] = serialized
            state["results"] = results
            return
    results.append(serialized)
    state["results"] = results


def _update_snapshot_game(sport: str, gid: str, serialized: dict) -> None:
    snap = _read_daily_snapshot()
    if not _snapshot_is_today(snap):
        return
    sp = dict(snap.get(sport) or {})
    res = list(sp.get("results") or [])
    found = False
    for i, r in enumerate(res):
        if _match_result_id(r, gid):
            res[i] = serialized
            found = True
            break
    if not found:
        res.append(serialized)
    sp["results"] = res
    payload = {k: v for k, v in sp.items() if k != "analyzed_at"}
    _write_daily_snapshot(sport, payload, datetime.now(timezone.utc))


def _rerun_single_game(sport: str, game: dict, reasons: list) -> bool:
    """Re-run the full model for ONE game (reuses the cached no-odds
    predictor's models + feature builder), re-serialize with the game's fresh
    odds for EV, and update the in-memory state + snapshot for that game only.
    Invalidates the game's Groq summary.  Best-effort -- returns False (leaving
    the existing pick untouched) on any failure."""
    gid = str(game.get("id") or "")
    if not gid:
        return False
    try:
        ctx = _ensure_no_odds_predictor(sport)
        if not ctx:
            return False
        fb, ml_model, rl_model, totals_model = ctx
        if not ml_model or not getattr(ml_model, "is_trained", False):
            return False
        built = fb.build_for_game(game)
        if built is None:
            return False
        feature_vec, meta = built
        try:
            mw = Ledger(path="data/ledger.json", starting_bankroll=1000.0).get_model_weights()
        except Exception:                                                 # noqa: BLE001
            mw = None
        prediction = ml_model.predict(feature_vec, weights=mw, game_meta=game)
        rl_pred = None
        if sport == "mlb" and rl_model and getattr(rl_model, "is_trained", False):
            try:
                rl_pred = rl_model.predict(
                    feature_vec, game, weights=mw,
                    ml_prob_home    = prediction.get("xgb_prob"),
                    ml_lr_prob_home = prediction.get("lr_prob"),
                    ml_nn_prob_home = prediction.get("nn_prob"),
                )
            except Exception:                                             # noqa: BLE001
                rl_pred = None
        totals_pred = None
        if (sport == "mlb" and totals_model and getattr(totals_model, "is_trained", False)
                and game.get("total_line") is not None):
            try:
                tv = fb.build_totals_from_meta(meta) if hasattr(fb, "build_totals_from_meta") else None
                if tv is not None:
                    totals_pred = totals_model.predict(tv, game, weights=mw)
            except Exception:                                             # noqa: BLE001
                totals_pred = None

        raw = {"game": game, "prediction": prediction, "shap": None,
               "meta": meta, "rl_pred": rl_pred, "totals_pred": totals_pred}
        bankroll = float(_analysis_state.get("bankroll") or 250)
        try:
            pstart = Ledger(path="data/ledger.json", starting_bankroll=1000.0) \
                .data.get("personal_starting_bankroll", bankroll)
        except Exception:                                                 # noqa: BLE001
            pstart = bankroll
        serialized = _serialize(raw, bankroll, sport, pstart)
        if not serialized.get("game_id"):
            serialized["game_id"] = gid

        _update_result_in_state(sport, gid, serialized)
        _update_snapshot_game(sport, gid, serialized)
        try:
            from src import ai_summaries
            ai_summaries.invalidate_game(sport, gid)
        except Exception:                                                 # noqa: BLE001
            pass
        _eprint(
            f"CYCLE game re-run [{sport}] {game.get('away_team')} @ "
            f"{game.get('home_team')}: {', '.join(reasons)}"
        )
        return True
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"CYCLE game re-run failed gid={gid}: {type(exc).__name__}: {exc}")
        return False


def _detect_prop_changes(pitcher_changed_pairs: set) -> dict:
    """Compare this cycle's scored props (already re-scored in bulk by step
    3-4) to last cycle's; flag SIGNIFICANT changes (line > 1.0, side flip,
    projection-gap change > 0.5, or a pitcher change in that prop's game).
    Minor drift (line <= 0.5 and confidence shift < 3%) is ignored.  Returns
    the significant set so the cycle can invalidate just those Groq summaries."""
    out = {"props_with_changes": 0, "significant": [],
           "counts": {"line": 0, "side": 0, "gap": 0, "pitcher": 0}}
    try:
        from src.props_scored_cache import load_scored_props
        picks = (load_scored_props() or {}).get("picks") or []
    except Exception:                                                     # noqa: BLE001
        return out
    for r in picks:
        player, market = r.get("player"), r.get("market")
        if not player or not market:
            continue
        key = f"{player}|{market}"
        cur = {
            "line":            _to_float(r.get("line")),
            "side":            (r.get("side") or "").title(),
            "recommendation":  r.get("recommendation"),
            "predicted_value": _to_float(r.get("predicted_value")),
            "confidence":      _to_float(r.get("confidence")),
        }
        prev = _last_prop_state.get(key)
        reasons: list[str] = []
        if prev:
            la, lb = prev.get("line"), cur.get("line")
            if isinstance(la, (int, float)) and isinstance(lb, (int, float)) and abs(lb - la) > 1.0:
                reasons.append(f"line {la}->{lb}")
                out["counts"]["line"] += 1
            if prev.get("side") and cur.get("side") and prev["side"] != cur["side"]:
                reasons.append(f"side {prev['side']}->{cur['side']}")
                out["counts"]["side"] += 1
            pv0, ln0, pv1, ln1 = (prev.get("predicted_value"), prev.get("line"),
                                  cur.get("predicted_value"), cur.get("line"))
            if all(isinstance(x, (int, float)) for x in (pv0, ln0, pv1, ln1)):
                if abs(abs(pv1 - ln1) - abs(pv0 - ln0)) > 0.5:
                    reasons.append("proj-gap")
                    out["counts"]["gap"] += 1
            if _team_pair(r.get("away_team"), r.get("home_team")) in pitcher_changed_pairs:
                reasons.append("pitcher change")
                out["counts"]["pitcher"] += 1
        _last_prop_state[key] = cur
        if reasons:
            out["props_with_changes"] += 1
            out["significant"].append((player, market, reasons))
            _eprint(f"CYCLE prop change {player} [{market}]: {', '.join(reasons)}")
    return out


def _run_consolidated_refresh_cycle() -> dict:
    """The auto_props_refresh callback: one coordinated 15-minute pass with
    change detection + conditional per-game (5 AM-12 PM ET) and per-prop model
    re-runs.  Guarded so overlapping ticks are skipped; every step is
    best-effort so a failure in one never aborts the rest of the cycle."""
    if not _refresh_cycle_lock.acquire(blocking=False):
        _eprint("CYCLE: previous refresh cycle still running — skipping this tick")
        return {}
    started = time.time()
    summary = {
        "schedule_sports": 0,
        "games_changed": 0, "game_reruns": 0,
        "props_changed": 0, "prop_reruns": 0,
        "groq_queued": 0,
        "props_refreshed": False, "settled": 0, "props_settled": 0, "voided": 0,
    }
    try:
        # ET hour gate for the game-model re-run window (5 AM - 12 PM ET).
        try:
            from zoneinfo import ZoneInfo as _ZI
            et_hour = datetime.now(_ZI("America/New_York")).hour
        except Exception:                                                 # noqa: BLE001
            et_hour = datetime.now(timezone(timedelta(hours=-4))).hour
        in_game_rerun_window = 5 <= et_hour < 12

        # 1) Schedule + live scores (+ Supabase).
        s1 = _refresh_schedule_and_scores()
        summary["schedule_sports"] = s1.get("sports", 0)

        # 2) Change detection: odds / pitcher / lineup / weather per game.
        gc = _detect_game_changes()
        summary["games_changed"] = gc["games_with_changes"]

        # 2b) Conditional per-GAME model re-run -- only in the 5 AM-12 PM
        #     window, only for games that changed, one game at a time.
        rerun_serialized: list = []
        if in_game_rerun_window:
            for gid, reasons in gc["reasons"].items():
                sport = gc["sport_by_id"].get(gid, "mlb")
                if _rerun_single_game(sport, gc["odds_by_id"][gid], reasons):
                    summary["game_reruns"] += 1
                    state = _analysis_state if sport == "mlb" else _wnba_analysis_state
                    row = next((r for r in (state.get("results") or [])
                                if _match_result_id(r, gid)), None)
                    if row is not None:
                        rerun_serialized.append((sport, row))
        elif gc["games_with_changes"]:
            _eprint(f"CYCLE: {gc['games_with_changes']} game change(s) detected but "
                    f"outside the 5 AM-12 PM re-run window — not re-running game models")

        # 3 + 4) Player prop lines re-fetch + bulk re-score (props model re-run)
        #         + the props Groq batch.
        try:
            from src.props_client import run_tier_1_refresh
            run_tier_1_refresh()
            summary["props_refreshed"] = True
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE props refresh/re-score error: {type(exc).__name__}: {exc}")

        # 4b) Prop change detection against the freshly re-scored cache.
        pc = _detect_prop_changes(gc.get("pitcher_changed_pairs") or set())
        summary["props_changed"] = pc["props_with_changes"]
        summary["prop_reruns"]   = len(pc["significant"])

        # 6) Groq summary updates -- invalidate ONLY the picks that had a
        #    significant change (game re-run already invalidated its summary);
        #    minor changes keep their cached summary.  Then queue regeneration.
        groq_queued = summary["game_reruns"]
        try:
            from src import ai_summaries
            for player, market, _reasons in pc["significant"]:
                if ai_summaries.invalidate_prop(player, market):
                    groq_queued += 1
            ai_summaries.launch_summary_queue(
                game_results=rerun_serialized,
                do_games=bool(rerun_serialized), do_props=True,
            )
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE groq invalidate/queue error: {type(exc).__name__}: {exc}")
        summary["groq_queued"] = groq_queued

        # 4c) FIX 4: record Top Plays server-side, BEFORE settlement, so the
        #     scorecard + Top Plays settlement have picks to grade even when
        #     nobody opened /top-picks today.  build_rankings() applies the
        #     same AI-agreement gate the page uses and calls record_plays
        #     internally (idempotent -- dedup by play_id); the page-render
        #     call stays as a fallback but the cycle call is authoritative.
        try:
            from src import top_picks as _tp
            _ranked = _tp.build_rankings(sys.modules[__name__])
            summary["top_plays_recorded"] = int((_ranked or {}).get("count") or 0)
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE top-plays record error: {type(exc).__name__}: {exc}")

        # 5) Settlement (replaces the standalone 30-min auto_settlement job).
        #    Gated to 12 PM-1 AM ET: games rarely finish before noon, so
        #    running settlement earlier just burns an Odds API scores call
        #    with nothing to grade.  et_hour was computed at the top of this
        #    cycle.  Window = hour >= 12 (noon-11:59 PM) OR hour <= 1
        #    (00:00-01:59 ET) so late west-coast finishers settle the same
        #    night -- the auto_props_refresh cron now also fires at hour 0/1.
        in_settlement_window = (et_hour >= 12) or (et_hour <= 1)
        if in_settlement_window:
            try:
                st = _run_auto_settlement_job(force=True) or {}
                summary["settled"]       = int(st.get("settled") or 0)
                summary["props_settled"] = int(st.get("props_settled") or 0)
                summary["voided"]        = int(st.get("voided") or 0)
                summary["top_plays_settled"] = int(st.get("top_plays_settled") or 0)
            except Exception as exc:                                      # noqa: BLE001
                _eprint(f"CYCLE settlement error: {type(exc).__name__}: {exc}")
        else:
            _eprint(f"CYCLE: settlement skipped — {et_hour:02d}:xx ET is "
                    f"outside the 12 PM-1 AM settlement window")
    finally:
        duration = round(time.time() - started, 1)
        _refresh_cycle_state.update({
            "last_ran_at":   datetime.now(timezone.utc).isoformat(),
            "last_duration": duration,
            "last_summary":  dict(summary),
        })
        _eprint(
            "CYCLE COMPLETE in %ss | games_changed=%d game_reruns=%d | "
            "props_changed=%d prop_reruns=%d | groq_queued=%d | "
            "settled %d game + %d props (%d voided)" % (
                duration, summary["games_changed"], summary["game_reruns"],
                summary["props_changed"], summary["prop_reruns"],
                summary["groq_queued"], summary["settled"],
                summary["props_settled"], summary["voided"],
            )
        )
        _refresh_cycle_lock.release()
    return summary


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


# ── On-demand "Run AI Analysis" (admin) ───────────────────────────────────────
# Generates the Groq game summaries, prop summaries, and player breakdowns
# that aren't already cached -- the same logic the post-analysis queue runs,
# but on demand with live progress.  Sequential, 150 ms between Groq calls.
_ai_run_lock  = threading.Lock()
_ai_run_state: dict = {
    "running": False, "phase": "", "done": 0, "total": 0,
    "games_generated": 0, "props_generated": 0, "breakdowns_generated": 0,
    "skipped": 0, "failed": 0,
    "started_at": None, "finished_at": None, "elapsed": None, "summary": None,
}
_AI_RUN_DELAY = 0.15   # 150 ms between Groq calls (free-tier friendly)


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


# ── Nightly retrain scheduler ─────────────────────────────────────────────────
print("STARTUP: all routes registered — starting scheduler...", flush=True, file=sys.stderr)

# Start the APScheduler background job that fires every night at 2 AM ET.
# Guard against Werkzeug's double-import when debug=True / use_reloader=True:
# the reloader spawns a child process and sets WERKZEUG_RUN_MAIN=true there;
# we only want the scheduler running in that child, not the parent watcher.
_werkzeug_main = os.environ.get("WERKZEUG_RUN_MAIN", "false") == "true"
_in_debug_mode  = app.debug
if not _in_debug_mode or _werkzeug_main:
    # Seed the rebuilt ledger bankrolls in Supabase if absent (My Bets
    # $166.55, Model one combined $1000).  seed-if-absent never overwrites
    # a live balance, so bankrolls survive redeploys.
    try:
        from src import supa_ledger as _sl_boot
        _seeded = _sl_boot.seed_starting_bankrolls()
        if any(_seeded.values()):
            print(f"STARTUP: seeded ledger bankrolls {_seeded}",
                  flush=True, file=sys.stderr)
    except Exception as _se_boot:                                          # noqa: BLE001
        print(f"STARTUP WARNING: ledger seed failed: {_se_boot}",
              flush=True, file=sys.stderr)
    try:
        _sched = nightly_retrain.start()
        if _sched is None:
            print("STARTUP: scheduler not started (APScheduler unavailable or disabled)", flush=True, file=sys.stderr)
        else:
            # Add 8 AM and 12 PM ET auto-analysis jobs to the existing scheduler
            try:
                from apscheduler.triggers.cron import CronTrigger as _CronTrigger
                _ET = "America/New_York"
                _sched.add_job(
                    _run_auto_analysis_job,
                    _CronTrigger(hour=8,  minute=0, timezone=_ET),
                    id="auto_analysis_morning",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                    kwargs={"label": "morning"},
                )
                _sched.add_job(
                    _run_auto_analysis_job,
                    _CronTrigger(hour=12, minute=0, timezone=_ET),
                    id="auto_analysis_noon",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                    kwargs={"label": "noon"},
                )
                # 8:30 AM ET: Meta-Consensus -- ONE batched compound-beta review
                # of all props the 8:00 morning pipeline scored.
                _sched.add_job(
                    _run_meta_consensus_job,
                    _CronTrigger(hour=8, minute=30, timezone=_ET),
                    id="meta_consensus_morning",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                # 30-min standalone settlement during game hours (12 PM-1 AM ET).
                # _run_auto_settlement_job already self-gates to the same window
                # and is idempotent (settle_pending only touches pending picks),
                # so running it both here and inside the 15-min cycle is safe;
                # this standalone registration is what the boot health report
                # and /api/auto_settlement_status look for via get_job("auto_settlement").
                _sched.add_job(
                    _run_auto_settlement_job,
                    _CronTrigger(hour="12-23,0,1", minute="0,30", timezone=_ET),
                    id="auto_settlement",
                    replace_existing=True,
                    misfire_grace_time=600,
                    max_instances=1,
                )
                print("STARTUP: auto_settlement job scheduled — every 30 min, "
                      "12 PM-1 AM ET (game hours)",
                      flush=True, file=sys.stderr)
                # ── Nightly three-job cycle ──────────────────────────────
                # JOB 1  1:00 AM ET  final settlement
                # JOB 2  2:00 AM ET  full clear
                # JOB 3  3:00 AM ET  games prefetch (schedule only)
                _sched.add_job(
                    _run_job1_final_settlement,
                    _CronTrigger(hour=1, minute=0, timezone=_ET),
                    id="nightly_settlement",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                _sched.add_job(
                    _run_job2_full_clear,
                    _CronTrigger(hour=2, minute=0, timezone=_ET),
                    id="nightly_clear",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                _sched.add_job(
                    _run_job3_games_prefetch,
                    _CronTrigger(hour=3, minute=0, timezone=_ET),
                    id="nightly_prefetch",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                # 3:30 AM ET: two-pass overnight AI pre-generation (after the
                # 3 AM prefetch) so breakdowns are ready before the user wakes.
                _sched.add_job(
                    _run_overnight_ai_gen,
                    _CronTrigger(hour=3, minute=30, timezone=_ET),
                    id="overnight_ai_gen",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                # 4 AM ET: refresh the My Bets daily limit off the morning bankroll.
                _sched.add_job(
                    _run_personal_daily_limit_refresh,
                    _CronTrigger(hour=4, minute=0, timezone=_ET),
                    id="personal_daily_limit",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                # Belt-and-braces: if an older deploy registered the
                # retired midnight_reset job (persisted in a jobstore),
                # remove it so it can't fire alongside the new cycle.
                try:
                    _sched.remove_job("midnight_reset")
                    print("STARTUP: removed retired midnight_reset job",
                          flush=True, file=sys.stderr)
                except Exception:                                          # noqa: BLE001
                    pass
                print("STARTUP: auto-analysis jobs scheduled — 8:00 AM and 12:00 PM ET (full model re-analysis)", flush=True, file=sys.stderr)
                print("STARTUP: nightly cycle scheduled — JOB1 settle 1 AM, JOB2 clear 2 AM, JOB3 prefetch 3 AM ET", flush=True, file=sys.stderr)

                # Consolidated 15-min refresh cycle (keeps the historic
                # auto_props_refresh job id).  One coordinated pass during game
                # hours (11 AM-1 AM ET, i.e. hours 11-23 plus 0,1): schedule +
                # live scores → game odds (line-move flagging) → prop lines →
                # re-score → top-plays record → settlement → Groq summary queue.
                # This single cycle now also performs the intraday settlement
                # that used to live in the standalone 30-min auto_settlement job.
                try:
                    _sched.add_job(
                        _run_consolidated_refresh_cycle,
                        _CronTrigger(hour="11-23,0,1", minute="0,15,30,45",
                                     timezone=_ET),
                        id="auto_props_refresh",
                        replace_existing=True,
                        misfire_grace_time=600,
                        max_instances=1,
                    )
                    print(
                        "STARTUP: auto_props_refresh job scheduled — CONSOLIDATED "
                        "15-min cycle, every :00/:15/:30/:45 during 11 AM–1 AM ET "
                        "(schedule+scores → odds → props → re-score → top-plays → "
                        "settlement → AI summaries); settlement gated 12 PM–1 AM ET",
                        flush=True, file=sys.stderr,
                    )
                except Exception as _pe:
                    print(
                        f"STARTUP WARNING: could not add auto_props_refresh: {_pe}",
                        flush=True, file=sys.stderr,
                    )
            except Exception as _ae:
                print(f"STARTUP WARNING: could not add auto-analysis jobs: {_ae}", flush=True, file=sys.stderr)
            print("STARTUP: nightly retrain scheduler running — fires 2 AM ET", flush=True, file=sys.stderr)

            # Per-job manifest: print id + next_run_time + trigger for every
            # registered job so the deploy log gives an at-a-glance view
            # of what's actually going to fire.  Catches the case where
            # nightly_retrain.start() silently failed to register the 2 AM
            # job, or where DST shifted next_run_time unexpectedly.
            try:
                from zoneinfo import ZoneInfo as _ZI
                _et_tz = _ZI("America/New_York")
                print("STARTUP: scheduler job manifest --", flush=True, file=sys.stderr)
                for _job in _sched.get_jobs():
                    nxt = getattr(_job, "next_run_time", None)
                    nxt_s = (
                        nxt.astimezone(_et_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                        if nxt else "—"
                    )
                    print(f"  • {_job.id:<24s} next={nxt_s}  trigger={_job.trigger}",
                          flush=True, file=sys.stderr)
            except Exception as _me:                                      # noqa: BLE001
                print(f"STARTUP WARNING: could not enumerate scheduler jobs: {_me}",
                      flush=True, file=sys.stderr)
    except Exception as _sched_err:
        print(f"STARTUP WARNING: nightly retrain scheduler failed: {_sched_err}",
              flush=True, file=sys.stderr)
        _logger.warning("nightly retrain scheduler failed to start: %s", _sched_err)

# ── Persistent-cache startup steps (Issues 1 + 2 + 4) ───────────────────────
# 1. Ensure data/ exists before any file op (Railway can drop it on redeploy)
# 2. Purge any cache file / Supabase row whose date != today
# 3. Restore today's cache rows from Supabase to disk when local files are
#    missing (the common case right after a Railway redeploy)
_purge_stale_caches_on_boot()
_restore_caches_from_supabase_on_boot()

# 4. Model cache inventory + pre-download from Supabase.  Railway's
# filesystem is ephemeral so the .cache/*.joblib model snapshots are
# wiped on every redeploy.  src.model_cache_persist mirrors them to
# the app_cache table; this block fires the download up-front (so the
# first analyze sees them on disk + bypasses the retrain path) and
# prints a structured inventory with paths + sizes for both the local
# and Supabase view of each file.
def _model_cache_boot_inventory() -> None:
    try:
        from src import model_cache_persist as _persist
    except Exception as exc:                                              # noqa: BLE001
        print(f"MODEL CACHE INVENTORY: persist module import failed "
              f"({type(exc).__name__}: {exc})", flush=True, file=sys.stderr)
        return

    model_paths = [
        Path(".cache/model_baseball_mlb.joblib"),
        Path(".cache/model_run_line_mlb.joblib"),
        Path(".cache/model_totals_mlb.joblib"),
        Path(".cache/model_basketball_wnba.joblib"),
        Path(".cache/model_spread_wnba.joblib"),
        Path(".cache/model_totals_wnba.joblib"),
    ]

    print("MODEL CACHE INVENTORY: pre-analyze view of joblib snapshots",
          flush=True, file=sys.stderr)
    rows = _persist.inventory(model_paths)
    for r in rows:
        local_s = (
            f"local={r['local_size_bytes']:,}B"
            if r["exists_locally"] else "local=MISSING"
        )
        if r["supabase_on"]:
            sb_s = (
                f"supabase={r['supabase_size']:,}B"
                if r["supabase_present"] else "supabase=MISSING"
            )
        else:
            sb_s = "supabase=OFF"
        print(f"  MODEL CACHE: {r['path']}  {local_s}  {sb_s}",
              flush=True, file=sys.stderr)

    # Pre-download every file that's missing locally but present in
    # Supabase.  Without this, the first analyze run would still hit
    # the retrain path because the train_or_load .exists() check fires
    # before its own _persist.try_download call (we double-cover both
    # paths so this is just an optimization, not a correctness fix).
    n_downloaded = 0
    for r in rows:
        if r["exists_locally"]:
            continue
        if not r["supabase_present"]:
            continue
        if _persist.try_download(Path(r["path"])):
            n_downloaded += 1

    # Pre-upload every file that exists locally but is MISSING from
    # Supabase.  PR #67 wired upload() into _train so newly-trained
    # models always land in Supabase, BUT for any model file that was
    # already on disk when #67 first deployed, train_or_load took the
    # LOAD path (model_path.exists() == True) and never invoked the
    # upload helper.  That left those files stuck local-only -- a
    # Railway redeploy wipes them and the next boot has to retrain
    # from scratch.  This loop closes the gap by force-uploading any
    # local file that doesn't have a Supabase counterpart.  Idempotent:
    # once a file is in Supabase, the inventory check skips it next
    # boot.
    n_uploaded = 0
    for r in rows:
        if not r["exists_locally"]:
            continue
        if r["supabase_present"]:
            continue
        if not r["supabase_on"]:
            continue
        if _persist.upload(Path(r["path"])):
            n_uploaded += 1

    print(
        f"MODEL CACHE INVENTORY: pre-downloaded {n_downloaded} "
        f"file(s) from Supabase, pre-uploaded {n_uploaded} "
        f"file(s) to Supabase",
        flush=True, file=sys.stderr,
    )

    # If we did anything mutative, print a fresh inventory so the user
    # can confirm local + supabase columns now agree.  Skipped on
    # steady-state boots where nothing was transferred.
    if n_downloaded or n_uploaded:
        print(
            "MODEL CACHE INVENTORY: post-sync view",
            flush=True, file=sys.stderr,
        )
        for r in _persist.inventory(model_paths):
            local_s = (
                f"local={r['local_size_bytes']:,}B"
                if r["exists_locally"] else "local=MISSING"
            )
            if r["supabase_on"]:
                sb_s = (
                    f"supabase={r['supabase_size']:,}B"
                    if r["supabase_present"] else "supabase=MISSING"
                )
            else:
                sb_s = "supabase=OFF"
            print(f"  MODEL CACHE: {r['path']}  {local_s}  {sb_s}",
                  flush=True, file=sys.stderr)


_model_cache_boot_inventory()


# ─────────────────────────────────────────────────────────────────────────────
# Boot Health Report -- one-glance OK/FAIL per subsystem, printed last so
# the user's eye lands here when scrolling Railway logs.  All checks are
# read-only (no mutations, no remote calls beyond a tiny db status read);
# safe to run on every boot.
# ─────────────────────────────────────────────────────────────────────────────

def _next_run_iso(sched, job_id: str) -> str | None:
    """Look up a scheduled job's next-fire time as an ISO-8601 string."""
    if sched is None:
        return None
    try:
        job = sched.get_job(job_id)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time.isoformat()
    except Exception:                                                     # noqa: BLE001
        return None


def _boot_health_report() -> None:
    lines: list[str] = []

    def _row(label: str, ok: bool, detail: str = "") -> None:
        tag = "OK  " if ok else "FAIL"
        lines.append(f"  [{tag}] {label:<28s} {detail}")

    # ── 1. Python / scheduler runtime
    try:
        import apscheduler                                                # noqa: F401
        _row("APScheduler available", True, "")
    except Exception as exc:                                              # noqa: BLE001
        _row("APScheduler available", False, f"{type(exc).__name__}: {exc}")

    sched = getattr(nightly_retrain, "_scheduler", None)
    _row(
        "Scheduler started",
        sched is not None,
        "BackgroundScheduler running" if sched is not None
        else "nightly_retrain.start() returned None",
    )

    # ── 2. Supabase connectivity
    try:
        from src import db as _db
        sb_on = _db.is_supabase()
        sb_status = _db.status() if hasattr(_db, "status") else {}
    except Exception as exc:                                              # noqa: BLE001
        sb_on = False
        sb_status = {"error": f"{type(exc).__name__}: {exc}"}
    _row(
        "Supabase connection",
        sb_on,
        f"mode={'supabase' if sb_on else 'json-only'}  "
        f"client_ok={sb_status.get('supabase', False)}",
    )

    # ── 3. Odds API key
    odds_key_set = bool(os.getenv("ODDS_API_KEY")) and os.getenv("ODDS_API_KEY") != "your_odds_api_key_here"
    _row(
        "Odds API key",
        odds_key_set,
        "configured" if odds_key_set else "ODDS_API_KEY env var missing or placeholder",
    )

    # ── 4. Scheduler job inventory + next run times
    expected_jobs = (
        ("auto_analysis_morning", "8 AM analysis"),
        ("auto_analysis_noon",    "12 PM refresh"),
        ("auto_settlement",       "30-min settlement"),
        ("nightly_settlement",    "JOB1 final settlement 1 AM"),
        ("nightly_clear",         "JOB2 full clear 2 AM"),
        ("nightly_prefetch",      "JOB3 games prefetch 3 AM"),
        ("nightly_retrain",       "2 AM model retrain"),
    )
    if sched is not None:
        for job_id, friendly in expected_jobs:
            nxt = _next_run_iso(sched, job_id)
            _row(
                f"Job {friendly}",
                nxt is not None,
                f"id={job_id}  next={nxt or 'NOT SCHEDULED'}",
            )
    else:
        _row("Job schedule",     False, "scheduler not running -- jobs not inspectable")

    # ── 5. Ensemble picks file (today)
    try:
        if _ENSEMBLE_PICKS_FILE.exists():
            payload = json.loads(_ENSEMBLE_PICKS_FILE.read_text(encoding="utf-8"))
            date_in_file = payload.get("date")
            n_mlb  = len((payload.get("picks") or {}).get("mlb")  or [])
            n_wnba = len((payload.get("picks") or {}).get("wnba") or [])
            stale = date_in_file != _today_et()
            _row(
                "Ensemble picks file",
                not stale,
                f"path={_ENSEMBLE_PICKS_FILE}  date={date_in_file}  "
                f"mlb={n_mlb} wnba={n_wnba}  "
                f"{'STALE -- will reset on next analyze' if stale else 'fresh'}",
            )
        else:
            _row(
                "Ensemble picks file",
                False,
                f"path={_ENSEMBLE_PICKS_FILE}  (not yet created -- normal on cold boot)",
            )
    except Exception as exc:                                              # noqa: BLE001
        _row("Ensemble picks file", False, f"read error: {type(exc).__name__}: {exc}")

    # ── 6. Per-classifier tracker files
    for tracker in _PICKS_HISTORY_FILES:
        try:
            if tracker.exists():
                payload = json.loads(tracker.read_text(encoding="utf-8"))
                n_picks = len(payload.get("picks") or [])
                _row(
                    f"Tracker {tracker.name}",
                    True,
                    f"path={tracker}  size={tracker.stat().st_size:,}B  picks={n_picks}",
                )
            else:
                _row(
                    f"Tracker {tracker.name}",
                    True,    # absent is OK on cold boot; analyze will create
                    f"path={tracker}  (not yet created)",
                )
        except Exception as exc:                                          # noqa: BLE001
            _row(f"Tracker {tracker.name}", False, f"{type(exc).__name__}: {exc}")

    # ── 7. Ledger files + bankroll sanity
    for path, sport in (
        (Path("data/ledger.json"),      "mlb"),
        (Path("data/wnba_ledger.json"), "wnba"),
    ):
        if not path.exists():
            _row(f"Ledger {sport}", True, f"path={path}  (not yet created)")
            continue
        try:
            led = json.loads(path.read_text(encoding="utf-8"))
            mb  = float(led.get("model_bankroll", 0) or 0)
            pb  = float(led.get("personal_bankroll", 0) or 0)
            ob  = len(led.get("open_bets") or [])
            hi  = len(led.get("history")   or [])
            _row(
                f"Ledger {sport}",
                True,
                f"model=${mb:,.2f}  personal=${pb:,.2f}  open={ob}  history={hi}",
            )
        except Exception as exc:                                          # noqa: BLE001
            _row(f"Ledger {sport}", False, f"{type(exc).__name__}: {exc}")

    # ── 8. Persistent cache restored
    for ckey, path in (
        (_CACHE_KEY_SNAPSHOT,      _DAILY_SNAPSHOT_FILE),
        (_CACHE_KEY_ANALYSIS_MLB,  _ANALYSIS_CACHE_FILE),
        (_CACHE_KEY_ANALYSIS_WNBA, _WNBA_ANALYSIS_CACHE_FILE),
    ):
        _row(
            f"Cache {ckey}",
            True,
            f"local={'present' if path.exists() else 'missing'}  "
            f"({path.name})",
        )

    # Header + body in one stderr write so log readers see them
    # as one contiguous block instead of interleaved with other
    # boot prints.
    border = "=" * 78
    sys.stderr.write(
        "\n" + border + "\n"
        + "BOOT HEALTH REPORT\n"
        + border + "\n"
        + "\n".join(lines)
        + "\n" + border + "\n"
    )
    sys.stderr.flush()


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

def _boot_predictions_warmup() -> None:
    """Background-thread entry point: ensures today's no-odds predictions
    exist in Supabase for both sports on every boot, not just midnight.

    Each sport's prefetch is wrapped so one slow / failing sport
    doesn't block the other (e.g. ESPN slowness shouldn't keep MLB
    predictions stuck).

    Retries up to 3 times with a 30 s gap between attempts so the
    warmup eventually succeeds once the model joblib cache restore
    finishes and the predictor stack can build cleanly -- the
    previous one-shot version permanently gave up if the first
    attempt landed before MODEL CACHE INVENTORY had pre-downloaded
    the joblibs."""
    import threading as _th
    import time as _time

    _MAX_ATTEMPTS = 3
    _RETRY_DELAY  = 30  # seconds

    def _run() -> None:
        # Tiny pause so the BOOT HEALTH REPORT block finishes printing
        # before the prefetch log lines start interleaving.
        _time.sleep(2)

        today = _today_et()
        try:
            from src import db as _db
            sb_on = _db.is_supabase()
        except Exception:                                                  # noqa: BLE001
            sb_on = False

        _eprint(
            f"BOOT WARMUP: checking no-odds predictions for {today} "
            f"(supabase_on={sb_on})..."
        )

        # Track which sports still need a successful prefetch so the
        # retry loop only re-tries the failures, not the ones that
        # already wrote to cache.
        pending: set[str] = set()
        for sport in ("mlb", "wnba"):
            cached = {}
            try:
                cached = _read_no_odds_predictions(sport, today)
            except Exception:                                              # noqa: BLE001
                pass
            if cached:
                _eprint(
                    f"BOOT WARMUP [{sport.upper()}]: {len(cached)} "
                    f"cached prediction(s) found -- skipping prefetch"
                )
            else:
                pending.add(sport)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if not pending:
                break
            for sport in list(pending):
                try:
                    preds = _prefetch_no_odds_predictions(sport, today)
                    if preds:
                        _eprint(
                            f"BOOT WARMUP [{sport.upper()}]: attempt "
                            f"{attempt}/{_MAX_ATTEMPTS} -- prefetched "
                            f"{len(preds)} prediction(s)"
                        )
                        pending.discard(sport)
                        # Reset the negative cache so subsequent on-demand
                        # predicts in the schedule endpoint can use the
                        # now-loaded predictor.
                        _no_odds_predictor_failed[sport] = False
                    else:
                        _eprint(
                            f"BOOT WARMUP [{sport.upper()}]: attempt "
                            f"{attempt}/{_MAX_ATTEMPTS} returned 0 "
                            f"predictions -- model not yet available, "
                            f"retrying in {_RETRY_DELAY}s"
                        )
                        # Clear the failure flag so the next attempt
                        # actually re-tries instead of short-circuiting
                        # on the cached failure.
                        _no_odds_predictor_failed[sport] = False
                        # Drop the cached predictor so the next attempt
                        # rebuilds it (the cached one may have a half-
                        # loaded GameStore from the previous attempt).
                        _no_odds_predictor[sport] = None
                except (ImportError, NameError) as exc:
                    # Specifically the user-spec'd cases -- model module
                    # didn't load (likely still hydrating from the
                    # joblib cache restore that runs earlier in boot).
                    _eprint(
                        f"BOOT WARMUP [{sport.upper()}]: attempt "
                        f"{attempt}/{_MAX_ATTEMPTS} -- model not yet "
                        f"available, retrying in {_RETRY_DELAY}s "
                        f"({type(exc).__name__}: {exc})"
                    )
                    _no_odds_predictor_failed[sport] = False
                    _no_odds_predictor[sport] = None
                except Exception as exc:                                   # noqa: BLE001
                    _eprint(
                        f"BOOT WARMUP [{sport.upper()}]: attempt "
                        f"{attempt}/{_MAX_ATTEMPTS} FAILED -- "
                        f"{type(exc).__name__}: {exc}"
                    )
                    _no_odds_predictor_failed[sport] = False
                    _no_odds_predictor[sport] = None
            if pending and attempt < _MAX_ATTEMPTS:
                _time.sleep(_RETRY_DELAY)

        if pending:
            _eprint(
                f"BOOT WARMUP: gave up on {sorted(pending)} after "
                f"{_MAX_ATTEMPTS} attempts -- schedule endpoint will "
                f"keep retrying on-demand for individual game requests"
            )
        else:
            _eprint("BOOT WARMUP: all sports warmed successfully")

    _th.Thread(target=_run, name="boot-pred-warmup", daemon=True).start()
    _eprint(
        "BOOT WARMUP: prediction-warmup thread started (predictions "
        "will land in 30-90s; UI shows cached predictions immediately "
        "if Supabase has them)."
    )


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
