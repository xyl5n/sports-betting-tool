"""
props.py
========
MLB player-props page.

Layout
------
1. Page title
2. Per-bucket model record (pitcher + batter W-L)
3. Filter bar -- game / market / minimum hit rate
4. Unified pick list, sorted by confidence DESC, with each card showing
   inline performance chips (season avg, L5/L10/L20 hit rate, H2H) and
   the opposing team's rank against that prop's market.

The pitcher/batter split that the prior version used is gone -- the
filter bar lets the user narrow the slate to whatever cross-section
they want (a single team, a single market, a hit-rate floor), so a
hard-coded split adds noise without helping discovery.
"""
from __future__ import annotations

import asyncio
import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


def _dbg(msg: str) -> None:
    """Tagged stderr log -- mirrors home.py's _dbg pattern."""
    print(f"[RENDER] {msg}", flush=True, file=sys.stderr)


def register(backend) -> None:
    @ui.page("/props")
    def props_page():
        _dbg("props_page ENTER")
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_PROPS)
            _layout(backend)
            bottom_nav.render(active=t.TAB_PROPS)
        except Exception as exc:                                          # noqa: BLE001
            import traceback as _tb
            tb_str = _tb.format_exc()
            print(
                f"[PROPS PAGE FATAL] {type(exc).__name__}: {exc}\n{tb_str}",
                flush=True, file=sys.stderr,
            )
            ui.label("Props page render failed").style(
                f"color: {t.NEG}; font-size: 16px; font-weight: 700; "
                f"padding: {t.SPACE_LG};"
            )
            ui.label(f"{type(exc).__name__}: {exc}").style(
                f"color: {t.TEXT_DIM}; font-family: monospace; "
                f"font-size: 12px; padding: 0 {t.SPACE_LG};"
            )


def _layout(backend) -> None:
    with ui.column().classes("page-content w-full").style(
        f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
        f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
    ):
        ui.label("PLAYER PROPS").classes("page-title").style(
            f"font-size: 22px; font-weight: 800; color: {t.TEXT};"
        )

        _section_unified_props_list(backend)
        # Model trackers moved to the bottom, below all the pick cards.
        _section_model_record(backend)
        _settle_trigger(backend)


# ── Model record ────────────────────────────────────────────────────────────

def _section_model_record(backend) -> None:
    """Two side-by-side cards showing each bucket's W-L + win%."""
    try:
        from src.props_model import get_record
        pitcher_rec = get_record("pitcher")
        batter_rec  = get_record("batter")
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props model record load failed: {exc}")
        pitcher_rec = batter_rec = {"wins": 0, "losses": 0, "total": 0, "pct": None}

    with ui.row().classes("w-full").style(
        f"gap: {t.SPACE_SM}; flex-wrap: nowrap;"
    ):
        _record_card("PITCHER MODEL", pitcher_rec)
        _record_card("BATTER MODEL",  batter_rec)


def _record_card(label: str, rec: dict) -> None:
    w, l, total = rec["wins"], rec["losses"], rec["total"]
    pct = rec["pct"]
    if pct is None:
        pct_s, pct_col = "—", t.TEXT_DIM2
    else:
        pct_s = f"{pct * 100:.1f}%"
        pct_col = t.POS if pct >= 0.55 else (t.NEG if pct < 0.50 else t.TEXT_DIM)
    with ui.column().style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
        f"gap: 4px; flex: 1 1 0; min-width: 0;"
    ):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(f"{w}-{l}").style(
            f"font-size: 18px; font-weight: 800; color: {t.TEXT}; "
            f"font-family: monospace;"
        )
        ui.label(f"{pct_s}  ({total} settled)").style(
            f"font-size: 11px; font-weight: 600; color: {pct_col}; "
            f"font-family: monospace;"
        )


# ── Unified props list (pitcher + batter combined, filter bar) ───────────────

