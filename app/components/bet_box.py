"""
Single bet box -- one of three columns inside a game card.

Renders a labelled pick (Moneyline / Run Line / Totals) with the model's
probability, edge, odds, and a small VALUE chip when value_pick is true.
The chip uses theme.PRIMARY for ML picks and theme.WARN for value highlights.

Below 480px viewport width, the full label (MONEYLINE / RUN LINE / SPREAD /
TOTALS) is swapped for an abbreviation (ML / RL / SPR / TOT) via the
theme's .bet-label-full / .bet-label-short CSS classes -- both labels are
emitted into the DOM and the media query toggles display.  Pure CSS swap,
no per-render JS.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


# Map full bet labels to their narrow-screen abbreviations.  Anything not
# in the map gets uppercased first 3 chars as a generic fallback so a
# future bet type (e.g. PROPS) renders sensibly without a code change.
_SHORT_LABEL: dict[str, str] = {
    "MONEYLINE": "ML",
    "RUN LINE":  "RL",
    "SPREAD":    "SPR",
    "TOTALS":    "TOT",
}


def _short_label(label: str) -> str:
    return _SHORT_LABEL.get(label.upper(), label[:3].upper())


def render(
    label: str,
    pick: str | None,
    prob: float | None,
    edge: float | None,
    odds: int | None,
    is_value: bool = False,
    result: str | None = None,
) -> None:
    """One bet box.  All numeric fields tolerate None (renders as —).

    `result` is "win" / "loss" / "push" / None.  Drives a subtle box
    tint + left border accent so a finished game's bet boxes show
    their per-market outcome at a glance (caller wires this up from
    the live-score feed -- pre-game / in-progress cards always pass
    None so they stay neutral).
    """
    # Background + left-border accent based on settled result.  rgba()
    # values match the user spec; left border is a thicker 3px slab
    # of the same hue at full opacity so the tint reads on OLED
    # without competing with the card-glow + global theme.
    if result == "win":
        bg          = "rgba(34, 197, 94, 0.15)"   # green tint
        border_left = "3px solid rgb(34, 197, 94)"
        border_rest = f"1px solid {t.BORDER}"
    elif result == "loss":
        bg          = "rgba(239, 68, 68, 0.15)"   # red tint
        border_left = "3px solid rgb(239, 68, 68)"
        border_rest = f"1px solid {t.BORDER}"
    else:
        # Includes "push" and the default pre-game / in-progress
        # case.  Push is intentionally neutral -- there's no clear
        # color convention for it and tinting it the same as a win
        # would be misleading.
        bg          = t.CARD_HI
        border_left = f"1px solid {t.BORDER}"
        border_rest = f"1px solid {t.BORDER}"
    with ui.column().style(
        f"background: {bg}; "
        f"border-top: {border_rest}; "
        f"border-right: {border_rest}; "
        f"border-bottom: {border_rest}; "
        f"border-left: {border_left}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 8px 10px; "
        f"min-width: 0; gap: 4px; flex: 1;"
    ):
        # Label + optional value chip.  Both labels (full + short) are
        # emitted; CSS toggles which is visible based on viewport width.
        with ui.row().classes("items-center justify-between w-full").style("gap: 4px;"):
            _LABEL_STYLE = (
                f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(label).classes("bet-label-full").style(_LABEL_STYLE)
            ui.label(_short_label(label)).classes("bet-label-short").style(_LABEL_STYLE)
            if is_value:
                ui.label("VALUE").style(
                    f"font-size: 8.5px; font-weight: 800; letter-spacing: .5px; "
                    f"background: {t.PRIMARY}; color: {t.BG}; "
                    f"padding: 1px 5px; border-radius: 3px;"
                )

        ui.label(pick or "—").classes("pick-text").style(
            f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )

        with ui.row().classes("items-center justify-between w-full text-row").style("gap: 4px;"):
            prob_s = f"{(prob or 0) * 100:.0f}%"
            ui.label(prob_s).style(
                f"font-size: 11px; font-weight: 700; color: {t.PRIMARY}; "
                f"font-family: monospace;"
            )
            edge_v = (edge or 0) * 100
            edge_s = f"{'+' if edge_v >= 0 else ''}{edge_v:.1f}%"
            edge_c = t.POS if edge_v >= 0 else t.NEG
            ui.label(edge_s).style(
                f"font-size: 11px; color: {edge_c}; font-family: monospace;"
            )
            odds_s = (
                f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
                if odds is not None else "—"
            )
            ui.label(odds_s).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
