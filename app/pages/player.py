"""
player.py
=========
MLB player profile page — route /player/mlb/{player_id}.

The player_id path segment can be either a numeric MLB Stats API person ID
(e.g. 592450) or a hyphenated name slug (e.g. "shohei-ohtani") which the
page resolves via the Stats API name-search endpoint.

Sections
--------
1. Back button + page header
2. Hero card — headshot, name, position, team, today's prop chip
3. Today's model prediction card (pitcher/batter prop if available today)
4. Season stats grid — pitcher (ERA/WHIP/K9/BB9/IP/W-L/Home ERA/Away ERA)
                        batter (AVG/OBP/SLG/OPS/HR/RBI/R/SB)
5. Recent performance — stat selector pills + ECharts bar chart +
                        game log table (last 10 starts or last 20 games)

Data sources
------------
MLB Stats API (free, no key required) via src.player_profile_client.
Game logs cached in Supabase + local file, refreshed once per day.
"""
from __future__ import annotations

import sys
from typing import Optional

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


def _log(msg: str) -> None:
    print(f"[player_page] {msg}", flush=True, file=sys.stderr)


# ── Stat selector config ────────────────────────────────────────────────────

_PITCHER_STATS = [
    ("K",    "Strikeouts"),
    ("ER",   "Earned Runs"),
    ("H",    "Hits Allowed"),
    ("BB",   "Walks"),
    ("outs", "Outs"),
]

_BATTER_STATS = [
    ("H",   "Hits"),
    ("TB",  "Total Bases"),
    ("HR",  "Home Runs"),
    ("RBI", "RBIs"),
    ("R",   "Runs"),
    ("BB",  "Walks"),
]

# Market names matching props_client keys (used to look up prop line)
_STAT_TO_MARKET = {
    "K":   "pitcher_strikeouts",
    "ER":  "pitcher_earned_runs",
    "H":   "pitcher_hits_allowed",   # pitcher context
    "BB":  "pitcher_walks",           # pitcher context — overridden for batters below
    "outs":"pitcher_outs",
    # batter context entries override pitcher defaults when is_pitcher=False
}
_BATTER_STAT_TO_MARKET = {
    "H":   "batter_hits",
    "TB":  "batter_total_bases",
    "HR":  "batter_home_runs",
    "RBI": "batter_rbis",
    "R":   "batter_runs_scored",
    "BB":  "batter_walks",
}


def register(backend) -> None:
    @ui.page("/player/mlb/{player_id_slug}")
    def player_page(player_id_slug: str):
        _log(f"player_page ENTER slug={player_id_slug!r}")
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_PROPS)
            _layout(player_id_slug)
            bottom_nav.render(active=t.TAB_PROPS)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            tb_str = _tb.format_exc()
            print(f"[PLAYER PAGE FATAL] {type(exc).__name__}: {exc}\n{tb_str}",
                  flush=True, file=sys.stderr)
            ui.label("Player page failed to render").style(
                f"color: {t.NEG}; font-size: 16px; padding: {t.SPACE_LG};"
            )
            ui.label(f"{type(exc).__name__}: {exc}").style(
                f"color: {t.TEXT_DIM}; font-family: monospace; font-size: 12px; "
                f"padding: 0 {t.SPACE_LG};"
            )


# ── Layout driver ────────────────────────────────────────────────────────────

