"""
player.py
=========
MLB player profile page — route /player/mlb/{player_id}.

Layout (mobile-first):

    ┌─────────────────────────────────────────────┐
    │ ← Props                                     │
    │ TABS  [ K  WALKS  OUTS  ER  HITS-ALLOWED ]  │  green underline
    │ ┌─────────────────────────────────────────┐ │
    │ │ Player header                           │ │
    │ │  [HS] Name pos·team           Grade     │ │
    │ │       • OVER 7.5              ┌────┐   │ │
    │ │                                │ B+ │   │ │
    │ │                                └────┘   │ │
    │ ├─────────────────────────────────────────┤ │
    │ │  OPP    GAME TIME                       │ │
    │ │  @NYY   7:05 PM ET                      │ │
    │ ├─────────────────────────────────────────┤ │
    │ │  [AVG 7.3]   <-- red/green vs line      │ │
    │ │  ┌──── bar chart ──────────────┐        │ │
    │ │  │ ▇ ▇ ▇  ─ line ─  ▇ ▇       │        │ │
    │ │  └────────────────────────────┘        │ │
    │ │  [2026][H2H][L5][L10][L20]  [filter]   │ │
    │ │   65%  50%  80% 70% 65%                 │ │
    │ ├─────────────────────────────────────────┤ │
    │ │  MATCHUP INSIGHTS                       │ │
    │ │  [LINEUP]   Hitting 3rd in last 5 games │ │
    │ │  [FORM]     L5 avg 1.4 (line 1.5)       │ │
    │ │  [APPROACH] Season OPS .812 vs RHP      │ │
    │ │  [team matchup card with donut + cols]  │ │
    │ ├─────────────────────────────────────────┤ │
    │ │  RECENT TRENDS                          │ │
    │ │  ┌── LAST 5 ──┐  ┌── LAST 10 ──┐       │ │
    │ │  │ ▲ +0.4    │  │ ▼ -0.2     │        │ │
    │ │  │ mini-chart│  │ mini-chart │        │ │
    │ │  └───────────┘  └────────────┘         │ │
    │ ├─────────────────────────────────────────┤ │
    │ │  GAME LOG (table)                       │ │
    │ └─────────────────────────────────────────┘ │

Active tab swaps the entire per-market block (header through trends);
the game-log table at the bottom is shared across markets.

Data sources
------------
* ``src.player_profile_client`` — all MLB Stats API hits + the
  scored-cache reader used to source today's props.  This page
  NEVER calls ``predict()``; it reads the cache populated by the
  scheduler (see ``props_scored_cache.score_today_props``).
* Game logs are cached in Supabase + local file, refreshed once
  per calendar day.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, controls


def _log(msg: str) -> None:
    print(f"[player_page] {msg}", flush=True, file=sys.stderr)


# ── Constants ───────────────────────────────────────────────────────────────

# Order matches the spec: 2026 | H2H | L5 | L10 | L20.  Internally we
# still call the season window "Season" so it picks up the existing
# game-log helper; the display label is overridden in the pills.
_TIME_WINDOWS: tuple[tuple[str, str], ...] = (
    ("Season",  "2026"),
    ("H2H",     "H2H"),
    ("Last 5",  "L5"),
    ("Last 10", "L10"),
    ("Last 20", "L20"),
)

# Per-market gamelog stat key.  Same map as
# ``src.player_profile_client._MARKET_TO_GAMELOG_STAT`` but kept local
# so the page can render even when the client's mapping is stale.
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

# Short tab labels per market.  Compact so several fit on a phone
# width without the q-tabs horizontal scroller engaging too early.
_MARKET_TAB_LABEL: dict[str, str] = {
    "pitcher_strikeouts":   "K",
    "pitcher_outs":         "OUTS",
    "pitcher_earned_runs":  "ER",
    "pitcher_hits_allowed": "HITS",
    "pitcher_walks":        "BB",
    "batter_hits":          "HITS",
    "batter_total_bases":   "TB",
    "batter_home_runs":     "HR",
    "batter_rbis":          "RBI",
    "batter_runs_scored":   "R",
    "batter_walks":         "BB",
    "batter_strikeouts":    "K",
    "batter_stolen_bases":  "SB",
}


# ── Page registration ──────────────────────────────────────────────────────

def register(backend) -> None:
    @ui.page("/player/mlb/{player_id_slug}")
    def player_page(player_id_slug: str):
        _log(f"player_page ENTER slug={player_id_slug!r}")
        try:
            ui.add_head_html(t.page_head_css())
            ui.add_head_html(_local_css())
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


# ── Page-scoped CSS ─────────────────────────────────────────────────────────

def _local_css() -> str:
    """Player-page-only CSS overrides.  Mounted via ui.add_head_html so
    the new layout's specific bits (green tab indicator, scrollable tab
    strip) don't have to reach into the global theme.css.
    """
    return f"""
    <style>
      /* Tabs at the top of /player/<id> get a green indicator (per
         spec) rather than the purple-gradient one the global theme
         paints.  Scope via .player-market-tabs so the rule doesn't
         leak to /mybets etc. */
      .player-market-tabs .q-tab__indicator {{
        background: {t.POS} !important;
        height: 3px !important;
        box-shadow: 0 0 8px rgba(16, 185, 129, 0.55) !important;
      }}
      .player-market-tabs .q-tab--active {{
        color: {t.POS} !important;
      }}
      /* Force horizontal scroll instead of squashing when there are
         more tabs than fit on a phone width. */
      .player-market-tabs .q-tabs__content {{
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
      }}
    </style>
    """


# ── Layout driver ────────────────────────────────────────────────────────────

def _layout(player_id_slug: str) -> None:
    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        # Back link
        with ui.row().classes("items-center").style("gap: 8px;"):
            ui.link("← Props", "/props").style(
                f"color: {t.TEXT_DIM}; font-size: 13px; "
                f"text-decoration: none; font-weight: 500;"
            )

        # Resolve player + fetch everything (all cached helpers)
        try:
            from src.player_profile_client import (
                resolve_player_id, get_player_info,
                get_player_gamelog,
                get_today_prop,
                get_today_props_for_player,
                get_player_today_opponent,
                _CURRENT_SEASON,
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
        raw_games  = get_player_gamelog(player_id, _CURRENT_SEASON, is_pitcher=is_pitcher) or []
        if is_pitcher:
            games = [g for g in raw_games if g.get("games_started", 0) > 0]
        else:
            games = list(raw_games)

        today_props_all = get_today_props_for_player(info["name"]) or []
        today_prop      = today_props_all[0] if today_props_all else get_today_prop(info["name"])

        opp_abbrev: Optional[str] = None
        if today_props_all:
            opp_abbrev = get_player_today_opponent(info["name"], today_props_all[0])
        if not opp_abbrev and raw_games:
            opp_abbrev = (raw_games[-1].get("opp") or "").upper() or None

        _log(f"render {info['name']} (id={player_id}, pitcher={is_pitcher}, "
             f"props={len(today_props_all)}, opp={opp_abbrev}, games={len(games)})")

        # No-props fallback: render the basic header + game log only.
        if not today_props_all:
            _section_player_header(info, today_prop, opp_abbrev, prop=None, grade=None)
            _empty_state_message(info, raw_games, is_pitcher)
            _section_game_log(games, is_pitcher, [])
            return

        # ── Market tabs at the top, per spec ─────────────────────────────
        tab_refs: list[tuple] = []
        with ui.tabs().props("dense align=left inline-label").classes(
            "player-market-tabs w-full"
        ).style(
            f"border-bottom: 1px solid {t.BORDER}; min-height: 40px;"
        ) as tabs:
            for prop in today_props_all:
                market = prop.get("market", "")
                label  = _MARKET_TAB_LABEL.get(market, market.upper())
                tab_refs.append((ui.tab(label), prop))

        with ui.tab_panels(tabs, value=tab_refs[0][0]).classes("w-full").style(
            f"background: transparent; padding: 0;"
        ):
            for tab_obj, prop in tab_refs:
                with ui.tab_panel(tab_obj).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                    _render_market_view(info, games, is_pitcher, prop, opp_abbrev)

        # Game log table at the very bottom -- shared across markets.
        _section_game_log(games, is_pitcher, today_props_all)


# ── Per-market view (everything below the tabs) ─────────────────────────────

def _render_market_view(
    info: dict,
    games: list[dict],
    is_pitcher: bool,
    prop: dict,
    opp_abbrev: Optional[str],
) -> None:
    """Renders one complete per-market view: player header, info row,
    avg badge, chart block, matchup insights, recent trends."""
    market = prop.get("market", "")
    try:
        line_f: Optional[float] = float(prop.get("line"))
    except (TypeError, ValueError):
        line_f = None

    grade = _letter_grade_for_prop(prop)

    # Pull the player+market+line+side performance summary now so every
    # section below can read from it without duplicating gamelog calls.
    summary = _player_prop_summary_safe(
        info["name"], market, prop.get("line"),
        prop.get("side") or "Over",
        opp_abbrev=opp_abbrev, is_pitcher=is_pitcher, games=games,
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_MD};"):
        _section_player_header(info, prop, opp_abbrev, prop=prop, grade=grade)
        _section_info_row(opp_abbrev, prop)
        _section_chart_block(
            games, is_pitcher, prop, market, line_f, summary, opp_abbrev,
        )
        _section_matchup_insights(
            info, prop, market, line_f, summary, opp_abbrev, is_pitcher, games,
        )
        _section_matchup_tabs(
            info, prop, market, line_f, summary, opp_abbrev, is_pitcher,
        )
        _section_recent_trends(games, is_pitcher, market, line_f, summary)


# ── Section: player header (per-market) ─────────────────────────────────────

def _section_player_header(
    info: dict,
    today_prop: Optional[dict],   # noqa: ARG001 (kept for compat with no-props path)
    opp_abbrev: Optional[str],    # noqa: ARG001
    *,
    prop: Optional[dict],
    grade: Optional[tuple[str, str]],
) -> None:
    """Card with headshot left, name+pos+team+line center, grade right."""
    player_id = info["id"]
    headshot_url = (
        f"https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
        f"v1/people/{player_id}/headshot/67/current"
    )
    name        = info.get("name") or "—"
    position    = info.get("position_name") or info.get("position_code") or ""
    team_abbrev = info.get("team_abbrev") or info.get("team_name") or ""

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_MD} {t.SPACE_LG}; "
        f"gap: 14px; flex-wrap: nowrap;"
    ):
        ui.html(
            f'<img src="{headshot_url}" alt="{name}" '
            f'style="width:64px; height:64px; border-radius:50%; '
            f'object-fit:cover; background:{t.CARD_HI}; '
            f'border:2px solid {t.BORDER}; flex-shrink:0;" '
            f'onerror="this.style.background=\'{t.CARD_HI}\'; this.style.opacity=\'.3\';"/>'
        ).style("flex-shrink: 0;")

        with ui.column().style("gap: 3px; flex: 1; min-width: 0;"):
            ui.label(name).style(
                f"font-size: 18px; font-weight: 800; color: {t.TEXT}; "
                f"line-height: 1.15; white-space: nowrap; overflow: hidden; "
                f"text-overflow: ellipsis;"
            )
            ui.label(f"{position}  •  {team_abbrev}").style(
                f"font-size: 11.5px; font-weight: 600; color: {t.TEXT_DIM}; "
                f"letter-spacing: .2px;"
            )

            # Prop line chip: green dot + SIDE LINE pill.
            if prop is not None and prop.get("recommendation") != "Pass":
                side    = (prop.get("side") or "Over").strip().title()
                line    = prop.get("line")
                conf_pct = int(round(float(prop.get("confidence") or 0.0) * 100))
                with ui.row().classes("items-center").style(
                    "gap: 6px; padding-top: 4px;"
                ):
                    ui.html(
                        f'<span style="display:inline-block; width:8px; '
                        f'height:8px; border-radius:50%; background:{t.POS}; '
                        f'box-shadow:0 0 8px rgba(16,185,129,.55);"></span>'
                    )
                    ui.label(f"{side.upper()} {line}").style(
                        f"font-size: 13px; font-weight: 800; color: {t.TEXT}; "
                        f"letter-spacing: .3px;"
                    )
                    ui.label(f"{conf_pct}%").style(
                        f"font-size: 11.5px; font-weight: 700; "
                        f"color: {t.TEXT_DIM}; font-family: monospace;"
                    )

        if grade is not None:
            grade_letter, grade_color = grade
            ui.html(
                _donut_gauge_html(
                    progress=_grade_progress(grade_letter),
                    color=grade_color,
                    center=grade_letter,
                    size=68,
                    stroke=6,
                )
            ).style("flex-shrink: 0;")


# ── Section: info row (OPP + GAME TIME) ─────────────────────────────────────

def _section_info_row(opp_abbrev: Optional[str], prop: Optional[dict]) -> None:
    opp_text  = f"@ {opp_abbrev}" if opp_abbrev else "—"
    time_text = _format_game_time(prop.get("commence_time") if prop else None) or "—"
    with ui.row().classes("items-stretch w-full").style(
        f"gap: {t.SPACE_SM}; flex-wrap: nowrap;"
    ):
        for caption, value in (("OPP", opp_text), ("GAME TIME", time_text)):
            with ui.column().classes("flex-grow").style(
                f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: 10px 14px; "
                f"gap: 2px; min-width: 0; flex: 1 1 0;"
            ):
                ui.label(caption).style(
                    f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(value).style(
                    f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
                    f"font-family: monospace;"
                )


# ── Section: chart block (avg badge, chart, time pills, filter icon) ────────

def _section_chart_block(
    games: list[dict],
    is_pitcher: bool,
    prop: dict,
    market: str,
    line_f: Optional[float],
    summary: dict,
    opp_abbrev: Optional[str],
) -> None:
    """The headline visualisation block.

    Contains the AVG pill, the bar chart, the time-window pill toggle
    with per-pill hit-rate labels underneath, and the more-filters
    icon.  Closure-state ``state`` drives the refreshable chart and
    caption when the user flips between windows or stat-context
    filters.
    """
    stat_key = _MARKET_TO_STAT.get(market) or ("K" if is_pitcher else "H")
    side     = (prop.get("side") or "Over").strip().title()

    state = {"window": "Last 10", "context": "all"}

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        # AVG badge -- season avg vs the line, coloured red/green.
        _section_avg_badge(summary.get("season_avg"), line_f, stat_key)

        @ui.refreshable
        def render_chart() -> None:                                       # noqa: WPS430
            filtered = _apply_window_filter(games, state["window"], opp_abbrev)
            filtered = _apply_context_filter(
                filtered, state["context"], is_pitcher, opp_abbrev=opp_abbrev,
            )
            if not filtered:
                ui.label(
                    f"No games match the current filter "
                    f"({state['window']} / {state['context']})."
                ).style(
                    f"color: {t.TEXT_DIM2}; font-size: 11.5px; "
                    f"font-style: italic; padding: 24px; text-align: center;"
                )
                return
            ui.echart(_per_prop_chart_options(
                filtered, stat_key=stat_key, prop_line=line_f, side=side,
            )).style("width: 100%; height: 250px;")

        with ui.column().classes("w-full").style("gap: 6px;"):
            render_chart()

        _render_time_pills_with_rates(
            summary, line_f, side, state,
            on_change=render_chart.refresh,
            ctx_options=_stat_context_options(is_pitcher, opp_abbrev),
        )


def _section_avg_badge(
    season_avg: Optional[float],
    line_f: Optional[float],
    stat_key: str,
) -> None:
    """Small pill: 'AVG 1.4 hits', coloured against the line."""
    if season_avg is None:
        return
    if line_f is None:
        color = t.TEXT_DIM
    elif season_avg >= line_f:
        color = t.POS
    else:
        color = t.NEG
    stat_label = stat_key if stat_key else ""
    ui.label(f"AVG {season_avg:.2f} {stat_label}".strip()).style(
        f"background: rgba(16, 185, 129, .08); color: {color}; "
        f"font-size: 11.5px; font-weight: 800; letter-spacing: .4px; "
        f"padding: 4px 10px; border-radius: {t.RADIUS_PILL}; "
        f"font-family: monospace; align-self: flex-start; "
        f"border: 1px solid {color};"
    )


def _render_time_pills_with_rates(
    summary: dict,
    line_f: Optional[float],
    side: str,
    state: dict,
    *,
    on_change,
    ctx_options: dict,
) -> None:
    """Custom pill row -- one pill per window with its hit-rate %
    rendered directly below.  Filter icon at the far right opens an
    additional filters dialog.

    We hand-roll the pills (rather than using ``controls.pill_toggle``)
    because the spec wants the hit-rate label flush under each pill,
    which a single segmented control can't deliver.
    """
    # Pre-compute hit rate per window for the labels.
    def _rate(win_key: str) -> Optional[float]:
        if win_key == "Last 5":
            hits, total = summary.get("last_5_hits") or 0, summary.get("last_5_games") or 0
        elif win_key == "Last 10":
            hits, total = summary.get("last_10_hits") or 0, summary.get("last_10_games") or 0
        elif win_key == "Last 20":
            hits, total = summary.get("last_20_hits") or 0, summary.get("last_20_games") or 0
        elif win_key == "Season":
            hits, total = summary.get("season_hits") or 0, summary.get("season_games") or 0
        elif win_key == "H2H":
            hits, total = summary.get("h2h_hits") or 0, summary.get("h2h_games") or 0
        else:
            return None
        if not total:
            return None
        return hits / total

    @ui.refreshable
    def render_pills() -> None:                                           # noqa: WPS430
        with ui.row().classes("items-stretch w-full").style(
            "gap: 6px; flex-wrap: nowrap; padding-top: 6px;"
        ):
            for win_key, label in _TIME_WINDOWS:
                is_active = state["window"] == win_key
                rate = _rate(win_key)
                _pill_with_rate(label, is_active, rate, win_key, state,
                                on_change=on_change, refresh=render_pills.refresh)
            # Spacer + filter icon
            ui.element("div").style("flex: 1;")
            _more_filters_button(state, ctx_options, on_change=on_change)

    render_pills()


def _pill_with_rate(
    label: str,
    is_active: bool,
    rate: Optional[float],
    win_key: str,
    state: dict,
    *,
    on_change,
    refresh,
) -> None:
    """Single window pill with its hit-rate caption below."""
    if is_active:
        bg, fg, border = t.POS, t.BG, t.POS
    else:
        bg, fg, border = "transparent", t.TEXT_DIM, t.BORDER
    if rate is None:
        rate_color = t.TEXT_DIM2
        rate_text  = "—"
    elif rate >= 0.5:
        rate_color = t.POS
        rate_text  = f"{int(round(rate * 100))}%"
    else:
        rate_color = t.NEG
        rate_text  = f"{int(round(rate * 100))}%"

    def _on_click():
        state["window"] = win_key
        on_change()
        refresh()

    with ui.column().style(
        "gap: 4px; align-items: center; flex: 1 1 0; min-width: 0;"
    ):
        ui.button(label, on_click=_on_click).props(
            "no-caps unelevated dense"
        ).style(
            f"background: {bg} !important; color: {fg} !important; "
            f"border: 1px solid {border}; "
            f"min-height: 28px; padding: 4px 0; width: 100%; "
            f"font-size: 11px; font-weight: 800; letter-spacing: .35px; "
            f"border-radius: {t.RADIUS_PILL}; "
            f"box-shadow: " + (
                f"0 0 12px rgba(16, 185, 129, .35) !important;"
                if is_active else "none !important;"
            )
        )
        ui.label(rate_text).style(
            f"font-size: 10.5px; font-weight: 800; color: {rate_color}; "
            f"font-family: monospace;"
        )


def _more_filters_button(state: dict, ctx_options: dict, *, on_change) -> None:
    """Filter-icon button.  Opens a dialog with the stat-context
    options that used to live in the dropdown."""
    dialog = ui.dialog().props("position=bottom")

    @ui.refreshable
    def label_caption():                                                  # noqa: WPS430
        if state["context"] != "all":
            ui.label("●").style(
                f"position: absolute; top: 4px; right: 4px; "
                f"color: {t.POS}; font-size: 8px;"
            )

    with dialog:
        with ui.column().style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; "
            f"gap: 12px; min-width: 260px;"
        ):
            ui.label("FILTERS").style(
                f"font-size: 10.5px; font-weight: 800; letter-spacing: .6px; "
                f"color: {t.TEXT_DIM2};"
            )
            for ctx_value, ctx_label in ctx_options.items():
                _ctx_radio_row(ctx_value, ctx_label, state, on_change=on_change,
                               refresh_caption=label_caption.refresh)
            with ui.row().classes("w-full justify-end").style("gap: 6px;"):
                ui.button("Done", on_click=dialog.close).props(
                    "no-caps unelevated dense"
                ).style(
                    f"background: {t.PRIMARY}; color: {t.BG}; "
                    f"font-size: 12px; font-weight: 800; "
                    f"padding: 6px 16px; border-radius: {t.RADIUS_SM};"
                )

    with ui.column().style(
        "gap: 4px; align-items: center; flex-shrink: 0; position: relative;"
    ):
        ui.button(
            icon="tune",
            on_click=dialog.open,
        ).props("flat dense round").style(
            f"color: {t.TEXT_DIM}; background: {t.CARD_HI}; "
            f"width: 32px; height: 28px; min-width: 32px; "
            f"border-radius: {t.RADIUS_PILL}; "
            f"border: 1px solid {t.BORDER};"
        )
        label_caption()


def _ctx_radio_row(
    value: str, label: str, state: dict,
    *, on_change, refresh_caption,
) -> None:
    """One row inside the filter dialog -- styled radio."""
    is_active = state["context"] == value

    def _click():
        state["context"] = value
        on_change()
        refresh_caption()

    with ui.row().classes("items-center w-full").style(
        f"gap: 10px; padding: 8px 4px; cursor: pointer; "
        f"border-radius: {t.RADIUS_SM}; "
        f"background: {(t.CARD_HI if is_active else 'transparent')};"
    ).on("click", _click):
        # Custom radio dot
        ui.html(
            f'<span style="display:inline-block; width:14px; height:14px; '
            f'border:2px solid {t.POS if is_active else t.BORDER}; '
            f'border-radius:50%; position:relative;">'
            + (
                f'<span style="position:absolute; inset:2px; '
                f'background:{t.POS}; border-radius:50%;"></span>'
                if is_active else ""
            )
            + "</span>"
        )
        ui.label(label).style(
            f"font-size: 12.5px; color: {t.TEXT}; font-weight: 600;"
        )


# ── Section: matchup insights ───────────────────────────────────────────────

def _section_matchup_insights(
    info: dict,
    prop: dict,
    market: str,
    line_f: Optional[float],
    summary: dict,
    opp_abbrev: Optional[str],
    is_pitcher: bool,
    games: list[dict],
) -> None:
    """3 insight rows + a team matchup card with opponent rank donut."""
    insights = _build_insights(info, market, summary, line_f, is_pitcher, games)
    if not insights and not opp_abbrev:
        return

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        ui.label("MATCHUP INSIGHTS").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )

        # Insight rows.  (The team-matchup card moved to the "vs Team"
        # tab in _section_matchup_tabs below.)
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; gap: 10px;"
        ):
            for tag, tag_color, text in insights:
                _insight_row(tag, tag_color, text)


def _insight_row(tag: str, tag_color: str, text: str) -> None:
    with ui.row().classes("items-start w-full").style(
        "gap: 10px; flex-wrap: nowrap;"
    ):
        ui.label(tag).style(
            f"background: {tag_color}1f; color: {tag_color}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 4px 8px; border-radius: {t.RADIUS_PILL}; "
            f"flex-shrink: 0; line-height: 1.2; min-width: 72px; "
            f"text-align: center;"
        )
        # Strip any stray HTML / markdown so tags never render as
        # literal text in this plain-text label (belt-and-braces on top
        # of the plain-text insight generation below).
        from src.utils import strip_formatting
        ui.label(strip_formatting(text)).style(
            f"font-size: 12px; color: {t.TEXT}; line-height: 1.45; "
            f"flex: 1; min-width: 0;"
        )


def _build_insights(
    info: dict,
    market: str,
    summary: dict,
    line_f: Optional[float],
    is_pitcher: bool,
    games: list[dict],
) -> list[tuple[str, str, str]]:
    """Templated insight strings -- (tag, tag_color, text).  All rows
    are derived from data already on the page so the page doesn't need
    extra API fetches."""
    out: list[tuple[str, str, str]] = []
    stat_key = _MARKET_TO_STAT.get(market) or ("K" if is_pitcher else "H")

    # 1. LINEUP / WORKLOAD row
    if is_pitcher and games:
        recent = games[-5:]
        ip_avg = sum(float(g.get("IP") or 0.0) for g in recent) / max(1, len(recent))
        out.append((
            "WORKLOAD", t.PRIMARY_HI,
            f"{ip_avg:.1f} IP average over the last {len(recent)} starts.",
        ))
    elif games:
        orders = [int(g.get("batting_order") or 0) for g in games[-5:] if g.get("batting_order")]
        if orders:
            mode = max(set(orders), key=orders.count)
            out.append((
                "LINEUP", t.PRIMARY_HI,
                f"Hitting {_ordinal(mode)} in {orders.count(mode)} of last {len(orders)} games.",
            ))

    # 2. RECENT FORM row
    l5_avg = summary.get("last_5_avg")
    if l5_avg is not None and line_f is not None:
        delta = l5_avg - line_f
        delta_word = "above" if delta > 0 else "below"
        out.append((
            "FORM", t.POS if delta > 0 else t.WARN,
            f"L5 avg {l5_avg:.2f} {stat_key}, "
            f"{abs(delta):.2f} {delta_word} the {line_f} line.",
        ))

    # 3. APPROACH / ARSENAL row
    if is_pitcher:
        # H2H or seasonal context for pitchers -- use H2H avg if any.
        h2h_avg = summary.get("h2h_avg")
        h2h_games = summary.get("h2h_games") or 0
        if h2h_games:
            out.append((
                "ARSENAL", t.WARN,
                f"H2H avg {h2h_avg:.2f} {stat_key} across {h2h_games} prior meetings.",
            ))
        else:
            l20_avg = summary.get("last_20_avg")
            if l20_avg is not None:
                out.append((
                    "ARSENAL", t.WARN,
                    f"L20 avg {l20_avg:.2f} {stat_key} is the cleanest baseline "
                    "absent H2H history.",
                ))
    else:
        # Batter -- use bats handedness + recent form
        bats = info.get("bats") or ""
        h2h_avg = summary.get("h2h_avg")
        h2h_games = summary.get("h2h_games") or 0
        if h2h_games:
            out.append((
                "APPROACH", t.WARN,
                f"{h2h_avg:.2f} avg in {h2h_games} prior matchups"
                + (f" (bats {bats})." if bats else "."),
            ))
        elif bats:
            out.append((
                "APPROACH", t.WARN,
                f"Bats {bats}. No prior history vs this opponent yet.",
            ))

    return out


# ── Section: team matchup card ──────────────────────────────────────────────

def _section_team_matchup_card(
    prop: dict,
    market: str,
    summary: dict,
    opp_abbrev: str,
    line_f: Optional[float],
) -> None:
    """Card with opp identity + rank donut + 3 stat cells."""
    try:
        from src.player_profile_client import get_opp_rank_for_prop
        rank = get_opp_rank_for_prop(opp_abbrev, market)
    except Exception:                                                     # noqa: BLE001
        rank = None
    rank_int = rank if isinstance(rank, int) else 30
    # Lower rank = better matchup (rank 1 = most favourable for over).
    rank_progress = max(0.0, min(1.0, (31 - rank_int) / 30.0))
    rank_color = (
        t.POS  if rank_int <= 10 else
        t.WARN if rank_int <= 20 else
        t.NEG
    )

    # Opponent full name from the prop's home/away_team
    home = prop.get("home_team") or ""
    away = prop.get("away_team") or ""
    opp_full = (
        home if home and home.split()[-1].upper().startswith(opp_abbrev) else
        away if away and away.split()[-1].upper().startswith(opp_abbrev) else
        opp_abbrev
    )
    game_time = _format_game_time(prop.get("commence_time")) or "—"

    # Stat cells -- L10 avg vs line, L10 hit rate, opp rank
    l10_avg   = summary.get("last_10_avg")
    l10_hits  = summary.get("last_10_hits") or 0
    l10_total = summary.get("last_10_games") or 0
    l10_rate  = (l10_hits / l10_total) if l10_total else None

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 12px;"
    ):
        # Top row: opp identity left, rank donut right
        with ui.row().classes("items-center w-full").style(
            "gap: 14px; flex-wrap: nowrap;"
        ):
            with ui.column().style("gap: 2px; flex: 1; min-width: 0;"):
                ui.label("VS").style(
                    f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(opp_full).style(
                    f"font-size: 15px; font-weight: 800; color: {t.TEXT}; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )
                ui.label(game_time).style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; "
                    f"font-family: monospace;"
                )

            ui.html(
                _donut_gauge_html(
                    progress=rank_progress,
                    color=rank_color,
                    center=(f"{rank}" if rank else "—"),
                    sub="OPP RANK",
                    size=72, stroke=6,
                )
            ).style("flex-shrink: 0;")

        # Three stat cells along the bottom
        cells: list[tuple[str, str, str]] = []
        if l10_avg is not None and line_f is not None:
            delta_color = t.POS if l10_avg >= line_f else t.NEG
            arrow       = "▲" if l10_avg >= line_f else "▼"
            cells.append((
                "L10 AVG", f"{arrow} {l10_avg:.2f}", delta_color,
            ))
        else:
            cells.append(("L10 AVG", "—", t.TEXT_DIM))
        if l10_rate is not None:
            rate_color = t.POS if l10_rate >= 0.5 else t.NEG
            rate_arrow = "▲" if l10_rate >= 0.5 else "▼"
            cells.append((
                "L10 HIT", f"{rate_arrow} {int(round(l10_rate * 100))}%", rate_color,
            ))
        else:
            cells.append(("L10 HIT", "—", t.TEXT_DIM))
        if rank is not None:
            cells.append(("OPP RANK", f"#{rank}/30", rank_color))
        else:
            cells.append(("OPP RANK", "—", t.TEXT_DIM))

        with ui.element("div").style(
            "display: grid; grid-template-columns: repeat(3, 1fr); "
            "gap: 8px; width: 100%;"
        ):
            for label, value, color in cells:
                with ui.column().style(
                    f"background: {t.CARD_HI}; border-radius: {t.RADIUS_SM}; "
                    f"padding: 8px 6px; gap: 2px; align-items: center; "
                    f"min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 13px; font-weight: 800; color: {color}; "
                        f"font-family: monospace;"
                    )


# ── Section: matchup tabs (vs Team + vs Lineup / vs Pitcher) ────────────────

def _section_matchup_tabs(
    info: dict,
    prop: dict,
    market: str,
    line_f: Optional[float],
    summary: dict,
    opp_abbrev: Optional[str],
    is_pitcher: bool,
) -> None:
    """Two tabs below the matchup insights:

      * "vs Team"   -- the opponent-rank donut + stat-column card.
      * "vs Lineup" (pitchers) -- opposing batting order + per-batter
        stat relevant to the prop market.
      * "vs Pitcher" (batters) -- head-to-head vs today's starter.

    The type-specific tab is lazy-loaded: its data (a multi-call MLB
    Stats API fan-out) is only fetched when the user opens the tab, on
    a background thread so the event loop stays responsive.  Results
    are day-cached in player_profile_client.
    """
    player_name = info.get("name") or ""
    type_label  = "vs Lineup" if is_pitcher else "vs Pitcher"
    state = {"loaded": False, "data": None}

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        ui.label("MATCHUP").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )

        with ui.tabs().props("dense align=left").classes(
            "player-market-tabs w-full"
        ).style(
            f"border-bottom: 1px solid {t.BORDER}; min-height: 38px;"
        ) as tabs:
            team_tab = ui.tab("vs Team")
            type_tab = ui.tab(type_label)

        @ui.refreshable
        def type_panel() -> None:                                         # noqa: WPS430
            if not state["loaded"]:
                ui.label("Loading matchup data…").style(
                    f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                    f"font-style: italic; padding: 16px 4px;"
                )
                return
            if is_pitcher:
                _render_vs_lineup(state["data"])
            else:
                _render_vs_pitcher(state["data"], line_f)

        async def _on_change(e) -> None:                                  # noqa: WPS430
            if str(getattr(e, "value", "")) != type_label or state["loaded"]:
                return
            state["loaded"] = True
            type_panel.refresh()   # show the "Loading…" placeholder
            try:
                from src.player_profile_client import (
                    get_opposing_lineup, get_batter_vs_pitcher,
                )
                if is_pitcher:
                    state["data"] = await asyncio.to_thread(
                        get_opposing_lineup, prop, player_name, market)
                else:
                    state["data"] = await asyncio.to_thread(
                        get_batter_vs_pitcher, prop, player_name)
            except Exception as exc:                                      # noqa: BLE001
                _log(f"matchup tab load failed: {exc}")
                state["data"] = None
            type_panel.refresh()

        with ui.tab_panels(tabs, value=team_tab, on_change=_on_change).classes(
            "w-full"
        ).style("background: transparent; padding: 0;"):
            with ui.tab_panel(team_tab).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                if opp_abbrev:
                    _section_team_matchup_card(prop, market, summary, opp_abbrev, line_f)
                else:
                    ui.label("Opponent not determined yet.").style(
                        f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                        f"font-style: italic; padding: 16px 4px;"
                    )
            with ui.tab_panel(type_tab).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                type_panel()


def _render_vs_lineup(data: Optional[dict]) -> None:
    """Opposing batting order table for a pitcher's matchup."""
    from src.utils import strip_formatting
    if not data or not data.get("available"):
        note = (data or {}).get("note") or "Lineup not available yet."
        ui.label(strip_formatting(note)).style(
            f"font-size: 12px; color: {t.TEXT_DIM2}; font-style: italic; "
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 16px; "
            f"text-align: center; width: 100%;"
        )
        return

    stat_label = strip_formatting(data.get("stat_label") or "STAT")
    batters    = data.get("batters") or []

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 6px 4px; gap: 0;"
    ):
        # Header row
        _lineup_grid_row(
            "#", "BATTER", "POS", "B", stat_label.upper(),
            header=True,
        )
        for i, b in enumerate(batters, 1):
            _lineup_grid_row(
                str(i),
                strip_formatting(b.get("name") or "—"),
                strip_formatting(b.get("position") or "—"),
                strip_formatting(b.get("hand") or "—"),
                strip_formatting(str(b.get("stat") or "—")),
            )


