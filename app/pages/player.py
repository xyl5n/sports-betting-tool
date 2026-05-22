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
                get_player_gamelog,
                get_today_prop,                  # kept for backward-compat
                get_today_props_for_player,      # PR: multi-prop fetcher
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

        # PR: fetch EVERY prop the player has today (not just one).  The
        # legacy single-prop helper is kept as a fallback so the hero
        # card's "TODAY: OVER 6.5  72%" pill stays populated when the
        # multi-fetch returns empty (e.g. props_client cache cold).
        today_props_all = get_today_props_for_player(info["name"]) or []
        today_prop      = today_props_all[0] if today_props_all else get_today_prop(info["name"])

        # Filter pitchers to starts only; take last 10/20
        if is_pitcher:
            games = [g for g in raw_games if g.get("games_started", 0) > 0][-10:]
        else:
            games = raw_games[-20:]

        _log(f"rendering {info['name']} (id={player_id}, pitcher={is_pitcher}, "
             f"games={len(games)}, props_today={len(today_props_all)})")
        _log(f"player_page game dict keys: {list(games[0].keys()) if games else 'no games'}")

        # ── Sections ──────────────────────────────────────────────────────
        _section_hero(info, is_pitcher, today_prop)
        if today_props_all:
            _section_today_props_overview(today_props_all, is_pitcher)
        _section_season_stats(season_stats, season_splits, is_pitcher)
        _section_props_charts(games, is_pitcher, today_props_all)
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

def _section_today_props_overview(props: list[dict], is_pitcher: bool) -> None:
    """Compact overview row: one small card per today's prop.

    Layout sits between the hero and season-stats sections so the user
    sees every prop the model has on the player BEFORE scrolling to the
    detailed charts.  Each card is sized to flex-shrink to a half-row
    on mobile and a third on desktop without breaking the grid.

    Sorted by confidence DESC (handled in get_today_props_for_player).
    """
    from pages.props import _short_market   # reuse market-name humanizer

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("TODAY'S PROPS").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(f"{len(props)} market{'s' if len(props) != 1 else ''}").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 10px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )

        # Two-up on desktop, single column on mobile.  Reuses the
        # existing .game-grid responsive grid from theme.page_head_css
        # so the breakpoint matches the rest of the app.
        with ui.element("div").classes("game-grid w-full"):
            for prop in props:
                _today_prop_minicard(prop, _short_market)


