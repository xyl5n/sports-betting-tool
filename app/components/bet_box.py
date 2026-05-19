"""
Single bet box -- one of three columns inside a game card.

Renders a labelled pick (Moneyline / Run Line / Totals) with the model's
probability, edge, odds, and a small VALUE chip when value_pick is true.
The chip uses theme.PRIMARY for ML picks and theme.WARN for value highlights.
"""
from __future__ import annotations

from nicegui import ui

from . import theme as t


def render(
    label: str,
    pick: str | None,
    prob: float | None,
    edge: float | None,
    odds: int | None,
    is_value: bool = False,
) -> None:
    """One bet box.  All numeric fields tolerate None (renders as —)."""
    with ui.column().style(
        f"background: {t.CARD_HI}; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 8px 10px; "
        f"min-width: 0; gap: 4px; flex: 1;"
    ):
        # Label + optional value chip
        with ui.row().classes("items-center justify-between w-full").style("gap: 4px;"):
            ui.label(label).style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            if is_value:
                ui.label("VALUE").style(
                    f"font-size: 8.5px; font-weight: 800; letter-spacing: .5px; "
                    f"background: {t.PRIMARY}; color: {t.BG}; "
                    f"padding: 1px 5px; border-radius: 3px;"
                )

        ui.label(pick or "—").style(
            f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )

        with ui.row().classes("items-center justify-between w-full").style("gap: 4px;"):
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