def _layout(player_id_slug: str) -> None:
    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        # Back button
        with ui.row().classes("items-center").style("gap: 8px;"):
            ui.link("← Props", "/props").style(
                f"color: {t.TEXT_DIM}; font-size: 13px; "
                f"text-decoration: none; font-weight: 500;"
            )

        # Resolve player
        try:
            from src.player_profile_client import (
                resolve_player_id, get_player_info,
                get_season_stats, get_season_splits,
                get_player_gamelog, get_today_prop,
            )
        except ImportError as exc:
            ui.label(f"Player data client unavailable: {exc}").style(
                f"color: {t.NEG}; font-size: 14px;"
            )
            return

        player_id = resolve_player_id(player_id_slug)
        if player_id is None:
            _not_found(player_id_slug)
            return

        info = get_player_info(player_id)
        if not info.get("name"):
            _not_found(player_id_slug)
            return

        is_pitcher = (info.get("position_code") or "") == "1"

        # Fetch all data up-front (cached after first call)
        from src.player_profile_client import _CURRENT_SEASON
        season_stats  = get_season_stats(player_id, is_pitcher=is_pitcher)
        season_splits = get_season_splits(player_id, is_pitcher=is_pitcher)
        raw_games     = get_player_gamelog(player_id, _CURRENT_SEASON, is_pitcher=is_pitcher)
        today_prop    = get_today_prop(info["name"])

        # Filter pitchers to starts only; take last 10/20
        if is_pitcher:
            games = [g for g in raw_games if g.get("games_started", 0) > 0][-10:]
        else:
            games = raw_games[-20:]

        _log(f"rendering {info['name']} (id={player_id}, pitcher={is_pitcher}, "
             f"games={len(games)}, today_prop={today_prop is not None})")
        _log(f"player_page game dict keys: {list(games[0].keys()) if games else 'no games'}")

        # ── Sections ──────────────────────────────────────────────────────
        _section_hero(info, is_pitcher, today_prop)
        if today_prop:
            _section_today_prop(today_prop, is_pitcher)
        _section_season_stats(season_stats, season_splits, is_pitcher)
        _section_recent_performance(games, is_pitcher, today_prop)


# ── Section: hero card ───────────────────────────────────────────────────────

def _section_hero(info: dict, is_pitcher: bool, today_prop: Optional[dict]) -> None:
    player_id = info["id"]
    headshot_url = (
        f"https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
        f"v1/people/{player_id}/headshot/67/current"
    )
    pos_label = info.get("position_name") or info.get("position_code") or "—"
    team_label = info.get("team_abbrev") or info.get("team_name") or "—"
    jersey = info.get("jersey_number") or ""
    throws = info.get("throws") or ""
    bats   = info.get("bats") or ""

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; gap: 20px; "
        f"flex-wrap: nowrap;"
    ):
        # Headshot
        ui.html(
            f'<img src="{headshot_url}" alt="{info["name"]}" '
            f'style="width:80px; height:80px; border-radius:{t.RADIUS_MD}; '
            f'object-fit:cover; flex-shrink:0; '
            f'border:2px solid {t.BORDER};" '
            f'onerror="this.style.display=\'none\'"/>'
        ).style("flex-shrink: 0;")

        # Info column
        with ui.column().style(f"gap: 4px; flex: 1; min-width: 0;"):
            ui.label(info["name"]).style(
                f"font-size: 22px; font-weight: 800; color: {t.TEXT}; "
                f"line-height: 1.2;"
            )
            with ui.row().classes("items-center").style("gap: 8px; flex-wrap: wrap;"):
                ui.label(pos_label).style(
                    f"font-size: 11px; font-weight: 700; color: {t.TEXT_DIM2}; "
                    f"background: {t.CARD_HI}; padding: 2px 8px; "
                    f"border-radius: {t.RADIUS_PILL};"
                )
                ui.label(team_label).style(
                    f"font-size: 13px; font-weight: 700; color: {t.TEXT_DIM};"
                )
                if jersey:
                    ui.label(f"#{jersey}").style(
                        f"font-size: 12px; color: {t.TEXT_DIM2};"
                    )
                if is_pitcher and throws:
                    ui.label(f"Throws: {throws}").style(
                        f"font-size: 11px; color: {t.TEXT_DIM2};"
                    )
                elif not is_pitcher and bats:
                    ui.label(f"Bats: {bats}").style(
                        f"font-size: 11px; color: {t.TEXT_DIM2};"
                    )

            # Today's prop chip (compact preview in hero)
            if today_prop and today_prop.get("recommendation") != "Pass":
                side  = today_prop.get("side") or "Over"
                conf  = int((today_prop.get("confidence") or 0) * 100)
                line  = today_prop.get("line")
                rec   = today_prop.get("recommendation") or side
                chip_bg = t.POS if rec == "Over" else t.NEG
                ui.label(
                    f"TODAY: {rec.upper()} {line}  {conf}%"
                ).style(
                    f"background: {chip_bg}; color: {t.BG}; "
                    f"font-size: 11px; font-weight: 800; letter-spacing: .4px; "
                    f"padding: 3px 10px; border-radius: {t.RADIUS_PILL}; "
                    f"margin-top: 4px;"
                )


