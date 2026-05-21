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
from components import navbar, game_card, bottom_nav, live_score


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

    # Sidebar (Top 5 Plays + Confidence Performance) is intentionally
    # NOT rendered on the slate page -- the home screen already shows
    # the highest-confidence picks + the EV scan, so duplicating them
    # here was pure noise.  Game cards now get the full content
    # column width.  `margin: 0 auto` centers it within the wider
    # viewport since the row no longer has a sidebar-occupied left
    # column to push against.
    # Closure dict so the date-nav click handlers + refreshable grid
    # can read + write the currently-selected date without prop drilling.
    today_str = backend._today_et()
    state = {"date": today_str, "today": today_str}

    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        _header(sport)
        _odds_quota_banner(backend)
        _date_nav(state)
        _refreshable_grid(backend, sport, state)

        def _tick() -> None:
            live_score.fetch_live(backend, sport)
            _refreshable_grid.refresh()

        ui.timer(_LIVE_POLL_INTERVAL, _tick)

        # Analysis-completion watcher.  Self-canceling: fires once when
        # an analyze running ELSEWHERE (admin click in another tab,
        # 8 AM scheduler, manual API call) writes a fresh completed_at
        # to backend._analysis_progress, then refreshes the grid in
        # place and cancels itself.  Avoids the "Timer cancelled because
        # client is not connected" noise from long-running polling
        # timers -- bounded by a 5-minute TTL so the watcher stops
        # polling even if no analyze ever completes.
        _completion_watcher(backend)

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


def _completion_watcher(backend) -> None:
    """Self-canceling ui.timer that refreshes the game grid when an
    analyze completes elsewhere.

    Polls backend._analysis_progress (the module-level dict populated
    by /api/analyze workers) every 2 s.  When any sport's completed_at
    timestamp exceeds the page's mount_ts, refreshes the grid in place
    and cancels itself -- one refresh per page session is enough.

    Bounded by a 5-minute TTL (150 ticks at 2 s) so the timer always
    stops eventually even when no analyze ever runs -- this is what
    "short-lived" in the user spec means.  Without a TTL the timer
    would keep firing the (cheap) callback forever and emit "Timer
    cancelled because client is not connected" noise in Railway logs
    every time someone navigates away from /sports/*.
    """
    import time as _time
    mount_ts = _time.time()
    state_ref: dict = {"timer": None, "ticks": 0, "fired": False}
    _MAX_TICKS = 150   # 150 ticks * 2 s = 5 minutes

    def _kill() -> None:
        timer = state_ref.get("timer")
        if timer is None:
            return
        try:
            timer.deactivate()
        except Exception:                                                  # noqa: BLE001
            pass
        try:
            timer.delete()
        except Exception:                                                  # noqa: BLE001
            pass

    def _tick() -> None:
        if state_ref["fired"]:
            return
        state_ref["ticks"] += 1

        # TTL bound -- stop polling after 5 minutes of no completion.
        if state_ref["ticks"] >= _MAX_TICKS:
            state_ref["fired"] = True
            _kill()
            return

        try:
            progress = getattr(backend, "_analysis_progress", None)
        except Exception:                                                  # noqa: BLE001
            return
        if not isinstance(progress, dict):
            return

        for sport_key, row in progress.items():
            ts = (row or {}).get("completed_at")
            if not ts:
                continue
            try:
                completed = float(ts)
            except (TypeError, ValueError):
                continue
            if completed > mount_ts:
                state_ref["fired"] = True
                try:
                    _refreshable_grid.refresh()
                except Exception:                                          # noqa: BLE001
                    pass
                _kill()
                return

    state_ref["timer"] = ui.timer(2.0, _tick, active=True)


@ui.refreshable
def _refreshable_grid(backend, sport: str, state: dict) -> None:
    """Wrapper so the timer can call `.refresh()` on tick.  Re-runs
    _game_grid which re-reads live_score's cache on every render.

    `state["date"]` is the currently-selected ET date string -- mutated
    by the date-nav click handlers and read by _game_grid to pick the
    right schedule slice."""
    _game_grid(backend, sport, state)