def _lineup_grid_row(
    num: str, name: str, pos: str, hand: str, stat: str,
    *, header: bool = False,
) -> None:
    """One row of the opposing-lineup grid."""
    fg     = t.TEXT_DIM2 if header else t.TEXT
    weight = "800" if header else "600"
    fsize  = "9.5px" if header else "12px"
    border = "" if header else f"border-top: 1px solid {t.BORDER_SOFT};"
    with ui.element("div").classes("w-full").style(
        "display: grid; "
        "grid-template-columns: 24px 1fr 42px 28px 56px; "
        f"gap: 6px; align-items: center; padding: 7px 8px; {border}"
    ):
        ui.label(num).style(
            f"font-size: {fsize}; font-weight: {weight}; color: {t.TEXT_DIM2}; "
            f"font-family: monospace;"
        )
        ui.label(name).style(
            f"font-size: {fsize}; font-weight: {weight}; color: {fg}; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        )
        ui.label(pos).style(
            f"font-size: {fsize}; font-weight: {weight}; color: {t.TEXT_DIM}; "
            f"font-family: monospace; text-align: center;"
        )
        ui.label(hand).style(
            f"font-size: {fsize}; font-weight: {weight}; color: {t.TEXT_DIM}; "
            f"font-family: monospace; text-align: center;"
        )
        ui.label(stat).style(
            f"font-size: {fsize}; font-weight: 800; "
            f"color: {(t.TEXT_DIM2 if header else t.PRIMARY_HI)}; "
            f"font-family: monospace; text-align: right;"
        )


def _render_vs_pitcher(data: Optional[dict], line_f: Optional[float]) -> None:
    """Head-to-head card for a batter vs today's starting pitcher."""
    from src.utils import strip_formatting
    if not data:
        ui.label("Matchup data unavailable.").style(
            f"font-size: 12px; color: {t.TEXT_DIM2}; font-style: italic; "
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 16px; "
            f"text-align: center; width: 100%;"
        )
        return

    pitcher_name = strip_formatting(data.get("pitcher_name") or "today's starter")
    pitcher_hand = strip_formatting(data.get("pitcher_hand") or "")
    hand_suffix  = f" ({pitcher_hand}HP)" if pitcher_hand else ""

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        ui.label(f"vs {pitcher_name}{hand_suffix}").style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT};"
        )

        if not data.get("available"):
            ui.label(strip_formatting(data.get("note") or "No prior history.")).style(
                f"font-size: 12px; color: {t.TEXT_DIM2}; font-style: italic;"
            )
            return

        games_n = int(data.get("games") or 0)
        if games_n < 5:
            ui.label("Limited H2H data").style(
                f"font-size: 11px; font-weight: 800; letter-spacing: .4px; "
                f"color: {t.WARN}; background: rgba(245, 158, 11, .08); "
                f"padding: 3px 8px; border-radius: {t.RADIUS_PILL}; "
                f"align-self: flex-start;"
            )

        # Career aggregate stat cells (per-game H2H isn't exposed by a
        # clean MLB endpoint, so we surface career AB / AVG / OPS etc.).
        cells = [
            ("G",   str(games_n)),
            ("AB",  str(data.get("ab") or 0)),
            ("H",   str(data.get("h") or 0)),
            ("HR",  str(data.get("hr") or 0)),
            ("AVG", strip_formatting(str(data.get("avg") or "—"))),
            ("OPS", strip_formatting(str(data.get("ops") or "—"))),
        ]
        with ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: repeat(6, 1fr); "
            "gap: 6px;"
        ):
            for label, value in cells:
                with ui.column().style(
                    f"background: {t.CARD_HI}; border-radius: {t.RADIUS_SM}; "
                    f"padding: 8px 4px; gap: 2px; align-items: center; min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 13px; font-weight: 800; color: {t.TEXT}; "
                        f"font-family: monospace;"
                    )

        so_bb = (
            f"{data.get('so') or 0} K · {data.get('bb') or 0} BB in "
            f"{data.get('ab') or 0} AB career"
        )
        ui.label(strip_formatting(so_bb)).style(
            f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
        )


