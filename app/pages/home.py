"""
Home page -- top-level dashboard.

Layout (top to bottom):

  1. Top bar stats         Three side-by-side chips.  Overall W/L (admin
                            toggle), Best Model (XGB/LR/NN), Best Bet Type.
                            Replaces the old Model Bankroll hero card.
  2. EV Scan compact       Per-market value picks (edge >= 3%) shown as
                            tight rows -- matchup, pick, edge, Track btn.
  3. Highest Confidence    Horizontal carousel of all positive-edge picks
                            sorted by model confidence DESC.  Max 10.
  4. AI banner             Link to /ai (kept from prior layout).

Sidebar (Top 5 Plays + Confidence Performance) and the bottom-nav are
unchanged.

All data comes from `backend._analysis_state` / `_wnba_analysis_state`
+ the ledger files.  No HTTP hops.
"""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

_ET = ZoneInfo("America/New_York")

# Schedule status strings that mean "game has started or finished" --
# anything matching these in g["_status"] / g["_detailed_status"] /
# g["status"] disqualifies the game from the home page pick lists.
# Sourced from the MLB Stats API (abstractGameState / detailedState):
# https://statsapi.mlb.com/api/v1/schedule -- and the ESPN WNBA
# scoreboard which uses similar tokens ("Final", "In Progress").
_STARTED_STATUS_TOKENS: frozenset[str] = frozenset(
    s.lower() for s in (
        "Final", "Live", "In Progress", "In_Progress",
        "Game Over", "Postponed", "Suspended", "Completed Early",
        "Final: Tied", "Manager Challenge", "Delayed",
        "Suspended: Rain", "Suspended Rain",
    )
)

from components import theme as t
from components import navbar, bottom_nav
from components import track_button, team_logo
from pages import home_stats as hs


def _dbg(msg: str) -> None:
    """Diagnostic print -- always flushes to stderr so the Railway log
    stream picks it up.  Tagged so it's grep-able in production."""
    print(f"[RENDER] {msg}", flush=True, file=sys.stderr)


def register(backend) -> None:
    @ui.page("/")
    def home_page():
        _dbg("home_page ENTER")
        # Wrap the entire render in a try/except that prints the full
        # traceback to stderr so NameErrors and similar runtime bugs
        # surface in Railway logs instead of just returning a 500 with
        # a one-line message.  Each section also gets its own guard so
        # one broken card doesn't blank the rest of the page.
        try:
            try:
                mlb_n, wnba_n = backend.hydrate_state()
                _dbg(f"home_page hydrate_state returned mlb={mlb_n} wnba={wnba_n}")
            except Exception as exc:                                       # noqa: BLE001
                _dbg(f"home_page hydrate_state FAILED: {type(exc).__name__}: {exc}")
            try:
                mlb_state  = backend._analysis_state
                wnba_state = backend._wnba_analysis_state
                _dbg(
                    f"home_page STATE_CHECK "
                    f"mlb_results={len(mlb_state.get('results') or [])} "
                    f"wnba_results={len(wnba_state.get('results') or [])} "
                    f"mlb_keys={list(mlb_state.keys())} "
                    f"wnba_keys={list(wnba_state.keys())}"
                )
            except Exception as exc:                                       # noqa: BLE001
                _dbg(f"home_page STATE_CHECK FAILED: {type(exc).__name__}: {exc}")
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_HOME)
            _layout(backend)
            bottom_nav.render(active=t.TAB_HOME)
        except Exception as exc:                                           # noqa: BLE001
            import traceback as _tb, sys as _sys
            _tb_str = _tb.format_exc()
            print(
                f"[HOME PAGE FATAL] {type(exc).__name__}: {exc}\n{_tb_str}",
                flush=True, file=_sys.stderr,
            )
            # Render an inline error banner instead of a 500 so the
            # rest of the app stays reachable (the user can still
            # click into Sports / Admin from the navbar).
            ui.label("Home page render failed").style(
                f"color: {t.NEG}; font-size: 16px; font-weight: 700; "
                f"padding: {t.SPACE_LG};"
            )
            ui.label(f"{type(exc).__name__}: {exc}").style(
                f"color: {t.TEXT_DIM}; font-family: monospace; "
                f"font-size: 12px; padding: 0 {t.SPACE_LG};"
            )
            # Full traceback in a pre block so it can be copy-pasted
            # straight from the page if Railway logs are unavailable.
            ui.html(
                f"<pre style='color: {t.TEXT_DIM}; font-size: 11px; "
                f"padding: {t.SPACE_LG}; white-space: pre-wrap; "
                f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                f"border-radius: 6px; margin: {t.SPACE_LG}; "
                f"overflow-x: auto;'>"
                f"{_tb_str.replace('<', '&lt;')}"
                f"</pre>"
            )
        # Wheel-to-horizontal-scroll handler for every .carousel-scroller
        # on the page.  Vertical wheel input on a carousel translates to
        # horizontal scroll; horizontal wheel input passes through
        # untouched.  Lives in add_body_html so the script tag runs
        # AFTER the page DOM is rendered.  Idempotent: the dataset
        # marker on each scroller prevents double-binding when the
        # page re-renders.
        ui.add_body_html("""
        <script>
        (function() {
          function bindWheel(el) {
            if (el.dataset.wheelBound === '1') return;
            el.dataset.wheelBound = '1';
            el.addEventListener('wheel', function(e) {
              // Pass through pure-horizontal wheel input (trackpads,
              // some mice).  Convert vertical wheel to horizontal.
              if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
              e.preventDefault();
              el.scrollBy({ left: e.deltaY, behavior: 'auto' });
            }, { passive: false });
          }
          document.querySelectorAll('.carousel-scroller').forEach(bindWheel);
          // Re-scan after any DOM mutation so refreshable wrappers that
          // recreate the scrollers still bind the wheel handler.
          new MutationObserver(function() {
            document.querySelectorAll('.carousel-scroller').forEach(bindWheel);
          }).observe(document.body, { childList: true, subtree: true });

          // Stop click propagation from .track-stop wrappers (Track
          // button area on EV / Confidence cards) so pressing Track
          // doesn't ALSO trigger the card-level navigate-to-matchup
          // handler.  Capture phase so we run before NiceGUI's own
          // click delegation.
          document.addEventListener('click', function(e) {
            if (e.target && e.target.closest && e.target.closest('.track-stop')) {
              e.stopPropagation();
            }
          }, true);
        })();
        </script>
        """)


def _layout(backend) -> None:
    """Sidebar (Top 5 Plays + Confidence Performance) was removed from
    the home page per user spec -- the Highest Confidence + EV Scan
    carousels below already surface the same picks with richer
    per-card detail.  Content column now spans the full content width
    centered via margin: 0 auto."""
    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        _guarded_section("chips", lambda: _section_chips(backend))
        _guarded_section("todays_games_stub",
                         lambda: _section_todays_games_stub(backend))
        _guarded_section("ev_compact", lambda: _section_ev_compact(backend))
        _guarded_section("news", lambda: _section_news(backend))
        _guarded_section("games", lambda: _section_games(backend))
        _guarded_section("rotation", lambda: _section_rotation(backend))
        _guarded_section("heatmap", lambda: _section_heatmap(backend))
        _guarded_section("confidence_carousel",
                         lambda: _section_confidence_carousel(backend))
        _guarded_section("ai_banner", _ai_banner)
        _guarded_section("model_performance",
                         lambda: _section_model_performance(backend))


