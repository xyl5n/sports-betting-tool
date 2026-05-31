"""
admin_diagnostics.py -- shared diagnostics + SharpAPI probe logic for the
admin surface.

Extracted from pages/admin.py:_run_diagnostics so the Flask /admin port
(app.py GET /api/admin/diagnostics) and the legacy NiceGUI admin page can
share one implementation.  Returns structured dicts -- {label, status,
detail} -- where status is one of "ok" | "warn" | "err" | "info", matching
the icon/colour map the front-end uses.

All probes are read-only: in-memory state reads, local file reads, a
quota-free Odds API /v4/sports GET, and Supabase cache reads.  No mutations,
no quota burn (the /v4/sports endpoint does not count against the plan).

`backend` is the imported app module (passed in so this stays import-cycle
free -- app.py imports this module, not the other way around).
"""
from __future__ import annotations

from typing import Any


def run_diagnostics(backend) -> list[dict[str, str]]:
    """Run every probe synchronously and return a list of
    {label, status, detail} dicts."""
    out: list[dict[str, str]] = []

    def _add(label: str, status: str, detail: str) -> None:
        out.append({"label": label, "status": status, "detail": detail})

    # 1. In-memory analysis state
    try:
        n_mlb  = len(backend._analysis_state.get("results")      or [])
        n_wnba = len(backend._wnba_analysis_state.get("results") or [])
        ts_mlb  = backend._analysis_state.get("last_analyzed_at")
        ts_wnba = backend._wnba_analysis_state.get("last_analyzed_at")
        ok = (n_mlb + n_wnba) > 0
        _add(
            "In-memory analysis state",
            "ok" if ok else "warn",
            f"MLB: {n_mlb} games (last {ts_mlb or '—'})  |  "
            f"WNBA: {n_wnba} games (last {ts_wnba or '—'})",
        )
    except Exception as exc:                                               # noqa: BLE001
        _add("In-memory analysis state", "err", f"{type(exc).__name__}: {exc}")

    # 1b. Games skipped during prediction (per-sport)
    for sport_label, state_attr in (
        ("MLB",  "_analysis_state"),
        ("WNBA", "_wnba_analysis_state"),
    ):
        try:
            state = getattr(backend, state_attr, {}) or {}
            sk = state.get("skipped") or []
            if not sk:
                continue
            preview = "  |  ".join(
                f"{s.get('matchup','?')} ({s.get('reason','?')})"
                for s in sk[:3]
            )
            more = f"  +{len(sk)-3} more" if len(sk) > 3 else ""
            _add(
                f"{sport_label} games skipped during prediction",
                "warn",
                f"{len(sk)} games dropped post-API.  {preview}{more}",
            )
        except Exception as exc:                                           # noqa: BLE001
            _add(f"{sport_label} skipped probe", "err",
                 f"{type(exc).__name__}: {exc}")

    # 2. Daily snapshot file
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/daily_snapshot.json")
        if not p.exists():
            _add("Daily snapshot file", "warn", "data/daily_snapshot.json -- missing")
        else:
            snap = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            snap_date = snap.get("date") or snap.get("mlb", {}).get("date")
            stale = snap_date != today
            mlb_n  = len((snap.get("mlb",  {}) or {}).get("results") or [])
            wnba_n = len((snap.get("wnba", {}) or {}).get("results") or [])
            status = "warn" if stale else ("ok" if (mlb_n + wnba_n) > 0 else "warn")
            _add(
                "Daily snapshot file",
                status,
                f"date={snap_date} (today={today})  MLB={mlb_n}  WNBA={wnba_n}",
            )
    except Exception as exc:                                               # noqa: BLE001
        _add("Daily snapshot file", "err", f"{type(exc).__name__}: {exc}")

    # 3. Per-sport analysis caches
    for sport, cache_path in (("MLB",  "data/analysis_cache.json"),
                              ("WNBA", "data/wnba_analysis_cache.json")):
        try:
            from pathlib import Path
            import json as _json
            p = Path(cache_path)
            if not p.exists():
                _add(f"{sport} analysis cache", "warn", f"{cache_path} -- missing")
                continue
            payload = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            stale = payload.get("date") != today
            n = len(payload.get("results") or [])
            _add(
                f"{sport} analysis cache",
                "warn" if stale else ("ok" if n > 0 else "warn"),
                f"{cache_path}  date={payload.get('date')} (today={today})  games={n}",
            )
        except Exception as exc:                                           # noqa: BLE001
            _add(f"{sport} analysis cache", "err", f"{type(exc).__name__}: {exc}")

    # 4. Daily picks file
    try:
        daily = backend.load_daily_picks() or {}
        picks = daily.get("picks") or {}
        n_game  = len(picks.get("game_picks") or [])
        n_props = len(picks.get("prop_picks") or [])
        total   = n_game + n_props
        _add(
            "Daily picks file",
            "ok" if total > 0 else "warn",
            f"data/daily_picks.json  Game={n_game}  Props={n_props}",
        )
    except Exception as exc:                                               # noqa: BLE001
        _add("Daily picks file", "err", f"{type(exc).__name__}: {exc}")

    # 5. Analysis timestamps file
    try:
        ts = backend._read_analysis_timestamps() or {}
        mlb_ts  = (ts.get("mlb")  or {}).get("analyzed_at")  or "—"
        wnba_ts = (ts.get("wnba") or {}).get("analyzed_at")  or "—"
        _add(
            "Analysis timestamps",
            "ok" if (mlb_ts != "—" or wnba_ts != "—") else "warn",
            f"MLB={mlb_ts}  WNBA={wnba_ts}",
        )
    except Exception as exc:                                               # noqa: BLE001
        _add("Analysis timestamps", "err", f"{type(exc).__name__}: {exc}")

    # 6. Supabase status
    try:
        from src import db as _db
        st = _db.status() or {}
        mode = st.get("mode", "json")
        sb_on = bool(st.get("supabase"))
        url_set = bool(st.get("url_set"))
        key_set = bool(st.get("key_set"))
        if mode == "supabase" and sb_on:
            level, detail = "ok", f"mode=supabase  url_set={url_set}  key_set={key_set}"
        elif url_set or key_set:
            level = "warn"
            detail = (f"mode={mode}  url_set={url_set}  key_set={key_set}  "
                      f"-- creds present but not connected")
        else:
            level = "info"
            detail = "mode=json  (SUPABASE_URL / SUPABASE_KEY not set -- JSON-only fallback)"
        _add("Supabase", level, detail)
    except Exception as exc:                                               # noqa: BLE001
        _add("Supabase", "err", f"{type(exc).__name__}: {exc}")

    # 6b. Supabase app_cache table
    try:
        from src import db as _db
        if not _db.is_supabase():
            _add("Supabase app_cache", "info", "skipped (Supabase not connected)")
        else:
            keys = ("daily_snapshot", "analysis_cache:mlb", "analysis_cache:wnba")
            bits = []
            anything = False
            for k in keys:
                row = _db.cache_get(k)
                if row:
                    anything = True
                    bits.append(f"{k}: date={row.get('date')}")
                else:
                    bits.append(f"{k}: —")
            _add("Supabase app_cache", "ok" if anything else "warn", " | ".join(bits))

            # 6c. Daily odds cache rows
            odds_keys = (
                ("MLB",  "odds_daily:baseball_mlb:h2h,spreads,totals:us"),
                ("WNBA", "odds_daily:basketball_wnba:h2h,spreads,totals:us"),
            )
            today = backend._today_et()
            for label, k in odds_keys:
                row = _db.cache_get(k)
                if row is None:
                    _add(
                        f"Odds daily cache: {label}",
                        "warn",
                        f"key={k}  -- no row yet, next analyze will burn 1 live API call",
                    )
                    continue
                row_date = row.get("date")
                data = row.get("data") or []
                n = len(data) if isinstance(data, list) else "?"
                fresh = (row_date == today)
                _add(
                    f"Odds daily cache: {label}",
                    "ok" if fresh else "warn",
                    f"date={row_date}  games={n}  "
                    + ("(today -- analyze will skip live API)" if fresh
                       else f"(stale -- today is {today}, will be refreshed)"),
                )
    except Exception as exc:                                               # noqa: BLE001
        msg = str(exc)
        hint = (" -- create the table via the SQL in src/db.py header"
                if "PGRST205" in msg or "does not exist" in msg.lower() else "")
        _add("Supabase app_cache", "err", f"{type(exc).__name__}: {msg}{hint}")

    # 7. Odds API key presence + live validity probe (quota-free /v4/sports)
    try:
        import os as _os
        key = _os.environ.get("ODDS_API_KEY") or ""
        if not key:
            _add("Odds API key", "err",
                 "ODDS_API_KEY env var not set -- analysis cannot fetch odds")
        else:
            try:
                import requests as _req
                resp = _req.get(
                    "https://api.the-odds-api.com/v4/sports",
                    params={"apiKey": key},
                    timeout=5,
                )
                used = resp.headers.get("x-requests-used", "?")
                rem  = resp.headers.get("x-requests-remaining", "?")
                if resp.status_code == 401:
                    _add("Odds API key", "err",
                         "401 Unauthorized -- key invalid / expired / revoked. "
                         "Update ODDS_API_KEY in Railway -> Variables.")
                elif resp.status_code == 429:
                    _add("Odds API key", "err",
                         f"429 quota exhausted (used={used}, remaining={rem}). "
                         f"Wait for monthly reset or upgrade plan.")
                elif resp.status_code >= 400:
                    _add("Odds API key", "warn",
                         f"HTTP {resp.status_code} from /v4/sports  "
                         f"(used={used}, remaining={rem})")
                else:
                    _add("Odds API key", "ok",
                         f"valid -- used={used}, remaining={rem}  "
                         f"(prefix={key[:4]}..., len={len(key)})")
                    try:
                        sports = resp.json() or []
                        wanted = {
                            "baseball_mlb":    "MLB",
                            "basketball_wnba": "WNBA",
                        }
                        seen = {
                            s.get("key"): s for s in sports
                            if isinstance(s, dict)
                        }
                        for sport_key, label in wanted.items():
                            entry = seen.get(sport_key)
                            if entry is None:
                                _add(
                                    f"Odds API: {label} availability",
                                    "err",
                                    f"sport_key '{sport_key}' NOT in your plan's "
                                    f"coverage list ({len(seen)} sports returned). "
                                    f"Upgrade plan or check "
                                    f"https://the-odds-api.com/sports-odds-data/",
                                )
                            elif not entry.get("active", True):
                                _add(
                                    f"Odds API: {label} availability",
                                    "warn",
                                    f"sport is in plan but currently inactive "
                                    f"(off-season / between updates). "
                                    f"title='{entry.get('title')}', "
                                    f"active=False -- no games will be returned",
                                )
                            else:
                                _add(
                                    f"Odds API: {label} availability",
                                    "ok",
                                    f"active=True, group={entry.get('group')}, "
                                    f"title='{entry.get('title')}' -- 0 games at "
                                    f"runtime just means no books have posted "
                                    f"lines yet (typical between ~midnight and "
                                    f"~10 AM ET)",
                                )
                    except Exception as exc:                               # noqa: BLE001
                        _add(
                            "Odds API: sport availability",
                            "warn",
                            f"could not parse /v4/sports response: "
                            f"{type(exc).__name__}: {exc}",
                        )
            except _req.Timeout:
                _add("Odds API key", "warn",
                     "probe timed out after 5s -- key untested, "
                     "analysis may still work")
            except Exception as exc:                                       # noqa: BLE001
                _add("Odds API key", "warn",
                     f"probe failed ({type(exc).__name__}: {exc}); "
                     f"env var is set (prefix={key[:4]}...)")
    except Exception as exc:                                               # noqa: BLE001
        _add("Odds API key", "err", f"{type(exc).__name__}: {exc}")

    # 8. Ledger files
    for sport, path in (("MLB", "data/ledger.json"), ("WNBA", "data/wnba_ledger.json")):
        try:
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                _add(f"{sport} ledger", "warn", f"{path} -- missing")
                continue
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
            s = led.get_summary()
            open_n = len(led.data.get("open_bets") or [])
            hist_n = len(led.data.get("history")   or [])
            _add(
                f"{sport} ledger",
                "ok",
                f"{path}  model=${s.get('model_bankroll',0):.2f}  "
                f"personal=${s.get('personal_bankroll',0):.2f}  "
                f"open={open_n}  history={hist_n}",
            )
        except Exception as exc:                                           # noqa: BLE001
            _add(f"{sport} ledger", "err", f"{type(exc).__name__}: {exc}")

    # 8b. Auto-analysis lock status
    try:
        lock = getattr(backend, "_auto_analysis_lock", None)
        if lock is None:
            _add("Auto-analysis lock", "info",
                 "lock object not found on backend module")
        else:
            held = lock.locked()
            _add(
                "Auto-analysis lock",
                "warn" if held else "ok",
                ("held -- the scheduled auto-analysis is currently running. "
                 "Manual Run buttons may appear stuck until this finishes.")
                if held else "free -- no scheduled analysis in flight",
            )
    except Exception as exc:                                               # noqa: BLE001
        _add("Auto-analysis lock", "err", f"{type(exc).__name__}: {exc}")

    # 9. Auto-analysis log
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/auto_analysis_log.json")
        if not p.exists():
            _add("Auto-analysis log", "info",
                 "data/auto_analysis_log.json -- not yet written")
        else:
            log = _json.loads(p.read_text(encoding="utf-8"))
            last = (log.get("last_run") or log.get("history", [{}])[-1]
                    if isinstance(log, dict) else None)
            _add("Auto-analysis log", "ok", f"last_run={last}")
    except Exception as exc:                                               # noqa: BLE001
        _add("Auto-analysis log", "err", f"{type(exc).__name__}: {exc}")

    return out


