"""
controls.py
===========
Themed wrappers for the NiceGUI / Quasar form controls used across the
app's filter bars and selector strips.

Every filter, toggle, dropdown, switch, slider and tab strip on the
props page, player profile page, mybets, and admin should route
through one of these helpers so the look stays consistent and a
single theme change flows everywhere.

The heavy lifting -- backgrounds, borders, fonts, dropdown popups,
focus states -- is done by the CSS block in
``components/theme.page_head_css``.  These functions are thin
wrappers around ``ui.toggle`` / ``ui.select`` / ``ui.switch`` /
``ui.slider`` that:
  * set consistent Quasar props (``dense``, ``outlined`` etc.)
  * apply opt-in marker classes (``pill-toggle``, ``styled-select``...)
    so theme CSS can target without globally hijacking every Quasar
    control elsewhere in the codebase
  * standardise sizing tokens so a select on /props matches one on
    /player matches one on /admin
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from nicegui import ui

from . import theme as t


# ── Pill toggle (segmented control) ─────────────────────────────────────────

def pill_toggle(
    options: list[str] | dict,
    value: Any,
    on_change: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
):
    """Horizontal segmented "pill" control used for short option sets
    (e.g. ``Last 5 / Last 10 / Last 20 / Season / H2H``).

    *options* may be a list (value == label) or a dict ``{value: label}``.
    *name* lets the caller debug-tag the resulting Quasar id.
    """
    el = ui.toggle(options, value=value, on_change=on_change)
    el.props("dense unelevated no-caps").classes("pill-toggle")
    if name:
        el.props(f'data-control="{name}"')
    return el


# ── Card-style dropdown (multi-option select) ───────────────────────────────

#: Popup-content class shared by every styled dropdown.  Lets the theme CSS
#: dark-theme the popup AND dock it as a bottom-sheet on mobile (the popup is
#: portalled out of the trigger's DOM subtree, so the trigger's own classes
#: can't reach it -- this marker is the hook the CSS keys off).  Routing every
#: ``ui.select`` through this helper is what makes "no native OS dropdown,
#: bottom-sheet on mobile" hold globally (UI redesign, Change 3).
SELECT_POPUP_CLASS = "styled-select-pop"


def styled_select(
    options: Iterable | dict,
    value: Any,
    on_change: Optional[Callable] = None,
    *,
    min_width: str = "200px",
    placeholder: Optional[str] = None,
    multiple: bool = False,
    with_input: bool = False,
    use_chips: bool = False,
):
    """Dark-card dropdown for option selectors (game, market, model,
    stat-context, ...).  Pops a dark themed menu rather than the default
    white Material list, and on mobile renders as a bottom-sheet picker
    rather than the native OS dropdown.

    This is the single canonical select for the whole app -- every filter
    and picker should route through here (or ``custom_select``) so the look,
    the popup theme, and the mobile bottom-sheet behaviour stay consistent.

    *min_width*  sets the trigger's minimum width so the control still reads
                 comfortably with short selections like ``"All"``.
    *placeholder* floating label shown when nothing is selected.
    *multiple*   multi-select (renders selections as chips when *use_chips*).
    *with_input* type-to-filter combobox (used by the mybets add-bet wizard).
    """
    el = ui.select(
        options=options, value=value, on_change=on_change,
        multiple=multiple, with_input=with_input,
    )
    # Quasar's q-select is never a native <select>; with the default
    # behaviour it pops a menu on desktop and a dialog on mobile, which the
    # theme CSS then docks to the bottom as a sheet picker.  The
    # popup-content-class is the hook that CSS keys off (menu + dialog both).
    props = (
        f'dense outlined options-dense '
        f'popup-content-class="{SELECT_POPUP_CLASS}"'
    )
    if use_chips:
        props += " use-chips"
    if placeholder:
        # Wrap in double-quotes since the prop string is space-tokenized.
        props += f' label="{placeholder}"'
    el.props(props).classes("styled-select").style(
        f"min-width: {min_width};"
    )
    return el


#: Backwards-friendly alias -- the redesign brief refers to the global
#: "custom select"; keep one name pointing at the canonical implementation.
custom_select = styled_select


# ── Switch (binary on/off) ──────────────────────────────────────────────────

def styled_switch(
    value: bool,
    on_change: Optional[Callable] = None,
):
    """Quasar switch tinted with the app's purple primary."""
    return (
        ui.switch(value=value, on_change=on_change)
          .props("dense color=primary")
          .classes("styled-switch")
    )


# ── Slider (range) ──────────────────────────────────────────────────────────

def styled_slider(
    *,
    min: int | float,
    max: int | float,
    step: int | float,
    value: int | float,
    on_change: Optional[Callable] = None,
):
    """Range slider with the app's purple selection bar."""
    el = ui.slider(
        min=min, max=max, step=step, value=value, on_change=on_change,
    )
    el.props("dense color=primary label-always switch-label-side").classes(
        "styled-slider"
    )
    return el


# ── Field label (the small caps-lock caption above each control) ────────────

def field_label(text: str) -> None:
    """Standard caps caption used above every filter control.  Same
    typography across every page so the rhythm is consistent."""
    ui.label(text).style(
        f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
        f"color: {t.TEXT_DIM2};"
    )
