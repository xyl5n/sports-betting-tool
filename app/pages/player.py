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
from components import navbar, bottom_nav, controls


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
                get_player_gamelog,
                get_today_prop,                  # kept for backward-compat
                get_today_props_for_player,      # multi-prop fetcher
                get_player_today_opponent,
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

        # Fetch EVERY prop the player has today (not just one).  The
        # legacy single-prop helper is kept as a fallback so the hero
        # card's "TODAY: OVER 6.5  72%" pill stays populated when the
        # multi-fetch returns empty (e.g. props_client cache cold).
        today_props_all = get_today_props_for_player(info["name"]) or []
        today_prop      = today_props_all[0] if today_props_all else get_today_prop(info["name"])

        # Pitcher gamelog rows include non-start relief outings; we keep
        # the full filtered list (not just last 10) so the new time-window
        # filter inside the tabs can offer Last 5 / 10 / 20 / Season / H2H.
        if is_pitcher:
            games = [g for g in raw_games if g.get("games_started", 0) > 0]
        else:
            games = list(raw_games)

        # Resolve today's opponent abbreviation (for the H2H filter and
        # the opp-rank chip on each tab).  Use the top prop -- every
        # prop in today_props_all is for the same game so the answer is
        # the same regardless of which we pick.
        opp_abbrev = None
        if today_props_all:
            opp_abbrev = get_player_today_opponent(info["name"], today_props_all[0])
        elif raw_games:
            # Fall back to most-recent game's opponent so the H2H pill
            # isn't blank when no props are posted yet.
            opp_abbrev = (raw_games[-1].get("opp") or "").upper() or None

        _log(f"rendering {info['name']} (id={player_id}, pitcher={is_pitcher}, "
             f"games={len(games)}, props_today={len(today_props_all)}, "
             f"opp={opp_abbrev})")
        _log(f"player_page game dict keys: {list(games[0].keys()) if games else 'no games'}")

        # ── Sections ──────────────────────────────────────────────────────
        _section_hero(info, is_pitcher, today_prop)
        _section_season_stats(season_stats, season_splits, is_pitcher)
        _section_market_tabs(games, is_pitcher, today_props_all, opp_abbrev)
        # Game log stays at the bottom; highlights the strongest prop's
        # stat column by default so the row that connects to the top
        # chart gets emphasized.
        _section_game_log(games, is_pitcher, today_props_all)


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
# (Today's-props overview row was folded into the per-market tabs below --
#  each tab header now carries the market name + line + side pill so the
#  overview is no longer a separate section.)


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


# ── Section: per-market tabs (recent performance vs each prop line) ─────────
#
# Each available prop today gets its own tab.  Inside a tab we render:
#   1. Header pill row -- market chip + side+line pill + confidence + opp
#      rank ("OPP 28th of 30 in opposing K's").
#   2. Summary stat row -- season avg, last-5/10/20 hit rate vs the line,
#      and H2H hit rate vs today's opponent.  Independent of the filter
#      controls below so the comparison is always anchored.
#   3. Time-window filter pills (Last 5 / Last 10 / Last 20 / Season / H2H)
#      controlling which games feed the chart.
#   4. Stat-context filter dropdown (Home/Away/vs opp/order tier/pitches).
#   5. Bar chart with the line drawn as a dashed reference; bars colored
#      green when the value would have hit the side, red when it would
#      have missed.

# Reverse map: market key -> game-log stat key.  Used by the multi-prop
# chart section to figure out which gamelog column corresponds to a
# given prop market.  Pitcher + batter combined so the same lookup
# works for both buckets.
_MARKET_TO_STAT: dict[str, str] = {
    "pitcher_strikeouts":   "K",
    "pitcher_earned_runs":  "ER",
    "pitcher_hits_allowed": "H",
    "pitcher_walks":        "BB",
    "pitcher_outs":         "outs",
    "batter_hits":          "H",
    "batter_total_bases":   "TB",
    "batter_home_runs":     "HR",
    "batter_rbis":          "RBI",
    "batter_runs_scored":   "R",
    "batter_walks":         "BB",
    "batter_strikeouts":    "SO",
    "batter_stolen_bases":  "SB",
}