# ── Section: recent trends ──────────────────────────────────────────────────

def _section_recent_trends(
    games: list[dict],
    is_pitcher: bool,
    market: str,
    line_f: Optional[float],
    summary: dict,
) -> None:
    """Two side-by-side cards: LAST 5 / LAST 10.  Each has a trend
    arrow, the value delta, and a mini bar chart with older bars grey
    and recent bars green/red against the line."""
    stat_key = _MARKET_TO_STAT.get(market) or ("K" if is_pitcher else "H")
    season_avg = summary.get("season_avg")

    def _trend_card(label: str, window_games: list[dict], avg_value: Optional[float]):
        if avg_value is None or season_avg is None:
            delta = None
        else:
            delta = avg_value - season_avg
        if delta is None:
            arrow_color = t.TEXT_DIM
            arrow_char  = "—"
            delta_text  = "—"
        elif delta > 0:
            arrow_color = t.POS
            arrow_char  = "▲"
            delta_text  = f"+{delta:.2f}"
        elif delta < 0:
            arrow_color = t.NEG
            arrow_char  = "▼"
            delta_text  = f"{delta:.2f}"
        else:
            arrow_color = t.TEXT_DIM
            arrow_char  = "▶"
            delta_text  = "0.00"

        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 8px; "
            f"min-width: 0; flex: 1 1 0;"
        ):
            ui.label(label).style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                f"color: {t.TEXT_DIM2};"
            )
            with ui.row().classes("items-baseline").style("gap: 6px;"):
                ui.label(arrow_char).style(
                    f"font-size: 18px; color: {arrow_color}; font-weight: 800;"
                )
                ui.label(delta_text).style(
                    f"font-size: 17px; font-weight: 800; color: {arrow_color}; "
                    f"font-family: monospace;"
                )
                if avg_value is not None:
                    ui.label(f"avg {avg_value:.2f}").style(
                        f"font-size: 10.5px; color: {t.TEXT_DIM}; "
                        f"font-family: monospace;"
                    )
            if window_games:
                ui.echart(_mini_bar_chart_options(
                    window_games, stat_key, line_f,
                )).style("width: 100%; height: 96px; min-width: 0;")
            else:
                ui.label("No games yet in this window.").style(
                    f"font-size: 11px; color: {t.TEXT_DIM2}; "
                    f"font-style: italic; padding: 16px 4px;"
                )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        ui.label("RECENT TRENDS").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        with ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: 1fr 1fr; "
            f"gap: {t.SPACE_SM};"
        ):
            _trend_card("LAST 5",  games[-5:],  summary.get("last_5_avg"))
            _trend_card("LAST 10", games[-10:], summary.get("last_10_avg"))


