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
import re
import sys
from typing import Optional

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, controls, live_score


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

# Short tag shown on the chart AVG badge so it's clear which time window
# the displayed average reflects (FIX 1).
_WINDOW_TAG: dict[str, str] = {
    "Season":  "SZN",
    "H2H":     "H2H",
    "Last 5":  "L5",
    "Last 10": "L10",
    "Last 20": "L20",
}

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

# Full human-readable market labels, used for the no-props gamelog tabs
# (the props path keeps the compact _MARKET_TAB_LABEL above).
_MARKET_HUMAN_LABEL: dict[str, str] = {
    "pitcher_strikeouts":   "Strikeouts",
    "pitcher_outs":         "Outs",
    "pitcher_earned_runs":  "Earned Runs",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks":        "Walks",
    "batter_hits":          "Hits",
    "batter_total_bases":   "Total Bases",
    "batter_home_runs":     "Home Runs",
    "batter_rbis":          "RBIs",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
    "batter_stolen_bases":  "Stolen Bases",
}

# Role-ordered market lists used to source the no-props gamelog tabs (a
# market only becomes a tab when the gamelog actually carries that column).
_PITCHER_MARKETS = [
    "pitcher_strikeouts", "pitcher_outs", "pitcher_hits_allowed",
    "pitcher_walks", "pitcher_earned_runs",
]
_BATTER_MARKETS = [
    "batter_hits", "batter_total_bases", "batter_home_runs",
    "batter_rbis", "batter_runs_scored", "batter_walks",
    "batter_strikeouts", "batter_stolen_bases",
]

# Fixed prop-category skeleton per player type: (display label, market key).
# The props tab lists EVERY one of these; categories without a line today
# render a muted "No line today" state.  Keys match the props pipeline's
# market keys (the documented Odds API MLB pitcher markets: pitcher_outs,
# pitcher_hits_allowed, pitcher_walks, pitcher_earned_runs, pitcher_strikeouts).
# Batters Faced / Home Runs Allowed / Total Bases Allowed were removed -- they
# are not real Odds API markets (no key ever returns a line) and too rare to
# be useful.
_PITCHER_PROP_CATEGORIES = [
    ("Strikeouts",          "pitcher_strikeouts"),
    ("Outs Recorded",       "pitcher_outs"),
    ("Hits Allowed",        "pitcher_hits_allowed"),
    ("Walks Allowed",       "pitcher_walks"),
    ("Earned Runs Allowed", "pitcher_earned_runs"),
]
_BATTER_PROP_CATEGORIES = [
    ("Hits",               "batter_hits"),
    ("Total Bases",        "batter_total_bases"),
    ("Home Runs",          "batter_home_runs"),
    ("RBIs",               "batter_rbis"),
    ("Runs Scored",        "batter_runs_scored"),
    ("Walks",              "batter_walks"),
    ("Strikeouts",         "batter_strikeouts"),
    ("Singles",            "batter_singles"),
    ("Doubles",            "batter_doubles"),
    ("Triples",            "batter_triples"),
    ("Stolen Bases",       "batter_stolen_bases"),
    ("Hits + Runs + RBIs", "batter_hits_runs_rbis"),
]


# ── Page registration ──────────────────────────────────────────────────────

def register(backend) -> None:
    @ui.page("/player/mlb/{player_id_slug}")
    def player_page(player_id_slug: str):
        _log(f"player_page ENTER slug={player_id_slug!r}")
        try:
            ui.add_head_html(t.page_head_css())
            ui.add_head_html(_local_css())
            navbar.render(active=t.TAB_PROPS)
            _layout(backend, player_id_slug)
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
      /* The 4 top-level profile tabs (Pick / Overview / Matchup / Game
         Log) use the app's purple accent: purple underline + purple
         active text, plain white inactive text on a transparent strip. */
      .player-main-tabs .q-tab__indicator {{
        background: {t.PRIMARY} !important;
        height: 3px !important;
        box-shadow: 0 0 8px rgba({t.PRIMARY_R}, {t.PRIMARY_G}, {t.PRIMARY_B}, 0.55) !important;
      }}
      .player-main-tabs .q-tab {{
        color: {t.TEXT} !important;
        opacity: 1 !important;
      }}
      .player-main-tabs .q-tab--active {{
        color: {t.PRIMARY_HI} !important;
      }}
      .player-main-tabs .q-tabs__content {{
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
      }}

      /* The per-market sub-tabs inside the Pick tab keep the original
         green indicator -- this tab's content is unchanged.  Scope via
         .player-market-tabs so the rule doesn't leak elsewhere. */
      .player-market-tabs .q-tab__indicator {{
        background: {t.POS} !important;
        height: 3px !important;
        box-shadow: 0 0 8px rgba(16, 185, 129, 0.55) !important;
      }}
      .player-market-tabs .q-tab--active {{
        color: {t.POS} !important;
      }}
      .player-market-tabs .q-tabs__content {{
        overflow-x: auto !important;
        -webkit-overflow-scrolling: touch;
      }}

      /* Collapse the multi-column stat grids on phones so the cells stay
         legible: the 2-up rows stack to a single column, and the 6-up H2H
         grid drops to 3 columns.  Desktop (>768px) layout is unchanged. */
      @media (max-width: {t.MOBILE_BREAKPOINT}) {{
        .grid-2col {{ grid-template-columns: 1fr !important; }}
        .h2h-grid  {{ grid-template-columns: repeat(3, 1fr) !important; }}
      }}

      /* Desktop only (>768px): cap the history charts' width and centre them
         so they don't stretch awkwardly wide across the 1180px content area
         with only a handful of bars.  Mobile keeps the full-width charts. */
      @media (min-width: 769px) {{
        .player-chart {{
          max-width: 720px !important;
          margin-left: auto !important;
          margin-right: auto !important;
        }}
      }}
    </style>
    """


# ── Layout driver ────────────────────────────────────────────────────────────

def _layout(backend, player_id_slug: str) -> None:
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

        # Only show props for games that have NOT started yet (FIX 1).
        today_props_raw = get_today_props_for_player(info["name"]) or []
        today_props_all = [
            p for p in today_props_raw
            if not live_score.game_has_started(
                backend,
                commence_time=p.get("commence_time"),
                home_team=p.get("home_team"),
                away_team=p.get("away_team"),
                sport="mlb",
            )
        ]
        started_filtered = bool(today_props_raw) and not today_props_all
        today_prop      = today_props_all[0] if today_props_all else get_today_prop(info["name"])

        opp_abbrev: Optional[str] = None
        if today_props_all:
            opp_abbrev = get_player_today_opponent(info["name"], today_props_all[0])
        if not opp_abbrev and raw_games:
            opp_abbrev = (raw_games[-1].get("opp") or "").upper() or None

        _log(f"render {info['name']} (id={player_id}, pitcher={is_pitcher}, "
             f"props={len(today_props_all)}, opp={opp_abbrev}, games={len(games)})")

        # Highlighted game-log column = the strongest prop's stat (else
        # the default per role).  The strongest prop's book line is the
        # over/under threshold used to colour that column green/red (FIX 6).
        hl_line: Optional[float] = None
        if today_props_all:
            _hl_market = today_props_all[0].get("market", "")
            highlighted = _MARKET_TO_STAT.get(_hl_market) or ("K" if is_pitcher else "H")
            try:
                hl_line = float(today_props_all[0].get("line"))
            except (TypeError, ValueError):
                hl_line = None
        else:
            highlighted = "K" if is_pitcher else "H"

        # ── 4 top-level profile tabs ─────────────────────────────────────
        with ui.tabs().props("dense align=left no-caps").classes(
            "player-main-tabs w-full"
        ).style(
            f"border-bottom: 1px solid {t.BORDER}; min-height: 44px;"
        ) as main_tabs:
            tab_pick     = ui.tab("Pick")
            tab_overview = ui.tab("Overview")
            tab_matchup  = ui.tab("Matchup")
            tab_log      = ui.tab("Game Log")

        with ui.tab_panels(main_tabs, value=tab_pick).classes("w-full").style(
            "background: transparent; padding: 0;"
        ):
            with ui.tab_panel(tab_pick).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _tab_pick(backend, info, games, raw_games, is_pitcher,
                          today_props_all, today_prop, opp_abbrev, started_filtered)
            with ui.tab_panel(tab_overview).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _tab_overview(info, is_pitcher)
            with ui.tab_panel(tab_matchup).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _tab_matchup(info, today_prop, opp_abbrev, games, is_pitcher)
            with ui.tab_panel(tab_log).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _tab_game_log(games, is_pitcher, highlighted, hl_line)


# ── TAB 1: PICK (the original player-profile content, unchanged) ────────────

def _tab_pick(
    backend,
    info: dict,
    games: list[dict],
    raw_games: list[dict],
    is_pitcher: bool,
    today_props_all: list[dict],
    today_prop: Optional[dict],
    opp_abbrev: Optional[str],
    started_filtered: bool,
) -> None:
    """All the existing player-profile content: per-market sub-tabs with
    header, AI breakdown, chart, time pills, and similar players.  The
    game-log table now lives in its own tab (TAB 4)."""
    # No-props fallback: header ALWAYS renders, then gamelog-sourced market
    # tabs + charts (no dashed line) so the page is useful even with zero
    # props today.
    if not today_props_all:
        _section_player_header(info, today_prop, opp_abbrev,
                               prop=None, grade=None, props_today=False)
        if started_filtered:
            ui.label(
                "No upcoming props — this player's game has already started. "
                "Showing season game-log history below."
            ).style(
                f"font-size: 12.5px; font-weight: 600; color: {t.TEXT_DIM}; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_SM} {t.SPACE_MD}; "
                f"text-align: center; width: 100%;"
            )
        _render_gamelog_market_tabs(games, raw_games, is_pitcher)
        # AI RECOMMENDATIONS (empty state here -- no scored picks).
        _render_ai_recommendations(today_props_all)
        # Full reference table -- every market for the player type, all
        # "No line today" in this no-props state.
        _render_prop_category_skeleton(today_props_all, is_pitcher, info.get("name"))
        return

    # ── Per-market sub-tabs (green indicator, unchanged) ─────────────────
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
        "background: transparent; padding: 0;"
    ):
        for tab_obj, prop in tab_refs:
            with ui.tab_panel(tab_obj).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _render_market_view(info, games, is_pitcher, prop, opp_abbrev,
                                    backend=backend)

    # AI RECOMMENDATIONS: the scored picks as a list (sits between the per-
    # market AI verdict tabs above and the ALL MARKETS reference table below).
    _render_ai_recommendations(today_props_all)

    # Full reference table: EVERY prop market for the player type, with the
    # current line + confidence where a line exists, else "No line today".
    _render_prop_category_skeleton(today_props_all, is_pitcher, info.get("name"))


# ── No-props gamelog market tabs (chart-only, no prop line) ─────────────────

def _gamelog_has_stat(games: list[dict], market: str) -> bool:
    """True when at least one game carries the column this market charts
    (pitcher 'outs' is derived from the IP column)."""
    stat_key = _MARKET_TO_STAT.get(market)
    if not stat_key:
        return False
    key = "IP" if stat_key == "outs" else stat_key
    return any(isinstance(g.get(key), (int, float)) for g in games)


def _render_gamelog_market_tabs(
    games: list[dict], raw_games: list[dict], is_pitcher: bool,
) -> None:
    """Market sub-tabs sourced from the gamelog columns (used when there are
    no props today).  Each tab renders the same history chart as the props
    path but with no dashed prop line and no line label."""
    if not games:
        _empty_state_message_noinfo(raw_games, is_pitcher)
        return
    candidate = _PITCHER_MARKETS if is_pitcher else _BATTER_MARKETS
    markets = [m for m in candidate if _gamelog_has_stat(games, m)]
    if not markets:
        _empty_state_message_noinfo(raw_games, is_pitcher)
        return

    tab_refs: list[tuple] = []
    with ui.tabs().props("dense align=left inline-label").classes(
        "player-market-tabs w-full"
    ).style(
        f"border-bottom: 1px solid {t.BORDER}; min-height: 40px;"
    ) as tabs:
        for market in markets:
            label = _MARKET_HUMAN_LABEL.get(market, market.replace("_", " ").title())
            tab_refs.append((ui.tab(label), market))

    with ui.tab_panels(tabs, value=tab_refs[0][0]).classes("w-full").style(
        "background: transparent; padding: 0;"
    ):
        for tab_obj, market in tab_refs:
            with ui.tab_panel(tab_obj).style(f"padding: {t.SPACE_MD} 0 0 0;"):
                _render_gamelog_market_view(games, is_pitcher, market)


def _render_gamelog_market_view(
    games: list[dict], is_pitcher: bool, market: str,
) -> None:
    """One market's history chart with no prop line (no-props state)."""
    stat_key = _MARKET_TO_STAT.get(market) or ("K" if is_pitcher else "H")
    vals = [_stat_value(g, stat_key) for g in games]
    avg = (sum(vals) / len(vals)) if vals else None
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        # prop_line=None -> the badge renders neutral, no over/under colour.
        _section_avg_badge(avg, None, stat_key, window_tag="SZN")
        ui.echart(_per_prop_chart_options(
            games, stat_key=stat_key, prop_line=None, side="Over",
        )).classes("player-chart").style("width: 100%; height: 250px;")