# Time-window labels offered above the chart.  The set is rendered as a
# horizontal pill toggle; values double as state keys.
_TIME_WINDOWS = ("Last 5", "Last 10", "Last 20", "Season", "H2H")


def _section_market_tabs(
    games: list[dict],
    is_pitcher: bool,
    today_props: list[dict],
    opp_abbrev: Optional[str],
) -> None:
    """Tabs across the top, one per available prop market for the player.

    When today_props is empty (off-day, props_client cache cold) we fall
    back to a single-tab view that just charts the player's primary
    stat with no reference line -- same behaviour as the prior section.
    """
    if not games:
        ui.label("No recent game data available.").style(
            f"color: {t.TEXT_DIM}; font-size: 13px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
            f"text-align: center;"
        )
        return

    # Drop any market we don't have a gamelog stat for so the tab list
    # is clean.  Preserves order (confidence DESC) from upstream.
    plottable_props = [p for p in today_props if _MARKET_TO_STAT.get(p.get("market"))]

    # Section header
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("PROPS vs RECENT PERFORMANCE").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2};"
            )
            if plottable_props:
                ui.label(
                    f"{len(plottable_props)} market"
                    f"{'s' if len(plottable_props) != 1 else ''}"
                ).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 10px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )

        # No props -> single fallback view (no tabs)
        if not plottable_props:
            _market_fallback_view(games, is_pitcher)
            return

        # Tabs (NiceGUI ui.tabs).  Horizontal scroll on narrow screens
        # is handled by Quasar's tabs component automatically when the
        # total tab width exceeds the container.
        from pages.props import _short_market
        tab_refs: list[tuple] = []   # [(tab_obj, prop, label)]
        with ui.tabs().props("dense align=left inline-label").style(
            f"border-bottom: 1px solid {t.BORDER}; "
            f"color: {t.TEXT_DIM}; min-height: 36px;"
        ) as tabs:
            for prop in plottable_props:
                market = prop.get("market", "")
                line   = prop.get("line")
                label  = f"{_short_market(market)} {line}".strip()
                tab_obj = ui.tab(label)
                tab_refs.append((tab_obj, prop, label))

        with ui.tab_panels(tabs, value=tab_refs[0][0]).classes("w-full").style(
            "background: transparent; padding: 0;"
        ):
            for tab_obj, prop, _label in tab_refs:
                with ui.tab_panel(tab_obj).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                    _market_tab_body(games, is_pitcher, prop, opp_abbrev)


def _market_fallback_view(games: list[dict], is_pitcher: bool) -> None:
    """Single chart for the player's primary stat -- used when no props
    are posted today.  No reference line, neutral-colored bars."""
    default_stat = "K" if is_pitcher else "H"
    label = "strikeouts" if is_pitcher else "hits"
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        ui.label(
            f"No props lines posted yet — showing recent {label} only."
        ).style(
            f"font-size: 11px; color: {t.TEXT_DIM2}; font-style: italic;"
        )
        window = games[-10:] if is_pitcher else games[-20:]
        ui.echart(_per_prop_chart_options(
            window, stat_key=default_stat, prop_line=None,
            side="Over", market_label=default_stat,
        )).style("width: 100%; height: 220px;")