def _section_unified_props_list(backend) -> None:
    """Render the pre-scored, pre-enriched picks from the props cache.

    Pure display layer -- this function NEVER calls predict() and never
    fetches gamelogs or opponent ranks.  The scoring + enrichment
    pipeline runs in the background scheduler (see
    src.props_scored_cache.score_today_props, wired into
    run_tier_1_refresh / run_tier_2_refresh).  If the scheduler hasn't
    populated the cache yet (e.g. before 11 AM ET on a fresh deploy)
    we render an empty-state message; we do NOT trigger a re-score
    from the page, which would block NiceGUI's event loop and drop
    the browser connection on slates with thousands of props.
    """
    try:
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props cache import failed: {exc}")
        return

    cache = load_scored_props() or {}
    rows: list[dict] = list(cache.get("picks") or [])
    rows.sort(key=lambda r: -float(r.get("confidence") or 0.0))

    _dbg(
        f"[PROPS-PAGE] read scored cache: {len(rows)} picks "
        f"(generated_at={cache.get('generated_at')})"
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        # Header row -- title + count + last refresh
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("PICKS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"{len(rows)} picks").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if cache.get("generated_at"):
                ui.label(f"scored {_short_iso(cache['generated_at'])}").style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                    f"margin-left: auto; font-family: monospace;"
                )

        if not rows:
            _empty_state_message()
            return

        # ── Sort pills ───────────────────────────────────────────────────
        # Single row of 6 pills (L5 / L10 / L20 / H2H / Proj / Edge).
        # Only the sort order changes -- every pick stays visible -- so
        # the count chip above is static.  Default sort is Proj (biggest
        # model-vs-book gap first).
        state = {"sort": "proj"}

        # ── Card list (refreshable so the sort re-renders in place) ──────
        @ui.refreshable
        def cards_refresh() -> None:                                      # noqa: WPS430
            shown = _sorted_picks(rows, state)
            with ui.element("div").classes("game-grid w-full"):
                for r in shown:
                    _prop_card(r, backend)

        _sort_pills(state, cards_refresh.refresh)
        cards_refresh()


def _empty_state_message() -> None:
    """Friendly placeholder shown when the scored-cache is empty.

    Two flavours: pre-window (before 11 AM ET) we tell the user to
    come back when scoring starts; in-window we explain a refresh is
    in flight.  Either way we DO NOT trigger scoring from the page --
    that's a scheduler-only concern.
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        pre_window = now_et.hour < 11
    except Exception:                                                     # noqa: BLE001
        pre_window = False

    if pre_window:
        title = "Props loading"
        body  = "Check back after 11 AM ET — scoring runs every 15 minutes during game hours."
    else:
        title = "Props refreshing"
        body  = "The next scored batch will appear here within a few minutes."

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL} {t.SPACE_LG}; "
        f"gap: 6px; align-items: center;"
    ):
        ui.label(title).style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label(body).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; text-align: center;"
        )


# ── Sort pills ───────────────────────────────────────────────────────────────
# A single row of six mutually-exclusive pills.  Each is a SORT (every
# pick stays visible -- only the order changes).  Default is "proj".

_SORT_PILLS: tuple[tuple[str, str], ...] = (
    ("l5",   "L5"),
    ("l10",  "L10"),
    ("l20",  "L20"),
    ("h2h",  "H2H"),
    ("proj", "Proj"),
    ("edge", "Edge"),
)


def _sort_pills(state: dict, on_change) -> None:
    """Render the six sort pills in one horizontal row, no title label.
    Active pill is highlighted green; clicking sets state['sort'] and
    refreshes the card list."""
    @ui.refreshable
    def render() -> None:                                                 # noqa: WPS430
        with ui.row().classes("items-stretch w-full").style(
            "gap: 6px; flex-wrap: nowrap;"
        ):
            for key, label in _SORT_PILLS:
                _sort_pill(label, key, state.get("sort") == key,
                           state, on_change, render.refresh)
    render()


def _sort_pill(label: str, key: str, active: bool, state: dict,
               on_change, refresh) -> None:
    if active:
        bg, fg, border = t.POS, t.BG, t.POS
        shadow = "0 0 12px rgba(16, 185, 129, .35)"
    else:
        bg, fg, border = "transparent", t.TEXT_DIM, t.BORDER
        shadow = "none"

    def _click() -> None:
        state["sort"] = key
        on_change()
        refresh()

    ui.button(label, on_click=_click).props("no-caps unelevated dense").style(
        f"background: {bg} !important; color: {fg} !important; "
        f"border: 1px solid {border}; flex: 1 1 0; min-width: 0; "
        f"min-height: 32px; padding: 4px 0; "
        f"font-size: 11.5px; font-weight: 800; letter-spacing: .4px; "
        f"border-radius: {t.RADIUS_PILL}; box-shadow: {shadow} !important;"
    )


# ── Sorting ───────────────────────────────────────────────────────────────────

def _window_hit_rate(r: dict, window: str) -> float:
    """Hit rate (0..1) for a summary window key prefix.  Returns -1.0
    when the pick has no games in that window so it sorts last."""
    s = r.get("summary") or {}
    hits  = s.get(f"{window}_hits") or 0
    games = s.get(f"{window}_games") or 0
    if not games:
        return -1.0
    return hits / games


def _proj_gap(r: dict) -> float:
    """Absolute gap between model projection and the book line.  The
    bigger the gap, the more the model disagrees with the book.
    Returns -1.0 when either value is missing so it sorts last."""
    pv   = r.get("predicted_value")
    line = r.get("line")
    if pv is None or line is None:
        return -1.0
    try:
        return abs(float(pv) - float(line))
    except (TypeError, ValueError):
        return -1.0


def _edge_ev(r: dict) -> float:
    """EV% for the pick.  Missing EV sorts to the very bottom."""
    ev = r.get("ev_pct")
    if ev is None:
        return float("-inf")
    try:
        return float(ev)
    except (TypeError, ValueError):
        return float("-inf")


def _sorted_picks(rows: list[dict], state: dict) -> list[dict]:
    """Return *rows* ordered by the active sort pill, highest first."""
    sort = state.get("sort") or "proj"
    if   sort == "l5":   key = lambda r: _window_hit_rate(r, "last_5")
    elif sort == "l10":  key = lambda r: _window_hit_rate(r, "last_10")
    elif sort == "l20":  key = lambda r: _window_hit_rate(r, "last_20")
    elif sort == "h2h":  key = lambda r: _window_hit_rate(r, "h2h")
    elif sort == "edge": key = _edge_ev
    else:                key = _proj_gap            # "proj" default
    return sorted(rows, key=key, reverse=True)


def _prop_card(r: dict, backend) -> None:
    """One prop pick rendered as a card.

    Density is the main design constraint here -- we want every piece
    of decision-relevant data visible without clicking through to the
    player page:
      * Market chip + matchup (header)
      * Player name (linked to profile)
      * OVER/UNDER pill + line + confidence (the page's visual anchor)
      * Predicted value chip when a regression model produced one
      * Inline summary chips: season avg + L5 + L10 + L20 + H2H hit rate
      * Opposing-team rank chip ("OPP NYY: 28th of 30")
      * Best odds + book + Track button

    On mobile the summary chips drop to a single column inside the
    same card -- ECharts not used here so the row is light enough to
    handle 30+ cards on a phone without jank.
    """
    side = (r.get("side") or "Over").strip().title()
    is_over = side == "Over"
    chip_bg = t.POS if is_over else t.NEG
    confidence_pct = r["confidence"] * 100

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; gap: {t.SPACE_SM}; "
        f"min-width: 0;"
    ):
        # Header row: market chip on the left, matchup on the right.
        with ui.row().classes("items-center w-full").style(
            f"gap: 8px;"
        ):
            ui.label(_short_market(r["market"]).upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            ui.label(r.get("team") or "").style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; "
                f"font-family: monospace; "
                f"margin-left: auto;"
            )

        # Player name: links to player profile page.
        _name_slug = r["player"].lower().replace(" ", "-")
        ui.link(r["player"], f"/player/mlb/{_name_slug}").style(
            f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
            f"line-height: 1.2; text-decoration: none; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        ).tooltip("View player profile")

        # Pick row: chip + line + confidence.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; flex-wrap: nowrap;"
        ):
            ui.label(f"{side.upper()} {r['line']}").style(
                f"background: {chip_bg}; color: {t.BG}; "
                f"font-size: 13px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 6px 12px; border-radius: {t.RADIUS_SM}; "
                f"flex-shrink: 0;"
            )
            ui.element("div").style("flex: 1;")
            with ui.column().style(
                "gap: 1px; align-items: flex-end; flex-shrink: 0;"
            ):
                ui.label("CONFIDENCE").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(f"{confidence_pct:.0f}%").style(
                    f"font-size: 18px; font-weight: 800; color: {chip_bg}; "
                    f"font-family: monospace; letter-spacing: -.2px;"
                )
                _ev_badge(r.get("ev_pct"))

        # Predicted value chip (only when a regression model produced one)
        pv = r.get("predicted_value")
        if pv is not None:
            try:
                line_f    = float(r["line"])
                side_str  = (r.get("side") or "Over").strip().title()
                margin    = (pv - line_f) if side_str == "Over" else (line_f - pv)
                pv_color  = t.POS if margin > 1.0 else t.WARN
            except (TypeError, ValueError):
                margin, pv_color = 0.0, t.TEXT_DIM
            stat_abbr = _market_stat_abbr(r.get("market", ""))
            with ui.row().classes("items-center w-full").style(
                f"gap: 8px; padding-top: 2px;"
            ):
                ui.label("PREDICTED").style(
                    f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                pv_label = f"{pv:.1f}" + (f" {stat_abbr}" if stat_abbr else "")
                ui.label(pv_label).style(
                    f"font-size: 13px; font-weight: 700; color: {pv_color}; "
                    f"font-family: monospace;"
                )

        # ── Inline performance summary chips ─────────────────────────────
        _card_summary_chips(r)

        # ── Opposing-team rank chip ──────────────────────────────────────
        _card_opp_rank_chip(r)

        # Footer row: line-type chip + best odds + book + Track Bet button.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; "
            f"padding-top: 6px; border-top: 1px solid {t.BORDER_SOFT};"
        ):
            _card_line_type_chip(r)
            ui.label("Best odds").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM};"
            )
            ui.label(_odds_str(r.get("best_odds"))).style(
                f"font-size: 12px; font-weight: 700; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
            ui.label(r.get("best_book") or "").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.element("div").style("flex: 1;")
            _track_btn(r, backend)

        # ── "See similar" expandable panel ───────────────────────────────
        _card_similar_panel(r)


def _card_similar_panel(r: dict) -> None:
    """Compact 'See similar' toggle that reveals the top 2-3 players in
    the same similarity cluster for this market, with their L10 hit
    rate.  Lazy: the cluster lookup + per-player hit-rate enrichment
    only runs when the user expands the panel."""
    market = r.get("market") or ""
    player = r.get("player") or ""
    try:
        line = float(r.get("line"))
    except (TypeError, ValueError):
        line = None
    side = (r.get("side") or "Over").strip().title()

    state = {"open": False, "loaded": False, "rows": None}

    @ui.refreshable
    def panel() -> None:                                                  # noqa: WPS430
        arrow = "▲" if state["open"] else "▼"
        ui.button(
            f"See similar  {arrow}", on_click=_toggle,
        ).props("flat dense no-caps").style(
            f"color: {t.PRIMARY_HI}; font-size: 10.5px; font-weight: 700; "
            f"min-height: 0; padding: 2px 4px; align-self: flex-start;"
        )
        if not state["open"]:
            return
        if not state["loaded"]:
            ui.label("Loading…").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"font-style: italic; padding: 4px;"
            )
            return
        rows = state["rows"] or []
        if not rows:
            ui.label("Not enough data to compare for this market yet.").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                f"font-style: italic; padding: 4px;"
            )
            return
        with ui.column().classes("w-full").style(
            f"gap: 4px; padding: 4px 0;"
        ):
            for s in rows:
                _similar_inline_row(s)

    async def _toggle() -> None:                                         # noqa: WPS430
        state["open"] = not state["open"]
        panel.refresh()
        if state["open"] and not state["loaded"]:
            try:
                state["rows"] = await asyncio.to_thread(
                    _similar_inline_data, market, player, line, side,
                )
            except Exception as exc:                                      # noqa: BLE001
                _dbg(f"see-similar load failed: {exc}")
                state["rows"] = []
            state["loaded"] = True
            panel.refresh()

    panel()


def _similar_inline_data(market: str, player: str, line, side: str) -> list[dict]:
    """Background-thread: top-3 similar players for this market with
    season avg + L10 hit rate vs the line.  Cached gamelog reads."""
    try:
        from src.player_similarity import get_similar_players
        from src.player_profile_client import (
            get_player_gamelog, get_player_prop_summary, _CURRENT_SEASON,
        )
    except Exception:                                                     # noqa: BLE001
        return []
    is_pitcher = market.startswith("pitcher_")
    sims = get_similar_players(market, player, limit=3)
    out: list[dict] = []
    for s in sims:
        pid = s.get("id")
        games: list[dict] = []
        if pid:
            try:
                games = get_player_gamelog(int(pid), _CURRENT_SEASON, is_pitcher=is_pitcher) or []
                if is_pitcher:
                    games = [g for g in games if g.get("games_started", 0) > 0]
            except Exception:                                             # noqa: BLE001
                games = []
        summ: dict = {}
        try:
            summ = get_player_prop_summary(
                s.get("name") or "", market, line, side,
                is_pitcher=is_pitcher, games=games,
            ) or {}
        except Exception:                                                 # noqa: BLE001
            summ = {}
        out.append({
            "name":      s.get("name") or "—",
            "team":      s.get("team") or "",
            "score":     s.get("score"),
            "l10_hits":  summ.get("last_10_hits") or 0,
            "l10_games": summ.get("last_10_games") or 0,
        })
    return out


def _similar_inline_row(s: dict) -> None:
    """One compact row inside the 'See similar' panel."""
    name = _strip(s.get("name") or "—")
    team = _strip(s.get("team") or "")
    slug = (s.get("name") or "").lower().replace(" ", "-")
    try:
        score_pct = f"{float(s.get('score')) * 100:.0f}%"
    except (TypeError, ValueError):
        score_pct = "—"
    hits  = int(s.get("l10_hits") or 0)
    games = int(s.get("l10_games") or 0)
    if games:
        rate = hits / games
        hit_str   = f"{int(round(rate * 100))}% L10"
        hit_color = t.POS if rate >= 0.5 else t.NEG
    else:
        hit_str, hit_color = "— L10", t.TEXT_DIM2

    with ui.row().classes("items-center w-full").style("gap: 8px; flex-wrap: nowrap;"):
        ui.link(name, f"/player/mlb/{slug}").style(
            f"font-size: 11.5px; font-weight: 700; color: {t.TEXT}; "
            f"text-decoration: none; white-space: nowrap; overflow: hidden; "
            f"text-overflow: ellipsis; flex: 1; min-width: 0;"
        )
        if team:
            ui.label(team).style(
                f"font-size: 9.5px; color: {t.TEXT_DIM2}; font-family: monospace;"
            )
        ui.label(hit_str).style(
            f"font-size: 11px; font-weight: 800; color: {hit_color}; "
            f"font-family: monospace;"
        )
        ui.label(f"sim {score_pct}").style(
            f"font-size: 9.5px; font-weight: 700; color: {t.PRIMARY_HI}; "
            f"font-family: monospace;"
        )


def _strip(text: str) -> str:
    try:
        from src.utils import strip_formatting
        return strip_formatting(text)
    except Exception:                                                     # noqa: BLE001
        return text


def _ev_badge(ev_pct, *, compact: bool = False) -> None:
    """Render a small ``+12.4% EV`` chip beside the confidence number.

    Green when EV is positive, red when negative, neutral grey when
    the cache hasn't computed it yet (e.g. an older payload from before
    this feature shipped).  ``compact=True`` shrinks the chip for use
    on the dense alt-row strip.

    EV% is sourced from the persisted ``ev_pct`` field on the pick
    dict; the props page never recomputes -- see src/props_ev.py for
    the formula.
    """
    try:
        from src.props_ev import ev_color, ev_label
    except Exception:                                                     # noqa: BLE001
        return
    label = ev_label(ev_pct)
    color = ev_color(ev_pct, t)
    if compact:
        size, padding = "10px", "2px 6px"
    else:
        size, padding = "10.5px", "3px 8px"
    ui.label(label).style(
        f"background: {t.CARD_HI}; color: {color}; "
        f"font-size: {size}; font-weight: 800; letter-spacing: .3px; "
        f"padding: {padding}; border-radius: {t.RADIUS_PILL}; "
        f"font-family: monospace; white-space: nowrap;"
    )


def _card_line_type_chip(r: dict) -> None:
    """Tiny MAIN / ALT chip on the footer row.  Surfaces the
    classification so an alt-only player (no main market available)
    isn't quietly priced as if it were a standard line."""
    line_type = (r.get("line_type") or "main").lower()
    if line_type == "main":
        label = "MAIN"
        fg    = t.POS
    else:
        label = "ALT"
        fg    = t.WARN
    ui.label(label).style(
        f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
        f"color: {fg}; background: {t.CARD_HI}; "
        f"padding: 2px 6px; border-radius: {t.RADIUS_PILL};"
    )


def _card_summary_chips(r: dict) -> None:
    """5-cell grid below the pick row: SEASON avg + L5/L10/L20 + H2H hit
    rate vs the line.  Same shape as the player-page summary row so the
    user can scan both pages without re-learning the layout."""
    summary = r.get("summary") or {}
    side    = (r.get("side") or "Over").strip().title()
    try:
        line_f = float(r.get("line"))
    except (TypeError, ValueError):
        line_f = None

    cells: list[tuple[str, str, str, str]] = []  # (label, value, color, sub)

    sa = summary.get("season_avg")
    cells.append((
        "SEASON",
        "—" if sa is None else f"{sa:.2f}",
        t.TEXT,
        f"avg/{summary.get('season_games') or 0}g",
    ))
    for w_key, w_label in (
        ("last_5",  "L5"),
        ("last_10", "L10"),
        ("last_20", "L20"),
    ):
        hits  = summary.get(f"{w_key}_hits") or 0
        total = summary.get(f"{w_key}_games") or 0
        if not total:
            cells.append((w_label, "—", t.TEXT_DIM2, "n/a"))
            continue
        pct = hits / total
        col = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append((
            w_label, f"{hits}/{total}", col,
            f"{int(round(pct * 100))}%",
        ))
    h2h_hits  = summary.get("h2h_hits") or 0
    h2h_total = summary.get("h2h_games") or 0
    if not h2h_total:
        cells.append(("H2H", "—", t.TEXT_DIM2, "n/a"))
    else:
        pct = h2h_hits / h2h_total
        col = t.POS if pct >= 0.6 else (t.NEG if pct < 0.4 else t.WARN)
        cells.append((
            "H2H", f"{h2h_hits}/{h2h_total}", col,
            f"{int(round(pct * 100))}%",
        ))

    side_suffix = (
        f"vs {side.upper()} {line_f}" if line_f is not None else ""
    )
    with ui.column().classes("w-full").style("gap: 4px; padding-top: 2px;"):
        if side_suffix:
            ui.label(f"HIT RATE {side_suffix}").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
        with ui.element("div").style(
            "display: grid; grid-template-columns: repeat(5, 1fr); "
            "gap: 4px; width: 100%;"
        ):
            for label, value, color, sub in cells:
                with ui.column().style(
                    f"background: {t.CARD_HI}; "
                    f"border-radius: {t.RADIUS_SM}; "
                    f"padding: 6px 4px; align-items: center; "
                    f"gap: 1px; min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 8.5px; font-weight: 800; letter-spacing: .4px; "
                        f"color: {t.TEXT_DIM2};"
                    )
                    ui.label(value).style(
                        f"font-size: 12px; font-weight: 800; "
                        f"color: {color}; font-family: monospace;"
                    )
                    if sub:
                        ui.label(sub).style(
                            f"font-size: 8.5px; color: {t.TEXT_DIM2}; "
                            f"font-family: monospace;"
                        )


def _card_opp_rank_chip(r: dict) -> None:
    """Compact 'OPP NYY: 28th of 30 vs K' chip.  The rank is precomputed
    in _section_unified_props_list so this is just formatting."""
    opp  = r.get("opp_abbrev")
    rank = r.get("opp_rank")
    if not opp or rank is None:
        return
    try:
        from src.player_profile_client import opp_rank_label
        label = opp_rank_label(rank)
    except Exception:                                                      # noqa: BLE001
        label = f"{rank}"

    # Color: top-10 favorable = green, bottom-10 = red, mid = amber.
    if rank <= 10:
        color = t.POS
    elif rank >= 21:
        color = t.NEG
    else:
        color = t.WARN

    stat_label = _market_stat_abbr(r.get("market", "")) or "stat"
    with ui.row().classes("items-center w-full").style(
        "gap: 6px; padding-top: 2px;"
    ):
        ui.label(f"OPP {opp}").style(
            f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
            f"color: {t.TEXT_DIM2}; "
            f"background: {t.CARD_HI}; padding: 2px 6px; "
            f"border-radius: {t.RADIUS_PILL};"
        )
        ui.label(f"{label} vs {stat_label}").style(
            f"font-size: 11px; font-weight: 700; color: {color}; "
            f"font-family: monospace;"
        )


# ── Small helpers ───────────────────────────────────────────────────────────

def _short_market(market: str) -> str:
    """Human-readable label for the market key."""
    mapping = {
        "pitcher_strikeouts":   "Strikeouts",
        "pitcher_outs":         "Outs Recorded",
        "pitcher_hits_allowed": "Hits Allowed",
        "pitcher_walks":        "Walks Allowed",
        "pitcher_earned_runs":  "Earned Runs",
        "pitcher_record_a_win": "Win",
        "batter_hits":          "Hits",
        "batter_total_bases":   "Total Bases",
        "batter_home_runs":     "Home Runs",
        "batter_rbis":          "RBIs",
        "batter_runs_scored":   "Runs",
        "batter_walks":         "Walks",
        "batter_strikeouts":    "Strikeouts",
        "batter_stolen_bases":  "Stolen Bases",
    }
    return mapping.get(market, market.replace("_", " ").title())


def _odds_str(o) -> str:
    if o is None:
        return "—"
    try:
        n = int(o)
    except (TypeError, ValueError):
        return str(o)
    return f"+{n}" if n > 0 else str(n)


def _market_stat_abbr(market: str) -> str:
    """Short stat label shown next to the predicted value."""
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
        "batter_strikeouts":    "K",
        "batter_stolen_bases":  "SB",
    }
    return mapping.get(market, "")


