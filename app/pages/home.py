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

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav
from components import track_button
from pages import home_stats as hs


def register(backend) -> None:
    @ui.page("/")
    def home_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_HOME)
        _layout(backend)
        bottom_nav.render(active=t.TAB_HOME)


def _layout(backend) -> None:
    with ui.row().classes("no-wrap w-full").style("gap: 0;"):
        sidebar.render(backend)
        with ui.column().classes("page-content").style(
            f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
            f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
        ):
            _section_chips(backend)                  # Section 1
            sidebar.render_top_plays_only(backend)   # mobile-only inline
            _section_ev_compact(backend)             # Section 2
            _section_confidence_carousel(backend)    # Section 3
            _ai_banner()
            _section_model_performance(backend)      # Section 5 (very bottom)


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
    games = _all_serialized_games(backend)
    rows  = hs.enumerate_value_picks(games, min_edge=0.03)
    rows.sort(key=lambda r: float(r.get("edge") or 0), reverse=True)

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
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
        if not rows:
            ui.label("No value picks at the 3% threshold yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_SM}; gap: 0;"
        ):
            for r in rows:
                _ev_row(backend, r)


def _ev_row(backend, r: dict) -> None:
    edge_pct = float(r.get("edge") or 0) * 100
    edge_s   = f"+{edge_pct:.1f}% Edge"
    with ui.row().classes("items-center w-full no-wrap").style(
        f"padding: 8px 6px; gap: 10px; "
        f"border-bottom: 1px solid {t.BORDER_SOFT};"
    ):
        with ui.column().style("flex: 1; min-width: 0; gap: 2px;"):
            ui.label(r["matchup"]).style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"letter-spacing: .3px; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(r["pick"]).style(
                f"font-size: 13.5px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
        ui.label(edge_s).style(
            f"font-size: 12.5px; font-weight: 800; color: {t.POS}; "
            f"font-family: monospace; flex-shrink: 0;"
        )
        if r.get("game_id"):
            track_button.render(
                backend, game_id=r["game_id"], sport=r.get("sport", "mlb"),
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
            scroller = ui.row().style(
                f"width: 100%; "
                f"overflow-x: auto; overflow-y: hidden; "
                f"gap: {t.SPACE_SM}; padding: 4px 2px; "
                f"scroll-snap-type: x mandatory; "
                f"scrollbar-width: thin; flex-wrap: nowrap;"
            )
            with scroller:
                for r in rows:
                    _confidence_card(r)

            # Scroll arrows -- thin overlay buttons on left/right.  Each
            # calls scrollBy on the parent row.  Hidden on touch devices
            # via the @media (hover: none) rule the runtime ignores --
            # native swipe/scroll still works there anyway.
            _carousel_arrow(scroller, direction="left")
            _carousel_arrow(scroller, direction="right")


def _confidence_card(r: dict) -> None:
    edge_pct = float(r.get("edge") or 0) * 100
    prob_pct = float(r.get("prob") or 0) * 100
    with ui.column().style(
        f"background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; "
        f"min-width: 200px; max-width: 200px; flex-shrink: 0; gap: 4px; "
        f"scroll-snap-align: start;"
    ):
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


def _carousel_arrow(scroller, direction: str) -> None:
    """Tiny < / > button absolutely positioned over the scroller's edge.

    Uses ui.run_javascript via the button's on_click to call scrollBy on
    the DOM node.  Native touch swipe still works regardless -- this is
    a desktop / pointer affordance.
    """
    is_left = direction == "left"
    arrow   = "‹" if is_left else "›"
    side    = "left: 2px;" if is_left else "right: 2px;"

    btn = ui.button(arrow).props("flat dense").style(
        f"position: absolute; top: 50%; {side} "
        f"transform: translateY(-50%); "
        f"background: {t.CARD}cc; color: {t.TEXT}; "
        f"width: 28px; height: 28px; min-height: 0; "
        f"font-size: 18px; font-weight: 800; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: 50%; padding: 0; line-height: 1; "
        f"z-index: 2;"
    )
    delta = -240 if is_left else 240

    # NiceGUI exposes the DOM id as element.html_id (typically "c<int>").
    # Use that directly so we don't depend on the internal id format.
    dom_id = getattr(scroller, "html_id", f"c{scroller.id}")

    async def _click():
        try:
            await ui.run_javascript(
                f"document.getElementById({dom_id!r})"
                f".scrollBy({{left: {delta}, behavior: 'smooth'}})"
            )
        except Exception:                                                 # noqa: BLE001
            # run_javascript may not be available in all NiceGUI builds; the
            # scroller still works via native swipe / wheel.
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
    try:
        bankroll = float(backend._analysis_state.get("bankroll") or 250)
        for r in (backend._analysis_state.get("results") or []):
            try:
                mlb_ledger = backend.Ledger(path="data/ledger.json",
                                            starting_bankroll=bankroll)
                s_bank = mlb_ledger.data.get("personal_starting_bankroll", bankroll)
                g = backend._serialize(r, bankroll, "mlb", s_bank)
                g["_sport"] = "mlb"
                out.append(g)
            except Exception:                                             # noqa: BLE001
                continue
    except Exception:                                                     # noqa: BLE001
        pass
    try:
        bankroll = float(backend._wnba_analysis_state.get("bankroll") or 1000)
        wnba_results = backend._wnba_analysis_state.get("results") or []
        if wnba_results:
            wnba_ledger = backend.Ledger(path="data/wnba_ledger.json",
                                         starting_bankroll=bankroll)
            s_bank = wnba_ledger.data.get("personal_starting_bankroll", bankroll)
            for r in wnba_results:
                try:
                    g = backend._serialize_wnba(r, bankroll, s_bank)
                    g["_sport"] = "wnba"
                    out.append(g)
                except Exception:                                         # noqa: BLE001
                    continue
    except Exception:                                                     # noqa: BLE001
        pass
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
