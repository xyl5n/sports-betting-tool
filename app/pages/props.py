"""
props.py
========
MLB player-props page.

Layout
------
1. Page title
2. Per-bucket model record (pitcher + batter W-L)
3. Filter bar -- game / market / minimum hit rate
4. Unified pick list, sorted by confidence DESC, with each card showing
   inline performance chips (season avg, L5/L10/L20 hit rate, H2H) and
   the opposing team's rank against that prop's market.

The pitcher/batter split that the prior version used is gone -- the
filter bar lets the user narrow the slate to whatever cross-section
they want (a single team, a single market, a hit-rate floor), so a
hard-coded split adds noise without helping discovery.
"""
from __future__ import annotations

import asyncio
import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, live_score


def _dbg(msg: str) -> None:
    """Tagged stderr log -- mirrors home.py's _dbg pattern."""
    print(f"[RENDER] {msg}", flush=True, file=sys.stderr)


def register(backend) -> None:
    @ui.page("/props")
    def props_page():
        _dbg("props_page ENTER")
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_PROPS)
            _layout(backend)
            bottom_nav.render(active=t.TAB_PROPS)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            tb_str = _tb.format_exc()
            print(
                f"[PROPS PAGE FATAL] {type(exc).__name__}: {exc}\n{tb_str}",
                flush=True, file=sys.stderr,
            )
            ui.label("Props page render failed").style(
                f"color: {t.NEG}; font-size: 16px; font-weight: 700; "
                f"padding: {t.SPACE_LG};"
            )
            ui.label(f"{type(exc).__name__}: {exc}").style(
                f"color: {t.TEXT_DIM}; font-family: monospace; "
                f"font-size: 12px; padding: 0 {t.SPACE_LG};"
            )


def _layout(backend) -> None:
    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        ui.label("PLAYER PROPS").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )

        _section_unified_props_list(backend)
        # Model trackers moved to the bottom, below all the pick cards.
        _section_model_record(backend)
        _settle_trigger(backend)


# ── Model record ────────────────────────────────────────────────────────────

def _section_model_record(backend) -> None:
    """Two side-by-side cards showing each bucket's W-L + win%."""
    try:
        from src import model_picks as _mp
        recs = _mp.prop_records("mlb")
        pitcher_rec, batter_rec = recs["pitcher"], recs["batter"]
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props model record load failed: {exc}")
        pitcher_rec = batter_rec = {"wins": 0, "losses": 0, "pct": None}
    # _record_card expects a 'total' field.
    for _rec in (pitcher_rec, batter_rec):
        _rec["total"] = int(_rec.get("wins") or 0) + int(_rec.get("losses") or 0)

    with ui.row().classes("w-full").style(
        f"gap: {t.SPACE_SM}; flex-wrap: nowrap;"
    ):
        _record_card("PITCHER MODEL", pitcher_rec)
        _record_card("BATTER MODEL",  batter_rec)


def _record_card(label: str, rec: dict) -> None:
    w, l, total = rec["wins"], rec["losses"], rec["total"]
    pct = rec["pct"]
    if pct is None:
        pct_s, pct_col = "—", t.TEXT_DIM2
    else:
        pct_s = f"{pct * 100:.1f}%"
        pct_col = t.POS if pct >= 0.55 else (t.NEG if pct < 0.50 else t.TEXT_DIM)
    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
        f"gap: 4px; flex: 1 1 0; min-width: 0;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(f"{w}-{l}").style(
            f"font-size: 18px; font-weight: 800; color: {t.TEXT}; "
            f"font-family: monospace;"
        )
        ui.label(f"{pct_s}  ({total} settled)").style(
            f"font-size: 11px; font-weight: 600; color: {pct_col}; "
            f"font-family: monospace;"
        )


# ── Unified props list (pitcher + batter combined, filter bar) ───────────────