def _empty_state_message_noinfo(raw_games: list[dict], is_pitcher: bool) -> None:
    """No-games fallback for the gamelog tabs path (no prop + no gamelog)."""
    ui.label("No game-log history available for this player yet.").style(
        f"font-size: 13px; font-weight: 700; color: {t.TEXT_DIM}; "
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
        f"text-align: center; width: 100%;"
    )


_BOOK_LABELS = {
    "fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
    "caesars": "Caesars", "williamhill_us": "Caesars", "betrivers": "BetRivers",
    "pointsbetus": "PointsBet", "espnbet": "ESPN BET", "fanatics": "Fanatics",
    "betonlineag": "BetOnline", "bovada": "Bovada", "lowvig": "LowVig",
    "mybookieag": "MyBookie", "ballybet": "Bally Bet",
}


def _book_label(book: Optional[str]) -> str:
    if not book:
        return ""
    return _BOOK_LABELS.get(book.lower()) or book.replace("_", " ").title()


def _render_prop_category_skeleton(
    today_props_all: list[dict], is_pitcher: bool,
    player_name: Optional[str] = None,
) -> None:
    """Fixed skeleton of EVERY prop category for the player type.

    Source of truth is the RAW props cache (every book line, regardless of
    scoring), so a market shows a line whenever any book posted one -- not
    only when it cleared the confidence + edge threshold.  Where a SCORED
    pick also exists, its model confidence % is overlaid (and its exact
    side/line used).  Markets with no book line at all show 'No line today'.
    """
    cats = _PITCHER_PROP_CATEGORIES if is_pitcher else _BATTER_PROP_CATEGORIES

    # Scored picks (confidence + the model's recommended side/line).
    scored_by_market: dict = {}
    for p in (today_props_all or []):
        m = p.get("market")
        if m and m not in scored_by_market:
            scored_by_market[m] = p

    # Raw book lines (all markets, unfiltered by scoring).
    raw_by_market: dict = {}
    if player_name:
        try:
            from src.player_profile_client import get_today_raw_lines_for_player
            raw_by_market = get_today_raw_lines_for_player(player_name) or {}
        except Exception:                                                 # noqa: BLE001
            raw_by_market = {}

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 2px; "
        f"margin-top: {t.SPACE_MD};"
    ):
        ui.label("ALL MARKETS").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.PRIMARY_HI}; margin-bottom: 6px;"
        )
        for label, market in cats:
            scored = scored_by_market.get(market)
            raw    = raw_by_market.get(market)
            with ui.row().classes("items-center w-full").style(
                f"gap: 10px; padding: 7px 0; "
                f"border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(label).style(
                    f"flex: 1; min-width: 0; font-size: 12.5px; font-weight: 700; "
                    f"color: {t.TEXT}; white-space: nowrap; overflow: hidden; "
                    f"text-overflow: ellipsis;"
                )
                if scored is not None:
                    # Scored pick: show the model's recommended side + line,
                    # the source book, and the confidence %.
                    side = (scored.get("side") or "Over").strip().upper()
                    line = scored.get("line")
                    book = _book_label(scored.get("best_book")
                                       or (raw or {}).get("best_book"))
                    try:
                        conf_s = f"{int(round(float(scored.get('confidence') or 0.0) * 100))}%"
                    except (TypeError, ValueError):
                        conf_s = "—"
                    _line_cell(f"{side} {line}")
                    _book_cell(book)
                    ui.label(conf_s).style(
                        f"font-size: 11.5px; font-weight: 700; color: {t.POS}; "
                        f"font-family: monospace; flex-shrink: 0; min-width: 42px; "
                        f"text-align: right;"
                    )
                elif raw is not None:
                    # Raw book line only (no scored pick): show the main line
                    # side-neutral + the best book, no confidence.
                    _line_cell(f"O/U {raw.get('line')}")
                    _book_cell(_book_label(raw.get("best_book")))
                    ui.label("").style("flex-shrink: 0; min-width: 42px;")
                else:
                    ui.label("No line today").style(
                        f"font-size: 11.5px; font-weight: 600; color: {t.TEXT_DIM2}; "
                        f"font-style: italic; flex-shrink: 0;"
                    )


def _line_cell(text: str) -> None:
    ui.label(text).style(
        f"font-size: 12.5px; font-weight: 800; color: {t.TEXT}; "
        f"font-family: monospace; flex-shrink: 0;"
    )


def _book_cell(book: str) -> None:
    if not book:
        return
    ui.label(book).style(
        f"font-size: 11px; font-weight: 600; color: {t.TEXT_DIM2}; "
        f"flex-shrink: 0; white-space: nowrap;"
    )


# ── AI RECOMMENDATIONS (scored picks only) ──────────────────────────────────

def _short_reason(text: str, limit: int = 160) -> str:
    """First sentence of the AI verdict, capped to *limit* chars."""
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", s)
    first = m.group(1) if m else s
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return first


def _render_ai_recommendations(today_props_all: list[dict]) -> None:
    """The player's SCORED picks (passed confidence + edge threshold) as a
    picks list -- distinct from the ALL MARKETS reference table below.  Always
    rendered (even with no picks) so the user knows the AI evaluated them."""
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 8px; "
        f"margin-top: {t.SPACE_MD};"
    ):
        ui.label("AI RECOMMENDATIONS").style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.PRIMARY_HI}; margin-bottom: 2px;"
        )
        if not today_props_all:
            ui.label("No picks recommended for this player today.").style(
                f"font-size: 12px; font-weight: 600; color: {t.TEXT_DIM2}; "
                f"font-style: italic;"
            )
            return
        for prop in today_props_all:
            _ai_rec_card(prop)


def _ai_rec_card(prop: dict) -> None:
    market = prop.get("market") or ""
    label  = _MARKET_HUMAN_LABEL.get(market, market.replace("_", " ").title())
    side   = (prop.get("side") or "Over").strip().upper()
    line   = prop.get("line")
    try:
        conf = float(prop.get("confidence") or 0.0) * 100.0
    except (TypeError, ValueError):
        conf = 0.0

    # ≥80 green, 60-79 yellow, <60 neutral (no accent).
    if conf >= 80:
        accent, conf_col = t.POS, t.POS
    elif conf >= 60:
        accent, conf_col = t.WARN, t.WARN
    else:
        accent, conf_col = t.BORDER, t.TEXT_DIM

    # Read-only peek at the cached AI breakdown -- never triggers generation.
    mv = reason = ""
    try:
        from src.player_ai_breakdown import peek_breakdown
        bd = peek_breakdown(prop) or {}
        mv     = (bd.get("model_version") or "").strip()
        reason = _short_reason(bd.get("verdict") or "")
    except Exception:                                                     # noqa: BLE001
        pass

    with ui.column().classes("w-full").style(
        f"background: {t.CARD_HI}; border: 1px solid {accent}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 4px;"
    ):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(label).style(
                f"flex: 1; min-width: 0; font-size: 13px; font-weight: 800; "
                f"color: {t.TEXT}; white-space: nowrap; overflow: hidden; "
                f"text-overflow: ellipsis;"
            )
            if mv:
                ui.label(mv).style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
                    f"color: {t.TEXT_DIM2}; background: {t.CARD}; padding: 2px 7px; "
                    f"border-radius: {t.RADIUS_PILL}; font-family: monospace; "
                    f"flex-shrink: 0;"
                ).tooltip("AI model version that produced this pick")
            ui.label(f"{int(round(conf))}%").style(
                f"font-size: 15px; font-weight: 800; color: {conf_col}; "
                f"font-family: monospace; flex-shrink: 0; min-width: 44px; "
                f"text-align: right;"
            )
        ui.label(f"{side} {line}").style(
            f"font-size: 12.5px; font-weight: 800; color: {t.TEXT}; "
            f"font-family: monospace;"
        )
        if reason:
            ui.label(reason).style(
                f"font-size: 11.5px; color: {t.TEXT_DIM}; line-height: 1.45; "
                f"white-space: normal;"
            )


def _render_header_track(backend, info: dict, prop: dict) -> None:
    """Track button for the player's primary/headline prop, shown in the
    header card.  components/track_button.py only handles game bets, so a
    PROP is tracked through the Props page's own prop-track control (POSTs
    /api/props/track) -- the same button used on the Props tab."""
    try:
        from pages.props import _track_btn
    except Exception:                                                     # noqa: BLE001
        return
    payload = {
        "player":          info.get("name") or prop.get("player"),
        "market":          prop.get("market"),
        "line":            prop.get("line"),
        "side":            (prop.get("side") or "Over").title(),
        "best_odds":       prop.get("best_odds") or prop.get("odds"),
        "confidence":      prop.get("confidence"),
        "predicted_value": prop.get("predicted_value"),
        "team":            prop.get("team", ""),
        "event_id":        prop.get("event_id"),
        "commence_time":   prop.get("commence_time"),
    }
    with ui.element("div").style("flex-shrink: 0;"):
        _track_btn(payload, backend)