def _mini_bar_chart_options(
    window_games: list[dict],
    stat_key: str,
    prop_line: Optional[float],
) -> dict:
    """Compact bar chart for the trend cards -- oldest 50% grey, most
    recent half green/red against the line."""
    values: list[float] = [_stat_value(g, stat_key) for g in window_games]
    n = len(values)
    mid = max(0, n - max(1, n // 2))

    items: list[dict] = []
    for i, v in enumerate(values):
        recent = i >= mid
        if not recent:
            col = t.TEXT_DIM2
        elif prop_line is not None and v > prop_line:
            col = t.POS
        elif prop_line is not None and v < prop_line:
            col = t.NEG
        else:
            col = t.WARN
        items.append({"value": v, "itemStyle": {"color": col}})

    mark_line = []
    if prop_line is not None:
        mark_line.append({
            "yAxis":     prop_line,
            "lineStyle": {"color": "#ffffff", "type": "dashed", "width": 1, "opacity": .55},
            "label":     {"show": False},
        })

    return {
        "backgroundColor": "transparent",
        "grid": {"left": "2%", "right": "2%", "top": "8%", "bottom": "2%", "containLabel": False},
        "xAxis": {
            "type": "category",
            "data": [""] * n,
            "axisLabel": {"show": False},
            "axisLine":  {"show": False},
            "axisTick":  {"show": False},
        },
        "yAxis": {
            "type": "value",
            "axisLabel": {"show": False},
            "axisLine":  {"show": False},
            "axisTick":  {"show": False},
            "splitLine": {"show": False},
            "min": 0,
        },
        "series": [{
            "type": "bar",
            "data": items,
            "barWidth": "70%",
            "markLine": ({
                "symbol": ["none", "none"],
                "silent": True,
                "data":   mark_line,
            } if mark_line else None),
        }],
    }


# ── Donut gauge ────────────────────────────────────────────────────────────

def _donut_gauge_html(
    *,
    progress: float,
    color: str,
    center: str,
    sub: str = "",
    size: int = 60,
    stroke: int = 6,
) -> str:
    """Inline SVG arc gauge.  *progress* is 0..1; *color* is the arc
    colour; *center* / *sub* are the inner labels."""
    progress = max(0.0, min(1.0, float(progress)))
    radius   = (size / 2.0) - stroke
    circ     = 2 * 3.14159265 * radius
    dash     = progress * circ
    rest     = circ - dash
    cx = cy = size / 2.0
    track_color = t.CARD_HI
    inner_fs    = max(14, int(size * 0.34))
    sub_fs      = max(8, int(size * 0.13))
    sub_y       = cy + inner_fs * 0.55
    return (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
        f'    stroke="{track_color}" stroke-width="{stroke}" />'
        f'  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" '
        f'    stroke="{color}" stroke-width="{stroke}" '
        f'    stroke-dasharray="{dash} {rest}" stroke-dashoffset="0" '
        f'    stroke-linecap="round" transform="rotate(-90 {cx} {cy})" />'
        f'  <text x="{cx}" y="{cy + inner_fs * 0.35}" text-anchor="middle" '
        f'    font-size="{inner_fs}" font-weight="800" '
        f'    fill="{t.TEXT}" font-family="monospace">{center}</text>'
        + (
            f'  <text x="{cx}" y="{sub_y}" text-anchor="middle" '
            f'    font-size="{sub_fs}" font-weight="700" '
            f'    fill="{t.TEXT_DIM2}" letter-spacing="0.6px">{sub}</text>'
            if sub else ""
        ) +
        f'</svg>'
    )


def _letter_grade_for_prop(prop: dict) -> tuple[str, str]:
    """Derive A+/A/B+/etc from confidence + opp rank + EV%.

    Composite (0..1):
        0.5 * confidence-normalised        (confidence above 0.55 baseline)
        0.3 * opp-rank-normalised          (rank 1 best, 30 worst)
        0.2 * ev-normalised                (0% .. 30% EV)
    """
    try:
        conf = float(prop.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf_score = max(0.0, min(1.0, (conf - 0.50) / 0.45))

    # Opp rank: lower is better.  Derive from the cached opp_rank field
    # if it was attached during scoring; default to mid-range otherwise.
    try:
        rank = int(prop.get("opp_rank") or 15)
    except (TypeError, ValueError):
        rank = 15
    rank_score = max(0.0, min(1.0, (31 - rank) / 30.0))

    try:
        ev = float(prop.get("ev_pct") or 0.0)
    except (TypeError, ValueError):
        ev = 0.0
    ev_score = max(0.0, min(1.0, ev / 30.0))

    composite = 0.5 * conf_score + 0.3 * rank_score + 0.2 * ev_score

    # Map composite -> letter grade with +/-.
    # Bands chosen so a typical 60% conf + middling rank lands in B
    # territory, and only a great composite reaches A+.
    if   composite >= 0.92: letter = "A+"
    elif composite >= 0.84: letter = "A"
    elif composite >= 0.76: letter = "A-"
    elif composite >= 0.68: letter = "B+"
    elif composite >= 0.60: letter = "B"
    elif composite >= 0.52: letter = "B-"
    elif composite >= 0.44: letter = "C+"
    elif composite >= 0.36: letter = "C"
    elif composite >= 0.28: letter = "C-"
    elif composite >= 0.20: letter = "D+"
    else:                   letter = "D"

    color = _grade_color(letter)
    return letter, color


def _grade_color(letter: str) -> str:
    """Map letter grade -> theme colour."""
    first = letter[0]
    if first == "A": return t.POS
    if first == "B": return t.PRIMARY_HI
    if first == "C": return t.WARN
    return t.NEG


def _grade_progress(letter: str) -> float:
    """Map letter grade -> arc fill 0..1 (visual aid)."""
    return {
        "A+": 1.00, "A": 0.93, "A-": 0.86,
        "B+": 0.78, "B": 0.70, "B-": 0.62,
        "C+": 0.54, "C": 0.46, "C-": 0.38,
        "D+": 0.30, "D":  0.22,
    }.get(letter, 0.5)


# ── Filter helpers (preserved from prior layout) ────────────────────────────

def _apply_window_filter(
    games: list[dict],
    window: str,
    opp_abbrev: Optional[str],
) -> list[dict]:
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
        return [g for g in games if (g.get("opp") or "").upper() == opp_abbrev.upper()]
    return games[-10:]


def _stat_context_options(
    is_pitcher: bool,
    opp_abbrev: Optional[str],
) -> dict:
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
    opp_abbrev: Optional[str] = None,    # noqa: ARG001 (only used in opp filter below)
) -> list[dict]:
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


# ── Main bar chart options ──────────────────────────────────────────────────

def _per_prop_chart_options(
    games: list[dict],
    *,
    stat_key: str,
    prop_line: Optional[float],
    side: str,
) -> dict:
    """ECharts options for the headline bar chart.  Green if value is
    on the side's hit direction, red if missed.  Solid white dashed
    line marks the book line.  X-axis shows MM-DD vs OPP for each
    bar."""
    labels = []
    for g in games:
        date = (g.get("date") or "")[-5:]
        opp  = (g.get("opp") or "").upper()
        labels.append(f"{date}\n{opp}" if opp else date)

    values = [_stat_value(g, stat_key) for g in games]

    bar_items: list[dict] = []
    for v in values:
        if prop_line is None:
            col = t.PRIMARY
        else:
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
            "label":     {
                "formatter": f"  {prop_line}",
                "position":  "insideEndTop",
                "color":     "#ffffff",
                "fontSize":  10,
                "fontWeight": "bold",
            },
            "lineStyle": {"color": "#ffffff", "type": "dashed", "width": 2, "opacity": .8},
        })

    return {
        "backgroundColor": "transparent",
        "grid": {
            "left": "2%", "right": "2%",
            "top":  "16%", "bottom": "4%",
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
            "data":      labels,
            "axisLabel": {
                "color":    t.TEXT_DIM2,
                "fontSize": 9.5,
                "interval": "auto",
                "rotate":   45 if len(labels) > 10 else 0,
                "lineHeight": 11,
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
            "type":  "bar",
            "data":  bar_items,
            "barMaxWidth": 28,
            "label": {
                "show": True,
                "position": "top",
                "color": t.TEXT_DIM,
                "fontSize": 10,
                "fontWeight": "bold",
                "formatter": "{c}",
            },
            "markLine": ({
                "symbol": ["none", "none"],
                "data":   mark_line_data,
            } if mark_line_data else None),
        }],
    }


