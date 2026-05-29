"""scheduler.py -- APScheduler job functions (PR #277 starter).

This module is the home for the APScheduler-driven background jobs.
Only the genuinely-cleanly-movable pieces live here today; the
remaining 7 job functions and the APScheduler bootstrap block are
documented in migration_log.txt under "deferred until prerequisite
extractions land" -- they have hard dependencies on app.py-private
helpers (Flask `app.test_client()`, `hydrate_state`, `_log_model_picks`,
`_supabase_cache_delete`, etc.) that the strict move-only rule
forbids us from following without rewriting call signatures.

The names this module exports are imported star-style by app.py:
    from scheduler import *

Direction:
    scheduler.py -> state.py, utils.py, src.*    (one-way down)
    app.py       -> scheduler.py                 (one-way down)

Never import app.py from here; that would create a cycle.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import urllib.request as _urlreq
from datetime import datetime, timedelta, timezone
from pathlib import Path  # PR #292: bare Path used by _write_daily_snapshot

# Credential redactor used by _eprint to keep API keys out of Railway
# logs even when an exception message embeds `?apiKey=...`.
from src.redact import redact as _redact

# PR #292 -- latent-NameError fixes.  These names were referenced (bare) by
# functions moved into scheduler.py during the pre-try-aware PRs (#279/#280/
# #281/#284/#285) but never imported here, so every call raised NameError
# at runtime, silently swallowed by the functions' `except Exception`
# handlers (same class of bug as the SPORTS hotfix, PR #291).  All four
# src.* modules are verified leaves (no state/app/satellite imports).
from src.ledger import Ledger              # _void_postponed_mlb_bets, _run_auto_settlement_job, _rerun_single_game
from src.odds_client import OddsClient     # _run_auto_settlement_job, _detect_game_changes
from src.game_store import GameStore       # _ensure_no_odds_predictor
import src.ensemble_store as ensemble_store  # _run_job2_full_clear

# State + utils star-imports: the cluster appended below in PR #278a
# (hydrate_state, _read_daily_snapshot, etc.) references _analysis_state,
# _SNAPSHOT_ENABLED, _today_et, _filter_stale_games, etc.  Star-importing
# keeps the call sites verbatim (zero rewrites) and the direction stays
# one-way down: scheduler.py -> state.py / utils.py / src.*.
from state import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403
# PR #292: _rerun_single_game references bare _serialize (owned by
# serializer.py).  scheduler -> serializer is a cycle-free downward edge
# (serializer imports only state/utils/src.kelly, never scheduler).
from serializer import *  # noqa: F401,F403

# Parallel reference to the same logger app.py owns.  Python's logging
# module is a process-wide registry keyed by name, so getLogger("sports_betting")
# returns the same singleton both modules use -- this is NOT a duplicate
# logger, it's a second name bound to the same object.  We need this here
# because _debug_print (moved in PR #279) references `_logger.debug(...)`
# verbatim, and the "move-only, no rewrites" rule forbids editing the
# call site.  Decision documented in migration_log.txt under PR #279.
_logger = logging.getLogger("sports_betting")

# ── PR #282: nightly_retrain integration + APScheduler bootstrap ─────────
# Moved from app.py.  Both the stub class (used when src.nightly_retrain
# fails to import) and the import itself live here so the bootstrap
# init() function below can call nightly_retrain.start() without
# reaching back into app.py.  App.py routes that read nightly_retrain
# resolve the name via `from scheduler import *`.

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


def init(app, werkzeug_main):
    """Bootstrap APScheduler and register every scheduled job.

    Single call site -- app.py's module-level code invokes this exactly
    once after all routes are registered.  Body is the verbatim bootstrap
    block that used to live inline in app.py:10284-10475, with two minimal
    parameterizations approved as the FIRST AND ONLY APPROVED REWRITE in
    the decomposition series (see migration_log.txt PR #282):

      1. _werkzeug_main is a parameter now (was computed inline).
      2. app.debug is read from the passed Flask `app` argument.

    Two scheduled job functions still live in app.py because their move
    is blocked by separate issues (Flask app.test_client() in the case of
    _run_auto_analysis_job, the _serialize chain in the case of
    _run_consolidated_refresh_cycle).  Both are pulled in via a runtime
    import of `app` -- safe because init() is invoked from app.py's
    module-level code AFTER both functions are defined, so the names are
    already in sys.modules['app'].

    Returns the running APScheduler (or None if disabled / failed).
    """
    # Runtime import to break the otherwise-circular dependency.
    # See docstring above.  When _run_auto_analysis_job and
    # _run_consolidated_refresh_cycle eventually move to scheduler.py,
    # these two lines disappear.
    import app as _app_module
    _run_auto_analysis_job = _app_module._run_auto_analysis_job
    _run_consolidated_refresh_cycle = _app_module._run_consolidated_refresh_cycle

    # ── Nightly retrain scheduler ─────────────────────────────────────────────────
    print("STARTUP: all routes registered — starting scheduler...", flush=True, file=sys.stderr)

    # Start the APScheduler background job that fires every night at 2 AM ET.
    # Guard against Werkzeug's double-import when debug=True / use_reloader=True:
    # the reloader spawns a child process and sets WERKZEUG_RUN_MAIN=true there;
    # we only want the scheduler running in that child, not the parent watcher.
    _in_debug_mode  = app.debug
    if not _in_debug_mode or werkzeug_main:
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
    return _sched

__all__ = [
    "_eprint",
    "_run_meta_consensus_job",
    "_run_personal_daily_limit_refresh",
    # PR #278a -- hydrate_state + _log_model_picks cluster
    "_read_daily_snapshot",
    "_snapshot_is_today",
    "hydrate_state",
    "_log_model_picks",
    # PR #279 -- settlement bridge + overnight AI + budget helpers
    "_today_et_str",
    "_debug_print",
    "_fetch_mlb_linescore_raw",
    "_persist_daily_budget",
    "_run_overnight_ai_gen",
    "_void_postponed_mlb_bets",
    "_completed_games_from_scores",
    "_grade_model_trackers",
    "_statsapi_pick_et_date",
    "_statsapi_date_window",
    "_statsapi_schedule_index",
    "_fetch_mlb_statsapi_scores",
    # PR #280 -- _run_auto_settlement_job + its remaining helper cluster.
    # _STAT_LOOKUP_LOG_BUDGET is intentionally NOT in __all__ -- it's
    # module-private state, mutated by _reset_stat_lookup_log_budget and
    # _stat_lookup_log via `global` (which only works because both rebinders
    # AND the variable now live in this same module -- the rebind blocker
    # documented in PR #276's preamble is resolved by moving the whole
    # cluster together).
    "_reset_stat_lookup_log_budget",
    "_stat_lookup_log",
    "_model_pick_stat_lookup",
    "_final_scores_from",
    "_run_auto_settlement_job",
    # PR #281 -- schedule fetch + dedupe + normalize helpers, plus the
    # three nightly jobs.  _run_consolidated_refresh_cycle deferred
    # because its _rerun_single_game dep chains into _serialize, which
    # is a large Flask-coupled function not yet extracted.
    "_supabase_cache_delete",
    "_dedup_schedule_games",
    "_normalize_espn_wnba_scoreboard",
    "_normalize_mlb_schedule",
    "_normalize_wnba_schedule",
    "_fetch_raw_schedule",
    "_run_job1_final_settlement",
    "_run_job2_full_clear",
    "_run_job3_games_prefetch",
    # PR #282 -- APScheduler bootstrap.  `init` is the entry point;
    # `nightly_retrain` and `_NightlyRetrainStub` are exposed so app.py's
    # routes (admin nightly-retrain endpoints + admin diagnostics) can
    # keep referencing them by bare name via `from scheduler import *`.
    "_NightlyRetrainStub",
    "nightly_retrain",
    "init",
    # PR #284 -- _rerun_single_game + its full cascade subtree.
    # Unblocks _run_consolidated_refresh_cycle (the last non-Flask job
    # on the #277 deferred list).  Audit on PR #283 wrongly reported
    # only _serialize as the blocker; this PR ships the corrected
    # cascade of 5 helpers + the target function + 2 state dicts
    # (in state.py via PR #284/1).
    "_supabase_cache_set",
    "_supabase_cache_get",  # PR #290 -- co-located with set/delete
    "_write_daily_snapshot",
    "_ensure_no_odds_predictor",
    "_update_result_in_state",
    "_update_snapshot_game",
    "_rerun_single_game",
    # PR #285 -- consolidated refresh cycle + its full transitive closure.
    # Fully closes out PR #277's deferred list except for _run_auto_analysis_job
    # (which is irreducibly Flask-coupled via app.test_client()).
    "_probables_by_pair",
    "_refresh_schedule_and_scores",
    "_detect_game_changes",
    "_detect_prop_changes",
    "_run_consolidated_refresh_cycle",
]

# moved from app.py:613
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

# moved from app.py:9501
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

# moved from app.py:9691
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

# moved from app.py:722
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

# moved from app.py:739
def _snapshot_is_today(snap: dict) -> bool:
    """True if snapshot's date equals today in Eastern time."""
    if not _SNAPSHOT_ENABLED:
        return False
    try:
        return bool(snap) and snap.get("date") == _today_et()
    except Exception:
        return False

# moved from app.py:749
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

# moved from app.py:10198
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

# moved from app.py:3217
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
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

# moved from app.py:3231
def _today_et_str() -> str:
    """Return today's date in America/New_York as YYYY-MM-DD."""
    try:
        # zoneinfo is stdlib in Python 3.9+
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        # fallback: UTC offset -5 (close enough for date purposes)
        return (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d")

# moved from app.py:3242
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

# moved from app.py:7150
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

# moved from app.py:9789
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

# moved from app.py:9854
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

# moved from app.py:10031
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

# moved from app.py:10067
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

# moved from app.py:10241
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

# moved from app.py:10256
def _statsapi_date_window(base_iso):
    """[base-1, base, base+1] ET date strings, tolerating UTC/ET rollover and
    games logged the morning before a late start."""
    try:
        from datetime import date as _date
        b = _date.fromisoformat(base_iso)
        return [(b + timedelta(days=o)).isoformat() for o in (0, -1, 1)]
    except Exception:                                                     # noqa: BLE001
        return [base_iso]

# moved from app.py:10267
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

# moved from app.py:10308
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

# moved from app.py:9911
# Per-cycle budget for the verbose STAT-LOOKUP diagnostic.  A settlement pass
# can call the lookup hundreds of times (one per pending prop), so we log only
# the first few per pass -- enough to confirm in Railway logs WHAT season/date
# is queried and what comes back, without flooding.  Reset before each settle.
_STAT_LOOKUP_LOG_BUDGET = 0

# moved from app.py:9914
def _reset_stat_lookup_log_budget(n: int = 15) -> None:
    global _STAT_LOOKUP_LOG_BUDGET
    _STAT_LOOKUP_LOG_BUDGET = int(n)

# moved from app.py:9919
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

# moved from app.py:9988
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

# moved from app.py:10009
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

# moved from app.py:10032
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

    odds_key = _ODDS_API_KEY
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

# moved from app.py:350
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

# moved from app.py:2285
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

# moved from app.py:2413
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

# moved from app.py:2455
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

# moved from app.py:2500
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

# moved from app.py:2534
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

# moved from app.py:9516
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

# moved from app.py:9547
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

# moved from app.py:9679
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

# moved from app.py:317
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

# moved from app.py:699
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
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)

# moved from app.py:1742
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

        season = _SEASON
        sports_key = _API_SPORTS_KEY

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

# moved from app.py:9045
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

# moved from app.py:9057
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

# moved from app.py:9076
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

# moved from app.py:8582
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
    out = {"sports": 0}
    # live_score.fetch_live(backend, ...) reaches the in-process Flask test
    # client via ``backend.app.test_client()``, so ``backend`` must be the
    # app.py module (it exposes module-level ``app = Flask(__name__)``), NOT
    # the scheduler module -- the scheduler has no ``app`` attribute, so the
    # old ``_sys.modules.get(__name__)`` raised AttributeError inside
    # fetch_live and left the live-score cache permanently empty.  Runtime
    # import is safe: app is fully in sys.modules long before any refresh.
    import app as _app_module
    for sport in ("mlb", "wnba"):
        try:
            _fetch_raw_schedule(sport, _today_et())
            out["sports"] += 1
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"CYCLE step1 {sport} schedule error: {exc}")
        try:
            from components import live_score as _ls
            _ls.fetch_live(_app_module, sport)
        except Exception:                                                 # noqa: BLE001
            pass
    return out

# moved from app.py:8696
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

# moved from app.py:8720
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
    odds_key = _ODDS_API_KEY
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

# moved from app.py:8842
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

# moved from app.py:8894
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

# moved from app.py:307 (PR #290 -- co-located with _supabase_cache_set/_delete)
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

