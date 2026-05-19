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


def _validate_sharpapi_key_on_boot() -> None:
    """Mirror of _validate_odds_api_key_on_boot for SHARPAPI_KEY.

    SharpAPI is the new primary source (see src/odds_client.SharpApiClient).
    Surfacing presence + first/last-4 + whitespace problems here means a
    misconfigured SharpAPI key fails loud at boot, not silently inside the
    first /api/analyze call when the fallback path quietly takes over.
    """
    print("STARTUP: validating SharpAPI credentials...",
          flush=True, file=sys.stderr)
    key_raw = os.environ.get("SHARPAPI_KEY")
    if key_raw is None:
        print("STARTUP CRED-CHECK [SHARPAPI_KEY]: NOT SET.  "
              "Analysis will skip SharpAPI and fall back to The Odds API.",
              flush=True, file=sys.stderr)
        return
    key = key_raw.strip()
    problems: list[str] = []
    if not key:
        problems.append("value is empty after strip()")
    if key == "your_sharpapi_key_here":
        problems.append("value is still the .env.example placeholder text")
    if key != key_raw:
        problems.append(
            f"value has surrounding whitespace "
            f"(raw_len={len(key_raw)}, stripped_len={len(key)}) -- "
            f"trim it in Railway Variables")
    if " " in key:
        problems.append("value contains an embedded space")
    if problems:
        print(f"STARTUP CRED-CHECK [SHARPAPI_KEY]: PROBLEMS -- "
              f"{'; '.join(problems)}",
              flush=True, file=sys.stderr)
    else:
        print(f"STARTUP CRED-CHECK [SHARPAPI_KEY]: present, "
              f"len={len(key)}, prefix={key[:4]!r}, suffix={key[-4:]!r}",
              flush=True, file=sys.stderr)


_validate_sharpapi_key_on_boot()


def _probe_sharpapi_leagues_on_boot() -> None:
    """One-shot GET /leagues at startup so the canonical SharpAPI league
    identifiers (e.g. 'MLB', 'WNBA', 'NBA') appear in the Railway deploy
    log without anyone having to click anything in /admin.

    Cheap: a single 5-second request.  Skipped entirely when SHARPAPI_KEY
    isn't set.  Errors are swallowed -- the app boots regardless.
    """
    key = (os.environ.get("SHARPAPI_KEY") or "").strip()
    if not key:
        return  # already logged by the cred-check; don't double-up
    url = "https://api.sharpapi.io/api/v1/leagues"
    print(f"STARTUP: probing SharpAPI -- GET {url}  (auth: X-API-Key header)",
          flush=True, file=sys.stderr)
    try:
        import requests as _req
        resp = _req.get(url, headers={"X-API-Key": key}, timeout=5)
        body = (resp.text or "")[:3000]
        print(f"STARTUP SHARPAPI [/leagues]: status={resp.status_code}  "
              f"bytes={len(resp.content)}",
              flush=True, file=sys.stderr)
        print(f"STARTUP SHARPAPI [/leagues] body (first 3000 chars):\n{body}",
              flush=True, file=sys.stderr)
    except Exception as exc:                                              # noqa: BLE001
        print(f"STARTUP SHARPAPI [/leagues]: probe FAILED -- "
              f"{type(exc).__name__}: {exc}",
              flush=True, file=sys.stderr)


