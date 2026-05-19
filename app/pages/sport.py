"""
Sports page -- two routes, one renderer.

/sports/mlb   -> register handles MLB schedule + predictions
/sports/wnba  -> same shell, WNBA data

Each game row is a `game_card.render()` invocation against the
in-memory analysis cache (_analysis_state / _wnba_analysis_state).
Live-state polling (the "inning / outs / clock" detail line) is a
follow-up; this skeleton just shows the slate + pick boxes.
"""
from __future__ import annotations

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, game_card, bottom_nav


def register(backend) -> None:
    @ui.page("/sports/mlb")
    def mlb_page():
        _render_sport(backend, "mlb")

    @ui.page("/sports/wnba")
    def wnba_page():
        _render_sport(backend, "wnba")

    # Bare /sports route -- redirect to MLB as the default.
    @ui.page("/sports")
    def sports_default():
        ui.navigate.to("/sports/mlb")


def _render_sport(backend, sport: str) -> None:
    ui.add_head_html(t.page_head_css())
    navbar.render(active=t.TAB_SPORTS)
    with ui.row().classes("no-wrap w-full").style("gap: 0;"):
        sidebar.render(backend)
        with ui.column().classes("page-content").style(
            f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
            f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
        ):
            _header(sport)
            _game_grid(backend, sport)
    bottom_nav.render(active=t.TAB_SPORTS)


def _header(sport: str) -> None:
    with ui.row().classes("items-center w-full").style("gap: 12px; flex-wrap: wrap;"):
        ui.label(sport.upper()).classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label("today's slate").style(
            f"font-size: 12px; color: {t.TEXT_DIM};"
        )
        # Sport switcher pills on the right
        with ui.row().classes("items-center").style("margin-left: auto; gap: 6px;"):
            _pill("MLB",  "/sports/mlb",  active=sport == "mlb")
            _pill("WNBA", "/sports/wnba", active=sport == "wnba")


def _pill(label: str, href: str, active: bool) -> None:
    bg     = t.PRIMARY if active else t.CARD_HI
    color  = t.BG      if active else t.TEXT_DIM
    weight = "800"     if active else "600"
    with ui.link(target=href).style("text-decoration: none;"):
        ui.label(label).style(
            f"background: {bg}; color: {color}; font-weight: {weight}; "
            f"font-size: 12px; letter-spacing: .5px; "
            f"padding: 6px 14px; border-radius: {t.RADIUS_PILL};"
        )


def _game_grid(backend, sport: str) -> None:
    games = _serialized_games(backend, sport)
    if not games:
        ui.label(
            f"No {sport.upper()} games loaded yet -- run analysis to populate."
        ).style(
            f"color: {t.TEXT_DIM}; font-size: 12px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; text-align: center;"
        )
        return

    for g in games:
        game_card.render(g, sport=sport)


def _serialized_games(backend, sport: str) -> list[dict]:
    """Pull the cached results for *sport* and serialize them via the
    same _serialize / _serialize_wnba functions the Flask UI used."""
    try:
        if sport == "mlb":
            state    = backend._analysis_state
            ser_fn   = backend._serialize
            ledg     = backend.Ledger(path="data/ledger.json", starting_bankroll=250)
        else:
            state    = backend._wnba_analysis_state
            ser_fn   = backend._serialize_wnba
            ledg     = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000)
    except Exception:                                                     # noqa: BLE001
        return []

    bankroll = float(state.get("bankroll") or 250)
    s_bank   = ledg.data.get("personal_starting_bankroll", bankroll)
    results  = state.get("results") or []
    out: list[dict] = []
    for r in results:
        try:
            if sport == "mlb":
                g = ser_fn(r, bankroll, "mlb", s_bank)
            else:
                g = ser_fn(r, bankroll, s_bank)
            out.append(g)
        except Exception:                                                 # noqa: BLE001
            continue
    return out
