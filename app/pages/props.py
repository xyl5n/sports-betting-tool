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
from components import navbar, bottom_nav, live_score, team_logo
from components import controls

# localStorage keys for the persisted Props UI controls.  These survive a page
# refresh AND a browser restart (localStorage, not sessionStorage), mirroring
# the filter-persistence layer added in #294 so the page comes back exactly as
# the user left it.
_VIEW_MODE_LS_KEY = "propsViewMode"   # By Game | By Sort  ("game" | "sort")
_SORT_LS_KEY      = "propsSortMode"   # active sort pill   (see _SORT_PILLS)
_SUBMODE_LS_KEY   = "propsSubMode"    # By Sort sub-view   ("list"|"swipe"|"xray")
_FILTERS_LS_KEY   = "propsFilters"    # filter bar state   (JSON blob)


def _dbg(msg: str) -> None:
    """Tagged stderr log -- mirrors home.py's _dbg pattern."""
    print(f"[RENDER] {msg}", flush=True, file=sys.stderr)


# ── Persistence helpers ───────────────────────────────────────────────────────
# Best-effort localStorage writes/reads for the persisted controls above.  All
# writes are wrapped in try/catch on the JS side so private-mode / quota errors
# never surface to the user, matching the #294 Filters store semantics.

def _persist(key: str, value: str) -> None:
    """Persist a single string *value* under *key* in localStorage (no-op on
    failure).  ``value`` is JSON-encoded so it is always a safe JS literal."""
    import json as _json
    ui.run_javascript(
        f"try{{localStorage.setItem('{key}', {_json.dumps(value)})}}catch(e){{}}"
    )


def _persist_filters(filters: dict) -> None:
    """Persist the filter-bar state as a JSON blob.  Sets (markets / games) are
    stored as sorted lists since sets aren't JSON-serialisable."""
    import json as _json
    blob = _json.dumps({
        "min_l10":   filters.get("min_l10", 0),
        "show_alt":  bool(filters.get("show_alt")),
        "markets":   sorted(filters.get("markets") or []),
        "games":     sorted(filters.get("games") or []),
        "min_conf":  filters.get("min_conf", 0.0),
        "min_grade": filters.get("min_grade", 0.0),
    })
    _persist(_FILTERS_LS_KEY, blob)


def _apply_filters_json(filters: dict, raw: str, rows: list[dict]) -> bool:
    """Merge a persisted filters JSON blob into *filters* in place.

    Stale market / game selections that aren't part of today's slate are
    dropped -- game keys are event-specific and change daily, so persisting a
    raw selection across days would otherwise trap the user in a permanent
    "No props match these filters" empty state.  Returns True iff anything
    actually changed (so the caller knows whether to re-render)."""
    import json as _json
    try:
        d = _json.loads(raw)
    except Exception:                                                     # noqa: BLE001
        return False
    if not isinstance(d, dict):
        return False
    market_opts, game_opts = _filter_options(rows)
    new = _default_filters()
    try:
        new["min_l10"] = int(d.get("min_l10") or 0)
    except (TypeError, ValueError):
        pass
    new["show_alt"] = bool(d.get("show_alt"))
    try:
        new["min_conf"] = float(d.get("min_conf") or 0.0)
    except (TypeError, ValueError):
        pass
    try:
        new["min_grade"] = float(d.get("min_grade") or 0.0)
    except (TypeError, ValueError):
        pass
    new["markets"] = {m for m in (d.get("markets") or []) if m in market_opts}
    new["games"]   = {g for g in (d.get("games") or []) if g in game_opts}
    if new == filters:
        return False
    filters.clear()
    filters.update(new)
    return True


# Page-scoped CSS: let the 7-pill sort row wrap on very narrow phones so the
# pills don't get squeezed to illegible widths.  Layout-only.
_PROPS_LOCAL_CSS = """
<style>
  @media (max-width: 480px) {
    .props-sort-pills { flex-wrap: wrap !important; }
  }
  /* Swipe mode: suppress text selection while dragging */
  .swipe-dragging, .swipe-dragging * {
    user-select: none !important;
    -webkit-user-select: none !important;
  }
</style>
"""

# ── Swipe gesture JS ─────────────────────────────────────────────────────────
# Injected after each swipe-mode render via ui.run_javascript().
# Uses a _swipeAttached flag on the wrapper element to avoid double-binding
# when NiceGUI re-renders the refreshable (same DOM element, new render cycle).
_SWIPE_JS = """
(function initSwipeGesture() {
  var wrapper = document.getElementById('swipe-card-wrapper');
  if (!wrapper) { setTimeout(initSwipeGesture, 60); return; }
  if (wrapper._swipeAttached) return;
  wrapper._swipeAttached = true;

  var startX = 0, dx = 0, dragging = false;
  var THRESH = 80, MAX_ROT = 15;

  wrapper.addEventListener('mousedown', onStart);
  wrapper.addEventListener('touchstart', onStart, {passive: true});

  function onStart(e) {
    startX = e.touches ? e.touches[0].clientX : e.clientX;
    dx = 0; dragging = true;
    wrapper.style.transition = 'none';
    document.body.classList.add('swipe-dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onMove, {passive: false});
    document.addEventListener('touchend', onEnd);
  }

  function onMove(e) {
    if (!dragging) return;
    if (e.cancelable) e.preventDefault();
    dx = (e.touches ? e.touches[0].clientX : e.clientX) - startX;
    var rot  = Math.max(-MAX_ROT, Math.min(MAX_ROT, dx * 0.15));
    var fade = Math.max(0.4, 1 - Math.abs(dx) / 250);
    wrapper.style.transform = 'translateX(' + dx + 'px) rotate(' + rot + 'deg)';
    wrapper.style.opacity   = fade;
    var hint = document.getElementById('swipe-hint');
    if (hint) {
      if (dx > 40) {
        hint.textContent  = '\\u2713';
        hint.style.color   = '#22c55e';
        hint.style.opacity = Math.min(0.85, (dx - 40) / 80);
      } else if (dx < -40) {
        hint.textContent  = '\\u2717';
        hint.style.color   = '#ef4444';
        hint.style.opacity = Math.min(0.85, (-dx - 40) / 80);
      } else {
        hint.style.opacity = '0';
      }
    }
  }

  function onEnd() {
    if (!dragging) return;
    dragging = false;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup',   onEnd);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend',  onEnd);
    document.body.classList.remove('swipe-dragging');

    wrapper.style.transition = 'transform 0.3s ease, opacity 0.3s ease';
    var hint = document.getElementById('swipe-hint');
    if (hint) hint.style.opacity = '0';

    if (dx > THRESH) {
      wrapper.style.transform = 'translateX(110vw) rotate(20deg)';
      wrapper.style.opacity   = '0';
      setTimeout(function() {
        var btn = document.getElementById('swipe-right-trigger');
        if (btn) btn.click();
      }, 300);
    } else if (dx < -THRESH) {
      wrapper.style.transform = 'translateX(-110vw) rotate(-20deg)';
      wrapper.style.opacity   = '0';
      setTimeout(function() {
        var btn = document.getElementById('swipe-left-trigger');
        if (btn) btn.click();
      }, 300);
    } else {
      /* Snap back */
      wrapper.style.transform = '';
      wrapper.style.opacity   = '1';
    }
    dx = 0;
  }
})();
"""


