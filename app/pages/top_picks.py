"""
top_picks.py
============
The Top Picks tab (/top-picks).  A single unified, ranked list of every
game pick and prop pick for today, scored by

    combined_score = ai_verdict_score * 0.60 + model_confidence * 0.40

Nothing is filtered out -- the All / Game Picks / Props pills just narrow
the view while keeping the combined-score order.  The ranking is built from
existing in-memory + cached data (src.top_picks.build_rankings); it's held
in a per-session closure and re-pulled on a timer so verdicts/reasoning that
are still generating populate without a page refresh.
"""
from __future__ import annotations

import sys

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav


def register(backend) -> None:
    @ui.page("/top-picks")
    def top_picks_page():
        try:
            ui.add_head_html(t.page_head_css())
            navbar.render(active=t.TAB_TOP)
            with ui.column().classes("page-content w-full").style(
                f"max-width: {t.MAX_CONTENT_W}; margin: 0 auto; "
                f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG}; min-width: 0;"
            ):
                _layout(backend)
            bottom_nav.render(active=t.TAB_TOP)
        except Exception as exc:                                          # noqa: BLE001
            print(f"[TOP-PICKS FATAL] {type(exc).__name__}: {exc}",
                  flush=True, file=sys.stderr)
            ui.label("Top Picks failed to render").style(
                f"color: {t.NEG}; font-size: 16px; padding: {t.SPACE_LG};")


_COLOR = {"pos": "POS", "warn": "WARN", "neg": "NEG"}


def _clr(token: str) -> str:
    return {"pos": t.POS, "warn": t.WARN, "neg": t.NEG}.get(token, t.TEXT_DIM)


def _layout(backend) -> None:
    state = {"filter": "all"}
    cache = {"data": None}

    def _rebuild() -> None:
        # Hydrate today's analysis into the in-memory state first -- game
        # picks come from backend._analysis_state, which is empty on a cold
        # process (post-redeploy) until something populates it.  Without this
        # the tab shows "No picks available" even though today's picks exist
        # on disk / in Supabase.  Mirrors what the home/sport pages do, so the
        # tab reflects the current day's picks with no manual analysis run.
        try:
            backend.hydrate_state()
        except Exception as exc:                                          # noqa: BLE001
            print(f"[TOP-PICKS] hydrate_state failed: {exc}",
                  flush=True, file=sys.stderr)
        from src.top_picks import build_rankings
        cache["data"] = build_rankings(backend)

    ui.label("TOP PICKS").classes("page-title").style(
        f"font-size: 22px; font-weight: 800; color: {t.TEXT};")
    ui.label("Today's best plays, ranked by AI verdict (60%) + model "
             "confidence (40%). Everything is shown — you decide where to cut off.").style(
        f"font-size: 12.5px; color: {t.TEXT_DIM};")

    # ── Scorecard (standalone Top Plays tracker) ─────────────────────────────
    @ui.refreshable
    def _scorecard() -> None:                                             # noqa: WPS430
        try:
            from src import top_plays_tracker
            sc = top_plays_tracker.scorecard()
        except Exception:                                                 # noqa: BLE001
            sc = {"win_pct": 0.0, "wins": 0, "losses": 0, "units": 0.0}
        ui.html(_scorecard_html(sc))
    _scorecard()

    # ── Filter pills ─────────────────────────────────────────────────────────
    _PILLS = (("all", "All"), ("game", "Game Picks"), ("props", "Props"))
    with ui.row().classes("items-center w-full").style("gap: 6px; flex-wrap: wrap;"):
        @ui.refreshable
        def _pill_row() -> None:                                          # noqa: WPS430
            for key, label in _PILLS:
                active = state["filter"] == key
                def _mk(k):
                    def _set() -> None:
                        state["filter"] = k
                        _pill_row.refresh()
                        _list.refresh()
                    return _set
                ui.button(label, on_click=_mk(key)).props("no-caps unelevated dense").style(
                    f"background: {t.PRIMARY if active else t.CARD_HI}; "
                    f"color: {t.BG if active else t.TEXT_DIM}; "
                    f"font-size: 11.5px; font-weight: 800; padding: 5px 14px; "
                    f"border-radius: {t.RADIUS_PILL}; min-height: 0;")
        _pill_row()

    # ── Ranked list ──────────────────────────────────────────────────────────
    @ui.refreshable
    def _list() -> None:                                                  # noqa: WPS430
        if cache["data"] is None:
            _rebuild()
        rows = (cache["data"] or {}).get("rows") or []
        if state["filter"] == "game":
            rows = [r for r in rows if r["kind"] == "game"]
        elif state["filter"] == "props":
            rows = [r for r in rows if r["kind"] == "prop"]
        if not rows:
            ui.label("No picks available yet. Run today's analysis first.").style(
                f"font-size: 12.5px; color: {t.TEXT_DIM}; font-style: italic; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; "
                f"text-align: center; width: 100%;")
            return
        for i, r in enumerate(rows, 1):
            _card(r, i)

    _list()

    # Re-pull from the caches periodically so reasoning/verdicts that are
    # still generating (and refreshed values from the 15-min cycle) appear
    # without a page reload.
    def _refresh_data() -> None:
        _rebuild()
        _list.refresh()
        _scorecard.refresh()
    ui.timer(20.0, _refresh_data)


