"""
Left sidebar -- one card: CONFIDENCE PERFORMANCE.

The TOP 5 PLAYS card that used to live here was removed per user
spec; the home page's Highest Confidence + EV Scan carousels
already surface the same picks with richer per-card detail.

Reads straight from the backend module so there's no HTTP hop:
  backend.Ledger("data/ledger.json").data["history"]  (+ wnba)
"""
from __future__ import annotations

from typing import Iterable

from nicegui import ui

from . import theme as t


def render(backend) -> None:
    """Render the sidebar (desktop only).  Hidden on mobile via the
    .desktop-only class -- the Confidence Performance card only shows
    on viewports wide enough to fit it next to the main content
    without crowding."""
    with ui.column().classes("desktop-only").style(
        f"width: {t.SIDEBAR_WIDTH}; "
        f"min-width: {t.SIDEBAR_WIDTH}; "
        f"gap: {t.SPACE_MD}; "
        f"padding: {t.SPACE_MD};"
    ):
        _confidence_card(backend)


# ── CONFIDENCE PERFORMANCE ───────────────────────────────────────────────────

def _confidence_card(backend) -> None:
    tiers = ("strong", "moderate", "low")
    counts = {tier: [0, 0] for tier in tiers}      # [wins, losses]
    try:
        # Pull from both per-sport ledgers and aggregate non-confirmed model
        # history per tier.  Confirmed bets are separate; this card tracks
        # the model's calibration, not the user's confirmed slate.
        for path in ("data/ledger.json", "data/wnba_ledger.json"):
            try:
                led = backend.Ledger(path=path, starting_bankroll=1000.0)
            except Exception:                                             # noqa: BLE001
                continue
            for h in (led.data.get("history") or []):
                tier = (h.get("confidence_tier") or "strong").lower()
                if tier not in counts:
                    continue
                if   h.get("result") == "win":  counts[tier][0] += 1
                elif h.get("result") == "loss": counts[tier][1] += 1
    except Exception:                                                     # noqa: BLE001
        pass

    with ui.card().classes("theme-card w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD};"
    ):
        ui.label("CONFIDENCE PERFORMANCE").style(
            f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-bottom: {t.SPACE_SM};"
        )
        for tier in tiers:
            _tier_row(tier, counts[tier])


def _tier_row(label: str, wl: Iterable[int]) -> None:
    w, l = list(wl)
    total = w + l
    pct = f"{(w / total * 100):.1f}%" if total else "—"
    pct_color = (
        t.POS if total and (w / total) >= 0.55 else
        t.NEG if total and (w / total) < 0.45 else t.TEXT_DIM
    )
    # Tier label gets its own color (palette redesign):
    #   Strong   -> emerald (POS)
    #   Moderate -> amber (WARN)
    #   Low      -> muted grey (TEXT_DIM2) -- explicitly NOT NEG so it
    #               doesn't read as a loss.
    tier_color = t.TIER_COLOR.get(label.lower(), t.TEXT_DIM)
    pretty = {"strong": "Strong Pick", "moderate": "Moderate Pick",
              "low": "Low Confidence"}.get(label.lower(), label.title())
    with ui.row().classes("items-center w-full justify-between").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        ui.label(pretty).style(
            f"font-size: 12px; font-weight: 700; color: {tier_color};"
        )
        ui.label(f"{w}-{l}  ({pct})").style(
            f"font-size: 12px; color: {pct_color}; font-family: monospace;"
        )
