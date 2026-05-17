"""
nightly_retrain.py
==================
APScheduler-based background job that retrains MLB and WNBA models every
night at 2:00 AM Eastern time, inside the existing Flask process.

Public API
----------
    start()   -> BackgroundScheduler   # call once at app startup
    get_log() -> dict                  # structured log for /api/retrain_status
    LOG_FILE  -> Path                  # data/retrain_log.json

Log schema (data/retrain_log.json)
----------------------------------
{
  "runs": [                           # newest-first, capped at 30 entries
    {
      "run_id":      "...",           # UTC ISO timestamp used as unique key
      "started_at":  "YYYY-MM-DDTHH:MM:SSZ",
      "finished_at": "YYYY-MM-DDTHH:MM:SSZ",
      "duration_s":  float,
      "status":      "success" | "partial" | "error",
      "mlb":  { "exit_code": int, "duration_s": float,
                "stdout_tail": str,   "stderr_tail": str  },
      "wnba": { "exit_code": int, "duration_s": float,
                "stdout_tail": str,   "stderr_tail": str  }
    },
    ...
  ],
  "last_success": "YYYY-MM-DDTHH:MM:SSZ" | null
}
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# APScheduler is imported lazily inside start() so this module can always be
# imported even when APScheduler is not yet installed (e.g. during a Railway
# build where pip install is still in flight, or when the package is absent).
# A missing APScheduler causes start() to return None; everything else keeps
# working normally without the scheduler.

_logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
# Works whether the module is imported from app/ or app/src/.
_APP_DIR  = Path(__file__).parent.parent   # …/app/
LOG_FILE  = _APP_DIR / "data" / "retrain_log.json"

_MAX_RUNS        = 30    # keep at most this many entries in the log
_TAIL_CHARS      = 4000  # chars of stdout/stderr to capture per script
_PROC_TIMEOUT_S  = 7200  # 2 h hard ceiling per script

# Singleton — created by start(), read by get_log() and the /api/retrain_status route
_scheduler: Optional[Any] = None


# ── Log helpers ───────────────────────────────────────────────────────────────

def _load_log() -> dict:
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"runs": [], "last_success": None}


def _save_log(data: dict) -> None:
    """Atomic write; never raises."""
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="retrain_log_", suffix=".json.tmp",
                                   dir=str(LOG_FILE.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LOG_FILE)
    except Exception as exc:
        _logger.warning("retrain log write failed: %s", exc)
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail(text: str, n: int = _TAIL_CHARS) -> str:
    return text[-n:] if len(text) > n else text


# ── Script runner ─────────────────────────────────────────────────────────────

def _run_script(script: Path) -> dict:
    """
    Run a retrain script as a subprocess.

    Returns a dict with exit_code, duration_s, stdout_tail, stderr_tail.
    Never raises.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(_APP_DIR),
            capture_output=True,
            text=True,
            timeout=_PROC_TIMEOUT_S,
        )
        return {
            "exit_code":   proc.returncode,
            "duration_s":  round(time.monotonic() - t0, 1),
            "stdout_tail": _tail(proc.stdout or ""),
            "stderr_tail": _tail(proc.stderr or ""),
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code":   -1,
            "duration_s":  round(time.monotonic() - t0, 1),
            "stdout_tail": "",
            "stderr_tail": f"TIMEOUT after {_PROC_TIMEOUT_S}s",
        }
    except Exception as exc:
        return {
            "exit_code":   -2,
            "duration_s":  round(time.monotonic() - t0, 1),
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


# ── The scheduled job ─────────────────────────────────────────────────────────

def run_nightly_retrain() -> None:
    """
    Run both retrain scripts sequentially and append the result to the log.
    Designed to be called by APScheduler; safe to call manually for testing.
    """
    started = _now_z()
    t_start = time.monotonic()
    _logger.info("nightly retrain started")

    mlb_script  = _APP_DIR / "retrain_mlb_models.py"
    wnba_script = _APP_DIR / "retrain_wnba_models.py"

    mlb_result  = _run_script(mlb_script)
    wnba_result = _run_script(wnba_script)

    finished  = _now_z()
    duration  = round(time.monotonic() - t_start, 1)

    mlb_ok  = mlb_result["exit_code"]  == 0
    wnba_ok = wnba_result["exit_code"] == 0

    if mlb_ok and wnba_ok:
        status = "success"
    elif mlb_ok or wnba_ok:
        status = "partial"
    else:
        status = "error"

    run_entry = {
        "run_id":      started,
        "started_at":  started,
        "finished_at": finished,
        "duration_s":  duration,
        "status":      status,
        "mlb":         mlb_result,
        "wnba":        wnba_result,
    }

    _logger.info(
        "nightly retrain finished: status=%s  mlb_rc=%d  wnba_rc=%d  %.0fs",
        status, mlb_result["exit_code"], wnba_result["exit_code"], duration,
    )

    data = _load_log()
    data["runs"].insert(0, run_entry)         # newest first
    data["runs"] = data["runs"][:_MAX_RUNS]   # cap log length

    if status in ("success", "partial"):
        data["last_success"] = finished if mlb_ok or wnba_ok else data.get("last_success")

    _save_log(data)


# ── Public API ────────────────────────────────────────────────────────────────

def start(*, timezone_str: str = "America/New_York",
          hour: int = 2, minute: int = 0) -> Optional[Any]:
    """
    Create and start the BackgroundScheduler.

    Returns the scheduler, or None if APScheduler is not installed.
    Calling start() more than once returns the existing scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    # Lazy import — keeps this module importable even when APScheduler is absent.
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as _imp_err:
        print(
            f"STARTUP WARNING: APScheduler not available — nightly retrain scheduler "
            f"disabled. Install APScheduler to enable it. Error: {_imp_err}",
            file=sys.stderr, flush=True,
        )
        _logger.warning("APScheduler not installed; nightly retrain scheduler disabled")
        return None

    try:
        sched = BackgroundScheduler(timezone=timezone_str)
        sched.add_job(
            run_nightly_retrain,
            CronTrigger(hour=hour, minute=minute, timezone=timezone_str),
            id="nightly_retrain",
            replace_existing=True,
            # Allow the job to fire up to 1 hour late (e.g. if the process was
            # briefly down at 2 AM and comes back at 2:45 AM).
            misfire_grace_time=3600,
            max_instances=1,       # never run two retrains in parallel
        )
        sched.start()
        _scheduler = sched

        # Register a clean shutdown so APScheduler's threads don't linger.
        atexit.register(_shutdown)

        next_run = _next_run_iso()
        _logger.info(
            "nightly retrain scheduler started — fires at %02d:%02d %s, next run: %s",
            hour, minute, timezone_str, next_run,
        )
        return sched
    except Exception as _sched_err:
        print(
            f"STARTUP WARNING: APScheduler scheduler failed to initialize "
            f"(timezone='{timezone_str}'): {_sched_err}",
            file=sys.stderr, flush=True,
        )
        _logger.warning("APScheduler failed to initialize: %s", _sched_err)
        return None


def _shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


def _next_run_iso() -> Optional[str]:
    """Return the next scheduled fire time as an ISO string, or None."""
    if _scheduler is None:
        return None
    try:
        job = _scheduler.get_job("nightly_retrain")
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


def get_log() -> dict:
    """
    Return the full retrain log plus live scheduler metadata.

    Shape:
        {
          "runs":         [...],          # from retrain_log.json
          "last_success": str | null,
          "next_run":     str | null,     # next APScheduler fire time (ISO)
          "scheduler_running": bool,
        }
    """
    data = _load_log()
    data["next_run"]          = _next_run_iso()
    data["scheduler_running"] = (_scheduler is not None and _scheduler.running)
    return data
