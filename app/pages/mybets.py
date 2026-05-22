"""
My Bets page.

Shows:
  - Personal bankroll snapshot (start / current / P&L)
  - Tabs: MLB  |  WNBA  |  PROPS
      MLB / WNBA: confirmed game bets (open + settled history)
      PROPS:      player-prop picks tracked from the Props page

The model's own auto-picks live on the Model page -- this page is the
personal-bankroll side only.
"""
from __future__ import annotations

from datetime import datetime

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav


def register(backend) -> None:
    @ui.page("/mybets")
    def mybets_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_MYBETS)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _personal_bankroll(backend)
                _tabs(backend)
        bottom_nav.render(active=t.TAB_MYBETS)


# ── Bankroll summary ─────────────────────────────────────────────────────────

def _personal_bankroll(backend) -> None:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        s = mlb.get_summary()
        start   = float(s.get("personal_starting_bankroll", 1000))
        current = float(s.get("personal_bankroll", start))
        pnl     = current - start
        open_confirmed = (
            [b for b in (mlb.data.get("open_bets")  or []) if b.get("confirmed")]
            + [b for b in (wnba.data.get("open_bets") or []) if b.get("confirmed")]
        )
        at_risk = sum(float(b.get("confirmed_amount") or 0) for b in open_confirmed)
    except Exception:                                                      # noqa: BLE001
        start, current, pnl, at_risk = 1000.0, 1000.0, 0.0, 0.0

    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    with ui.row().classes("w-full hero-stats").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; "
        f"gap: {t.SPACE_XL};"
    ):
        _stat("START",   f"${start:,.2f}",            t.TEXT_DIM)
        _stat("CURRENT", f"${current:,.2f}",           t.TEXT)
        _stat("P / L",   f"{pnl_sign}${abs(pnl):,.2f}", pnl_color)
        _stat("AT RISK", f"${at_risk:,.2f}",           t.WARN)


def _stat(label: str, value: str, color: str) -> None:
    with ui.column().style("gap: 4px;"):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 700; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(value).classes("stat-value").style(
            f"font-size: 20px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px;"
        )


# ── Tabs ─────────────────────────────────────────────────────────────────────

def _tabs(backend) -> None:
    with ui.tabs().props("dense align=left").style(
        f"border-bottom: 1px solid {t.BORDER}; "
        f"color: {t.TEXT_DIM};"
    ) as tabs:
        tab_mlb   = ui.tab("MLB")
        tab_wnba  = ui.tab("WNBA")
        tab_props = ui.tab("PROPS")

    with ui.tab_panels(tabs, value=tab_mlb).classes("w-full").style(
        "background: transparent; padding: 0;"
    ):
        with ui.tab_panel(tab_mlb).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _game_open_bets(backend, sport="mlb")
                _game_history(backend, sport="mlb")

        with ui.tab_panel(tab_wnba).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _game_open_bets(backend, sport="wnba")
                _game_history(backend, sport="wnba")

        with ui.tab_panel(tab_props).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _props_record()
                _props_open_bets()
                _props_history()


# ── Game bets (MLB / WNBA) ───────────────────────────────────────────────────

def _game_open_bets(backend, sport: str) -> None:
    bets = _confirmed_game_bets(backend, sport=sport, settled=False)
    _game_section("OPEN BETS", bets, settled=False)


def _game_history(backend, sport: str) -> None:
    bets = _confirmed_game_bets(backend, sport=sport, settled=True)
    _game_section("RECENT HISTORY", bets[:50], settled=True)


def _confirmed_game_bets(backend, sport: str, settled: bool) -> list[dict]:
    try:
        path = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
        ledger = backend.Ledger(path=path, starting_bankroll=1000.0)
    except Exception:                                                      # noqa: BLE001
        return []
    key = "history" if settled else "open_bets"
    bets = [b for b in (ledger.data.get(key) or []) if b.get("confirmed")]
    rev_key = "settled_at" if settled else "placed_at"
    bets.sort(key=lambda b: b.get(rev_key, ""), reverse=True)
    return bets