def _section_unified_props_list(backend) -> None:
    """Render the pre-scored, pre-enriched picks from the props cache.

    Pure display layer -- this function NEVER calls predict() and never
    fetches gamelogs or opponent ranks.  The scoring + enrichment
    pipeline runs in the background scheduler (see
    src.props_scored_cache.score_today_props, wired into
    run_tier_1_refresh / run_tier_2_refresh).  If the scheduler hasn't
    populated the cache yet (e.g. before 11 AM ET on a fresh deploy)
    we render an empty-state message; we do NOT trigger a re-score
    from the page, which would block NiceGUI's event loop and drop
    the browser connection on slates with thousands of props.
    """
    try:
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props cache import failed: {exc}")
        return

    cache = load_scored_props() or {}
    all_rows: list[dict] = list(cache.get("picks") or [])
    # Only show props for games that have NOT started yet (FIX 1): drop any
    # whose start time has passed or whose game is Live/Final per the
    # schedule.  Cached in-process so this doesn't re-fetch on every render.
    rows = [
        r for r in all_rows
        if not live_score.game_has_started(
            backend,
            commence_time=r.get("commence_time"),
            home_team=r.get("home_team"),
            away_team=r.get("away_team"),
            sport="mlb",
        )
    ]
    rows.sort(key=lambda r: -float(r.get("confidence") or 0.0))

    _dbg(
        f"[PROPS-PAGE] read scored cache: {len(all_rows)} picks, "
        f"{len(rows)} upcoming (generated_at={cache.get('generated_at')})"
    )

    # Kick the breakdown queue for the full slate so each card's AI breakdown
    # (the same artifact the player page shows) gets generated via the
    # existing Groq pipeline + 150 ms spacing.  Lock-guarded -> launches once;
    # cards below show a loading state and populate as breakdowns land.
    try:
        from src import player_ai_breakdown as _pab
        _pab.launch_breakdown_queue(rows)
    except Exception as exc:                                               # noqa: BLE001
        _dbg(f"breakdown queue launch failed: {exc}")

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        # Header row -- title + count + last refresh
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("PICKS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            count_chip = ui.label(f"{len(rows)} picks").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if cache.get("generated_at"):
                ui.label(f"scored {_short_iso(cache['generated_at'])}").style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                    f"margin-left: auto; font-family: monospace;"
                )

        if not rows:
            if all_rows:
                # Had picks today, but every game has already started.
                _no_upcoming_message(
                    "No upcoming props",
                    "All of today's games have already started — check back tomorrow.",
                )
            else:
                _empty_state_message()
            return

        state = {"sort": "proj"}
        filters = _default_filters()

        # ── Card list (refreshable so sort + filters re-render in place) ──
        @ui.refreshable
        def cards_refresh() -> None:                                      # noqa: WPS430
            shown = [r for r in rows if _passes_filters(r, filters)]
            shown = _sorted_picks(shown, state)
            # Keep the header count in sync with what's actually shown.
            if _active_filter_count(filters):
                count_chip.set_text(f"{len(shown)} of {len(rows)} picks")
            else:
                count_chip.set_text(f"{len(rows)} picks")
            if not shown:
                _no_match_message()
                return
            with ui.element("div").classes("game-grid w-full"):
                for r in shown:
                    _prop_card(r, backend)

        # ── Filter button + collapsible panel ─────────────────────────────
        _filter_bar(rows, filters, cards_refresh.refresh)

        # ── Sort pills ─────────────────────────────────────────────────────
        # Single row of 6 pills (L5 / L10 / L20 / H2H / Proj / Edge); only the
        # order changes.  Default sort is Proj (biggest model-vs-book gap).
        _sort_pills(state, cards_refresh.refresh)
        cards_refresh()


def _no_match_message() -> None:
    """Shown when the active filters exclude every prop (never a blank list)."""
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL} {t.SPACE_LG}; "
        f"gap: 6px; align-items: center;"
    ):
        ui.label("No props match these filters").style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label("Loosen or reset the filters to see more props.").style(
            f"font-size: 12px; color: {t.TEXT_DIM}; text-align: center;"
        )


def _filter_bar(rows: list[dict], filters: dict, on_change) -> None:
    """Filter button + collapsible panel.  All controls read their option
    sets from the loaded props and mutate the shared *filters* dict, then call
    *on_change* (the card-list refresh) so the list re-renders without a page
    reload.  AND-stacked via _passes_filters."""
    market_opts, game_opts = _filter_options(rows)
    open_state = {"v": False}

    @ui.refreshable
    def bar() -> None:                                                    # noqa: WPS430
        n_active = _active_filter_count(filters)
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            label = "Filters" + (f" ({n_active})" if n_active else "")
            ui.button(label, icon="filter_list",
                      on_click=_toggle).props("no-caps unelevated dense").style(
                f"background: {(t.POS if n_active else 'transparent')} !important; "
                f"color: {(t.BG if n_active else t.TEXT_DIM)} !important; "
                f"border: 1px solid {(t.POS if n_active else t.BORDER)}; "
                f"min-height: 32px; padding: 4px 12px; font-size: 11.5px; "
                f"font-weight: 800; border-radius: {t.RADIUS_PILL};"
            )
            if n_active:
                ui.button("Reset filters",
                          on_click=_reset).props("no-caps flat dense").style(
                    f"color: {t.TEXT_DIM}; font-size: 11px; font-weight: 700; "
                    f"min-height: 32px; padding: 4px 8px;"
                )
        if open_state["v"]:
            _panel()

    def _toggle() -> None:
        open_state["v"] = not open_state["v"]
        bar.refresh()

    def _reset() -> None:
        filters.update(_default_filters())
        on_change()
        bar.refresh()

    def _set(key, value) -> None:
        filters[key] = value
        on_change()
        bar.refresh()

    def _panel() -> None:
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 12px;"
        ):
            sel_style = (f"min-width: 180px; flex: 1 1 200px;")
            with ui.row().classes("w-full").style("gap: 12px; flex-wrap: wrap;"):
                ui.select(_L10_OPTIONS, value=filters["min_l10"],
                          label="Min last-10 hit rate",
                          on_change=lambda e: _set("min_l10", e.value)) \
                    .props("outlined dense").style(sel_style)
                ui.select(_CONF_OPTIONS, value=filters["min_conf"],
                          label="Min model confidence",
                          on_change=lambda e: _set("min_conf", e.value)) \
                    .props("outlined dense").style(sel_style)
                ui.select(_GRADE_OPTIONS, value=filters["min_grade"],
                          label="Min matchup grade",
                          on_change=lambda e: _set("min_grade", e.value)) \
                    .props("outlined dense").style(sel_style)
            with ui.row().classes("w-full").style("gap: 12px; flex-wrap: wrap;"):
                ui.select(market_opts, value=sorted(filters["markets"]),
                          multiple=True, label="Prop type",
                          on_change=lambda e: _set("markets", set(e.value or []))) \
                    .props("outlined dense use-chips").style(sel_style)
                ui.select(game_opts, value=sorted(filters["games"]),
                          multiple=True, label="Game",
                          on_change=lambda e: _set("games", set(e.value or []))) \
                    .props("outlined dense use-chips").style(sel_style)
            with ui.row().classes("items-center").style("gap: 8px;"):
                ui.switch("Show alternative lines", value=filters["show_alt"],
                          on_change=lambda e: _set("show_alt", bool(e.value))) \
                    .props("dense")

    bar()