# ── TAB 4: GAME LOG (always expanded) ───────────────────────────────────────

def _tab_game_log(games: list[dict], is_pitcher: bool, highlighted: str,
                  line: Optional[float] = None) -> None:
    """The full game-log table, always expanded (no collapse toggle)."""
    if not games:
        ui.label("No game log available for this player.").style(
            f"font-size: 13px; font-weight: 700; color: {t.TEXT_DIM}; "
            f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
            f"text-align: center; width: 100%;"
        )
        return
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        _game_log_table(games, is_pitcher, highlighted, line)


# ── Shared helpers for the Overview / Matchup tabs ─────────────────────────

def _clr(name: str) -> str:
    """Semantic colour name -> theme hex."""
    return {
        "pos": t.POS, "neg": t.NEG, "warn": t.WARN, "dim": t.TEXT_DIM,
        "primary": t.PRIMARY, "primary_hi": t.PRIMARY_HI,
    }.get(name, t.TEXT)


def _section_label(text: str) -> None:
    ui.label(text).style(
        f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
        f"color: {t.PRIMARY_HI};"
    )


def _card():
    return ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px; "
        f"min-width: 0;"
    )


def _note(msg: str, *, dashed: bool = False) -> None:
    border = "1px dashed" if dashed else "0"
    ui.label(msg).style(
        f"font-size: 12px; font-weight: 600; color: {t.TEXT_DIM2}; "
        f"font-style: italic; padding: 4px 2px; border: {border} {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; white-space: normal; line-height: 1.5;"
    )


def _lazy(loader, render, *, unavailable: str, spinner_text: str = "Loading…") -> None:
    """Per-section async loader: shows a spinner, runs the blocking
    *loader* in a thread, then calls *render(data)*.  On failure / None /
    ``{"available": False}`` it shows the *unavailable* note instead of
    crashing.  Never blocks page load."""
    holder = ui.column().classes("w-full").style("gap: 10px; min-width: 0;")
    with holder:
        with ui.row().classes("items-center").style("gap: 8px; padding: 4px 2px;"):
            ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
            ui.label(spinner_text).style(
                f"font-size: 12px; color: {t.TEXT_DIM2}; font-style: italic;")

    async def _run() -> None:                                             # noqa: WPS430
        try:
            data = await asyncio.to_thread(loader)
        except Exception as exc:                                          # noqa: BLE001
            _log(f"lazy section error: {type(exc).__name__}: {exc}")
            data = None
        holder.clear()
        with holder:
            ok = data is not None and (
                not isinstance(data, dict) or data.get("available", True)
            )
            if ok:
                try:
                    render(data)
                except Exception as exc:                                  # noqa: BLE001
                    _log(f"lazy render error: {type(exc).__name__}: {exc}")
                    _note(unavailable)
            else:
                _note(unavailable)

    ui.timer(0.05, _run, once=True)


def _stat_box(label: str, value: str, *, color: Optional[str] = None) -> None:
    with ui.column().classes("flex-grow").style(
        f"background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 2px; "
        f"min-width: 0; flex: 1 1 0; align-items: center;"
    ):
        ui.label(label).style(
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
            f"color: {t.TEXT_DIM2};")
        ui.label(value).style(
            f"font-size: 17px; font-weight: 800; font-family: monospace; "
            f"color: {color or t.TEXT};")


def _pct_badge(pct: Optional[float]) -> str:
    """Return an inline-HTML percentile badge (colored pill) string."""
    from src import player_matchup as _pm
    if pct is None:
        return ""
    color = _clr(_pm.percentile_color(pct))
    return (
        f'<span style="display:inline-block; font-size:9.5px; font-weight:800; '
        f'font-family:monospace; color:{color}; border:1px solid {color}; '
        f'border-radius:999px; padding:1px 7px; margin-left:6px; '
        f'background:rgba(0,0,0,.2);">{int(pct)}th</span>'
    )


# ── TAB 2: OVERVIEW ─────────────────────────────────────────────────────────

def _tab_overview(info: dict, is_pitcher: bool) -> None:
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_MD}; min-width: 0;"):
        # Section A — Season Averages (batters only).
        if not is_pitcher:
            with _card():
                _section_label("SEASON AVERAGES")

                def _load_season():                                       # noqa: WPS430
                    from src.player_profile_client import get_season_stats
                    return get_season_stats(info["id"], is_pitcher=False)

                _lazy(_load_season, _render_season_averages,
                      unavailable="Season averages unavailable.",
                      spinner_text="Loading season averages…")

        # Section B — Statcast Percentiles (pybaseball, weekly cache).
        with _card():
            _section_label("STATCAST PERCENTILES")
            pid = info["id"]

            def _load_pct():                                              # noqa: WPS430
                from src import statcast_client as _sc
                fn = (_sc.get_pitcher_percentiles if is_pitcher
                      else _sc.get_batter_percentiles)
                return fn(pid)

            _lazy(_load_pct, _render_statcast_percentiles,
                  unavailable="Statcast data unavailable.",
                  spinner_text="Loading Statcast data…")


def _render_season_averages(stats: dict) -> None:
    rows = (("AVG", "avg"), ("OBP", "obp"), ("SLG", "slg"), ("OPS", "ops"))
    if not any(stats.get(k) for _, k in rows):
        _note("No season batting stats yet for this player.")
        return
    with ui.row().classes("items-stretch w-full").style(
        "gap: 8px; flex-wrap: nowrap;"
    ):
        for label, key in rows:
            v = stats.get(key)
            try:
                txt = f"{float(v):.3f}".lstrip("0") if v else "—"
            except (TypeError, ValueError):
                txt = "—"
            _stat_box(label, txt)


# ── TAB 3: MATCHUP ──────────────────────────────────────────────────────────

def _tab_matchup(
    info: dict,
    prop: Optional[dict],
    opp_abbrev: Optional[str],
    games: list[dict],
    is_pitcher: bool,
) -> None:
    name      = info.get("name", "")
    home_team = (prop or {}).get("home_team") or ""
    commence  = (prop or {}).get("commence_time")

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_MD}; min-width: 0;"):
        # Section A — Overall matchup grade.
        with _card():
            _section_label("MATCHUP GRADE")
            if prop:
                def _load_grade():                                        # noqa: WPS430
                    from src import player_matchup as _pm
                    return _pm.get_matchup_grade(info, prop, games, is_pitcher)
                _lazy(_load_grade, _render_matchup_grade,
                      unavailable="Matchup grade unavailable.",
                      spinner_text="Grading matchup…")
            else:
                _note("No upcoming game to grade.")

        # Section B — Weather & Park (two side-by-side cards).
        with ui.row().classes("items-stretch w-full").style(
            "gap: 8px; flex-wrap: wrap;"
        ):
            with ui.column().style("flex: 1 1 240px; min-width: 0; gap: 0;"):
                with _card():
                    _section_label("WEATHER")
                    if home_team:
                        def _load_wx():                                   # noqa: WPS430
                            from src import player_matchup as _pm
                            return _pm.get_weather(home_team, commence)
                        _lazy(_load_wx, _render_weather,
                              unavailable="Weather unavailable.",
                              spinner_text="Loading weather…")
                    else:
                        _note("No game venue resolved.")
            with ui.column().style("flex: 1 1 240px; min-width: 0; gap: 0;"):
                with _card():
                    _section_label("PARK FACTORS")
                    if home_team:
                        def _load_park():                                 # noqa: WPS430
                            from src import player_matchup as _pm
                            return _pm.get_park(home_team)
                        _lazy(_load_park, _render_park,
                              unavailable="Park data unavailable.",
                              spinner_text="Loading park factors…")
                    else:
                        _note("No game venue resolved.")

        # Section C — Tonight's starter (batters) / opposing lineup (pitchers).
        if is_pitcher:
            with _card():
                _section_label("OPPOSING LINEUP")
                if prop:
                    _pitcher_hand = info.get("throws") or ""

                    def _load_lineup():                                   # noqa: WPS430
                        from src.player_profile_client import get_opposing_lineup_basic
                        return get_opposing_lineup_basic(prop, name, _pitcher_hand)
                    _lazy(_load_lineup, _render_lineup,
                          unavailable="Opposing lineup not posted yet.",
                          spinner_text="Loading opposing lineup…")
                else:
                    _note("No upcoming game.")
        else:
            with _card():
                _section_label("TONIGHT'S STARTER")
                if prop:
                    def _load_starter():                                  # noqa: WPS430
                        from src import player_matchup as _pm
                        return _pm.get_opposing_starter(prop, name)
                    _lazy(_load_starter, _render_starter,
                          unavailable="Opposing starter not announced yet.",
                          spinner_text="Loading opposing starter…")
            with _card():
                _section_label("CAREER H2H")
                if prop:
                    def _load_h2h():                                      # noqa: WPS430
                        from src.player_profile_client import get_batter_vs_pitcher
                        return get_batter_vs_pitcher(prop, name)
                    _lazy(_load_h2h, _render_h2h,
                          unavailable="No prior matchups.",
                          spinner_text="Loading head-to-head…")
                else:
                    _note("No upcoming game.")
            with _card():
                _section_label("BATTER VS PITCH TYPE")
                if prop:
                    bid = info["id"]

                    def _load_bvp():                                      # noqa: WPS430
                        from src import statcast_client as _sc
                        from src.player_profile_client import get_today_opposing_pitcher
                        pit = get_today_opposing_pitcher(prop, name)
                        if not pit or not pit.get("id"):
                            return {"available": False,
                                    "note": "No opposing starter announced yet."}
                        return _sc.get_batter_vs_pitch_types(bid, pit["id"])
                    _lazy(_load_bvp, _render_bvp_table,
                          unavailable="Statcast data unavailable.",
                          spinner_text="Loading pitch-type splits…")
                else:
                    _note("No upcoming game.")

        # Section D — Pitcher Arsenal donut (batters; opposing starter).
        if not is_pitcher:
            with _card():
                _section_label("PITCHER ARSENAL")
                if prop:
                    def _load_arsenal():                                  # noqa: WPS430
                        from src import statcast_client as _sc
                        from src.player_profile_client import get_today_opposing_pitcher
                        pit = get_today_opposing_pitcher(prop, name)
                        if not pit or not pit.get("id"):
                            return {"available": False,
                                    "note": "No opposing starter announced yet."}
                        return _sc.get_pitch_mix(pit["id"])
                    _lazy(_load_arsenal, _render_arsenal,
                          unavailable="Pitch data unavailable.",
                          spinner_text="Loading pitch arsenal…")
                else:
                    _note("No upcoming game.")

        # Section E — Team Bullpen (pitching-staff aggregate + league ranks).
        with _card():
            _section_label("TEAM BULLPEN")
            # Pitching team = opponent for a batter; own team for a pitcher.
            team_abbrev = (info.get("team_abbrev") if is_pitcher else opp_abbrev) or ""
            if team_abbrev:
                def _load_pen():                                          # noqa: WPS430
                    from src import player_matchup as _pm
                    return _pm.get_bullpen_stats(team_abbrev)
                _lazy(_load_pen, _render_bullpen,
                      unavailable="Bullpen data unavailable.",
                      spinner_text="Loading bullpen stats…")
            else:
                _note("No pitching team resolved.")