def _guarded_section(label: str, render_fn) -> None:
    """Render one section inside its own try/except so a single broken
    card doesn't blank the rest of the home page.  Failures print a
    full traceback to stderr (Railway logs) AND emit a small in-page
    error stripe so the user sees which section failed without
    scrolling the log.
    """
    try:
        render_fn()
    except Exception as exc:                                              # noqa: BLE001
        import traceback as _tb, sys as _sys
        _tb_str = _tb.format_exc()
        print(
            f"[HOME SECTION {label!r} FAILED] "
            f"{type(exc).__name__}: {exc}\n{_tb_str}",
            flush=True, file=_sys.stderr,
        )
        with ui.row().classes("w-full").style(
            f"background: {t.CARD}; border: 1px dashed {t.NEG}; "
            f"border-radius: {t.RADIUS_MD}; padding: 10px 14px; "
            f"gap: 8px; align-items: center;"
        ):
            ui.icon("error").style(f"font-size: 18px; color: {t.NEG};")
            ui.label(
                f"Section '{label}' failed: {type(exc).__name__}: {exc}"
            ).style(
                f"font-size: 12px; color: {t.NEG}; font-family: monospace; "
                f"flex: 1;"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 1 -- three stat chips at the top
# ─────────────────────────────────────────────────────────────────────────────

def _section_chips(backend) -> None:
    """Three equal-width chips, never stack vertically.

    Chip #1 (Overall W/L) is hidden when model_settings.show_overall_chip
    is False -- toggle lives in /admin -> MODEL BETS section.
    """
    try:
        settings = backend._load_model_settings()
    except Exception:                                                     # noqa: BLE001
        settings = {}
    show_overall = bool(settings.get("show_overall_chip", True))

    overall = hs.overall_record(backend)
    best_m  = hs.best_classifier(backend)
    best_t  = hs.best_bet_type(backend)
    props   = hs.props_record(backend)

    # AUDIT/FIX debug: surface what the home page now reads (ledger-backed)
    # so the W/L numbers are visible + verifiable in the Railway logs.
    try:
        print(
            f"[WL] home render: overall(ledger)={overall.get('wins')}-{overall.get('losses')} "
            f"props={props.get('wins')}-{props.get('losses')} "
            f"best_model={best_m} best_bet_type={best_t}",
            flush=True, file=sys.stderr,
        )
    except Exception:                                                      # noqa: BLE001
        pass

    # Single row with nowrap so chips stay side-by-side at every viewport.
    # min-width:0 on each child lets them shrink past content with ellipsis
    # instead of overflowing the page width.
    with ui.row().classes("w-full").style(
        f"gap: {t.SPACE_SM}; flex-wrap: nowrap; align-items: stretch;"
    ):
        if show_overall:
            _chip_overall(overall)
        _chip_props(props)
        _chip_best_model(best_m)
        _chip_best_bet_type(best_t)


def _section_todays_games_stub(backend) -> None:
    """Schedule-only stub cards shown on a fresh day.

    Between the 2 AM full-clear and the 8 AM analysis run, there are no
    picks to surface -- but the 3 AM games-prefetch job has cached
    today's schedule.  This section renders those games as lightweight
    "Analysis pending" stub cards so the user has something to see when
    they wake up.

    Suppressed entirely once analysis has produced results (the EV /
    confidence carousels below take over).  Renders nothing if the
    schedule cache is also empty.
    """
    # If either sport already has analysis results, the carousels cover
    # the slate -- don't show stubs.
    try:
        mlb_results  = backend._analysis_state.get("results") or []
        wnba_results = backend._wnba_analysis_state.get("results") or []
    except Exception:                                                     # noqa: BLE001
        mlb_results = wnba_results = []
    if mlb_results or wnba_results:
        return

    # Pull today's prefetched schedule (cheap, cache-backed -- no odds,
    # no analysis).
    try:
        games = list(backend.get_todays_schedule("mlb"))
    except Exception:                                                     # noqa: BLE001
        games = []
    try:
        games += list(backend.get_todays_schedule("wnba"))
    except Exception:                                                     # noqa: BLE001
        pass
    if not games:
        return

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("TODAY'S GAMES").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label("Analysis pending").style(
                f"background: {t.CARD_HI}; color: {t.WARN}; "
                f"font-size: 10.5px; font-weight: 700; letter-spacing: .3px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )

        with ui.element("div").classes("game-grid w-full"):
            for g in _dedup_stub_games(games):
                _stub_game_card(g)


def _dedup_stub_games(games: list[dict]) -> list[dict]:
    """Belt-and-braces dedup for the stub list -- collapses any same
    matchup + same ET date that slipped through (e.g. a schedule row
    cached before the upstream dedup shipped, or a postponed twin).
    Prefers a live/scored entry over a plain scheduled one."""
    def key(g: dict) -> tuple:
        ct = g.get("commence_time") or ""
        return ((g.get("away_team") or "").strip().lower(),
                (g.get("home_team") or "").strip().lower(), ct[:10])

    def is_ppd(g: dict) -> bool:
        ds = (g.get("detailed_status") or "").lower()
        return "postpon" in ds or (g.get("coded_status") or "") in ("D", "DR", "PR")

    def prio(g: dict) -> int:
        if is_ppd(g):
            return 0
        if _stub_is_live(g):
            return 3
        return 1

    best: dict[tuple, dict] = {}
    for g in games:
        k = key(g)
        if k not in best or prio(g) > prio(best[k]):
            best[k] = g
    return list(best.values())


def _stub_is_live(game: dict) -> bool:
    """True when the schedule data marks this game in progress."""
    return bool(
        game.get("is_live")
        or game.get("status") == "Live"
        or (game.get("coded_status") or "") == "I"
    )


def _stub_game_card(game: dict) -> None:
    """One schedule-only stub card.

    In-progress games (BUG 2) show a LIVE badge + the current score
    pulled straight from the schedule response instead of the static
    tip time; pre-game cards keep the scheduled time + "Analysis
    pending" label.
    """
    away = (game.get("away_team") or "").strip() or "TBD"
    home = (game.get("home_team") or "").strip() or "TBD"
    is_live = _stub_is_live(game)

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 8px; "
        f"min-width: 0;"
    ):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(f"{away}  @  {home}").style(
                f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
                f"flex: 1; min-width: 0; white-space: nowrap; "
                f"overflow: hidden; text-overflow: ellipsis;"
            )
            if is_live:
                ui.html(
                    f'<span style="display:inline-flex; align-items:center; gap:5px; '
                    f'color:{t.NEG}; font-size:11px; font-weight:800; '
                    f'letter-spacing:.4px; flex-shrink:0;">'
                    f'<span style="width:7px; height:7px; border-radius:50%; '
                    f'background:{t.NEG}; box-shadow:0 0 6px {t.NEG};"></span>LIVE</span>'
                )
            else:
                ui.label(_fmt_game_time(game.get("commence_time"))).style(
                    f"font-size: 11.5px; font-weight: 700; color: {t.TEXT_DIM}; "
                    f"font-family: monospace; flex-shrink: 0;"
                )

        if is_live:
            a_sc = game.get("away_score")
            h_sc = game.get("home_score")
            a_sc = 0 if a_sc is None else a_sc
            h_sc = 0 if h_sc is None else h_sc
            ui.label(f"{away} {a_sc}   —   {home} {h_sc}").style(
                f"font-size: 13px; font-weight: 800; color: {t.POS}; "
                f"font-family: monospace; align-self: flex-start;"
            )
        else:
            ui.label("Analysis pending").style(
                f"font-size: 10px; font-weight: 700; letter-spacing: .4px; "
                f"color: {t.WARN}; background: rgba(245, 158, 11, .08); "
                f"padding: 3px 8px; border-radius: {t.RADIUS_PILL}; "
                f"align-self: flex-start;"
            )


def _fmt_game_time(iso) -> str:
    """ISO commence_time -> '7:05 PM ET'.  Returns 'TBD' on failure."""
    if not iso:
        return "TBD"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return "TBD"


def _chip_overall(overall: dict) -> None:
    w, l, pct = overall["wins"], overall["losses"], overall.get("pct")
    color = hs.winrate_color(pct, t)
    main  = f"{w}-{l}"
    pct_s = f"{pct * 100:.0f}%" if pct is not None else "—"
    _chip(label="GAME MODELS", main=main, suffix=pct_s, color=color)


def _chip_props(props: dict) -> None:
    """Model record -- MLB pitcher + batter prop models aggregated into one
    collective W/L (from model_picks, never the ledger)."""
    w, l, pct = props.get("wins", 0), props.get("losses", 0), props.get("pct")
    color = hs.winrate_color(pct, t)
    pct_s = f"{pct * 100:.0f}%" if pct is not None else "—"
    _chip(label="PROPS MODELS", main=f"{w}-{l}", suffix=pct_s, color=color)


def _chip_best_model(best: dict | None) -> None:
    if not best:
        _chip(label="BEST GAME MODEL", main="—", suffix="not enough data",
              color=t.TEXT_DIM)
        return
    color = hs.winrate_color(best["pct"], t)
    _chip(
        label="BEST GAME MODEL",
        main=best["model"],
        suffix=f"{best['pct'] * 100:.0f}%",
        color=color,
    )


def _chip_best_bet_type(best: dict | None) -> None:
    if not best:
        _chip(label="BEST PROP MODEL", main="—", suffix="not enough data",
              color=t.TEXT_DIM)
        return
    color = hs.winrate_color(best["pct"], t)
    _chip(
        label="BEST PROP MODEL",
        main=best["label"],
        suffix=f"{best['wins']}-{best['losses']}  {best['pct'] * 100:.0f}%",
        color=color,
    )


def _chip(label: str, main: str, suffix: str, color: str) -> None:
    """One stat chip.  flex: 1 1 0 + min-width: 0 = equal width + shrinkable."""
    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; "
        f"gap: 4px; "
        f"flex: 1 1 0; min-width: 0; overflow: hidden;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(main).style(
            f"font-size: 18px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(suffix).style(
            f"font-size: 11px; font-weight: 600; color: {color}; "
            f"font-family: monospace; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 2 -- EV compact rows (edge >= 3%)
# ─────────────────────────────────────────────────────────────────────────────

def _section_ev_compact(backend) -> None:
    """EV Scan -- horizontal card carousel.

    One card per qualifying pick.  Cards are equal-width via CSS
    calc() with a media-query split: 3 visible on desktop (>768px),
    2 on mobile.  Native overflow-x: auto + scroll-snap gives the
    swipe gesture for free on mobile.

    Desktop adds < / > arrow buttons outside the carousel that scroll
    by exactly one card width.  A dot indicator sits below the
    carousel; an inline JS listener updates the active dot as the
    user scrolls.  All wiring runs against element.html_id so it
    survives NiceGUI id-format changes.

    Only games that haven't started yet make it into the EV scan.  The
    full slate (on /sports) still shows everything; the home page lists
    are forward-looking only.
    """
    _ev_min      = getattr(backend, "EV_MIN_EDGE", 0.03)
    all_games    = _all_serialized_games(backend)
    games        = _filter_upcoming(all_games, label="ev_compact")
    rows         = hs.enumerate_value_picks(games, min_edge=_ev_min)
    rows.sort(key=lambda r: float(r.get("edge") or 0), reverse=True)
    _dbg(
        f"ev_compact: in_state={len(all_games)}  upcoming={len(games)}"
        f"  value_picks={len(rows)}  min_edge={_ev_min:.0%}"
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        # Header: title + edge threshold + count badge
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("EV SCAN").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"edge ≥ {_ev_min:.0%}").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};"
            )
            ui.label(f"{len(rows)}").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL}; "
                f"margin-left: auto;"
            )

        # Empty state: explain WHY so the cause is diagnosable.
        if not rows:
            if not all_games:
                _empty_msg = "Analysis pipeline hasn't run yet today — visit Admin to trigger a run."
            elif not games:
                _empty_msg = "Today's games have already started — picks will refresh tonight."
            else:
                _empty_msg = f"No picks with edge ≥ {_ev_min:.0%} found in today's slate."
            _dbg(f"ev_compact empty: {_empty_msg}")
            ui.label(_empty_msg).style(
                f"color: {t.TEXT_DIM}; font-size: 13px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
                f"text-align: center; font-style: italic;"
            )
            return

        # Carousel: same overlay-arrow pattern as the Highest Confidence
        # carousel (below).  Wrap the scroller in a relative div so the
        # < / > overlays can be absolutely positioned over its edges.
        # The dot-indicator + inline-script-with-data-target approach
        # has been removed -- user spec asked to drop the slider/scrollbar
        # below the carousel and unify arrow behavior with the
        # confidence carousel.
        with ui.element("div").style("position: relative; width: 100%;"):
            scroller = ui.row().classes("ev-scroller carousel-scroller").style(
                f"width: 100%; "
                f"overflow-x: auto; overflow-y: hidden; "
                f"gap: 8px; padding: 4px 2px; "
                f"scroll-snap-type: x mandatory; flex-wrap: nowrap;"
            )
            result_index = _pick_result_index(backend)
            with scroller:
                for r in rows:
                    _ev_card(backend, r, result_index)

            # Scroll arrows: same overlay buttons as _section_confidence_carousel.
            _carousel_arrow(scroller, direction="left")
            _carousel_arrow(scroller, direction="right")


