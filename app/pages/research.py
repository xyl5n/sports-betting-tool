"""
research.py
===========
/research -- model-performance analytics dashboard (replaces the Players tab).

Reads the forward-only settled-prop history (src/research_store) and renders:
  * a sticky filter bar (Model / Sport / Prop Type / Time Window),
  * four KPI cards (Win Rate, Total Picks, Avg Edge, ROI),
  * a sortable leaderboard grouped by (model, prop type).

Read-only: all aggregation is done by research_store.aggregate(); this module
is pure presentation.  Matches the OLED dark theme; purple (PRIMARY) marks the
active filter pills.
"""
from __future__ import annotations

import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, controls
from src import research_store as rs


def _dbg(msg: str) -> None:
    print(f"[RESEARCH] {msg}", flush=True, file=sys.stderr)


# ── Filter option sets (UI redesign, Change 4) ───────────────────────────────
# All dropdowns are {value: label} dicts so the value drives the store filter
# while the label stays human-friendly.  Multi-select dropdowns carry an "all"
# sentinel; selecting any concrete option drops it (see _on_multi).

# Prediction model.  XGBoost / Neural Net can't be told apart in the current
# store (it only records the AI-review model + the prop market), so those
# values map to no market restriction -- the UI shows a muted note explaining
# selecting them won't narrow results.  Pitcher / Batter map to a market
# prefix (best-effort), the user-approved Option 1 behaviour.
_PRED_OPTIONS = {
    "all":          "All",
    "xgb_mlb":      "XGBoost (MLB)",
    "nn_mlb":       "Neural Net (MLB)",
    "pitcher_mlb":  "Pitcher Model (MLB)",
    "xgb_wnba":     "XGBoost (WNBA)",
    "nn_wnba":      "Neural Net (WNBA)",
    "pitcher_wnba": "Pitcher Model (WNBA)",
    "batter":       "Batter Model",
}
# Prediction model -> market-key prefix.  Absent => no restriction (show all).
_PRED_PREFIX = {
    "pitcher_mlb":  "pitcher_",
    "pitcher_wnba": "pitcher_",
    "batter":       "batter_",
}
# The values that carry no market mapping (and thus "show all" with a note).
_PRED_SHOW_ALL = {"xgb_mlb", "nn_mlb", "xgb_wnba", "nn_wnba"}

# AI review model.  Values are the actual model names frozen into the store
# (groq_models.MODELS[...].name) so they match research_store rows directly.
# "Ollama" is a forward-looking placeholder with no data yet -> sentinel that
# never restricts (rendered with a "soon" label).
_OLLAMA_SENTINEL = "__ollama__"
_REVIEW_OPTIONS = {
    "all":                      "All",
    "llama-3.3-70b-versatile":  "Llama-3.3-70b",
    "compound-beta":            "Compound-Beta",
    "qwen/qwen3-32b":           "Qwen3",
    _OLLAMA_SENTINEL:           "Ollama (soon)",
}

_SPORT_OPTIONS = {"all": "All", "mlb": "MLB", "wnba": "WNBA"}

_WINDOW_OPTIONS = {
    "7d":     "Last 7 Days",
    "30d":    "Last 30 Days",
    "season": "This Season",
    "all":    "All Time",
}

# Bet type (renamed from "Prop Type").  Each value maps to one or more store
# market keys; selecting it keeps those markets as SEPARATE row groups in the
# results table (the store groups by market, so this falls out naturally).
_BET_TYPE_OPTIONS = {
    "all":                 "All",
    "moneyline":           "Moneyline",
    "run_line":            "Run Line",
    "total":               "Total",
    "batter_hits":         "Hits",
    "strikeouts":          "Strikeouts",
    "batter_rbis":         "RBIs",
    "batter_runs_scored":  "Runs",
    "walks":               "Walks",
    "batter_total_bases":  "Total Bases",
    "pitcher_earned_runs": "Earned Runs",
    "pitcher_outs":        "Outs",
    "pitcher_hits_allowed": "Hits Allowed",
}
_BET_TYPE_MARKETS = {
    "moneyline":            {"moneyline"},
    "run_line":             {"run_line"},
    "total":                {"total"},
    "batter_hits":          {"batter_hits"},
    "strikeouts":           {"pitcher_strikeouts", "batter_strikeouts"},
    "batter_rbis":          {"batter_rbis"},
    "batter_runs_scored":   {"batter_runs_scored"},
    "walks":                {"pitcher_walks", "batter_walks"},
    "batter_total_bases":   {"batter_total_bases"},
    "pitcher_earned_runs":  {"pitcher_earned_runs"},
    "pitcher_outs":         {"pitcher_outs"},
    "pitcher_hits_allowed": {"pitcher_hits_allowed"},
}
# Sentinel passed to the store when a filter combination resolves to "match
# nothing" (e.g. Pitcher Model + a batter-only bet type) so the table shows an
# honest empty state instead of silently widening back to everything.
_NO_MATCH = "__no_match__"