def probe_sharpapi() -> list[dict[str, str]]:
    """One-shot SharpAPI endpoint + auth-style probe.  Returns the same
    {label, status, detail} dict shape as run_diagnostics so the front-end
    renders both into the same results panel.

    Mirrors pages/admin.py:_sharpapi_probe_button -- gated on SHARPAPI_KEY,
    delegates to SharpApiClient.probe_endpoints()."""
    out: list[dict[str, str]] = []

    def _add(label: str, status: str, detail: str) -> None:
        out.append({"label": label, "status": status, "detail": detail})

    import os as _os
    key = (_os.environ.get("SHARPAPI_KEY") or "").strip()
    if not key:
        _add("SharpAPI", "warn",
             "SHARPAPI_KEY is not set in Railway -- nothing to probe.")
        return out

    try:
        from src.odds_client import SharpApiClient
        from src.cache import Cache
        client = SharpApiClient(key, Cache())
        rows: list[dict[str, Any]] = client.probe_endpoints() or []
    except Exception as exc:                                               # noqa: BLE001
        _add("SharpAPI probe", "err", f"{type(exc).__name__}: {exc}")
        return out

    _add("SharpAPI probe",
         "info",
         f"{len(rows)} endpoint/auth combos tried")
    for r in rows:
        label = f"{r.get('endpoint')}  [{r.get('auth')}]"
        status_code = r.get("status")
        if r.get("ok") and status_code == 200:
            level = "ok"
        elif status_code in (200, 401, 403):
            level = "warn"
        else:
            level = "err"
        sample = (r.get("sample") or "").replace("\n", " ")[:160]
        detail = f"HTTP {status_code}  ({r.get('bytes', 0)} bytes)"
        if sample:
            detail += f"  {sample}"
        _add(label, level, detail)
    return out