def _pick_result_index(backend) -> dict[tuple[str, str], dict]:
    """Walk both ledger history lists and return
    {(game_id, bet_type): history_row} -- shared by _ev_card and
    _confidence_card so settlement state is visible on every home
    card without re-reading the ledger per card.  Same shape as
    pages/model._build_result_index but lives here to avoid a
    cross-page import.  Empty dict on any read error."""
    out: dict[tuple[str, str], dict] = {}
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = backend.Ledger(path=path, starting_bankroll=1000.0)
        except Exception:                                                 # noqa: BLE001
            continue
        for h in (led.data.get("history") or []):
            gid = h.get("game_id")
            bt  = h.get("bet_type") or "single"
            if not gid:
                continue
            out[(str(gid), str(bt))] = h
    return out


def _result_for_row(r: dict, result_index: dict) -> tuple[str, float, float]:
    """Look up settlement result for an EV / Confidence row.

    Returns (result, pnl, stake) where:
      result -- "win" / "loss" / "push" / ""  (empty for not-yet-settled)
      pnl    -- ledger.model_pnl (signed; positive for wins, negative for
                losses, zero for push)
      stake  -- ledger.model_amount (used for the "-$<stake>" loss
                display)

    Picks that didn't make the daily-picks selector's top-5 (so no
    ledger row) return ("", 0.0, 0.0) -- the card stays neutral.
    """
    gid = str(r.get("game_id") or "")
    if not gid:
        return ("", 0.0, 0.0)
    bt = (r.get("bet_type") or "single").lower()
    hist = result_index.get((gid, bt))
    if hist is None:
        return ("", 0.0, 0.0)
    return (
        (hist.get("result") or "").lower(),
        float(hist.get("model_pnl") or 0.0),
        float(hist.get("model_amount") or 0.0),
    )


def _settled_badge(result: str, pnl: float, stake: float) -> None:
    """Inline WIN / LOST chip + signed P/L for a settled card.  No-op
    when result is empty / push (pending stays neutral, push has no
    clear color convention)."""
    if result == "win":
        with ui.row().classes("items-center w-full").style("gap: 6px;"):
            ui.label("WIN").style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"background: {t.POS}; color: {t.BG}; "
                f"padding: 2px 7px; border-radius: 3px;"
            )
            ui.label(f"+${pnl:.2f}").style(
                f"font-size: 12px; font-weight: 800; color: {t.POS}; "
                f"font-family: monospace;"
            )
    elif result == "loss":
        with ui.row().classes("items-center w-full").style("gap: 6px;"):
            ui.label("LOST").style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"background: {t.NEG}; color: {t.BG}; "
                f"padding: 2px 7px; border-radius: 3px;"
            )
            ui.label(f"-${stake:.2f}").style(
                f"font-size: 12px; font-weight: 800; color: {t.NEG}; "
                f"font-family: monospace;"
            )


def _result_card_style(result: str) -> tuple[str, str]:
    """Return (background, border) CSS values for a card based on its
    settled result.  Mirrors PR #65's game-card bet-box treatment so
    the home-screen tints match the slate cards exactly."""
    if result == "win":
        return (
            f"rgba({t.SECONDARY_R}, {t.SECONDARY_G}, {t.SECONDARY_B}, 0.15)",
            f"1px solid {t.POS}",
        )
    if result == "loss":
        return (
            f"rgba({t.NEG_R}, {t.NEG_G}, {t.NEG_B}, 0.15)",
            f"1px solid {t.NEG}",
        )
    return (t.CARD, f"1px solid {t.BORDER}")


def _ev_card(backend, r: dict, result_index: dict | None = None) -> None:
    """One EV-scan card in the carousel.

    Width is set via CSS class .ev-card so the breakpoint-aware
    calc() in theme.page_head_css can control it without per-card
    inline math.

    Per user spec the whole card is clickable -- clicking anywhere
    except the Track button navigates to /matchup/<sport>/<gid>.
    Track button has its own click handler with stopPropagation so
    pressing Track doesn't also navigate away.
    """
    edge_pct = float(r.get("edge") or 0) * 100
    edge_s   = f"+{edge_pct:.1f}% Edge"
    sport_r  = r.get("sport", "mlb")
    gid      = r.get("game_id")
    result, pnl, stake = _result_for_row(r, result_index or {})
    bg, border = _result_card_style(result)
    settled = result in ("win", "loss")
    cursor_css = "cursor: pointer;" if gid else ""
    card = ui.column().classes("ev-card").style(
        f"background: {bg}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; "
        f"gap: 6px; "
        f"flex-shrink: 0; "
        f"scroll-snap-align: start; "
        f"{cursor_css}"
    )
    if gid:
        card.on("click", lambda: ui.navigate.to(f"/matchup/{sport_r}/{gid}"))
    with card:
        with ui.row().classes("items-center").style("gap: 4px;"):
            team_logo.render(r.get("away_full", ""), sport=sport_r, size=20)
            team_logo.render(r.get("home_full", ""), sport=sport_r, size=20)
        ui.label(r["matchup"]).style(
            f"font-size: 11px; color: {t.TEXT_DIM2}; letter-spacing: .3px; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(r["pick"]).style(
            f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(edge_s).style(
            f"font-size: 12.5px; font-weight: 800; color: {t.POS}; "
            f"font-family: monospace;"
        )
        if settled:
            _settled_badge(result, pnl, stake)
        else:
            # Pending: keep the Track button so the user can record
            # the bet.  Settled cards drop it -- the bet is already
            # past the trackable window.
            # The wrapper carries .track-stop so the body-html script
            # in home_page() can stopPropagation on clicks here and
            # prevent the card-level navigate.to from firing when
            # the user actually meant to track the bet.
            with ui.row().classes("w-full track-stop").style("margin-top: 4px;"):
                if r.get("game_id"):
                    track_button.render(
                        backend, game_id=r["game_id"], sport=sport_r,
                        size="sm", label="Track",
                    )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 3 -- horizontal confidence carousel (any positive edge, max 10)
# ─────────────────────────────────────────────────────────────────────────────

def _section_confidence_carousel(backend) -> None:
    # Same pre-filter as the EV scan: home page lists are forward-
    # looking; started / completed games drop out so the user sees
    # actionable picks only.
    all_games = _all_serialized_games(backend)
    games     = _filter_upcoming(all_games, label="confidence_carousel")
    rows      = hs.enumerate_value_picks(games, min_edge=0.0001)   # any positive edge
    rows.sort(key=lambda r: float(r.get("prob") or 0), reverse=True)
    rows = rows[:10]
    _dbg(
        f"confidence_carousel: in_state={len(all_games)}  upcoming={len(games)}"
        f"  value_picks={len(rows)}"
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("HIGHEST CONFIDENCE").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label("by model confidence").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};"
            )
        if not rows:
            if not all_games:
                _empty_msg = "Analysis pipeline hasn't run yet today."
            elif not games:
                _empty_msg = "Today's games have already started."
            else:
                _empty_msg = "No positive-edge picks yet."
            _dbg(f"confidence_carousel empty: {_empty_msg}")
            ui.label(_empty_msg).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return

        # Wrap the scrollable row in a relative container so the < / >
        # arrow overlays can be absolutely positioned over its edges.
        with ui.element("div").style(
            "position: relative; width: 100%;"
        ):
            scroller = ui.row().classes("carousel-scroller").style(
                f"width: 100%; "
                f"overflow-x: auto; overflow-y: hidden; "
                f"gap: {t.SPACE_SM}; padding: 4px 2px; "
                f"scroll-snap-type: x mandatory; flex-wrap: nowrap;"
            )
            result_index = _pick_result_index(backend)
            with scroller:
                for r in rows:
                    _confidence_card(r, result_index)

            # Scroll arrows -- overlay buttons positioned just outside
            # the card area.  Native swipe still works on touch.
            _carousel_arrow(scroller, direction="left")
            _carousel_arrow(scroller, direction="right")