def _render_matchup_grade(d: dict) -> None:
    """Numeric matchup score out of 100 (FIX 3).  No explanatory sentence
    -- the Weather / Park / Starter / Bullpen cards below convey the why."""
    color = _clr(d.get("color", "dim"))
    score = d.get("score")
    score_txt = f"{int(round(score))}/100" if isinstance(score, (int, float)) else "—"
    with ui.row().classes("items-baseline w-full").style("gap: 4px;"):
        ui.label(score_txt.split("/")[0]).style(
            f"font-size: 40px; font-weight: 900; color: {color}; "
            f"line-height: 1; font-family: monospace;")
        ui.label("/100").style(
            f"font-size: 18px; font-weight: 800; color: {t.TEXT_DIM2}; "
            f"font-family: monospace;")


def _render_weather(d: dict) -> None:
    temp = d.get("temperature")
    temp_txt = f"{int(temp)}°F" if isinstance(temp, (int, float)) else "—"
    wind = d.get("wind_speed")
    wind_txt = (f"{int(wind)} mph {d.get('wind_dir', '')}".strip()
                if isinstance(wind, (int, float)) else "—")
    with ui.column().classes("w-full").style("gap: 8px; min-width: 0;"):
        ui.label(d.get("conditions", "—")).style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};")
        with ui.row().classes("items-stretch w-full").style(
            "gap: 8px; flex-wrap: nowrap;"
        ):
            _stat_box("TEMP", temp_txt)
            _stat_box("WIND", wind_txt)


def _render_park(d: dict) -> None:
    with ui.column().classes("w-full").style("gap: 8px; min-width: 0;"):
        ui.label(d.get("park_name", "—")).style(
            f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
            f"white-space: normal; line-height: 1.3;")

        def _factor_color(f):
            if not isinstance(f, (int, float)):
                return t.TEXT
            return t.POS if f > 1.02 else (t.NEG if f < 0.98 else t.TEXT_DIM)

        run_f = d.get("run_factor")
        hr_f  = d.get("hr_factor")
        with ui.row().classes("items-stretch w-full").style(
            "gap: 8px; flex-wrap: nowrap;"
        ):
            _stat_box("RUN FACTOR",
                      f"{run_f:.2f}" if isinstance(run_f, (int, float)) else "—",
                      color=_factor_color(run_f))
            _stat_box("HR FACTOR",
                      f"{hr_f:.2f}" if isinstance(hr_f, (int, float)) else "—",
                      color=_factor_color(hr_f))


