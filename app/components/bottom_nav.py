"""
Bottom tab bar -- mobile-only navigation.

Anchored to the viewport bottom via Quasar's QFooter (``ui.footer``)
so it stays fixed across every page even when scrolling.  We render
the floating "pill" bar look as a child of the footer rather than
positioning the bar itself with ``position: fixed`` -- earlier
attempts at that broke whenever a parent picked up a CSS ``transform``
(which establishes a new containing block and de-anchors fixed
descendants).  Quasar's QFooter handles the fixed positioning at
the layout level, so the bar is robust against future CSS additions.

The bar visually lifts off the viewport edge by ``max(12px,
env(safe-area-inset-bottom))`` so iPhone home indicator + Safari
swipe zone don't sit under the tap targets.  Quasar auto-reserves
matching ``padding-bottom`` on ``.q-page-container`` so content above
stays fully scrollable.

Hidden on desktop via the ``.mobile-only`` class -- show/hide is
pure CSS in ``theme.page_head_css``.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


# Order matches the desktop navbar: Home, Sports, AI, Model, My Bets.
_TABS = (
    ("Home",     t.TAB_HOME,   "/",            "home"),
    ("Sports",   t.TAB_SPORTS, "/sports/mlb",  "sports_baseball"),
    ("Props",    t.TAB_PROPS,  "/props",       "person"),
    ("Top",      t.TAB_TOP,    "/top-picks",   "leaderboard"),
    ("AI",       t.TAB_AI,     "/ai",          "auto_awesome"),
    ("Model",    t.TAB_MODEL,  "/model",       "insights"),
    ("My Bets",  t.TAB_MYBETS, "/mybets",      "receipt_long"),
)


def render(active: str = t.TAB_HOME) -> None:
    """Render the bottom tab bar.  Mounted as a Quasar QFooter, so it
    stays fixed at the bottom of the viewport on every page."""
    # ui.footer() defaults to fixed=True (anchored at the bottom of
    # the QLayout).  We strip the default elevation/border and use the
    # footer purely as a transparent positioning host -- all visual
    # styling lives on the inner floating bar.
    with ui.footer(elevated=False, bordered=False).classes("mobile-only").style(
        f"background: transparent !important; "
        f"box-shadow: none !important; "
        # Horizontal inset + safe-area-aware bottom inset.  The inner
        # bar's height + this padding is what Quasar reserves as
        # padding-bottom on .q-page-container so content above never
        # gets covered.
        f"padding: 0 8px max(12px, env(safe-area-inset-bottom)) 8px;"
    ):
        with ui.element("div").style(
            f"width: 100%; box-sizing: border-box; "
            f"height: {t.BOTTOM_NAV_HEIGHT}; "
            f"background: {t.CARD}; "
            f"border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_LG}; "
            f"box-shadow: 0 4px 18px rgba(0, 0, 0, 0.6); "
            f"padding: 4px 6px; "
            f"display: flex; justify-content: space-around; align-items: stretch;"
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
