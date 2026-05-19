"""
My Bets page.

Shows:
  - Personal bankroll snapshot (start / current / P&L)
  - Open confirmed bets (the ones you Tracked)
  - Recent settled history with W/L colors

The model's own auto-picks live on the Model page -- this page is the
personal-bankroll side only.
"""
from __future__ import annotations

from datetime import datetime

from nicegui import ui

from components import theme as t
from components import navbar, sidebar


def register(backend) -> None:
    @ui.page("/mybets")
    def mybets_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_MYBETS)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _personal_bankroll(backend)
                _open_bets(backend)
                _history(backend)


def _personal_bankroll(backend) -> None:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        s = mlb.get_summary()
        start = float(s.get("personal_starting_bankroll", 1000))
        current = float(s.get("personal_bankroll", start))
        pnl = current - start
        # Open confirmed bets across both sports
        open_confirmed = (
            [b for b in (mlb.data.get("open_bets") or [])  if b.get("confirmed")]
            + [b for b in (wnba.data.get("open_bets") or []) if b.get("confirmed")]
        )
        at_risk = sum(float(b.get("confirmed_amount") or 0) for b in open_confirmed)
    except Exception:                                                     # noqa: BLE001
        start, current, pnl, at_risk = 1000.0, 1000.0, 0.0, 0.0

    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    with ui.row().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; "
        f"gap: {t.SPACE_XL};"
    ):
        _stat("START",     f"${start:,.2f}",   t.TEXT_DIM)
        _stat("CURRENT",   f"${current:,.2f}", t.TEXT)
        _stat("P / L",     f"{pnl_sign}${abs(pnl):,.2f}", pnl_color)
        _stat("AT RISK",   f"${at_risk:,.2f}", t.WARN)


def _stat(label: str, value: str, color: str) -> None:
    with ui.column().style("gap: 4px;"):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 700; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(value).style(
            f"font-size: 20px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px;"
        )


def _open_bets(backend) -> None:
    confirmed_open = _confirmed_bets(backend, settled=False)
    _section("OPEN BETS", confirmed_open, settled=False)


def _history(backend) -> None:
    confirmed_hist = _confirmed_bets(backend, settled=True)
    _section("RECENT HISTORY", confirmed_hist[:50], settled=True)


def _confirmed_bets(backend, settled: bool) -> list[dict]:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
    except Exception:                                                     # noqa: BLE001
        return []
    if settled:
        merged = [
            *(b for b in (mlb.data.get("history") or [])  if b.get("confirmed")),
            *(b for b in (wnba.data.get("history") or []) if b.get("confirmed")),
        ]
        merged.sort(key=lambda b: b.get("settled_at", ""), reverse=True)
    else:
        merged = [
            *(b for b in (mlb.data.get("open_bets") or [])  if b.get("confirmed")),
            *(b for b in (wnba.data.get("open_bets") or []) if b.get("confirmed")),
        ]
        merged.sort(key=lambda b: b.get("placed_at", ""), reverse=True)
    return merged


def _section(title: str, bets: list[dict], settled: bool) -> None:
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(title).style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"{len(bets)}").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not bets:
            ui.label("No bets yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        for b in bets:
            _bet_row(b, settled)


def _bet_row(b: dict, settled: bool) -> None:
    result = (b.get("result") or "").lower()
    result_color = {
        "win": t.POS, "loss": t.NEG, "push": t.WARN, "void": t.TEXT_DIM2,
    }.get(result, t.TEXT_DIM)

    sport = (b.get("sport") or "mlb").upper()
    team = b.get("bet_team") or b.get("parlay_name") or "—"
    bet_type = (b.get("bet_type") or "single").upper().replace("_", " ")
    odds = b.get("american_odds")
    odds_s = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
    amount = float(b.get("confirmed_amount") or 0)
    pnl = float(b.get("confirmed_pnl") or 0) if settled else 0.0
    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    border = f"1px solid {result_color}" if settled and result in ("win", "loss", "push") \
        else f"1px solid {t.BORDER}"

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 10px;"
    ):
        ui.label(sport).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL};"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(team).style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{bet_type} · {odds_s}").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
        with ui.column().style("gap: 2px; text-align: right; align-items: flex-end;"):
            ui.label(f"${amount:.2f}").style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; font-family: monospace;"
            )
            if settled:
                ui.label(f"{result.upper()}  {pnl_sign}${abs(pnl):.2f}").style(
                    f"font-size: 11px; color: {pnl_color}; font-family: monospace;"
                )
            else:
                ui.label("PENDING").style(
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
