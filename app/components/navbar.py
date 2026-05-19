"""
Top navigation bar.

A fixed, persistent strip across every page.  Five entry points:
  Home, Sports (dropdown -> MLB / WNBA), AI Breakdown, My Bets, Model

The "active tab" is passed in by each page so the link for the current
page renders with the primary accent.  No global state is kept here --
every page renders the navbar fresh and tells it which tab is active.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


_NAV_LINKS = (
    ("Home",           t.TAB_HOME,    "/"),
    ("AI Breakdown",   t.TAB_AI,      "/ai"),
    ("My Bets",        t.TAB_MYBETS,  "/mybets"),
    ("Model",          t.TAB_MODEL,   "/model"),
)


def render(active: str = t.TAB_HOME) -> None:
    """Render the persistent top nav, marking *active* with the accent color."""
    with ui.header(elevated=False).style(
        f"background: {t.BG}; "
        f"border-bottom: 1px solid {t.BORDER}; "
        f"padding: 0 {t.SPACE_LG}; "
        f"height: {t.NAVBAR_HEIGHT};"
    ).classes("items-center justify-between"):
        # Brand
        with ui.row().classes("items-center gap-3"):
            ui.label("Sports").style(
                f"font-weight: 800; font-size: 16px; color: {t.TEXT};"
            )
            ui.label("Analysis").style(
                f"font-weight: 800; font-size: 16px; color: {t.PRIMARY};"
            )

        # Main links
        with ui.row().classes("items-center gap-1"):
            # Home / AI / My Bets / Model
            for label, tab_key, href in _NAV_LINKS:
                _nav_link(label, href, active == tab_key)

            # Sports dropdown sits between Home and AI in the visual order;
            # render it where we want by reordering this row if needed.
            _sports_dropdown(active == t.TAB_SPORTS)


def _nav_link(label: str, href: str, is_active: bool) -> None:
    color = t.PRIMARY if is_active else t.TEXT_DIM
    weight = "700" if is_active else "500"
    border = f"2px solid {t.PRIMARY}" if is_active else "2px solid transparent"
    ui.link(label, href).style(
        f"color: {color}; "
        f"font-size: 13px; font-weight: {weight}; letter-spacing: .3px; "
        f"text-decoration: none; "
        f"padding: 6px 12px; "
        f"border-bottom: {border};"
    )


def _sports_dropdown(is_active: bool) -> None:
    color = t.PRIMARY if is_active else t.TEXT_DIM
    weight = "700" if is_active else "500"
    border = f"2px solid {t.PRIMARY}" if is_active else "2px solid transparent"
    with ui.button("Sports ▾").props("flat dense").style(
        f"color: {color}; "
        f"font-size: 13px; font-weight: {weight}; letter-spacing: .3px; "
        f"padding: 6px 12px; "
        f"border-bottom: {border};"
        f"min-height: 0;"
    ):
        with ui.menu().style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 4px 0;"
        ):
            ui.menu_item("MLB",  on_click=lambda: ui.navigate.to("/sports/mlb")).style(
                f"color: {t.TEXT}; min-height: 36px;"
            )
            ui.menu_item("WNBA", on_click=lambda: ui.navigate.to("/sports/wnba")).style(
                f"color: {t.TEXT}; min-height: 36px;"
            )