def register(backend) -> None:
    @ui.page("/props")
    def props_page():
        _dbg("props_page ENTER")
        try:
            ui.add_head_html(t.page_head_css())
            ui.add_head_html(_PROPS_LOCAL_CSS)
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
        from src import model_picks as _mp
        recs = _mp.prop_records("mlb")
        pitcher_rec, batter_rec = recs["pitcher"], recs["batter"]
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props model record load failed: {exc}")
        pitcher_rec = batter_rec = {"wins": 0, "losses": 0, "pct": None}
    # _record_card expects a 'total' field.
    for _rec in (pitcher_rec, batter_rec):
        _rec["total"] = int(_rec.get("wins") or 0) + int(_rec.get("losses") or 0)

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

    Two view modes (toggled by the List | Swipe pill in the header row):
      List  -- the existing game-grid layout (default).
      Swipe -- one card at a time; drag right to Track, drag left to dismiss.
    """
    try:
        from src.props_scored_cache import load_scored_props
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props cache import failed: {exc}")
        return

    cache = load_scored_props() or {}
    all_rows: list[dict] = list(cache.get("picks") or [])
    # Only show props for games that have NOT started yet (FIX 1): drop any
    # whose start time has passed or whose game is Live/Final per the
    # schedule.  Cached in-process so this doesn't re-fetch on every render.
    rows = [
        r for r in all_rows
        if not live_score.game_has_started(
            backend,
            commence_time=r.get("commence_time"),
            home_team=r.get("home_team"),
            away_team=r.get("away_team"),
            sport="mlb",
        )
    ]
    rows.sort(key=lambda r: -float(r.get("confidence") or 0.0))

    _dbg(
        f"[PROPS-PAGE] read scored cache: {len(all_rows)} picks, "
        f"{len(rows)} upcoming (generated_at={cache.get('generated_at')})"
    )

    # Kick the breakdown queue for the full slate so each card's AI breakdown
    # (the same artifact the player page shows) gets generated via the
    # existing Groq pipeline + 150 ms spacing.  Lock-guarded -> launches once;
    # cards below show a loading state and populate as breakdowns land.
    try:
        from src import player_ai_breakdown as _pab
        _pab.launch_breakdown_queue(rows)
    except Exception as exc:                                               # noqa: BLE001
        _dbg(f"breakdown queue launch failed: {exc}")

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        if not rows:
            # Static header (no mode toggle needed when there are no picks)
            with ui.row().classes("items-center w-full").style("gap: 8px;"):
                ui.label("PICKS").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                if cache.get("generated_at"):
                    ui.label(f"scored {_short_iso(cache['generated_at'])}").style(
                        f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                        f"margin-left: auto; font-family: monospace;"
                    )
            if all_rows:
                _no_upcoming_message(
                    "No upcoming props",
                    "All of today's games have already started — check back tomorrow.",
                )
            else:
                _empty_state_message()
            return

        state      = {"sort": "proj"}
        filters    = _default_filters()
        # group: "game" (collapsible game groups, default) | "sort" (flat list).
        # mode:  the By Sort sub-view (list | swipe | xray), unchanged.
        view_state = {"mode": "list", "group": "game"}
        # Game keys currently expanded in By Game view (default: all collapsed).
        expanded: set = set()
        # dismissed: set of _prop_swipe_key() strings; survives filter / sort
        # changes within the same page session (never serialised).
        dismissed: set = set()
        # X-Ray table sort state: persists across filter / sort changes.
        xray_sort  = {"col": "conf", "asc": False}

        # ── Card area (refreshable so sort, filters, and mode re-render) ──
        @ui.refreshable
        def cards_refresh() -> None:                                      # noqa: WPS430
            shown = [r for r in rows if _passes_filters(r, filters)]
            shown = _sorted_picks(shown, state)

            # Header row: title + count chip + timestamp + List|Swipe toggle
            with ui.row().classes("items-center w-full").style(
                "gap: 8px; flex-wrap: wrap;"
            ):
                ui.label("PICKS").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                n_str = (
                    f"{len(shown)} of {len(rows)} picks"
                    if _active_filter_count(filters)
                    else f"{len(rows)} picks"
                )
                ui.label(n_str).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
                if cache.get("generated_at"):
                    ui.label(f"scored {_short_iso(cache['generated_at'])}").style(
                        f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                        f"font-family: monospace;"
                    )
                # Push the toggles to the far right
                ui.element("div").style("flex: 1; min-width: 4px;")
                # View toggle: By Game (grouped, default) | By Sort (flat list)
                def _mk_group(g):
                    def _set():
                        view_state["group"] = g
                        _persist(_VIEW_MODE_LS_KEY, g)
                        cards_refresh.refresh()
                    return _set
                with ui.row().style("gap: 3px; flex-shrink: 0;"):
                    for _gkey, _glabel in (("game", "By Game"), ("sort", "By Sort")):
                        _ga = view_state["group"] == _gkey
                        ui.button(_glabel, on_click=_mk_group(_gkey)).props(
                            "no-caps unelevated dense"
                        ).style(
                            f"background: {t.PRIMARY if _ga else t.CARD_HI}; "
                            f"color: {t.BG if _ga else t.TEXT_DIM}; "
                            f"font-size: 10.5px; font-weight: 700; letter-spacing: .2px; "
                            f"padding: 4px 10px; border-radius: {t.RADIUS_PILL}; "
                            f"min-height: 0;"
                        )
                # List | Swipe | X-Ray sub-mode pills -- only in By Sort view
                if view_state["group"] == "sort":
                    with ui.row().style("gap: 3px; flex-shrink: 0;"):
                        for _mkey, _mlabel in (("list", "≡ List"), ("swipe", "⇄ Swipe"), ("xray", "⊞ X-Ray")):
                            _active = view_state["mode"] == _mkey
                            def _mk_mode(mk=_mkey):
                                def _set():
                                    view_state["mode"] = mk
                                    _persist(_SUBMODE_LS_KEY, mk)
                                    cards_refresh.refresh()
                                return _set
                            ui.button(_mlabel, on_click=_mk_mode()).props(
                                "no-caps unelevated dense"
                            ).style(
                                f"background: {t.PRIMARY if _active else t.CARD_HI}; "
                                f"color: {t.BG if _active else t.TEXT_DIM}; "
                                f"font-size: 10.5px; font-weight: 700; letter-spacing: .2px; "
                                f"padding: 4px 10px; border-radius: {t.RADIUS_PILL}; "
                                f"min-height: 0;"
                            )

            if not shown:
                _no_match_message()
                return

            if view_state["group"] == "game":
                _render_by_game(shown, backend, expanded)
            elif view_state["mode"] == "list":
                with ui.element("div").classes("game-grid w-full"):
                    for r_item in shown:
                        _prop_card(r_item, backend)
            elif view_state["mode"] == "swipe":
                swipe_shown = [
                    r_item for r_item in shown
                    if _prop_swipe_key(r_item) not in dismissed
                ]
                _render_swipe_mode(
                    swipe_shown, len(shown), backend, dismissed,
                    cards_refresh.refresh,
                )
            else:
                _render_xray_mode(shown, backend, xray_sort, cards_refresh.refresh)

        # ── Filter button + collapsible panel ─────────────────────────────
        bar_refresh = _filter_bar(rows, filters, cards_refresh.refresh)

        # ── Sort pills ─────────────────────────────────────────────────────
        # Single row of 7 pills (L5 / L10 / L20 / H2H / Proj / Edge / Conf);
        # only the order changes.  Default sort is Proj (model-vs-book gap).
        sort_refresh = _sort_pills(state, cards_refresh.refresh)
        cards_refresh()

        # Hydrate every persisted control from localStorage in one shot (after
        # the socket connects); re-render only the pieces that actually differ
        # from their defaults so a fresh visitor sees no flicker.
        async def _hydrate_state() -> None:                               # noqa: WPS430
            import json as _json
            try:
                raw = await ui.run_javascript(
                    "JSON.stringify({"
                    f"g:localStorage.getItem('{_VIEW_MODE_LS_KEY}'),"
                    f"s:localStorage.getItem('{_SORT_LS_KEY}'),"
                    f"m:localStorage.getItem('{_SUBMODE_LS_KEY}'),"
                    f"f:localStorage.getItem('{_FILTERS_LS_KEY}')"
                    "})"
                )
                data = _json.loads(raw) if raw else {}
            except Exception:                                             # noqa: BLE001
                return
            if not isinstance(data, dict):
                return

            changed = filters_changed = False
            g = data.get("g")
            if g in ("game", "sort") and g != view_state["group"]:
                view_state["group"] = g
                changed = True
            s = data.get("s")
            if s in _SORT_KEYS and s != state["sort"]:
                state["sort"] = s
                changed = True
            m = data.get("m")
            if m in ("list", "swipe", "xray") and m != view_state["mode"]:
                view_state["mode"] = m
                changed = True
            f = data.get("f")
            if f and _apply_filters_json(filters, f, rows):
                changed = filters_changed = True

            if filters_changed:
                bar_refresh()
            if changed:
                sort_refresh()
                cards_refresh.refresh()
        ui.timer(0.1, _hydrate_state, once=True)


def _render_by_game(shown: list[dict], backend, expanded: set) -> None:
    """By Game view: group the already-filtered/sorted props by game and
    render one collapsible card per game (earliest start first).  Games with
    no props in the current filter are absent (we group from *shown*)."""
    groups: dict[str, list[dict]] = {}
    for r in shown:
        groups.setdefault(_game_key(r), []).append(r)

    def _start(gk: str) -> str:
        return groups[gk][0].get("commence_time") or "9999"

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        for gk in sorted(groups, key=_start):
            _render_game_group(gk, groups[gk], backend, expanded)


def _render_game_group(
    gk: str, group_rows: list[dict], backend, expanded: set,
) -> None:
    """One collapsible game group: a clickable header (logos + abbrevs + time +
    prop count + chevron) over a hidden body of the standard prop cards."""
    rep   = group_rows[0]
    away  = rep.get("away_team") or "?"
    home  = rep.get("home_team") or "?"
    time_s = _game_time_et(rep.get("commence_time"))
    n     = len(group_rows)
    is_open = gk in expanded

    header = ui.row().classes("items-center w-full cursor-pointer").style(
        f"gap: 8px; background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; min-width: 0;"
    )
    with header:
        team_logo.render(away, sport="mlb", size=22)
        ui.label(team_logo.abbrev(away)).style(
            f"font-size: 12.5px; font-weight: 800; color: {t.TEXT};")
        ui.label("@").style(f"font-size: 11px; color: {t.TEXT_DIM2};")
        team_logo.render(home, sport="mlb", size=22)
        ui.label(team_logo.abbrev(home)).style(
            f"font-size: 12.5px; font-weight: 800; color: {t.TEXT};")
        ui.element("div").style("flex: 1; min-width: 4px;")
        if time_s:
            ui.label(time_s).style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; font-family: monospace; "
                f"flex-shrink: 0;")
        ui.label(f"{n} prop{'s' if n != 1 else ''}").style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; font-size: 10.5px; "
            f"font-weight: 700; padding: 2px 8px; border-radius: {t.RADIUS_PILL}; "
            f"flex-shrink: 0;")
        chevron = ui.icon("chevron_right").style(_chevron_style(is_open))

    body = ui.element("div").classes("game-grid w-full").style(
        f"margin: 4px 0 2px 0; padding-left: 10px; "
        f"border-left: 2px solid {t.BORDER};")
    with body:
        for r in group_rows:
            _prop_card(r, backend)
    body.set_visibility(is_open)

    def _toggle() -> None:
        opened = gk not in expanded
        if opened:
            expanded.add(gk)
        else:
            expanded.discard(gk)
        body.set_visibility(opened)
        chevron.style(replace=_chevron_style(opened))
    header.on("click", _toggle)


def _chevron_style(is_open: bool) -> str:
    return (f"color: {t.TEXT_DIM}; transition: transform .15s ease; "
            f"transform: rotate({'90' if is_open else '0'}deg); flex-shrink: 0;")


def _game_time_et(iso) -> str:
    """ISO UTC timestamp -> compact local game time, e.g. '7:05 PM ET'."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return ""


