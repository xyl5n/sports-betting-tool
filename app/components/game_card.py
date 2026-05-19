"""
One game card -- matchup header + three bet boxes (ML / RL-Spread / Totals).

Takes a serialized game dict in the shape produced by app._serialize() /
app._serialize_wnba().  Tolerates missing fields so a half-populated
result still renders without crashing.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from . import theme as t
from . import bet_box
from . import track_button

_ET = ZoneInfo("America/New_York")


def render(g: dict, sport: str = "mlb", backend=None) -> None:
    """Render one game's card.  `g` is a _serialize() output dict.

    When `g["_no_model"]` is truthy the matchup is shown with market odds
    but the three bet-box row is replaced by an inline NO MODEL PICK
    notice.  This is how games involving teams missing from the model's
    training data (e.g. 2026 WNBA expansion teams) still appear on the
    Sports tab instead of vanishing silently.

    `backend` is the imported `app` module (passed through pages -> here).
    When provided, a Track button appears at the bottom of the card and
    posts to the in-process /api/.../ledger/confirm/<game_id> endpoint.
    When omitted (legacy call sites that haven't been updated), no Track
    button is rendered -- the card still works, just without tracking.
    """
    sport = (sport or g.get("_sport") or "mlb").lower()
    is_mlb = sport == "mlb"

    with ui.column().style(
        f"background: {t.CARD}; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; "
        f"gap: {t.SPACE_SM}; width: 100%;"
    ):
        _meta_row(g, sport)
        _matchup_row(g)
        if g.get("_no_model"):
            _no_model_row(g)
        else:
            _bet_boxes(g, is_mlb)
        if backend is not None:
            _track_row(backend, g, sport)


def _track_row(backend, g: dict, sport: str) -> None:
    """One Track button per card -- aligned right under the bet boxes.

    Records the model's moneyline pick to the personal ledger via
    /api/{sport}/ledger/confirm/<game_id>.  For NO MODEL PICK cards
    the button is disabled with a hint -- there's nothing for the
    model to confirm in that case.
    """
    with ui.row().classes("w-full justify-end").style("gap: 6px; margin-top: 4px;"):
        if g.get("_no_model"):
            track_button.render(
                backend, game_id=(g.get("game_id") or g.get("id")), sport=sport, size="sm",
                label="Track",
                disabled_reason=(
                    "No model pick available for this matchup -- "
                    "tracking would record an empty bet."
                ),
            )
        else:
            track_button.render(
                backend, game_id=(g.get("game_id") or g.get("id")), sport=sport, size="sm",
                label="Track",
            )


def _no_model_row(g: dict) -> None:
    """Inline notice that replaces the three bet boxes when the model
    couldn't generate a prediction for this matchup."""
    reason = g.get("_no_model_reason") or "No model pick available for this matchup."
    with ui.row().classes("w-full").style(
        f"background: {t.CARD_HI}; "
        f"border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 10px 12px; gap: 10px; align-items: center;"
    ):
        ui.label("NO MODEL PICK").style(
            f"flex-shrink: 0; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"color: {t.WARN}; "
            f"background: rgba(251, 191, 36, 0.12); "
            f"padding: 2px 7px; border-radius: 3px;"
        )
        ui.label(reason).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; "
            f"flex: 1; line-height: 1.4;"
        )


def _meta_row(g: dict, sport: str) -> None:
    when = _fmt_et(g.get("commence_time", ""))
    with ui.row().classes("items-center w-full").style("gap: 8px;"):
        ui.label(sport.upper()).style(
            f"background: {t.CARD_HI}; color: {t.TEXT}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL};"
        )
        ui.label(when).style(f"font-size: 12px; color: {t.TEXT_DIM};")


def _matchup_row(g: dict) -> None:
    away = _short(g.get("away_team", "—"))
    home = _short(g.get("home_team", "—"))
    a_odds = _odds_str(g.get("away_odds"))
    h_odds = _odds_str(g.get("home_odds"))
    with ui.row().classes("items-center w-full").style("gap: 12px;"):
        with ui.column().style("flex: 1; gap: 2px;"):
            ui.label(away).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT};"
            )
            ui.label(a_odds).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
        ui.label("@").style(f"color: {t.TEXT_DIM2}; font-size: 12px;")
        with ui.column().style("flex: 1; gap: 2px; text-align: right; align-items: flex-end;"):
            ui.label(home).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT};"
            )
            ui.label(h_odds).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )


def _bet_boxes(g: dict, is_mlb: bool) -> None:
    rl = g.get("run_line") if is_mlb else g.get("spread_pick")
    totals = g.get("totals") or {}

    with ui.row().classes("w-full bet-boxes").style("gap: 6px;"):
        # Moneyline -- always present
        bet_box.render(
            label="MONEYLINE",
            pick=_short(g.get("pick_team")) if g.get("pick_team") else None,
            prob=g.get("pick_prob"),
            edge=g.get("pick_edge"),
            odds=g.get("pick_odds"),
            is_value=bool(g.get("value_pick")),
        )

        # Run Line (MLB) / Spread (WNBA)
        if rl:
            line = rl.get("run_line_point") if is_mlb else rl.get("spread_line")
            line_str = ""
            if line is not None:
                pt = float(line)
                if rl.get("side") != "home":
                    pt = -pt
                line_str = f" {pt:+g}"
            pick_str = _short(rl.get("pick_team", "")) + line_str if rl.get("pick_team") else None
            bet_box.render(
                label="RUN LINE" if is_mlb else "SPREAD",
                pick=pick_str,
                prob=rl.get("pick_prob"),
                edge=rl.get("edge"),
                odds=rl.get("pick_odds"),
                is_value=bool(rl.get("value_bet")),
            )
        else:
            bet_box.render(
                label="RUN LINE" if is_mlb else "SPREAD",
                pick=None, prob=None, edge=None, odds=None, is_value=False,
            )

        # Totals
        if totals and totals.get("total_line") is not None:
            direction = (totals.get("direction") or "over").upper()
            pick_str = f"{direction} {totals.get('total_line')}"
            odds = (
                totals.get("over_odds") if direction == "OVER"
                else totals.get("under_odds")
            )
            bet_box.render(
                label="TOTALS",
                pick=pick_str,
                prob=totals.get("pick_prob"),
                edge=totals.get("edge"),
                odds=odds,
                is_value=bool(totals.get("value_bet")),
            )
        else:
            bet_box.render(
                label="TOTALS",
                pick=None, prob=None, edge=None, odds=None, is_value=False,
            )


# ── Small helpers ───────────────────────────────────────────────────────────

def _short(name: str | None) -> str:
    if not name:
        return "—"
    parts = name.split()
    if not parts:
        return name
    # "San Francisco Giants" -> "SF Giants" -- match the legacy UI's pattern
    if len(parts) >= 3:
        initials = "".join(p[0] for p in parts[:-1] if p[0].isupper())
        return f"{initials} {parts[-1]}" if initials else name
    return name


def _odds_str(o) -> str:
    if o is None:
        return "—"
    try:
        n = int(o)
    except Exception:                                                     # noqa: BLE001
        return str(o)
    return f"+{n}" if n > 0 else f"{n}"


def _fmt_et(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return iso[:16]
