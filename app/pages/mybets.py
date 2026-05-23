"""
My Bets page.

Shows:
  - Personal bankroll snapshot (start / current / P&L)
  - Tabs: MLB  |  WNBA  |  PROPS
      MLB / WNBA: confirmed game bets (open + settled history)
      PROPS:      player-prop picks tracked from the Props page

The model's own auto-picks live on the Model page -- this page is the
personal-bankroll side only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, sidebar, bottom_nav, track_button


def register(backend) -> None:
    @ui.page("/mybets")
    def mybets_page():
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_MYBETS)
        with ui.row().classes("no-wrap w-full").style("gap: 0;"):
            sidebar.render(backend)
            with ui.column().classes("page-content").style(
                f"flex: 1; max-width: {t.MAX_CONTENT_W}; "
                f"gap: {t.SPACE_LG}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _personal_bankroll(backend)
                _recommendations_section(backend)
                _tabs(backend)
        bottom_nav.render(active=t.TAB_MYBETS)


# ── Bankroll summary ─────────────────────────────────────────────────────────

def _personal_bankroll(backend) -> None:
    try:
        mlb  = backend.Ledger(path="data/ledger.json",      starting_bankroll=1000.0)
        wnba = backend.Ledger(path="data/wnba_ledger.json", starting_bankroll=1000.0)
        s = mlb.get_summary()
        start   = float(s.get("personal_starting_bankroll", 1000))
        current = float(s.get("personal_bankroll", start))
        pnl     = current - start
        open_confirmed = (
            [b for b in (mlb.data.get("open_bets")  or []) if b.get("confirmed")]
            + [b for b in (wnba.data.get("open_bets") or []) if b.get("confirmed")]
        )
        at_risk = sum(float(b.get("confirmed_amount") or 0) for b in open_confirmed)
    except Exception:                                                      # noqa: BLE001
        start, current, pnl, at_risk = 1000.0, 1000.0, 0.0, 0.0

    pnl_color = t.POS if pnl >= 0 else t.NEG
    pnl_sign  = "+" if pnl >= 0 else "−"

    with ui.row().classes("w-full hero-stats").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; "
        f"gap: {t.SPACE_XL};"
    ):
        _stat("START",   f"${start:,.2f}",            t.TEXT_DIM)
        _stat("CURRENT", f"${current:,.2f}",           t.TEXT)
        _stat("P / L",   f"{pnl_sign}${abs(pnl):,.2f}", pnl_color)
        _stat("AT RISK", f"${at_risk:,.2f}",           t.WARN)

    # Today's conservative bet budget (FIX 4).
    budget = _todays_budget(current)
    ui.label(
        f"Today's Budget: ${budget['total']:,.2f} total "
        f"/ ${budget['max_per_bet']:,.2f} max per bet"
    ).style(
        f"font-size: 12px; font-weight: 700; color: {t.TEXT_DIM}; "
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 8px 12px; width: 100%;"
    ).tooltip(
        "Conservative daily cap: 20% of your bankroll across all bets, "
        "5% on any single bet. Recalculated each night at 2 AM ET."
    )


def _todays_budget(current_bankroll: float) -> dict:
    """Today's persisted budget from Supabase, or a live fallback computed
    off the current personal bankroll when none is stored yet."""
    from src.ledger import compute_daily_budget
    try:
        from src import db
        row = db.cache_get("daily_budget")
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if (isinstance(row, dict) and row.get("date") == today
                and isinstance(row.get("data"), dict)
                and "total" in row["data"]):
            return row["data"]
    except Exception:                                                      # noqa: BLE001
        pass
    return compute_daily_budget(current_bankroll)


def _stat(label: str, value: str, color: str) -> None:
    with ui.column().style("gap: 4px;"):
        ui.label(label).style(
            f"font-size: 10px; font-weight: 700; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )
        ui.label(value).classes("stat-value").style(
            f"font-size: 20px; font-weight: 800; color: {color}; "
            f"font-family: monospace; letter-spacing: -.2px;"
        )


# ── Today's Recommendations ──────────────────────────────────────────────────

def _recommendations_section(backend) -> None:
    """Section at the top of the page listing every model pick for today
    that hasn't been tracked yet -- the same picks shown on the home
    page game cards.  Each row has its own Track button; tracking a pick
    refreshes the section so it drops out (and appears in the tracked
    list once the user switches tabs / reloads)."""
    try:
        backend.hydrate_state()
    except Exception:                                                      # noqa: BLE001
        pass

    # Paging: show 5 game + 5 prop picks per page; "Show more" advances to
    # the next 5 of each (each list wraps independently so a page is never
    # empty), cycling back to the top at the end.
    page = {"i": 0}
    PAGE = 5

    @ui.refreshable
    def render() -> None:                                                  # noqa: WPS430
        game_picks = _build_recommendations(backend)
        prop_picks = _build_prop_recommendations(backend)
        total = len(game_picks) + len(prop_picks)

        def _ceil_pages(n: int) -> int:
            return max(1, -(-n // PAGE))

        n_game_pages = _ceil_pages(len(game_picks))
        n_prop_pages = _ceil_pages(len(prop_picks))
        n_pages = max(n_game_pages, n_prop_pages)
        if page["i"] >= n_pages:
            page["i"] = 0

        # Each list wraps on its own page count -> always full when non-empty.
        g_start = (page["i"] % n_game_pages) * PAGE
        p_start = (page["i"] % n_prop_pages) * PAGE
        game_page = game_picks[g_start:g_start + PAGE]
        prop_page = prop_picks[p_start:p_start + PAGE]

        def _show_more() -> None:
            page["i"] = (page["i"] + 1) % n_pages
            render.refresh()

        with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
            with ui.row().classes("items-center w-full").style("gap: 8px;"):
                ui.label("TODAY'S RECOMMENDATIONS").style(
                    f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT};"
                )
                ui.label(str(total)).style(
                    f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                    f"font-size: 11px; font-weight: 700; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
                )
                ui.element("div").style("flex: 1;")
                if n_pages > 1:
                    ui.button(
                        f"Show more · {page['i'] + 1}/{n_pages}",
                        on_click=_show_more,
                    ).props("no-caps unelevated dense").style(
                        f"background: {t.CARD_HI}; color: {t.TEXT}; "
                        f"font-size: 10.5px; font-weight: 800; "
                        f"letter-spacing: .4px; padding: 4px 12px; "
                        f"border-radius: {t.RADIUS_SM}; min-height: 0;"
                    )
            if total == 0:
                ui.label(
                    "No untracked model picks for today. Run analysis from "
                    "the home page, or you've already tracked everything."
                ).style(
                    f"color: {t.TEXT_DIM}; font-size: 12px; "
                    f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                    f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                    f"text-align: center; font-style: italic;"
                )
                return

            if game_page:
                _rec_subheader("GAME PICKS", len(game_picks))
                for p in game_page:
                    _recommendation_row(backend, p, on_tracked=render.refresh)
            if prop_page:
                _rec_subheader("PROP PICKS", len(prop_picks))
                for p in prop_page:
                    _prop_recommendation_row(backend, p, on_tracked=render.refresh)

    render()


def _rec_subheader(label: str, count: int) -> None:
    with ui.row().classes("items-center w-full").style("gap: 6px; margin-top: 4px;"):
        ui.label(label).style(
            f"font-size: 10.5px; font-weight: 800; letter-spacing: .6px; "
            f"color: {t.TEXT_DIM};"
        )
        ui.label(str(count)).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 700; "
            f"padding: 1px 6px; border-radius: {t.RADIUS_PILL};"
        )


def _build_recommendations(backend) -> list[dict]:
    """Flatten today's model picks (both sports) into per-pick dicts,
    dropping any that are already tracked.  Sorted by confidence DESC."""
    out: list[dict] = []
    states = (
        ("mlb",  getattr(backend, "_analysis_state", {}) or {}),
        ("wnba", getattr(backend, "_wnba_analysis_state", {}) or {}),
    )
    for sport, state in states:
        for g in (state.get("results") or []):
            if g.get("_no_model") or g.get("_no_odds"):
                continue
            gid = g.get("id") or g.get("game_id")
            if not gid:
                continue
            tracked = track_button.tracked_bet_types(backend, gid, sport)
            matchup = f"{g.get('away_team', '')} @ {g.get('home_team', '')}".strip(" @")

            # Moneyline (both sports)
            if g.get("pick_team") and "single" not in tracked:
                out.append({
                    "sport": sport, "game_id": gid, "bet_type": "ml",
                    "team": g.get("pick_team"), "line": "",
                    "odds": g.get("pick_odds"), "conf": g.get("pick_prob"),
                    "matchup": matchup, "type_label": "Moneyline",
                })

            if sport != "mlb":
                continue   # RL / totals tracking is MLB-only

            rl = g.get("run_line") or {}
            if rl.get("pick_team") and "run_line" not in tracked:
                pt = rl.get("run_line_point")
                line = f"{float(pt):+g}" if isinstance(pt, (int, float)) else ""
                out.append({
                    "sport": sport, "game_id": gid, "bet_type": "rl",
                    "team": rl.get("pick_team"), "line": line,
                    "odds": rl.get("pick_odds"), "conf": rl.get("pick_prob"),
                    "matchup": matchup, "type_label": "Run Line",
                })

            tot = g.get("totals") or {}
            if tot.get("total_line") and "totals" not in tracked:
                direction = (tot.get("direction") or "over").title()
                out.append({
                    "sport": sport, "game_id": gid, "bet_type": "total",
                    "team": f"{direction} {tot.get('total_line')}", "line": "",
                    "odds": tot.get("pick_odds"), "conf": tot.get("pick_prob"),
                    "matchup": matchup, "type_label": "Total",
                })

    out.sort(key=lambda p: -float(p.get("conf") or 0.0))
    return out


def _recommendation_row(backend, p: dict, *, on_tracked) -> None:
    """One untracked-pick row: team + matchup + detail + Track button."""
    sport   = p.get("sport") or "mlb"
    conf    = p.get("conf")
    conf_s  = f"{int(round(float(conf) * 100))}%" if isinstance(conf, (int, float)) else "—"
    odds_s  = _odds_str(p.get("odds"))
    detail  = p.get("type_label") or ""
    if p.get("line"):
        detail += f" {p['line']}"
    if odds_s != "—":
        detail += f" ({odds_s})"

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 10px;"
    ):
        ui.label(sport.upper()).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL}; flex-shrink: 0;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(p.get("team") or "—").style(
                f"font-size: 13px; font-weight: 800; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{detail}  ·  {conf_s}  ·  {p.get('matchup', '')}").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; "
                f"font-family: monospace; white-space: normal;"
            )
        track_button.render(
            backend, game_id=p.get("game_id"), sport=sport, size="sm",
            bet_type=p.get("bet_type", "ml"), on_tracked=on_tracked,
        )


def _build_prop_recommendations(backend) -> list[dict]:
    """Today's scored player-prop picks minus those already tracked,
    sorted by confidence DESC and normalized to the recommendation-row
    shape.  Best-effort -- returns [] on any error."""
    try:
        from src.props_scored_cache import load_scored_props
        from src import props_picks_tracker
    except Exception:                                                      # noqa: BLE001
        return []

    try:
        picks = (load_scored_props() or {}).get("picks") or []
    except Exception:                                                      # noqa: BLE001
        picks = []

    def _key(d: dict) -> tuple:
        return (
            d.get("player"),
            d.get("market"),
            round(float(d.get("line") or 0), 2),
            (d.get("side") or "").strip().title(),
        )

    try:
        open_keys = {_key(p) for p in props_picks_tracker.get_open()}
    except Exception:                                                      # noqa: BLE001
        open_keys = set()

    out: list[dict] = []
    for r in picks:
        if _key(r) in open_keys:
            continue
        out.append({
            "player":  r.get("player"),
            "market":  r.get("market"),
            "line":    r.get("line"),
            "side":    r.get("side"),
            "odds":    r.get("best_odds"),
            "conf":    r.get("confidence"),
            "team":    r.get("team"),
            "matchup": f"{r.get('away_team', '')} @ {r.get('home_team', '')}".strip(" @"),
            "raw":     r,
        })
    out.sort(key=lambda p: -float(p.get("conf") or 0.0))
    return out


def _prop_recommendation_row(backend, p: dict, *, on_tracked) -> None:
    """One untracked prop-pick row with a Track button posting to
    /api/props/track (in-process Flask client, same as the Props page)."""
    conf   = p.get("conf")
    conf_s = f"{int(round(float(conf) * 100))}%" if isinstance(conf, (int, float)) else "—"
    odds_s = _odds_str(p.get("odds"))
    market = (p.get("market") or "").replace("_", " ").title()
    side   = (p.get("side") or "").title()
    line   = p.get("line")
    detail = f"{side} {line} {market}".strip()
    if odds_s != "—":
        detail += f" ({odds_s})"

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 10px;"
    ):
        ui.label("PROP").style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL}; flex-shrink: 0;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(p.get("player") or "—").style(
                f"font-size: 13px; font-weight: 800; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{detail}  ·  {conf_s}  ·  {p.get('matchup', '')}").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; "
                f"font-family: monospace; white-space: normal;"
            )
        _prop_track_button(backend, p, on_tracked=on_tracked)


def _prop_track_button(backend, p: dict, *, on_tracked) -> None:
    btn = ui.button("Track").props("no-caps unelevated dense").style(
        f"background: {t.PRIMARY}; color: {t.BG}; "
        f"font-weight: 800; font-size: 10.5px; letter-spacing: .4px; "
        f"padding: 4px 10px; border-radius: {t.RADIUS_SM}; min-height: 0;"
    )
    raw = p.get("raw") or {}

    async def _click():
        btn.props("loading")
        btn.disable()
        try:
            payload = {
                "player":          p.get("player", ""),
                "market":          p.get("market", ""),
                "line":            p.get("line"),
                "side":            p.get("side", "Over"),
                "odds":            p.get("odds"),
                "confidence":      p.get("conf"),
                "predicted_value": raw.get("predicted_value"),
                "team":            p.get("team", ""),
                "event_id":        raw.get("event_id"),
                "commence_time":   raw.get("commence_time"),
            }
            ok, data, _ = await asyncio.to_thread(
                _post_prop, backend, payload
            )
            if ok:
                ui.notify(
                    f"Tracked: {p.get('player')} {p.get('side')} {p.get('line')}",
                    type="positive",
                )
                btn.text = "Tracked ✓"
                btn.props("disable")
                if on_tracked is not None:
                    try:
                        on_tracked()
                    except Exception:                                      # noqa: BLE001
                        pass
            else:
                err = data.get("error") or "unknown error"
                if "already tracked" in err.lower():
                    ui.notify("Already tracked.", type="info")
                    if on_tracked is not None:
                        try:
                            on_tracked()
                        except Exception:                                  # noqa: BLE001
                            pass
                else:
                    ui.notify(f"Track failed: {err}", type="negative")
        except Exception as exc:                                           # noqa: BLE001
            ui.notify(f"Track failed: {exc}", type="negative")
        finally:
            btn.props(remove="loading")
            if btn.text == "Track":
                btn.enable()

    btn.on("click", _click)


def _post_prop(backend, body: dict) -> tuple[bool, dict, int]:
    """POST a prop pick to /api/props/track via the in-process test client."""
    client = backend.app.test_client()
    try:
        resp = client.post("/api/props/track", json=body or {})
        data = resp.get_json(force=True, silent=True) or {}
        ok   = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                               # noqa: BLE001
        return False, {"error": str(exc)}, 500


# ── Tabs ─────────────────────────────────────────────────────────────────────

def _tabs(backend) -> None:
    # Current personal bankroll, read once and threaded into every row so
    # all Kelly recommendations size off the same number (FIX 3).
    bankroll = _current_personal_bankroll(backend)
    with ui.tabs().props("dense align=left").style(
        f"border-bottom: 1px solid {t.BORDER}; "
        f"color: {t.TEXT_DIM};"
    ) as tabs:
        tab_mlb   = ui.tab("MLB")
        tab_wnba  = ui.tab("WNBA")
        tab_props = ui.tab("PROPS")

    with ui.tab_panels(tabs, value=tab_mlb).classes("w-full").style(
        "background: transparent; padding: 0;"
    ):
        with ui.tab_panel(tab_mlb).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _game_open_bets(backend, sport="mlb", bankroll=bankroll)
                _game_history(backend, sport="mlb", bankroll=bankroll)

        with ui.tab_panel(tab_wnba).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _game_open_bets(backend, sport="wnba", bankroll=bankroll)
                _game_history(backend, sport="wnba", bankroll=bankroll)

        with ui.tab_panel(tab_props).style("padding: 0;"):
            with ui.column().classes("w-full").style(f"gap: {t.SPACE_LG};"):
                _props_record()
                _props_open_bets(bankroll=bankroll)
                _props_history(bankroll=bankroll)


def _current_personal_bankroll(backend) -> float:
    try:
        led = backend.Ledger(path="data/ledger.json", starting_bankroll=1000.0)
        return float(
            led.data.get("personal_bankroll")
            or led.data.get("personal_starting_bankroll")
            or 0.0
        )
    except Exception:                                                      # noqa: BLE001
        return 0.0


# ── Game bets (MLB / WNBA) ───────────────────────────────────────────────────

def _game_open_bets(backend, sport: str, bankroll: float = 0.0) -> None:
    bets = _confirmed_game_bets(backend, sport=sport, settled=False)
    _game_section("OPEN BETS", bets, settled=False, bankroll=bankroll)


def _game_history(backend, sport: str, bankroll: float = 0.0) -> None:
    bets = _confirmed_game_bets(backend, sport=sport, settled=True)
    _game_section("RECENT HISTORY", bets[:50], settled=True, bankroll=bankroll)


def _confirmed_game_bets(backend, sport: str, settled: bool) -> list[dict]:
    try:
        path = "data/wnba_ledger.json" if sport == "wnba" else "data/ledger.json"
        ledger = backend.Ledger(path=path, starting_bankroll=1000.0)
    except Exception:                                                      # noqa: BLE001
        return []
    key = "history" if settled else "open_bets"
    bets = [b for b in (ledger.data.get(key) or []) if b.get("confirmed")]
    rev_key = "settled_at" if settled else "placed_at"
    bets.sort(key=lambda b: b.get(rev_key, ""), reverse=True)
    return bets


def _game_section(title: str, bets: list[dict], settled: bool,
                  bankroll: float = 0.0) -> None:
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(title).style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(str(len(bets))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not bets:
            ui.label("No bets yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        for b in bets:
            _game_bet_row(b, settled, bankroll)


def _bet_type_label(bet_type: str) -> str:
    """Human label for a ledger bet_type."""
    return {
        "single":   "Moneyline",
        "run_line": "Run Line",
        "spread":   "Spread",
        "totals":   "Total",
    }.get((bet_type or "single").lower(), (bet_type or "").replace("_", " ").title())


def _odds_str(odds) -> str:
    if not isinstance(odds, (int, float)):
        return "—"
    return f"+{int(odds)}" if odds > 0 else str(int(odds))


def _bet_line_str(b: dict) -> str:
    """The handicap/line for a tracked bet, signed.  Empty for ML and
    for totals (the line is already baked into the team string, e.g.
    'Over 8.5')."""
    bt = (b.get("bet_type") or "single").lower()
    if bt in ("run_line", "spread"):
        pl = b.get("prop_line")
        if pl is None:
            return ""
        try:
            # Run-line settlement threshold is stored as -run_line_point;
            # flip it back to the bettor-facing point (+1.5 / -1.5).
            point = -float(pl)
            return f"{point:+g}"
        except (TypeError, ValueError):
            return ""
    return ""


def _confidence_pct(b: dict) -> Optional[int]:
    p = b.get("model_prob")
    if not isinstance(p, (int, float)):
        return None
    return int(round(float(p) * 100))


def _placed_date(b: dict) -> str:
    iso = b.get("placed_at") or b.get("commence_time") or ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%b %-d")
    except Exception:                                                      # noqa: BLE001
        return ""


def _game_bet_row(b: dict, settled: bool, bankroll: float = 0.0) -> None:
    result       = (b.get("result") or "").lower()
    result_color = {
        "win": t.POS, "loss": t.NEG, "push": t.WARN, "void": t.TEXT_DIM2,
    }.get(result, t.TEXT_DIM)

    sport    = (b.get("sport") or "mlb").upper()
    team     = b.get("bet_team") or b.get("parlay_name") or "—"
    bet_type = (b.get("bet_type") or "single").lower()
    line_s   = _bet_line_str(b)
    odds_s   = _odds_str(b.get("american_odds"))
    conf     = _confidence_pct(b)
    date_s   = _placed_date(b)
    amount   = float(b.get("confirmed_amount") or 0)
    pnl      = float(b.get("confirmed_pnl")    or 0) if settled else 0.0

    # Primary line: the actual pick (FIX 2).  Totals already bake the
    # side+line into `team` ("Over 8.5"); ML gets an explicit "ML" tag;
    # run line / spread append the signed handicap ("Kansas City +1.5").
    if bet_type == "single":
        pick_str = f"{team} ML"
    elif bet_type in ("run_line", "spread") and line_s:
        pick_str = f"{team} {line_s}"
    else:
        pick_str = team

    # Secondary line: matchup + model confidence (FIX 2).
    matchup = _matchup_str(b)
    sub_parts: list[str] = []
    if matchup:
        sub_parts.append(matchup)
    if conf is not None:
        sub_parts.append(f"{conf}% confidence")
    if odds_s != "—":
        sub_parts.append(odds_s)
    if date_s:
        sub_parts.append(date_s)
    sub_line = "  ·  ".join(sub_parts)

    if settled and result == "win":
        pick_color, amount_text, amount_color = t.POS, f"+${pnl:.2f}", t.POS
    elif settled and result == "loss":
        pick_color, amount_text, amount_color = t.NEG, f"-${amount:.2f}", t.NEG
    elif settled and result == "push":
        pick_color, amount_text, amount_color = t.TEXT, "$0.00", t.TEXT_DIM
    else:
        pick_color, amount_text, amount_color = t.TEXT, f"${amount:.2f}", t.TEXT

    border = (
        f"1px solid {result_color}" if settled and result in ("win", "loss", "push")
        else f"1px solid {t.BORDER}"
    )

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 10px;"
    ):
        ui.label(sport).style(
            f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL}; flex-shrink: 0;"
        )
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(pick_str).style(
                f"font-size: 16px; font-weight: 800; color: {pick_color}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            if sub_line:
                ui.label(sub_line).style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; "
                    f"font-family: monospace; white-space: normal;"
                )
            _kelly_rec_label(b.get("model_prob"), b.get("american_odds"), bankroll)
        with ui.column().style("gap: 2px; text-align: right; align-items: flex-end; flex-shrink: 0;"):
            ui.label(amount_text).style(
                f"font-size: 13px; font-weight: 700; "
                f"color: {amount_color}; font-family: monospace;"
            )
            if settled:
                ui.label(result.upper()).style(
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {result_color};"
                )
            else:
                ui.label("PENDING").style(
                    f"font-size: 10.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )


def _matchup_str(b: dict) -> str:
    away = b.get("away_team") or ""
    home = b.get("home_team") or ""
    if away and home:
        return f"{away} @ {home}"
    return b.get("game") or ""


def _kelly_rec_label(prob, american_odds, bankroll: float) -> None:
    """Small 'Rec ½K $X' line under a tracked bet (FIX 3).  Shows the
    half-Kelly stake off the current bankroll, '$1 min' when a real edge
    rounds to zero, or 'No edge — skip this bet' on a negative edge."""
    from src.kelly import tracked_bet_kelly
    dollars, flag = tracked_bet_kelly(prob, american_odds, bankroll)
    if flag == "invalid":
        return
    if flag == "no_edge":
        ui.label("No edge — skip this bet").style(
            f"font-size: 10.5px; font-weight: 700; color: {t.TEXT_DIM2}; "
            f"font-family: monospace;"
        )
        return
    ui.label(f"Rec ½-Kelly: ${dollars:,.0f}").style(
        f"font-size: 10.5px; font-weight: 800; color: {t.PRIMARY_HI}; "
        f"font-family: monospace;"
    )


# ── Props bets ───────────────────────────────────────────────────────────────

_MARKET_LABEL: dict[str, str] = {
    "pitcher_strikeouts":   "Ks",
    "pitcher_outs":         "Outs",
    "pitcher_hits_allowed": "H Allow",
    "pitcher_walks":        "BB Allow",
    "pitcher_earned_runs":  "ER",
    "batter_hits":          "Hits",
    "batter_total_bases":   "Total Bases",
    "batter_home_runs":     "Home Runs",
    "batter_rbis":          "RBIs",
    "batter_runs_scored":   "Runs",
    "batter_walks":         "Walks",
    "batter_strikeouts":    "Strikeouts",
}


def _load_props_bets() -> tuple[list[dict], list[dict]]:
    """Return (open_picks, history) from the props picks tracker
    (props_picks_history.json -- same pattern as the game trackers)."""
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        return _ppt.get_open(), _ppt.get_history()
    except Exception:                                                      # noqa: BLE001
        return [], []


def _props_record() -> None:
    """Small record summary card for prop picks."""
    try:
        from src import props_picks_tracker as _ppt
        _ppt.reload()
        rec = _ppt.get_record()
    except Exception:                                                      # noqa: BLE001
        rec = {"wins": 0, "losses": 0, "voids": 0, "open": 0, "total": 0, "pct": None}

    w, l, total = rec["wins"], rec["losses"], rec["total"]
    pct = rec["pct"]
    pct_s   = f"{pct * 100:.1f}%" if pct is not None else "—"
    pct_col = t.POS if (pct or 0) >= 0.55 else (t.NEG if (pct or 0.5) < 0.50 else t.TEXT_DIM)

    with ui.row().classes("w-full items-center").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: {t.SPACE_LG};"
    ):
        with ui.column().style("gap: 2px;"):
            ui.label("PROPS MODEL RECORD").style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT_DIM2};"
            )
            ui.label(f"{w}-{l}").style(
                f"font-size: 22px; font-weight: 800; color: {t.TEXT}; font-family: monospace;"
            )
        with ui.column().style("gap: 2px;"):
            ui.label(pct_s).style(
                f"font-size: 16px; font-weight: 800; color: {pct_col}; font-family: monospace;"
            )
            ui.label(f"{total} settled · {rec.get('open', 0)} open").style(
                f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
            )


def _props_open_bets(bankroll: float = 0.0) -> None:
    open_bets, _ = _load_props_bets()
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("OPEN PROPS BETS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT};"
            )
            ui.label(str(len(open_bets))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not open_bets:
            ui.label(
                "No open props bets. Track picks from the Props page."
            ).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                f"text-align: center; font-style: italic;"
            )
            return
        for b in open_bets:
            _prop_bet_row(b, settled=False, bankroll=bankroll)


def _props_history(bankroll: float = 0.0) -> None:
    _, history = _load_props_bets()
    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label("SETTLED PROPS").style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; color: {t.TEXT};"
            )
            ui.label(str(len(history[:50]))).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
        if not history:
            ui.label("No settled props bets yet.").style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; text-align: center;"
            )
            return
        for b in history[:50]:
            _prop_bet_row(b, settled=True, bankroll=bankroll)


def _prop_bet_row(b: dict, settled: bool, bankroll: float = 0.0) -> None:
    """Single row card for a prop pick (open or settled)."""
    result       = (b.get("result") or "").lower()
    # New tracker uses won/lost/void/pending; map both the new and the
    # legacy win/loss spellings to colors so old rows still render.
    result_color = {
        "won": t.POS, "win": t.POS,
        "lost": t.NEG, "loss": t.NEG,
        "void": t.WARN,
    }.get(result, t.TEXT_DIM)

    side    = (b.get("side") or "Over").strip().title()
    is_over = side == "Over"
    side_bg = t.POS if is_over else t.NEG

    player  = b.get("player") or "—"
    market  = _MARKET_LABEL.get(b.get("market", ""), (b.get("market") or "").replace("_", " ").title())
    line    = b.get("line")
    line_s  = f"{float(line):.1f}" if line is not None else "—"
    conf    = b.get("confidence")
    conf_s  = f"{conf * 100:.0f}%" if conf is not None else "—"
    pv      = b.get("predicted_value")
    pv_s    = f"{pv:.1f}" if pv is not None else None
    actual  = b.get("actual_value")
    actual_s = f"{float(actual):.1f}" if actual is not None else None
    odds    = b.get("odds")
    odds_s  = (f"+{odds}" if odds > 0 else str(odds)) if isinstance(odds, int) else "—"
    team    = b.get("team") or ""

    border = (
        f"1px solid {result_color}"
        if settled and result in ("won", "win", "lost", "loss", "void")
        else f"1px solid {t.BORDER}"
    )

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: {border}; "
        f"border-radius: {t.RADIUS_MD}; padding: 10px 12px; gap: 6px;"
    ):
        # Header: market label + team + result/pending badge
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(market.upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if team:
                ui.label(team).style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; font-family: monospace;"
                )
            ui.element("div").style("flex: 1;")
            if settled:
                badge_label = result.upper() if result else "—"
                ui.label(badge_label).style(
                    f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {result_color};"
                )
            else:
                ui.label("PENDING").style(
                    f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )

        # Player name + side chip
        with ui.row().classes("items-center w-full").style("gap: 10px;"):
            ui.label(player).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{side.upper()} {line_s}").style(
                f"background: {side_bg}; color: {t.BG}; "
                f"font-size: 11px; font-weight: 800; "
                f"padding: 3px 9px; border-radius: {t.RADIUS_SM}; flex-shrink: 0;"
            )

        # Stats row: confidence + predicted + actual (if settled) + odds
        with ui.row().classes("items-center w-full").style("gap: 14px; flex-wrap: wrap;"):
            _mini_stat("CONF", conf_s)
            if pv_s:
                _mini_stat("MODEL", pv_s)
            if settled and actual_s is not None:
                _actual_color = (
                    t.POS if result in ("won", "win")
                    else (t.NEG if result in ("lost", "loss") else t.WARN)
                )
                _mini_stat("ACTUAL", actual_s, _actual_color)
            # Half-Kelly recommended stake off the current bankroll (FIX 3).
            from src.kelly import tracked_bet_kelly
            _k_dollars, _k_flag = tracked_bet_kelly(conf, odds, bankroll)
            if _k_flag == "no_edge":
                _mini_stat("REC ½K", "no edge — skip", t.TEXT_DIM2)
            elif _k_flag is None:
                _mini_stat("REC ½K", f"${_k_dollars:,.0f}", t.PRIMARY_HI)
            ui.element("div").style("flex: 1;")
            _mini_stat("ODDS", odds_s)


def _mini_stat(label: str, value: str, value_color: str | None = None) -> None:
    color = value_color or t.TEXT
    with ui.column().style("gap: 1px; align-items: flex-start;"):
        ui.label(label).style(
            f"font-size: 9px; font-weight: 800; letter-spacing: .5px; color: {t.TEXT_DIM2};"
        )
        ui.label(value).style(
            f"font-size: 12px; font-weight: 700; color: {color}; font-family: monospace;"
        )