def _game_section(title: str, bets: list[dict], settled: bool) -> None:
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(title).style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(str(len(bets))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not bets:
            ui.label("No bets yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        for b in bets:
            _game_bet_row(b, settled)


def _game_bet_row(b: dict, settled: bool) -> None:
    result       = (b.get("result") or "").lower()
    result_color = {
        "win": t.POS, "loss": t.NEG, "push": t.WARN, "void": t.TEXT_DIM2,
    }.get(result, t.TEXT_DIM)

    sport    = (b.get("sport") or "mlb").upper()
    team     = b.get("bet_team") or b.get("parlay_name") or "—"
    bet_type = (b.get("bet_type") or "single").upper().replace("_", " ")
    odds     = b.get("american_odds")
    odds_s   = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else f"{odds}"
    amount   = float(b.get("confirmed_amount") or 0)
    pnl      = float(b.get("confirmed_pnl")    or 0) if settled else 0.0

    if settled and result == "win":
        team_color, amount_text, amount_color = t.POS, f"+${pnl:.2f}", t.POS
    elif settled and result == "loss":
        team_color, amount_text, amount_color = t.NEG, f"-${amount:.2f}", t.NEG
    elif settled and result == "push":
        team_color, amount_text, amount_color = t.TEXT, "$0.00", t.TEXT_DIM
    else:
        team_color, amount_text, amount_color = t.TEXT, f"${amount:.2f}", t.TEXT

    border = (
        f"1px solid {result_color}" if settled and result in ("win", "loss", "push")
        else f"1px solid {t.BORDER}"
    )

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 10px;"
    ):
        ui.label(sport).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL};"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(team).style(
                f"font-size: 13px; font-weight: 700; color: {team_color}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{bet_type} · {odds_s}").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
        with ui.column().style("gap: 2px; text-align: right; align-items: flex-end;"):
            ui.label(amount_text).style(
                f"font-size: 13px; font-weight: 700; "
                f"color: {amount_color}; font-family: monospace;"
            )
            if settled:
                ui.label(result.upper()).style(
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {result_color};"
                )
            else:
                ui.label("PENDING").style(
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )


# ── Props bets ───────────────────────────────────────────────────────────────

_MARKET_LABEL: dict[str, str] = {
    "pitcher_strikeouts":   "Ks",
    "pitcher_outs":         "Outs",
    "pitcher_hits_allowed": "H Allow",
    "pitcher_walks":        "BB Allow",
    "pitcher_earned_runs":  "ER",
    "batter_hits":          "Hits",
    "batter_total_bases":   "Total Bases",
    "batter_home_runs":     "Home Runs",
    "batter_rbis":          "RBIs",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
}


def _load_props_bets() -> tuple[list[dict], list[dict]]:
    """Return (open_bets, history) from PropsLedger."""
    try:
        from src.props_ledger import get_props_ledger
        pl = get_props_ledger()
        pl.reload()
        return pl.get_open_bets(), pl.get_history()
    except Exception:                                                      # noqa: BLE001
        return [], []


def _props_record() -> None:
    """Small record summary card for prop picks."""
    try:
        from src.props_ledger import get_props_ledger
        rec = get_props_ledger().get_record()
    except Exception:                                                      # noqa: BLE001
        rec = {"wins": 0, "losses": 0, "voids": 0, "open": 0, "total": 0, "pct": None}

    w, l, total = rec["wins"], rec["losses"], rec["total"]
    pct = rec["pct"]
    pct_s   = f"{pct * 100:.1f}%" if pct is not None else "—"
    pct_col = t.POS if (pct or 0) >= 0.55 else (t.NEG if (pct or 0.5) < 0.50 else t.TEXT_DIM)

    with ui.row().classes("w-full items-center").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: {t.SPACE_LG};"
    ):
        with ui.column().style("gap: 2px;"):
            ui.label("PROPS MODEL RECORD").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT_DIM2};"
            )
            ui.label(f"{w}-{l}").style(
                f"font-size: 22px; font-weight: 800; color: {t.TEXT}; font-family: monospace;"
            )
        with ui.column().style("gap: 2px;"):
            ui.label(pct_s).style(
                f"font-size: 16px; font-weight: 800; color: {pct_col}; font-family: monospace;"
            )
            ui.label(f"{total} settled · {rec.get('open', 0)} open").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )


