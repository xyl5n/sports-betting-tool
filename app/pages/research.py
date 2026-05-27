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


_SPORTS = (("all", "All"), ("mlb", "MLB"), ("wnba", "WNBA"))
_WINDOWS = (("7d", "Last 7 Days"), ("30d", "Last 30 Days"),
            ("season", "This Season"), ("all", "All Time"))

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
        "models":   {"all"},          # set of model names, or {"all"}
        "sport":    "all",
        "prop":     "all",
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
            models = None if "all" in state["models"] or not state["models"] \
                else sorted(state["models"])
            agg = rs.aggregate(
                models=models, sport=state["sport"],
                prop_type=state["prop"], window=state["window"],
            )
            _kpi_row(agg["kpis"])
            _table(agg["table"], state, lambda: _dashboard.refresh())

        _dashboard()


# ── Filter bar (sticky) ──────────────────────────────────────────────────────

def _filter_bar(state: dict, refresh) -> None:
    # Sticky under the top nav; pill rows scroll horizontally on mobile.
    with ui.column().style(
        f"position: sticky; top: {t.NAVBAR_HEIGHT}; z-index: 20; "
        f"background: {t.BG}; gap: 6px; width: 100%; "
        f"padding: 8px 0; border-bottom: 1px solid {t.BORDER};"
    ):
        # MODEL (multiselect)
        def _toggle_model(name: str):
            def _h():
                sel = state["models"]
                if name == "all":
                    state["models"] = {"all"}
                else:
                    sel.discard("all")
                    sel.symmetric_difference_update({name})
                    if not sel:
                        state["models"] = {"all"}
                refresh()
            return _h

        model_opts = [("all", "All")] + [(n, _short_model(n)) for n in _model_options()]
        with _pill_row("MODEL"):
            for val, lab in model_opts:
                active = (val == "all" and "all" in state["models"]) or val in state["models"]
                _pill(lab, active, _toggle_model(val))

        # SPORT (single)
        with _pill_row("SPORT"):
            for val, lab in _SPORTS:
                _pill(lab, state["sport"] == val, _single_setter(state, "sport", val, refresh))

        # PROP TYPE (single, populated from the store)
        prop_opts = [("all", "All")] + [(p, _prop_label(p)) for p in rs.distinct_prop_types()]
        with _pill_row("PROP TYPE"):
            for val, lab in prop_opts:
                _pill(lab, state["prop"] == val, _single_setter(state, "prop", val, refresh))

        # TIME WINDOW (single)
        with _pill_row("WINDOW"):
            for val, lab in _WINDOWS:
                _pill(lab, state["window"] == val, _single_setter(state, "window", val, refresh))


def _single_setter(state, key, val, refresh):
    def _h():
        state[key] = val
        refresh()
    return _h


def _pill_row(label: str):
    row = ui.row().classes("items-center no-wrap").style(
        "gap: 6px; width: 100%; overflow-x: auto; "
        "-webkit-overflow-scrolling: touch; padding-bottom: 2px;"
    )
    with row:
        ui.label(label).style(
            f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
            f"color: {t.TEXT_DIM2}; flex-shrink: 0; min-width: 64px;"
        )
    return row


def _pill(label: str, active: bool, on_click) -> None:
    bg  = t.PRIMARY if active else t.CARD_HI
    fg  = "#ffffff" if active else t.TEXT_DIM
    ui.button(label, on_click=on_click).props("no-caps unelevated dense").style(
        f"background: {bg}; color: {fg}; "
        f"font-size: 11px; font-weight: 700; padding: 4px 12px; "
        f"border-radius: {t.RADIUS_PILL}; min-height: 0; flex-shrink: 0; "
        f"white-space: nowrap;"
    )


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
