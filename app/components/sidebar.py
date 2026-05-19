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
            _pick_row(p, backend)


def _pick_row(p: dict, backend=None) -> None:
    rank   = p.get("rank", "·")
    team   = p.get("team", "—")
    sport_raw = (p.get("sport") or p.get("sport_label") or "mlb").lower()
    # Normalize for the Track endpoint routing: "MLB"/"mlb" -> "mlb",
    # anything WNBA-ish -> "wnba".  Daily picks rows store the sport_label
    # in upper-case ("MLB" / "WNBA") so this lookup is forgiving.
    sport_norm = "wnba" if "wnba" in sport_raw else "mlb"
    prob   = float(p.get("pick_prob") or 0) * 100
    odds   = p.get("odds")
    odds_s = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
    # Daily picks rows carry the game id as `game_id` (see daily_picks.py).
    game_id = p.get("game_id") or p.get("id")

    # Two-line description:
    #   line 1 -- matchup ("Braves vs Marlins")
    #   line 2 -- bet pick formatted by bet_type ("Braves ML" /
    #             "Braves -1.5" / "8.5 Over")
    # daily_picks.py builds matchup as "Away @ Home" with full names; we
    # split on " @ ", drop each team's city via _team_nick, and rejoin
    # with " vs " so the row reads naturally and fits the narrow sidebar.
    matchup_raw = (p.get("matchup") or "").strip()
    if " @ " in matchup_raw:
        _away, _home = matchup_raw.split(" @ ", 1)
        matchup = f"{_team_nick(_away.strip())} vs {_team_nick(_home.strip())}"
    else:
        matchup = matchup_raw or "—"
    pick_desc   = _format_pick_desc(p)

    with ui.row().classes("items-center w-full").style(
        f"padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT}; gap: 8px;"
    ):
        ui.label(f"{rank}").style(
            f"color: {t.TEXT_DIM}; font-weight: 800; min-width: 16px; "
            f"font-family: monospace;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0; overflow: hidden;"):
            # Matchup is the contextual line (dim), pick is the prominent one.
            ui.label(matchup).style(
                f"font-size: 10px; color: {t.TEXT_DIM2}; "
                f"letter-spacing: .3px; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(pick_desc).style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        with ui.column().style("gap: 2px; text-align: right;"):
            ui.label(f"{prob:.0f}%").style(
                f"font-size: 12px; font-weight: 700; color: {t.PRIMARY}; "
                f"font-family: monospace;"
            )
            ui.label(odds_s).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
        # Track button -- compact, lives at the right edge of the row.
        # If backend wasn't passed or the row has no game_id, render
        # nothing (vs a disabled button) so the layout stays tight.
        if backend is not None and game_id:
            from . import track_button as _tb
            _tb.render(
                backend, game_id=game_id, sport=sport_norm,
                size="sm", label="Track",
            )


def _team_nick(name: str) -> str:
    """City -> nickname only ("Atlanta Braves" -> "Braves").

    Matches the legacy template's shortName() heuristics: Sox + Blue Jays
    keep the 2-word nickname; everything else drops everything before the
    last word.  Returns the input unchanged if it's already 1 word or
    blank.
    """
    if not name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    last = parts[-1]
    if last == "Sox":                                                     # Red Sox / White Sox
        return " ".join(parts[-2:])
    if last == "Jays":                                                    # Blue Jays
        return "Blue Jays"
    return last


def _format_pick_desc(p: dict) -> str:
    """Build a human-readable one-liner for the pick column in the Top 5
    Plays row.  Branches by bet_type so the reader sees exactly what was
    picked without having to cross-reference fields.

    Examples (matching the user's spec):
       Moneyline:  "Braves ML"
       Run line:   "Braves -1.5"      (sign is the line value as stored,
                                       which daily_picks already signs to
                                       the pick team's perspective)
       Spread:     "Liberty +6.5"
       Totals:     "8.5 Over"         (note: daily_picks stores the team
                                       field pre-formatted as "Over 8.5";
                                       this swaps to line-first to match
                                       the requested ordering)
    """
    bet_type = (p.get("bet_type") or "single").lower()
    team     = (p.get("team") or "").strip()
    line     = p.get("prop_line")

    if bet_type == "single":
        # Pure moneyline -- nickname + ML tag
        nick = _team_nick(team)
        return f"{nick} ML" if nick else "ML"

    if bet_type in ("run_line", "spread"):
        # daily_picks stores prop_line as a signed float from the picked
        # team's perspective (e.g. -1.5 for an MLB favorite on the RL,
        # +6.5 for a WNBA underdog on the spread).  `:+g` shows sign +
        # drops trailing zeros so 1.5 stays "1.5" not "1.5000".
        nick = _team_nick(team)
        try:
            line_str = f"{float(line):+g}"
            return f"{nick} {line_str}" if nick else line_str
        except (TypeError, ValueError):
            return nick or "—"

    if bet_type == "totals":
        # daily_picks builds the team field as e.g. "Over 8.5" or
        # "Under 9.0".  User wants line-first ("8.5 Over"), so split
        # and rebuild.  Fall back to the team string if the format
        # doesn't match what we expect (e.g. a future provider that
        # changes how this is composed).
        parts = team.split()
        if len(parts) == 2 and parts[0].lower() in ("over", "under"):
            direction, ln = parts
            return f"{ln} {direction.title()}"
        # Alternate path -- prop_line + parse direction
        try:
            if isinstance(line, (int, float)):
                return f"{float(line):g} {team}"
        except Exception:                                                 # noqa: BLE001
            pass
        return team or "—"

    # Unknown bet type -- show the raw team value so nothing is hidden.
    return team or "—"


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