_probe_sharpapi_leagues_on_boot()


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

    for path in (_ANALYSIS_CACHE_FILE, _WNBA_ANALYSIS_CACHE_FILE, _DAILY_SNAPSHOT_FILE):
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
        except Exception as exc:                                          # noqa: BLE001
            # Don't fail boot over a corrupt cache file -- just nuke it
            # and let the next analysis run recreate it.
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"STARTUP WARNING: removed unreadable {path.name}: {exc}",
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
    """Read the timestamps file; return {} on any error."""
    try:
        return json.loads(_ANALYSIS_TIMESTAMPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_analysis_timestamp(sport: str, ts: datetime) -> None:
    """Persist a single sport's analysis timestamp.  Best-effort; never raises."""
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
    except Exception:
        pass


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


def _write_daily_snapshot(sport: str, payload: dict, ts: datetime) -> None:
    """
    Persist sport's analysis into the daily snapshot file.  Write-once per day
    per sport — if an entry already exists for today's ET date this is a no-op.
    Uses an atomic temp-file + rename so the file is never partially written.
    Thread-safe via _snapshot_lock.
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
            # Fresh day → start clean
            if snap.get("date") != today:
                snap = {"date": today}
            # Write-once: never overwrite an existing entry for this sport
            if snap.get(sport):
                return
            snap[sport] = {"analyzed_at": ts.isoformat(), **payload}
            # Step 4: atomic write — temp file then rename so the live file is
            # never in a partially-written state.
            raw_out = json.dumps(snap, indent=2, default=str)
            _DAILY_SNAPSHOT_TMP.write_text(raw_out, encoding="utf-8")
            _DAILY_SNAPSHOT_TMP.replace(_DAILY_SNAPSHOT_FILE)
            # Issue 4: also mirror to Supabase app_cache so the snapshot
            # survives Railway redeploys.  Best-effort -- silent on failure.
            _supabase_cache_set(_CACHE_KEY_SNAPSHOT, None, today, snap)
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
    "You are a professional sports analyst assistant. You have access to "
    "today's MLB and WNBA model predictions, pick data, confidence scores, "
    "edge percentages, SHAP feature importance values, pitcher stats, team "
    "stats, and betting lines. You answer questions about sports picks, "
    "player performance, team matchups, betting strategy, and sports data "
    "only. If the user asks anything unrelated to sports you respond with "
    "I can only answer sports related questions about picks, players, "
    "teams, and betting data. You never use markdown formatting like "
    "asterisks for bold or pound signs for headers in your responses "
    "because they display as raw symbols on this interface. Use plain "
    "text only with line breaks for separation. When referencing picks "
    "always include the confidence level, edge percentage, and the key "
    "factors the model used to make the pick based on the SHAP values "
    "available."
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
        if a_sp_name:
            sp_parts.append(f"{a_sp_name} ERA:{a_sp.get('era', '?')} WHIP:{a_sp.get('whip', '?')}")
        if h_sp_name:
            sp_parts.append(f"{h_sp_name} ERA:{h_sp.get('era', '?')} WHIP:{h_sp.get('whip', '?')}")
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
    """Return current model-bets settings.  Always returns a complete dict."""
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

    # Adjust displayed probability (floor at 0.48)
    pick_prob_adj = max(0.48, pick_prob - conf_red)
    pick_edge_adj = pick_prob_adj - (market_prob if pick_side == "home" else 1.0 - market_prob)

    # ML confidence tier — pure probability of the picked outcome, no edge
    # or model-agreement input.  Strong > 0.62, Moderate 0.52-0.62, Low < 0.52.
    ml_conf = confidence_tier_from_prob(pick_prob_adj)

    # Edge is computed independently and gates value separately from tier.
    is_value = (
        ml_conf in ("strong", "moderate") and
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
        # force_refresh threads from the /api/analyze request body all the
        # way down to bypass the daily Supabase cache when the user explicitly
        # asks for a fresh fetch from the admin Force Refresh button.
        games_pre_filter = odds_client.get_odds(
            sport_key=sport_cfg.odds_key, force_refresh=force_refresh,
        )
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
    bankrolls to their starting values.  Model files / analysis caches /
    API settings are untouched.  Returns {"success": true, "wiped": [...]}.
    """
    try:
        sport = (request.json or {}).get("sport", "").strip().lower()
        if sport not in ("mlb", "wnba", "both"):
            return jsonify({"success": False, "error": "sport must be 'mlb', 'wnba', or 'both'"}), 400

        sports = ["mlb", "wnba"] if sport == "both" else [sport]
        paths  = {
            "mlb":  Path("data/ledger.json"),
            "wnba": Path("data/wnba_ledger.json"),
        }
        wiped = []
        for s in sports:
            path = paths[s]
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except Exception:
                data = {}
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
        return jsonify({"success": True, "wiped": wiped})
    except Exception as exc:
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


@app.route("/api/admin/reset/model_record", methods=["POST"])
def admin_reset_model_record():
    """Reset Model Record:
       - drop all settled MODEL history (confirmed=False entries)
       - keeps personal/confirmed history rows
       - keeps every open_bet (both model + personal sides)
       - keeps model_bankroll + personal_bankroll dollar amounts
    """
    try:
        def _mut(data: dict) -> int:
            hist = data.get("history") or []
            kept = [h for h in hist if h.get("confirmed")]
            removed = len(hist) - len(kept)
            data["history"] = kept
            return removed
        return jsonify({"success": True, "removed": _reset_each_ledger(_mut)})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/model_bankroll", methods=["POST"])
def admin_reset_model_bankroll():
    """Reset Model Bankroll:
       - model_bankroll <- model_starting_bankroll (defaults to 1000)
       - drop unconfirmed (model-only) open_bets so the bankroll matches
       - keeps history + personal_bankroll untouched
    """
    try:
        def _mut(data: dict) -> int:
            start = float(data.get("model_starting_bankroll", 1000.0))
            data["model_bankroll"] = start
            opens = data.get("open_bets") or []
            kept  = [b for b in opens if b.get("confirmed")]
            removed = len(opens) - len(kept)
            data["open_bets"] = kept
            return removed
        return jsonify({"success": True, "removed_open_bets": _reset_each_ledger(_mut)})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/confidence_record", methods=["POST"])
def admin_reset_confidence_record():
    """Reset Confidence Record:
       - sets confidence_tier=None on every settled-history row (both
         model + personal) so the Confidence Performance card recomputes
         to Strong/Moderate/Low all at 0-0
       - leaves W/L counters intact -- they aggregate from result, not tier
       - leaves bankrolls + open_bets untouched
    """
    try:
        def _mut(data: dict) -> int:
            cleared = 0
            for h in (data.get("history") or []):
                if h.get("confidence_tier") is not None:
                    h["confidence_tier"] = None
                    cleared += 1
            return cleared
        return jsonify({"success": True, "cleared": _reset_each_ledger(_mut)})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc))}), 500