# ── Section: today's prop ───────────────────────────────────────────────────

def _section_today_prop(prop: dict, is_pitcher: bool) -> None:
    from pages.props import _short_market, _odds_str  # reuse helpers
    market    = prop.get("market", "")
    rec       = prop.get("recommendation") or "Pass"
    conf_pct  = int((prop.get("confidence") or 0) * 100)
    line      = prop.get("line")
    pv        = prop.get("predicted_value")
    chip_bg   = t.POS if rec == "Over" else (t.NEG if rec == "Under" else t.CARD_HI)
    chip_text = t.BG if rec in ("Over", "Under") else t.TEXT_DIM

    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        ui.label("TODAY'S PREDICTION").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        with ui.row().classes("items-center w-full").style("gap: 12px; flex-wrap: wrap;"):
            # Market chip
            ui.label(_short_market(market).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 10px; font-weight: 700; letter-spacing: .5px; "
                f"padding: 4px 10px; border-radius: {t.RADIUS_PILL};"
            )
            # Line
            ui.label(f"Line: {line}").style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
            # Side pill
            ui.label(f"{rec.upper()} {line}").style(
                f"background: {chip_bg}; color: {chip_text}; "
                f"font-size: 14px; font-weight: 800; padding: 6px 14px; "
                f"border-radius: {t.RADIUS_SM};"
            )
            # Confidence
            with ui.column().style("gap: 1px; align-items: flex-end; margin-left: auto;"):
                ui.label("CONFIDENCE").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(f"{conf_pct}%").style(
                    f"font-size: 20px; font-weight: 800; color: {chip_bg}; "
                    f"font-family: monospace;"
                )
        if pv is not None:
            stat_abbr = _prop_stat_abbr(market)
            pv_label = f"{pv:.1f}" + (f" {stat_abbr}" if stat_abbr else "")
            try:
                margin = (float(pv) - float(line)) if rec == "Over" else (float(line) - float(pv))
                pv_color = t.POS if margin > 1.0 else t.WARN
            except (TypeError, ValueError):
                pv_color = t.TEXT_DIM
            with ui.row().classes("items-center").style("gap: 8px;"):
                ui.label("PREDICTED").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(pv_label).style(
                    f"font-size: 14px; font-weight: 700; color: {pv_color}; "
                    f"font-family: monospace;"
                )


# ── Section: season stats ───────────────────────────────────────────────────

def _section_season_stats(
    stats: dict,
    splits: dict,
    is_pitcher: bool,
) -> None:
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        ui.label("SEASON STATS").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        if is_pitcher:
            cells = [
                ("ERA",      f"{stats.get('era', 0.0):.2f}"),
                ("WHIP",     f"{stats.get('whip', 0.0):.2f}"),
                ("K/9",      f"{stats.get('k9', 0.0):.1f}"),
                ("BB/9",     f"{stats.get('bb9', 0.0):.1f}"),
                ("IP",       f"{stats.get('ip', 0.0):.1f}"),
                ("W-L",      f"{stats.get('wins', 0)}-{stats.get('losses', 0)}"),
                ("HOME ERA", f"{splits.get('home_era', 0.0):.2f}"
                             if splits.get('home_era') else "—"),
                ("AWAY ERA", f"{splits.get('away_era', 0.0):.2f}"
                             if splits.get('away_era') else "—"),
            ]
        else:
            cells = [
                ("AVG",  f"{stats.get('avg', 0.0):.3f}"),
                ("OBP",  f"{stats.get('obp', 0.0):.3f}"),
                ("SLG",  f"{stats.get('slg', 0.0):.3f}"),
                ("OPS",  f"{stats.get('ops', 0.0):.3f}"),
                ("HR",   str(stats.get("hr", 0))),
                ("RBI",  str(stats.get("rbi", 0))),
                ("R",    str(stats.get("runs", 0))),
                ("SB",   str(stats.get("sb", 0))),
            ]

        with ui.element("div").style(
            "display: grid; "
            "grid-template-columns: repeat(4, 1fr); "
            "gap: 8px; width: 100%;"
        ):
            for label, value in cells:
                with ui.column().style(
                    f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: 10px 14px; "
                    f"align-items: center; gap: 2px;"
                ):
                    ui.label(label).style(
                        f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 18px; font-weight: 800; color: {t.TEXT}; "
                        f"font-family: monospace;"
                    )