def _empty_state_message() -> None:
    """Friendly placeholder shown when the scored-cache is empty.

    Two flavours: pre-window (before 11 AM ET) we tell the user to
    come back when scoring starts; in-window we explain a refresh is
    in flight.  Either way we DO NOT trigger scoring from the page --
    that's a scheduler-only concern.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        pre_window = now_et.hour < 11
    except Exception:                                                     # noqa: BLE001
        pre_window = False

    if pre_window:
        title = "Props loading"
        body  = "Check back after 11 AM ET — scoring runs every 15 minutes during game hours."
    else:
        title = "Props refreshing"
        body  = "The next scored batch will appear here within a few minutes."

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL} {t.SPACE_LG}; "
        f"gap: 6px; align-items: center;"
    ):
        ui.label(title).style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label(body).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; text-align: center;"
        )


def _no_upcoming_message(title: str, body: str) -> None:
    """Placeholder shown when props exist today but all games have started."""
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL} {t.SPACE_LG}; "
        f"gap: 6px; align-items: center;"
    ):
        ui.label(title).style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label(body).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; text-align: center;"
        )


# ── Sort pills ───────────────────────────────────────────────────────────────
# A single row of six mutually-exclusive pills.  Each is a SORT (every
# pick stays visible -- only the order changes).  Default is "proj".

_SORT_PILLS: tuple[tuple[str, str], ...] = (
    ("l5",   "L5"),
    ("l10",  "L10"),
    ("l20",  "L20"),
    ("h2h",  "H2H"),
    ("proj", "Proj"),
    ("edge", "Edge"),
    ("conf", "Conf"),
)


def _sort_pills(state: dict, on_change) -> None:
    """Render the six sort pills in one horizontal row, no title label.
    Active pill is highlighted green; clicking sets state['sort'] and
    refreshes the card list."""
    @ui.refreshable
    def render() -> None:                                                 # noqa: WPS430
        with ui.row().classes("items-stretch w-full").style(
            "gap: 6px; flex-wrap: nowrap;"
        ):
            for key, label in _SORT_PILLS:
                _sort_pill(label, key, state.get("sort") == key,
                           state, on_change, render.refresh)
    render()


def _sort_pill(label: str, key: str, active: bool, state: dict,
               on_change, refresh) -> None:
    if active:
        bg, fg, border = t.POS, t.BG, t.POS
        shadow = "0 0 12px rgba(16, 185, 129, .35)"
    else:
        bg, fg, border = "transparent", t.TEXT_DIM, t.BORDER
        shadow = "none"

    def _click() -> None:
        state["sort"] = key
        on_change()
        refresh()

    ui.button(label, on_click=_click).props("no-caps unelevated dense").style(
        f"background: {bg} !important; color: {fg} !important; "
        f"border: 1px solid {border}; flex: 1 1 0; min-width: 0; "
        f"min-height: 32px; padding: 4px 0; "
        f"font-size: 11.5px; font-weight: 800; letter-spacing: .4px; "
        f"border-radius: {t.RADIUS_PILL}; box-shadow: {shadow} !important;"
    )


# ── Sorting ───────────────────────────────────────────────────────────────────

def _window_hit_rate(r: dict, window: str) -> float:
    """Hit rate (0..1) for a summary window key prefix.  Returns -1.0
    when the pick has no games in that window so it sorts last."""
    s = r.get("summary") or {}
    hits  = s.get(f"{window}_hits") or 0
    games = s.get(f"{window}_games") or 0
    if not games:
        return -1.0
    return hits / games


def _proj_gap(r: dict) -> float:
    """Absolute gap between model projection and the book line.  The
    bigger the gap, the more the model disagrees with the book.
    Returns -1.0 when either value is missing so it sorts last."""
    pv   = r.get("predicted_value")
    line = r.get("line")
    if pv is None or line is None:
        return -1.0
    try:
        return abs(float(pv) - float(line))
    except (TypeError, ValueError):
        return -1.0


def _edge_ev(r: dict) -> float:
    """EV% for the pick.  Missing EV sorts to the very bottom."""
    ev = r.get("ev_pct")
    if ev is None:
        return float("-inf")
    try:
        return float(ev)
    except (TypeError, ValueError):
        return float("-inf")


def _sorted_picks(rows: list[dict], state: dict) -> list[dict]:
    """Return *rows* ordered by the active sort pill, highest first."""
    sort = state.get("sort") or "proj"
    if   sort == "l5":   key = lambda r: _window_hit_rate(r, "last_5")
    elif sort == "l10":  key = lambda r: _window_hit_rate(r, "last_10")
    elif sort == "l20":  key = lambda r: _window_hit_rate(r, "last_20")
    elif sort == "h2h":  key = lambda r: _window_hit_rate(r, "h2h")
    elif sort == "edge": key = _edge_ev
    elif sort == "conf": key = lambda r: float(r.get("confidence") or 0.0)
    else:                key = _proj_gap            # "proj" default
    return sorted(rows, key=key, reverse=True)


# ── Filtering ──────────────────────────────────────────────────────────────────
# Every value below is read straight off the prop the page already loaded
# (the scored cache row + its summary) -- no new data source, no recompute.

# Min last-10 hit-rate options (raw count out of the player's last 10 games
# in which they cleared the current line) -> required hit count.
_L10_OPTIONS = {0: "Any", 5: "5+/10", 6: "6+/10", 7: "7+/10", 8: "8+/10", 9: "9+/10"}
# Min model confidence options -> fraction.
_CONF_OPTIONS = {0.0: "Any", 0.55: "55%+", 0.60: "60%+", 0.65: "65%+",
                 0.70: "70%+", 0.75: "75%+"}
# Min matchup-grade options -> composite floor (matches the bands used by
# pages.player._letter_grade_for_prop).
_GRADE_OPTIONS = {0.0: "Any", 0.52: "B- or better", 0.60: "B or better",
                  0.68: "B+ or better", 0.76: "A- or better", 0.84: "A or better"}


def _default_filters() -> dict:
    # Alt-line props are hidden by default; the user opts into them.
    return {"min_l10": 0, "show_alt": False, "markets": set(), "games": set(),
            "min_conf": 0.0, "min_grade": 0.0}


def _active_filter_count(f: dict) -> int:
    """How many filters deviate from their default (drives the badge)."""
    n = 0
    n += 1 if f.get("min_l10") else 0
    n += 1 if f.get("show_alt") else 0              # default is hidden; showing alts is the deviation
    n += 1 if f.get("markets") else 0
    n += 1 if f.get("games") else 0
    n += 1 if f.get("min_conf") else 0
    n += 1 if f.get("min_grade") else 0
    return n


def _prop_grade_composite(r: dict) -> float:
    """0..1 matchup-grade composite from the prop's own confidence + opp rank
    + EV%, mirroring pages.player._letter_grade_for_prop (no new data)."""
    try:
        conf = float(r.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf_score = max(0.0, min(1.0, (conf - 0.50) / 0.45))
    try:
        rank = int(r.get("opp_rank") or 15)
    except (TypeError, ValueError):
        rank = 15
    rank_score = max(0.0, min(1.0, (31 - rank) / 30.0))
    try:
        ev = float(r.get("ev_pct") or 0.0)
    except (TypeError, ValueError):
        ev = 0.0
    ev_score = max(0.0, min(1.0, ev / 30.0))
    return 0.5 * conf_score + 0.3 * rank_score + 0.2 * ev_score


def _game_key(r: dict) -> str:
    return str(r.get("event_id")
               or f"{r.get('away_team') or '?'}@{r.get('home_team') or '?'}")


def _game_label(r: dict) -> str:
    return f"{r.get('away_team') or '?'} @ {r.get('home_team') or '?'}"


def _filter_options(rows: list[dict]) -> tuple[dict, dict]:
    """Available prop-type + game options for the current slate, derived from
    the loaded props.  Returns ({market: label}, {game_key: label})."""
    markets: dict[str, str] = {}
    games: dict[str, str] = {}
    for r in rows:
        m = r.get("market")
        if m and m not in markets:
            markets[m] = _short_market(m)
        gk = _game_key(r)
        if gk not in games:
            games[gk] = _game_label(r)
    return (dict(sorted(markets.items(), key=lambda kv: kv[1])),
            dict(sorted(games.items(), key=lambda kv: kv[1])))


def _passes_filters(r: dict, f: dict) -> bool:
    """True iff *r* satisfies every active filter (AND-stacked)."""
    # 1. Minimum last-10 hit rate (raw hits over the player's last 10 games).
    if f.get("min_l10"):
        s = r.get("summary") or {}
        if int(s.get("last_10_hits") or 0) < int(f["min_l10"]):
            return False
    # 2. Alternative lines: when hidden, drop anything not on the main line.
    if not f.get("show_alt") and (r.get("line_type") or "main").lower() != "main":
        return False
    # 3. Prop type (empty selection = all types).
    if f.get("markets") and r.get("market") not in f["markets"]:
        return False
    # 4. Game (empty selection = all games).
    if f.get("games") and _game_key(r) not in f["games"]:
        return False
    # 5. Minimum model confidence.
    if f.get("min_conf"):
        try:
            if float(r.get("confidence") or 0.0) < float(f["min_conf"]):
                return False
        except (TypeError, ValueError):
            return False
    # 6. Minimum matchup grade (independent of confidence).
    if f.get("min_grade") and _prop_grade_composite(r) < float(f["min_grade"]):
        return False
    return True


def _prop_card(r: dict, backend) -> None:
    """One prop pick rendered as a card.

    Density is the main design constraint here -- we want every piece
    of decision-relevant data visible without clicking through to the
    player page:
      * Market chip + matchup (header)
      * Player name (linked to profile)
      * OVER/UNDER pill + line + confidence (the page's visual anchor)
      * Predicted value chip when a regression model produced one
      * Inline summary chips: season avg + L5 + L10 + L20 + H2H hit rate
      * Opposing-team rank chip ("OPP NYY: 28th of 30")
      * Best odds + book + Track button

    On mobile the summary chips drop to a single column inside the
    same card -- ECharts not used here so the row is light enough to
    handle 30+ cards on a phone without jank.
    """
    side = (r.get("side") or "Over").strip().title()
    is_over = side == "Over"
    chip_bg = t.POS if is_over else t.NEG
    confidence_pct = r["confidence"] * 100

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; gap: {t.SPACE_SM}; "
        f"min-width: 0;"
    ):
        # Header row: player headshot + market chip on the left, matchup
        # on the right.
        with ui.row().classes("items-center w-full").style(
            f"gap: 8px;"
        ):
            _card_avatar(r)
            ui.label(_short_market(r["market"]).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            ui.label(r.get("team") or "").style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; "
                f"font-family: monospace; "
                f"margin-left: auto;"
            )

        # Player name: links to player profile page.
        _name_slug = r["player"].lower().replace(" ", "-")
        ui.link(r["player"], f"/player/mlb/{_name_slug}").style(
            f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
            f"line-height: 1.2; text-decoration: none; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        ).tooltip("View player profile")

        # Pick row: chip + line + confidence.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; flex-wrap: nowrap;"
        ):
            ui.label(f"{side.upper()} {r['line']}").style(
                f"background: {chip_bg}; color: {t.BG}; "
                f"font-size: 13px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 6px 12px; border-radius: {t.RADIUS_SM}; "
                f"flex-shrink: 0;"
            )
            ui.element("div").style("flex: 1;")
            with ui.column().style(
                "gap: 1px; align-items: flex-end; flex-shrink: 0;"
            ):
                ui.label("CONFIDENCE").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(f"{confidence_pct:.0f}%").style(
                    f"font-size: 18px; font-weight: 800; color: {chip_bg}; "
                    f"font-family: monospace; letter-spacing: -.2px;"
                )
                _ev_badge(r.get("ev_pct"))

        # Predicted value chip (only when a regression model produced one)
        pv = r.get("predicted_value")
        if pv is not None:
            try:
                line_f    = float(r["line"])
                side_str  = (r.get("side") or "Over").strip().title()
                margin    = (pv - line_f) if side_str == "Over" else (line_f - pv)
                pv_color  = t.POS if margin > 1.0 else t.WARN
            except (TypeError, ValueError):
                margin, pv_color = 0.0, t.TEXT_DIM
            stat_abbr = _market_stat_abbr(r.get("market", ""))
            with ui.row().classes("items-center w-full").style(
                f"gap: 8px; padding-top: 2px;"
            ):
                ui.label("PREDICTED").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                pv_label = f"{pv:.1f}" + (f" {stat_abbr}" if stat_abbr else "")
                ui.label(pv_label).style(
                    f"font-size: 13px; font-weight: 700; color: {pv_color}; "
                    f"font-family: monospace;"
                )

        # ── Inline performance summary chips ─────────────────────────────
        _card_summary_chips(r)

        # ── AI summary (short, cached; never generated at render) ────────
        _card_ai_summary(r)

        # ── Opposing-team rank chip ──────────────────────────────────────
        _card_opp_rank_chip(r)

        # Footer row: line-type chip + best odds + book + Track Bet button.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; "
            f"padding-top: 6px; border-top: 1px solid {t.BORDER_SOFT};"
        ):
            _card_line_type_chip(r)
            ui.label("Best odds").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM};"
            )
            ui.label(_odds_str(r.get("best_odds"))).style(
                f"font-size: 12px; font-weight: 700; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
            ui.label(r.get("best_book") or "").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.element("div").style("flex: 1;")
            _track_btn(r, backend)


def _headshot_url(player_id) -> str:
    """MLB Stats API headshot URL.  The d_people:generic default transform
    makes it fall back to a generic silhouette for unknown/invalid ids."""
    return (
        f"https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
        f"v1/people/{player_id}/headshot/67/current"
    )


def _avatar_img_html(player_id, name: str) -> str:
    url = _headshot_url(player_id)
    return (
        f'<img src="{url}" alt="{name}" '
        f'style="width:34px; height:34px; border-radius:50%; '
        f'object-fit:cover; background:{t.CARD_HI}; '
        f'border:1px solid {t.BORDER}; flex-shrink:0;" '
        f'onerror="this.style.opacity=\'.35\';"/>'
    )


def _card_avatar(r: dict) -> None:
    """Small circular player headshot on the left of a prop card (FIX 6),
    matching the player-profile header style.  Uses the player_id stamped
    on the pick when present; otherwise shows the generic silhouette and
    resolves the real id lazily on a background thread (cached) so the
    page never blocks on a name->id lookup."""
    name = r.get("player") or ""
    pid  = r.get("player_id")
    if pid:
        ui.html(_avatar_img_html(pid, name)).style("flex-shrink: 0;")
        return
    holder = ui.html(_avatar_img_html("generic", name)).style("flex-shrink: 0;")

    async def _resolve() -> None:                                         # noqa: WPS430
        try:
            from src.player_profile_client import search_player_by_name
            rid = await asyncio.to_thread(search_player_by_name, name)
        except Exception:                                                 # noqa: BLE001
            rid = None
        if rid:
            holder.set_content(_avatar_img_html(rid, name))

    if name:
        ui.timer(0.05, _resolve, once=True)


def _strip(text: str) -> str:
    try:
        from src.utils import strip_formatting
        return strip_formatting(text)
    except Exception:                                                     # noqa: BLE001
        return text


def _ev_badge(ev_pct, *, compact: bool = False) -> None:
    """Render a small ``+12.4% EV`` chip beside the confidence number.

    Green when EV is positive, red when negative, neutral grey when
    the cache hasn't computed it yet (e.g. an older payload from before
    this feature shipped).  ``compact=True`` shrinks the chip for use
    on the dense alt-row strip.

    EV% is sourced from the persisted ``ev_pct`` field on the pick
    dict; the props page never recomputes -- see src/props_ev.py for
    the formula.
    """
    try:
        from src.props_ev import ev_color, ev_label
    except Exception:                                                     # noqa: BLE001
        return
    label = ev_label(ev_pct)
    color = ev_color(ev_pct, t)
    if compact:
        size, padding = "10px", "2px 6px"
    else:
        size, padding = "10.5px", "3px 8px"
    ui.label(label).style(
        f"background: {t.CARD_HI}; color: {color}; "
        f"font-size: {size}; font-weight: 800; letter-spacing: .3px; "
        f"padding: {padding}; border-radius: {t.RADIUS_PILL}; "
        f"font-family: monospace; white-space: nowrap;"
    )


def _card_line_type_chip(r: dict) -> None:
    """Tiny MAIN / ALT chip on the footer row.  Surfaces the
    classification so an alt-only player (no main market available)
    isn't quietly priced as if it were a standard line."""
    line_type = (r.get("line_type") or "main").lower()
    if line_type == "main":
        label = "MAIN"
        fg    = t.POS
    else:
        label = "ALT"
        fg    = t.WARN
    ui.label(label).style(
        f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
        f"color: {fg}; background: {t.CARD_HI}; "
        f"padding: 2px 6px; border-radius: {t.RADIUS_PILL};"
    )