def _render_starter(d: dict) -> None:
    from src import player_matchup as _pm
    hand = (d.get("hand") or "").upper()
    hand_txt = f" ({hand})" if hand in ("L", "R") else ""
    record = f"{d.get('wins', 0)}-{d.get('losses', 0)}"
    with ui.column().classes("w-full").style("gap: 10px; min-width: 0;"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(f"{d.get('name', '—')}{hand_txt}").style(
                f"font-size: 15px; font-weight: 800; color: {t.TEXT}; "
                f"white-space: normal;")
            ui.label(f"  {record}").style(
                f"font-size: 12px; font-weight: 700; color: {t.TEXT_DIM}; "
                f"font-family: monospace;")
        # Rate-stat rows with percentile badges.
        for label, key, fmt in (
            ("ERA",  "era",  "{:.2f}"),
            ("WHIP", "whip", "{:.2f}"),
            ("K/9",  "k9",   "{:.1f}"),
            ("BB/9", "bb9",  "{:.1f}"),
        ):
            val = d.get(key)
            try:
                vtxt = fmt.format(float(val)) if val is not None else "—"
            except (TypeError, ValueError):
                vtxt = "—"
            pct = _pm.pitcher_stat_percentile(key, val)
            with ui.row().classes("items-center w-full").style(
                f"gap: 8px; padding: 6px 0; border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(label).style(
                    f"font-size: 11px; font-weight: 800; letter-spacing: .4px; "
                    f"color: {t.TEXT_DIM2}; min-width: 48px;")
                ui.label(vtxt).style(
                    f"font-size: 14px; font-weight: 800; font-family: monospace; "
                    f"color: {t.TEXT}; flex: 1;")
                ui.html(_pct_badge(pct))


def _render_h2h(d: dict) -> None:
    if not d.get("available") or int(d.get("ab", 0)) < 5:
        _note("No prior matchups (fewer than 5 PA).")
        return
    ab = int(d.get("ab", 0))
    h  = int(d.get("h", 0))
    hr = int(d.get("hr", 0))
    so = int(d.get("so", 0))
    k_pct = f"{(so / ab * 100):.0f}%" if ab else "—"
    with ui.row().classes("items-stretch w-full").style(
        "gap: 8px; flex-wrap: nowrap;"
    ):
        _stat_box("PA", str(ab))
        _stat_box("AVG", d.get("avg", "—"))
        _stat_box("HR", str(hr))
        _stat_box("K%", k_pct)


# ── Statcast percentile bars (Tab 2) ────────────────────────────────────────

def _percentile_bar(label: str, value: str, pct: Optional[float]) -> None:
    """One metric row: name | gradient track w/ colored circle | raw value."""
    from src import player_matchup as _pm
    color = _clr(_pm.percentile_color(pct))
    pos = pct if isinstance(pct, (int, float)) else 50
    track = (
        f'<div style="position:relative; height:8px; border-radius:999px; '
        f'background:linear-gradient(90deg, rgba(244,63,94,.30), '
        f'rgba(245,158,11,.30), rgba(16,185,129,.30)); width:100%;">'
        f'<div style="position:absolute; top:50%; left:{pos}%; '
        f'transform:translate(-50%,-50%); width:14px; height:14px; '
        f'border-radius:50%; background:{color}; border:2px solid {t.BG}; '
        f'box-shadow:0 0 6px {color};"></div></div>'
    )
    with ui.row().classes("items-center w-full").style(
        "gap: 10px; padding: 5px 0; min-width: 0;"
    ):
        ui.label(label).style(
            f"font-size: 11px; font-weight: 700; color: {t.TEXT_DIM}; "
            f"min-width: 104px; white-space: nowrap;")
        ui.html(track).style("flex: 1; min-width: 0;")
        ui.label(value).style(
            f"font-size: 12px; font-weight: 800; font-family: monospace; "
            f"color: {t.TEXT}; min-width: 52px; text-align: right;")


def _render_statcast_percentiles(data: dict) -> None:
    splits = data.get("splits", {}) or {}
    state = {"split": "all"}
    options = (("all", "SZN"), ("rhp", "vs RHP"), ("lhp", "vs LHP"))

    @ui.refreshable
    def render_pills() -> None:                                           # noqa: WPS430
        with ui.row().classes("items-center").style(
            "gap: 6px; flex-wrap: wrap; padding-bottom: 8px;"
        ):
            for key, plabel in options:
                active = state["split"] == key
                bg = t.PRIMARY if active else "transparent"
                fg = t.TEXT if active else t.TEXT_DIM
                bd = t.PRIMARY if active else t.BORDER
                ui.button(plabel, on_click=lambda k=key: _set(k)).props(
                    "no-caps unelevated dense"
                ).style(
                    f"background: {bg}; color: {fg}; border: 1px solid {bd}; "
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .4px; "
                    f"padding: 4px 12px; border-radius: {t.RADIUS_PILL}; "
                    f"min-height: 0;")

    @ui.refreshable
    def render_bars() -> None:                                            # noqa: WPS430
        sp = splits.get(state["split"]) or {}
        if not sp.get("available"):
            _note(sp.get("note") or "Not enough data for this split.")
            return
        for r in sp.get("rows", []):
            _percentile_bar(r["label"], r["value"], r["percentile"])

    def _set(split: str) -> None:
        state["split"] = split
        render_pills.refresh()
        render_bars.refresh()

    render_pills()
    render_bars()


# ── Opposing lineup (Tab 3, pitcher view) ───────────────────────────────────

# League-average baselines for the lineup split arrows (FIX 4).
_LG_SPLIT = {"avg": 0.245, "woba": 0.315, "iso": 0.155, "k_pct": 22.5}


def _split_arrow(metric: str, value) -> str:
    """Inline ▲/▼ vs league average for a split metric.  Up+green = better
    than league (for K% 'better' means lower); down+red = worse."""
    if not isinstance(value, (int, float)):
        return ""
    base = _LG_SPLIT[metric]
    better = (value < base) if metric == "k_pct" else (value > base)
    sym = "▲" if better else "▼"
    color = t.POS if better else t.NEG
    return (f"<span style='color:{color}; font-size:10px; "
            f"margin-left:2px;'>{sym}</span>")


def _render_lineup(d: dict) -> None:
    batters = d.get("batters") or []
    if not batters:
        _note("Opposing lineup not posted yet.")
        return
    split_label = d.get("split_label") or "vs RHP"

    def _r3(v):  # rate -> '.287'
        return f"{v:.3f}".lstrip("0") if isinstance(v, (int, float)) else "—"

    with ui.column().classes("w-full").style("gap: 0; min-width: 0;"):
        for b in batters:
            hand = f" ({b.get('hand')})" if b.get("hand") in ("L", "R", "S") else ""
            with ui.column().classes("w-full").style(
                f"gap: 3px; padding: 8px 0; "
                f"border-bottom: 1px solid {t.BORDER_SOFT}; min-width: 0;"
            ):
                # Row 1: order, name (hand), season slash.
                with ui.row().classes("items-center w-full").style("gap: 8px;"):
                    ui.label(str(b.get("order") or "")).style(
                        f"font-size: 11px; font-weight: 800; color: {t.TEXT_DIM2}; "
                        f"font-family: monospace; min-width: 16px;")
                    ui.label(f"{b.get('name', '')}{hand}").style(
                        f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                        f"flex: 1; min-width: 0; white-space: nowrap; "
                        f"overflow: hidden; text-overflow: ellipsis;")
                    ui.label(
                        f"{b.get('avg','—')}/{b.get('obp','—')}/{b.get('slg','—')}"
                    ).style(
                        f"font-size: 11px; font-weight: 700; color: {t.TEXT_DIM}; "
                        f"font-family: monospace; flex-shrink: 0;")
                # Row 2: split vs pitcher hand with arrows.
                pa = b.get("split_pa")
                if isinstance(pa, (int, float)) and pa > 0:
                    parts = [
                        f"<span style='color:{t.TEXT_DIM2};'>{split_label}</span>",
                        f"PA {int(pa)}",
                        f"AVG {_r3(b.get('split_avg'))}{_split_arrow('avg', b.get('split_avg'))}",
                        f"wOBA {_r3(b.get('split_woba'))}{_split_arrow('woba', b.get('split_woba'))}",
                        f"ISO {_r3(b.get('split_iso'))}{_split_arrow('iso', b.get('split_iso'))}",
                        (f"K% {b.get('split_k_pct'):.0f}%{_split_arrow('k_pct', b.get('split_k_pct'))}"
                         if isinstance(b.get('split_k_pct'), (int, float)) else "K% —"),
                    ]
                    ui.html(
                        "<div style='display:flex; flex-wrap:wrap; gap:10px; "
                        "font-size:11px; font-family:monospace; "
                        f"color:{t.TEXT}; padding-left:24px;'>"
                        + "".join(f"<span>{p}</span>" for p in parts)
                        + "</div>"
                    )
                else:
                    ui.label(f"{split_label}: no split data").style(
                        f"font-size: 10.5px; font-style: italic; "
                        f"color: {t.TEXT_DIM2}; padding-left: 24px;")


# ── Batter vs pitch type (Tab 3, batter view) ───────────────────────────────

def _render_bvp_table(d: dict) -> None:
    rows = d.get("rows") or []
    if not rows:
        _note("No pitch-type data available.")
        return
    th = (f"font-size:9.5px; font-weight:800; letter-spacing:.4px; "
          f"color:{t.TEXT_DIM2}; padding:5px 6px; text-align:right; "
          f"border-bottom:1px solid {t.BORDER};")
    th_l = th.replace("text-align:right", "text-align:left")
    head = (f"<th style='{th_l}'>Pitch</th><th style='{th}'>Faced</th>"
            f"<th style='{th}'>AVG</th><th style='{th}'>SLG</th>"
            f"<th style='{th}'>HR</th><th style='{th}'>K%</th>")
    body = ""
    for r in rows:
        td = (f"font-size:12px; font-family:monospace; padding:5px 6px; "
              f"text-align:right; color:{t.TEXT}; "
              f"border-bottom:1px solid {t.BORDER_SOFT};")
        td_l = td.replace("text-align:right", "text-align:left")
        body += (
            f"<tr><td style='{td_l}'>{r.get('pitch', '')}</td>"
            f"<td style='{td}'>{r.get('faced', 0)}</td>"
            f"<td style='{td}'>{r.get('avg', '—')}</td>"
            f"<td style='{td}'>{r.get('slg', '—')}</td>"
            f"<td style='{td}'>{r.get('hr', 0)}</td>"
            f"<td style='{td}'>{r.get('k_pct', '—')}</td></tr>"
        )
    ui.html(
        f"<div style='overflow-x:auto; width:100%;'>"
        f"<table style='width:100%; border-collapse:collapse;'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


# ── Pitcher arsenal donut (Tab 3, batter view) ──────────────────────────────

_ARSENAL_PALETTE = [
    t.PRIMARY, t.PRIMARY_HI, t.POS, t.WARN, t.NEG,
    "#3b82f6", "#14b8a6", "#eab308", "#ec4899", "#8b5cf6",
]


def _render_arsenal(mix: dict) -> None:
    pitches = mix.get("pitches") or []
    if not pitches:
        _note("Pitch data unavailable.")
        return
    data = []
    for i, p in enumerate(pitches):
        data.append({
            "value": p.get("usage", 0),
            "name":  p.get("name", p.get("type", "?")),
            "itemStyle": {"color": _ARSENAL_PALETTE[i % len(_ARSENAL_PALETTE)]},
        })
    opts = {
        "backgroundColor": "transparent",
        "title": {
            "text": str(mix.get("total_types", len(pitches))),
            "subtext": "Pitches",
            "left": "center", "top": "36%",
            "textStyle": {"color": t.TEXT, "fontSize": 22, "fontWeight": "bold"},
            "subtextStyle": {"color": t.TEXT_DIM2, "fontSize": 10},
        },
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}%"},
        "series": [{
            "type": "pie", "radius": ["52%", "74%"],
            "avoidLabelOverlap": False,
            "label": {"show": False},
            "labelLine": {"show": False},
            "data": data,
        }],
    }
    with ui.row().classes("items-center w-full").style(
        "gap: 14px; flex-wrap: wrap; min-width: 0;"
    ):
        ui.echart(opts).style("width: 160px; height: 160px; flex-shrink: 0;")
        with ui.column().style("gap: 5px; flex: 1; min-width: 140px;"):
            for i, p in enumerate(pitches):
                color = _ARSENAL_PALETTE[i % len(_ARSENAL_PALETTE)]
                velo = p.get("velocity")
                velo_txt = f"{velo:.1f} mph" if isinstance(velo, (int, float)) else "—"
                ui.html(
                    f"<div style='display:flex; align-items:center; gap:8px; "
                    f"font-size:12px;'>"
                    f"<span style='width:10px; height:10px; border-radius:50%; "
                    f"background:{color}; flex-shrink:0;'></span>"
                    f"<span style='color:{t.TEXT}; font-weight:700; flex:1;'>"
                    f"{p.get('name', '')}</span>"
                    f"<span style='color:{t.TEXT_DIM}; font-family:monospace; "
                    f"font-weight:800;'>{p.get('usage', 0):.0f}%</span>"
                    f"<span style='color:{t.TEXT_DIM2}; font-family:monospace; "
                    f"min-width:64px; text-align:right;'>{velo_txt}</span></div>"
                )


# ── Team bullpen (Tab 3) ────────────────────────────────────────────────────

def _render_bullpen(d: dict) -> None:
    from src import player_matchup as _pm
    n = d.get("n_teams", 30)

    def _rank_badge(rank) -> str:
        if not rank:
            return ""
        color = _clr(_pm.rank_color(rank))
        return (
            f'<span style="display:inline-block; font-size:9px; font-weight:800; '
            f'font-family:monospace; color:{color}; border:1px solid {color}; '
            f'border-radius:999px; padding:1px 6px; margin-top:3px; '
            f'background:rgba(0,0,0,.2);">#{rank} of {n}</span>'
        )

    items = (
        ("ERA",  d.get("era"),  d.get("era_rank")),
        ("R/9",  d.get("r9"),   d.get("r9_rank")),
        ("WHIP", d.get("whip"), d.get("whip_rank")),
    )
    with ui.column().classes("w-full").style("gap: 8px; min-width: 0;"):
        ui.label("Season bullpen (relief) aggregate"
                 if d.get("scope") == "relief"
                 else "Season pitching-staff aggregate").style(
            f"font-size: 10.5px; font-style: italic; color: {t.TEXT_DIM2};")
        with ui.row().classes("items-stretch w-full").style(
            "gap: 8px; flex-wrap: nowrap;"
        ):
            for label, val, rank in items:
                vtxt = f"{val:.2f}" if isinstance(val, (int, float)) else "—"
                with ui.column().classes("flex-grow").style(
                    f"background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 2px; "
                    f"min-width: 0; flex: 1 1 0; align-items: center;"
                ):
                    ui.label(label).style(
                        f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                        f"color: {t.TEXT_DIM2};")
                    ui.label(vtxt).style(
                        f"font-size: 17px; font-weight: 800; font-family: monospace; "
                        f"color: {t.TEXT};")
                    ui.html(_rank_badge(rank))


# ── Per-market view (everything below the tabs) ─────────────────────────────

def _section_ai_breakdown(
    info: dict,
    games: list[dict],
    is_pitcher: bool,
    prop: dict,
    market: str,
    line_f: Optional[float],
    summary: dict,
    opp_abbrev: Optional[str],
) -> None:
    """AI matchup breakdown -- four labeled dark cards (Matchup, Trends,
    Arsenal/Approach or Plate Discipline, Game Script).  Cached once per
    player/market/day in Supabase.  Shows a subtle spinner while generating
    and renders nothing on any failure so the rest of the page is unaffected."""
    holder = ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};")
    with holder:
        with ui.row().classes("items-center").style("gap: 8px; padding: 4px 2px;"):
            ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
            ui.label("Generating AI breakdown…").style(
                f"font-size: 12px; color: {t.TEXT_DIM2}; font-style: italic;")

    async def _load() -> None:                                            # noqa: WPS430
        try:
            from src import player_ai_breakdown as _pab
            bd = await asyncio.to_thread(
                _pab.get_breakdown, info, games, is_pitcher, prop, market,
                line_f, summary, opp_abbrev,
            )
        except Exception:                                                 # noqa: BLE001
            bd = None
        holder.clear()
        if not bd:
            return  # API failed / no data -> show nothing (page still loads)

        from src.player_ai_breakdown import (
            approach_label, verdict_label, tier_color, agreement_outline_token,
        )
        with holder:
            # ── AI Verdict box (full width, above the grid) ──────────────
            # Badge comes from the AI's OWN verdict_tier (same determination
            # that wrote the text) so the badge and the written verdict can
            # never point in opposite directions.  Fall back to the
            # confidence-derived label only when the AI gave no tier.
            ai_tier = (bd.get("verdict_tier") or "").strip()
            if ai_tier:
                label, color_tok = ai_tier, tier_color(ai_tier)
            else:
                label, color_tok = verdict_label(prop.get("confidence"), prop.get("edge"))
            vcolor = {"pos": t.POS, "warn": t.WARN, "neg": t.NEG}.get(color_tok, t.TEXT_DIM)
            # Outline reflects AI-vs-model AGREEMENT (not the model's
            # confidence): green when the AI backs the model's side, red when
            # it fades it, neutral border for a Neutral verdict.
            _ocolor = {"pos": t.POS, "neg": t.NEG}.get(
                agreement_outline_token(ai_tier), t.BORDER)
            verdict_text = (bd.get("verdict") or "").strip()
            with ui.column().classes("w-full").style(
                f"background: {t.CARD}; border: 2px solid {_ocolor}; "
                f"border-radius: {t.RADIUS_MD}; "
                f"padding: {t.SPACE_MD}; gap: 6px;"
            ):
                with ui.row().classes("items-center w-full").style("gap: 8px;"):
                    ui.label("AI VERDICT").style(
                        f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                        f"color: {t.TEXT_DIM2};")
                    ui.label(label.upper()).style(
                        f"background: {vcolor}; color: {t.BG}; font-size: 10px; "
                        f"font-weight: 800; letter-spacing: .5px; padding: 2px 9px; "
                        f"border-radius: {t.RADIUS_PILL};")
                    _mv = (bd.get("model_version") or "").strip()
                    if _mv:
                        ui.label(_mv).style(
                            f"margin-left: auto; font-size: 9px; font-weight: 800; "
                            f"letter-spacing: .4px; color: {t.TEXT_DIM2}; "
                            f"background: {t.CARD_HI}; padding: 2px 7px; "
                            f"border-radius: {t.RADIUS_PILL}; font-family: monospace;"
                        ).tooltip("AI model version that produced this breakdown")
                if verdict_text:
                    ui.label(verdict_text).style(
                        f"font-size: 12.5px; color: {t.TEXT}; line-height: 1.5; "
                        f"white-space: normal;")

            # ── 2x2 insight grid ────────────────────────────────────────
            sections = [
                ("matchup",     "MATCHUP"),
                ("trends",      "TRENDS"),
                ("approach",    approach_label(is_pitcher)),
                ("game_script", "GAME SCRIPT"),
            ]
            with ui.element("div").classes("w-full grid-2col").style(
                "display: grid; grid-template-columns: 1fr 1fr; gap: 8px;"
            ):
                for key, lbl in sections:
                    text = (bd.get(key) or "").strip()
                    with ui.column().style(
                        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
                        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 4px; "
                        f"min-width: 0;"
                    ):
                        ui.label(lbl).style(
                            f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                            f"color: {t.PRIMARY_HI};")
                        ui.label(text or "—").style(
                            f"font-size: 11.5px; color: {t.TEXT_DIM}; line-height: 1.45; "
                            f"white-space: normal;")

    ui.timer(0.05, _load, once=True)