def _props_open_bets() -> None:
    open_bets, _ = _load_props_bets()
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("OPEN PROPS BETS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT};"
            )
            ui.label(str(len(open_bets))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not open_bets:
            ui.label(
                "No open props bets. Track picks from the Props page."
            ).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                f"text-align: center; font-style: italic;"
            )
            return
        for b in open_bets:
            _prop_bet_row(b, settled=False)


def _props_history() -> None:
    _, history = _load_props_bets()
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("SETTLED PROPS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT};"
            )
            ui.label(str(len(history[:50]))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not history:
            ui.label("No settled props bets yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        for b in history[:50]:
            _prop_bet_row(b, settled=True)


def _prop_bet_row(b: dict, settled: bool) -> None:
    """Single row card for a prop pick (open or settled)."""
    result       = (b.get("result") or "").lower()
    result_color = {
        "win": t.POS, "loss": t.NEG, "void": t.WARN,
    }.get(result, t.TEXT_DIM)

    side    = (b.get("side") or "Over").strip().title()
    is_over = side == "Over"
    side_bg = t.POS if is_over else t.NEG

    player  = b.get("player") or "—"
    market  = _MARKET_LABEL.get(b.get("market", ""), (b.get("market") or "").replace("_", " ").title())
    line    = b.get("line")
    line_s  = f"{float(line):.1f}" if line is not None else "—"
    conf    = b.get("confidence")
    conf_s  = f"{conf * 100:.0f}%" if conf is not None else "—"
    pv      = b.get("predicted_value")
    pv_s    = f"{pv:.1f}" if pv is not None else None
    actual  = b.get("actual_value")
    actual_s = f"{float(actual):.1f}" if actual is not None else None
    odds    = b.get("odds")
    odds_s  = (f"+{odds}" if odds > 0 else str(odds)) if isinstance(odds, int) else "—"
    team    = b.get("team") or ""

    border = (
        f"1px solid {result_color}" if settled and result in ("win", "loss", "void")
        else f"1px solid {t.BORDER}"
    )

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 6px;"
    ):
        # Header: market label + team + result/pending badge
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(market.upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if team:
                ui.label(team).style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; font-family: monospace;"
                )
            ui.element("div").style("flex: 1;")
            if settled:
                badge_label = result.upper() if result else "—"
                ui.label(badge_label).style(
                    f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {result_color};"
                )
            else:
                ui.label("PENDING").style(
                    f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )

        # Player name + side chip
        with ui.row().classes("items-center w-full").style("gap: 10px;"):
            ui.label(player).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{side.upper()} {line_s}").style(
                f"background: {side_bg}; color: {t.BG}; "
                f"font-size: 11px; font-weight: 800; "
                f"padding: 3px 9px; border-radius: {t.RADIUS_SM}; flex-shrink: 0;"
            )

        # Stats row: confidence + predicted + actual (if settled) + odds
        with ui.row().classes("items-center w-full").style("gap: 14px; flex-wrap: wrap;"):
            _mini_stat("CONF", conf_s)
            if pv_s:
                _mini_stat("MODEL", pv_s)
            if settled and actual_s is not None:
                _mini_stat("ACTUAL", actual_s,
                           t.POS if result == "win" else (t.NEG if result == "loss" else t.WARN))
            ui.element("div").style("flex: 1;")
            _mini_stat("ODDS", odds_s)


def _mini_stat(label: str, value: str, value_color: str | None = None) -> None:
    color = value_color or t.TEXT
    with ui.column().style("gap: 1px; align-items: flex-start;"):
        ui.label(label).style(
            f"font-size: 9px; font-weight: 800; letter-spacing: .5px; color: {t.TEXT_DIM2};"
        )
        ui.label(value).style(
            f"font-size: 12px; font-weight: 700; color: {color}; font-family: monospace;"
        )