# ── Game log table (preserved) ──────────────────────────────────────────────

def _section_game_log(
    games: list[dict],
    is_pitcher: bool,
    today_props: list[dict],
) -> None:
    """Collapsible game log at the very bottom of the page.

    Hidden by default behind a pill toggle ("Game Log ▼"); tapping
    expands the full table ("Game Log ▲") and tapping again collapses
    it.  Highlights the column matching the strongest prop's stat."""
    if not games:
        return
    if today_props:
        top_market = today_props[0].get("market", "")
        highlighted = _MARKET_TO_STAT.get(top_market) or ("K" if is_pitcher else "H")
    else:
        highlighted = "K" if is_pitcher else "H"

    state = {"open": False}

    @ui.refreshable
    def render() -> None:                                                 # noqa: WPS430
        arrow = "▲" if state["open"] else "▼"
        ui.button(
            f"Game Log  {arrow}", on_click=_toggle,
        ).props("no-caps unelevated dense").style(
            f"background: {t.CARD}; color: {t.TEXT}; "
            f"border: 1px solid {t.BORDER}; "
            f"font-size: 11px; font-weight: 800; letter-spacing: .6px; "
            f"padding: 8px 14px; border-radius: {t.RADIUS_PILL}; "
            f"align-self: flex-start; min-height: 0;"
        )
        if state["open"]:
            _game_log_table(games, is_pitcher, highlighted)

    def _toggle() -> None:
        state["open"] = not state["open"]
        render.refresh()

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        render()