def _confidence_card(r: dict, result_index: dict | None = None) -> None:
    edge_pct = float(r.get("edge") or 0) * 100
    prob_pct = float(r.get("prob") or 0) * 100
    sport_r  = r.get("sport", "mlb")
    gid      = r.get("game_id")
    result, pnl, stake = _result_for_row(r, result_index or {})
    bg, border = _result_card_style(result)
    # _result_card_style returns CARD-default; this card historically
    # uses CARD_HI for the surface so override only when no settlement
    # color applies.
    if result not in ("win", "loss"):
        bg = t.CARD_HI
    cursor_css = "cursor: pointer;" if gid else ""
    card = ui.column().style(
        f"background: {bg}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; "
        f"min-width: 200px; max-width: 200px; flex-shrink: 0; gap: 4px; "
        f"scroll-snap-align: start; "
        f"{cursor_css}"
    )
    if gid:
        card.on("click", lambda: ui.navigate.to(f"/matchup/{sport_r}/{gid}"))
    with card:
        with ui.row().style("gap: 4px; align-items: center;"):
            team_logo.render(r.get("away_full", ""), sport=sport_r, size=22)
            team_logo.render(r.get("home_full", ""), sport=sport_r, size=22)
        ui.label(r["matchup"]).style(
            f"font-size: 10px; color: {t.TEXT_DIM2}; "
            f"letter-spacing: .3px; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(r["pick"]).style(
            f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        # Main number: confidence (the model's pick probability).
        ui.label(f"{prob_pct:.0f}%").style(
            f"font-size: 26px; font-weight: 800; color: {t.PRIMARY}; "
            f"font-family: monospace; letter-spacing: -.4px; "
            f"margin-top: 4px;"
        )
        ui.label(f"+{edge_pct:.1f}% edge").style(
            f"font-size: 10.5px; font-weight: 600; color: {t.POS}; "
            f"font-family: monospace;"
        )
        if result in ("win", "loss"):
            _settled_badge(result, pnl, stake)


def _carousel_arrow(scroller, direction: str) -> None:
    """< / > overlay button for a carousel scroller.

    Positioned just OUTSIDE the scroller's horizontal bounds (left: -14px
    or right: -14px) so the button never sits on top of card content --
    user spec explicitly requires "outside the card area without
    overlapping any card content or text".  The relative-positioned
    parent container's overflow defaults to visible so the negative
    offset paints into the surrounding gap.

    Hidden on touch screens via the desktop-only class -- native swipe
    + the wheel handler in the carousel-scroller injected JS keep the
    scroller usable on phones.
    """
    is_left = direction == "left"
    arrow   = "‹" if is_left else "›"
    side    = "left: -14px;" if is_left else "right: -14px;"

    btn = ui.button(arrow).classes("desktop-only").props("flat dense").style(
        f"position: absolute; top: 50%; {side} "
        f"transform: translateY(-50%); "
        f"background: {t.CARD}; color: {t.TEXT}; "
        f"width: 32px; height: 32px; min-height: 0; "
        f"font-size: 18px; font-weight: 800; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: 50%; padding: 0; line-height: 1; "
        f"box-shadow: 0 2px 6px rgba(0,0,0,0.4); "
        f"z-index: 2;"
    )
    delta = -240 if is_left else 240

    dom_id = getattr(scroller, "html_id", f"c{scroller.id}")

    async def _click():
        try:
            await ui.run_javascript(
                f"document.getElementById({dom_id!r})"
                f".scrollBy({{left: {delta}, behavior: 'smooth'}})"
            )
        except Exception:                                                 # noqa: BLE001
            pass

    btn.on("click", _click)


# ─────────────────────────────────────────────────────────────────────────────
#  Section 5 -- Model Performance (bottom of page)
# ─────────────────────────────────────────────────────────────────────────────

def _section_model_performance(backend) -> None:
    """Three model-only stats at the very bottom of the home page.

    Distinct from "personal betting performance" (which lives on /mybets):
    this section reports the MODEL's settled-history results across both
    sports.  Units only -- no dollar amounts, no open bets, no bankroll
    figures.  See hs.model_performance for the unit-tracking convention.
    """
    perf = hs.model_performance(backend)
    wins, losses = perf["wins"], perf["losses"]
    pct = perf["pct"]

    pct_s   = f"{pct * 100:.1f}%" if pct is not None else "—"
    pct_col = hs.winrate_color(pct, t)

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("MODEL PERFORMANCE").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label("MLB combined · finished picks").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};"
            )
        with ui.row().classes("w-full").style(
            f"gap: {t.SPACE_SM}; flex-wrap: nowrap; align-items: stretch;"
        ):
            _perf_stat("WIN %",  pct_s,                 pct_col)
            _perf_stat("RECORD", f"{wins}-{losses}",    t.TEXT)


def _perf_stat(label: str, value: str, color: str) -> None:
    """One stat cell for the Model Performance row.  Equal-width siblings,
    never wrap; matches the visual rhythm of Section 1 chips while staying
    purely informational (no Track / no nav)."""
    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; "
        f"gap: 4px; "
        f"flex: 1 1 0; min-width: 0; overflow: hidden;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(value).style(
            f"font-size: 20px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _has_started(g: dict, now_et: datetime) -> bool:
    """True when game *g* has already kicked off or finished.  Used by
    _filter_upcoming below to drop completed / in-progress games from
    the home page pick lists.

    Detection order (cheapest first):
      1. _final_score on the row -- only set when the game ended
      2. _status / status / detailed_status string matches one of the
         "started" tokens (Final, Live, In Progress, Postponed, ...).
         Tokens are matched case-insensitively against the full status
         AND against each whitespace-separated word so "Final/Tied"
         and "FINAL" both register.
      3. commence_time has already passed (in ET).  This is the
         schedule-only fallback used for rows that came from
         /api/schedule before live linescore polling kicked in.
    """
    if g.get("_final_score"):
        return True

    for key in ("_status", "status", "_detailed_status", "detailed_status"):
        raw = g.get(key)
        if not raw:
            continue
        s = str(raw).strip().lower()
        if not s:
            continue
        if s in _STARTED_STATUS_TOKENS:
            return True
        # Multi-word forms like "Final: Tied" or "In Progress, 5th".
        if any(tok and tok in _STARTED_STATUS_TOKENS for tok in s.replace(",", " ").split()):
            return True

    # Schedule-only games carry commence_time as ISO.  If it's already
    # past, treat the game as started even when no status string came
    # through (the per-page live poller hadn't run yet, etc.).
    ct = g.get("commence_time") or ""
    if ct:
        try:
            dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if dt.astimezone(_ET) <= now_et:
                return True
        except Exception:                                                 # noqa: BLE001
            pass
    return False


def _filter_upcoming(games: list[dict], *, label: str = "") -> list[dict]:
    """Drop games that have already started / finished.  Used by the
    home page pick lists (EV scan, Highest Confidence carousel) so the
    user only sees actionable picks.  The full slate on /sports keeps
    everything -- this filter never touches that pipeline.

    Logs one [HOME-FILTER] line per call with the input + kept + dropped
    counts so Railway captures the impact of the filter on every render.
    """
    if not games:
        return games
    now_et = datetime.now(_ET)
    upcoming: list[dict] = []
    dropped: list[str] = []
    for g in games:
        if _has_started(g, now_et):
            label_str = f"{g.get('away_team')} @ {g.get('home_team')}"
            dropped.append(label_str)
            continue
        upcoming.append(g)
    _dbg(
        f"[HOME-FILTER] {label or 'unspecified'}: "
        f"input={len(games)}  kept={len(upcoming)}  dropped={len(dropped)}"
    )
    if dropped:
        _dbg(f"[HOME-FILTER] {label or 'unspecified'} dropped games: "
             f"{', '.join(dropped[:10])}"
             f"{' ...' if len(dropped) > 10 else ''}")
    return upcoming


def _all_serialized_games(backend) -> list[dict]:
    """Pull serialized games from both sport caches.  Each result is the
    same dict shape pages/sport.py renders, with `_sport` set so the
    Track button can route to the right endpoint.

    Unlike the previous _value_games helper, this does NOT filter on
    value_pick -- the caller is responsible for filtering by edge or
    by market.  Returning all games (including NO MODEL PICK stubs) is
    safe; enumerate_value_picks skips _no_model entries.
    """
    out: list[dict] = []
    mlb_failures = 0
    wnba_failures = 0
    first_mlb_err: str | None = None
    first_wnba_err: str | None = None
    mlb_total = len(backend._analysis_state.get("results") or [])
    wnba_total = len(backend._wnba_analysis_state.get("results") or [])
    _dbg(
        f"_all_serialized_games ENTER mlb_in_state={mlb_total} "
        f"wnba_in_state={wnba_total}"
    )
    # Pre-serialized passthrough rationale: when state was hydrated from
    # data/analysis_cache.json or daily_snapshot.json, the cached
    # entries are already flat _serialize() outputs (home_team,
    # away_team, pick_prob, etc.) -- calling _serialize() again crashes
    # with KeyError: 'game' because the raw nested r["game"] /
    # r["prediction"] shape only exists in the in-process post-analyze
    # pipeline.  The guard `if "home_team" in r and "away_team" in r`
    # routes cache entries straight through and only invokes the
    # serializer on raw results.
    mlb_passthrough = 0
    try:
        bankroll = float(backend._analysis_state.get("bankroll") or 250)
        mlb_ledger = None  # lazy: only built when we hit a raw row
        for r in (backend._analysis_state.get("results") or []):
            try:
                if "home_team" in r and "away_team" in r:
                    g = dict(r)
                    mlb_passthrough += 1
                else:
                    if mlb_ledger is None:
                        mlb_ledger = backend.Ledger(
                            path="data/ledger.json",
                            starting_bankroll=bankroll,
                        )
                    s_bank = mlb_ledger.data.get(
                        "personal_starting_bankroll", bankroll
                    )
                    g = backend._serialize(r, bankroll, "mlb", s_bank)
                g["_sport"] = "mlb"
                out.append(g)
            except Exception as exc:                                      # noqa: BLE001
                mlb_failures += 1
                if first_mlb_err is None:
                    import traceback as _tb
                    first_mlb_err = f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"
                continue
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"_all_serialized_games MLB SETUP FAILED: {type(exc).__name__}: {exc}")
    wnba_passthrough = 0
    try:
        bankroll = float(backend._wnba_analysis_state.get("bankroll") or 1000)
        wnba_results = backend._wnba_analysis_state.get("results") or []
        if wnba_results:
            wnba_ledger = None  # lazy
            for r in wnba_results:
                try:
                    if "home_team" in r and "away_team" in r:
                        g = dict(r)
                        wnba_passthrough += 1
                    else:
                        if wnba_ledger is None:
                            wnba_ledger = backend.Ledger(
                                path="data/wnba_ledger.json",
                                starting_bankroll=bankroll,
                            )
                        s_bank = wnba_ledger.data.get(
                            "personal_starting_bankroll", bankroll
                        )
                        g = backend._serialize_wnba(r, bankroll, s_bank)
                    g["_sport"] = "wnba"
                    out.append(g)
                except Exception as exc:                                  # noqa: BLE001
                    wnba_failures += 1
                    if first_wnba_err is None:
                        import traceback as _tb
                        first_wnba_err = f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"
                    continue
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"_all_serialized_games WNBA SETUP FAILED: {type(exc).__name__}: {exc}")
    if first_mlb_err:
        _dbg(f"_all_serialized_games FIRST MLB SERIALIZE FAILURE: {first_mlb_err}")
    if first_wnba_err:
        _dbg(f"_all_serialized_games FIRST WNBA SERIALIZE FAILURE: {first_wnba_err}")
    _dbg(
        f"_all_serialized_games EXIT serialized={len(out)} "
        f"mlb_passthrough={mlb_passthrough}/{mlb_total} "
        f"wnba_passthrough={wnba_passthrough}/{wnba_total} "
        f"mlb_failures={mlb_failures}/{mlb_total} "
        f"wnba_failures={wnba_failures}/{wnba_total}"
    )
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Sections: Upcoming Games + Final Scores  (below News, above Confidence)
# ─────────────────────────────────────────────────────────────────────────────