@ui.refreshable
def _date_nav(state: dict) -> None:
    """Top-of-slate date navigation: < arrow | date label + calendar
    icon | > arrow.  Calendar icon opens a ui.menu containing a
    ui.date picker so users can jump to any date in one click instead
    of arrowing through one day at a time.

    Decorated @ui.refreshable so the click handlers can re-render
    this whole row (and crucially the date label) when state["date"]
    mutates -- without the decorator the label was created once with
    the initial value and never updated on subsequent nav clicks.

    Tap targets sized 36px+ here, bumped to 44px on mobile via the
    global rule in components/theme.py."""
    from datetime import date as _date, timedelta as _td

    today_str = state["today"]

    def _set_date(d: str) -> None:
        if not d:
            return
        # ui.date returns the value as either str or datetime depending
        # on Quasar version; coerce to ISO string for consistency.
        state["date"] = str(d)[:10]
        # Refresh BOTH the date nav (so the label + picker initial value
        # pick up the new date) AND the grid (so the cards reload for
        # the new schedule).  Without the _date_nav.refresh() call the
        # label was stuck at whatever string it had on first render.
        _date_nav.refresh()
        _refreshable_grid.refresh()

    def _step(delta_days: int) -> None:
        cur = _date.fromisoformat(state["date"])
        _set_date((cur + _td(days=delta_days)).isoformat())

    current = state["date"]
    pretty  = _pretty_date(current, today_str)

    with ui.row().classes("items-center w-full no-wrap").style(
        f"gap: 8px; background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 8px 12px; "
        f"justify-content: center;"
    ):
        # Left arrow -- one day back
        ui.button(icon="chevron_left", on_click=lambda: _step(-1)).props(
            "flat dense round"
        ).style(
            f"background: {t.CARD_HI}; color: {t.TEXT}; "
            f"min-width: 36px; min-height: 36px;"
        ).tooltip("Previous day")

        # Date label + calendar icon (icon opens the picker menu)
        with ui.row().classes("items-center").style("gap: 8px;"):
            ui.label(pretty).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"letter-spacing: .2px;"
            )
            calendar_btn = ui.button(icon="calendar_month").props(
                "flat dense round"
            ).style(
                f"background: {t.CARD_HI}; color: {t.PRIMARY}; "
                f"min-width: 36px; min-height: 36px;"
            ).tooltip("Pick any date")
            with calendar_btn:
                with ui.menu() as menu:
                    # ui.date is the Quasar QDate wrapper.  on_change
                    # fires with e.value as a 'YYYY-MM-DD' string.
                    # Close the menu after a pick so the next nav
                    # interaction doesn't need an extra click.
                    def _on_pick(e):
                        _set_date(e.value)
                        try:
                            menu.close()
                        except Exception:                                  # noqa: BLE001
                            pass
                    ui.date(value=current, on_change=_on_pick).props(
                        "color=primary"
                    )

        # Right arrow -- one day forward
        ui.button(icon="chevron_right", on_click=lambda: _step(+1)).props(
            "flat dense round"
        ).style(
            f"background: {t.CARD_HI}; color: {t.TEXT}; "
            f"min-width: 36px; min-height: 36px;"
        ).tooltip("Next day")


def _pretty_date(current: str, today: str) -> str:
    """Format the date label.  Per spec: 'Tuesday May 21' when the
    selected date is the current year; year shown only when crossing
    a year boundary so the label stays compact day-to-day."""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(current)
        td = _date.fromisoformat(today)
    except Exception:                                                      # noqa: BLE001
        return current
    if d.year == td.year:
        return d.strftime("%A %B %-d")
    return d.strftime("%A %B %-d, %Y")


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


def _norm_team(name) -> str:
    """Aggressive team-name normalization for cross-API matching.
    Lowercases, strips whitespace, and drops every non-alphanumeric
    character.  "Los Angeles Dodgers" -> "losangelesdodgers", same as
    "LA Dodgers".  Used by the renderer-side merge below to bridge
    MLB statsapi game ids to Odds API ids when the schedule endpoint
    fails to merge them itself."""
    if not name:
        return ""
    s = str(name).lower()
    return "".join(ch for ch in s if ch.isalnum())