_PROP_LABELS = {
    "pitcher_strikeouts":   "Strikeouts (K)",
    "pitcher_outs":         "Outs",
    "pitcher_earned_runs":  "Earned Runs",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks":        "Walks",
    "batter_home_runs":     "Home Runs",
    "batter_hits":          "Hits",
    "batter_rbis":          "RBI",
    "batter_total_bases":   "Total Bases",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
    "points":               "Points",
    "rebounds":             "Rebounds",
    "assists":              "Assists",
}


def _prop_label(market: str) -> str:
    if not market or market == "all":
        return "All"
    return _PROP_LABELS.get(market) or market.replace("_", " ").title()


# ── Sortable column registry ─────────────────────────────────────────────────
# (header label, row key, numeric?)
_COLUMNS = (
    ("Model",      "model",     False, "180px"),
    ("Prop Type",  "prop_type", False, "150px"),
    ("Picks",      "picks",     True,  "70px"),
    ("Wins",       "wins",      True,  "70px"),
    ("Win %",      "win_pct",   True,  "80px"),
    ("Avg Edge %", "avg_edge",  True,  "100px"),
    ("ROI %",      "roi",       True,  "80px"),
    ("Hot Streak", "streak",    True,  "100px"),
)


# ── Page ─────────────────────────────────────────────────────────────────────

def register(backend) -> None:
    @ui.page("/research")
    def research_page():
        _dbg("research_page ENTER")
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_RESEARCH)
            _layout()
            bottom_nav.render(active=t.TAB_RESEARCH)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            print(f"[RESEARCH PAGE FATAL] {type(exc).__name__}: {exc}\n"
                  f"{_tb.format_exc()}", flush=True, file=sys.stderr)
            ui.label("Research page failed to render").style(
                f"color: {t.NEG}; font-size: 16px; font-weight: 700; "
                f"padding: {t.SPACE_LG};"
            )


def _layout() -> None:
    state = {
        "review":   {"all"},          # AI-review model names, or {"all"}
        "pred":     {"all"},          # prediction-model values, or {"all"}
        "sport":    "all",
        "bets":     {"all"},          # bet-type values, or {"all"}
        "window":   "all",
        "sort_key": "win_pct",
        "sort_dir": "desc",
    }

    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        ui.label("RESEARCH").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label("Model Analytics — settled-prop performance by AI model and prop type.").style(
            f"font-size: 12.5px; color: {t.TEXT_DIM};"
        )

        _filter_bar(state, lambda: _dashboard.refresh())

        @ui.refreshable
        def _dashboard() -> None:                                         # noqa: WPS430
            agg = rs.aggregate(
                models=_effective_models(state),
                sport=state["sport"],
                prop_types=_effective_prop_types(state),
                window=state["window"],
            )
            _kpi_row(agg["kpis"])
            _table(agg["table"], state, lambda: _dashboard.refresh())

        _dashboard()


# ── Filter -> store query translation ─────────────────────────────────────────

def _effective_models(state: dict):
    """AI-review selection -> the *models* list research_store expects.
    Drops the "all" + Ollama sentinels; returns None (= no restriction)
    when nothing concrete is selected."""
    sel = {m for m in state["review"] if m not in ("all", _OLLAMA_SENTINEL)}
    return sorted(sel) if sel else None


def _effective_prop_types(state: dict):
    """Combine the Bet Type (multi) and prediction-Model (prefix) selections
    into a single market allow-list for research_store.aggregate.

    * None              -> no market restriction (show everything).
    * {markets...}      -> only those markets (each still groups separately).
    * {_NO_MATCH}       -> the combination excludes every market -> empty table.
    """
    bets = state["bets"]
    if "all" in bets or not bets:
        bt = None
    else:
        bt = set()
        for b in bets:
            bt |= _BET_TYPE_MARKETS.get(b, {b})

    prefixes = {_PRED_PREFIX[p] for p in state["pred"] if p in _PRED_PREFIX}
    if prefixes:
        universe = set(rs.distinct_prop_types())
        pm = {k for k in universe if any(k.startswith(pf) for pf in prefixes)}
    else:
        pm = None

    if bt is None and pm is None:
        return None
    if bt is None:
        eff = pm
    elif pm is None:
        eff = bt
    else:
        eff = bt & pm
    return eff if eff else {_NO_MATCH}