# Team-name → abbreviation lookup tables.  Used by both sections so the
# compact row format shows "NYY" instead of "New York Yankees".
_MLB_ABBR: dict[str, str] = {
    "Arizona Diamondbacks": "ARI",  "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",     "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",          "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",       "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",      "Detroit Tigers": "DET",
    "Houston Astros": "HOU",        "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",         "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",       "New York Mets": "NYM",
    "New York Yankees": "NYY",      "Oakland Athletics": "OAK",
    "Athletics": "ATH",             "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",   "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",   "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",         "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}
_WNBA_ABBR: dict[str, str] = {
    "Atlanta Dream": "ATL",         "Chicago Sky": "CHI",
    "Connecticut Sun": "CONN",      "Dallas Wings": "DAL",
    "Indiana Fever": "IND",         "Las Vegas Aces": "LV",
    "Los Angeles Sparks": "LA",     "Minnesota Lynx": "MIN",
    "New York Liberty": "NY",       "Phoenix Mercury": "PHX",
    "Seattle Storm": "SEA",         "Washington Mystics": "WSH",
    "Golden State Valkyries": "GSV",
}

# 5-minute in-process cache for today's game data (keyed sport_YYYY-MM-DD)
# so the schedule/odds merge is only computed once per TTL even when the
# page is hot-reloaded by multiple browser tabs.
import time as _time_mod
_GAMES_CACHE: dict[str, dict] = {}
_GAMES_CACHE_TTL = 300


def _g_abbr(name: str, sport: str) -> str:
    """Return a short team abbreviation from the lookup tables.
    Falls back to the last word's first 3-4 chars for unknown names."""
    table = _MLB_ABBR if sport == "mlb" else _WNBA_ABBR
    abbr  = table.get(name.strip())
    if abbr:
        return abbr
    # Fallback: strip common suffixes, take last word first chars.
    parts = name.strip().split()
    return parts[-1][:4].upper() if parts else name[:4].upper()


def _g_ml(odds) -> str:
    """Format American moneyline odds as '+130' / '-145' / ''."""
    if odds is None:
        return ""
    try:
        n = int(odds)
        return f"+{n}" if n > 0 else str(n)
    except (TypeError, ValueError):
        return ""


def _g_spread(spread, for_home: bool) -> str:
    """Format spread as '+1.5' / '-1.5'.  spread is the home team's line."""
    if spread is None:
        return ""
    try:
        v = float(spread)
        v = v if for_home else -v
        return f"{v:+.1f}"
    except (TypeError, ValueError):
        return ""


def _g_time(commence_time: str) -> str:
    """Format ISO UTC commence time as 'H:MM AM/PM ET' (no zero-padding)."""
    if not commence_time:
        return ""
    try:
        dt  = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        et  = dt.astimezone(ZoneInfo("America/New_York"))
        h   = et.hour % 12 or 12
        p   = "PM" if et.hour >= 12 else "AM"
        return f"{h}:{et.minute:02d} {p}"
    except Exception:                                                     # noqa: BLE001
        return ""


def _g_total(g: dict):
    """Extract the O/U line from wherever it lives in a serialized game."""
    for getter in (
        lambda d: d.get("total_line"),
        lambda d: (d.get("totals") or {}).get("total_line"),
        lambda d: (d.get("totals") or {}).get("line"),
    ):
        try:
            v = getter(g)
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    return None


def _get_today_games(backend, sport: str) -> dict:
    """Merge today's schedule (status + scores) with serialized odds data.

    Returns {upcoming: list[dict], final: list[dict]}.

    Upcoming dicts: home_team, away_team, home/away_abbr, commence_time,
        home_ml, away_ml, spread, total_line.
    Final dicts: same plus home_score, away_score.

    5-minute in-process cache keyed sport+date so a hot page reload pays
    only one merge per TTL window.
    """
    today     = datetime.now(_ET).date().isoformat()
    cache_key = f"{sport}_{today}"
    entry     = _GAMES_CACHE.get(cache_key)
    if entry and (_time_mod.monotonic() - entry["ts"]) < _GAMES_CACHE_TTL:
        return entry["data"]

    # ── Schedule: all games for today, with status + live/final scores ──
    try:
        schedule: list[dict] = list(backend.get_todays_schedule(sport) or [])
    except Exception:                                                     # noqa: BLE001
        schedule = []

    # ── Odds: from already-serialized (model-analyzed) games ────────────
    # Keyed by (home_team, away_team) string pair.  Games not analyzed by
    # the model will still show in the schedule but without odds data.
    odds_map: dict[tuple, dict] = {}
    try:
        for g in _all_serialized_games(backend):
            if (g.get("_sport") or "").lower() == sport.lower():
                key = (
                    (g.get("home_team") or "").strip(),
                    (g.get("away_team") or "").strip(),
                )
                odds_map[key] = g
    except Exception:                                                     # noqa: BLE001
        pass

    upcoming: list[dict] = []
    final:    list[dict] = []

    for game in schedule:
        home   = (game.get("home_team") or "").strip()
        away   = (game.get("away_team") or "").strip()
        status = (game.get("status")      or "Preview").strip()
        coded  = (game.get("coded_status") or "").upper()

        odds = odds_map.get((home, away), {})

        item: dict = {
            "home_team":     home,
            "away_team":     away,
            "home_abbr":     _g_abbr(home, sport),
            "away_abbr":     _g_abbr(away, sport),
            "commence_time": game.get("commence_time", ""),
            "home_ml":       odds.get("home_odds"),
            "away_ml":       odds.get("away_odds"),
            "spread":        odds.get("spread"),
            "total_line":    _g_total(odds),
            "home_score":    game.get("home_score"),
            "away_score":    game.get("away_score"),
        }

        # coded "F" / "O" = Final / Game Over; also catch status strings.
        is_final = (
            coded in ("F", "O")
            or status.lower() in ("final", "game over", "completed")
        )
        if is_final and item["home_score"] is not None:
            final.append(item)
        else:
            upcoming.append(item)

    upcoming.sort(key=lambda g: g.get("commence_time") or "")
    final.sort(key=lambda g: g.get("commence_time") or "", reverse=True)

    data = {"upcoming": upcoming, "final": final}
    _GAMES_CACHE[cache_key] = {"ts": _time_mod.monotonic(), "data": data}
    return data


def _upcoming_rows_html(games: list[dict]) -> str:
    """Build a concatenated HTML string for all upcoming-game rows.

    Each row: [O/U badge] [Away abbr] [away ML] [away spread]  vs
              [Home abbr] [home ML] [home spread]  [Time ET]

    Using raw HTML (not NiceGUI helpers) so the <div> flex layout
    renders correctly without NiceGUI's div wrappers around each child.
    The outer container carries overflow-x: auto for mobile.
    """
    import html as _he
    rows: list[str] = []

    for i, g in enumerate(games):
        is_last    = i == len(games) - 1
        border_css = "" if is_last else f"border-bottom:1px solid {t.BORDER_SOFT};"

        home_abbr = _he.escape(g["home_abbr"])
        away_abbr = _he.escape(g["away_abbr"])
        home_ml   = _g_ml(g.get("home_ml"))
        away_ml   = _g_ml(g.get("away_ml"))
        home_sp   = _g_spread(g.get("spread"), for_home=True)
        away_sp   = _g_spread(g.get("spread"), for_home=False)
        time_str  = _g_time(g.get("commence_time", ""))
        total     = g.get("total_line")

        # O/U badge — fixed-width slot so columns align even when absent
        if total is not None:
            ou = (
                f'<span style="background:{t.CARD_HI};color:{t.TEXT_DIM2};'
                f'font-size:9px;font-weight:800;letter-spacing:.4px;'
                f'padding:2px 6px;border-radius:{t.RADIUS_PILL};'
                f'white-space:nowrap;flex-shrink:0;">O/U {total:.1f}</span>'
            )
        else:
            ou = (
                f'<span style="display:inline-block;width:54px;flex-shrink:0;">'
                f'</span>'
            )

        def _bold(s: str) -> str:
            return (
                f'<span style="color:{t.TEXT};font-size:12px;font-weight:700;'
                f'white-space:nowrap;">{s}</span>'
            )

        def _muted(s: str) -> str:
            return (
                f'<span style="color:{t.TEXT_DIM2};font-size:11px;'
                f'font-family:monospace;white-space:nowrap;">{s}</span>'
            )

        def _sep(s: str) -> str:
            return (
                f'<span style="color:{t.TEXT_DIM2};font-size:11px;'
                f'padding:0 2px;">{s}</span>'
            )

        rows.append(
            f'<div style="display:flex;align-items:center;gap:8px;'
            f'padding:9px 14px;{border_css}min-width:420px;">'
            + ou
            + _bold(away_abbr)
            + (_muted(away_ml) if away_ml else "")
            + (_muted(away_sp) if away_sp else "")
            + _sep("vs")
            + _bold(home_abbr)
            + (_muted(home_ml) if home_ml else "")
            + (_muted(home_sp) if home_sp else "")
            + (
                f'<span style="margin-left:auto;font-size:10.5px;'
                f'color:{t.TEXT_DIM2};font-family:monospace;'
                f'white-space:nowrap;flex-shrink:0;">{time_str}</span>'
                if time_str else ""
            )
            + "</div>"
        )

    return "".join(rows)


def _final_rows_html(games: list[dict]) -> str:
    """Build concatenated HTML for all final-game rows.

    Each row: [FINAL badge] [Away abbr] [away score]  at
              [Home abbr] [home score]

    Winner's score is bright white + bold; loser's score is muted.
    """
    import html as _he
    rows: list[str] = []

    for i, g in enumerate(games):
        is_last    = i == len(games) - 1
        border_css = "" if is_last else f"border-bottom:1px solid {t.BORDER_SOFT};"

        hs = g.get("home_score")
        as_ = g.get("away_score")
        home_abbr = _he.escape(g["home_abbr"])
        away_abbr = _he.escape(g["away_abbr"])

        home_wins = (hs is not None and as_ is not None and hs > as_)
        away_wins = (hs is not None and as_ is not None and as_ > hs)

        def _team(abbr: str, wins: bool) -> str:
            col = t.TEXT if wins else t.TEXT_DIM
            wt  = "700"  if wins else "400"
            return (
                f'<span style="color:{col};font-size:13px;font-weight:{wt};'
                f'white-space:nowrap;">{abbr}</span>'
            )

        def _score(val, wins: bool) -> str:
            col = t.TEXT if wins else t.TEXT_DIM2
            wt  = "800"  if wins else "400"
            s   = str(val) if val is not None else "—"
            return (
                f'<span style="color:{col};font-size:16px;font-weight:{wt};'
                f'font-family:monospace;min-width:26px;text-align:right;'
                f'display:inline-block;">{s}</span>'
            )

        rows.append(
            f'<div style="display:flex;align-items:center;gap:10px;'
            f'padding:9px 14px;{border_css}min-width:280px;">'
            + f'<span style="background:{t.CARD_HI};color:{t.TEXT_DIM2};'
              f'font-size:8.5px;font-weight:800;letter-spacing:.4px;'
              f'padding:2px 6px;border-radius:{t.RADIUS_PILL};'
              f'white-space:nowrap;flex-shrink:0;">FINAL</span>'
            + _team(away_abbr, away_wins)
            + _score(as_, away_wins)
            + f'<span style="color:{t.TEXT_DIM2};font-size:11px;'
              f'padding:0 2px;">at</span>'
            + _team(home_abbr, home_wins)
            + _score(hs, home_wins)
            + "</div>"
        )

    return "".join(rows)


def _section_games(backend) -> None:
    """Upcoming Games + Final Scores — two sub-sections sharing a sport toggle.

    A single @ui.refreshable wraps both so toggling [MLB | WNBA] re-renders
    both sections atomically (one call to _render.refresh()).

    Sport auto-selection mirrors the news section: defaults to MLB; switches
    to WNBA only when WNBA has analysis results and MLB does not.
    """
    sport_state: dict = {"sport": "mlb"}
    try:
        wnba_ok = bool((backend._wnba_analysis_state or {}).get("results"))
        mlb_ok  = bool((backend._analysis_state or {}).get("results"))
        if wnba_ok and not mlb_ok:
            sport_state["sport"] = "wnba"
    except Exception:                                                     # noqa: BLE001
        pass

    @ui.refreshable
    def _render() -> None:                                               # noqa: WPS430
        sport    = sport_state["sport"]
        data     = _get_today_games(backend, sport)
        upcoming = data["upcoming"]
        final    = data["final"]

        with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):

            # ── Upcoming header + sport toggle ─────────────────────────
            with ui.row().classes("items-center w-full").style("gap: 8px;"):
                ui.label("UPCOMING").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(str(len(upcoming))).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
                ui.element("div").style("flex: 1; min-width: 4px;")
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _sk, _sl in (("mlb", "MLB"), ("wnba", "WNBA")):
                        _active = sport_state["sport"] == _sk

                        def _mk_toggle(sk=_sk):
                            def _toggle():
                                sport_state["sport"] = sk
                                _render.refresh()
                            return _toggle

                        ui.button(_sl, on_click=_mk_toggle()).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {t.PRIMARY if _active else t.CARD_HI}; "
                            f"color: {t.BG if _active else t.TEXT_DIM}; "
                            f"font-size: 10.5px; font-weight: 700; "
                            f"padding: 4px 10px; "
                            f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                        )

            # ── Upcoming rows ───────────────────────────────────────────
            if not upcoming:
                ui.label("No upcoming games today.").style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; text-align: center; "
                    f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                    f"width: 100%;"
                )
            else:
                ui.html(
                    f'<div style="background:{t.CARD};border:1px solid {t.BORDER};'
                    f'border-radius:{t.RADIUS_MD};overflow:hidden;'
                    f'overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%;">'
                    + _upcoming_rows_html(upcoming)
                    + "</div>"
                )

            # ── Final header ────────────────────────────────────────────
            with ui.row().classes("items-center w-full").style(
                f"gap: 8px; padding-top: {t.SPACE_SM};"
            ):
                ui.label("FINAL").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(str(len(final))).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )

            # ── Final rows ──────────────────────────────────────────────
            if not final:
                ui.label("No completed games yet.").style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; text-align: center; "
                    f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                    f"width: 100%;"
                )
            else:
                ui.html(
                    f'<div style="background:{t.CARD};border:1px solid {t.BORDER};'
                    f'border-radius:{t.RADIUS_MD};overflow:hidden;'
                    f'overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%;">'
                    + _final_rows_html(final)
                    + "</div>"
                )

    _render()