def _scorecard_html(sc: dict) -> str:
    """Two boxes (single ui.html string): Box 1 = win % + W/L record;
    Box 2 = cumulative units won/lost.  Sized off a fixed $1000 reference;
    units are the only running total (1 unit = $10)."""
    wins   = int(sc.get("wins") or 0)
    losses = int(sc.get("losses") or 0)
    pct    = float(sc.get("win_pct") or 0.0)
    units  = float(sc.get("units") or 0.0)
    decided = wins + losses

    pct_str    = f"{pct:.1f}%" if decided else "—"
    pct_color  = t.POS if pct >= 50.0 and decided else (t.NEG if decided else t.TEXT_DIM)
    rec_str    = f"{wins}-{losses}"
    units_str  = f"{units:+.2f}u" if decided else "0.00u"
    units_color = t.POS if units > 0 else (t.NEG if units < 0 else t.TEXT_DIM)

    def _box(label: str, value: str, value_color: str, sub: str) -> str:
        return (
            f'<div style="flex:1 1 0;min-width:0;background:{t.CARD};'
            f'border:1px solid {t.BORDER};border-radius:{t.RADIUS_MD};'
            f'padding:14px 16px;display:flex;flex-direction:column;gap:4px;">'
            f'<div style="font-size:10px;font-weight:800;letter-spacing:.7px;'
            f'color:{t.TEXT_DIM2};">{label}</div>'
            f'<div style="display:flex;align-items:baseline;gap:8px;">'
            f'<span style="font-size:26px;font-weight:800;font-family:monospace;'
            f'letter-spacing:-.5px;color:{value_color};">{value}</span>'
            f'<span style="font-size:13px;font-weight:700;font-family:monospace;'
            f'color:{t.TEXT_DIM};">{sub}</span></div></div>'
        )

    return (
        f'<div style="display:flex;gap:10px;width:100%;flex-wrap:wrap;">'
        + _box("WIN %", pct_str, pct_color, rec_str)
        + _box("UNITS WON / LOST", units_str, units_color, "1u = $10")
        + '</div>'
    )


def _card(r: dict, display_rank: int) -> None:
    vcolor = _clr(r.get("verdict_color"))
    # Outline reflects AI-vs-model AGREEMENT (green = AI backs the model's
    # side, red = AI fades it, neutral border otherwise), keyed off the same
    # ai_tier the Top Plays gate uses.
    try:
        from src.player_ai_breakdown import agreement_outline_token
        ocolor = {"pos": t.POS, "neg": t.NEG}.get(
            agreement_outline_token(r.get("ai_tier")), t.BORDER)
    except Exception:                                                     # noqa: BLE001
        ocolor = t.BORDER
    combined_pct = f"{r.get('combined_score', 0) * 100:.0f}%"
    conf_pct = f"{r.get('confidence', 0) * 100:.0f}%"
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 2px solid {ocolor}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: 10px 14px; gap: 6px; min-width: 0;"
    ):
        # Top row: rank · name · combined score
        with ui.row().classes("items-center w-full").style("gap: 10px;"):
            ui.label(f"#{display_rank}").style(
                f"font-size: 14px; font-weight: 800; color: {t.TEXT_DIM2}; "
                f"font-family: monospace; flex-shrink: 0; min-width: 30px;")
            with ui.column().style("flex: 1; gap: 1px; min-width: 0;"):
                ui.label(r.get("name", "—")).style(
                    f"font-size: 14.5px; font-weight: 800; color: {t.TEXT}; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;")
                ui.label(f"{r.get('pick_type', '')} · {r.get('side', '')}").style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;")
            with ui.column().style("align-items: flex-end; gap: 1px; flex-shrink: 0;"):
                ui.label(combined_pct).style(
                    f"font-size: 18px; font-weight: 800; color: {vcolor}; "
                    f"font-family: monospace;")
                ui.label("score").style(
                    f"font-size: 8.5px; font-weight: 800; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};")

        # Badge row: verdict · confidence · agree/fade tags
        with ui.row().classes("items-center w-full").style("gap: 8px; flex-wrap: wrap;"):
            ui.label(r.get("verdict_label", "Neutral").upper()).style(
                f"background: {vcolor}; color: {t.BG}; font-size: 9.5px; "
                f"font-weight: 800; letter-spacing: .4px; padding: 2px 9px; "
                f"border-radius: {t.RADIUS_PILL};")
            ui.label(f"Model {conf_pct}").style(
                f"font-size: 10.5px; font-weight: 700; color: {t.TEXT_DIM}; "
                f"font-family: monospace;")
            if r.get("agree"):
                ui.label("MODEL + AI AGREE").style(
                    f"background: rgba(16,185,129,.15); color: {t.POS}; "
                    f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL}; "
                    f"border: 1px solid {t.POS};")
            if r.get("fade"):
                ui.label("AI FADE").style(
                    f"background: rgba(239,68,68,.15); color: {t.NEG}; "
                    f"font-size: 9px; font-weight: 800; letter-spacing: .4px; "
                    f"padding: 2px 8px; border-radius: {t.RADIUS_PILL}; "
                    f"border: 1px solid {t.NEG};")

        # Reasoning (or a spinner while it generates)
        if r.get("pending"):
            with ui.row().classes("items-center").style("gap: 6px;"):
                ui.spinner(size="sm").style(f"color: {t.PRIMARY};")
                ui.label("AI analysis generating…").style(
                    f"font-size: 11.5px; color: {t.TEXT_DIM2}; font-style: italic;")
        else:
            ui.label(r.get("reasoning", "")).style(
                f"font-size: 12px; color: {t.TEXT_DIM}; line-height: 1.5; "
                f"white-space: normal;")