def _market_tab_body(
    games: list[dict],
    is_pitcher: bool,
    prop: dict,
    opp_abbrev: Optional[str],
) -> None:
    """Inside one tab: header pill, summary row, filter controls, chart.

    The chart re-renders when filter state changes via ui.refreshable so
    the user can switch windows without reloading the page.
    """
    from pages.props import _short_market
    from src.player_profile_client import (
        get_player_prop_summary,
        get_opp_rank_for_prop,
        opp_rank_label,
    )

    market   = prop.get("market", "")
    side     = (prop.get("recommendation") or prop.get("side") or "Over").strip().title()
    line     = prop.get("line")
    try:
        line_f = float(line)
    except (TypeError, ValueError):
        line_f = None
    conf_pct = int(round((float(prop.get("confidence") or 0)) * 100))
    pv       = prop.get("predicted_value")
    stat_key = _MARKET_TO_STAT.get(market) or ""
    chip_bg  = t.POS if side == "Over" else (t.NEG if side == "Under" else t.CARD_HI)

    # Pre-compute the summary chips (season/L5/L10/L20/H2H vs the line).
    # We pass the already-loaded gamelog so this is a pure in-memory call.
    summary = get_player_prop_summary(
        prop.get("player_name") or "",
        market, line, side,
        opp_abbrev=opp_abbrev,
        is_pitcher=is_pitcher,
        games=games,
    )

    opp_rank      = get_opp_rank_for_prop(opp_abbrev, market) if opp_abbrev else None
    opp_rank_str  = opp_rank_label(opp_rank)

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 12px; "
        f"min-width: 0;"
    ):
        # ── Header pill row ──────────────────────────────────────────────
        with ui.row().classes("items-center w-full").style(
            "gap: 8px; flex-wrap: wrap;"
        ):
            ui.label(_short_market(market).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if line is not None:
                ui.label(f"{side.upper()} {line}").style(
                    f"background: {chip_bg}; color: {t.BG}; "
                    f"font-size: 13px; font-weight: 800; "
                    f"padding: 4px 10px; border-radius: {t.RADIUS_SM};"
                )
            with ui.column().style(
                "gap: 1px; align-items: flex-end; margin-left: auto;"
            ):
                ui.label("CONF").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(f"{conf_pct}%").style(
                    f"font-size: 18px; font-weight: 800; color: {chip_bg}; "
                    f"font-family: monospace;"
                )
            _player_ev_badge(prop.get("ev_pct"))

        # Predicted value + opponent rank in a sub-row beneath the pill
        with ui.row().classes("items-center w-full").style(
            "gap: 10px; flex-wrap: wrap;"
        ):
            if isinstance(pv, (int, float)) and line is not None:
                stat_abbr = _prop_stat_abbr(market)
                try:
                    pv_color = t.POS if (float(pv) - float(line)) > 0 else t.NEG
                except (TypeError, ValueError):
                    pv_color = t.TEXT_DIM
                ui.label(
                    f"Projects {pv:.1f}" + (f" {stat_abbr}" if stat_abbr else "")
                ).style(
                    f"font-size: 11px; color: {pv_color}; "
                    f"font-family: monospace; font-weight: 600;"
                )
            if opp_abbrev:
                ui.label(f"OPP ({opp_abbrev}): {opp_rank_str}").style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; "
                    f"font-family: monospace; "
                    f"background: {t.CARD_HI}; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )

        # ── Summary row: season + L5/L10/L20 + H2H hit rate ──────────────
        _market_summary_row(summary, side, line_f)

        # ── Filter controls (time window + stat context) ─────────────────
        # State + refreshable body so changing a filter just rebuilds
        # the caption + chart without re-fetching gamelog data.  The
        # refreshable's render slot is wherever render_body() is called
        # below -- ui.refreshable handles the clear-and-rerun internally.
        state = {"window": "Last 10", "context": "all"}

        @ui.refreshable
        def render_body() -> None:                                            # noqa: WPS430
            filtered = _apply_window_filter(
                games, state["window"], opp_abbrev,
            )
            filtered = _apply_context_filter(
                filtered, state["context"], is_pitcher,
                opp_abbrev=opp_abbrev,
            )
            if not filtered:
                ui.label(
                    f"No games match this filter ({state['window']}"
                    f" / {state['context']})."
                ).style(
                    f"color: {t.TEXT_DIM2}; font-size: 11px; "
                    f"font-style: italic; padding: 12px;"
                )
                return
            cap_bits = [f"{len(filtered)} game{'s' if len(filtered) != 1 else ''}"]
            if line_f is not None:
                hits = sum(
                    1 for v in (_stat_value(g, stat_key) for g in filtered)
                    if (v > line_f if side == "Over" else v < line_f)
                )
                cap_bits.append(f"{hits}/{len(filtered)} hit {side.lower()}")
            ui.label("  ·  ".join(cap_bits)).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; "
                f"font-family: monospace; letter-spacing: .3px;"
            )
            ui.echart(_per_prop_chart_options(
                filtered, stat_key=stat_key, prop_line=line_f,
                side=side, market_label=_short_market(market),
            )).style("width: 100%; height: 220px;")

        # Time-window pills above the chart
        with ui.row().classes("items-center w-full").style(
            "gap: 6px; flex-wrap: wrap;"
        ):
            controls.field_label("WINDOW")
            controls.pill_toggle(
                list(_TIME_WINDOWS),
                value=state["window"],
                on_change=lambda e: (
                    state.update(window=e.value or "Last 10"),
                    render_body.refresh(),
                ),
                name="window",
            )

        # Chart container -- render_body() populates this slot and
        # ui.refreshable replaces its contents on each refresh().
        with ui.column().classes("w-full").style(
            f"gap: 10px; padding-top: 4px; "
            f"border-top: 1px solid {t.BORDER_SOFT};"
        ):
            render_body()

        # Stat-context filter dropdown beneath the chart
        with ui.row().classes("items-center w-full").style(
            "gap: 6px; flex-wrap: wrap; padding-top: 4px;"
        ):
            controls.field_label("FILTER")
            ctx_options = _stat_context_options(is_pitcher, opp_abbrev)
            controls.styled_select(
                ctx_options, state["context"],
                on_change=lambda e: (
                    state.update(context=e.value or "all"),
                    render_body.refresh(),
                ),
                min_width="220px",
            )


