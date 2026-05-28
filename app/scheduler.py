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

import sys
import traceback

# Credential redactor used by _eprint to keep API keys out of Railway
# logs even when an exception message embeds `?apiKey=...`.
from src.redact import redact as _redact

__all__ = [
    "_eprint",
    "_run_meta_consensus_job",
    "_run_personal_daily_limit_refresh",
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