def _card_summary_chips(r: dict) -> None:
    """5-cell grid below the pick row: SEASON avg + L5/L10/L20 + H2H hit
    rate vs the line.  Same shape as the player-page summary row so the
    user can scan both pages without re-learning the layout."""
    summary = r.get("summary") or {}
    side    = (r.get("side") or "Over").strip().title()
    try:
        line_f = float(r.get("line"))
    except (TypeError, ValueError):
        line_f = None

    cells: list[tuple[str, str, str, str]] = []  # (label, value, color, sub)

    sa = summary.get("season_avg")
    cells.append((
        "SEASON",
        "—" if sa is None else f"{sa:.2f}",
        t.TEXT,
        f"avg/{summary.get('season_games') or 0}g",
    ))
    for w_key, w_label in (
        ("last_5",  "L5"),
        ("last_10", "L10"),
        ("last_20", "L20"),
    ):
        hits  = summary.get(f"{w_key}_hits") or 0
        total = summary.get(f"{w_key}_games") or 0
        if not total:
            cells.append((w_label, "—", t.TEXT_DIM2, "n/a"))
            continue
        pct = hits / total
        col = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append((
            w_label, f"{hits}/{total}", col,
            f"{int(round(pct * 100))}%",
        ))
    h2h_hits  = summary.get("h2h_hits") or 0
    h2h_total = summary.get("h2h_games") or 0
    if not h2h_total:
        cells.append(("H2H", "—", t.TEXT_DIM2, "n/a"))
    else:
        pct = h2h_hits / h2h_total
        col = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append((
            "H2H", f"{h2h_hits}/{h2h_total}", col,
            f"{int(round(pct * 100))}%",
        ))

    side_suffix = (
        f"vs {side.upper()} {line_f}" if line_f is not None else ""
    )
    with ui.column().classes("w-full").style("gap: 4px; padding-top: 2px;"):
        if side_suffix:
            ui.label(f"HIT RATE {side_suffix}").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
        with ui.element("div").style(
            "display: grid; grid-template-columns: repeat(5, 1fr); "
            "gap: 4px; width: 100%;"
        ):
            for label, value, color, sub in cells:
                with ui.column().style(
                    f"background: {t.CARD_HI}; "
                    f"border-radius: {t.RADIUS_SM}; "
                    f"padding: 6px 4px; align-items: center; "
                    f"gap: 1px; min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 8.5px; font-weight: 800; letter-spacing: .4px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 12px; font-weight: 800; "
                        f"color: {color}; font-family: monospace;"
                    )
                    if sub:
                        ui.label(sub).style(
                            f"font-size: 8.5px; color: {t.TEXT_DIM2}; "
                            f"font-family: monospace;"
                        )


