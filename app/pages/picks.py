"""
picks.py
========
Consolidated Picks tab (/picks).

Three sub-tabs as pill buttons: Sports | Props | Top Picks

Calls the existing _layout / _render helpers from sport.py, props.py,
and top_picks.py directly -- those pages keep their own routes intact.
"""
from __future__ import annotations

import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


_PILLS = (
    ("sports",    "Sports"),
    ("props",     "Props"),
    ("top_picks", "Top Picks"),
)


def register(backend) -> None:
    @ui.page("/picks")
    def picks_page():
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_PICKS)
            with ui.column().classes("page-content w-full").style(
                f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
                f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _layout(backend)
            bottom_nav.render(active=t.TAB_PICKS)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[PICKS FATAL] {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label("Picks failed to render").style(
                f"color: {t.NEG}; font-size: 16px; padding: {t.SPACE_LG};")


def _layout(backend) -> None:
    state = {"tab": "sports"}

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
            if tab == "sports":
                from pages import sport as _sport
                _sport._render_sport(backend, "mlb")
            elif tab == "props":
                from pages import props as _props
                _props._layout(backend)
            elif tab == "top_picks":
                from pages import top_picks as _tp
                _tp._layout(backend)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[PICKS] tab={tab} error: {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label(f"Failed to load {tab} content.").style(
                f"color: {t.NEG}; font-size: 13px; padding: {t.SPACE_MD};"
            )

    _content()
