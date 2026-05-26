"""
bets.py
=======
Consolidated Bets tab (/bets).

Two sub-tabs as pill buttons: Model Picks | My Bets

Calls the existing _layout / _render helpers from model.py and mybets.py
directly -- those pages keep their own routes intact.
"""
from __future__ import annotations

import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


_PILLS = (
    ("model",  "Model Picks"),
    ("mybets", "My Bets"),
)


def register(backend) -> None:
    @ui.page("/bets")
    def bets_page():
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_BETS)
            with ui.column().classes("page-content w-full").style(
                f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
                f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _layout(backend)
            bottom_nav.render(active=t.TAB_BETS)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[BETS FATAL] {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label("Bets failed to render").style(
                f"color: {t.NEG}; font-size: 16px; padding: {t.SPACE_LG};")


def _layout(backend) -> None:
    state = {"tab": "model"}

    with ui.row().classes("items-center w-full").style("gap: 6px; flex-wrap: wrap;"):
        @ui.refreshable
        def _pill_row() -> None:
            for key, label in _PILLS:
                active = state["tab"] == key
                def _mk(k):
                    def _set() -> None:
                        state["tab"] = k
                        _pill_row.refresh()
                        _content.refresh()
                    return _set
                ui.button(label, on_click=_mk(key)).props(
                    "no-caps unelevated dense"
                ).style(
                    f"background: {t.PRIMARY if active else t.CARD_HI}; "
                    f"color: {t.BG if active else t.TEXT_DIM}; "
                    f"font-size: 12px; font-weight: 800; padding: 6px 18px; "
                    f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                )
        _pill_row()

    @ui.refreshable
    def _content() -> None:
        tab = state["tab"]
        try:
            if tab == "model":
                from pages import model as _model
                _model._refreshable_model_sections(backend)
            elif tab == "mybets":
                from pages import mybets as _mybets
                _mybets._add_bet_bar(backend)
                _mybets._personal_bankroll(backend)
                _mybets._unified_bets(backend)
                _mybets._recommendations_section(backend)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[BETS] tab={tab} error: {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label(f"Failed to load {tab} content.").style(
                f"color: {t.NEG}; font-size: 13px; padding: {t.SPACE_MD};"
            )

    _content()