def _card_ai_summary(r: dict) -> None:
    """The prop's AI breakdown verdict -- the SAME artifact the player page
    shows (player_ai_breakdown), not a separate blurb.  Reads it from cache;
    while the background breakdown queue is still generating this one, shows a
    loading state and polls until it lands, then renders the verdict (matches
    the player page's 'loading -> appears' behaviour)."""
    from src import player_ai_breakdown as _pab

    holder = ui.column().classes("w-full").style("gap: 0; min-width: 0;")

    def _ai_box(text: str, tier: str = "", version: str = "") -> None:
        # Outline by AI-vs-model agreement: green = AI backs the model's side,
        # red = AI fades it, neutral border otherwise.
        ocolor = {"pos": t.POS, "neg": t.NEG}.get(
            _pab.agreement_outline_token(tier), t.BORDER)
        with ui.column().classes("w-full").style(
            f"gap: 4px; padding: 6px 8px; "
            f"background: {t.CARD_HI}; border-radius: {t.RADIUS_SM}; "
            f"border: 2px solid {ocolor}; min-width: 0;"
        ):
            with ui.row().classes("items-center w-full").style("gap: 6px;"):
                ui.label("AI").style(
                    f"font-size: 8px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.PRIMARY_HI}; background: {t.CARD}; "
                    f"padding: 1px 5px; border-radius: {t.RADIUS_PILL}; flex-shrink: 0;"
                )
                if version:
                    ui.label(version).style(
                        f"margin-left: auto; font-size: 8px; font-weight: 800; "
                        f"color: {t.TEXT_DIM2}; background: {t.CARD}; "
                        f"padding: 1px 5px; border-radius: {t.RADIUS_PILL}; "
                        f"font-family: monospace;"
                    ).tooltip("AI model version")
            ui.label(text).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; line-height: 1.35; "
                f"font-style: italic; white-space: normal;"
            )

    def _loading() -> None:
        with ui.row().classes("items-center w-full").style("gap: 6px; padding: 6px 8px;"):
            ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
            ui.label("Generating AI breakdown…").style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; font-style: italic;")

    def _verdict(mem_only: bool) -> tuple[str, str, str] | None:
        try:
            bd = (_pab.peek_breakdown_mem(r) if mem_only
                  else _pab.peek_breakdown(r)) or {}
        except Exception:                                                 # noqa: BLE001
            bd = {}
        v = (bd.get("verdict") or "").strip()
        return (v, (bd.get("verdict_tier") or "").strip(),
                (bd.get("model_version") or "").strip()) if v else None

    # First paint: one Supabase-backed read (handles already-cached props).
    v0 = _verdict(mem_only=False)
    with holder:
        (_ai_box(*v0) if v0 else _loading())

    if v0:
        return

    # Not cached yet -> poll cheaply (memory-only; the in-process queue fills
    # _MEM_CACHE as it generates) and render the moment it arrives.  Stop after
    # ~5 min so a never-generated prop doesn't poll forever.
    attempts = {"n": 0}

    def _poll() -> None:
        attempts["n"] += 1
        v = _verdict(mem_only=True)
        if v:
            holder.clear()
            with holder:
                _ai_box(*v)
            timer.active = False
        elif attempts["n"] >= 100:        # ~5 min at 3 s
            holder.clear()                # give up quietly (matches player page)
            timer.active = False

    timer = ui.timer(3.0, _poll, active=True)