# ── Filter helpers ───────────────────────────────────────────────────────────

def _apply_window_filter(
    games: list[dict],
    window: str,
    opp_abbrev: Optional[str],
) -> list[dict]:
    """Slice *games* by the chosen time window."""
    if window == "Last 5":
        return games[-5:]
    if window == "Last 10":
        return games[-10:]
    if window == "Last 20":
        return games[-20:]
    if window == "Season":
        return list(games)
    if window == "H2H":
        if not opp_abbrev:
            return []
        opp_u = opp_abbrev.upper()
        return [g for g in games if (g.get("opp") or "").upper() == opp_u]
    return games[-10:]


def _stat_context_options(
    is_pitcher: bool,
    opp_abbrev: Optional[str],
) -> dict:
    """Return {value: label} for the stat-context dropdown.

    Options vary by role; the "vs <OPP>" option is only included when
    we know who the player is facing today.  All keys correspond to
    cases handled in `_apply_context_filter`.
    """
    opts: dict[str, str] = {"all": "All games"}
    opts["home"] = "Home games only"
    opts["away"] = "Away games only"
    if opp_abbrev:
        opts[f"opp:{opp_abbrev}"] = f"vs {opp_abbrev} only"
    if is_pitcher:
        opts["high_pitch"] = "High pitch count (≥90)"
        opts["low_pitch"]  = "Low pitch count (<90)"
    else:
        opts["order_top"] = "Top of order (1–2)"
        opts["order_mid"] = "Middle of order (3–5)"
        opts["order_bot"] = "Bottom of order (6–9)"
    return opts


def _apply_context_filter(
    games: list[dict],
    context: str,
    is_pitcher: bool,
    *,
    opp_abbrev: Optional[str] = None,
) -> list[dict]:
    """Apply the secondary stat-context filter chosen by the user."""
    if not context or context == "all":
        return games
    if context == "home":
        return [g for g in games if g.get("is_home")]
    if context == "away":
        return [g for g in games if not g.get("is_home")]
    if context.startswith("opp:"):
        opp = context.split(":", 1)[1].upper()
        return [g for g in games if (g.get("opp") or "").upper() == opp]
    if is_pitcher:
        if context == "high_pitch":
            return [g for g in games if (g.get("pitches_thrown") or 0) >= 90]
        if context == "low_pitch":
            return [g for g in games if 0 < (g.get("pitches_thrown") or 0) < 90]
    else:
        if context == "order_top":
            return [g for g in games if 1 <= (g.get("batting_order") or 0) <= 2]
        if context == "order_mid":
            return [g for g in games if 3 <= (g.get("batting_order") or 0) <= 5]
        if context == "order_bot":
            return [g for g in games if 6 <= (g.get("batting_order") or 0) <= 9]
    return games