# ─────────────────────────────────────────────────────────────────────────────
#  Section: ESPN news feed (between EV Scan and Highest Confidence)
# ─────────────────────────────────────────────────────────────────────────────

# Sport → tag label shown in the badge on each news row.
# The homepage uses the MLB feed as its primary source; the tag makes
# it obvious to the user where the headlines come from.
_SPORT_TAG: dict[str, str] = {
    "mlb":  "MLB",
    "nba":  "NBA",
    "nfl":  "NFL",
    "nhl":  "NHL",
    "wnba": "WNBA",
}

# Sport → badge colours.  Intentionally different from the primary pick
# colours so news badges don't visually compete with the pick cards.
_SPORT_TAG_STYLE: dict[str, str] = {
    "mlb":  f"background: #1a2e4a; color: #60a5fa;",   # MLB blue
    "nba":  f"background: #1f1a00; color: #f59e0b;",   # NBA amber
    "nfl":  f"background: #1a1a2e; color: #a78bfa;",   # NFL purple
    "nhl":  f"background: #001a1a; color: #34d399;",   # NHL teal
    "wnba": f"background: #1f1a00; color: #f59e0b;",   # WNBA amber (NBA feed)
}


def _section_news(backend) -> None:
    """ESPN RSS headlines — fetched server-side, cached 5 min.

    The homepage always loads the MLB feed (primary sport).  The fetch
    is synchronous with a 5-second timeout; the 5-minute cache means
    only the first page load per Railway dyno pays the network cost.
    All subsequent renders within the TTL window are pure in-process
    dict reads (~microseconds).

    Wrapped in _guarded_section so a network failure or a parse error
    never blanks the rest of the home page.
    """
    # Determine the dominant active sport so the tag and feed match
    # what the user is seeing in the rest of the page.
    try:
        wnba_active = bool(
            (backend._wnba_analysis_state or {}).get("results")
        )
        mlb_active = bool(
            (backend._analysis_state or {}).get("results")
        )
    except Exception:                                                     # noqa: BLE001
        wnba_active = mlb_active = False

    # MLB is the primary sport; fall back to WNBA only when MLB has no
    # results and WNBA does.
    sport = "wnba" if (wnba_active and not mlb_active) else "mlb"

    try:
        from src.news_feed import fetch as _nf_fetch
        items = _nf_fetch(sport, max_items=10)
    except Exception:                                                     # noqa: BLE001
        items = []

    tag_label = _SPORT_TAG.get(sport, "ESPN")
    tag_style  = _SPORT_TAG_STYLE.get(
        sport,
        f"background: {t.CARD_HI}; color: {t.WARN};",
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        # ── Section header ────────────────────────────────────────────
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("NEWS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            if items:
                ui.label(str(len(items))).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
            ui.label(f"via ESPN · {tag_label}").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"margin-left: auto;"
            )

        # ── Empty / error state ───────────────────────────────────────
        if not items:
            ui.label("Headlines unavailable — check back shortly.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; text-align: center; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                f"width: 100%;"
            )
            return

        # ── News rows rendered as raw HTML ────────────────────────────
        # ui.html() is used so the anchor tags are real <a> elements
        # with proper href / target / rel attributes -- NiceGUI's link
        # helpers add unwanted wrappers that break flex row alignment.
        # Titles are HTML-escaped by news_feed._parse_items() before
        # they reach this point so no double-escaping is needed here.
        border_color = t.BORDER_SOFT

        rows_html: list[str] = []
        for i, item in enumerate(items):
            is_last    = i == len(items) - 1
            border_css = "" if is_last else (
                f"border-bottom: 1px solid {border_color};"
            )
            time_span  = (
                f'<span style="font-size:10.5px;color:{t.TEXT_DIM2};'
                f'font-family:monospace;white-space:nowrap;flex-shrink:0;">'
                f'{item["time_ago"]}</span>'
            ) if item.get("time_ago") else ""
            rows_html.append(
                f'<a href="{item["link"]}" target="_blank" rel="noopener noreferrer"'
                f' style="display:flex;align-items:center;gap:10px;'
                f'padding:10px 14px;text-decoration:none;{border_css}'
                f'transition:background 150ms ease-out;cursor:pointer;"'
                f' onmouseover="this.style.background=\'rgba(255,255,255,0.04)\'"'
                f' onmouseout="this.style.background=\'\'">'
                # Sport badge
                f'<span style="font-size:8.5px;font-weight:800;letter-spacing:.5px;'
                f'padding:2px 6px;border-radius:{t.RADIUS_PILL};white-space:nowrap;'
                f'flex-shrink:0;{tag_style}">{tag_label}</span>'
                # Time ago
                f'{time_span}'
                # Headline (already HTML-safe from news_feed module)
                f'<span style="font-size:13px;color:{t.TEXT};flex:1;'
                f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">'
                f'{item["title"]}</span>'
                # Chevron
                f'<span style="font-size:14px;color:{t.TEXT_DIM2};flex-shrink:0;'
                f'line-height:1;">›</span>'
                f'</a>'
            )

        ui.html(
            f'<div style="background:{t.CARD};border:1px solid {t.BORDER};'
            f'border-radius:{t.RADIUS_MD};overflow:hidden;width:100%;">'
            + "".join(rows_html)
            + "</div>"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Section: Team Rotation Quadrant Chart
# ─────────────────────────────────────────────────────────────────────────────

def _rotation_chart_opts(points: list[dict], metric: str, sport: str) -> dict:
    """Build ECharts scatter-plot options for the team rotation quadrant.

    Axes are scaled to 0–100 so ECharts can render '%' labels natively.
    Each data point carries its own tooltip HTML via the per-item
    tooltip.formatter property — no JavaScript function needed.

    Quadrant colour mapping (matches zone tint):
      Top-right  (x≥50, y≥50) = Leading    → emerald
      Top-left   (x<50,  y≥50) = Improving  → blue
      Bottom-right(x≥50, y<50) = Weakening  → amber
      Bottom-left (x<50,  y<50) = Lagging    → rose
    """
    _metric_axis = {
        "ml":  "Win %",
        "ats": "ATS Cover % (run-line proxy)",
        "ou":  "Over %",
    }
    _metric_short = {"ml": "ML", "ats": "ATS", "ou": "O/U"}

    # WNBA ATS / O/U fall back to ML — tell the user
    wnba_proxy = (
        sport == "wnba" and metric in ("ats", "ou") and
        any(pt.get("ml_proxy") for pt in points)
    )
    x_name = (
        f"Recent 14d — {_metric_axis['ml'] if wnba_proxy else _metric_axis[metric]}"
        + (" (ML proxy)" if wnba_proxy else "")
    )
    y_name = (
        f"Season — {_metric_axis['ml'] if wnba_proxy else _metric_axis[metric]}"
        + (" (ML proxy)" if wnba_proxy else "")
    )

    # Build scatter data with per-item tooltip + quadrant colour
    scatter_data: list[dict] = []
    for pt in points:
        x100 = round(pt["x"] * 100, 1)
        y100 = round(pt["y"] * 100, 1)

        if x100 >= 50 and y100 >= 50:
            color = t.POS           # Leading — emerald
        elif x100 < 50 and y100 >= 50:
            color = "#3b82f6"       # Improving — blue
        elif x100 >= 50 and y100 < 50:
            color = t.WARN          # Weakening — amber
        else:
            color = t.NEG           # Lagging — rose

        ml_lbl = _metric_short["ml" if wnba_proxy else metric]
        szn_g  = pt["szn_w"] + pt["szn_l"]
        rec_g  = pt["rec_w"] + pt["rec_l"]

        # Pre-built HTML tooltip — no JS function required
        tooltip_html = (
            f"<b style='font-size:13px'>{pt['name']}</b><br/>"
            f"Season {ml_lbl}: {pt['szn_w']}-{pt['szn_l']}"
            + (f" ({round(y100)}%)" if szn_g else "")
            + f"<br/>Recent L14: {pt['rec_w']}-{pt['rec_l']}"
            + (f" ({round(x100)}%)" if rec_g else "")
        )

        scatter_data.append({
            "value":     [x100, y100],
            "name":      pt["abbr"],
            "itemStyle": {
                "color":       color,
                "borderColor": "rgba(0,0,0,0.2)",
                "borderWidth": 1,
            },
            "tooltip":   {"formatter": tooltip_html},
        })

    # Quadrant background zones + labels (via markArea)
    _zones = [
        # [start_point, end_point]  — (xAxis, yAxis) give the diagonal corners
        [
            {
                "name": "Leading",
                "xAxis": 50, "yAxis": 50,
                "itemStyle": {"color": "rgba(16,185,129,0.09)"},
                "label": {
                    "position": "insideTopRight",
                    "color": "rgba(16,185,129,0.45)",
                    "fontSize": 11, "fontWeight": "700", "fontStyle": "italic",
                },
            },
            {"xAxis": 100, "yAxis": 100},
        ],
        [
            {
                "name": "Improving",
                "xAxis": 0, "yAxis": 50,
                "itemStyle": {"color": "rgba(59,130,246,0.07)"},
                "label": {
                    "position": "insideTopLeft",
                    "color": "rgba(59,130,246,0.45)",
                    "fontSize": 11, "fontWeight": "700", "fontStyle": "italic",
                },
            },
            {"xAxis": 50, "yAxis": 100},
        ],
        [
            {
                "name": "Weakening",
                "xAxis": 50, "yAxis": 0,
                "itemStyle": {"color": "rgba(245,158,11,0.08)"},
                "label": {
                    "position": "insideBottomRight",
                    "color": "rgba(245,158,11,0.45)",
                    "fontSize": 11, "fontWeight": "700", "fontStyle": "italic",
                },
            },
            {"xAxis": 100, "yAxis": 50},
        ],
        [
            {
                "name": "Lagging",
                "xAxis": 0, "yAxis": 0,
                "itemStyle": {"color": "rgba(244,63,94,0.08)"},
                "label": {
                    "position": "insideBottomLeft",
                    "color": "rgba(244,63,94,0.45)",
                    "fontSize": 11, "fontWeight": "700", "fontStyle": "italic",
                },
            },
            {"xAxis": 50, "yAxis": 50},
        ],
    ]

    _axis_common = {
        "type": "value",
        "min": 0, "max": 100,
        "splitLine": {"show": False},
        "axisLine": {"lineStyle": {"color": t.BORDER}},
        "axisTick": {"show": False},
        "axisLabel": {
            "color":     t.TEXT_DIM2,
            "fontSize":  9,
            "formatter": "{value}%",
        },
    }

    return {
        "backgroundColor": "transparent",
        "grid": {"left": "54px", "right": "24px", "top": "20px", "bottom": "48px"},
        "xAxis": {
            **_axis_common,
            "name":          x_name,
            "nameLocation":  "middle",
            "nameGap":       28,
            "nameTextStyle": {"color": t.TEXT_DIM2, "fontSize": 10},
        },
        "yAxis": {
            **_axis_common,
            "name":          y_name,
            "nameLocation":  "middle",
            "nameGap":       42,
            "nameTextStyle": {"color": t.TEXT_DIM2, "fontSize": 10},
        },
        "tooltip": {
            "trigger":         "item",
            "backgroundColor": t.CARD_HI,
            "borderColor":     t.BORDER,
            "textStyle":       {"color": t.TEXT, "fontSize": 12},
            "padding":         [8, 12],
        },
        "series": [
            # ── Background: quadrant tints + zone labels + dividers ─────────
            {
                "type":   "scatter",
                "data":   [],
                "silent": True,
                "markArea": {
                    "silent": True,
                    "label":  {"show": True},
                    "data":   _zones,
                },
                "markLine": {
                    "silent":    True,
                    "symbol":    "none",
                    "lineStyle": {"color": t.BORDER, "width": 1, "type": "solid"},
                    "label":     {"show": False},
                    "data": [
                        # Vertical   divider at x = 50
                        [{"xAxis": 50, "yAxis": 0}, {"xAxis": 50, "yAxis": 100}],
                        # Horizontal divider at y = 50
                        [{"xAxis": 0, "yAxis": 50}, {"xAxis": 100, "yAxis": 50}],
                    ],
                },
            },
            # ── Team dots ────────────────────────────────────────────────────
            {
                "type":       "scatter",
                "symbolSize": 28,
                "data":       scatter_data,
                "label": {
                    "show":            True,
                    "position":        "inside",
                    "formatter":       "{b}",
                    "color":           "#ffffff",
                    "fontSize":        8,
                    "fontWeight":      "700",
                    "textShadowBlur":  3,
                    "textShadowColor": "rgba(0,0,0,0.9)",
                },
                "emphasis": {
                    "scale":     True,
                    "itemStyle": {"shadowBlur": 10, "shadowColor": "rgba(0,0,0,0.4)"},
                },
            },
        ],
    }


def _section_rotation(backend) -> None:
    """Team Rotation quadrant chart.

    X-axis = recent 14-day performance; Y-axis = full-season performance.
    Toggle switches between ML (win%), ATS (run-line cover proxy), O/U.

    Uses src.team_rotation_cache which fetches statsapi.mlb.com (MLB) or
    ESPN standings (WNBA) and caches results for 1 hour.
    """
    # ── Initial state ────────────────────────────────────────────────────────
    metric_state: dict = {"metric": "ml"}
    sport_state:  dict = {"sport": "mlb"}
    try:
        wnba_ok = bool((backend._wnba_analysis_state or {}).get("results"))
        mlb_ok  = bool((backend._analysis_state       or {}).get("results"))
        if wnba_ok and not mlb_ok:
            sport_state["sport"] = "wnba"
    except Exception:                                                      # noqa: BLE001
        pass

    @ui.refreshable
    def _render() -> None:                                                 # noqa: WPS430
        try:
            from src.team_rotation_cache import get_rotation_data as _grd
        except ImportError:
            return

        sport  = sport_state["sport"]
        metric = metric_state["metric"]
        points = _grd(sport=sport, metric=metric)

        with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):

            # ── Header + toggles ──────────────────────────────────────────
            with ui.row().classes("items-center w-full").style(
                "gap: 8px; flex-wrap: wrap;"
            ):
                ui.label("TEAM ROTATION").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(str(len(points))).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
                ui.element("div").style("flex: 1; min-width: 4px;")

                # Sport toggle — subtle variant (outline style)
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _sk, _sl in (("mlb", "MLB"), ("wnba", "WNBA")):
                        _sp_active = sport_state["sport"] == _sk

                        def _mk_sport(sk=_sk):
                            def _cb():
                                sport_state["sport"] = sk
                                _render.refresh()
                            return _cb

                        ui.button(_sl, on_click=_mk_sport()).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {'rgba(124,58,237,0.15)' if _sp_active else t.CARD_HI}; "
                            f"color: {t.PRIMARY if _sp_active else t.TEXT_DIM2}; "
                            f"font-size: 10.5px; font-weight: 700; "
                            f"padding: 4px 10px; "
                            f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                        )

                # Metric toggle — primary style for active
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _mk, _ml in (
                        ("ml",  "ML"),
                        ("ats", "ATS"),
                        ("ou",  "O/U"),
                    ):
                        _m_active = metric_state["metric"] == _mk

                        def _mk_metric(mk=_mk):
                            def _cb():
                                metric_state["metric"] = mk
                                _render.refresh()
                            return _cb

                        ui.button(_ml, on_click=_mk_metric()).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {t.PRIMARY if _m_active else t.CARD_HI}; "
                            f"color: {t.BG if _m_active else t.TEXT_DIM}; "
                            f"font-size: 10.5px; font-weight: 700; "
                            f"padding: 4px 10px; "
                            f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                        )

            # ── Chart ─────────────────────────────────────────────────────
            if not points:
                with ui.row().classes("items-center justify-center w-full").style(
                    f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: 48px 20px;"
                ):
                    ui.label("No team data available yet — check back later.").style(
                        f"font-size: 13px; color: {t.TEXT_DIM};"
                    )
            else:
                with ui.element("div").style(
                    f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: 12px 8px 8px 8px; "
                    f"width: 100%; box-sizing: border-box;"
                ):
                    ui.echart(
                        _rotation_chart_opts(points, metric, sport)
                    ).style("width: 100%; height: 400px;")

    _render()