@app.route("/api/admin/reset/my_bets_record", methods=["POST"])
def admin_reset_my_bets_record():
    """Reset My Bets Record:
       - drop all settled PERSONAL history (confirmed=True entries)
       - keeps model history rows
       - keeps every open_bet
       - keeps both bankroll dollar amounts
    """
    try:
        def _mut(data: dict) -> int:
            hist = data.get("history") or []
            kept = [h for h in hist if not h.get("confirmed")]
            removed = len(hist) - len(kept)
            data["history"] = kept
            return removed
        return jsonify({"success": True, "removed": _reset_each_ledger(_mut)})
    except Exception as exc:                                              # noqa: BLE001
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
    """Single endpoint the Admin sub-page polls for header status fields."""
    try:
        ts = _read_analysis_timestamps()
        mlb_ts  = (ts.get("mlb")  or {}).get("analyzed_at")
        wnba_ts = (ts.get("wnba") or {}).get("analyzed_at")
        try:
            from src import db as _db
            db_status = _db.status()
        except Exception:
            db_status = {"mode": "json"}
        return jsonify({
            "mlb_analyzed_at":  mlb_ts,
            "wnba_analyzed_at": wnba_ts,
            "db":               db_status,
        })
    except Exception as exc:
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
    Wipe today's non-confirmed model picks for the chosen sport(s) and
    refund their stakes to model_bankroll.  Confirmed (personal) bets and
    settled history are untouched.  Body: {sport: "mlb"|"wnba"|"both"}.
    """
    try:
        from src.daily_picks import reset_today_model_bets
        sport = (request.json or {}).get("sport", "").strip().lower()
        if sport not in ("mlb", "wnba", "both"):
            return jsonify({"success": False, "error": "sport must be 'mlb', 'wnba', or 'both'"}), 400
        sports = ["mlb", "wnba"] if sport == "both" else [sport]
        paths = {"mlb": "data/ledger.json", "wnba": "data/wnba_ledger.json"}
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        removed_by_sport: dict[str, int] = {}
        for s in sports:
            led = Ledger(path=paths[s], starting_bankroll=1000.0)
            n = reset_today_model_bets(led, today_str)
            led.save()
            removed_by_sport[s] = n
        return jsonify({"success": True, "removed": removed_by_sport})
    except Exception as exc:                                              # noqa: BLE001
        return jsonify({"success": False, "error": _redact(str(exc)), "detail": _redact(traceback.format_exc())}), 500


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
    # Tier from the picked-outcome probability (model_p), not model agreement.
    ml_conf = confidence_tier_from_prob(model_p)
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
    sport_cfg = SPORTS["wnba"]
    ledger   = Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)

    for bet in ledger.data["open_bets"]:
        if bet["game_id"] == game_id and bet.get("bet_type", "single") == "single":
            if bet["confirmed"]:
                return jsonify({"error": "Already confirmed"}), 409
            _, conf_amt = ledger.kelly_amounts(bet["model_prob"], bet["american_odds"])
            conf_amt = round(conf_amt, 2)
            bet["confirmed"]        = True
            bet["confirmed_amount"] = conf_amt
            if conf_amt > 0:
                ledger.data["personal_bankroll"] = round(
                    ledger.data["personal_bankroll"] - conf_amt, 2
                )
            ledger.save()
            return jsonify({"success": True, "confirmed_amount": conf_amt})

    raw = next(
        (r for r in _wnba_analysis_state["results"]
         if r.get("game", {}).get("id") == game_id),
        None,
    )
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
        games_pre_filter = odds_client.get_odds(
            sport_key="basketball_wnba", force_refresh=force_refresh,
        )
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
            mlb_games = len(data.get("results", []))
            mlb_picks = sum(1 for r in data.get("results", []) if r.get("value_pick"))
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
            wnba_games = len(data.get("results", []))
            wnba_picks = sum(1 for r in data.get("results", []) if r.get("value_pick"))
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

    if not overall_ok and not is_retry:
        _schedule_auto_retry(label)


def _run_midnight_reset() -> None:
    """
    Midnight ET reset — clear all game data so the new day starts clean.
    Deletes analysis caches, snapshot, timestamps, and odds API cache files.
    In-memory state is also zeroed so the UI shows "Never run" until analysis
    is triggered manually or by the 8 AM auto-analysis job.
    """
    _eprint("MIDNIGHT-RESET: clearing all game data for new day")
    try:
        # Delete disk analysis caches
        for _path in (_ANALYSIS_CACHE_FILE, _WNBA_ANALYSIS_CACHE_FILE):
            try:
                _path.unlink(missing_ok=True)
            except Exception:
                pass
        # Clear daily snapshot (both sports at once)
        with _snapshot_lock:
            try:
                _DAILY_SNAPSHOT_FILE.unlink(missing_ok=True)
                _DAILY_SNAPSHOT_TMP.unlink(missing_ok=True)
            except Exception:
                pass
        # Clear analysis timestamps file
        try:
            _ANALYSIS_TIMESTAMPS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        # Zero in-memory analysis states
        _analysis_state["results"]          = []
        _analysis_state["parlays"]          = {}
        _analysis_state["last_analyzed_at"] = None
        _analysis_state["last_analysis_meta"] = {}
        _wnba_analysis_state["results"]          = []
        _wnba_analysis_state["parlays"]          = {}
        _wnba_analysis_state["last_analyzed_at"] = None
        _wnba_analysis_state["last_analysis_meta"] = {}
        # Evict odds API cache so fresh data is fetched on the next run
        for _odds_key in (
            "odds_baseball_mlb_h2h,spreads,totals_us",
            "odds_basketball_wnba_h2h,spreads,totals_us",
        ):
            try:
                _cache.invalidate(_odds_key)
            except Exception:
                pass
        # Issue 4: also wipe the Supabase mirror so a redeploy after
        # midnight doesn't restore yesterday's data from cache.
        for _ckey in (_CACHE_KEY_SNAPSHOT, _CACHE_KEY_ANALYSIS_MLB, _CACHE_KEY_ANALYSIS_WNBA):
            _supabase_cache_delete(_ckey)
        _eprint("MIDNIGHT-RESET: complete — new day ready")
    except Exception as _mre:
        _eprint(f"MIDNIGHT-RESET: unexpected error: {_mre}")


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


def _run_auto_settlement_job() -> None:
    """
    APScheduler callback: every 30 min.
    Gated to 11 AM–2 AM ET (game hours). Settles completed bets via Odds API
    scores; voids postponed MLB games via MLB Stats API. Logs summary to stderr.
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
    # Allow 11:00–23:59 and 00:00–02:59
    in_window = (et_hour >= 11) or (et_hour <= 2)
    if not in_window:
        return

    odds_key = os.getenv("ODDS_API_KEY", "")
    if not odds_key or odds_key == "your_odds_api_key_here":
        return

    _eprint(f"AUTO-SETTLE: checking at {now_et.strftime('%H:%M ET')}")
    oc = OddsClient(odds_key, _cache)

    settled: list = []

    # ── MLB ───────────────────────────────────────────────────────────────────
    try:
        _mlb_ldr = Ledger(path="data/ledger.json", starting_bankroll=250)
        if _mlb_ldr.data.get("open_bets"):
            # Invalidate stale scores cache so we always see fresh completions
            _cache.invalidate("scores_baseball_mlb_3")
            _new = _mlb_ldr.settle(oc, "baseball_mlb")
            settled.extend(_new)
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: MLB error: {type(_exc).__name__}: {_exc}")

    # ── WNBA ──────────────────────────────────────────────────────────────────
    try:
        _wnba_ldr = Ledger(path="data/wnba_ledger.json", starting_bankroll=250)
        if _wnba_ldr.data.get("open_bets"):
            _cache.invalidate("scores_basketball_wnba_3")
            _new = _wnba_ldr.settle(oc, "basketball_wnba")
            settled.extend(_new)
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: WNBA error: {type(_exc).__name__}: {_exc}")

    # ── Postponed games (MLB Stats API) ───────────────────────────────────────
    voided: list = []
    try:
        voided = _void_postponed_mlb_bets()
    except Exception as _exc:
        _eprint(f"AUTO-SETTLE: postponed check error: {type(_exc).__name__}: {_exc}")

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

    # ── Update state ──────────────────────────────────────────────────────────
    with _auto_settlement_lock:
        _auto_settlement_state.update({
            "last_ran_at":  datetime.now(timezone.utc).isoformat(),
            "last_settled": len(settled),
            "last_wins":    wins,
            "last_losses":  losses,
            "last_voided":  len(voided),
        })