def _render_market_view(
    info: dict,
    games: list[dict],
    is_pitcher: bool,
    prop: dict,
    opp_abbrev: Optional[str],
    backend=None,
) -> None:
    """Renders one complete per-market view: player header, info row,
    AI breakdown, chart block, similar players."""
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
        _section_player_header(info, prop, opp_abbrev, prop=prop, grade=grade,
                               props_today=True, backend=backend)
        _section_info_row(opp_abbrev, prop)
        # AI-powered breakdown (replaces the old matchup-insights / matchup-tabs
        # / recent-trends sections).  Sits between the stat data and the chart.
        _section_ai_breakdown(
            info, games, is_pitcher, prop, market, line_f, summary, opp_abbrev,
        )
        _section_chart_block(
            games, is_pitcher, prop, market, line_f, summary, opp_abbrev,
        )
        _section_similar_players(info, prop, market, line_f, is_pitcher, opp_abbrev)


# ── Section: player header (per-market) ─────────────────────────────────────

def _section_player_header(
    info: dict,
    today_prop: Optional[dict],   # noqa: ARG001 (kept for compat with no-props path)
    opp_abbrev: Optional[str],    # noqa: ARG001
    *,
    prop: Optional[dict],
    grade: Optional[tuple[str, str]],
    props_today: bool = False,
    backend=None,
) -> None:
    """Card with headshot left, name+pos+team+line center, grade right.

    *props_today* drives the small status chip below the name: a green
    "Props available" when the player has props today, else a gray
    "No props today".  When *backend* and an active *prop* are supplied, a
    Track button is shown to the left of the grade (tracks that prop)."""
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

            # Status chip: green "Props available" / gray "No props today".
            _chip_text  = "Props available" if props_today else "No props today"
            _chip_color = t.POS if props_today else t.TEXT_DIM
            _chip_bg    = ("rgba(16, 185, 129, .12)" if props_today
                           else t.CARD_HI)
            ui.label(_chip_text).style(
                f"align-self: flex-start; margin-top: 2px; "
                f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                f"text-transform: uppercase; color: {_chip_color}; "
                f"background: {_chip_bg}; border: 1px solid {_chip_color}; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
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

        # Track button (Task 2): right of the player info block, left of the
        # grade.  Only when an active prop exists; tracks the headline prop.
        if (backend is not None and prop is not None
                and prop.get("recommendation") != "Pass"):
            _render_header_track(backend, info, prop)

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

def _opp_full_name(opp_abbrev: Optional[str], prop: Optional[dict]) -> Optional[str]:
    """Full opponent team name for the OPP box.  Prefers the prop's own
    home/away full names (matched to the abbrev); falls back to the static
    abbrev->name map; else the abbrev itself."""
    if not opp_abbrev:
        return None
    try:
        from src.player_profile_client import team_name_to_abbrev, team_abbrev_to_name
        for full in ((prop or {}).get("home_team"), (prop or {}).get("away_team")):
            if full and team_name_to_abbrev(full) == opp_abbrev:
                return full
        return team_abbrev_to_name(opp_abbrev) or opp_abbrev
    except Exception:                                                     # noqa: BLE001
        return opp_abbrev


def _section_info_row(opp_abbrev: Optional[str], prop: Optional[dict]) -> None:
    opp_full  = _opp_full_name(opp_abbrev, prop)
    opp_text  = f"@ {opp_full}" if opp_full else "—"
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

    def _windowed_games() -> list[dict]:
        f = _apply_window_filter(games, state["window"], opp_abbrev)
        return _apply_context_filter(
            f, state["context"], is_pitcher, opp_abbrev=opp_abbrev,
        )

    def _windowed_avg() -> Optional[float]:
        """Mean of the active stat over the games matching the current
        window + context filter -- so the badge tracks the selected
        time-filter pill (L5/L10/L20/Season/H2H) instead of always the
        season average."""
        vals = [g.get(stat_key) for g in _windowed_games()
                if isinstance(g.get(stat_key), (int, float))]
        return (sum(vals) / len(vals)) if vals else None

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 10px;"
    ):
        # AVG badge -- average for the ACTIVE window vs the line (FIX 1).
        @ui.refreshable
        def render_badge() -> None:                                       # noqa: WPS430
            _section_avg_badge(_windowed_avg(), line_f, stat_key,
                               window_tag=_WINDOW_TAG.get(state["window"], ""))

        render_badge()

        @ui.refreshable
        def render_chart() -> None:                                       # noqa: WPS430
            filtered = _windowed_games()
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
            )).classes("player-chart").style("width: 100%; height: 250px;")

        with ui.column().classes("w-full").style("gap: 6px;"):
            render_chart()

        def _on_window_change() -> None:                                  # noqa: WPS430
            render_badge.refresh()
            render_chart.refresh()

        _render_time_pills_with_rates(
            summary, line_f, side, state,
            on_change=_on_window_change,
            ctx_options=_stat_context_options(is_pitcher, opp_abbrev),
        )


def _section_avg_badge(
    avg_value: Optional[float],
    line_f: Optional[float],
    stat_key: str,
    *,
    window_tag: str = "",
) -> None:
    """Small pill: '{tag} AVG 1.4 H', coloured against the line.  *avg_value*
    is the average for the currently-active time window (FIX 1), and
    *window_tag* (e.g. 'L5', 'SZN') prefixes the label so it's clear which
    window the average reflects."""
    if avg_value is None:
        return
    if line_f is None:
        color = t.TEXT_DIM
    elif avg_value >= line_f:
        color = t.POS
    else:
        color = t.NEG
    stat_label = stat_key if stat_key else ""
    prefix = f"{window_tag} " if window_tag else ""
    ui.label(f"{prefix}AVG {avg_value:.2f} {stat_label}".strip()).style(
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

    with ui.row().classes("items-center w-full touch-44h").style(
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
        with ui.element("div").classes("w-full h2h-grid").style(
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
                )).classes("player-chart").style("width: 100%; height: 96px; min-width: 0;")
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
        with ui.element("div").classes("w-full grid-2col").style(
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


# ── Section: similar players (prop-market specific) ─────────────────────────

def _section_similar_players(
    info: dict,
    prop: dict,
    market: str,
    line_f: Optional[float],
    is_pitcher: bool,
    opp_abbrev: Optional[str],
) -> None:
    """Show 3-5 players in the same similarity cluster for THIS market,
    each with how they performed *against this prop's opponent team* using
    the prop's own stat (Ks for a strikeouts prop, ER for an earned-runs
    prop, etc.) over the last ~3 seasons -- not their general recent form.

    The cluster lookup (KMeans) is unchanged; only the displayed stat is.
    The per-player matchup enrichment (last-3-season gamelogs filtered to
    the opponent) is lazy-loaded on a background thread so it never blocks
    the initial render.  Built as a single ui.html() table.
    """
    from pages.props import _short_market
    from src.utils import strip_formatting
    player_name  = info.get("name") or ""
    market_label = _short_market(market)
    opp_u        = (opp_abbrev or "").strip().upper()

    try:
        from src.player_similarity import get_similar_players
        sims = get_similar_players(market, player_name, limit=5)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"similar lookup failed: {exc}")
        sims = []

    def _note(msg: str):
        return ui.label(msg).style(
            f"font-size: 11.5px; color: {t.TEXT_DIM2}; font-style: italic; "
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: 14px; "
            f"text-align: center; width: 100%;"
        )

    def _title(txt: str):
        ui.label(txt).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )

    # ── Pitchers: two Past-Games-style tables SIDE BY SIDE (50/50) in one
    #    ui.html() block -- Box 1 similar pitchers vs opponent (flat, newest
    #    first, ≤5); Box 2 the last 5 pitchers (any) to face the opponent.
    #    Batters keep the existing single per-opponent summary table. ───────
    if is_pitcher:
        stat_key = _MARKET_TO_STAT.get(market) or "K"
        opp_full = _opp_full_name(opp_abbrev, prop) or opp_u
        holder = ui.column().classes("w-full").style("gap: 0; min-width: 0;")
        with holder:
            ui.label("Loading matchup history…").style(
                f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                f"font-style: italic; padding: 8px 4px;"
            )

        async def _load_pitcher() -> None:                               # noqa: WPS430
            try:
                data = await asyncio.to_thread(
                    _build_pitcher_sections, sims, market, stat_key, opp_u,
                )
            except Exception as exc:                                      # noqa: BLE001
                _log(f"pitcher similar build failed: {exc}")
                data = {"similar": [], "recent": []}
            holder.clear()
            with holder:
                # w-full so the ui.html element itself stretches to the
                # holder's full width -- otherwise it shrinks to content and
                # the inner 50/50 flex leaves a gap beside the boxes.
                ui.html(_pitcher_dual_box_html(
                    data["similar"], data["recent"], stat_key, opp_u, opp_full
                )).classes("w-full").style("width: 100%;")

        ui.timer(0.05, _load_pitcher, once=True)
        return

    # ── Batters: existing per-opponent summary table (unchanged). ─────────
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        _title(f"SIMILAR BY {market_label.upper()} — VS {opp_u}"
               if opp_u else f"SIMILAR BY {market_label.upper()}")

        if not sims:
            _note("Not enough data to compare players for this market yet.")
            return
        if not opp_u:
            _note("Opponent unknown for this game — no head-to-head matchup data.")
            return

        body = ui.column().classes("w-full").style("gap: 8px;")
        with body:
            ui.label("Loading matchup history…").style(
                f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                f"font-style: italic; padding: 8px 4px;"
            )

        async def _load() -> None:                                        # noqa: WPS430
            try:
                enriched = await asyncio.to_thread(
                    _enrich_similar_vs_opp, sims, market, is_pitcher, opp_u,
                )
            except Exception as exc:                                      # noqa: BLE001
                _log(f"similar enrich failed: {exc}")
                enriched = []
            body.clear()
            with body:
                if not enriched:
                    _note("Similar player data unavailable right now.")
                    return
                ui.html(_similar_vs_opp_table_html(enriched, opp_u, is_pitcher))

        ui.timer(0.05, _load, once=True)


# Number of seasons (including the current one) of game logs to scan for
# head-to-head matchups against the opponent team.
_SIMILAR_H2H_SEASONS = 3