def _card_opp_rank_chip(r: dict) -> None:
    """Compact 'OPP NYY: 28th of 30 vs K' chip.  The rank is precomputed
    in _section_unified_props_list so this is just formatting."""
    opp  = r.get("opp_abbrev")
    rank = r.get("opp_rank")
    if not opp or rank is None:
        return
    try:
        from src.player_profile_client import opp_rank_label
        label = opp_rank_label(rank)
    except Exception:                                                      # noqa: BLE001
        label = f"{rank}"

    # Color: top-10 favorable = green, bottom-10 = red, mid = amber.
    if rank <= 10:
        color = t.POS
    elif rank >= 21:
        color = t.NEG
    else:
        color = t.WARN

    stat_label = _market_stat_abbr(r.get("market", "")) or "stat"
    with ui.row().classes("items-center w-full").style(
        "gap: 6px; padding-top: 2px;"
    ):
        ui.label(f"OPP {opp}").style(
            f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
            f"color: {t.TEXT_DIM2}; "
            f"background: {t.CARD_HI}; padding: 2px 6px; "
            f"border-radius: {t.RADIUS_PILL};"
        )
        ui.label(f"{label} vs {stat_label}").style(
            f"font-size: 11px; font-weight: 700; color: {color}; "
            f"font-family: monospace;"
        )