# ── Nightly retrain scheduler ─────────────────────────────────────────────────
print("STARTUP: all routes registered — starting scheduler...", flush=True, file=sys.stderr)

# Start the APScheduler background job that fires every night at 2 AM ET.
# Guard against Werkzeug's double-import when debug=True / use_reloader=True:
# the reloader spawns a child process and sets WERKZEUG_RUN_MAIN=true there;
# we only want the scheduler running in that child, not the parent watcher.
_werkzeug_main = os.environ.get("WERKZEUG_RUN_MAIN", "false") == "true"
_in_debug_mode  = app.debug
if not _in_debug_mode or _werkzeug_main:
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
                # Settlement runs every 30 minutes BUT only during the
                # game-hours window (11 AM ET through 2 AM ET the next
                # morning).  Cron hour syntax "11-23,0-2" covers that
                # exact range so we don't burn CPU + Odds API quota
                # polling for completed scores at 4 AM ET when nothing
                # is in play.
                _sched.add_job(
                    _run_auto_settlement_job,
                    _CronTrigger(hour="11-23,0-2", minute="0,30",
                                 timezone=_ET),
                    id="auto_settlement",
                    replace_existing=True,
                    misfire_grace_time=1800,
                    max_instances=1,
                )
                _sched.add_job(
                    _run_midnight_reset,
                    _CronTrigger(hour=0, minute=0, timezone=_ET),
                    id="midnight_reset",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    max_instances=1,
                )
                print("STARTUP: auto-analysis jobs scheduled — 8:00 AM and 12:00 PM ET", flush=True, file=sys.stderr)
                print("STARTUP: auto-settlement job scheduled — every 30 min during 11 AM–2 AM ET window", flush=True, file=sys.stderr)
                print("STARTUP: midnight reset job scheduled — 12:00 AM ET", flush=True, file=sys.stderr)
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

print("STARTUP: app ready", flush=True, file=sys.stderr)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