def _pred_note_active(state: dict) -> bool:
    """True when the user picked an XGBoost/Neural-Net model whose results
    can't be narrowed in the current data -> surface the muted note."""
    return any(p in _PRED_SHOW_ALL for p in state["pred"])


# ── Filter bar (dropdown bar; bottom-sheet on mobile) ────────────────────────

def _filter_bar(state: dict, refresh) -> None:
    """Compact dropdown filter bar (UI redesign, Change 4).

    Desktop: a single horizontal bar of custom dropdowns above the stats
    cards.  Mobile: collapsed behind a "Filters" button that opens a
    bottom-sheet dialog holding the same dropdowns stacked.

    Changing a filter re-renders only the results — the dropdowns keep
    their own open state so multi-selects don't snap shut between picks.
    Reset rebuilds both layouts so their displayed values resync.
    """

    def _set_single(key):
        def _h(e):
            state[key] = e.value
            refresh()
        return _h

    def _set_multi(key):
        def _h(e):
            vals = set(e.value or [])
            if len(vals) > 1:          # "All" is exclusive with concrete picks
                vals.discard("all")
            state[key] = vals or {"all"}
            refresh()
        return _h

    def _field(label: str, stacked: bool):
        col = ui.column().style(
            "gap: 4px; min-width: 0; "
            + ("width: 100%;" if stacked else "flex: 0 1 auto;")
        )
        with col:
            ui.label(label).style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
        return col

    def _dropdowns(stacked: bool) -> None:
        w = "width: 100%;" if stacked else ""
        # MODEL (multi) — with the best-effort note for XGBoost / Neural Net.
        with _field("MODEL", stacked):
            controls.styled_select(
                _PRED_OPTIONS, value=sorted(state["pred"]),
                multiple=True, use_chips=True, placeholder="Model",
                min_width="190px", on_change=_set_multi("pred"),
            ).style(w).tooltip(
                "XGBoost / Neural Net can't be distinguished in current "
                "data — selecting them shows all results."
            )
            if _pred_note_active(state):
                ui.label("Showing all — XGBoost / Neural Net aren't "
                         "distinguishable in current data.").style(
                    f"font-size: 9px; color: {t.TEXT_DIM2}; line-height: 1.3; "
                    f"max-width: 230px;"
                )
        # AI REVIEW (multi)
        with _field("AI REVIEW", stacked):
            controls.styled_select(
                _REVIEW_OPTIONS, value=sorted(state["review"]),
                multiple=True, use_chips=True, placeholder="AI review",
                min_width="190px", on_change=_set_multi("review"),
            ).style(w)
        # SPORT (single)
        with _field("SPORT", stacked):
            controls.styled_select(
                _SPORT_OPTIONS, value=state["sport"],
                min_width="120px", on_change=_set_single("sport"),
            ).style(w)
        # BET TYPE (multi)
        with _field("BET TYPE", stacked):
            controls.styled_select(
                _BET_TYPE_OPTIONS, value=sorted(state["bets"]),
                multiple=True, use_chips=True, placeholder="Bet type",
                min_width="190px", on_change=_set_multi("bets"),
            ).style(w)
        # WINDOW (single)
        with _field("WINDOW", stacked):
            controls.styled_select(
                _WINDOW_OPTIONS, value=state["window"],
                min_width="150px", on_change=_set_single("window"),
            ).style(w)

    def _reset() -> None:
        state.update({"review": {"all"}, "pred": {"all"}, "sport": "all",
                      "bets": {"all"}, "window": "all"})
        refresh()
        _desktop_bar.refresh()
        _sheet_body.refresh()
        sheet.close()

    # ── Desktop: sticky horizontal bar ──────────────────────────────────
    @ui.refreshable
    def _desktop_bar() -> None:                                           # noqa: WPS430
        with ui.row().classes("desktop-only items-end w-full").style(
            f"position: sticky; top: {t.NAVBAR_HEIGHT}; z-index: 20; "
            f"background: {t.BG}; gap: 14px; flex-wrap: wrap; "
            f"padding: 10px 0; border-bottom: 1px solid {t.BORDER};"
        ):
            _dropdowns(stacked=False)
            ui.button("Reset", icon="restart_alt", on_click=_reset).props(
                "no-caps flat dense").style(
                f"color: {t.TEXT_DIM}; font-size: 11px; font-weight: 700; "
                f"align-self: flex-end; margin-bottom: 2px;"
            )

    # ── Mobile: Filters button -> bottom-sheet dialog ───────────────────
    sheet = ui.dialog().props("position=bottom")
    with sheet:
        with ui.column().style(
            f"background: {t.CARD}; border-top-left-radius: {t.RADIUS_LG}; "
            f"border-top-right-radius: {t.RADIUS_LG}; "
            f"border: 1px solid {t.BORDER}; border-bottom: 0; "
            f"padding: {t.SPACE_MD}; gap: 12px; width: 100%; max-width: 100%;"
        ):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("FILTERS").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .6px; "
                    f"color: {t.TEXT};"
                )
                ui.button(icon="close", on_click=sheet.close).props(
                    "flat round dense").style(f"color: {t.TEXT_DIM};")

            @ui.refreshable
            def _sheet_body() -> None:                                    # noqa: WPS430
                with ui.column().style("gap: 12px; width: 100%;"):
                    _dropdowns(stacked=True)
            _sheet_body()

            with ui.row().classes("items-center justify-between w-full").style(
                "gap: 8px;"
            ):
                ui.button("Reset", icon="restart_alt", on_click=_reset).props(
                    "no-caps flat dense").style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; font-weight: 700;"
                )
                ui.button("Done", on_click=sheet.close).props(
                    "no-caps unelevated dense").style(
                    f"background: {t.PRIMARY}; color: #fff; font-size: 12px; "
                    f"font-weight: 800; padding: 6px 18px; "
                    f"border-radius: {t.RADIUS_PILL};"
                )

    with ui.row().classes("mobile-only items-center w-full").style(
        f"position: sticky; top: {t.NAVBAR_HEIGHT}; z-index: 20; "
        f"background: {t.BG}; gap: 8px; padding: 10px 0; "
        f"border-bottom: 1px solid {t.BORDER};"
    ):
        ui.button("Filters", icon="filter_list", on_click=sheet.open).props(
            "no-caps unelevated dense").style(
            f"background: {t.CARD_HI}; color: {t.TEXT}; "
            f"border: 1px solid {t.BORDER}; min-height: 34px; "
            f"padding: 4px 14px; font-size: 12px; font-weight: 800; "
            f"border-radius: {t.RADIUS_PILL};"
        )

    _desktop_bar()