# ── Section: recent performance ──────────────────────────────────────────────

def _section_recent_performance(
    games: list[dict],
    is_pitcher: bool,
    today_prop: Optional[dict],
) -> None:
    if not games:
        ui.label(
            "No recent game data available."
        ).style(
            f"color: {t.TEXT_DIM}; font-size: 13px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
            f"text-align: center;"
        )
        return

    stat_opts = _PITCHER_STATS if is_pitcher else _BATTER_STATS
    market_map = _STAT_TO_MARKET if is_pitcher else _BATTER_STAT_TO_MARKET
    default_stat = stat_opts[0][0]

    # Find prop line for the current stat (from today's prop if available)
    def _prop_line_for_stat(stat_key: str) -> Optional[float]:
        if not today_prop:
            return None
        market = market_map.get(stat_key)
        if market and today_prop.get("market") == market:
            try:
                return float(today_prop["line"])
            except (TypeError, ValueError):
                return None
        return None

    # Build chart options
    def _chart_options(stat_key: str) -> dict:
        prop_line = _prop_line_for_stat(stat_key)
        dates  = [g.get("date", "")[-5:] for g in games]  # "MM-DD"
        values = [_stat_value(g, stat_key) for g in games]
        bar_color = _bar_color(stat_key, values, prop_line)

        mark_line_data = []
        if prop_line is not None:
            mark_line_data = [{
                "yAxis": prop_line,
                "label": {
                    "formatter": f"Line {prop_line}",
                    "position": "end",
                    "color": t.WARN,
                    "fontSize": 10,
                },
                "lineStyle": {"color": t.WARN, "type": "dashed", "width": 2},
            }]

        return {
            "backgroundColor": t.BG,
            "grid": {
                "left": "2%", "right": "2%",
                "top": "12%", "bottom": "3%",
                "containLabel": True,
            },
            "tooltip": {
                "trigger": "axis",
                "backgroundColor": t.CARD,
                "borderColor": t.BORDER,
                "textStyle": {"color": t.TEXT, "fontSize": 12},
                "formatter": f"{{b}}: {{c}} {stat_key}",
            },
            "xAxis": {
                "type": "category",
                "data": dates,
                "axisLabel": {"color": t.TEXT_DIM2, "fontSize": 10},
                "axisLine": {"lineStyle": {"color": t.BORDER}},
                "axisTick": {"show": False},
            },
            "yAxis": {
                "type": "value",
                "axisLabel": {"color": t.TEXT_DIM2, "fontSize": 10},
                "splitLine": {"lineStyle": {"color": t.BORDER_SOFT}},
                "min": 0,
            },
            "series": [{
                "type": "bar",
                "data": values,
                "itemStyle": {"color": bar_color},
                "markLine": {
                    "symbol": ["none", "none"],
                    "data": mark_line_data,
                } if mark_line_data else None,
            }],
        }

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("RECENT PERFORMANCE").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2};"
            )
            n_label = f"last {len(games)} starts" if is_pitcher else f"last {len(games)} games"
            ui.label(n_label).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 10px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )

        # Stat selector pills
        selected: dict = {"stat": default_stat}
        pill_refs: dict[str, ui.label] = {}
        chart_ref: dict = {}

        def _pill_style(is_active: bool) -> str:
            if is_active:
                return (
                    f"background: {t.PRIMARY}; color: #fff; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 5px 12px; border-radius: {t.RADIUS_PILL}; "
                    f"cursor: pointer;"
                )
            return (
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 600; "
                f"padding: 5px 12px; border-radius: {t.RADIUS_PILL}; "
                f"cursor: pointer;"
            )

        def _on_stat_click(stat_key: str) -> None:
            prev = selected["stat"]
            selected["stat"] = stat_key
            # Update pill styles
            for sk, lbl in pill_refs.items():
                lbl.style(_pill_style(sk == stat_key))
            # Update chart
            chart_el = chart_ref.get("el")
            if chart_el:
                opts = _chart_options(stat_key)
                chart_el.options.clear()
                chart_el.options.update(opts)
                chart_el.update()
            # Refresh table
            tbl_ref = chart_ref.get("tbl")
            if tbl_ref:
                tbl_ref.clear()
                with tbl_ref:
                    _game_log_table(games, is_pitcher, stat_key)

        with ui.row().classes("items-center").style(f"gap: 6px; flex-wrap: wrap;"):
            for sk, slabel in stat_opts:
                lbl = ui.label(slabel).style(
                    _pill_style(sk == default_stat)
                )
                lbl.on("click", lambda e, s=sk: _on_stat_click(s))
                pill_refs[sk] = lbl

        # ECharts bar chart
        chart_el = ui.echart(_chart_options(default_stat)).style(
            "width: 100%; height: 220px;"
        )
        chart_ref["el"] = chart_el

        # Game log table
        tbl_container = ui.column().classes("w-full")
        chart_ref["tbl"] = tbl_container
        with tbl_container:
            _game_log_table(games, is_pitcher, default_stat)


