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
from components import navbar, bottom_nav
from src import research_store as rs


def _dbg(msg: str) -> None:
    print(f"[RESEARCH] {msg}", flush=True, file=sys.stderr)


# ── Filter option sets ───────────────────────────────────────────────────────

def _model_options() -> list[str]:
    try:
        from src.groq_models import MODELS
        return [m["name"] for m in MODELS.values() if m.get("name")]
    except Exception:                                                     # noqa: BLE001
        return []


_SPORT_LABELS = {"mlb": "MLB", "wnba": "WNBA"}
_PERIOD_OPTS = (("7d", "Last 7 Days"), ("30d", "Last 30 Days"),
                ("season", "This Season"), ("all", "All Time"))

# PROP TYPE dropdown -- two grouped sections.  Each label maps to the set of
# underlying bet_type / market keys it filters on (games use ml/rl/total).
_PROP_GROUPS = (
    ("Games", (("Moneyline", ("ml",)), ("Runline", ("rl",)),
               ("Total (O/U)", ("total",)))),
    ("Props", (("Strikeouts", ("pitcher_strikeouts", "batter_strikeouts")),
               ("Hits", ("batter_hits",)),
               ("Home Runs", ("batter_home_runs",)),
               ("RBI", ("batter_rbis",)),
               ("Points", ("points",)),
               ("Rebounds", ("rebounds",)),
               ("Assists", ("assists",)))),
)
_PROP_LABEL_MARKETS = {lab: set(mks) for _g, opts in _PROP_GROUPS for lab, mks in opts}


def _markets_for(labels: set) -> list | None:
    """Expand selected PROP TYPE labels to the underlying market keys."""
    if not labels:
        return None
    out: set = set()
    for lab in labels:
        out |= _PROP_LABEL_MARKETS.get(lab, set())
    return sorted(out) or None

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
            ui.add_head_html(
                f"<style>.research-opt:hover{{background:{t.CARD_HI};}}</style>"
            )
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
        "ai_models":    set(),    # Groq model names; empty == All
        "sport_models": set(),    # model_type labels;  empty == All
        "sports":       set(),    # "mlb"/"wnba";        empty == All
        "prop_types":   set(),    # PROP TYPE labels;    empty == All
        "period":       "all",    # single-select
        "sort_key":     "win_pct",
        "sort_dir":     "desc",
    }

    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        ui.label("RESEARCH").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label("Model Analytics — pick performance by model, prop type and period.").style(
            f"font-size: 12.5px; color: {t.TEXT_DIM};"
        )

        # Facets (Sport Model / Sport option lists) are derived from the data
        # once; the filter bar is built once so its dropdowns stay open while
        # multi-selecting.  Only the results area re-renders on each change.
        facets = rs.facets(_model_pick_rows(), rs.rows())

        @ui.refreshable
        def _results() -> None:                                           # noqa: WPS430
            ai      = sorted(state["ai_models"]) or None
            sm      = sorted(state["sport_models"]) or None
            sp      = sorted(state["sports"]) or None
            markets = _markets_for(state["prop_types"])
            period  = state["period"]

            research = rs.rows()
            res = rs.dashboard(_model_pick_rows(), research, ai_models=ai,
                               sport_models=sm, sports=sp, markets=markets,
                               period=period)
            table = rs.leaderboard(research, ai_models=ai, sports=sp,
                                   markets=markets, period=period)
            _kpi_row(res["kpis"])
            _table(table, state, _results.refresh)

        _filter_bar(state, facets, _results.refresh)
        _results()


def _model_pick_rows() -> list:
    try:
        from src import model_picks as _mp
        return _mp._all() or []
    except Exception:                                                     # noqa: BLE001
        return []


# ── Filter bar (sticky dropdown buttons) ──────────────────────────────────────

def _filter_bar(state: dict, facets: dict, refresh) -> None:
    sport_model_opts = [(m, m) for m in facets.get("sport_models", [])]
    sport_opts = [(s, _SPORT_LABELS.get(s, s.upper())) for s in facets.get("sports", [])]
    ai_opts = [(n, _short_model(n)) for n in _model_options()]

    with ui.row().classes("items-center no-wrap").style(
        f"position: sticky; top: {t.NAVBAR_HEIGHT}; z-index: 20; "
        f"background: {t.BG}; gap: 8px; width: 100%; padding: 10px 0; "
        f"border-bottom: 1px solid {t.BORDER}; overflow-x: auto; "
        f"-webkit-overflow-scrolling: touch;"
    ):
        _dropdown("AI Model", ai_opts, state["ai_models"], refresh)
        _dropdown("Sport Model", sport_model_opts, state["sport_models"], refresh)
        _dropdown("Sport", sport_opts, state["sports"], refresh)
        _dropdown("Prop Type", None, state["prop_types"], refresh, groups=_PROP_GROUPS)
        _period_dropdown(state, refresh)


