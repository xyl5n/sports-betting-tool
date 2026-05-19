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
