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

        _section_model_record(backend)
        _section_props_list(backend, bucket="pitcher", title="PITCHER PROPS")
        _section_props_list(backend, bucket="batter",  title="BATTER PROPS")
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


# ── Props list ──────────────────────────────────────────────────────────────

def _section_props_list(backend, *, bucket: str, title: str) -> None:
    """Render one bucket's prop list as cards.

    Each card is ONE pick (the side the model has higher confidence in
    for that player + market + line).  Both-sides dedup happens here,
    not at the predictor: the predictor still scores Over and Under
    separately so we can compare them and pick the winner.

    Only picks with confidence > 55% land on the page -- everything
    weaker collapses into a single "no high confidence props available"
    notice so the user sees a clean slate rather than coin-flip noise.
    """
    try:
        from src.props_client import get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS
        from src.props_model  import predict
    except Exception as exc:                                              # noqa: BLE001
        _dbg(f"props imports failed: {exc}")
        return

    _CONF_THRESHOLD = 0.55

    payload = get_client().get_today_props() or {}
    all_markets = payload.get("markets") or {}
    bucket_markets = ALL_PITCHER_MARKETS if bucket == "pitcher" else ALL_BATTER_MARKETS

    # Score every prop, then bucket by (player, market, line) so the
    # over + under pair can be compared.  Each bucket keeps ONE entry
    # -- whichever side scored higher confidence.
    by_pick: dict[tuple[str, str, float], dict] = {}
    for market, props in all_markets.items():
        if market not in bucket_markets:
            continue
        for p in (props or []):
            try:
                pred = predict(p)
            except Exception as exc:                                      # noqa: BLE001
                _dbg(f"predict failed for {market} {p.get('player_name')}: {exc}")
                continue
            try:
                line_f = float(p.get("line"))
            except (TypeError, ValueError):
                continue
            key = (p.get("player_name", "?"), market, line_f)
            # When _model_prob > _market_prob the predictor's
            # recommendation is "Over" / "Under"; when it equals or
            # underwhelms it's "Pass".  Either way `confidence` is
            # always >= 0.5 because the helper clamps the floor.  We
            # care about which raw model probability is higher: that
            # tells us which side the model actually believes in.
            side = (p.get("side") or "Over").strip().title()
            score = float(pred.get("confidence") or 0.0)
            existing = by_pick.get(key)
            if existing is None or score > existing["confidence"]:
                by_pick[key] = {
                    "market":          market,
                    "player":          p.get("player_name", "?"),
                    "team":            _team_for_prop(p, bucket),
                    "line":            p.get("line"),
                    "side":            side,
                    "best_odds":       p.get("best_odds"),
                    "best_book":       p.get("best_book"),
                    "recommendation":  pred.get("recommendation"),
                    "confidence":      score,
                    "edge":            float(pred.get("edge") or 0.0),
                    "model_prob":      float(pred.get("model_prob") or 0.0),
                    "source":          pred.get("source"),
                    "predicted_value": pred.get("predicted_value"),
                    "event_id":        p.get("event_id"),
                    "commence_time":   p.get("commence_time"),
                }

    # Filter to >= 55% confidence AND (when a regression model exists)
    # a predicted value that clears the line by >= 0.5 units.
    def _has_reg_edge(r: dict) -> bool:
        pv = r.get("predicted_value")
        if pv is None:
            return True   # no regressor — confidence alone is sufficient
        try:
            line_f = float(r["line"])
            if (r.get("side") or "Over").strip().title() == "Over":
                return pv >= line_f + 0.5
            return pv <= line_f - 0.5
        except (TypeError, ValueError):
            return True

    rows = [
        r for r in by_pick.values()
        if r["confidence"] >= _CONF_THRESHOLD and _has_reg_edge(r)
    ]
    rows.sort(key=lambda r: -r["confidence"])
    _dbg(
        f"[PROPS-PAGE] {bucket}: scored {len(by_pick)} picks (dedup), "
        f"{len(rows)} above {int(_CONF_THRESHOLD * 100)}% threshold"
    )

    with ui.column().classes("w-full").style(f"gap: {t.SPACE_SM};"):
        with ui.row().classes("items-center w-full").style("gap: 8px;"):
            ui.label(title).style(
                f"font-size: 13px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT};"
            )
            ui.label(f"{len(rows)} high-conf picks").style(
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
                f"No high confidence {bucket} props available right now.  "
                f"Tier 1 refreshes every 15 min during game hours "
                f"(11 AM–11 PM ET)."
            ).style(
                f"color: {t.TEXT_DIM}; font-size: 12px; "
                f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
                f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
                f"text-align: center; font-style: italic;"
            )
            return

        # Card grid -- single column on mobile, two columns on desktop
        # via the existing .game-grid class so the props page feels
        # consistent with the slate cards on /sports.
        with ui.element("div").classes("game-grid w-full"):
            for r in rows:
                _prop_card(r, backend)


def _prop_card(r: dict, backend) -> None:
    """One prop pick rendered as a card matching the visual rhythm of
    the slate's game cards (matchup header on top, accent bar on the
    side of the recommended chip, monospace stat values, soft border).

    The Over / Under chip is the visual focal point -- colored to
    match the recommendation so a quick scan of the page shows which
    side the model picked at a glance.
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
        # Mirrors the meta_row pattern on the slate's game cards.
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
        # Slug = lowercase hyphenated name (e.g. "Spencer Strider" → "spencer-strider").
        _name_slug = r["player"].lower().replace(" ", "-")
        ui.link(r["player"], f"/player/mlb/{_name_slug}").style(
            f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
            f"line-height: 1.2; text-decoration: none; "
            f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        ).tooltip("View player profile")

        # Pick row: chip + line + confidence + best odds.  Three flex
        # zones so the chip stays left-anchored and the odds column
        # right-aligns the same way bet boxes do on the slate.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; flex-wrap: nowrap;"
        ):
            # OVER / UNDER pill -- the page's visual anchor.
            ui.label(f"{side.upper()} {r['line']}").style(
                f"background: {chip_bg}; color: {t.BG}; "
                f"font-size: 13px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 6px 12px; border-radius: {t.RADIUS_SM}; "
                f"flex-shrink: 0;"
            )

            # Spacer
            ui.element("div").style("flex: 1;")

            # Confidence number with subdued label above (matches the
            # CONF / PROB blocks inside bet_box.render on the slate).
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

        # Predicted value row (only shown when a regression model produced
        # a numeric estimate).  Green when margin > 1.0, amber otherwise.
        pv = r.get("predicted_value")
        if pv is not None:
            try:
                line_f    = float(r["line"])
                side_str  = (r.get("side") or "Over").strip().title()
                margin    = (pv - line_f) if side_str == "Over" else (line_f - pv)
                pv_color  = t.POS if margin > 1.0 else "#F59E0B"
            except (TypeError, ValueError):
                margin, pv_color = 0.0, t.TEXT_DIM
            stat_abbr = _market_stat_abbr(r.get("market", ""))
            with ui.row().classes("items-center w-full").style(
                f"gap: 8px; padding-top: 4px;"
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

        # Footer row: best odds + book + Track Bet button.
        with ui.row().classes("items-center w-full").style(
            f"gap: 10px; "
            f"padding-top: 6px; border-top: 1px solid {t.BORDER_SOFT};"
        ):
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
