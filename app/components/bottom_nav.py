"""
Bottom tab bar -- mobile-only navigation.

Fixed strip across the bottom of the viewport.  Five large tap targets
matching the desktop navbar's TAB_* keys.  Hidden on desktop via the
.mobile-only class -- show/hide is pure CSS in theme.page_head_css().

Sport switcher (MLB / WNBA) lives inside the /sports/* page header
on mobile because cramming a dropdown into a bottom-nav tab makes the
tap-targets too small.  The "Sports" tab here just routes to
/sports/mlb as a sensible default.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


# Order matches the desktop navbar: Home, Sports, AI, Model, My Bets.
_TABS = (
    ("Home",     t.TAB_HOME,   "/",            "home"),
    ("Sports",   t.TAB_SPORTS, "/sports/mlb",  "sports_baseball"),
    ("AI",       t.TAB_AI,     "/ai",          "auto_awesome"),
    ("Model",    t.TAB_MODEL,  "/model",       "insights"),
    ("My Bets",  t.TAB_MYBETS, "/mybets",      "receipt_long"),
)


def render(active: str = t.TAB_HOME) -> None:
    """Render the bottom tab bar.  Hidden on desktop via .mobile-only."""
    with ui.element("div").classes("mobile-only").style(
        f"position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; "
        f"height: {t.BOTTOM_NAV_HEIGHT}; "
        f"background: {t.CARD}; "
        f"border-top: 1px solid {t.BORDER}; "
        f"padding: 4px 0 max(4px, env(safe-area-inset-bottom)) 0; "
        f"justify-content: space-around; align-items: stretch;"
    ):
        for label, tab_key, href, icon in _TABS:
            _tab(label, href, icon, active == tab_key)


def _tab(label: str, href: str, icon: str, is_active: bool) -> None:
    color  = t.PRIMARY if is_active else t.TEXT_DIM
    weight = "700" if is_active else "500"
    bg     = "rgba(59, 130, 246, .08)" if is_active else "transparent"
    with ui.link(target=href).style(
        f"flex: 1; display: flex; flex-direction: column; align-items: center; "
        f"justify-content: center; gap: 2px; "
        f"text-decoration: none; color: {color}; "
        f"background: {bg}; border-radius: 6px; margin: 0 2px; "
        f"min-height: 44px;"
    ):
        ui.icon(icon).style(f"font-size: 22px; color: {color};")
        ui.label(label).style(
            f"font-size: 10px; font-weight: {weight}; letter-spacing: .3px; "
            f"color: {color};"
        )