def _btn_style(active: bool) -> str:
    return (f"background: {t.CARD}; color: {t.PRIMARY_HI if active else t.TEXT}; "
            f"border: 1px solid {t.PRIMARY if active else t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; font-size: 12px; font-weight: 700; "
            f"padding: 6px 10px; min-height: 0; flex-shrink: 0; white-space: nowrap;")


def _menu_shell():
    return ui.menu().props("auto-close=false").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; box-shadow: 0 8px 24px rgba(0,0,0,.55);"
    )


def _opt_item(text: str, checked: bool, on_click) -> None:
    item = ui.element("div").classes("research-opt").style(
        "display: flex; align-items: center; gap: 8px; padding: 7px 14px; "
        "cursor: pointer; white-space: nowrap; font-size: 12.5px;"
    )
    item.on("click", on_click)
    with item:
        ui.icon("check").style(
            f"font-size: 15px; color: {t.PRIMARY_HI if checked else 'transparent'};"
        )
        ui.label(text).style(f"color: {t.TEXT if checked else t.TEXT_DIM};")


def _dropdown(label: str, options, sel: set, refresh, *, groups=None) -> None:
    """Multiselect dropdown button.  Empty *sel* == All.  Selecting a specific
    option clears All; clearing every option reverts to All."""
    def _lbl() -> str:
        return f"{label} ({len(sel)})" if sel else label

    btn = ui.button(_lbl()).props("no-caps unelevated").style(_btn_style(bool(sel)))

    def _sync_btn() -> None:
        btn.set_text(_lbl())
        btn.style(replace=_btn_style(bool(sel)))

    def _choose(val):
        if val in sel:
            sel.discard(val)
        else:
            sel.add(val)
        body.refresh(); _sync_btn(); refresh()

    def _choose_all():
        sel.clear()
        body.refresh(); _sync_btn(); refresh()

    with btn:
        ui.icon("arrow_drop_down").style(f"font-size: 18px; color: {t.TEXT_DIM};")
        with _menu_shell():
            @ui.refreshable
            def body() -> None:                                           # noqa: WPS430
                with ui.column().style("padding: 4px 0; min-width: 190px; gap: 0;"):
                    _opt_item("All", not sel, _choose_all)
                    if groups:
                        for gname, gopts in groups:
                            _group_header(gname)
                            for lab, _mks in gopts:
                                _opt_item(lab, lab in sel, lambda v=lab: _choose(v))
                    else:
                        for val, disp in options:
                            _opt_item(disp, val in sel, lambda v=val: _choose(v))
            body()


def _period_dropdown(state: dict, refresh) -> None:
    """Single-select TIME PERIOD dropdown."""
    label_of = dict(_PERIOD_OPTS)

    def _lbl() -> str:
        return label_of.get(state["period"], "All Time")

    active = state["period"] != "all"
    btn = ui.button(_lbl()).props("no-caps unelevated").style(_btn_style(active))

    with btn:
        ui.icon("arrow_drop_down").style(f"font-size: 18px; color: {t.TEXT_DIM};")
        menu = ui.menu().style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; box-shadow: 0 8px 24px rgba(0,0,0,.55);"
        )

        def _pick(val):
            state["period"] = val
            btn.set_text(label_of.get(val, "All Time"))
            btn.style(replace=_btn_style(val != "all"))
            menu.close()
            refresh()

        with menu:
            with ui.column().style("padding: 4px 0; min-width: 170px; gap: 0;"):
                for val, disp in _PERIOD_OPTS:
                    _opt_item(disp, state["period"] == val, lambda v=val: _pick(v))


def _group_header(name: str) -> None:
    ui.label(name.upper()).style(
        f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
        f"color: {t.TEXT_DIM2}; padding: 8px 14px 3px;"
    )


def _short_model(name: str) -> str:
    """Compact label for a Groq model id."""
    n = name.split("/")[-1]
    return (n.replace("-versatile", "").replace("-instant", "")
             .replace("llama-", "Llama-").replace("qwen", "Qwen")
             .replace("compound-beta", "Compound-Beta"))


# ── KPI cards ─────────────────────────────────────────────────────────────────

def _kpi_row(kpis: dict) -> None:
    decided = kpis["wins"] + kpis["losses"]
    win_col = (t.POS if kpis["win_pct"] >= 50 else t.NEG) if decided else t.TEXT
    units = kpis.get("units")
    if units is None:
        units_str, units_col = "—", t.TEXT
    else:
        units_str = f"{units:+.2f}u"
        units_col = t.POS if units > 0 else t.NEG if units < 0 else t.TEXT

    # flex-wrap gives a 4-across row on desktop and a 2x2 grid on mobile.
    with ui.row().classes("w-full").style("gap: 8px; flex-wrap: wrap;"):
        _kpi_card("WIN RATE",   f"{kpis['win_pct']:.1f}%" if decided else "—", win_col)
        _kpi_card("TOTAL PICKS", str(kpis["total"]),                           t.TEXT)
        _kpi_card("W-L RECORD",  f"{kpis['wins']} - {kpis['losses']}",         t.TEXT)
        _kpi_card("UNITS",       units_str,                                    units_col)


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