# ── Summary chip row ─────────────────────────────────────────────────────────

def _market_summary_row(summary: dict, side: str, line_f: Optional[float]) -> None:
    """5-chip strip across the top of a tab: Season avg, L5/L10/L20 hit
    rate, and H2H hit rate.  Stays anchored regardless of which filter
    the user picks below."""
    cells = []
    # Season average -- shown as a number rather than a hit rate so the
    # user has a baseline raw value to compare against the line.
    sa = summary.get("season_avg")
    cells.append((
        "SEASON",
        "—" if sa is None else f"{sa:.2f}",
        None,
        f"avg over {summary.get('season_games') or 0} games",
    ))
    for w_key, w_label in (
        ("last_5",  "L5"),
        ("last_10", "L10"),
        ("last_20", "L20"),
    ):
        hits  = summary.get(f"{w_key}_hits") or 0
        total = summary.get(f"{w_key}_games") or 0
        if not total:
            cells.append((w_label, "—", None, "no data"))
            continue
        pct  = (hits / total) if total else 0.0
        col  = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append((w_label, f"{hits}/{total}", col, f"{int(round(pct * 100))}%"))

    # H2H
    h2h_hits  = summary.get("h2h_hits") or 0
    h2h_total = summary.get("h2h_games") or 0
    if not h2h_total:
        cells.append(("H2H", "—", None, "no matchups"))
    else:
        pct = (h2h_hits / h2h_total) if h2h_total else 0.0
        col = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append(("H2H", f"{h2h_hits}/{h2h_total}",
                      col, f"{int(round(pct * 100))}%"))

    side_suffix = f"vs {side.upper()} {line_f}" if line_f is not None else ""
    with ui.column().classes("w-full").style(
        f"gap: 4px; padding-top: 4px;"
    ):
        if side_suffix:
            ui.label(f"HIT RATE {side_suffix}").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
        with ui.element("div").style(
            "display: grid; grid-template-columns: repeat(5, 1fr); "
            "gap: 6px; width: 100%;"
        ):
            for label, value, color, sub in cells:
                with ui.column().style(
                    f"background: {t.CARD_HI}; "
                    f"border-radius: {t.RADIUS_SM}; "
                    f"padding: 8px 6px; align-items: center; gap: 2px; "
                    f"min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 13px; font-weight: 800; "
                        f"color: {color or t.TEXT}; font-family: monospace;"
                    )
                    if sub:
                        ui.label(sub).style(
                            f"font-size: 9px; color: {t.TEXT_DIM2}; "
                            f"font-family: monospace;"
                        )


