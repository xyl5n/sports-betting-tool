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

from nicegui import ui

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
        _guarded_section("ev_compact", lambda: _section_ev_compact(backend))
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

    # Single row with nowrap so chips stay side-by-side at every viewport.
    # min-width:0 on each child lets them shrink past content with ellipsis
    # instead of overflowing the page width.
    with ui.row().classes("w-full").style(
        f"gap: {t.SPACE_SM}; flex-wrap: nowrap; align-items: stretch;"
    ):
        if show_overall:
            _chip_overall(overall)
        _chip_best_model(best_m)
        _chip_best_bet_type(best_t)


def _chip_overall(overall: dict) -> None:
    w, l, pct = overall["wins"], overall["losses"], overall.get("pct")
    color = hs.winrate_color(pct, t)
    main  = f"{w}-{l}"
    pct_s = f"{pct * 100:.0f}%" if pct is not None else "—"
    _chip(label="OVERALL", main=main, suffix=pct_s, color=color)


def _chip_best_model(best: dict | None) -> None:
    if not best:
        _chip(label="BEST MODEL", main="—", suffix="not enough data",
              color=t.TEXT_DIM)
        return
    color = hs.winrate_color(best["pct"], t)
    _chip(
        label="BEST MODEL",
        main=best["model"],
        suffix=f"{best['pct'] * 100:.0f}%",
        color=color,
    )


def _chip_best_bet_type(best: dict | None) -> None:
    if not best:
        _chip(label="BEST BET TYPE", main="—", suffix="not enough data",
              color=t.TEXT_DIM)
        return
    color = hs.winrate_color(best["pct"], t)
    _chip(
        label="BEST BET TYPE",
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
    """
    games = _all_serialized_games(backend)
    rows  = hs.enumerate_value_picks(games, min_edge=0.03)
    rows.sort(key=lambda r: float(r.get("edge") or 0), reverse=True)

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        # Header: title + edge threshold + count badge
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("EV SCAN").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label("edge ≥ 3%").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};"
            )
            ui.label(f"{len(rows)}").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL}; "
                f"margin-left: auto;"
            )

        # Empty state -- centered notice the spec asks for verbatim.
        if not rows:
            ui.label("No high value picks available right now").style(
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
    games = _all_serialized_games(backend)
    rows  = hs.enumerate_value_picks(games, min_edge=0.0001)   # any positive edge
    rows.sort(key=lambda r: float(r.get("prob") or 0), reverse=True)
    rows = rows[:10]

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
            ui.label("No positive-edge picks yet.").style(
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
    pct, units  = perf["pct"], perf["units"]

    pct_s   = f"{pct * 100:.1f}%" if pct is not None else "—"
    pct_col = hs.winrate_color(pct, t)

    units_sign  = "+" if units >= 0 else "−"
    units_s     = f"{units_sign}{abs(units):.1f}U"
    units_col   = t.POS if units >= 0 else t.NEG

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("MODEL PERFORMANCE").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label("settled history · 1U flat").style(
                f"font-size: 11px; color: {t.TEXT_DIM2};"
            )
        with ui.row().classes("w-full").style(
            f"gap: {t.SPACE_SM}; flex-wrap: nowrap; align-items: stretch;"
        ):
            _perf_stat("WIN %",  pct_s,                 pct_col)
            _perf_stat("RECORD", f"{wins}-{losses}",    t.TEXT)
            _perf_stat("UNITS",  units_s,               units_col)


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
