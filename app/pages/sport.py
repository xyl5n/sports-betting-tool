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

import sys

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, game_card, bottom_nav, live_score


_LIVE_POLL_INTERVAL = 60.0   # seconds between live-score refresh ticks


def _dbg(msg: str) -> None:
    """Diagnostic print -- always flushes to stderr so the Railway log
    stream picks it up.  Tagged so it's grep-able in production."""
    print(f"[RENDER] {msg}", flush=True, file=sys.stderr)


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
    _dbg(f"_render_sport ENTER sport={sport!r}")
    # Re-read today's analysis cache into the in-memory state dict so
    # this render sees the newest picks on disk.  Without this, a Run
    # Analysis triggered elsewhere (admin, scheduler) only becomes
    # visible after a container restart.
    try:
        mlb_n, wnba_n = backend.hydrate_state()
        _dbg(f"_render_sport hydrate_state returned mlb={mlb_n} wnba={wnba_n}")
    except Exception as exc:                                               # noqa: BLE001
        _dbg(f"_render_sport hydrate_state FAILED: {type(exc).__name__}: {exc}")

    # State sanity check -- prove the dict the renderer is about to read
    # actually has the games hydrate just wrote.  If the keys here are
    # 0 even though hydrate returned non-zero, the issue is a state
    # reference mismatch (different module imported under a different
    # path, or _analysis_state rebound somewhere).
    try:
        state = backend._analysis_state if sport == "mlb" else backend._wnba_analysis_state
        n_results = len(state.get("results") or [])
        _dbg(
            f"_render_sport STATE_CHECK sport={sport} "
            f"results={n_results} "
            f"bankroll={state.get('bankroll')!r} "
            f"keys={list(state.keys())}"
        )
    except Exception as exc:                                               # noqa: BLE001
        _dbg(f"_render_sport STATE_CHECK FAILED: {type(exc).__name__}: {exc}")

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
    _dbg(f"_game_grid ENTER sport={sport}")
    games = _serialized_games(backend, sport)
    _dbg(f"_game_grid RENDER {sport.upper()} SLATE: {len(games)} games")
    if not games:
        _dbg(f"_game_grid SHOWING EMPTY-STATE for {sport} (no serialized games)")
        ui.label(
            f"No {sport.upper()} games loaded yet -- run analysis to populate."
        ).style(
            f"color: {t.TEXT_DIM}; font-size: 12px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; text-align: center;"
        )
        return

    for i, g in enumerate(games):
        gid = g.get("game_id") or g.get("id") or "?"
        away = g.get("away_team", "?")
        home = g.get("home_team", "?")
        _dbg(f"_game_grid CARD[{i}] gid={gid} {away} @ {home}")
        # backend=backend so each card renders a Track button wired to
        # /api/{sport}/ledger/confirm/<game_id> via the Flask test client.
        try:
            game_card.render(g, sport=sport, backend=backend)
        except Exception as exc:                                           # noqa: BLE001
            import traceback as _tb
            _dbg(
                f"_game_grid CARD[{i}] RENDER FAILED: "
                f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"
            )


def _serialized_games(backend, sport: str) -> list[dict]:
    """Pull the cached results for *sport* and serialize them via the
    same _serialize / _serialize_wnba functions the Flask UI used."""
    _dbg(f"_serialized_games ENTER sport={sport}")
    try:
        if sport == "mlb":
            state    = backend._analysis_state
            ser_fn   = backend._serialize
            ledg     = backend.Ledger(path="data/ledger.json", starting_bankroll=250)
        else:
            state    = backend._wnba_analysis_state
            ser_fn   = backend._serialize_wnba
            ledg     = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000)
    except Exception as exc:                                              # noqa: BLE001
        _dbg(
            f"_serialized_games SETUP FAILED sport={sport}: "
            f"{type(exc).__name__}: {exc}"
        )
        return []

    bankroll = float(state.get("bankroll") or 250)
    s_bank   = ledg.data.get("personal_starting_bankroll", bankroll)
    results  = state.get("results") or []
    _dbg(
        f"_serialized_games sport={sport} "
        f"results_in_state={len(results)} bankroll={bankroll} s_bank={s_bank}"
    )
    out: list[dict] = []
    failures = 0
    passthrough = 0
    first_err: str | None = None
    for i, r in enumerate(results):
        try:
            # Pre-serialized passthrough: when the state was hydrated
            # from data/{,wnba_}analysis_cache.json or the daily
            # snapshot, the cached entries are ALREADY flat
            # _serialize() outputs (home_team, away_team, pick_prob,
            # etc.).  Calling _serialize() on them crashes with
            # KeyError: 'game' because the raw nested shape (r["game"],
            # r["prediction"]) only exists in the in-process post-
            # analyze pipeline.  The guard distinguishes the two
            # shapes by the presence of the user-facing team names.
            if "home_team" in r and "away_team" in r:
                g = dict(r)
                passthrough += 1
            else:
                if sport == "mlb":
                    g = ser_fn(r, bankroll, "mlb", s_bank)
                else:
                    g = ser_fn(r, bankroll, s_bank)
            out.append(g)
        except Exception as exc:                                          # noqa: BLE001
            failures += 1
            if first_err is None:
                import traceback as _tb
                first_err = f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"
                _dbg(
                    f"_serialized_games sport={sport} "
                    f"FIRST SERIALIZE FAILURE on game[{i}]: {first_err}"
                )
            continue
    if passthrough:
        _dbg(
            f"_serialized_games sport={sport} passthrough={passthrough} "
            f"(already-serialized cache entries)"
        )
    if failures:
        _dbg(
            f"_serialized_games sport={sport} {failures} of {len(results)} "
            f"games failed to serialize"
        )

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