def _game_et_date(commence_time) -> str:
    """Convert an ISO commence_time to its ET calendar date.  Returns
    "" on parse failure so unparseable rows don't collide on a single
    empty-date bucket."""
    if not commence_time:
        return ""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return ""


def _analysis_index(backend, sport: str) -> dict[tuple[str, str, str], dict]:
    """Build a {(home_norm, away_norm, et_date): serialized_game} index
    from every place an analysis result could live -- in-memory state
    + on-disk cache.  Used by _game_grid to merge schedule stubs with
    full analysis rows when /api/schedule failed to do the join (the
    ID-mismatch bug -- MLB statsapi gamePk vs Odds API hex id).

    Tries hard to produce a flat serialized row for each entry: if the
    row is already serialized (passthrough shape with home_team /
    away_team top-level), it's used as-is; if it's a raw analysis dict
    (nested r["game"]) the backend's _serialize / _serialize_wnba is
    called to flatten it.  Serialization failures are skipped silently
    so one bad row doesn't break the merge.
    """
    state = (
        backend._wnba_analysis_state if sport == "wnba"
        else backend._analysis_state
    )
    rows: list[dict] = list(state.get("results") or [])

    # Also pull from the daily snapshot on disk -- when the analyze
    # route ran in a prior worker process the in-memory state in this
    # NiceGUI worker may be empty even though the snapshot has the
    # fresh picks.  hydrate_state() is the canonical path but tolerate
    # its absence.
    try:
        backend.hydrate_state()
        rows = list(state.get("results") or [])
    except Exception:                                                     # noqa: BLE001
        pass

    out: dict[tuple[str, str, str], dict] = {}
    try:
        bankroll = float(state.get("bankroll") or (1000 if sport == "wnba" else 250))
    except Exception:                                                     # noqa: BLE001
        bankroll = 250.0
    for r in rows:
        try:
            if "home_team" in r and "away_team" in r:
                ser = dict(r)
            else:
                if sport == "wnba":
                    ser = backend._serialize_wnba(r, bankroll, bankroll)
                else:
                    ser = backend._serialize(r, bankroll, "mlb", bankroll)
        except Exception:                                                 # noqa: BLE001
            continue
        home = _norm_team(ser.get("home_team"))
        away = _norm_team(ser.get("away_team"))
        et_d = _game_et_date(ser.get("commence_time"))
        if home and away and et_d:
            out[(home, away, et_d)] = ser
    return out


def _merge_schedule_with_analysis(backend, sport: str, games: list[dict]) -> list[dict]:
    """For each schedule row flagged _no_odds=True, try to find a
    matching analysis row by (home_norm, away_norm, et_date).  When a
    match is found, swap the stub for the analysis row so the card
    renders with full odds + picks instead of "NO ODDS AVAILABLE".
    Logs MERGED / NO MATCH per game so the deploy log shows whether
    the join did anything.
    """
    if not games:
        return games
    idx = _analysis_index(backend, sport)
    _dbg(f"_merge_schedule_with_analysis [{sport.upper()}] analysis_index_size={len(idx)}")
    if not idx:
        for g in games:
            _dbg(
                f"  NO MATCH (empty index) kept as stub  "
                f"gid={g.get('game_id') or g.get('id')!r}  "
                f"{g.get('away_team')} @ {g.get('home_team')}"
            )
        return games

    out: list[dict] = []
    merged_count = 0
    stub_count   = 0
    for g in games:
        # Only attempt the merge for stubs -- rows that came back from
        # /api/schedule with full odds (_no_odds is False or absent and
        # pick_team is set) already won the join server-side.
        already_merged = (not g.get("_no_odds")) and bool(g.get("pick_team"))
        gid = g.get("game_id") or g.get("id")
        if already_merged:
            _dbg(
                f"  MERGED with odds (server-side) gid={gid!r}  "
                f"{g.get('away_team')} @ {g.get('home_team')}  "
                f"data_source={g.get('_data_source')!r}"
            )
            out.append(g)
            continue

        key = (
            _norm_team(g.get("home_team")),
            _norm_team(g.get("away_team")),
            _game_et_date(g.get("commence_time")),
        )
        match = idx.get(key)
        if match is None:
            _dbg(
                f"  NO MATCH kept as stub  gid={gid!r}  "
                f"{g.get('away_team')} @ {g.get('home_team')}  "
                f"key={key}"
            )
            stub_count += 1
            out.append(g)
            continue

        # Preserve schedule-only fields the analysis row doesn't carry
        # (status, final-score linescore values, schedule id).  Layer
        # the analysis row on top so its odds + pick_* fields win.
        merged = dict(match)
        merged["_data_source"] = "renderer_merged_team_date"
        merged["_schedule_id"] = str(gid) if gid else merged.get("_schedule_id")
        if g.get("_status"):
            merged["_status"] = g["_status"]
        if g.get("_final_score"):
            merged["_final_score"] = g["_final_score"]
        # Clear any leftover no-odds markers since we now have odds.
        merged.pop("_no_odds", None)
        merged.pop("_model_prediction", None)
        _dbg(
            f"  MERGED with odds (renderer) gid={gid!r}  "
            f"{g.get('away_team')} @ {g.get('home_team')}  "
            f"-> pick_team={merged.get('pick_team')!r}"
        )
        merged_count += 1
        out.append(merged)

    _dbg(
        f"_merge_schedule_with_analysis [{sport.upper()}] "
        f"merged={merged_count} kept_as_stub={stub_count}"
    )
    return out