def _today_prop_minicard(prop: dict, short_market_fn) -> None:
    """One prop in the TODAY'S PROPS overview row.  Compact -- chip,
    line, side, confidence, predicted_value.  Designed so multiple
    fit on a mobile screen without scroll within the card itself."""
    market    = prop.get("market", "")
    rec       = (prop.get("recommendation") or "Pass").strip().title()
    conf_pct  = int(round((float(prop.get("confidence") or 0)) * 100))
    line      = prop.get("line")
    pv        = prop.get("predicted_value")
    chip_bg   = t.POS if rec == "Over" else (t.NEG if rec == "Under" else t.CARD_HI)
    chip_text = t.BG if rec in ("Over", "Under") else t.TEXT_DIM

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; gap: 8px; "
        f"min-width: 0;"
    ):
        # Market chip + confidence on a single line
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(short_market_fn(market).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            ui.label(f"{conf_pct}%").style(
                f"font-size: 14px; font-weight: 800; color: {chip_bg}; "
                f"font-family: monospace; margin-left: auto;"
            )

        # Big side pill + line
        ui.label(f"{rec.upper()} {line}").style(
            f"background: {chip_bg}; color: {chip_text}; "
            f"font-size: 14px; font-weight: 800; "
            f"padding: 6px 12px; border-radius: {t.RADIUS_SM}; "
            f"align-self: flex-start;"
        )

        # Predicted value footnote (small)
        if isinstance(pv, (int, float)) and line is not None:
            stat_abbr = _prop_stat_abbr(market)
            pv_label = f"projects {pv:.1f}" + (f" {stat_abbr}" if stat_abbr else "")
            try:
                margin = float(pv) - float(line)
                pv_color = t.POS if margin > 0 else t.NEG
            except (TypeError, ValueError):
                pv_color = t.TEXT_DIM
            ui.label(pv_label).style(
                f"font-size: 11px; color: {pv_color}; "
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

# ── Section: per-prop charts ─────────────────────────────────────────────────

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
}


def _section_props_charts(
    games: list[dict],
    is_pitcher: bool,
    today_props: list[dict],
) -> None:
    """Per-prop chart grid.

    One card per available prop -- each card shows the last N games'
    actual value as a bar, with a dashed horizontal reference line at
    today's book line.  Bars are colored green when the player hit the
    over and red when they hit the under, so the hit-rate vs the line
    is readable at a glance.

    When today has zero scored props (off-day, props haven't been
    fetched yet) we fall back to the legacy single-chart view so the
    page isn't blank -- it just renders the player's primary stat
    without a reference line.
    """
    if not games:
        ui.label("No recent game data available.").style(
            f"color: {t.TEXT_DIM}; font-size: 13px; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
            f"text-align: center;"
        )
        return

    n_games_label = (
        f"last {len(games)} starts" if is_pitcher else f"last {len(games)} games"
    )

    # Section header
    with ui.row().classes("items-center w-full").style("gap: 8px;"):
        ui.label("RECENT PERFORMANCE vs LINE").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(n_games_label).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 10px; font-weight: 700; "
            f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
        )

    # No-props fallback: render a single chart for the default stat with
    # no reference line so the page still shows the player's recent
    # output.  Picks the primary stat for the role (K for pitchers,
    # H for batters).
    if not today_props:
        default_stat = "K" if is_pitcher else "H"
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
        ):
            ui.label(
                f"No props lines posted yet — showing recent "
                f"{'strikeouts' if is_pitcher else 'hits'} only."
            ).style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; font-style: italic;"
            )
            ui.echart(_per_prop_chart_options(
                games, stat_key=default_stat, prop_line=None,
                side="Over", market_label=default_stat,
            )).style("width: 100%; height: 200px;")
        return

    # Card-per-prop grid.  Reuses .game-grid so it's 2-up on desktop and
    # single-column on mobile, matching the rest of the slate cards.
    with ui.element("div").classes("game-grid w-full"):
        for prop in today_props:
            _prop_chart_card(games, is_pitcher, prop)


def _prop_chart_card(games: list[dict], is_pitcher: bool, prop: dict) -> None:
    """Render one card containing a single prop's chart + summary chips.

    Header shows the market name + line + side + confidence so the user
    sees the bet context above the bars.  The bars themselves carry the
    hit/miss color signal so the over/under rate against the line is
    obvious at a glance.
    """
    from pages.props import _short_market

    market    = prop.get("market", "")
    side      = (prop.get("recommendation") or prop.get("side") or "Over").strip().title()
    line      = prop.get("line")
    try:
        line_f = float(line)
    except (TypeError, ValueError):
        line_f = None
    conf_pct  = int(round((float(prop.get("confidence") or 0)) * 100))
    chip_bg   = t.POS if side == "Over" else (t.NEG if side == "Under" else t.CARD_HI)
    chip_text = t.BG if side in ("Over", "Under") else t.TEXT_DIM
    stat_key  = _MARKET_TO_STAT.get(market)
    if stat_key is None:
        return  # market we can't visualize from gamelog — silently skip

    # Hit/miss tally for the header pill ("hit over 7/10")
    values = [_stat_value(g, stat_key) for g in games]
    if line_f is not None:
        if side == "Over":
            hits = sum(1 for v in values if v > line_f)
        else:
            hits = sum(1 for v in values if v < line_f)
        rate_label = f"{hits}/{len(values)}  hit {side.lower()}"
    else:
        rate_label = ""

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px; "
        f"min-width: 0;"
    ):
        # Header row: market + side pill + confidence
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(_short_market(market).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if line is not None:
                ui.label(f"{side.upper()} {line}").style(
                    f"background: {chip_bg}; color: {chip_text}; "
                    f"font-size: 12px; font-weight: 800; "
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
                    f"font-size: 16px; font-weight: 800; color: {chip_bg}; "
                    f"font-family: monospace;"
                )

        # Sub-row: hit rate against line ("hit over 7/10")
        if rate_label:
            ui.label(rate_label).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; "
                f"font-family: monospace; letter-spacing: .3px;"
            )

        # The chart itself
        ui.echart(_per_prop_chart_options(
            games, stat_key=stat_key, prop_line=line_f,
            side=side, market_label=_short_market(market),
        )).style("width: 100%; height: 200px;")


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
            "axisLabel": {"color": t.TEXT_DIM2, "fontSize": 10},
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
