"""boot.py -- startup / boot-sequence helpers (Phase D, PR #290).

The functions that run once at import time to validate credentials, purge
+ restore caches, inventory model artifacts, emit the boot health report,
and warm up predictions.  Full BFS closure: 8 functions, 524 lines.

IMPORTANT -- call sites stay in app.py:
    These functions are INVOKED at module scope in app.py's boot section
    (lines ~144 and ~7900+), in a specific order relative to
    `_sched = init(...)` and `_debug_thread.start()`.  Only the DEFINITIONS
    moved here; every call remains in app.py at its original position, so
    the boot sequence and its ordering are byte-for-byte preserved.
    boot.py itself contains NO module-scope calls -- importing it only
    defines the functions (side-effect-free), so `from boot import *`
    near app.py's top does not perturb the boot order.

Verified: no boot function references `_sched` (the app.py-local scheduler
handle), so none depend on scheduler-started state.

Direction (cycle-free -- boot is a pure sink):
    state/utils -> scheduler -> predictor -> boot -> app
    Nothing imports boot except app.py.  boot.py NEVER imports app.py.
    (_boot_predictions_warmup uses predictor._prefetch/_read_no_odds_*;
     several functions use scheduler._eprint / nightly_retrain /
     _supabase_cache_get.)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from state import *      # noqa: F401,F403  (cache file paths + keys, _ODDS_API_KEY)
from utils import *      # noqa: F401,F403  (_today_et)
from scheduler import *  # noqa: F401,F403  (_eprint, nightly_retrain, _supabase_cache_get)
from predictor import *  # noqa: F401,F403  (_prefetch_no_odds_predictions, _read_no_odds_predictions)

__all__ = [
    "_validate_odds_api_key_on_boot",
    "_ensure_data_dir",
    "_purge_stale_caches_on_boot",
    "_restore_caches_from_supabase_on_boot",
    "_model_cache_boot_inventory",
    "_next_run_iso",
    "_boot_health_report",
    "_boot_predictions_warmup",
]

# moved from app.py:89
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

# moved from app.py:296
def _ensure_data_dir() -> None:
    """Make sure data/ exists before any file op.  Railway's filesystem can
    drop directories between deployments, so re-creating at every import is
    cheaper than trying to detect when it's gone."""
    try:
        Path("data").mkdir(parents=True, exist_ok=True)
    except Exception as exc:                                              # noqa: BLE001
        print(f"STARTUP WARNING: could not create data/: {exc}",
              flush=True, file=sys.stderr)

# moved from app.py:388
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
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)
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

# moved from app.py:522
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

# moved from app.py:7974
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

# moved from app.py:8086
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

# moved from app.py:8099
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
    odds_key_set = bool(_ODDS_API_KEY) and _ODDS_API_KEY != "your_odds_api_key_here"
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

# moved from app.py:8282
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
