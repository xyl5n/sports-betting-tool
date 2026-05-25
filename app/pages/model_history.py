"""
model_history.py
================
Date-browsing history for one model store (/model-history/{sport}/{model}).

Reads only the Supabase ``model_picks`` table (via model_picks.history) — no
JSON trackers.  Strictly filtered by sport + model + ET date, so MLB xgb and
WNBA xgb are separate views and any future sport works with no new code.

Two timeframe controls: a row of presets (Today / Yesterday / Last 7 Days /
Last 30 Days) and a single-day calendar picker.  Defaults to Today.  Shows
the model's finished W/L/void record for the timeframe, then the full pick
list (pending picks included, marked Pending) newest-first as one ui.html()
table.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav

_ET = ZoneInfo("America/New_York")

_PRESETS = (("today", "Today"), ("yesterday", "Yesterday"),
            ("7d", "Last 7 Days"), ("30d", "Last 30 Days"))


def register(backend) -> None:
    @ui.page("/model-history/{sport}/{model}")
    def model_history_page(sport: str, model: str):
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_ADMIN)
            with ui.column().classes("page-content w-full").style(
                f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
                f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _layout((sport or "mlb").lower(), (model or "combined").lower())
            bottom_nav.render(active=t.TAB_ADMIN)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[MODEL-HISTORY FATAL] {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label("Model history failed to render").style(
                f"color: {t.NEG}; padding: {t.SPACE_LG};")


def _fmt_dt(iso: str) -> str:
    """ISO UTC -> 'MM-DD HH:MM' ET."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso)[:16]


def _layout(sport: str, model: str) -> None:
    from src import model_picks as mp
    state = {"mode": "preset", "preset": "today", "date": mp._today_et()}

    ui.label(f"{model.upper()} · {sport.upper()} — PICK HISTORY").classes(
        "page-title").style(f"font-size: 20px; font-weight: 800; color: {t.TEXT};")
    ui.link("← back to admin", "/admin").style(
        f"font-size: 12px; color: {t.PRIMARY}; text-decoration: none;")

    # ── Timeframe controls: presets + calendar ───────────────────────────────
    with ui.row().classes("items-center w-full").style("gap: 6px; flex-wrap: wrap;"):
        @ui.refreshable
        def _pills() -> None:                                             # noqa: WPS430
            for key, label in _PRESETS:
                active = state["mode"] == "preset" and state["preset"] == key
                def _mk(k):
                    def _set() -> None:
                        state["mode"] = "preset"
                        state["preset"] = k
                        _pills.refresh()
                        _body.refresh()
                    return _set
                ui.button(label, on_click=_mk(key)).props("no-caps unelevated dense").style(
                    f"background: {t.PRIMARY if active else t.CARD_HI}; "
                    f"color: {t.BG if active else t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 800; padding: 5px 12px; "
                    f"border-radius: {t.RADIUS_PILL}; min-height: 0;")
        _pills()

        def _on_date(e) -> None:
            if e.value:
                state["mode"] = "date"
                state["date"] = e.value
                _pills.refresh()
                _body.refresh()
        with ui.input("Custom day").props("dense outlined").style(
            "width: 150px;") as _date_in:
            with _date_in.add_slot("append"):
                ui.icon("event").classes("cursor-pointer")
            with ui.menu() as _menu:
                ui.date(value=state["date"], on_change=_on_date).bind_value(_date_in)
            _date_in.on("click", _menu.open)

    @ui.refreshable
    def _body() -> None:                                                  # noqa: WPS430
        if state["mode"] == "date":
            start = end = state["date"]
            label = state["date"]
        else:
            start, end = mp.date_range(state["preset"])
            label = dict(_PRESETS)[state["preset"]]
        try:
            data = mp.history(sport, model, start, end)
        except Exception as exc:                                          # noqa: BLE001
            ui.label(f"History unavailable: {exc}").style(
                f"font-size: 12px; color: {t.TEXT_DIM};")
            return
        rec = data.get("record") or {}
        picks = data.get("picks") or []

        # ── Record header ────────────────────────────────────────────────────
        w, l, v = rec.get("wins", 0), rec.get("losses", 0), rec.get("voids", 0)
        pct = rec.get("pct")
        pct_s = f"{pct * 100:.1f}%" if pct is not None else "—"
        rec_color = (t.POS if (pct or 0) >= 0.55 else
                     t.NEG if (pct is not None and pct < 0.50) else t.TEXT_DIM)
        void_s = f" · {v}V" if v else ""
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; gap: 2px;"
        ):
            ui.label(f"{model} · {sport.upper()} · {label}").style(
                f"font-size: 11px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};")
            ui.label(f"{w}-{l}{void_s}, {pct_s}").style(
                f"font-size: 22px; font-weight: 800; color: {rec_color}; "
                f"font-family: monospace;")
            ui.label(f"{len(picks)} pick(s) in this timeframe "
                     f"({w + l} finished, {len(picks) - (w + l + v)} pending)").style(
                f"font-size: 11px; color: {t.TEXT_DIM};")

        if not picks:
            ui.label("No picks logged for this model in the selected timeframe.").style(
                f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                f"text-align: center; width: 100%;")
            return

        # ── Pick list (single ui.html table, newest first) ───────────────────
        th = (f"font-size:10px; font-weight:800; letter-spacing:.4px; "
              f"color:{t.TEXT_DIM2}; padding:6px 8px; text-align:left; "
              f"border-bottom:1px solid {t.BORDER}; white-space:nowrap;")
        head = "".join(f"<th style='{th}'>{c}</th>" for c in (
            "Made", "Player / Matchup", "Bet", "Side", "Line", "Conf", "Status", "Result"))
        body = ""
        for p in picks:
            td = (f"font-size:11.5px; font-family:monospace; padding:6px 8px; "
                  f"text-align:left; color:{t.TEXT}; "
                  f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap;")
            status = (p.get("status") or "pending").lower()
            result = (p.get("result") or "").lower()
            if status != "finished":
                res_html = f"<span style='color:{t.TEXT_DIM2};'>Pending</span>"
            else:
                rc = {"win": t.POS, "loss": t.NEG, "void": t.WARN}.get(result, t.TEXT_DIM)
                res_html = f"<span style='color:{rc}; font-weight:800;'>{result.upper() or '—'}</span>"
            conf = p.get("confidence")
            conf_s = f"{float(conf) * 100:.0f}%" if isinstance(conf, (int, float)) else "—"
            line = p.get("line")
            line_s = f"{float(line):g}" if isinstance(line, (int, float)) else "—"
            who = p.get("player_name") or p.get("game_id") or "—"
            body += (
                f"<tr><td style='{td} color:{t.TEXT_DIM2};'>{_fmt_dt(p.get('created_at'))}</td>"
                f"<td style='{td}'>{who}</td>"
                f"<td style='{td} color:{t.TEXT_DIM};'>{(p.get('bet_type') or '')}</td>"
                f"<td style='{td}'>{p.get('pick_side') or '—'}</td>"
                f"<td style='{td} text-align:right;'>{line_s}</td>"
                f"<td style='{td} text-align:right;'>{conf_s}</td>"
                f"<td style='{td} color:{t.TEXT_DIM};'>{status}</td>"
                f"<td style='{td}'>{res_html}</td></tr>"
            )
        ui.html(
            f"<div style='overflow-x:auto; width:100%;'>"
            f"<table style='width:100%; border-collapse:collapse; background:{t.CARD}; "
            f"border:1px solid {t.BORDER}; border-radius:{t.RADIUS_MD};'>"
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
        )

    _body()