def _short_model(name: str) -> str:
    """Compact pill label for a Groq model id."""
    n = name.split("/")[-1]
    return (n.replace("-versatile", "").replace("-instant", "")
             .replace("llama-", "Llama-").replace("qwen", "Qwen")
             .replace("compound-beta", "Compound-Beta"))


# ── KPI cards ─────────────────────────────────────────────────────────────────

def _kpi_row(kpis: dict) -> None:
    decided = kpis["wins"] + kpis["losses"]
    win_col = (t.POS if kpis["win_pct"] >= 50 else t.NEG) if decided else t.TEXT
    edge_col = (t.POS if kpis["avg_edge"] > 0 else t.NEG) if kpis["picks"] else t.TEXT
    roi_col  = (t.POS if kpis["roi"] > 0 else t.NEG) if decided else t.TEXT

    # flex-wrap gives a 4-across row on desktop and a 2x2 grid on mobile.
    with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
        _kpi_card("WIN RATE",    f"{kpis['win_pct']:.1f}%" if decided else "—", win_col)
        _kpi_card("TOTAL PICKS", str(kpis["picks"]),                            t.TEXT)
        _kpi_card("AVG EDGE",    f"{kpis['avg_edge']:+.1f}%" if kpis["picks"] else "—", edge_col)
        _kpi_card("ROI",         f"{kpis['roi']:+.1f}%" if decided else "—",    roi_col)


def _kpi_card(label: str, value: str, color: str) -> None:
    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 4px; "
        f"flex: 1 1 150px; min-width: 140px; overflow: hidden;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2}; white-space: nowrap;"
        )
        ui.label(value).style(
            f"font-size: 22px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px; white-space: nowrap;"
        )


# ── Leaderboard table ─────────────────────────────────────────────────────────

def _row_tint(win_pct: float, decided: bool) -> str:
    if not decided:
        return t.CARD
    if win_pct > 60:
        return "rgba(16, 185, 129, .12)"      # POS tint
    if win_pct < 40:
        return "rgba(244, 63, 94, .12)"       # NEG tint
    return t.CARD