def _game_log_table(
    games: list[dict],
    is_pitcher: bool,
    highlighted_stat: str,
) -> None:
    """Single ui.html block -- avoids NiceGUI's per-element div wrapping
    that would otherwise break <tr>/<td> nesting."""
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
        "K": "K", "ER": "ER", "H": "H", "BB": "BB", "IP": "IP",
        "TB": "TB", "HR": "HR", "RBI": "RBI", "R": "R",
        "SO": "SO", "AB": "AB", "PA": "PA",
    }

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
            return str(ip_val).strip() or "—"

    def _opp_display(game: dict) -> str:
        prefix = "vs" if game.get("is_home") else "@"
        opp = game.get("opp")
        if opp and str(opp).strip():
            return f"{prefix}&nbsp;{str(opp).strip()}"
        park = game.get("park_team")
        if park and str(park).strip():
            return f"{prefix}&nbsp;{str(park).strip()}"
        return "—"

    hl = highlighted_stat.upper()
    th_base = (
        f"font-size:10px; font-weight:800; letter-spacing:.5px; "
        f"color:{t.TEXT_DIM2}; padding:6px 10px; "
        f"border-bottom:1px solid {t.BORDER}; white-space:nowrap;"
    )

    def _td(col: str, extra: str = "") -> str:
        highlighted = col_stat_map.get(col) == hl
        color  = t.POS if highlighted else t.TEXT
        weight = "800" if highlighted else "400"
        return (
            f"font-size:12px; font-family:monospace; font-weight:{weight}; "
            f"padding:6px 10px; text-align:right; color:{color}; "
            f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
            f"min-width:{col_min_w.get(col, '44px')}; {extra}"
        )

    header_cells = ""
    for col in cols:
        align = "left" if col in ("Date", "OPP") else "right"
        mw    = col_min_w.get(col, "44px")
        header_cells += (
            f"<th style='{th_base} text-align:{align}; min-width:{mw};'>{col}</th>"
        )

    _mw_date = col_min_w["Date"]
    _mw_opp  = col_min_w["OPP"]
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
                    cells += (
                        f"<td style='font-size:12px; font-family:monospace; "
                        f"padding:6px 10px; text-align:left; color:{t.TEXT_DIM}; "
                        f"border-bottom:1px solid {t.BORDER_SOFT}; white-space:nowrap; "
                        f"min-width:{_mw_opp};'>{_opp_display(g)}</td>"
                    )
                elif col == "IP":
                    cells += f"<td style='{_td(col)}'>{_safe_ip(g)}</td>"
                else:
                    raw_key = col_stat_map.get(col, col)
                    cells += f"<td style='{_td(col)}'>{_safe_num(g.get(raw_key))}</td>"
            body_rows += f"<tr class='game-log-row'>{cells}</tr>"
        except Exception as _row_exc:                                      # noqa: BLE001
            _log(f"game log row skipped ({type(_row_exc).__name__}: {_row_exc})")

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