def _per_prop_chart_options(
    games: list[dict],
    *,
    stat_key: str,
    prop_line: Optional[float],
    side: str,
    market_label: str,
) -> dict:
    """ECharts options for a single prop's last-N-games bar chart.

    Each bar's color is driven by whether the actual value cleared the
    line in the betting side's direction (green) or didn't (red).  When
    no line is provided (no-props fallback) every bar gets the neutral
    primary color so the chart still renders informatively.
    """
    dates  = [g.get("date", "")[-5:] for g in games]   # "MM-DD"
    values = [_stat_value(g, stat_key) for g in games]

    # Per-bar items so each bar can carry its own color.  ECharts
    # accepts {value, itemStyle: {color: ...}} dicts in series[].data.
    bar_items: list[dict] = []
    for v in values:
        if prop_line is None:
            col = t.PRIMARY
        else:
            # Over wins -> green when actual > line; Under wins -> green
            # when actual < line.  Equality is a push -> dim color.
            if side == "Under":
                if   v < prop_line: col = t.POS
                elif v > prop_line: col = t.NEG
                else:               col = t.TEXT_DIM2
            else:
                if   v > prop_line: col = t.POS
                elif v < prop_line: col = t.NEG
                else:               col = t.TEXT_DIM2
        bar_items.append({"value": v, "itemStyle": {"color": col}})

    mark_line_data: list[dict] = []
    if prop_line is not None:
        mark_line_data.append({
            "yAxis":     prop_line,
            "label": {
                "formatter": f"Line {prop_line}",
                "position":  "end",
                "color":     t.WARN,
                "fontSize":  10,
            },
            "lineStyle": {"color": t.WARN, "type": "dashed", "width": 2},
        })

    return {
        "backgroundColor": t.BG,
        "grid": {
            "left": "2%", "right": "2%",
            "top":  "10%", "bottom": "3%",
            "containLabel": True,
        },
        "tooltip": {
            "trigger":         "axis",
            "backgroundColor": t.CARD,
            "borderColor":     t.BORDER,
            "textStyle":       {"color": t.TEXT, "fontSize": 12},
            "formatter":       f"{{b}}: {{c}} {stat_key}",
        },
        "xAxis": {
            "type":      "category",
            "data":      dates,
            # When the user switches to a wide window (Season = 30-50
            # games) labels rotate so they stay legible; below ~12 bars
            # we keep them horizontal.
            "axisLabel": {
                "color":    t.TEXT_DIM2,
                "fontSize": 10,
                "rotate":   45 if len(dates) > 12 else 0,
                "interval": "auto",
            },
            "axisLine":  {"lineStyle": {"color": t.BORDER}},
            "axisTick":  {"show": False},
        },
        "yAxis": {
            "type":      "value",
            "axisLabel": {"color": t.TEXT_DIM2, "fontSize": 10},
            "splitLine": {"lineStyle": {"color": t.BORDER_SOFT}},
            "min":       0,
        },
        "series": [{
            "type":      "bar",
            "data":      bar_items,
            "markLine":  ({
                "symbol": ["none", "none"],
                "data":   mark_line_data,
            } if mark_line_data else None),
        }],
    }


def _section_game_log(
    games: list[dict],
    is_pitcher: bool,
    today_props: list[dict],
) -> None:
    """Game log table at the bottom of the page.

    Carries forward from the prior layout but no longer has a stat
    picker -- the per-prop charts above already show each market
    individually.  Highlights whichever stat column corresponds to the
    strongest-confidence prop (or the role's primary stat when no
    props exist) so the row that produced the top chart is visually
    linked to the table.
    """
    if not games:
        return

    if today_props:
        top_market = today_props[0].get("market", "")
        highlighted = _MARKET_TO_STAT.get(top_market) or ("K" if is_pitcher else "H")
    else:
        highlighted = "K" if is_pitcher else "H"

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        ui.label("GAME LOG").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        _game_log_table(games, is_pitcher, highlighted)


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

    # Pre-extract min-width values used in the Date/OPP cells so f-strings
    # do not need backslash-escaped quotes inside expressions — that pattern
    # is a SyntaxError on Python < 3.12.
    _mw_date = col_min_w["Date"]
    _mw_opp  = col_min_w["OPP"]

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
                        f"min-width:{_mw_date};'>{val}</td>"
                    )
                elif col == "OPP":
                    val = _opp_display(g)
                    cells += (
                        f"<td style='font-size:12px; font-family:monospace; "
                        f"padding:6px 10px; text-align:left; color:{t.TEXT_DIM}; "
                        f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
                        f"min-width:{_mw_opp};'>{val}</td>"
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

def _player_ev_badge(ev_pct) -> None:
    """Render a small ``+12.4% EV`` chip in the market-tab header.

    Same visual treatment as the props page's badge so the user
    scans either page with the same color cues -- green positive,
    red negative, grey when the cache hasn't computed EV yet.  EV%
    is read straight off the prop dict (computed once in
    props_scored_cache or player_profile_client).
    """
    try:
        from src.props_ev import ev_color, ev_label
    except Exception:                                                     # noqa: BLE001
        return
    ui.label(ev_label(ev_pct)).style(
        f"background: {t.CARD_HI}; color: {ev_color(ev_pct, t)}; "
        f"font-size: 10.5px; font-weight: 800; letter-spacing: .3px; "
        f"padding: 3px 8px; border-radius: {t.RADIUS_PILL}; "
        f"font-family: monospace; white-space: nowrap; "
        f"align-self: flex-start; margin-left: 4px;"
    )


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
