"""
Home page -- top-level dashboard.

Shows three rails:
  1. Hero card with model bankroll snapshot
  2. EV SCANS -- value picks across both sports (today + tomorrow)
  3. AI banner -- prompt to chat with the model (links to /ai)

Wired entirely against the imported backend module:
  backend._analysis_state, backend._wnba_analysis_state  -- cached results
  backend.Ledger(...)                                    -- bankroll snapshot
"""
from __future__ import annotations

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, game_card


def register(backend) -> None:
    @ui.page("/")
    def home_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_HOME)
        _layout(backend)


def _layout(backend) -> None:
    with ui.row().classes("no-wrap w-full").style("gap: 0;"):
        sidebar.render(backend)
        with ui.column().style(
            f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
            f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
        ):
            _bankroll_hero(backend)
            _ev_section(backend)
            _ai_banner()


def _bankroll_hero(backend) -> None:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        s = mlb.get_summary()
        start = float(s.get("model_starting_bankroll", 1000))
        current = float(s.get("model_bankroll", start))
        pnl = current - start
        # Open bets across both ledgers
        open_n = len(mlb.data.get("open_bets") or []) + len(wnba.data.get("open_bets") or [])
    except Exception:                                                     # noqa: BLE001
        start, current, pnl, open_n = 1000.0, 1000.0, 0.0, 0

    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    with ui.row().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; "
        f"gap: {t.SPACE_XL}; align-items: center;"
    ):
        _stat_cell("MODEL BANKROLL", f"${current:,.2f}", t.TEXT)
        _stat_cell("P / L",          f"{pnl_sign}${abs(pnl):,.2f}", pnl_color)
        _stat_cell("OPEN BETS",      f"{open_n}", t.PRIMARY)


def _stat_cell(label: str, value: str, color: str) -> None:
    with ui.column().style("gap: 4px;"):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 700; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(value).style(
            f"font-size: 22px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px;"
        )


def _ev_section(backend) -> None:
    games = _value_games(backend)
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_MD};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("EV SCANS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"{len(games)}").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not games:
            ui.label(
                "No value picks yet -- run analysis (legacy /api/analyze or via the "
                "Admin section in the prior UI) to populate today's slate."
            ).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; text-align: center;"
            )
            return
        for g in games[:8]:
            game_card.render(g, sport=g.get("_sport", "mlb"))


def _value_games(backend) -> list[dict]:
    """Pull serialized value picks from the in-memory analysis cache for
    both sports.  Falls back to an empty list when no analysis has run."""
    out: list[dict] = []
    try:
        bankroll = float(backend._analysis_state.get("bankroll") or 250)
        mlb_results = backend._analysis_state.get("results") or []
        if mlb_results:
            mlb_ledger = backend.Ledger(path="data/ledger.json", starting_bankroll=bankroll)
            s_bank = mlb_ledger.data.get("personal_starting_bankroll", bankroll)
            for r in mlb_results:
                try:
                    g = backend._serialize(r, bankroll, "mlb", s_bank)
                    g["_sport"] = "mlb"
                    if g.get("value_pick"):
                        out.append(g)
                except Exception:                                         # noqa: BLE001
                    continue
    except Exception:                                                     # noqa: BLE001
        pass
    try:
        bankroll = float(backend._wnba_analysis_state.get("bankroll") or 1000)
        wnba_results = backend._wnba_analysis_state.get("results") or []
        if wnba_results:
            wnba_ledger = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=bankroll)
            s_bank = wnba_ledger.data.get("personal_starting_bankroll", bankroll)
            for r in wnba_results:
                try:
                    g = backend._serialize_wnba(r, bankroll, s_bank)
                    g["_sport"] = "wnba"
                    if g.get("value_pick"):
                        out.append(g)
                except Exception:                                         # noqa: BLE001
                    continue
    except Exception:                                                     # noqa: BLE001
        pass

    # Sort by edge descending
    out.sort(key=lambda g: float(g.get("pick_edge") or 0), reverse=True)
    return out


def _ai_banner() -> None:
    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_MD}; "
        f"gap: {t.SPACE_MD}; cursor: pointer;"
    ).on("click", lambda: ui.navigate.to("/ai")):
        with ui.column().style("flex: 1; gap: 4px;"):
            ui.label("AI Breakdown").style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT};"
            )
            ui.label("Ask the model anything about today's picks.").style(
                f"font-size: 12px; color: {t.TEXT_DIM};"
            )
        ui.label("→").style(
            f"font-size: 18px; color: {t.PRIMARY};"
        )
