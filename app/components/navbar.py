"""
Top navigation bar.

A fixed, persistent strip across every page.  Five primary tabs in this
left-to-right order:

  Home  |  Sports  |  AI Breakdown  |  Model  |  My Bets

Plus a gear icon on the far right for /admin.

The Sports tab is a regular link (not a dropdown) -- clicking it goes
straight to /sports/mlb.  The MLB <-> WNBA toggle lives inside the
Sports page header (see pages/sport.py) as a pill switcher, so the
sport choice happens contextually after navigation, not as a
prerequisite for it.

The "active tab" is passed in by each page so the link for the current
page renders with the primary accent.  No global state is kept here --
every page renders the navbar fresh and tells it which tab is active.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


# Order matters: this list IS the display order, left to right.
_NAV_LINKS = (
    ("Home",         t.TAB_HOME,    "/"),
    ("Sports",       t.TAB_SPORTS,  "/sports/mlb"),
    ("AI Breakdown", t.TAB_AI,      "/ai"),
    ("Model",        t.TAB_MODEL,   "/model"),
    ("My Bets",      t.TAB_MYBETS,  "/mybets"),
)


def render(active: str = t.TAB_HOME) -> None:
    """Render the persistent top nav, marking *active* with the accent color."""
    with ui.header(elevated=False).style(
        f"background: {t.BG}; "
        f"border-bottom: 1px solid {t.BORDER}; "
        f"padding: 0 {t.SPACE_LG}; "
        f"height: {t.NAVBAR_HEIGHT};"
    ).classes("items-center justify-between no-wrap"):
        # Brand -- shrinks to its content, never wraps
        with ui.row().classes("items-center gap-2 no-wrap").style("flex-shrink: 0;"):
            ui.label("Sports").style(
                f"font-weight: 800; font-size: 16px; color: {t.TEXT};"
            )
            ui.label("Analysis").style(
                f"font-weight: 800; font-size: 16px; color: {t.PRIMARY};"
            )

        # Main links (hidden on mobile -- bottom_nav takes over there).
        # `no-wrap` + flex-shrink:0 keeps the row from collapsing or
        # wrapping when the viewport is tight (e.g. landscape tablet).
        with ui.row().classes("items-center gap-1 desktop-only no-wrap").style(
            "flex-shrink: 0;"
        ):
            for label, tab_key, href in _NAV_LINKS:
                _nav_link(label, href, active == tab_key)

        # Admin gear -- always visible (desktop + mobile).  Sits at the
        # far right.  Not part of the 5 primary tabs and intentionally
        # absent from the mobile bottom-nav (lives in the header instead
        # so the bottom-nav stays at 5 tap targets).
        _admin_gear(active == t.TAB_ADMIN)


def _admin_gear(is_active: bool) -> None:
    color = t.PRIMARY if is_active else t.TEXT_DIM
    bg    = "rgba(59, 130, 246, .12)" if is_active else "transparent"
    with ui.link(target="/admin").style(
        f"display: flex; align-items: center; justify-content: center; "
        f"width: 38px; height: 38px; border-radius: 8px; "
        f"background: {bg}; color: {color}; text-decoration: none; "
        f"transition: background .15s ease; flex-shrink: 0;"
    ).tooltip("Admin"):
        ui.icon("settings").style(f"font-size: 20px; color: {color};")


def _nav_link(label: str, href: str, is_active: bool) -> None:
    color  = t.PRIMARY if is_active else t.TEXT_DIM
    weight = "700" if is_active else "500"
    border = f"2px solid {t.PRIMARY}" if is_active else "2px solid transparent"
    ui.link(label, href).style(
        f"color: {color}; "
        f"font-size: 13px; font-weight: {weight}; letter-spacing: .3px; "
        f"text-decoration: none; "
        f"padding: 6px 12px; "
        f"border-bottom: {border}; "
        f"white-space: nowrap;"   # never wrap "AI Breakdown" mid-label
    )