def _enrich_similar_vs_opp(
    sims: list[dict], market: str, is_pitcher: bool, opp_u: str,
) -> list[dict]:
    """Background-thread enrichment.  For each similar player, compute their
    prop-matching stat against the opponent team (opp_u) across the last
    ~3 seasons of game logs.  Returns per-player {name, team, score,
    vs_avg, vs_games}.  Cached gamelog reads only -- no new data source."""
    from src.player_profile_client import (
        get_player_gamelog, gamelog_stat_value, _CURRENT_SEASON,
    )
    stat_key = _MARKET_TO_STAT.get(market) or ("K" if is_pitcher else "H")
    seasons  = [_CURRENT_SEASON - i for i in range(_SIMILAR_H2H_SEASONS)]

    out: list[dict] = []
    for s in sims:
        pid = s.get("id")
        vs_values: list[float] = []
        if pid:
            games: list[dict] = []
            for yr in seasons:
                try:
                    games.extend(
                        get_player_gamelog(int(pid), yr, is_pitcher=is_pitcher) or []
                    )
                except Exception:                                         # noqa: BLE001
                    continue
            if is_pitcher:
                games = [g for g in games if g.get("games_started", 0) > 0]
            vs_games = [g for g in games
                        if (g.get("opp") or "").strip().upper() == opp_u]
            vs_values = [gamelog_stat_value(g, stat_key) for g in vs_games]

        out.append({
            "name":     s.get("name") or "—",
            "team":     s.get("team") or "",
            "score":    s.get("score"),
            "vs_avg":   (sum(vs_values) / len(vs_values)) if vs_values else None,
            "vs_games": len(vs_values),
            "stat_key": stat_key,
        })
    return out


def _similar_vs_opp_table_html(
    rows: list[dict], opp_u: str, is_pitcher: bool,
) -> str:
    """Render the similar-players-vs-opponent table as a single HTML string.

    Each row: player name (+ team) | prop stat per game vs the opponent over
    the last ~3 seasons | sample size ('3 GS vs NYM').  Thin/empty samples
    are labelled explicitly -- a missing matchup shows 'No starts vs TEAM'
    (never a blank/zero), and a single-game sample is rendered muted with a
    '1 GS' count so it never reads as a trend.
    """
    import html as _html

    game_word  = "GS" if is_pitcher else "G"
    none_label = (f"No starts vs {opp_u}" if is_pitcher
                  else f"No games vs {opp_u}")
    stat_key   = (rows[0].get("stat_key") if rows else "") or ""

    head = (
        f'<thead><tr>'
        f'<th style="text-align:left;padding:7px 10px;font-size:8.5px;'
        f'font-weight:800;letter-spacing:.5px;color:{t.TEXT_DIM2};">PLAYER</th>'
        f'<th style="text-align:right;padding:7px 10px;font-size:8.5px;'
        f'font-weight:800;letter-spacing:.5px;color:{t.TEXT_DIM2};">'
        f'{_html.escape(stat_key)}/G VS {_html.escape(opp_u)}</th>'
        f'<th style="text-align:right;padding:7px 10px;font-size:8.5px;'
        f'font-weight:800;letter-spacing:.5px;color:{t.TEXT_DIM2};">SAMPLE</th>'
        f'</tr></thead>'
    )

    body_rows: list[str] = []
    for s in rows:
        name = _html.escape(s.get("name") or "—")
        team = _html.escape(s.get("team") or "")
        n    = int(s.get("vs_games") or 0)
        avg  = s.get("vs_avg")

        team_html = (
            f'<span style="font-size:10px;color:{t.TEXT_DIM2};'
            f'font-family:monospace;margin-left:6px;">{team}</span>'
            if team else ""
        )
        name_cell = (
            f'<td style="text-align:left;padding:9px 10px;font-size:13px;'
            f'font-weight:800;color:{t.TEXT};white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis;max-width:160px;">{name}{team_html}</td>'
        )

        if n == 0 or not isinstance(avg, (int, float)):
            # Empty sample -> explicit label, never a blank or zero.
            stat_cell = (
                f'<td colspan="2" style="text-align:right;padding:9px 10px;'
                f'font-size:11.5px;font-style:italic;color:{t.TEXT_DIM2};">'
                f'{_html.escape(none_label)}</td>'
            )
            body_rows.append(f'<tr>{name_cell}{stat_cell}</tr>')
            continue

        # 1-game samples are muted so a single outing never reads as a trend
        # (the SAMPLE count makes the one-game basis explicit either way).
        thin       = n == 1
        stat_color = t.TEXT_DIM if thin else t.TEXT
        smpl_color = t.WARN if thin else t.TEXT_DIM
        stat_cell = (
            f'<td style="text-align:right;padding:9px 10px;font-size:13px;'
            f'font-weight:800;font-family:monospace;color:{stat_color};">'
            f'{avg:.1f}</td>'
        )
        smpl_cell = (
            f'<td style="text-align:right;padding:9px 10px;font-size:11px;'
            f'font-weight:700;font-family:monospace;color:{smpl_color};'
            f'white-space:nowrap;">{n} {game_word} vs {_html.escape(opp_u)}</td>'
        )
        body_rows.append(f'<tr>{name_cell}{stat_cell}{smpl_cell}</tr>')

    rows_html = "".join(
        # zebra striping via inline style on each <tr>'s cells is awkward;
        # keep a flat look matching the existing card aesthetic.
        r for r in body_rows
    )
    return (
        f'<table style="width:100%;border-collapse:collapse;'
        f'background:{t.CARD};border:1px solid {t.BORDER};'
        f'border-radius:{t.RADIUS_MD};overflow:hidden;">'
        f'{head}<tbody>{rows_html}</tbody></table>'
    )


# ── Pitcher Past-Games-style sections (Section 1 vs opponent + Section 2 L5) ──

def _prop_history_index() -> dict:
    """Index the saved prop-pick history by (player_lower, market, date) so a
    game row can look up the line + odds it carried that day.  Read-only; no
    new data source.  Returns {} on any failure."""
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        idx: dict[tuple, dict] = {}
        for p in (_ppt.get_all() or []):
            player = (p.get("player") or "").strip().lower()
            market = p.get("market")
            date10 = (p.get("date") or "")[:10]
            if player and market and date10:
                idx[(player, market, date10)] = p
        return idx
    except Exception as exc:                                              # noqa: BLE001
        _log(f"prop-history index failed: {exc}")
        return {}


def _pitcher_game_rows(pid, name: str, market: str, stat_key: str,
                       hist_idx: dict, *, opp_u: Optional[str] = None,
                       last_n: Optional[int] = None) -> list[dict]:
    """Build per-game rows for one pitcher from the cached gamelog (last ~3
    seasons of starts).  Each row: {date, opp, is_home, stat, line, odds}.
    Optionally filter to games vs *opp_u*, and/or keep only the last *last_n*
    (most recent first).  Line/odds joined from the saved prop history; None
    when nothing was stored for that game."""
    from src.player_profile_client import (
        get_player_gamelog, gamelog_stat_value, _CURRENT_SEASON,
    )
    if not pid:
        return []
    games: list[dict] = []
    for yr in [_CURRENT_SEASON - i for i in range(_SIMILAR_H2H_SEASONS)]:
        try:
            games.extend(get_player_gamelog(int(pid), yr, is_pitcher=True) or [])
        except Exception:                                                 # noqa: BLE001
            continue
    games = [g for g in games if g.get("games_started", 0) > 0]
    if opp_u:
        games = [g for g in games if (g.get("opp") or "").strip().upper() == opp_u]
    # Most-recent first by date.
    games.sort(key=lambda g: g.get("date", ""), reverse=True)
    if last_n is not None:
        games = games[:last_n]

    player_l = (name or "").strip().lower()
    rows: list[dict] = []
    for g in games:
        date10 = (g.get("date") or "")[:10]
        hist = hist_idx.get((player_l, market, date10)) or {}
        rows.append({
            "name":    name,
            "date":    g.get("date"),
            "opp":     g.get("opp"),
            "is_home": bool(g.get("is_home")),
            "stat":    gamelog_stat_value(g, stat_key),
            "line":    hist.get("line"),
            "odds":    hist.get("odds"),
        })
    return rows


def _stat_for_pitcher_game(pid, name: str, date10: str, stat_key: str):
    """The pitcher's *stat_key* value in their game on *date10*, from the
    cached gamelog (this + last season).  None when not found."""
    from src.player_profile_client import (
        get_player_gamelog, gamelog_stat_value, _CURRENT_SEASON,
    )
    if not pid or not date10:
        return None
    try:
        yr = int(date10[:4])
    except (TypeError, ValueError):
        yr = _CURRENT_SEASON
    for season in (yr, yr - 1):
        try:
            games = get_player_gamelog(int(pid), season, is_pitcher=True) or []
        except Exception:                                                 # noqa: BLE001
            continue
        g = next((x for x in games if (x.get("date") or "")[:10] == date10), None)
        if g:
            return gamelog_stat_value(g, stat_key)
    return None


def _build_pitcher_sections(sims: list[dict], market: str,
                            stat_key: str, opp_u: str) -> dict:
    """Background-thread builder.  Returns:
      similar -> ONE flat, newest-first list (≤5) of the similar pitchers'
                 games vs opp_u (not grouped by pitcher).
      recent  -> the last 5 pitchers (any) to face opp_u, newest first.
    """
    hist_idx = _prop_history_index()

    # Box 1 -- flatten every similar pitcher's vs-opp starts into one list.
    similar: list[dict] = []
    if opp_u:
        for s in sims:
            similar.extend(_pitcher_game_rows(
                s.get("id"), s.get("name") or "", market, stat_key,
                hist_idx, opp_u=opp_u,
            ))
        similar.sort(key=lambda r: (r.get("date") or ""), reverse=True)
        similar = similar[:5]

    # Box 2 -- the most recent pitchers to face this opponent (any pitcher).
    recent: list[dict] = []
    if opp_u:
        try:
            from src.player_profile_client import get_recent_pitchers_vs_team
            for rp in get_recent_pitchers_vs_team(opp_u, limit=5):
                date10 = rp.get("date") or ""
                stat = _stat_for_pitcher_game(
                    rp.get("player_id"), rp.get("name") or "", date10, stat_key)
                hist = hist_idx.get(
                    ((rp.get("name") or "").strip().lower(), market, date10)) or {}
                recent.append({
                    "name":    rp.get("name") or "—",
                    "date":    date10,
                    "opp":     opp_u,
                    "is_home": bool(rp.get("is_home_pitcher")),
                    "stat":    stat,
                    "line":    hist.get("line"),
                    "odds":    hist.get("odds"),
                })
        except Exception as exc:                                          # noqa: BLE001
            _log(f"recent opp pitchers failed: {exc}")
    return {"similar": similar, "recent": recent}


def _pg_date_opp(row: dict) -> str:
    import html as _html
    d   = _fmt_short_date(row.get("date")) or "—"
    opp = (row.get("opp") or "").strip()
    if opp:
        prefix = "vs" if row.get("is_home") else "@"
        return f"{_html.escape(d)} {prefix}&nbsp;{_html.escape(opp)}"
    return _html.escape(d)