def _no_match_message() -> None:
    """Shown when the active filters exclude every prop (never a blank list)."""
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_XL} {t.SPACE_LG}; "
        f"gap: 6px; align-items: center;"
    ):
        ui.label("No props match these filters").style(
            f"font-size: 15px; font-weight: 800; color: {t.TEXT};"
        )
        ui.label("Loosen or reset the filters to see more props.").style(
            f"font-size: 12px; color: {t.TEXT_DIM}; text-align: center;"
        )


def _filter_bar(rows: list[dict], filters: dict, on_change):
    """Filter button + collapsible panel.  All controls read their option
    sets from the loaded props and mutate the shared *filters* dict, then call
    *on_change* (the card-list refresh) so the list re-renders without a page
    reload.  AND-stacked via _passes_filters."""
    market_opts, game_opts = _filter_options(rows)
    open_state = {"v": False}

    @ui.refreshable
    def bar() -> None:                                                    # noqa: WPS430
        n_active = _active_filter_count(filters)
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            label = "Filters" + (f" ({n_active})" if n_active else "")
            ui.button(label, icon="filter_list",
                      on_click=_toggle).props("no-caps unelevated dense").style(
                f"background: {(t.POS if n_active else 'transparent')} !important; "
                f"color: {(t.BG if n_active else t.TEXT_DIM)} !important; "
                f"border: 1px solid {(t.POS if n_active else t.BORDER)}; "
                f"min-height: 32px; padding: 4px 12px; font-size: 11.5px; "
                f"font-weight: 800; border-radius: {t.RADIUS_PILL};"
            )
            if n_active:
                ui.button("Reset filters",
                          on_click=_reset).props("no-caps flat dense").style(
                    f"color: {t.TEXT_DIM}; font-size: 11px; font-weight: 700; "
                    f"min-height: 32px; padding: 4px 8px;"
                )
        if open_state["v"]:
            _panel()

    def _toggle() -> None:
        open_state["v"] = not open_state["v"]
        bar.refresh()

    def _reset() -> None:
        filters.update(_default_filters())
        _persist_filters(filters)
        on_change()
        bar.refresh()

    def _set(key, value) -> None:
        filters[key] = value
        _persist_filters(filters)
        on_change()
        bar.refresh()

    def _panel() -> None:
        with ui.column().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
            f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 12px;"
        ):
            sel_style = (f"min-width: 180px; flex: 1 1 200px;")
            with ui.row().classes("w-full").style("gap: 12px; flex-wrap: wrap;"):
                controls.styled_select(
                    _L10_OPTIONS, value=filters["min_l10"],
                    placeholder="Min last-10 hit rate", min_width="180px",
                    on_change=lambda e: _set("min_l10", e.value)).style(sel_style)
                controls.styled_select(
                    _CONF_OPTIONS, value=filters["min_conf"],
                    placeholder="Min model confidence", min_width="180px",
                    on_change=lambda e: _set("min_conf", e.value)).style(sel_style)
                controls.styled_select(
                    _GRADE_OPTIONS, value=filters["min_grade"],
                    placeholder="Min matchup grade", min_width="180px",
                    on_change=lambda e: _set("min_grade", e.value)).style(sel_style)
            with ui.row().classes("w-full").style("gap: 12px; flex-wrap: wrap;"):
                controls.styled_select(
                    market_opts, value=sorted(filters["markets"]),
                    multiple=True, use_chips=True, placeholder="Prop type",
                    min_width="180px",
                    on_change=lambda e: _set("markets", set(e.value or []))).style(sel_style)
                controls.styled_select(
                    game_opts, value=sorted(filters["games"]),
                    multiple=True, use_chips=True, placeholder="Game",
                    min_width="180px",
                    on_change=lambda e: _set("games", set(e.value or []))).style(sel_style)
            with ui.row().classes("items-center").style("gap: 8px;"):
                ui.switch("Show alternative lines", value=filters["show_alt"],
                          on_change=lambda e: _set("show_alt", bool(e.value))) \
                    .props("dense")

    bar()
    return bar.refresh


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


