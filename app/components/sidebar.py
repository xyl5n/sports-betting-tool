"""
Left sidebar -- shown on every page next to the main content.

Two cards, top-to-bottom:
  1. TOP 5 PLAYS         -- moneyline picks from daily_picks.json
  2. CONFIDENCE PERFORMANCE -- per-tier W/L from the unified ledger

Reads straight from the backend module so there's no HTTP hop:
  backend.load_daily_picks()
  backend.Ledger("data/ledger.json").data["history"]  (+ wnba)
"""
from __future__ import annotations

from typing import Iterable

from nicegui import ui

from . import theme as t


def render(backend) -> None:
    """Build the sidebar against the imported `app` module (passed as backend).

    Hidden on mobile -- the TOP 5 PLAYS card is re-rendered inline on the
    home page below the bankroll hero so it stays reachable without the
    sidebar.  CONFIDENCE PERFORMANCE is desktop-only for now (lives next
    to the Model page in spirit, where its data already appears)."""
    with ui.column().classes("desktop-only").style(
        f"width: {t.SIDEBAR_WIDTH}; "
        f"min-width: {t.SIDEBAR_WIDTH}; "
        f"gap: {t.SPACE_MD}; "
        f"padding: {t.SPACE_MD};"
    ):
        _top_plays_card(backend)
        _confidence_card(backend)


def render_top_plays_only(backend) -> None:
    """Mobile inline version -- just the TOP 5 PLAYS card, no sidebar shell.

    Called from pages/home.py so the home page still surfaces the picks
    rail when the desktop sidebar is hidden by the .desktop-only rule."""
    with ui.column().classes("mobile-only").style(
        f"width: 100%; gap: {t.SPACE_MD};"
    ):
        _top_plays_card(backend)


# ── TOP 5 PLAYS ─────────────────────────────────────────────────────────────

def _top_plays_card(backend) -> None:
    try:
        daily = backend.load_daily_picks() or {}
        picks = (daily.get("picks") or {}).get("moneyline") or []
    except Exception:                                                     # noqa: BLE001
        picks = []

    with ui.card().classes("theme-card w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD};"
    ):
        ui.label("TOP 5 PLAYS").style(
            f"font-size: 11px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; margin-bottom: {t.SPACE_SM};"
        )
        if not picks:
            ui.label("No model picks yet -- run analysis.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px;"
            )
            return
        for p in picks[:5]:
            _pick_row(p)


def _pick_row(p: dict) -> None:
    rank   = p.get("rank", "·")
    team   = p.get("team", "—")
    sport  = (p.get("sport_label") or p.get("sport") or "").upper()
    prob   = float(p.get("pick_prob") or 0) * 100
    odds   = p.get("odds")
    odds_s = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
    with ui.row().classes("items-center w-full").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT}; gap: 8px;"
    ):
        ui.label(f"{rank}").style(
            f"color: {t.TEXT_DIM}; font-weight: 800; min-width: 16px; "
            f"font-family: monospace;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0; overflow: hidden;"):
            ui.label(team).style(
                f"font-size: 13px; font-weight: 600; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(sport).style(
                f"font-size: 9px; font-weight: 700; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
        with ui.column().style("gap: 2px; text-align: right;"):
            ui.label(f"{prob:.0f}%").style(
                f"font-size: 12px; font-weight: 700; color: {t.PRIMARY}; "
                f"font-family: monospace;"
            )
            ui.label(odds_s).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )


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
    with ui.row().classes("items-center w-full justify-between").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        ui.label(label.title()).style(
            f"font-size: 12px; color: {t.TEXT_DIM};"
        )
        ui.label(f"{w}-{l}  ({pct})").style(
            f"font-size: 12px; color: {pct_color}; font-family: monospace;"
        )