def _short_iso(iso: str) -> str:
    """ISO timestamp -> compact "HH:MM ET" for the page header."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    except Exception:                                                     # noqa: BLE001
        return str(iso)[:16]


# ── Track Bet button ─────────────────────────────────────────────────────────

def _track_btn(r: dict, backend) -> None:
    """Render a small Track Bet button that POSTs the pick to /api/props/track.

    Uses the same in-process Flask test-client pattern as track_button.py
    so no HTTP hop is needed and Railway deploy constraints are respected.
    """
    btn = ui.button("Track").props("no-caps unelevated dense").style(
        f"background: {t.PRIMARY}; color: {t.BG}; "
        f"font-weight: 800; font-size: 10.5px; letter-spacing: .4px; "
        f"padding: 4px 10px; border-radius: {t.RADIUS_SM}; min-height: 0;"
    )

    async def _click():
        btn.props("loading")
        btn.disable()
        try:
            payload = {
                "player":          r.get("player", ""),
                "market":          r.get("market", ""),
                "line":            r.get("line"),
                "side":            r.get("side", "Over"),
                "odds":            r.get("best_odds"),
                "confidence":      r.get("confidence"),
                "predicted_value": r.get("predicted_value"),
                "team":            r.get("team", ""),
                "event_id":        r.get("event_id"),
                "commence_time":   r.get("commence_time"),
            }
            ok, data, _ = await asyncio.to_thread(
                _post_api, backend, "/api/props/track", payload
            )
            if ok:
                ui.notify(
                    f"Tracked: {r.get('player')} {r.get('side')} {r.get('line')}",
                    type="positive",
                )
                btn.text = "Tracked ✓"
                btn.props("disable")
            else:
                err = data.get("error") or "unknown error"
                if "already tracked" in err.lower():
                    btn.text = "Tracked ✓"
                    btn.props("disable")
                    ui.notify("Already tracked.", type="info")
                else:
                    ui.notify(f"Track failed: {err}", type="negative")
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"Track failed: {exc}", type="negative")
        finally:
            btn.props(remove="loading")
            if btn.text == "Track":
                btn.enable()

    btn.on("click", _click)


def _settle_trigger(backend) -> None:
    """Fire /api/props/settle_open once when the page loads so recently
    completed games are settled without the user having to do anything."""
    async def _try_settle():
        try:
            await asyncio.to_thread(
                _post_api, backend, "/api/props/settle_open", {}
            )
        except Exception:                                                 # noqa: BLE001
            pass

    ui.timer(0.5, _try_settle, once=True)


def _post_api(backend, path: str, body: dict) -> tuple[bool, dict, int]:
    """Invoke a Flask /api/ route via the in-process test client."""
    client = backend.app.test_client()
    try:
        resp = client.post(path, json=body or {})
        data = resp.get_json(force=True, silent=True) or {}
        ok   = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500