def _table(table_rows: list[dict], state: dict, refresh) -> None:
    if not table_rows:
        ui.label(
            "No settled research data yet. This dashboard fills in as today's "
            "scored props settle — model + edge are captured going forward."
        ).style(
            f"font-size: 12.5px; color: {t.TEXT_DIM}; font-style: italic; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
            f"text-align: center; width: 100%;"
        )
        return

    key, dirn = state["sort_key"], state["sort_dir"]
    rows_sorted = sorted(
        table_rows,
        key=lambda r: (r.get(key) if isinstance(r.get(key), (int, float))
                       else str(r.get(key) or "").lower()),
        reverse=(dirn == "desc"),
    )
    total_w = sum(int(w[:-2]) for _, _, _, w in _COLUMNS)

    def _set_sort(col_key: str):
        def _h():
            if state["sort_key"] == col_key:
                state["sort_dir"] = "asc" if state["sort_dir"] == "desc" else "desc"
            else:
                state["sort_key"] = col_key
                state["sort_dir"] = "desc"
            refresh()
        return _h

    # Horizontal-scroll wrapper; inner min-width forces the scroll on mobile.
    with ui.element("div").style(
        f"width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; "
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD};"
    ):
        with ui.column().style(f"min-width: {total_w}px; gap: 0;"):
            # Header row
            with ui.row().classes("no-wrap items-stretch").style(
                f"gap: 0; width: 100%; border-bottom: 1px solid {t.BORDER};"
            ):
                for i, (lab, ckey, _num, width) in enumerate(_COLUMNS):
                    arrow = ""
                    if state["sort_key"] == ckey:
                        arrow = " ▼" if state["sort_dir"] == "desc" else " ▲"
                    _hcell(lab + arrow, width, first=(i == 0),
                           active=(state["sort_key"] == ckey), on_click=_set_sort(ckey))

            # Body rows
            for r in rows_sorted:
                decided = (r["wins"] + r["losses"]) > 0
                tint = _row_tint(r["win_pct"], decided)
                with ui.row().classes("no-wrap items-stretch").style(
                    f"gap: 0; width: 100%; background: {tint}; "
                    f"border-bottom: 1px solid {t.BORDER_SOFT};"
                ):
                    _cell(_short_model(r["model"]) if r["model"] != "—" else "—",
                          _COLUMNS[0][3], first=True, bg=tint, bold=True)
                    _cell(_prop_label(r["prop_type"]), _COLUMNS[1][3])
                    _cell(str(r["picks"]),  _COLUMNS[2][3], mono=True)
                    _cell(str(r["wins"]),   _COLUMNS[3][3], mono=True)
                    _cell(f"{r['win_pct']:.1f}%", _COLUMNS[4][3], mono=True,
                          color=(t.POS if r["win_pct"] > 60 else
                                 t.NEG if r["win_pct"] < 40 else t.TEXT))
                    _cell(f"{r['avg_edge']:+.1f}%", _COLUMNS[5][3], mono=True,
                          color=(t.POS if r["avg_edge"] > 0 else t.NEG))
                    _cell(f"{r['roi']:+.1f}%", _COLUMNS[6][3], mono=True,
                          color=(t.POS if r["roi"] > 0 else t.NEG))
                    _cell(("🔥 " + str(r["streak"])) if r["streak"] >= 3 else str(r["streak"]),
                          _COLUMNS[7][3], mono=True)


def _hcell(label: str, width: str, *, first: bool, active: bool, on_click) -> None:
    sticky = (f"position: sticky; left: 0; z-index: 2; background: {t.CARD_HI};"
              if first else f"background: {t.CARD};")
    color = t.PRIMARY_HI if active else t.TEXT_DIM2
    cell = ui.element("div").style(
        f"width: {width}; flex: 0 0 {width}; box-sizing: border-box; "
        f"padding: 9px 10px; cursor: pointer; {sticky} "
        f"font-size: 10px; font-weight: 800; letter-spacing: .4px; color: {color}; "
        f"text-align: {'left' if first else 'right'}; white-space: nowrap;"
    )
    cell.on("click", on_click)
    with cell:
        ui.html(label)


def _cell(text: str, width: str, *, first: bool = False, mono: bool = False,
          bold: bool = False, color: str = None, bg: str = None) -> None:
    color = color or (t.TEXT if (first or bold) else t.TEXT_DIM)
    sticky = (f"position: sticky; left: 0; z-index: 1; "
              f"background: {bg or t.CARD};" if first else "")
    font = "font-family: monospace;" if mono else ""
    weight = "700" if (bold or first) else "500"
    with ui.element("div").style(
        f"width: {width}; flex: 0 0 {width}; box-sizing: border-box; "
        f"padding: 9px 10px; {sticky} {font} font-size: 13px; font-weight: {weight}; "
        f"color: {color}; text-align: {'left' if first else 'right'}; "
        f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
    ):
        ui.html(text)