# ─────────────────────────────────────────────────────────────────────────────
#  Section: Season Heatmap
# ─────────────────────────────────────────────────────────────────────────────

def _heatmap_table_html(points: list[dict]) -> str:
    """Build the full HTML string for the season heatmap table.

    Renders a single <table> block so every cell aligns properly without
    NiceGUI's automatic div wrappers breaking <tr>/<td> semantics.

    Columns: # | Team | W-L | Win % | Bar
    Rows sorted by season_pct descending (best record first).
    """
    # ── Column header widths ────────────────────────────────────────────────
    col_rank  = "32px"
    col_team  = "auto"       # flex to fill remaining space
    col_wl    = "64px"
    col_pct   = "52px"
    col_bar   = "128px"

    th_style = (
        f"padding: 0 10px 8px 10px; text-align: left; "
        f"font-size: 10px; font-weight: 700; letter-spacing: .7px; "
        f"color: {t.TEXT_DIM2}; white-space: nowrap; border-bottom: 1px solid {t.BORDER};"
    )
    th_right = th_style.replace("left", "right")

    header = (
        f"<thead><tr>"
        f"<th style='{th_style} width:{col_rank}; padding-left:14px;'>#</th>"
        f"<th style='{th_style} min-width:120px;'>TEAM</th>"
        f"<th style='{th_right} width:{col_wl};'>W-L</th>"
        f"<th style='{th_right} width:{col_pct};'>WIN%</th>"
        f"<th style='{th_style} width:{col_bar}; text-align:center;'>BAR</th>"
        f"</tr></thead>"
    )

    def _bar_color(pct100: float) -> str:
        if pct100 >= 60:
            return "#22c55e"
        if pct100 >= 50:
            return "#84cc16"
        return "#ef4444"

    rows_html: list[str] = []
    for rank, pt in enumerate(points, start=1):
        pct100   = pt["y"] * 100
        wl_str   = f"{pt['szn_w']}-{pt['szn_l']}"
        pct_str  = f"{pct100:.1f}%"
        bar_col  = _bar_color(pct100)
        bar_w    = f"{min(pct100, 100):.1f}%"   # fill proportional to win%
        alt_bg   = "rgba(255,255,255,0.025)" if rank % 2 == 0 else "transparent"

        td_base = (
            f"padding: 0 10px; height: 40px; "
            f"font-size: 12px; font-weight: 500; "
            f"color: {t.TEXT}; white-space: nowrap; vertical-align: middle; "
            f"border-bottom: 1px solid {t.BORDER_SOFT};"
        )
        td_dim   = td_base.replace(f"color: {t.TEXT}", f"color: {t.TEXT_DIM2}")
        td_right = td_base + " text-align: right;"
        td_mono  = td_right + " font-family: monospace;"

        bar_html = (
            f"<div style='width:100%;height:6px;background:{t.CARD_HI};"
            f"border-radius:3px;overflow:hidden;'>"
            f"<div style='width:{bar_w};height:100%;background:{bar_col};"
            f"border-radius:3px;'></div>"
            f"</div>"
        )

        rows_html.append(
            f"<tr style='background:{alt_bg};'>"
            f"<td style='{td_dim} padding-left:14px; width:{col_rank};'>{rank}</td>"
            f"<td style='{td_base} font-weight:600;'>{pt['name']}</td>"
            f"<td style='{td_mono} width:{col_wl};'>{wl_str}</td>"
            f"<td style='{td_mono} width:{col_pct};'>{pct_str}</td>"
            f"<td style='{td_base} width:{col_bar}; padding: 0 12px;'>{bar_html}</td>"
            f"</tr>"
        )

    return (
        f"<div style='overflow-x:auto; width:100%;'>"
        f"<table style='width:100%; border-collapse:collapse; "
        f"table-layout:fixed;'>"
        + header
        + "<tbody>" + "".join(rows_html) + "</tbody>"
        + "</table></div>"
    )