# ── Game log table ───────────────────────────────────────────────────────────

def _game_log_table(
    games: list[dict],
    is_pitcher: bool,
    highlighted_stat: str,
) -> None:
    """Render the game log as a single ui.html() block.

    Layout rationale
    ----------------
    When individual <td>/<th> elements are built with ui.html() inside a
    ui.element("tr") context, NiceGUI inserts a <div> wrapper around each
    cell, producing:

        <tr>
          <div><td>…</td></div>   ← invalid HTML
          <div><td>…</td></div>
        </tr>

    Browsers move those rogue <div>s out of the table, so all cells end up
    collapsed onto one line.  Rendering the entire <table> as one ui.html()
    string avoids this — NiceGUI adds only a single outer <div> around the
    whole block, which is harmless.
    """
    if is_pitcher:
        cols = ["Date", "OPP", "IP", "H", "ER", "BB", "K"]
        col_min_w = {
            "Date": "65px", "OPP": "76px", "IP": "52px",
            "H": "44px", "ER": "44px", "BB": "44px", "K": "44px",
        }
    else:
        cols = ["Date", "OPP", "AB", "H", "HR", "RBI", "R", "BB", "SO", "TB"]
        col_min_w = {
            "Date": "65px", "OPP": "76px", "AB": "44px",
            "H": "44px", "HR": "44px", "RBI": "44px", "R": "44px",
            "BB": "44px", "SO": "44px", "TB": "44px",
        }

    col_stat_map = {
        "K": "K", "ER": "ER", "H": "H", "BB": "BB",
        "IP": "IP", "TB": "TB", "HR": "HR", "RBI": "RBI", "R": "R",
        "SO": "SO", "AB": "AB", "PA": "PA",
    }

    # ── value helpers ────────────────────────────────────────────────────────

    def _safe_num(raw) -> str:
        return "—" if raw is None else str(raw)

    def _safe_ip(game: dict) -> str:
        raw_str = game.get("IP_raw")
        if raw_str is not None:
            s = str(raw_str).strip()
            return s or "—"
        ip_val = game.get("IP")
        if ip_val is None:
            return "—"
        try:
            return f"{float(ip_val):.1f}"
        except (TypeError, ValueError):
            s = str(ip_val).strip()
            return s or "—"

    def _opp_display(game: dict) -> str:
        """'vs/@ TEAM' for the OPP column.

        Falls back to park_team when opp is None/empty, so games where
        the opponent abbreviation is missing still show something useful.
        """
        prefix = "vs" if game.get("is_home") else "@"
        opp = game.get("opp")
        if opp and str(opp).strip():
            return f"{prefix}&nbsp;{str(opp).strip()}"
        park = game.get("park_team")
        if park and str(park).strip():
            return f"{prefix}&nbsp;{str(park).strip()}"
        return "—"

    # ── style helpers ────────────────────────────────────────────────────────

    hl = highlighted_stat.upper()

    th_base = (
        f"font-size:10px; font-weight:800; letter-spacing:.5px; "
        f"color:{t.TEXT_DIM2}; padding:6px 10px; "
        f"border-bottom:1px solid {t.BORDER}; white-space:nowrap;"
    )

    def _td(col: str, extra: str = "") -> str:
        """Return the full inline style string for a data cell."""
        highlighted = col_stat_map.get(col) == hl
        color  = t.PRIMARY if highlighted else t.TEXT
        weight = "800" if highlighted else "400"
        return (
            f"font-size:12px; font-family:monospace; font-weight:{weight}; "
            f"padding:6px 10px; text-align:right; color:{color}; "
            f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
            f"min-width:{col_min_w.get(col, '44px')}; {extra}"
        )

    # ── build HTML ───────────────────────────────────────────────────────────

    # header
    header_cells = ""
    for col in cols:
        align = "left" if col in ("Date", "OPP") else "right"
        mw    = col_min_w.get(col, "44px")
        header_cells += (
            f"<th style='{th_base} text-align:{align}; min-width:{mw};'>{col}</th>"
        )

    # body rows (newest first)
    body_rows = ""
    for g in reversed(games):
        try:
            cells = ""
            for col in cols:
                if col == "Date":
                    raw_date = g.get("date")
                    val = (raw_date[-5:] if raw_date else None) or "—"
                    cells += (
                        f"<td style='font-size:12px; font-family:monospace; "
                        f"padding:6px 10px; text-align:left; color:{t.TEXT_DIM2}; "
                        f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
                        f"min-width:{col_min_w[\"Date\"]};'>{val}</td>"
                    )
                elif col == "OPP":
                    val = _opp_display(g)
                    cells += (
                        f"<td style='font-size:12px; font-family:monospace; "
                        f"padding:6px 10px; text-align:left; color:{t.TEXT_DIM}; "
                        f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
                        f"min-width:{col_min_w[\"OPP\"]};'>{val}</td>"
                    )
                elif col == "IP":
                    cells += f"<td style='{_td(col)}'>{_safe_ip(g)}</td>"
                else:
                    raw_key = col_stat_map.get(col, col)
                    cells += f"<td style='{_td(col)}'>{_safe_num(g.get(raw_key))}</td>"
            body_rows += f"<tr class='game-log-row'>{cells}</tr>"
        except Exception as _row_exc:                                       # noqa: BLE001
            _log(f"game log row skipped ({type(_row_exc).__name__}: {_row_exc})")

    # hover highlight via stylesheet (pseudo-classes can't go in inline style)
    ui.add_css(
        f".game-log-row:hover td {{ background:{t.CARD_HI} !important; }}"
    )

    ui.html(
        f"<div style='overflow-x:auto; width:100%;'>"
        f"<table class='w-full table-fixed' style='width:100%; "
        f"border-collapse:collapse; background:{t.CARD}; "
        f"border-radius:{t.RADIUS_MD}; overflow:hidden;'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body_rows}</tbody>"
        f"</table></div>"
    )