def _no_upcoming_message(title: str, body: str) -> None:
    """Placeholder shown when props exist today but all games have started."""
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
    ("conf", "Conf"),
)

# Valid sort keys -- guards a persisted value before it's trusted on hydration.
_SORT_KEYS: frozenset = frozenset(k for k, _ in _SORT_PILLS)


def _sort_pills(state: dict, on_change):
    """Render the seven sort pills in one horizontal row, no title label.
    Active pill is highlighted green; clicking sets state['sort'] and
    refreshes the card list."""
    @ui.refreshable
    def render() -> None:                                                 # noqa: WPS430
        with ui.row().classes("items-stretch w-full props-sort-pills").style(
            "gap: 6px; flex-wrap: nowrap;"
        ):
            for key, label in _SORT_PILLS:
                _sort_pill(label, key, state.get("sort") == key,
                           state, on_change, render.refresh)
    render()
    return render.refresh


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
        _persist(_SORT_LS_KEY, key)
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
    elif sort == "conf": key = lambda r: float(r.get("confidence") or 0.0)
    else:                key = _proj_gap            # "proj" default
    return sorted(rows, key=key, reverse=True)


# ── Filtering ──────────────────────────────────────────────────────────────────
# Every value below is read straight off the prop the page already loaded
# (the scored cache row + its summary) -- no new data source, no recompute.

# Min last-10 hit-rate options (raw count out of the player's last 10 games
# in which they cleared the current line) -> required hit count.
_L10_OPTIONS = {0: "Any", 5: "5+/10", 6: "6+/10", 7: "7+/10", 8: "8+/10", 9: "9+/10"}
# Min model confidence options -> fraction.
_CONF_OPTIONS = {0.0: "Any", 0.55: "55%+", 0.60: "60%+", 0.65: "65%+",
                 0.70: "70%+", 0.75: "75%+"}
# Min matchup-grade options -> composite floor (matches the bands used by
# pages.player._letter_grade_for_prop).
_GRADE_OPTIONS = {0.0: "Any", 0.52: "B- or better", 0.60: "B or better",
                  0.68: "B+ or better", 0.76: "A- or better", 0.84: "A or better"}


def _default_filters() -> dict:
    # Alt-line props are hidden by default; the user opts into them.
    return {"min_l10": 0, "show_alt": False, "markets": set(), "games": set(),
            "min_conf": 0.0, "min_grade": 0.0}


def _active_filter_count(f: dict) -> int:
    """How many filters deviate from their default (drives the badge)."""
    n = 0
    n += 1 if f.get("min_l10") else 0
    n += 1 if f.get("show_alt") else 0              # default is hidden; showing alts is the deviation
    n += 1 if f.get("markets") else 0
    n += 1 if f.get("games") else 0
    n += 1 if f.get("min_conf") else 0
    n += 1 if f.get("min_grade") else 0
    return n


