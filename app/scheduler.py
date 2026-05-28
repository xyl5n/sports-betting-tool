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
import sys
import traceback
from datetime import datetime

# Credential redactor used by _eprint to keep API keys out of Railway
# logs even when an exception message embeds `?apiKey=...`.
from src.redact import redact as _redact

# State + utils star-imports: the cluster appended below in PR #278a
# (hydrate_state, _read_daily_snapshot, etc.) references _analysis_state,
# _SNAPSHOT_ENABLED, _today_et, _filter_stale_games, etc.  Star-importing
# keeps the call sites verbatim (zero rewrites) and the direction stays
# one-way down: scheduler.py -> state.py / utils.py / src.*.
from state import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403

__all__ = [
    "_eprint",
    "_run_meta_consensus_job",
    "_run_personal_daily_limit_refresh",
    # PR #278a -- hydrate_state + _log_model_picks cluster
    "_read_daily_snapshot",
    "_snapshot_is_today",
    "hydrate_state",
    "_log_model_picks",
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