def _game_grid(backend, sport: str, state: dict) -> None:
    """Render every game from the schedule for the currently-selected
    date.  Games with model picks get the full bet-box card; games
    without odds get a 'No Odds Available' placeholder.

    Source of truth is /api/schedule/<sport>?date=...  -- joins the
    free MLB Stats / ESPN feed with whatever picks live in
    _analysis_state (today) or the ledger history (past dates).
    Cached per-date in Supabase by the backend so repeated visits
    don't burn live API calls.

    Defense in depth: even when /api/schedule misses the join (MLB
    statsapi gamePk vs Odds API hex id), the renderer re-runs the
    match by normalized team name + ET date before drawing each
    card.  See _merge_schedule_with_analysis for the details.
    """
    date_str = state["date"]
    _dbg(f"_game_grid ENTER sport={sport} date={date_str}")

    try:
        client = backend.app.test_client()
        resp   = client.get(f"/api/schedule/{sport}?date={date_str}")
        data   = resp.get_json(force=True, silent=True) or {}
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"_game_grid schedule fetch FAILED: {type(exc).__name__}: {exc}")
        data = {}
    games = data.get("games") or []
    _dbg(f"_game_grid RENDER {sport.upper()} SLATE: {len(games)} games for {date_str} (pre-merge)")

    # Only attempt the renderer-side merge for today's slate -- past
    # dates render from ledger history (settled bets), and future
    # dates render from the schedule alone (no analysis was ever run).
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        today_et = ""
    if date_str == today_et:
        games = _merge_schedule_with_analysis(backend, sport, games)
        _dbg(f"_game_grid RENDER {sport.upper()} SLATE: {len(games)} games for {date_str} (post-merge)")

    if not games:
        ui.label(
            f"No {sport.upper()} games scheduled for {date_str}."
        ).style(
            f"color: {t.TEXT_DIM}; font-size: 12px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; text-align: center;"
        )
        return

    # Two-column CSS grid on desktop, single column on mobile.  The
    # `.game-grid` class is defined in components/theme.py with a
    # 768px media query so the layout flips automatically without
    # any JS.  Cards fill left-to-right top-to-bottom -- when an odd
    # game count lands the final card occupies the left column only,
    # which is what CSS grid does by default.
    with ui.element("div").classes("game-grid w-full"):
        for i, g in enumerate(games):
            gid = g.get("game_id") or g.get("id") or "?"
            away = g.get("away_team", "?")
            home = g.get("home_team", "?")
            _dbg(f"_game_grid CARD[{i}] gid={gid} {away} @ {home} no_odds={g.get('_no_odds')}")
            try:
                game_card.render(g, sport=sport, backend=backend)
            except Exception as exc:                                       # noqa: BLE001
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