def _prop_grade_composite(r: dict) -> float:
    """0..1 matchup-grade composite from the prop's own confidence + opp rank
    + EV%, mirroring pages.player._letter_grade_for_prop (no new data)."""
    try:
        conf = float(r.get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf_score = max(0.0, min(1.0, (conf - 0.50) / 0.45))
    try:
        rank = int(r.get("opp_rank") or 15)
    except (TypeError, ValueError):
        rank = 15
    rank_score = max(0.0, min(1.0, (31 - rank) / 30.0))
    try:
        ev = float(r.get("ev_pct") or 0.0)
    except (TypeError, ValueError):
        ev = 0.0
    ev_score = max(0.0, min(1.0, ev / 30.0))
    return 0.5 * conf_score + 0.3 * rank_score + 0.2 * ev_score


def _game_key(r: dict) -> str:
    return str(r.get("event_id")
               or f"{r.get('away_team') or '?'}@{r.get('home_team') or '?'}")


def _game_label(r: dict) -> str:
    return f"{r.get('away_team') or '?'} @ {r.get('home_team') or '?'}"


def _filter_options(rows: list[dict]) -> tuple[dict, dict]:
    """Available prop-type + game options for the current slate, derived from
    the loaded props.  Returns ({market: label}, {game_key: label})."""
    markets: dict[str, str] = {}
    games: dict[str, str] = {}
    for r in rows:
        m = r.get("market")
        if m and m not in markets:
            markets[m] = _short_market(m)
        gk = _game_key(r)
        if gk not in games:
            games[gk] = _game_label(r)
    return (dict(sorted(markets.items(), key=lambda kv: kv[1])),
            dict(sorted(games.items(), key=lambda kv: kv[1])))


def _passes_filters(r: dict, f: dict) -> bool:
    """True iff *r* satisfies every active filter (AND-stacked)."""
    # 1. Minimum last-10 hit rate (raw hits over the player's last 10 games).
    if f.get("min_l10"):
        s = r.get("summary") or {}
        if int(s.get("last_10_hits") or 0) < int(f["min_l10"]):
            return False
    # 2. Alternative lines: when hidden, drop anything not on the main line.
    if not f.get("show_alt") and (r.get("line_type") or "main").lower() != "main":
        return False
    # 3. Prop type (empty selection = all types).
    if f.get("markets") and r.get("market") not in f["markets"]:
        return False
    # 4. Game (empty selection = all games).
    if f.get("games") and _game_key(r) not in f["games"]:
        return False
    # 5. Minimum model confidence.
    if f.get("min_conf"):
        try:
            if float(r.get("confidence") or 0.0) < float(f["min_conf"]):
                return False
        except (TypeError, ValueError):
            return False
    # 6. Minimum matchup grade (independent of confidence).
    if f.get("min_grade") and _prop_grade_composite(r) < float(f["min_grade"]):
        return False
    return True


def _prop_swipe_key(r: dict) -> str:
    """Stable identity string used for the dismissed-set in swipe mode."""
    return f"{r.get('player')}|{r.get('market')}|{r.get('line')}|{r.get('side')}"


def _render_swipe_mode(
    swipe_shown: list[dict],
    total_shown: int,
    backend,
    dismissed: set,
    on_refresh,
) -> None:
    """Swipe mode: one card at a time with drag-to-track / drag-to-dismiss.

    Right swipe (or ✓ button) → Track via /api/props/track (same payload as
    the list-mode Track button).
    Left swipe  (or ✗ button) → Dismiss; adds prop key to session dismissed set.

    Gesture threshold: 80 px in either direction.  While dragging the card
    rotates ±15 ° and fades toward the swipe direction.  Releasing below the
    threshold snaps the card back.

    JS communication bridge: two hidden native <button> elements with IDs
    'swipe-right-trigger' / 'swipe-left-trigger'.  The gesture JS calls
    .click() on these after the fly-out animation (300 ms), which fires
    NiceGUI's Python click handler.  Native <button> responds reliably to
    programmatic .click() calls (Vue's @click listener is wired to the DOM
    event, so dispatchEvent / .click() both work).
    """
    if not swipe_shown:
        # ── Empty state ──────────────────────────────────────────────────
        with ui.column().classes("w-full").style(
            f"align-items: center; padding: {t.SPACE_XL} {t.SPACE_LG}; "
            f"gap: {t.SPACE_MD};"
        ):
            ui.icon("done_all").style(f"font-size: 48px; color: {t.POS};")
            ui.label("All caught up!").style(
                f"font-size: 18px; font-weight: 800; color: {t.TEXT};"
            )
            ui.label("You've reviewed all props.").style(
                f"font-size: 13px; color: {t.TEXT_DIM}; text-align: center;"
            )
        return

    r         = swipe_shown[0]
    reviewed  = total_shown - len(swipe_shown)
    remaining = len(swipe_shown)

    # ── Counter ──────────────────────────────────────────────────────────
    ui.label(f"{reviewed} reviewed  ·  {remaining} remaining").style(
        f"font-size: 11px; color: {t.TEXT_DIM2}; text-align: center; "
        f"font-family: monospace; width: 100%; padding: 2px 0 4px;"
    )

    # ── Card + drag-direction hint ────────────────────────────────────────
    # The outer div is `position: relative` so the absolute hint overlay
    # is positioned relative to the card, not the viewport.
    with ui.element("div").style(
        "position: relative; width: 100%; max-width: 480px; margin: 0 auto;"
    ):
        # Translucent ✓ / ✗ label that fades in during drag (JS-controlled)
        ui.element("div").props('id="swipe-hint"').style(
            "position: absolute; top: 50%; left: 50%; z-index: 20; "
            "transform: translate(-50%, -50%); pointer-events: none; "
            "font-size: 72px; font-weight: 900; opacity: 0; "
            "transition: opacity 0.08s linear;"
        )
        # Swipe target wrapper: the drag CSS transform is applied here
        with ui.element("div").props('id="swipe-card-wrapper"').style(
            "cursor: grab; touch-action: none; will-change: transform; "
            "transform-origin: center 80%; "
            "transition: transform 0.3s ease, opacity 0.3s ease;"
        ):
            _prop_card(r, backend)

    # ── Track (right-swipe) action ────────────────────────────────────────
    async def _track():
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
                _amt = data.get("amount")
                _s   = (f" (${float(_amt):.2f})"
                        if isinstance(_amt, (int, float)) else "")
                ui.notify(
                    f"Tracked: {r.get('player')} {r.get('side')} "
                    f"{r.get('line')}{_s}",
                    type="positive",
                )
            elif "already tracked" in (data.get("error") or "").lower():
                ui.notify("Already tracked.", type="info")
            else:
                ui.notify(
                    f"Track failed: {data.get('error') or 'unknown'}",
                    type="negative",
                )
        except Exception as exc:                                          # noqa: BLE001
            ui.notify(f"Track failed: {exc}", type="negative")
        finally:
            dismissed.add(_prop_swipe_key(r))
            on_refresh()

    # ── Dismiss (left-swipe) action ───────────────────────────────────────
    def _dismiss():
        dismissed.add(_prop_swipe_key(r))
        on_refresh()

    # Hidden native buttons — the gesture JS calls .click() on these after
    # the card fly-out animation completes (300 ms delay in _SWIPE_JS).
    ui.element("button").props('id="swipe-right-trigger"').style(
        "position: absolute; width: 0; height: 0; opacity: 0; "
        "overflow: hidden; border: none; padding: 0; pointer-events: none;"
    ).on("click", _track)
    ui.element("button").props('id="swipe-left-trigger"').style(
        "position: absolute; width: 0; height: 0; opacity: 0; "
        "overflow: hidden; border: none; padding: 0; pointer-events: none;"
    ).on("click", _dismiss)

    # ── Visible action buttons ────────────────────────────────────────────
    with ui.row().classes("w-full").style(
        f"justify-content: center; gap: {t.SPACE_XL}; "
        f"padding: {t.SPACE_MD} 0 {t.SPACE_SM};"
    ):
        ui.button("✗", on_click=_dismiss).props("no-caps unelevated").style(
            "background: rgba(239, 68, 68, 0.12); color: #ef4444; "
            "border: 2px solid #ef4444; border-radius: 50%; "
            "width: 60px; height: 60px; font-size: 24px; font-weight: 900; "
            "min-height: 0; padding: 0;"
        )
        ui.button("✓", on_click=_track).props("no-caps unelevated").style(
            "background: rgba(34, 197, 94, 0.12); color: #22c55e; "
            "border: 2px solid #22c55e; border-radius: 50%; "
            "width: 60px; height: 60px; font-size: 24px; font-weight: 900; "
            "min-height: 0; padding: 0;"
        )

    # Attach the gesture listeners after NiceGUI has committed this render
    # to the DOM.  The JS has a self-retry loop (setTimeout 60 ms) in case
    # the element isn't in the DOM yet on the first tick.
    ui.run_javascript(_SWIPE_JS)


# ── X-Ray table ──────────────────────────────────────────────────────────────
# Column spec tuples: (header_label, sort_key_or_None, right_align, flex_css)
# flex_css is applied identically to both the sticky header cell and every
# data cell so the columns stay pixel-perfect aligned.
_XRAY_COLS: tuple = (
    ("PLAYER", "player", False, "flex: 1 1 180px; min-width: 160px;"),
    ("LINE",   "line",   True,  "flex: 0 0 80px;"),
    ("ODDS",   "odds",   True,  "flex: 0 0 72px;"),
    ("CONF",   "conf",   True,  "flex: 0 0 72px;"),
    ("EV",     "ev",     True,  "flex: 0 0 90px;"),
    ("L5",     "l5",     True,  "flex: 0 0 110px;"),
    ("L10",    "l10",    True,  "flex: 0 0 110px;"),
    ("SZN",    "szn",    True,  "flex: 0 0 72px;"),
    ("TRACK",  None,     True,  "flex: 0 0 90px;"),
)

# Sum of fixed-column flex-basis values + PLAYER min-width + a bit of padding
# headroom.  Drives min-width on every row so the table doesn't collapse
# before the outer overflow-x: auto wrapper kicks in.
_XRAY_MIN_W = "900px"


def _xray_sort_rows(rows: list[dict], xs: dict) -> list[dict]:
    """Sort *rows* for the X-Ray table.  xs = {col, asc}."""
    col = xs.get("col") or "conf"
    asc = xs.get("asc", False)

    def _key(r):
        s = r.get("summary") or {}
        if col == "player":
            return (r.get("player") or "").lower()
        if col == "line":
            try:
                return float(r.get("line") or 0)
            except (TypeError, ValueError):
                return 0.0
        if col == "odds":
            try:
                return int(r.get("best_odds") or -9999)
            except (TypeError, ValueError):
                return -9999
        if col == "conf":
            return float(r.get("confidence") or 0)
        if col == "ev":
            v = r.get("ev_pct")
            try:
                return float(v) if v is not None else float("-inf")
            except (TypeError, ValueError):
                return float("-inf")
        if col == "l5":
            return _window_hit_rate(r, "last_5")
        if col == "l10":
            return _window_hit_rate(r, "last_10")
        if col == "szn":
            v = s.get("season_avg")
            try:
                return float(v) if v is not None else -1.0
            except (TypeError, ValueError):
                return -1.0
        return 0

    return sorted(rows, key=_key, reverse=not asc)


def _render_xray_mode(shown: list[dict], backend,
                      xray_sort: dict, on_refresh) -> None:
    """Condensed, sortable data table: one prop per row.

    Uses a flexbox-based grid (not <table>) so NiceGUI's div wrappers
    around child elements don't break table-cell alignment.  Column widths
    are kept identical between the sticky header row and data rows via the
    shared _XRAY_COLS flex specs.

    Sort state is held in the caller's *xray_sort* dict (col, asc).
    Clicking a header mutates the dict in-place and calls *on_refresh*
    (= cards_refresh.refresh), which re-renders the whole cards area with
    the new sort applied.  The dict survives re-renders because it lives in
    the _section_unified_props_list closure.
    """
    sorted_rows = _xray_sort_rows(shown, xray_sort)
    active_col  = xray_sort.get("col") or "conf"
    is_asc      = xray_sort.get("asc", False)

    # Shared vertical cell padding — identical for header and data rows.
    _PAD = "0 12px"

    def _mk_sort_fn(k: str):
        def _do():
            if xray_sort["col"] == k:
                xray_sort["asc"] = not xray_sort["asc"]
            else:
                xray_sort["col"] = k
                # player name → ascending; all numeric columns → descending
                xray_sort["asc"] = (k == "player")
            on_refresh()
        return _do

    with ui.element("div").style(
        "overflow-x: auto; -webkit-overflow-scrolling: touch; width: 100%; "
        f"border-radius: {t.RADIUS_MD}; border: 1px solid {t.BORDER};"
    ):
        # ── Sticky header row ────────────────────────────────────────────
        with ui.element("div").style(
            f"display: flex; flex-direction: row; align-items: center; "
            f"background: {t.CARD_HI}; border-bottom: 2px solid {t.BORDER}; "
            f"position: sticky; top: 0; z-index: 10; "
            f"min-width: {_XRAY_MIN_W};"
        ):
            for hdr, sort_key, right_align, flex in _XRAY_COLS:
                is_active = bool(sort_key and active_col == sort_key)
                arrow     = (" ▲" if is_asc else " ▼") if is_active else ""
                justify   = "flex-end" if right_align else "flex-start"
                hdr_cell  = ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: {justify}; padding: {_PAD}; height: 36px; "
                    f"cursor: {'pointer' if sort_key else 'default'}; "
                    f"user-select: none; white-space: nowrap; "
                    f"font-size: 9px; font-weight: 800; letter-spacing: .6px; "
                    f"color: {t.TEXT if is_active else t.TEXT_DIM2};"
                )
                with hdr_cell:
                    ui.label(hdr + arrow)
                if sort_key:
                    hdr_cell.on("click", _mk_sort_fn(sort_key))

        # ── Data rows ────────────────────────────────────────────────────
        for i, r in enumerate(sorted_rows):
            row_bg  = t.CARD if i % 2 == 0 else "rgba(255,255,255,0.025)"
            side    = (r.get("side") or "Over").strip().title()
            is_over = side == "Over"
            summary = r.get("summary") or {}

            with ui.element("div").style(
                f"display: flex; flex-direction: row; align-items: center; "
                f"background: {row_bg}; min-width: {_XRAY_MIN_W}; height: 44px; "
                f"border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                # PLAYER — avatar + linked name + market label
                _, _, _, flex = _XRAY_COLS[0]
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"padding: {_PAD}; gap: 8px; overflow: hidden;"
                ):
                    _card_avatar(r)
                    with ui.column().style("gap: 0; min-width: 0; flex: 1 1 0;"):
                        _slug = (r.get("player") or "").lower().replace(" ", "-")
                        ui.link(
                            r.get("player") or "—",
                            f"/player/mlb/{_slug}",
                        ).style(
                            f"font-size: 12px; font-weight: 700; color: {t.TEXT}; "
                            f"text-decoration: none; white-space: nowrap; "
                            f"overflow: hidden; text-overflow: ellipsis; "
                            f"display: block; max-width: 140px;"
                        )
                        ui.label(
                            _short_market(r.get("market", "")).upper()
                        ).style(
                            f"font-size: 8px; font-weight: 800; letter-spacing: .4px; "
                            f"color: {t.TEXT_DIM2};"
                        )

                # LINE — "O 5.5" / "U 2.5" coloured pill
                _, _, _, flex = _XRAY_COLS[1]
                chip_bg = t.POS if is_over else t.NEG
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: center; padding: {_PAD};"
                ):
                    ui.label(
                        f"{'O' if is_over else 'U'} {r.get('line', '—')}"
                    ).style(
                        f"background: {chip_bg}; color: {t.BG}; "
                        f"font-size: 11px; font-weight: 800; letter-spacing: .3px; "
                        f"padding: 2px 8px; border-radius: {t.RADIUS_SM}; "
                        f"font-family: monospace; white-space: nowrap;"
                    )

                # ODDS
                _, _, _, flex = _XRAY_COLS[2]
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: center; padding: {_PAD};"
                ):
                    ui.label(_odds_str(r.get("best_odds"))).style(
                        f"font-size: 12px; font-weight: 700; "
                        f"font-family: monospace; color: {t.TEXT_DIM};"
                    )

                # CONF — threshold-coloured percentage
                _, _, _, flex = _XRAY_COLS[3]
                conf     = float(r.get("confidence") or 0)
                if conf >= 0.65:   conf_col = t.POS
                elif conf >= 0.55: conf_col = t.WARN
                else:              conf_col = t.TEXT_DIM
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: center; padding: {_PAD};"
                ):
                    ui.label(f"{conf * 100:.0f}%").style(
                        f"font-size: 13px; font-weight: 800; "
                        f"font-family: monospace; color: {conf_col};"
                    )

                # EV — reuse _ev_badge(compact=True)
                _, _, _, flex = _XRAY_COLS[4]
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: center; padding: {_PAD};"
                ):
                    _ev_badge(r.get("ev_pct"), compact=True)

                # L5 + L10 — coloured pills + ROI sub-text via _hr_cell_bg
                _XRAY_WIN_ROI = {"last_5": "l5_roi", "last_10": "l10_roi"}
                for w_idx, w_key in enumerate(("last_5", "last_10")):
                    _, _, _, flex = _XRAY_COLS[5 + w_idx]
                    hits  = int(summary.get(f"{w_key}_hits")  or 0)
                    total = int(summary.get(f"{w_key}_games") or 0)
                    roi_s = _roi_str(summary.get(_XRAY_WIN_ROI[w_key]))
                    with ui.element("div").style(
                        f"{flex}; display: flex; flex-direction: column; "
                        f"align-items: center; justify-content: center; "
                        f"padding: {_PAD}; gap: 1px;"
                    ):
                        if not total:
                            ui.label("—").style(
                                f"font-family: monospace; color: {t.TEXT_DIM2};"
                            )
                        else:
                            pct              = hits / total
                            cell_bg, colored = _hr_cell_bg(pct)
                            lbl_txt          = f"{hits}/{total} · {int(round(pct * 100))}%"
                            if colored:
                                ui.label(lbl_txt).style(
                                    f"background: {cell_bg}; color: #ffffff; "
                                    f"font-size: 10.5px; font-weight: 800; "
                                    f"padding: 2px 7px; border-radius: {t.RADIUS_PILL}; "
                                    f"font-family: monospace; white-space: nowrap;"
                                )
                            else:
                                ui.label(lbl_txt).style(
                                    f"font-size: 10.5px; color: {t.TEXT}; "
                                    f"font-family: monospace; white-space: nowrap;"
                                )
                            if roi_s:
                                try:
                                    roi_col = t.POS if float(roi_s.rstrip("%")) > 0 else t.NEG
                                except (ValueError, AttributeError):
                                    roi_col = t.TEXT_DIM2
                                ui.label(roi_s).style(
                                    f"font-size: 8px; font-weight: 800; color: {roi_col}; "
                                    f"font-family: monospace; letter-spacing: .2px;"
                                )

                # SZN — season average, neutral background; ROI sub-text in green/red
                _, _, _, flex = _XRAY_COLS[7]
                sa    = summary.get("season_avg")
                szn_r = _roi_str(summary.get("szn_roi"))
                with ui.element("div").style(
                    f"{flex}; display: flex; flex-direction: column; "
                    f"align-items: center; justify-content: center; "
                    f"padding: {_PAD}; gap: 1px;"
                ):
                    ui.label("—" if sa is None else f"{sa:.2f}").style(
                        f"font-size: 12px; color: {t.TEXT_DIM}; "
                        f"font-family: monospace;"
                    )
                    if szn_r:
                        try:
                            szn_roi_col = t.POS if float(szn_r.rstrip("%")) > 0 else t.NEG
                        except (ValueError, AttributeError):
                            szn_roi_col = t.TEXT_DIM2
                        ui.label(szn_r).style(
                            f"font-size: 8px; font-weight: 800; color: {szn_roi_col}; "
                            f"font-family: monospace; letter-spacing: .2px;"
                        )

                # TRACK — reuse the existing Track button
                _, _, _, flex = _XRAY_COLS[8]
                with ui.element("div").style(
                    f"{flex}; display: flex; align-items: center; "
                    f"justify-content: flex-end; padding: {_PAD};"
                ):
                    _track_btn(r, backend)


