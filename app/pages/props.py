"""
props.py
========
MLB player-props page.

Two sections (pitcher + batter), each rendering the per-market lines
fetched by src.props_client.  Each row shows the player + team, prop
type, line, best available odds, the model's Over/Under call, and the
model's confidence as a colored bar.  High-confidence picks (>65%) are
highlighted with a left-border accent.

Top of page: per-bucket model record (W-L, win%) so the user can see
how the props models have been performing.

Sortable header lets the user flip between "by confidence" (default)
and "by start time" / "by edge".
"""
from __future__ import annotations

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

        _section_model_record(backend)
        _section_props_list(backend, bucket="pitcher", title="PITCHER PROPS")
        _section_props_list(backend, bucket="batter",  title="BATTER PROPS")


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


# ── Props list ──────────────────────────────────────────────────────────────

def _section_props_list(backend, *, bucket: str, title: str) -> None:
    """Render one bucket's prop list, sorted by model confidence."""
    try:
        from src.props_client import get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS
        from src.props_model  import predict
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props imports failed: {exc}")
        return

    payload = get_client().get_today_props() or {}
    all_markets = payload.get("markets") or {}
    bucket_markets = ALL_PITCHER_MARKETS if bucket == "pitcher" else ALL_BATTER_MARKETS

    # Flatten + score + sort.
    rows: list[dict] = []
    for market, props in all_markets.items():
        if market not in bucket_markets:
            continue
        for p in (props or []):
            try:
                pred = predict(p)
            except Exception as exc:                                      # noqa: BLE001
                _dbg(f"predict failed for {market} {p.get('player_name')}: {exc}")
                continue
            rows.append({
                "market":         market,
                "player":         p.get("player_name", "?"),
                "team":           _team_for_prop(p, bucket),
                "line":           p.get("line"),
                "side":           p.get("side"),
                "best_odds":      p.get("best_odds"),
                "best_book":      p.get("best_book"),
                "recommendation": pred.get("recommendation"),
                "confidence":     float(pred.get("confidence") or 0.0),
                "edge":           float(pred.get("edge") or 0.0),
                "source":         pred.get("source"),
                "event_id":       p.get("event_id"),
                "commence_time":  p.get("commence_time"),
            })

    # Default sort: highest confidence first.  Pass calls bubble to the bottom.
    rows.sort(
        key=lambda r: (
            r["recommendation"] == "Pass",
            -r["confidence"],
        )
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(title).style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"{len(rows)} props").style(
                f"background: {t.CARD_HI}; color: {t.TEXT_DIM}; "
                f"font-size: 11px; font-weight: 700; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            if payload.get("fetched_at"):
                ui.label(f"updated {_short_iso(payload['fetched_at'])}").style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                    f"margin-left: auto; font-family: monospace;"
                )

        if not rows:
            ui.label(
                f"No {bucket} props fetched yet.  Tier 1 refreshes every 15 min "
                f"during game hours (11 AM–11 PM ET)."
            ).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
                f"text-align: center; font-style: italic;"
            )
            return

        with ui.column().classes("w-full").style("gap: 6px;"):
            for r in rows:
                _prop_row(r)


def _prop_row(r: dict) -> None:
    """One prop in the list.  High-confidence (>65%) gets a left-border
    accent so it visually pops without breaking the grid."""
    high_conf = r["confidence"] >= 0.65
    rec_col = (
        t.POS if r["recommendation"] == "Over"
        else (t.NEG if r["recommendation"] == "Under" else t.TEXT_DIM)
    )
    border_left = (
        f"4px solid {rec_col};" if high_conf
        else f"4px solid transparent;"
    )

    with ui.row().classes("items-center w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-left: {border_left} "
        f"border-radius: {t.RADIUS_SM}; padding: 10px 12px; gap: 10px;"
    ):
        # Player + team
        with ui.column().style("flex: 2; min-width: 0; gap: 2px;"):
            ui.label(r["player"]).style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(f"{r['team'] or ''}  ·  {_short_market(r['market'])}").style(
                f"font-size: 10.5px; color: {t.TEXT_DIM}; "
                f"letter-spacing: .3px; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )

        # Line
        with ui.column().style("flex: 0 0 60px; gap: 2px; text-align: center;"):
            ui.label("LINE").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(f"{r['line']}" if r.get('line') is not None else "—").style(
                f"font-size: 14px; font-weight: 800; color: {t.TEXT}; "
                f"font-family: monospace;"
            )

        # Recommendation
        with ui.column().style("flex: 0 0 70px; gap: 2px; text-align: center;"):
            ui.label("MODEL").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(r["recommendation"] or "—").style(
                f"font-size: 13px; font-weight: 800; color: {rec_col};"
            )

        # Confidence
        with ui.column().style("flex: 0 0 90px; gap: 2px; text-align: center;"):
            ui.label("CONF").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(f"{r['confidence'] * 100:.0f}%").style(
                f"font-size: 13px; font-weight: 800; "
                f"color: {t.POS if high_conf else t.PRIMARY}; "
                f"font-family: monospace;"
            )

        # Best odds + book
        with ui.column().style(
            "flex: 0 0 110px; gap: 2px; text-align: right; "
            "align-items: flex-end;"
        ):
            ui.label("BEST ODDS").style(
                f"font-size: 9px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(_odds_str(r.get("best_odds"))).style(
                f"font-size: 13px; font-weight: 700; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
            ui.label((r.get("best_book") or "")[:14]).style(
                f"font-size: 9.5px; color: {t.TEXT_DIM2}; "
                f"white-space: nowrap; overflow: hidden;"
            )


# ── Small helpers ───────────────────────────────────────────────────────────

def _team_for_prop(p: dict, bucket: str) -> str:
    """Best-effort team label.  For pitchers we can't tell from the
    payload alone whether they're home or away; fall back to "vs <opp>".
    For batters same problem -- show both team initials."""
    home = (p.get("home_team") or "")[:3].upper()
    away = (p.get("away_team") or "")[:3].upper()
    if home and away:
        return f"{away} @ {home}"
    return home or away or ""


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


def _short_iso(iso: str) -> str:
    """ISO timestamp -> compact "HH:MM ET" for the page header."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    except Exception:                                                     # noqa: BLE001
        return str(iso)[:16]