def _section_heatmap(backend) -> None:
    """Season Heatmap — every team ranked by ATS/ML/O/U season record.

    Reuses team_rotation_cache.get_rotation_data() so no extra API calls
    are made; data is already cached from the Team Rotation section above.
    The 'y' field (season performance, 0–1) and szn_w/szn_l supply all
    the data needed for the table.
    """
    metric_state: dict = {"metric": "ml"}
    sport_state:  dict = {"sport": "mlb"}
    try:
        wnba_ok = bool((backend._wnba_analysis_state or {}).get("results"))
        mlb_ok  = bool((backend._analysis_state       or {}).get("results"))
        if wnba_ok and not mlb_ok:
            sport_state["sport"] = "wnba"
    except Exception:                                                      # noqa: BLE001
        pass

    @ui.refreshable
    def _render() -> None:                                                 # noqa: WPS430
        try:
            from src.team_rotation_cache import get_rotation_data as _grd
        except ImportError:
            return

        sport  = sport_state["sport"]
        metric = metric_state["metric"]
        raw    = _grd(sport=sport, metric=metric)

        # Sort by season win% descending; filter out entries with no games
        points = sorted(
            [p for p in raw if (p["szn_w"] + p["szn_l"]) > 0],
            key=lambda p: -p["y"],
        )

        with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):

            # ── Header + toggles ──────────────────────────────────────────
            with ui.row().classes("items-center w-full").style(
                "gap: 8px; flex-wrap: wrap;"
            ):
                ui.label("SEASON HEATMAP").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(str(len(points))).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
                ui.element("div").style("flex: 1; min-width: 4px;")

                # Sport toggle
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _sk, _sl in (("mlb", "MLB"), ("wnba", "WNBA")):
                        _sp_active = sport_state["sport"] == _sk

                        def _mk_sport(sk=_sk):
                            def _cb():
                                sport_state["sport"] = sk
                                _render.refresh()
                            return _cb

                        ui.button(_sl, on_click=_mk_sport()).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {'rgba(124,58,237,0.15)' if _sp_active else t.CARD_HI}; "
                            f"color: {t.PRIMARY if _sp_active else t.TEXT_DIM2}; "
                            f"font-size: 10.5px; font-weight: 700; "
                            f"padding: 4px 10px; "
                            f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                        )

                # Metric toggle
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _mk, _ml in (
                        ("ml",  "ML"),
                        ("ats", "ATS"),
                        ("ou",  "O/U"),
                    ):
                        _m_active = metric_state["metric"] == _mk

                        def _mk_metric(mk=_mk):
                            def _cb():
                                metric_state["metric"] = mk
                                _render.refresh()
                            return _cb

                        ui.button(_ml, on_click=_mk_metric()).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {t.PRIMARY if _m_active else t.CARD_HI}; "
                            f"color: {t.BG if _m_active else t.TEXT_DIM}; "
                            f"font-size: 10.5px; font-weight: 700; "
                            f"padding: 4px 10px; "
                            f"border-radius: {t.RADIUS_PILL}; min-height: 0;"
                        )

            # ── Table or empty state ──────────────────────────────────────
            if not points:
                with ui.row().classes("items-center justify-center w-full").style(
                    f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: 48px 20px;"
                ):
                    ui.label("No team data available yet — check back later.").style(
                        f"font-size: 13px; color: {t.TEXT_DIM};"
                    )
            else:
                with ui.element("div").style(
                    f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; overflow: hidden; width: 100%;"
                ):
                    ui.html(_heatmap_table_html(points))

    _render()


def _ai_banner() -> None:
    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_MD}; "
        f"gap: {t.SPACE_MD}; cursor: pointer;"
    ).on("click", lambda: ui.navigate.to("/ai")):
        with ui.column().style("flex: 1; gap: 4px;"):
            ui.label("AI Breakdown").style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT};"
            )
            ui.label("Ask the model anything about today's picks.").style(
                f"font-size: 12px; color: {t.TEXT_DIM};"
            )
        ui.label("→").style(
            f"font-size: 18px; color: {t.PRIMARY};"
        )