def _pg_stat_cell(row: dict) -> str:
    """The prop-matching stat as the result number, coloured green if it beat
    that game's line / red if it missed (amber on a push, neutral when no line
    was stored)."""
    v    = row.get("stat")
    line = row.get("line")
    if not isinstance(v, (int, float)):
        txt, color = "—", t.TEXT_DIM
    else:
        txt = f"{v:g}"
        if isinstance(line, (int, float)):
            color = t.POS if v > line else t.NEG if v < line else t.WARN
        else:
            color = t.TEXT          # no line stored -> can't grade -> neutral
    return (
        f'<td style="text-align:right;padding:7px 10px;font-size:13px;'
        f'font-weight:800;font-family:monospace;color:{color};'
        f'border-bottom:1px solid {t.BORDER_SOFT};">{txt}</td>'
    )


def _pg_plain_cell(text: str, *, color: str = None, dim: bool = False,
                   font_px: int = 12, min_px: int | None = None) -> str:
    color = color or (t.TEXT_DIM2 if dim else t.TEXT)
    mw = f"min-width:{min_px}px;" if min_px else ""
    return (
        f'<td style="text-align:right;padding:7px 10px;font-size:{font_px}px;'
        f'font-family:monospace;color:{color};'
        f'border-bottom:1px solid {t.BORDER_SOFT};white-space:nowrap;{mw}">{text}</td>'
    )


def _pg_line_str(row: dict) -> str:
    line = row.get("line")
    return f"{line:g}" if isinstance(line, (int, float)) else "N/A"


def _pg_odds_str(row: dict) -> str:
    o = row.get("odds")
    try:
        return f"{int(o):+d}"
    except (TypeError, ValueError):
        return "N/A"


def _pg_header(stat_key: str, mobile: bool = False) -> str:
    import html as _html
    # white-space:nowrap on every <th> in both layouts so column labels never
    # wrap.  ODDS/LINE get a 48px floor in both; PITCHER gets an 80px floor on
    # mobile (where the table uses auto layout instead of the fixed colgroup).
    th = (f"font-size:8.5px;font-weight:800;letter-spacing:.5px;"
          f"color:{t.TEXT_DIM2};padding:7px 10px;border-bottom:1px solid {t.BORDER};"
          f"white-space:nowrap;")
    pitcher_th = th + ("min-width:80px;" if mobile else "")
    num_th = th + "min-width:48px;"
    return (
        f'<thead><tr>'
        f'<th style="{th}text-align:left;">DATE / OPP</th>'
        f'<th style="{pitcher_th}text-align:left;">PITCHER</th>'
        f'<th style="{th}text-align:right;">{_html.escape(stat_key)}</th>'
        f'<th style="{num_th}text-align:right;">LINE</th>'
        f'<th style="{num_th}text-align:right;">ODDS</th>'
        f'</tr></thead>'
    )


def _pg_game_tr(row: dict, mobile: bool = False) -> str:
    import html as _html
    if mobile:
        # Mobile (stacked, auto layout): DATE/OPP wraps instead of truncating,
        # PITCHER keeps an 80px floor, content sits at 13px.
        date_cell = (
            f'<td style="text-align:left;padding:7px 10px;font-size:13px;'
            f'font-family:monospace;color:{t.TEXT_DIM};'
            f'border-bottom:1px solid {t.BORDER_SOFT};white-space:normal;">'
            f'{_pg_date_opp(row)}</td>'
        )
        name_cell = (
            f'<td style="text-align:left;padding:7px 10px;font-size:13px;'
            f'font-weight:700;color:{t.TEXT};border-bottom:1px solid {t.BORDER_SOFT};'
            f'min-width:80px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
            f'{_html.escape(row.get("name") or "—")}</td>'
        )
        font_px = 13
    else:
        # Desktop (side-by-side, table-layout:fixed): Date/Opp + Pitcher
        # truncate so a long name never forces the table past its 50% track.
        date_cell = (
            f'<td style="text-align:left;padding:7px 10px;font-size:12px;'
            f'font-family:monospace;color:{t.TEXT_DIM};'
            f'border-bottom:1px solid {t.BORDER_SOFT};white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis;">{_pg_date_opp(row)}</td>'
        )
        name_cell = (
            f'<td style="text-align:left;padding:7px 10px;font-size:12px;'
            f'font-weight:700;color:{t.TEXT};border-bottom:1px solid {t.BORDER_SOFT};'
            f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">'
            f'{_html.escape(row.get("name") or "—")}</td>'
        )
        font_px = 12
    return (
        f'<tr>{date_cell}{name_cell}{_pg_stat_cell(row)}'
        f'{_pg_plain_cell(_pg_line_str(row), font_px=font_px, min_px=48)}'
        f'{_pg_plain_cell(_pg_odds_str(row), dim=True, font_px=font_px, min_px=48)}</tr>'
    )


def _pitcher_games_table_html(rows: list[dict], stat_key: str,
                              mobile: bool = False) -> str:
    """A flat Past-Games table -- identical 5-column layout in both boxes:
    DATE/OPP · PITCHER · stat · LINE · ODDS, newest first.

    Desktop: table-layout:fixed + an explicit <colgroup> keeps the two
    side-by-side boxes aligned column-for-column and makes long names/teams
    truncate rather than push the table past its 50% grid track.

    Mobile: each table is full-width and stacked, so it uses table-layout:auto
    (no colgroup) -- that lets the per-column min-widths apply and the DATE/OPP
    cell wrap instead of being clipped to a fixed track."""
    body = "".join(_pg_game_tr(r, mobile=mobile) for r in rows)
    base = (f'background:{t.CARD};border:1px solid {t.BORDER};'
            f'border-radius:{t.RADIUS_MD};overflow:hidden;')
    if mobile:
        return (
            f'<table style="width:100%;table-layout:auto;border-collapse:collapse;{base}">'
            f'{_pg_header(stat_key, mobile=True)}<tbody>{body}</tbody></table>'
        )
    colgroup = (
        '<colgroup>'
        '<col style="width:30%;"><col style="width:31%;">'   # Date/Opp, Pitcher
        '<col style="width:13%;"><col style="width:13%;">'   # stat, Line
        '<col style="width:13%;">'                            # Odds
        '</colgroup>'
    )
    return (
        f'<table style="width:100%;table-layout:fixed;border-collapse:collapse;{base}">'
        f'{colgroup}{_pg_header(stat_key)}<tbody>{body}</tbody></table>'
    )


def _pg_heading_html(text: str) -> str:
    import html as _html
    return (
        f'<div style="font-size:10px;font-weight:800;letter-spacing:.8px;'
        f'color:{t.TEXT_DIM2};margin-bottom:6px;">{_html.escape(text)}</div>'
    )


def _pg_note_html(msg: str) -> str:
    import html as _html
    return (
        f'<div style="font-size:11.5px;color:{t.TEXT_DIM2};font-style:italic;'
        f'background:{t.CARD};border:1px solid {t.BORDER};'
        f'border-radius:{t.RADIUS_MD};padding:14px;text-align:center;">'
        f'{_html.escape(msg)}</div>'
    )


def _pitcher_dual_box_html(similar_rows: list[dict], recent_rows: list[dict],
                           stat_key: str, opp_u: str, opp_full: str) -> str:
    """Both pitcher boxes SIDE BY SIDE (50/50) as a single ui.html() string,
    spanning the full width under the chart edge to edge.

    Box 1 — similar pitchers vs the opponent (flat, newest first, ≤5).
    Box 2 — the last 5 pitchers (any) to face the opponent, newest first.

    Two renders share one data set: a `.desktop-only` 50/50 CSS grid (the
    original side-by-side layout) and a `.mobile-only` stacked column (Similar
    on top, Last 5 below, each full width).  The global .mobile-only /
    .desktop-only utilities (components/theme.py, 768px breakpoint) pick which
    one the browser shows -- no data or logic differs between them.

    The desktop grid uses `minmax(0, 1fr)` tracks (not flex): the two columns
    are exactly 50/50 and fill 100% regardless of cell content, so there's no
    empty gap beside them and a long pitcher name can't blow out a column.
    """
    team = opp_full or opp_u or "OPP"

    def _boxes(mobile: bool) -> tuple[str, str]:
        if not opp_u:
            b1 = _pg_heading_html("SIMILAR PITCHERS VS OPPONENT") + \
                _pg_note_html("Opponent unknown for this game — no head-to-head data.")
            b2 = _pg_heading_html("LAST 5 PITCHERS VS OPPONENT") + \
                _pg_note_html("Opponent unknown for this game.")
        else:
            b1 = _pg_heading_html(f"SIMILAR PITCHERS VS {team}") + (
                _pitcher_games_table_html(similar_rows, stat_key, mobile=mobile)
                if similar_rows
                else _pg_note_html(f"No similar pitchers have faced {team}."))
            b2 = _pg_heading_html(f"LAST 5 PITCHERS VS {team}") + (
                _pitcher_games_table_html(recent_rows, stat_key, mobile=mobile)
                if recent_rows
                else _pg_note_html(f"No recent pitchers found vs {team}."))
        return b1, b2

    d1, d2 = _boxes(mobile=False)
    desktop = (
        '<div class="desktop-only" style="grid-template-columns:repeat(2,minmax(0,1fr));'
        'gap:10px;width:100%;box-sizing:border-box;align-items:start;display:grid;">'
        f'<div style="min-width:0;">{d1}</div>'
        f'<div style="min-width:0;">{d2}</div>'
        '</div>'
    )

    m1, m2 = _boxes(mobile=True)
    mobile = (
        '<div class="mobile-only" style="flex-direction:column;'
        'gap:14px;width:100%;box-sizing:border-box;">'
        f'<div style="width:100%;min-width:0;">{m1}</div>'
        f'<div style="width:100%;min-width:0;">{m2}</div>'
        '</div>'
    )

    return desktop + mobile


def _fmt_short_date(iso: Optional[str]) -> str:
    """'2026-04-12' -> 'Apr 12'.  Empty string on any parse failure."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %-d")
    except Exception:                                                     # noqa: BLE001
        return ""


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

def _game_log_table(
    games: list[dict],
    is_pitcher: bool,
    highlighted_stat: str,
    line: Optional[float] = None,
) -> None:
    """Single ui.html block -- avoids NiceGUI's per-element div wrapping
    that would otherwise break <tr>/<td> nesting.

    When *line* is given, the highlighted stat column is coloured per row
    against that book line (FIX 6): green if the value exceeded the line,
    red if it fell short, amber on an exact push -- instead of a flat green."""
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

    def _td(col: str, value=None, extra: str = "") -> str:
        highlighted = col_stat_map.get(col) == hl
        if highlighted and line is not None and isinstance(value, (int, float)):
            # Per-row over/under colouring vs the active market's book line.
            if value > line:
                color = t.POS          # exceeded the line
            elif value < line:
                color = t.NEG          # fell short
            else:
                color = t.WARN         # exact push
            weight = "800"
        elif highlighted:
            color  = t.POS             # highlighted column, no line known
            weight = "800"
        else:
            color  = t.TEXT
            weight = "400"
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
                    raw_val = g.get(raw_key)
                    cells += f"<td style='{_td(col, raw_val)}'>{_safe_num(raw_val)}</td>"
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