# ── Not found ────────────────────────────────────────────────────────────────

def _not_found(slug: str) -> None:
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL}; "
        f"align-items: center; gap: 8px;"
    ):
        ui.label("Player not found").style(
            f"font-size: 16px; font-weight: 700; color: {t.TEXT};"
        )
        ui.label(f"Could not resolve '{slug}' to an MLB player.").style(
            f"font-size: 13px; color: {t.TEXT_DIM};"
        )
        ui.link("Back to Props", "/props").style(
            f"color: {t.PRIMARY}; font-size: 13px; font-weight: 600;"
        )


# ── Small helpers ─────────────────────────────────────────────────────────────

def _stat_value(game: dict, stat_key: str) -> float:
    """Extract the numeric value for *stat_key* from a game dict.

    Returns 0.0 for any field that is missing or None so the chart never
    crashes on sparse game-log data from the Stats API.
    """
    if stat_key == "outs":
        ip = game.get("IP")
        return float(round((ip if ip is not None else 0.0) * 3))
    raw = game.get(stat_key)
    return float(raw if raw is not None else 0)


def _bar_color(stat_key: str, values: list[float], prop_line: Optional[float]) -> str:
    """Purple normally; green if most bars are over the line, red if under."""
    if prop_line is None or not values:
        return t.PRIMARY
    over_count = sum(1 for v in values if v >= prop_line)
    if over_count >= len(values) * 0.6:
        return t.POS
    if over_count <= len(values) * 0.35:
        return t.NEG
    return t.WARN


def _prop_stat_abbr(market: str) -> str:
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
    }
    return mapping.get(market, "")