# ── Small helpers ───────────────────────────────────────────────────────────

def _short_market(market: str) -> str:
    """Human-readable label for the market key."""
    mapping = {
        "pitcher_strikeouts":   "Strikeouts",
        "pitcher_outs":         "Outs Recorded",
        "pitcher_hits_allowed": "Hits Allowed",
        "pitcher_walks":        "Walks Allowed",
        "pitcher_earned_runs":  "Earned Runs",
        "pitcher_record_a_win": "Win",
        "batter_hits":          "Hits",
        "batter_total_bases":   "Total Bases",
        "batter_home_runs":     "Home Runs",
        "batter_rbis":          "RBIs",
        "batter_runs_scored":   "Runs",
        "batter_walks":         "Walks",
        "batter_strikeouts":    "Strikeouts",
        "batter_stolen_bases":  "Stolen Bases",
    }
    return mapping.get(market, market.replace("_", " ").title())


def _odds_str(o) -> str:
    if o is None:
        return "—"
    try:
        n = int(o)
    except (TypeError, ValueError):
        return str(o)
    return f"+{n}" if n > 0 else str(n)


def _market_stat_abbr(market: str) -> str:
    """Short stat label shown next to the predicted value."""
    mapping = {
        "pitcher_strikeouts":   "K",
        "pitcher_outs":         "outs",
        "pitcher_hits_allowed": "H",
        "pitcher_walks":        "BB",
        "pitcher_earned_runs":  "ER",
        "batter_hits":          "H",
        "batter_total_bases":   "TB",
        "batter_home_runs":     "HR",
        "batter_rbis":          "RBI",
        "batter_runs_scored":   "R",
        "batter_walks":         "BB",
        "batter_strikeouts":    "K",
        "batter_stolen_bases":  "SB",
    }
    return mapping.get(market, "")