# ── Empty / not-found states ────────────────────────────────────────────────

def _empty_state_message(info: dict, raw_games: list[dict], is_pitcher: bool) -> None:
    label = "strikeouts" if is_pitcher else "hits"
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"align-items: center; gap: 6px;"
    ):
        ui.label(
            f"No props posted yet for {info.get('name') or 'this player'}."
        ).style(f"font-size: 14px; font-weight: 700; color: {t.TEXT};")
        ui.label(
            f"Showing recent {label} in the game log below."
        ).style(f"font-size: 11.5px; color: {t.TEXT_DIM2};")


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


# ── Small data helpers ──────────────────────────────────────────────────────

def _stat_value(game: dict, stat_key: str) -> float:
    if stat_key == "outs":
        ip = game.get("IP")
        return float(round((ip if ip is not None else 0.0) * 3))
    raw = game.get(stat_key)
    try:
        return float(raw if raw is not None else 0)
    except (TypeError, ValueError):
        return 0.0


def _format_game_time(iso: Optional[str]) -> Optional[str]:
    """ISO commence_time -> '5/23 · 7:05 PM ET' (date + time in ET).
    Returns None on failure so callers can render a dash."""
    if not iso:
        return None
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.strftime("%-m/%-d · %-I:%M %p ET")
    except Exception:                                                      # noqa: BLE001
        return None


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _player_prop_summary_safe(
    player_name: str, market: str, line, side: str,
    *, opp_abbrev: Optional[str], is_pitcher: bool, games: list[dict],
) -> dict:
    """Wrap ``player_profile_client.get_player_prop_summary`` so a
    transient failure (cold cache, network blip) doesn't crash the
    page render."""
    try:
        from src.player_profile_client import get_player_prop_summary
        return get_player_prop_summary(
            player_name, market, line, side,
            opp_abbrev=opp_abbrev,
            is_pitcher=is_pitcher,
            games=games,
        ) or {}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"player_prop_summary failed: {exc}")
        return {}