# Meta-Consensus badge: only UNANIMOUS / STRONG get a card badge.  Gold (amber)
# for unanimous, green for strong.  Uses the existing palette only.
_CONSENSUS_BADGE = {
    "UNANIMOUS": ("★ UNANIMOUS", t.WARN),
    "STRONG":    ("✓ STRONG",   t.POS),
}


def _consensus_badge(r: dict) -> None:
    """Render the meta-consensus card badge for *r* when its tier is UNANIMOUS
    or STRONG.  Silent no-op when consensus data is missing/stale/other tier."""
    try:
        from services import meta_consensus
        entry = meta_consensus.consensus_for(r)
    except Exception:                                                     # noqa: BLE001
        return
    spec = _CONSENSUS_BADGE.get((entry or {}).get("tier"))
    if not spec:
        return
    label, color = spec
    ui.label(label).style(
        f"background: {color}; color: {t.BG}; font-size: 8.5px; "
        f"font-weight: 800; letter-spacing: .4px; padding: 2px 7px; "
        f"border-radius: {t.RADIUS_PILL}; flex-shrink: 0; white-space: nowrap;"
    ).tooltip((entry or {}).get("compound_reason") or "")


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
    try:
        confidence_pct = float(r.get("confidence") or 0.0) * 100
    except (TypeError, ValueError):
        confidence_pct = 0.0

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; gap: {t.SPACE_SM}; "
        f"min-width: 0;"
    ):
        # Header row: player headshot + market chip on the left, matchup
        # on the right.
        with ui.row().classes("items-center w-full").style(
            f"gap: 8px;"
        ):
            _card_avatar(r)
            ui.label(_short_market(r.get("market") or "").upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            _consensus_badge(r)
            ui.label(r.get("team") or "").style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; "
                f"font-family: monospace; "
                f"margin-left: auto;"
            )

        # Player name: links to player profile page.
        _player = r.get("player") or "—"
        _name_slug = _player.lower().replace(" ", "-")
        ui.link(_player, f"/player/mlb/{_name_slug}").style(
            f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
            f"line-height: 1.2; text-decoration: none; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        ).tooltip("View player profile")

        # Pick row: chip + line + confidence.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; flex-wrap: nowrap;"
        ):
            _line_disp = r.get("line")
            _line_disp = "—" if _line_disp is None else _line_disp
            ui.label(f"{side.upper()} {_line_disp}").style(
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
                line_f    = float(r.get("line"))
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

        # ── AI summary (short, cached; never generated at render) ────────
        _card_ai_summary(r)

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


def _headshot_url(player_id) -> str:
    """MLB Stats API headshot URL.  The d_people:generic default transform
    makes it fall back to a generic silhouette for unknown/invalid ids."""
    return (
        f"https://img.mlbstatic.com/mlb-photos/image/upload/"
        f"d_people:generic:headshot:67:current.png/w_213,q_auto:best/"
        f"v1/people/{player_id}/headshot/67/current"
    )


def _avatar_img_html(player_id, name: str) -> str:
    url = _headshot_url(player_id)
    return (
        f'<img src="{url}" alt="{name}" '
        f'style="width:34px; height:34px; border-radius:50%; '
        f'object-fit:cover; background:{t.CARD_HI}; '
        f'border:1px solid {t.BORDER}; flex-shrink:0;" '
        f'onerror="this.style.opacity=\'.35\';"/>'
    )


def _card_avatar(r: dict) -> None:
    """Small circular player headshot on the left of a prop card (FIX 6),
    matching the player-profile header style.  Uses the player_id stamped
    on the pick when present; otherwise shows the generic silhouette and
    resolves the real id lazily on a background thread (cached) so the
    page never blocks on a name->id lookup."""
    name = r.get("player") or ""
    pid  = r.get("player_id")
    if pid:
        ui.html(_avatar_img_html(pid, name)).style("flex-shrink: 0;")
        return
    holder = ui.html(_avatar_img_html("generic", name)).style("flex-shrink: 0;")

    async def _resolve() -> None:                                         # noqa: WPS430
        try:
            from src.player_profile_client import search_player_by_name
            rid = await asyncio.to_thread(search_player_by_name, name)
        except Exception:                                                 # noqa: BLE001
            rid = None
        if rid:
            holder.set_content(_avatar_img_html(rid, name))

    if name:
        ui.timer(0.05, _resolve, once=True)


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


def _hr_cell_bg(pct: float) -> tuple[str, bool]:
    """Return (cell_bg_color, is_colored) for a hit-rate fraction (0..1).

    Used by _card_summary_chips (prop card) and _render_xray_mode (table)
    so the colour thresholds live in exactly one place.
    """
    if pct >= 0.70:
        return "#22c55e", True   # bright green
    if pct >= 0.55:
        return "#84cc16", True   # yellow-green
    if pct >= 0.40:
        return t.CARD_HI, False  # neutral — no colour block
    return "#ef4444", True       # red


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

    # (label, value, sub, cell_bg, is_colored, roi_str)
    # cell_bg / is_colored control whether the cell gets a colored background
    # with white text (hit-rate cells) or stays neutral (SEASON avg).
    # roi_str is a pre-formatted "+14.2%" / "-3.1%" or None when unavailable.
    cells: list[tuple[str, str, str, str, bool, str | None]] = []

    # Window key → ROI summary key
    _WIN_ROI = {"last_5": "l5_roi", "last_10": "l10_roi", "last_20": "l20_roi"}

    sa = summary.get("season_avg")
    cells.append((
        "SEASON",
        "—" if sa is None else f"{sa:.2f}",
        f"avg/{summary.get('season_games') or 0}g",
        t.CARD_HI, False,            # SEASON is an avg, never color-coded
        _roi_str(summary.get("szn_roi")),
    ))
    for w_key, w_label in (
        ("last_5",  "L5"),
        ("last_10", "L10"),
        ("last_20", "L20"),
    ):
        hits  = summary.get(f"{w_key}_hits") or 0
        total = summary.get(f"{w_key}_games") or 0
        if not total:
            cells.append((w_label, "—", "n/a", t.CARD_HI, False, None))
            continue
        pct = hits / total
        bg, colored = _hr_cell_bg(pct)
        cells.append((
            w_label, f"{hits}/{total}",
            f"{int(round(pct * 100))}%",
            bg, colored,
            _roi_str(summary.get(_WIN_ROI[w_key])),
        ))
    h2h_hits  = summary.get("h2h_hits") or 0
    h2h_total = summary.get("h2h_games") or 0
    if not h2h_total:
        cells.append(("H2H", "—", "n/a", t.CARD_HI, False, None))
    else:
        pct = h2h_hits / h2h_total
        bg, colored = _hr_cell_bg(pct)
        cells.append((
            "H2H", f"{h2h_hits}/{h2h_total}",
            f"{int(round(pct * 100))}%",
            bg, colored, None,           # H2H ROI not in spec
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
            "display: grid; grid-template-columns: repeat(auto-fit, minmax(60px, 1fr)); "
            "gap: 4px; width: 100%;"
        ):
            for label, value, sub, cell_bg, is_colored, roi_str in cells:
                lbl_color = "rgba(255,255,255,0.75)" if is_colored else t.TEXT_DIM2
                val_color = "#ffffff"                 if is_colored else t.TEXT
                sub_color = "rgba(255,255,255,0.80)" if is_colored else t.TEXT_DIM2
                with ui.column().style(
                    f"background: {cell_bg}; "
                    f"border-radius: {t.RADIUS_SM}; "
                    f"padding: 6px 4px; align-items: center; "
                    f"gap: 1px; min-width: 0;"
                ):
                    ui.label(label).style(
                        f"font-size: 8.5px; font-weight: 800; letter-spacing: .4px; "
                        f"color: {lbl_color};"
                    )
                    ui.label(value).style(
                        f"font-size: 12px; font-weight: 800; "
                        f"color: {val_color}; font-family: monospace;"
                    )
                    if sub:
                        ui.label(sub).style(
                            f"font-size: 8.5px; color: {sub_color}; "
                            f"font-family: monospace;"
                        )
                    if roi_str:
                        # On a coloured cell bg (green/red) use translucent
                        # white so the ROI is legible.  On neutral (SEASON)
                        # use the standard green/red colour as the spec asks.
                        if is_colored:
                            roi_col = "rgba(255,255,255,0.85)"
                        else:
                            try:
                                roi_col = t.POS if float(roi_str.rstrip("%")) > 0 else t.NEG
                            except (ValueError, AttributeError):
                                roi_col = t.TEXT_DIM2
                        ui.label(roi_str).style(
                            f"font-size: 8px; font-weight: 800; color: {roi_col}; "
                            f"font-family: monospace; letter-spacing: .2px;"
                        )


def _card_ai_summary(r: dict) -> None:
    """The prop's AI breakdown verdict -- the SAME artifact the player page
    shows (player_ai_breakdown), not a separate blurb.  Reads it from cache;
    while the background breakdown queue is still generating this one, shows a
    loading state and polls until it lands, then renders the verdict (matches
    the player page's 'loading -> appears' behaviour)."""
    from src import player_ai_breakdown as _pab

    holder = ui.column().classes("w-full").style("gap: 0; min-width: 0;")

    def _ai_box(text: str, tier: str = "", version: str = "") -> None:
        # Outline by AI-vs-model agreement: green = AI backs the model's side,
        # red = AI fades it, neutral border otherwise.
        ocolor = {"pos": t.POS, "neg": t.NEG}.get(
            _pab.agreement_outline_token(tier), t.BORDER)
        with ui.column().classes("w-full").style(
            f"gap: 4px; padding: 6px 8px; "
            f"background: {t.CARD_HI}; border-radius: {t.RADIUS_SM}; "
            f"border: 2px solid {ocolor}; min-width: 0;"
        ):
            with ui.row().classes("items-center w-full").style("gap: 6px;"):
                ui.label("AI").style(
                    f"font-size: 8px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.PRIMARY_HI}; background: {t.CARD}; "
                    f"padding: 1px 5px; border-radius: {t.RADIUS_PILL}; flex-shrink: 0;"
                )
                if version:
                    ui.label(version).style(
                        f"margin-left: auto; font-size: 8px; font-weight: 800; "
                        f"color: {t.TEXT_DIM2}; background: {t.CARD}; "
                        f"padding: 1px 5px; border-radius: {t.RADIUS_PILL}; "
                        f"font-family: monospace;"
                    ).tooltip("AI model version")
            ui.label(text).style(
                f"font-size: 11px; color: {t.TEXT_DIM}; line-height: 1.35; "
                f"font-style: italic; white-space: normal;"
            )

    def _loading() -> None:
        with ui.row().classes("items-center w-full").style("gap: 6px; padding: 6px 8px;"):
            ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
            ui.label("Generating AI breakdown…").style(
                f"font-size: 11px; color: {t.TEXT_DIM2}; font-style: italic;")

    def _verdict(mem_only: bool) -> tuple[str, str, str] | None:
        try:
            bd = (_pab.peek_breakdown_mem(r) if mem_only
                  else _pab.peek_breakdown(r)) or {}
        except Exception:                                                 # noqa: BLE001
            bd = {}
        v = (bd.get("verdict") or "").strip()
        return (v, (bd.get("verdict_tier") or "").strip(),
                (bd.get("model_version") or "").strip()) if v else None

    # First paint: one Supabase-backed read (handles already-cached props).
    v0 = _verdict(mem_only=False)
    with holder:
        (_ai_box(*v0) if v0 else _loading())

    if v0:
        return

    # Not cached yet -> poll cheaply (memory-only; the in-process queue fills
    # _MEM_CACHE as it generates) and render the moment it arrives.  Stop after
    # ~5 min so a never-generated prop doesn't poll forever.
    attempts = {"n": 0}

    def _poll() -> None:
        attempts["n"] += 1
        v = _verdict(mem_only=True)
        if v:
            holder.clear()
            with holder:
                _ai_box(*v)
            timer.active = False
        elif attempts["n"] >= 100:        # ~5 min at 3 s
            holder.clear()                # give up quietly (matches player page)
            timer.active = False

    timer = ui.timer(3.0, _poll, active=True)


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


def _roi_str(roi) -> str | None:
    """Format a ROI% value as '+14.2%' / '-3.1%', or None when absent."""
    if roi is None:
        return None
    try:
        return f"{float(roi):+.1f}%"
    except (TypeError, ValueError):
        return None


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

    Persistence: if this prop is already tracked (open in My Bets), render a
    static "Tracked ✓" chip instead of a live button so the tracked state
    survives page reloads -- parity with the game-pick Track buttons.
    """
    try:
        from src import props_picks_tracker as _ppt
        if _ppt.is_tracked(r.get("player"), r.get("market"),
                           r.get("line"), r.get("side"), r.get("event_id")):
            ui.label("Tracked ✓").style(
                f"background: {t.CARD_HI}; color: {t.POS}; "
                f"border: 1px solid {t.POS}; font-weight: 800; "
                f"font-size: 10.5px; letter-spacing: .4px; "
                f"padding: 4px 10px; border-radius: {t.RADIUS_SM};"
            )
            return
    except Exception:                                                     # noqa: BLE001
        pass

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
                _amt = data.get("amount")
                _amt_s = (f" (${float(_amt):.2f})"
                          if isinstance(_amt, (int, float)) else "")
                ui.notify(
                    f"Tracked: {r.get('player')} {r.get('side')} "
                    f"{r.get('line')}{_amt_s}",
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