def _short_iso(iso: str) -> str:
    """ISO timestamp -> compact "HH:MM ET" for the page header."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    except Exception:                                                     # noqa: BLE001
        return str(iso)[:16]


# ── Track Bet button ─────────────────────────────────────────────────────────

def _track_btn(r: dict, backend) -> None:
    """Render a small Track Bet button that POSTs the pick to /api/props/track.

    Uses the same in-process Flask test-client pattern as track_button.py
    so no HTTP hop is needed and Railway deploy constraints are respected.
    """
    btn = ui.button("Track").props("no-caps unelevated dense").style(
        f"background: {t.PRIMARY}; color: {t.BG}; "
        f"font-weight: 800; font-size: 10.5px; letter-spacing: .4px; "
        f"padding: 4px 10px; border-radius: {t.RADIUS_SM}; min-height: 0;"
    )

    async def _click():
        btn.props("loading")
        btn.disable()
        try:
            payload = {
                "player":          r.get("player", ""),
                "market":          r.get("market", ""),
                "line":            r.get("line"),
                "side":            r.get("side", "Over"),
                "odds":            r.get("best_odds"),
                "confidence":      r.get("confidence"),
                "predicted_value": r.get("predicted_value"),
                "team":            r.get("team", ""),
                "event_id":        r.get("event_id"),
                "commence_time":   r.get("commence_time"),
            }
            ok, data, _ = await asyncio.to_thread(
                _post_api, backend, "/api/props/track", payload
            )
            if ok:
                ui.notify(
                    f"Tracked: {r.get('player')} {r.get('side')} {r.get('line')}",
                    type="positive",
                )
                btn.text = "Tracked ✓"
                btn.props("disable")
            else:
                err = data.get("error") or "unknown error"
                if "already tracked" in err.lower():
                    btn.text = "Tracked ✓"
                    btn.props("disable")
                    ui.notify("Already tracked.", type="info")
                else:
                    ui.notify(f"Track failed: {err}", type="negative")
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"Track failed: {exc}", type="negative")
        finally:
            btn.props(remove="loading")
            if btn.text == "Track":
                btn.enable()

    btn.on("click", _click)


def _settle_trigger(backend) -> None:
    """Fire /api/props/settle_open once when the page loads so recently
    completed games are settled without the user having to do anything."""
    async def _try_settle():
        try:
            await asyncio.to_thread(
                _post_api, backend, "/api/props/settle_open", {}
            )
        except Exception:                                                 # noqa: BLE001
            pass

    ui.timer(0.5, _try_settle, once=True)


def _post_api(backend, path: str, body: dict) -> tuple[bool, dict, int]:
    """Invoke a Flask /api/ route via the in-process test client."""
    client = backend.app.test_client()
    try:
        resp = client.post(path, json=body or {})
        data = resp.get_json(force=True, silent=True) or {}
        ok   = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500
