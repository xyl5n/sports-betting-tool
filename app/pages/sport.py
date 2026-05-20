"""
Sports page -- two routes, one renderer.

/sports/mlb   -> register handles MLB schedule + predictions
/sports/wnba  -> same shell, WNBA data

Each game row is a `game_card.render()` invocation against the
in-memory analysis cache (_analysis_state / _wnba_analysis_state).

Live-score polling
------------------
On page open we fetch /api/{sport}/schedule?hydrate=linescore once
(populates components/live_score's module cache), then schedule a
ui.timer(60s, ...) that re-fetches + refreshes the game grid.  Each
card consults live_score.lookup(...) by gamePk (with a team-name
fallback) inside game_card.render -- LIVE games show the big center
score + inning + B/S/O detail, FINAL games show the score + Final
label, scheduled games show the matchup row with VS between names.
"""
from __future__ import annotations

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, game_card, bottom_nav, live_score
from components import completion_watcher


_LIVE_POLL_INTERVAL = 60.0   # seconds between live-score refresh ticks


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

    # Kick off the live-score poller for this page.  Fetches the linescore
    # feed immediately + every 60 seconds thereafter, populating the cache
    # live_score.lookup() reads inside game_card.render.  The refreshable
    # grid below redraws on each tick so the latest score lands in the UI
    # without any per-card binding gymnastics.
    live_score.fetch_live(backend, sport)

    with ui.row().classes("no-wrap w-full").style("gap: 0;"):
        sidebar.render(backend)
        with ui.column().classes("page-content").style(
            f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
            f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
        ):
            _header(sport)
            _odds_quota_banner(backend)
            # First render of the refreshable grid -- args are captured so
            # `.refresh()` on tick re-uses the same backend + sport.
            _refreshable_grid(backend, sport)

            def _tick() -> None:
                live_score.fetch_live(backend, sport)
                _refreshable_grid.refresh()

            ui.timer(_LIVE_POLL_INTERVAL, _tick)

            # Watch for analysis completions kicked off from /admin or
            # the scheduler.  Forces a full ui.navigate.reload() rather
            # than calling _refreshable_grid.refresh() -- the refreshable
            # path was redrawing from closures that still pointed at
            # stale Python state on this NiceGUI version, so users saw
            # the same picks even after the worker wrote fresh ones.
            completion_watcher.mount(backend)

    bottom_nav.render(active=t.TAB_SPORTS)


def _odds_quota_banner(backend) -> None:
    """Show the 'Daily Odds API limit reached' banner when applicable.

    Hits /api/odds/usage (cheap, no upstream traffic).  When the daily cap
    is reached, renders a dashed-red strip above the game grid so the
    user sees immediately why automatic refreshes aren't happening.

    Silent when the limit hasn't been hit -- no decoration so the slate
    layout stays clean on normal days.
    """
    try:
        client = backend.app.test_client()
        resp   = client.get("/api/odds/usage")
        data   = resp.get_json(force=True, silent=True) or {}
    except Exception:                                                     # noqa: BLE001
        return
    if not data.get("limit_reached"):
        return
    count = int(data.get("count") or 0)
    limit = int(data.get("effective_limit") or 500)
    with ui.row().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.NEG}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 14px; "
        f"gap: 8px; align-items: center;"
    ):
        ui.icon("warning").style(f"font-size: 18px; color: {t.NEG};")
        with ui.column().style("flex: 1; gap: 2px;"):
            ui.label(
                f"Daily Odds API limit of {limit} reached, "
                f"additional pulls require manual approval."
            ).style(
                f"font-size: 12.5px; font-weight: 700; color: {t.NEG};"
            )
            ui.label(
                f"{count} / {limit} requests used today.  Open /admin and "
                f"click Approve Additional Odds Pull (+50) to allow more."
            ).style(
                f"font-size: 11.5px; color: {t.TEXT_DIM};"
            )


@ui.refreshable
def _refreshable_grid(backend, sport: str) -> None:
    """Wrapper so the timer can call `.refresh()` on tick.  Re-runs
    _game_grid which re-reads live_score's cache on every render."""
    _game_grid(backend, sport)


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
        # backend=backend so each card renders a Track button wired to
        # /api/{sport}/ledger/confirm/<game_id> via the Flask test client.
        game_card.render(g, sport=sport, backend=backend)


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

    # Append no-model stubs for games the model couldn't predict (e.g.
    # 2026 WNBA expansion teams without training data).  WNBA only --
    # MLB doesn't track skipped games yet.  The stubs render as cards
    # with matchup + market odds + a NO MODEL PICK badge so the user
    # at least sees the game is on tonight.
    if sport == "wnba":
        try:
            stub_fn = backend._serialize_wnba_no_model
            for sk in (state.get("skipped") or []):
                game = sk.get("game")
                if not game:
                    continue
                reason = (
                    f"No model pick: {sk.get('detail') or sk.get('reason') or '—'}"
                )
                try:
                    out.append(stub_fn(game, reason))
                except Exception:                                         # noqa: BLE001
                    continue
        except AttributeError:
            # backend._serialize_wnba_no_model not yet deployed -- ignore.
            pass

    return out
