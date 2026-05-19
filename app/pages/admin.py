"""
Admin page -- operational controls for analysis, models, and bets.

Mirrors the legacy More -> Admin sub-page in templates/index.html.
Every action is driven by the Flask test client (backend.app.test_client()),
which invokes the existing /api/admin/* routes in-process with no HTTP
hop and no modifications to app.py.

Sections (top to bottom)
------------------------
  ANALYSIS      Run analysis for MLB / WNBA / Both; last-run timestamps.
  MODELS        Refresh models with cached odds; clear today's snapshot.
  MODEL BETS    Per-sport auto-pick toggles; re-pick / reset today's picks;
                reset model bankroll.
  MY BETS       Wipe tracked bets per sport; set personal bankroll.
  SYSTEM        Read-only: Supabase + DB mode.

Long-running calls (Run Analysis, Refresh Models) are dispatched via
asyncio.to_thread so the NiceGUI event loop stays responsive and the
button shows a spinner while the work runs.  Destructive actions
(wipe / reset) go through a confirmation dialog.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav


_ET = ZoneInfo("America/New_York")


def register(backend) -> None:
    @ui.page("/admin")
    def admin_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_ADMIN)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                ui.label("ADMIN").classes("page-title").style(
                    f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
                )

                # status_holder doubles as the SYSTEM card -- declared
                # first so it sits at the top of the page (last-analyzed
                # timestamps are the first thing the user wants to see),
                # populated by _refresh() below after every mutation.
                status_holder = ui.column().classes("w-full")
                _refresh = lambda: _render_status(backend, status_holder)

                _section_analysis(backend, _refresh)
                _section_models(backend, _refresh)
                _section_model_bets(backend, _refresh)
                _section_my_bets(backend, _refresh)
                _section_diagnostics(backend)
                _refresh()
        bottom_nav.render(active=t.TAB_ADMIN)


# ───────────────────────────────────────────────────────────────────────────
#  Backend invocation helper -- Flask test client over the imported app
# ───────────────────────────────────────────────────────────────────────────

def _call(backend, method: str, path: str, body: dict | None = None) -> tuple[bool, dict, int]:
    """Invoke an /api/* route in-process via Flask's test client.

    Returns (ok, payload, status_code).  ok=False either when the HTTP
    status is >= 400 or when the JSON payload has {"success": false}.
    payload is always a dict (empty on parse error).
    """
    client = backend.app.test_client()
    fn = client.post if method.upper() == "POST" else client.get
    try:
        resp = fn(path, json=body or {})
        try:
            data = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                 # noqa: BLE001
            data = {}
        ok = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500


# ───────────────────────────────────────────────────────────────────────────
#  Section: ANALYSIS
# ───────────────────────────────────────────────────────────────────────────

def _section_analysis(backend, refresh) -> None:
    with _card("ANALYSIS", "Fetch odds, run models, regenerate today's picks."):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Run MLB Analysis",
                "POST", "/api/analyze",
                body={"bankroll": 250},
                spinner_msg="Running MLB analysis...",
                done_msg=lambda d: f"MLB: analyzed {len(d.get('results') or [])} games.",
                refresh_status=refresh,
                style="primary",
            )
            _async_button(
                backend, "Run WNBA Analysis",
                "POST", "/api/wnba/analyze",
                body={"bankroll": 1000},
                spinner_msg="Running WNBA analysis...",
                done_msg=lambda d: f"WNBA: analyzed {len(d.get('results') or [])} games.",
                refresh_status=refresh,
                style="primary",
            )
            _run_both_button(backend, refresh)


def _run_both_button(backend, refresh) -> None:
    btn = ui.button("Run Both").props("no-caps unelevated").style(
        f"background: {t.PRIMARY}; color: {t.BG}; "
        f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
    )

    async def _click():
        btn.props("loading")
        btn.disable()
        try:
            ui.notify("Running MLB + WNBA analysis...", type="ongoing")
            ok_mlb,  d_mlb,  _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/analyze", {"bankroll": 250})
            ok_wnba, d_wnba, _ = await asyncio.to_thread(
                _call, backend, "POST", "/api/wnba/analyze", {"bankroll": 1000})
            msgs = []
            if ok_mlb:  msgs.append(f"MLB: {len(d_mlb.get('results') or [])} games")
            else:       msgs.append(f"MLB failed: {d_mlb.get('error') or 'unknown'}")
            if ok_wnba: msgs.append(f"WNBA: {len(d_wnba.get('results') or [])} games")
            else:       msgs.append(f"WNBA failed: {d_wnba.get('error') or 'unknown'}")
            kind = "positive" if (ok_mlb and ok_wnba) else "warning"
            ui.notify(" | ".join(msgs), type=kind, multi_line=True)
            refresh()
        finally:
            btn.props(remove="loading")
            btn.enable()

    btn.on("click", _click)


# ───────────────────────────────────────────────────────────────────────────
#  Section: MODELS
# ───────────────────────────────────────────────────────────────────────────

def _section_models(backend, refresh) -> None:
    with _card("MODELS", "Re-run predictions against cached odds; clear today's snapshot."):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Refresh Models (cached odds)",
                "POST", "/api/refresh_models",
                spinner_msg="Re-running predictions on cached odds...",
                done_msg=lambda d: "Models refreshed against cached odds.",
                refresh_status=refresh,
            )
            _async_button(
                backend, "Clear MLB Snapshot",
                "POST", "/api/reset-sport",
                body={"sport": "mlb"},
                spinner_msg="Clearing MLB snapshot...",
                done_msg=lambda d: d.get("message") or "MLB snapshot cleared.",
                refresh_status=refresh,
                style="warn",
            )
            _async_button(
                backend, "Clear WNBA Snapshot",
                "POST", "/api/reset-sport",
                body={"sport": "wnba"},
                spinner_msg="Clearing WNBA snapshot...",
                done_msg=lambda d: d.get("message") or "WNBA snapshot cleared.",
                refresh_status=refresh,
                style="warn",
            )


# ───────────────────────────────────────────────────────────────────────────
#  Section: MODEL BETS
# ───────────────────────────────────────────────────────────────────────────

def _section_model_bets(backend, refresh) -> None:
    # Pull current toggle state up-front so the switches render correctly
    ok, data, _ = _call(backend, "GET", "/api/admin/model/settings")
    settings = (data or {}).get("settings") or {"mlb_enabled": True, "wnba_enabled": True}

    with _card(
        "MODEL BETS",
        "Top 5 by confidence per bet type. Auto-runs after each analysis for enabled sports.",
    ):
        _toggle_row(
            backend, "MLB auto-picks",
            "Include MLB in the model's auto-picks",
            field="mlb_enabled", initial=bool(settings.get("mlb_enabled")),
        )
        _toggle_row(
            backend, "WNBA auto-picks",
            "Include WNBA in the model's auto-picks",
            field="wnba_enabled", initial=bool(settings.get("wnba_enabled")),
        )

        ui.label("Re-pick").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _async_button(
                backend, "Re-pick Both",
                "POST", "/api/admin/model/repick", body={"sport": "both"},
                spinner_msg="Re-picking model picks...",
                done_msg=lambda d: "Model picks regenerated.",
                refresh_status=refresh,
                style="primary",
            )
            _async_button(
                backend, "Re-pick MLB",
                "POST", "/api/admin/model/repick", body={"sport": "mlb"},
                spinner_msg="Re-picking MLB...",
                done_msg=lambda d: "MLB model picks regenerated.",
                refresh_status=refresh,
            )
            _async_button(
                backend, "Re-pick WNBA",
                "POST", "/api/admin/model/repick", body={"sport": "wnba"},
                spinner_msg="Re-picking WNBA...",
                done_msg=lambda d: "WNBA model picks regenerated.",
                refresh_status=refresh,
            )

        ui.label("Reset today's picks").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _confirm_button(
                backend, "Reset MLB",
                "Wipe today's MLB model picks and refund their stakes?",
                "POST", "/api/admin/model/reset", body={"sport": "mlb"},
                done_msg=lambda d: f"MLB picks reset. Removed: {(d.get('removed') or {}).get('mlb', 0)}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Reset WNBA",
                "Wipe today's WNBA model picks and refund their stakes?",
                "POST", "/api/admin/model/reset", body={"sport": "wnba"},
                done_msg=lambda d: f"WNBA picks reset. Removed: {(d.get('removed') or {}).get('wnba', 0)}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Reset Both",
                "Wipe today's MLB + WNBA model picks and refund all stakes?",
                "POST", "/api/admin/model/reset", body={"sport": "both"},
                done_msg=lambda d: (
                    f"Reset. MLB removed: {(d.get('removed') or {}).get('mlb', 0)}, "
                    f"WNBA removed: {(d.get('removed') or {}).get('wnba', 0)}."
                ),
                refresh_status=refresh,
                style="danger",
            )

        ui.label("Bankroll").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-top: 10px;"
        )
        _bankroll_button(
            backend, "Reset Model Bankroll...",
            which="model",
            done_msg="Model bankroll reset.",
            refresh_status=refresh,
        )


# ───────────────────────────────────────────────────────────────────────────
#  Section: MY BETS
# ───────────────────────────────────────────────────────────────────────────

def _section_my_bets(backend, refresh) -> None:
    with _card(
        "MY BETS",
        "Wipe your tracked bets and set your personal bankroll. Bets are unified across MLB + WNBA.",
    ):
        with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
            _confirm_button(
                backend, "Wipe MLB Bets",
                "Wipe all MLB bets (open + history) and reset MLB bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "mlb"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Wipe WNBA Bets",
                "Wipe all WNBA bets (open + history) and reset WNBA bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "wnba"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="warn",
            )
            _confirm_button(
                backend, "Wipe Both Sports",
                "Wipe ALL bets across MLB + WNBA and reset all bankrolls?",
                "POST", "/api/admin/wipe_ledger", body={"sport": "both"},
                done_msg=lambda d: f"Wiped: {', '.join(d.get('wiped') or [])}.",
                refresh_status=refresh,
                style="danger",
            )
            _bankroll_button(
                backend, "Set My Bankroll...",
                which="personal",
                done_msg="Personal bankroll updated.",
                refresh_status=refresh,
            )


# ───────────────────────────────────────────────────────────────────────────
#  Section: DIAGNOSTICS
#  End-to-end probe of every data source the UI depends on.  No mutations,
#  no API quota burn -- everything here is read-only.
# ───────────────────────────────────────────────────────────────────────────

def _section_diagnostics(backend) -> None:
    """Live probe panel.  Renders empty rows and a 'Run' button; the
    button populates the rows when pressed (and re-runs on click)."""
    with _card(
        "DIAGNOSTICS",
        "Probe every data source: in-memory state, snapshot files, daily "
        "picks, Supabase, Odds API key, ledgers.",
    ):
        results_holder = ui.column().classes("w-full").style("gap: 0;")

        async def _run():
            results_holder.clear()
            with results_holder:
                ui.label("Running probes...").style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; padding: 8px 0;"
                )
            probes = await asyncio.to_thread(_run_diagnostics, backend)
            results_holder.clear()
            with results_holder:
                for label, status, detail in probes:
                    _diag_row(label, status, detail)

        with ui.row().style("gap: 8px; flex-wrap: wrap;"):
            ui.button("Run Diagnostics", on_click=_run) \
                .props("no-caps unelevated") \
                .style(_btn_style("primary"))
            _sharpapi_probe_button(results_holder)

        # Auto-run on page open so the user doesn't need to click first
        ui.timer(0.5, _run, once=True)


def _sharpapi_probe_button(results_holder) -> None:
    """One-shot SharpAPI endpoint + auth-style probe.  Renders the result
    rows into the same `results_holder` the main diagnostics use, so the
    output sits in the user's existing field of view."""
    btn = ui.button("Probe SharpAPI").props("no-caps unelevated") \
        .style(_btn_style("default"))

    async def _click():
        btn.props("loading"); btn.disable()
        try:
            import os as _os
            key = (_os.environ.get("SHARPAPI_KEY") or "").strip()
            if not key:
                ui.notify(
                    "SHARPAPI_KEY is not set in Railway -- nothing to probe.",
                    type="warning",
                )
                return

            ui.notify("Probing SharpAPI endpoints + auth styles...",
                      type="ongoing")

            # Run in a thread so the event loop stays responsive
            def _do():
                from src.odds_client import SharpApiClient
                from src.cache import Cache
                client = SharpApiClient(key, Cache())
                return client.probe_endpoints()

            rows = await asyncio.to_thread(_do)

            # Render results inline.  Use the same diag-row format so the
            # output flows directly under the existing diagnostics rows.
            results_holder.clear()
            with results_holder:
                ui.label(f"SharpAPI probe: {len(rows)} endpoint/auth combos tried").style(
                    f"font-size: 12px; font-weight: 700; color: {t.TEXT}; "
                    f"padding: 8px 0; letter-spacing: .5px;"
                )
                for r in rows:
                    label  = f"{r['endpoint']}  [{r['auth']}]"
                    status = (
                        "ok"   if (r.get("ok") and r.get("status") == 200)
                        else "warn" if (r.get("status") in (200, 401, 403))
                        else "err"
                    )
                    sample = (r.get("sample") or "")[:400].replace("\n", " ")
                    detail = (
                        f"status={r.get('status')}  bytes={r.get('bytes')}  "
                        f"body[:400]={sample}"
                    )
                    _diag_row(label, status, detail)

            ok_count  = sum(1 for r in rows if r.get("ok") and r.get("status") == 200)
            ui.notify(
                f"SharpAPI probe done: {ok_count}/{len(rows)} returned 200 OK. "
                f"Look at the rows -- the body samples show what each endpoint "
                f"actually returns.",
                type="positive" if ok_count else "warning",
                multi_line=True,
            )
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"SharpAPI probe failed: {type(exc).__name__}: {exc}",
                      type="negative", multi_line=True)
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _diag_row(label: str, status: str, detail: str) -> None:
    """One diagnostic line.  status: 'ok' | 'warn' | 'err' | 'info'."""
    color = {
        "ok":   t.POS,
        "warn": t.WARN,
        "err":  t.NEG,
        "info": t.TEXT_DIM,
    }.get(status, t.TEXT_DIM)
    icon = {"ok": "✓", "warn": "!", "err": "×", "info": "·"}.get(status, "·")
    with ui.row().classes("items-start w-full").style(
        f"padding: 8px 0; gap: 10px; "
        f"border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        ui.label(icon).style(
            f"color: {color}; font-weight: 800; min-width: 16px; "
            f"font-family: monospace; font-size: 13px;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(label).style(
                f"color: {t.TEXT}; font-size: 13px; font-weight: 600;"
            )
            ui.label(detail).style(
                f"color: {color}; font-size: 11.5px; font-family: monospace; "
                f"line-height: 1.4; word-break: break-word;"
            )


# ─── Probe runner ─────────────────────────────────────────────────────────

def _run_diagnostics(backend) -> list[tuple[str, str, str]]:
    """Run every probe synchronously and return (label, status, detail)
    tuples.  Called via asyncio.to_thread so the UI thread stays free."""
    out: list[tuple[str, str, str]] = []

    # 1. In-memory analysis state
    try:
        n_mlb  = len(backend._analysis_state.get("results")      or [])
        n_wnba = len(backend._wnba_analysis_state.get("results") or [])
        ts_mlb  = backend._analysis_state.get("last_analyzed_at")
        ts_wnba = backend._wnba_analysis_state.get("last_analyzed_at")
        ok = (n_mlb + n_wnba) > 0
        out.append((
            "In-memory analysis state",
            "ok" if ok else "warn",
            f"MLB: {n_mlb} games (last {ts_mlb or '—'})  |  "
            f"WNBA: {n_wnba} games (last {ts_wnba or '—'})",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("In-memory analysis state", "err", f"{type(exc).__name__}: {exc}"))

    # 2. Daily snapshot file
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/daily_snapshot.json")
        if not p.exists():
            out.append(("Daily snapshot file", "warn", "data/daily_snapshot.json -- missing"))
        else:
            snap = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            snap_date = snap.get("date") or snap.get("mlb", {}).get("date")
            stale = snap_date != today
            mlb_n  = len((snap.get("mlb",  {}) or {}).get("results") or [])
            wnba_n = len((snap.get("wnba", {}) or {}).get("results") or [])
            status = "warn" if stale else ("ok" if (mlb_n + wnba_n) > 0 else "warn")
            out.append((
                "Daily snapshot file",
                status,
                f"date={snap_date} (today={today})  MLB={mlb_n}  WNBA={wnba_n}",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Daily snapshot file", "err", f"{type(exc).__name__}: {exc}"))

    # 3. Per-sport analysis caches
    for sport, cache_path in (("MLB",  "data/analysis_cache.json"),
                               ("WNBA", "data/wnba_analysis_cache.json")):
        try:
            from pathlib import Path
            import json as _json
            p = Path(cache_path)
            if not p.exists():
                out.append((f"{sport} analysis cache", "warn", f"{cache_path} -- missing"))
                continue
            payload = _json.loads(p.read_text(encoding="utf-8"))
            today = backend._today_et()
            stale = payload.get("date") != today
            n = len(payload.get("results") or [])
            out.append((
                f"{sport} analysis cache",
                "warn" if stale else ("ok" if n > 0 else "warn"),
                f"{cache_path}  date={payload.get('date')} (today={today})  games={n}",
            ))
        except Exception as exc:                                          # noqa: BLE001
            out.append((f"{sport} analysis cache", "err", f"{type(exc).__name__}: {exc}"))

    # 4. Daily picks file (model's top-5-per-category selections)
    try:
        daily = backend.load_daily_picks() or {}
        picks = daily.get("picks") or {}
        cats = {k: len(v or []) for k, v in picks.items()}
        total = sum(cats.values())
        out.append((
            "Daily picks file",
            "ok" if total > 0 else "warn",
            f"data/daily_picks.json  ML={cats.get('moneyline',0)}  "
            f"RL/Spread={cats.get('run_line_spread',0)}  Totals={cats.get('totals',0)}",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Daily picks file", "err", f"{type(exc).__name__}: {exc}"))

    # 5. Analysis timestamps file
    try:
        ts = backend._read_analysis_timestamps() or {}
        mlb_ts  = (ts.get("mlb")  or {}).get("analyzed_at")  or "—"
        wnba_ts = (ts.get("wnba") or {}).get("analyzed_at")  or "—"
        out.append((
            "Analysis timestamps",
            "ok" if (mlb_ts != "—" or wnba_ts != "—") else "warn",
            f"MLB={mlb_ts}  WNBA={wnba_ts}",
        ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Analysis timestamps", "err", f"{type(exc).__name__}: {exc}"))

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
            detail = f"mode=json  (SUPABASE_URL / SUPABASE_KEY not set -- JSON-only fallback)"
        out.append(("Supabase", level, detail))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Supabase", "err", f"{type(exc).__name__}: {exc}"))

    # 6b. Supabase app_cache table (persistent snapshot + analysis caches)
    try:
        from src import db as _db
        if not _db.is_supabase():
            out.append(("Supabase app_cache", "info",
                        "skipped (Supabase not connected)"))
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
            out.append((
                "Supabase app_cache",
                "ok" if anything else "warn",
                " | ".join(bits),
            ))
    except Exception as exc:                                              # noqa: BLE001
        # Most likely failure: table doesn't exist yet.  Surface that clearly.
        msg = str(exc)
        hint = (" -- create the table via the SQL in src/db.py header"
                if "PGRST205" in msg or "does not exist" in msg.lower() else "")
        out.append(("Supabase app_cache", "err",
                    f"{type(exc).__name__}: {msg}{hint}"))

    # 7. Odds API key presence + live validity probe.  Calls the
    # /v4/sports endpoint which does NOT count against quota, so this
    # probe is safe to run on every diagnostic refresh.  Surfaces 401
    # ("key invalid / expired / revoked") and 429 ("quota exhausted")
    # explicitly so the user doesn't see them as a 15-minute "hang"
    # again -- those are the exact failures behind today's incident.
    try:
        import os as _os
        key = _os.environ.get("ODDS_API_KEY") or ""
        if not key:
            out.append(("Odds API key", "err",
                        "ODDS_API_KEY env var not set -- analysis cannot fetch odds"))
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
                    out.append((
                        "Odds API key", "err",
                        f"401 Unauthorized -- key invalid / expired / revoked. "
                        f"Update ODDS_API_KEY in Railway -> Variables.",
                    ))
                elif resp.status_code == 429:
                    out.append((
                        "Odds API key", "err",
                        f"429 quota exhausted (used={used}, remaining={rem}). "
                        f"Wait for monthly reset or upgrade plan.",
                    ))
                elif resp.status_code >= 400:
                    out.append((
                        "Odds API key", "warn",
                        f"HTTP {resp.status_code} from /v4/sports  "
                        f"(used={used}, remaining={rem})",
                    ))
                else:
                    out.append((
                        "Odds API key", "ok",
                        f"valid -- used={used}, remaining={rem}  "
                        f"(prefix={key[:4]}..., len={len(key)})",
                    ))
                    # Drill-down: which sports does this plan cover?  The
                    # /v4/sports response is a JSON array of {key, active,
                    # title, ...} dicts.  Surface a per-sport row for
                    # baseball_mlb and basketball_wnba so the user can
                    # tell "0 games returned" apart from "plan does not
                    # include this sport".
                    try:
                        sports = resp.json() or []
                        wanted = {
                            "baseball_mlb":     "MLB",
                            "basketball_wnba":  "WNBA",
                        }
                        seen = {
                            s.get("key"): s for s in sports
                            if isinstance(s, dict)
                        }
                        for sport_key, label in wanted.items():
                            entry = seen.get(sport_key)
                            if entry is None:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "err",
                                    f"sport_key '{sport_key}' NOT in your plan's "
                                    f"coverage list ({len(seen)} sports returned). "
                                    f"Upgrade plan or check "
                                    f"https://the-odds-api.com/sports-odds-data/",
                                ))
                            elif entry.get("active") is False:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "warn",
                                    f"sport is in plan but currently inactive "
                                    f"(off-season / between updates). "
                                    f"title='{entry.get('title')}', "
                                    f"active=False -- no games will be returned",
                                ))
                            else:
                                out.append((
                                    f"Odds API: {label} availability",
                                    "ok",
                                    f"active=True, group={entry.get('group')}, "
                                    f"title='{entry.get('title')}' -- 0 games at "
                                    f"runtime just means no books have posted "
                                    f"lines yet (typical between ~midnight and "
                                    f"~10 AM ET)",
                                ))
                    except Exception as exc:                              # noqa: BLE001
                        out.append((
                            "Odds API: sport availability",
                            "warn",
                            f"could not parse /v4/sports response: "
                            f"{type(exc).__name__}: {exc}",
                        ))
            except _req.Timeout:
                out.append(("Odds API key", "warn",
                            "probe timed out after 5s -- key untested, "
                            "analysis may still work"))
            except Exception as exc:                                      # noqa: BLE001
                out.append(("Odds API key", "warn",
                            f"probe failed ({type(exc).__name__}: {exc}); "
                            f"env var is set (prefix={key[:4]}...)"))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Odds API key", "err", f"{type(exc).__name__}: {exc}"))

    # 8. Ledger files
    for sport, path in (("MLB", "data/ledger.json"), ("WNBA", "data/wnba_ledger.json")):
        try:
            from pathlib import Path
            p = Path(path)
            if not p.exists():
                out.append((f"{sport} ledger", "warn", f"{path} -- missing"))
                continue
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
            s = led.get_summary()
            open_n = len(led.data.get("open_bets") or [])
            hist_n = len(led.data.get("history")   or [])
            out.append((
                f"{sport} ledger",
                "ok",
                f"{path}  model=${s.get('model_bankroll',0):.2f}  "
                f"personal=${s.get('personal_bankroll',0):.2f}  "
                f"open={open_n}  history={hist_n}",
            ))
        except Exception as exc:                                          # noqa: BLE001
            out.append((f"{sport} ledger", "err", f"{type(exc).__name__}: {exc}"))

    # 8b. Auto-analysis lock status -- is an analysis actively running?
    try:
        lock = getattr(backend, "_auto_analysis_lock", None)
        if lock is None:
            out.append(("Auto-analysis lock", "info",
                        "lock object not found on backend module"))
        else:
            held = lock.locked()
            out.append((
                "Auto-analysis lock",
                "warn" if held else "ok",
                ("held -- the scheduled auto-analysis is currently running. "
                 "Manual Run buttons may appear stuck until this finishes.")
                if held else "free -- no scheduled analysis in flight",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Auto-analysis lock", "err", f"{type(exc).__name__}: {exc}"))

    # 9. Auto-analysis log (when did the scheduler last fire?)
    try:
        from pathlib import Path
        import json as _json
        p = Path("data/auto_analysis_log.json")
        if not p.exists():
            out.append(("Auto-analysis log", "info",
                        "data/auto_analysis_log.json -- not yet written"))
        else:
            log = _json.loads(p.read_text(encoding="utf-8"))
            last = log.get("last_run") or log.get("history", [{}])[-1] if isinstance(log, dict) else None
            out.append((
                "Auto-analysis log",
                "ok",
                f"last_run={last}",
            ))
    except Exception as exc:                                              # noqa: BLE001
        out.append(("Auto-analysis log", "err", f"{type(exc).__name__}: {exc}"))

    return out


# ───────────────────────────────────────────────────────────────────────────
#  Status header (last-analyzed + DB)
# ───────────────────────────────────────────────────────────────────────────

def _render_status(backend, holder) -> None:
    """Re-poll /api/admin/status and refresh the meta rows.  Called on page
    load and after every mutation so timestamps stay accurate."""
    holder.clear()
    ok, data, _ = _call(backend, "GET", "/api/admin/status")
    mlb_ts  = data.get("mlb_analyzed_at")  if ok else None
    wnba_ts = data.get("wnba_analyzed_at") if ok else None
    db      = (data.get("db") or {}) if ok else {}

    def _row(label: str, value: str, value_color: str = t.TEXT) -> None:
        with ui.row().classes("items-center w-full").style(
            f"justify-content: space-between; gap: 8px; "
            f"padding: 4px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
        ):
            ui.label(label).style(f"color: {t.TEXT_DIM}; font-size: 12px;")
            ui.label(value).style(
                f"color: {value_color}; font-size: 12px; font-family: monospace;"
            )

    with holder:
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 2px;"
        ):
            ui.label("STATUS").style(
                f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2}; margin-bottom: 4px;"
            )
            _row("Last MLB analyzed",  _fmt_ts(mlb_ts))
            _row("Last WNBA analyzed", _fmt_ts(wnba_ts))
            _row("DB mode", str(db.get("mode") or "json"))
            sb = db.get("supabase")
            if sb is not None:
                _row("Supabase", "connected" if sb else "off",
                     value_color=t.POS if sb else t.TEXT_DIM)


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %b %-d  %-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return iso[:19]


# ───────────────────────────────────────────────────────────────────────────
#  Reusable widgets
# ───────────────────────────────────────────────────────────────────────────

def _card(title: str, subtitle: str | None = None):
    """Context-manager card.  Body of caller's `with` block goes inside."""
    col = ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    )
    with col:
        ui.label(title).style(
            f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT};"
        )
        if subtitle:
            ui.label(subtitle).style(
                f"font-size: 12px; color: {t.TEXT_DIM};"
            )
    return col


def _btn_style(style: str) -> str:
    """Common styling for admin buttons.  `style` is one of:
       'default' | 'primary' | 'warn' | 'danger'."""
    if style == "primary":
        return (
            f"background: {t.PRIMARY}; color: {t.BG}; "
            f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
        )
    if style == "warn":
        return (
            f"background: transparent; color: {t.WARN}; "
            f"border: 1px solid {t.WARN}; "
            f"font-weight: 700; padding: 7px 15px; border-radius: {t.RADIUS_SM};"
        )
    if style == "danger":
        return (
            f"background: {t.NEG}; color: {t.TEXT}; "
            f"font-weight: 700; padding: 8px 16px; border-radius: {t.RADIUS_SM};"
        )
    return (
        f"background: {t.CARD_HI}; color: {t.TEXT}; "
        f"border: 1px solid {t.BORDER}; "
        f"font-weight: 600; padding: 7px 15px; border-radius: {t.RADIUS_SM};"
    )


def _async_button(
    backend, label: str, method: str, path: str, *,
    body: dict | None = None,
    spinner_msg: str = "Working...",
    done_msg=None,
    refresh_status=None,
    style: str = "default",
) -> None:
    btn = ui.button(label).props("no-caps unelevated").style(_btn_style(style))

    async def _click():
        btn.props("loading")
        btn.disable()
        try:
            ui.notify(spinner_msg, type="ongoing")
            ok, data, _ = await asyncio.to_thread(_call, backend, method, path, body)
            if ok:
                msg = done_msg(data) if callable(done_msg) else (done_msg or "Done.")
                ui.notify(msg, type="positive")
                if refresh_status:
                    refresh_status()
            else:
                ui.notify(f"{label} failed: {data.get('error') or 'unknown error'}",
                          type="negative", multi_line=True)
        finally:
            btn.props(remove="loading")
            btn.enable()

    btn.on("click", _click)


def _confirm_button(
    backend, label: str, prompt: str, method: str, path: str, *,
    body: dict | None = None,
    done_msg=None,
    refresh_status=None,
    style: str = "default",
) -> None:
    """Button that opens a confirm dialog before firing the request."""
    btn = ui.button(label).props("no-caps unelevated").style(_btn_style(style))

    async def _click():
        confirmed = await _confirm_dialog(prompt)
        if not confirmed:
            return
        btn.props("loading"); btn.disable()
        try:
            ok, data, _ = await asyncio.to_thread(_call, backend, method, path, body)
            if ok:
                msg = done_msg(data) if callable(done_msg) else (done_msg or "Done.")
                ui.notify(msg, type="positive")
                if refresh_status:
                    refresh_status()
            else:
                ui.notify(f"{label} failed: {data.get('error') or 'unknown'}",
                          type="negative", multi_line=True)
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _bankroll_button(
    backend, label: str, *, which: str,
    done_msg: str, refresh_status=None,
) -> None:
    """Open a numeric-input dialog, then POST the new value.  The
    underlying endpoints update BOTH MLB + WNBA ledgers in one call --
    `which` only selects personal vs model field, not a sport."""
    path = (
        "/api/ledger/set_model_bankroll" if which == "model"
        else "/api/ledger/set_bankroll"
    )

    btn = ui.button(label).props("no-caps unelevated").style(_btn_style("default"))

    async def _click():
        value = await _number_dialog(
            title=label.rstrip("."),
            placeholder="e.g. 1000",
        )
        if value is None:
            return
        if value <= 0:
            ui.notify("Bankroll must be greater than 0.", type="warning")
            return
        btn.props("loading"); btn.disable()
        try:
            ok, data, _ = await asyncio.to_thread(
                _call, backend, "POST", path, {"bankroll": value})
            if ok:
                ui.notify(done_msg, type="positive")
                if refresh_status:
                    refresh_status()
            else:
                ui.notify(f"Failed: {data.get('error') or 'unknown'}",
                          type="negative", multi_line=True)
        finally:
            btn.props(remove="loading"); btn.enable()

    btn.on("click", _click)


def _toggle_row(backend, label: str, sub: str, field: str, initial: bool) -> None:
    """Per-sport auto-pick toggle backed by /api/admin/model/settings."""
    with ui.row().classes("items-center w-full justify-between").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        with ui.column().style("gap: 2px;"):
            ui.label(label).style(f"color: {t.TEXT}; font-size: 13px; font-weight: 600;")
            ui.label(sub).style(f"color: {t.TEXT_DIM}; font-size: 11px;")
        sw = ui.switch(value=initial)

        async def _on_change(e):
            try:
                body = {field: bool(e.value)}
                ok, data, _ = await asyncio.to_thread(
                    _call, backend, "POST", "/api/admin/model/settings", body)
                if ok:
                    ui.notify(f"{label} {'enabled' if e.value else 'disabled'}.",
                              type="positive")
                else:
                    ui.notify(f"Toggle failed: {data.get('error') or 'unknown'}",
                              type="negative")
                    sw.value = not e.value
            except Exception as exc:                                      # noqa: BLE001
                ui.notify(f"Toggle failed: {exc}", type="negative")
                sw.value = not e.value

        sw.on_value_change(_on_change)


# ───────────────────────────────────────────────────────────────────────────
#  Dialog helpers (awaitable)
# ───────────────────────────────────────────────────────────────────────────

async def _confirm_dialog(prompt: str) -> bool:
    """Awaitable Yes / No dialog.  `await dlg` resolves to whatever the
    button passed to dlg.submit(...); closing without submitting returns
    None, which we coerce to False below."""
    with ui.dialog() as dlg, ui.card().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"min-width: 320px; max-width: 480px; gap: {t.SPACE_MD};"
    ):
        ui.label("Confirm").style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
            f"letter-spacing: .5px;"
        )
        ui.label(prompt).style(f"color: {t.TEXT_DIM}; font-size: 13px; line-height: 1.5;")
        with ui.row().classes("w-full justify-end").style("gap: 8px; margin-top: 8px;"):
            ui.button("Cancel", on_click=lambda: dlg.submit(False)) \
                .props("no-caps flat") \
                .style(f"color: {t.TEXT_DIM};")
            ui.button("Confirm", on_click=lambda: dlg.submit(True)) \
                .props("no-caps unelevated") \
                .style(f"background: {t.PRIMARY}; color: {t.BG}; font-weight: 700;")
    result = await dlg
    return bool(result)


async def _number_dialog(title: str, placeholder: str) -> float | None:
    """Awaitable numeric-input dialog.  Returns the float, or None on
    cancel / close.  Validation lives at the caller -- this just collects
    the value."""
    with ui.dialog() as dlg, ui.card().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"min-width: 320px; max-width: 480px; gap: {t.SPACE_MD};"
    ):
        ui.label(title).style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
            f"letter-spacing: .5px;"
        )
        amount = ui.number(label="Amount ($)", placeholder=placeholder,
                           min=0, step=1, format="%.2f").style("width: 100%;")

        def _save():
            v = amount.value
            if v is None:
                ui.notify("Enter a number.", type="warning")
                return
            dlg.submit(float(v))

        with ui.row().classes("w-full justify-end").style("gap: 8px; margin-top: 8px;"):
            ui.button("Cancel", on_click=lambda: dlg.submit(None)) \
                .props("no-caps flat") \
                .style(f"color: {t.TEXT_DIM};")
            ui.button("Save", on_click=_save) \
                .props("no-caps unelevated") \
                .style(f"background: {t.PRIMARY}; color: {t.BG}; font-weight: 700;")
    result = await dlg
    return None if result is None else float(result)
