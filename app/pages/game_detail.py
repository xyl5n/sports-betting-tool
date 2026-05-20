"""
Game detail page -- /game/<sport>/<game_id>.

Click any game card in the MLB or WNBA slate (or the Home EV
carousel) to open this page.  Sections:

   1. Header           -- team logos, time, venue, series ctx, live score
   2. Betting Lines    -- ML / RL or Spread / Totals odds table
   3. Model Picks      -- per-bet-type pick + confidence + edge + Kelly
                          + top-3 SHAP factors + per-pick Analyze button
   4. Pitching matchup -- SP ERA/WHIP/K-rate/etc (placeholder slots for
                          last-3-starts + days-rest pending lineup_client
                          integration)
   5. Confirmed Lineups -- placeholder ("Coming soon" -- requires
                          BatterSplitsClient + LineupClient integration)
   6. Team Context     -- L10 / home-away splits / H2H / streak from the
                          upset calculator and game store
   7. Game Context     -- ballpark run factor + weather + line movement
                          (plus umpire placeholder per spec)
   8. Upset Factor     -- chaos score with component breakdown

WNBA differences (handled inline):
   - Section 4 swaps pitcher matchup for "starting five" placeholder
   - Sections that only apply to MLB (run line, pitcher splits) render
     a small "—" instead of crashing

Data lookup
-----------
The route reads the raw analysis dict from
  backend._analysis_state["results"]      (MLB)
  backend._wnba_analysis_state["results"] (WNBA)
matched by game id.  All sections render from the data inside that
dict + the serialized output of `backend._serialize` / `_serialize_wnba`.
If the game isn't in the cache (analyze hasn't run for that sport
today), the page shows a friendly "game not found" state with a link
back to the sport slate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, team_logo, track_button, live_score


_ET = ZoneInfo("America/New_York")


def register(backend) -> None:
    @ui.page("/game/{sport}/{game_id}")
    def game_detail_page(sport: str, game_id: str):
        # Re-read today's analysis cache into the in-memory state so
        # _lookup_game below sees the freshest picks on disk, not
        # whatever was hydrated at boot.
        try:
            backend.hydrate_state()
        except Exception:                                                  # noqa: BLE001
            pass
        ui.add_head_html(t.page_head_css())
        navbar.render(active=t.TAB_SPORTS)
        sport = (sport or "mlb").lower()

        with ui.column().classes("page-content w-full").style(
            f"max-width: {t.MAX_CONTENT_W}; "
            f"margin: 0 auto; "
            f"gap: {t.SPACE_MD}; padding: {t.SPACE_LG};"
        ):
            _back_button(sport)
            raw, serialized = _lookup_game(backend, sport, game_id)
            if not raw:
                _not_found(sport, game_id)
            else:
                _section_header(backend, raw, serialized, sport)
                _section_betting_lines(serialized, sport)
                _section_model_picks(backend, raw, serialized, sport, game_id)
                _section_pitching_or_lineup(serialized, sport)
                _section_lineups_placeholder(sport)
                _section_team_context_placeholder()
                _section_game_context(serialized)
                _section_upset_factor(serialized)
        bottom_nav.render(active=t.TAB_SPORTS)


# ─────────────────────────────────────────────────────────────────────────────
#  Game lookup + not-found
# ─────────────────────────────────────────────────────────────────────────────

def _lookup_game(backend, sport: str, game_id: str) -> tuple[dict | None, dict | None]:
    """Return (raw_analysis_dict, serialized_game_dict) or (None, None).

    raw  -- {game, prediction, shap, meta, rl_pred, totals_pred, ...}
            from _analysis_state["results"][i].  Carries SHAP detail.
    ser  -- _serialize / _serialize_wnba output for the same game.  This
            is the JSON-safe view used by the existing UI.
    """
    state    = backend._wnba_analysis_state if sport == "wnba" else backend._analysis_state
    results  = state.get("results") or []
    raw = next(
        (r for r in results if (r.get("game") or {}).get("id") == game_id),
        None,
    )
    if raw is None:
        return None, None
    try:
        bankroll  = float(state.get("bankroll") or (1000 if sport == "wnba" else 250))
        path      = f"data/{'wnba_ledger' if sport == 'wnba' else 'ledger'}.json"
        ledger    = backend.Ledger(path=path, starting_bankroll=bankroll)
        s_bank    = ledger.data.get("personal_starting_bankroll", bankroll)
        if sport == "wnba":
            ser = backend._serialize_wnba(raw, bankroll, s_bank)
        else:
            ser = backend._serialize(raw, bankroll, "mlb", s_bank)
    except Exception:                                                     # noqa: BLE001
        ser = {}
    return raw, ser


def _not_found(sport: str, game_id: str) -> None:
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_LG}; "
        f"gap: {t.SPACE_SM}; text-align: center;"
    ):
        ui.label(f"Game not found in today's {sport.upper()} analysis.").style(
            f"font-size: 14px; font-weight: 700; color: {t.TEXT};"
        )
        ui.label(
            f"Game id: {game_id}.  Run analysis for this sport to populate "
            f"the cache, or pick a different game from the slate."
        ).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; line-height: 1.5;"
        )


def _back_button(sport: str) -> None:
    href = f"/sports/{sport}"
    ui.link("← Back to slate", href).style(
        f"color: {t.PRIMARY}; text-decoration: none; "
        f"font-size: 13px; font-weight: 700; "
        f"padding: 4px 0; align-self: flex-start;"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 1 -- Header
# ─────────────────────────────────────────────────────────────────────────────

def _section_header(backend, raw: dict, ser: dict, sport: str) -> None:
    game = raw.get("game") or {}
    upset = raw.get("upset") or {}
    away_full = game.get("away_team", "—") or "—"
    home_full = game.get("home_team", "—") or "—"
    when = _fmt_when(game.get("commence_time", ""))
    venue = (game.get("venue") or {}).get("name") if isinstance(game.get("venue"), dict) \
        else game.get("venue_name") or "—"
    sgn = upset.get("series_game_number")
    series_ctx = f"Game {sgn} of series" if sgn else None

    # Live score lookup -- carries inning + score when in-progress.
    live  = live_score.lookup(sport, game_id=(game.get("id") or ""),
                              away_team=away_full, home_team=home_full)
    state = live_score.state_of(live)

    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_LG}; padding: {t.SPACE_LG}; gap: 12px;"
    ):
        # Meta row -- sport chip + time + venue + series + LIVE/FINAL chip
        with ui.row().classes("items-center w-full").style("gap: 10px; flex-wrap: wrap;"):
            ui.label(sport.upper()).style(
                f"background: {t.CARD_HI}; color: {t.TEXT}; "
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"padding: 3px 9px; border-radius: {t.RADIUS_PILL};"
            )
            ui.label(when).style(f"font-size: 13px; color: {t.TEXT_DIM};")
            if venue and venue != "—":
                ui.label(f"@ {venue}").style(
                    f"font-size: 12.5px; color: {t.TEXT_DIM2};"
                )
            if series_ctx:
                ui.label(series_ctx).style(
                    f"font-size: 11.5px; color: {t.TEXT_DIM2}; "
                    f"background: {t.CARD_HI}; padding: 2px 8px; "
                    f"border-radius: {t.RADIUS_PILL};"
                )
            if state == "live":
                with ui.row().classes("items-center").style(
                    "margin-left: auto; gap: 4px;"
                ):
                    live_score.render_live_dot()
                    ui.label("LIVE").style(
                        f"font-size: 10px; font-weight: 800; "
                        f"letter-spacing: .8px; color: {t.POS};"
                    )
            elif state == "final":
                ui.label("FINAL").style(
                    f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.TEXT_DIM}; margin-left: auto;"
                )

        # Big matchup row -- logos + names.  Score block below if live/final.
        with ui.row().classes("items-center w-full").style("gap: 16px;"):
            with ui.column().classes("items-center").style("gap: 6px; flex: 1;"):
                team_logo.render(away_full, sport=sport, size=64)
                ui.label(away_full).style(
                    f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
                    f"text-align: center;"
                )
            ui.label("@").style(f"color: {t.TEXT_DIM2}; font-size: 18px;")
            with ui.column().classes("items-center").style("gap: 6px; flex: 1;"):
                team_logo.render(home_full, sport=sport, size=64)
                ui.label(home_full).style(
                    f"font-size: 16px; font-weight: 700; color: {t.TEXT}; "
                    f"text-align: center;"
                )

        if state in ("live", "final") and live is not None:
            live_score.render_score_block(live, sport)


# ─────────────────────────────────────────────────────────────────────────────
#  Section 2 -- Betting Lines
# ─────────────────────────────────────────────────────────────────────────────

def _section_betting_lines(ser: dict, sport: str) -> None:
    is_mlb = sport == "mlb"
    rl_pick = ser.get("run_line") or ser.get("spread_pick") or {}
    tot     = ser.get("totals") or {}
    away_team = ser.get("away_team", "Away")
    home_team = ser.get("home_team", "Home")

    rows: list[tuple[str, str, str]] = [
        ("Moneyline (Away)", away_team, _odds_str(ser.get("away_odds"))),
        ("Moneyline (Home)", home_team, _odds_str(ser.get("home_odds"))),
    ]
    if rl_pick:
        line = rl_pick.get("run_line_point") if is_mlb else rl_pick.get("spread_line")
        line_str = f"{float(line):+g}" if isinstance(line, (int, float)) else "—"
        label_root = "Run Line" if is_mlb else "Spread"
        rows.append((
            f"{label_root} (Home)",
            f"{home_team} {line_str}",
            _odds_str(rl_pick.get("run_line_home_odds") or rl_pick.get("pick_odds")),
        ))
        rows.append((
            f"{label_root} (Away)",
            f"{away_team} {(-float(line)):+g}" if isinstance(line, (int, float)) else away_team,
            _odds_str(rl_pick.get("run_line_away_odds") or rl_pick.get("pick_odds")),
        ))
    if tot and tot.get("total_line") is not None:
        ln = tot.get("total_line")
        rows.append(("Total (Over)",  f"O {ln}", _odds_str(tot.get("over_odds"))))
        rows.append(("Total (Under)", f"U {ln}", _odds_str(tot.get("under_odds"))))

    _section_card("BETTING LINES", rows_renderer=lambda: _odds_table(rows))


def _odds_table(rows: list[tuple[str, str, str]]) -> None:
    with ui.column().classes("w-full").style("gap: 0;"):
        for label, value, odds in rows:
            with ui.row().classes("items-center w-full").style(
                f"padding: 8px 0; gap: 12px; "
                f"border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(label).style(
                    f"flex: 0 0 35%; font-size: 12px; color: {t.TEXT_DIM};"
                )
                ui.label(value).style(
                    f"flex: 1; font-size: 13px; color: {t.TEXT}; font-weight: 600; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )
                ui.label(odds).style(
                    f"font-size: 13px; font-weight: 700; color: {t.PRIMARY}; "
                    f"font-family: monospace; flex-shrink: 0;"
                )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 3 -- Model Picks with per-pick Analyze button
# ─────────────────────────────────────────────────────────────────────────────

# Per-page cache for Analyze responses.  Keyed by (game_id, bet_type) so the
# spec's "clicking Analyze again shows the cached version without another
# API call" works across multiple bet types on the same page.
_analysis_cache: dict = {}


def _section_model_picks(backend, raw: dict, ser: dict, sport: str, game_id: str) -> None:
    is_mlb = sport == "mlb"
    picks: list[dict] = []

    # Moneyline -- always present in serialized output.
    if ser.get("pick_team"):
        picks.append({
            "bet_type":  "moneyline",
            "label":     "Moneyline",
            "pick":      ser.get("pick_team", "—"),
            "prob":      ser.get("pick_prob"),
            "edge":      ser.get("pick_edge"),
            "odds":      ser.get("pick_odds"),
            "kelly":     ser.get("bet_dollars"),
            "agree":     ser.get("models_agree", True),
            "shap":      (raw.get("prediction") or {}).get("shap") or [],
        })

    # Run Line (MLB) / Spread (WNBA)
    rl = ser.get("run_line") or ser.get("spread_pick")
    if rl and rl.get("pick_team"):
        bt = "run_line" if is_mlb else "spread"
        line = rl.get("run_line_point") if is_mlb else rl.get("spread_line")
        line_str = f" {float(line):+g}" if isinstance(line, (int, float)) else ""
        picks.append({
            "bet_type":  bt,
            "label":     "Run Line" if is_mlb else "Spread",
            "pick":      f"{rl.get('pick_team', '')}{line_str}".strip(),
            "prob":      rl.get("pick_prob"),
            "edge":      rl.get("edge"),
            "odds":      rl.get("pick_odds"),
            "kelly":     rl.get("bet_dollars"),
            "agree":     rl.get("models_agree", True),
            "shap":      (raw.get("rl_pred") or raw.get("spread_pred") or {}).get("shap") or [],
        })

    # Totals
    tot = ser.get("totals") or {}
    if tot and tot.get("total_line") is not None:
        direction = (tot.get("direction") or "over").title()
        picks.append({
            "bet_type":  "totals",
            "label":     "Totals",
            "pick":      f"{direction} {tot.get('total_line')}",
            "prob":      tot.get("pick_prob"),
            "edge":      tot.get("edge"),
            "odds":      tot.get("over_odds") if direction == "Over"
                          else tot.get("under_odds"),
            "kelly":     tot.get("bet_dollars"),
            "agree":     tot.get("models_agree", True),
            "shap":      (raw.get("totals_pred") or {}).get("shap") or [],
        })

    _section_card(
        "MODEL PICKS",
        rows_renderer=lambda: _picks_renderer(backend, picks, sport, game_id),
    )


def _picks_renderer(backend, picks: list[dict], sport: str, game_id: str) -> None:
    if not picks:
        ui.label("No model picks for this game.").style(
            f"color: {t.TEXT_DIM}; font-size: 12px;"
        )
        return
    for p in picks:
        _pick_block(backend, p, sport, game_id)


def _pick_block(backend, p: dict, sport: str, game_id: str) -> None:
    prob_pct = (float(p.get("prob") or 0) * 100)
    edge_pct = (float(p.get("edge") or 0) * 100)
    odds     = p.get("odds")
    kelly    = p.get("kelly")
    edge_col = t.POS if edge_pct >= 0 else t.NEG
    edge_s   = f"{'+' if edge_pct >= 0 else ''}{edge_pct:.1f}% Edge"
    odds_s   = _odds_str(odds)
    kelly_s  = (f"½K  ${float(kelly):.0f}"
                if isinstance(kelly, (int, float)) and kelly > 0
                else "½K  —")

    with ui.column().classes("w-full").style(
        f"background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 12px 14px; gap: 8px;"
    ):
        with ui.row().classes("items-center w-full").style("gap: 10px;"):
            ui.label(p["label"].upper()).style(
                f"font-size: 10px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM2};"
            )
            ui.label(p["pick"]).style(
                f"flex: 1; font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(odds_s).style(
                f"font-size: 12.5px; font-weight: 700; color: {t.TEXT}; "
                f"font-family: monospace;"
            )

        with ui.row().classes("items-center w-full").style("gap: 14px; flex-wrap: wrap;"):
            ui.label(f"{prob_pct:.0f}% confidence").style(
                f"font-size: 11.5px; color: {t.PRIMARY}; font-family: monospace;"
            )
            ui.label(edge_s).style(
                f"font-size: 11.5px; color: {edge_col}; font-family: monospace; "
                f"font-weight: 700;"
            )
            ui.label(kelly_s).style(
                f"font-size: 11.5px; color: {t.TEXT_DIM}; font-family: monospace;"
            )
            ui.label(
                "models agree" if p.get("agree") else "MODELS SPLIT"
            ).style(
                f"font-size: 11px; color: "
                f"{t.TEXT_DIM2 if p.get('agree') else t.WARN}; "
                f"font-weight: 700;"
            )

        # SHAP factors -- top 3, plain English via _FEATURE_LABELS labels.
        shap = (p.get("shap") or [])[:3]
        if shap:
            ui.label("TOP FACTORS").style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .6px; "
                f"color: {t.TEXT_DIM2}; margin-top: 4px;"
            )
            for s in shap:
                label = s.get("label") or s.get("feature", "factor")
                val   = float(s.get("shap_value") or 0)
                arrow = "↑" if val > 0 else ("↓" if val < 0 else "·")
                color = t.POS if val > 0 else (t.NEG if val < 0 else t.TEXT_DIM)
                with ui.row().classes("items-center").style("gap: 8px;"):
                    ui.label(arrow).style(
                        f"color: {color}; font-weight: 800; "
                        f"font-family: monospace; min-width: 12px;"
                    )
                    ui.label(label).style(
                        f"font-size: 12px; color: {t.TEXT};"
                    )

        # Analyze button + response container.  The container starts empty;
        # clicking Analyze fills it with the cached or freshly-fetched
        # response.
        analysis_holder = ui.column().classes("w-full").style("gap: 4px;")

        async def _on_analyze():
            cache_key = (game_id, p["bet_type"])
            if cache_key in _analysis_cache:
                _render_analysis(analysis_holder, _analysis_cache[cache_key],
                                 from_cache=True)
                return
            _render_analysis(analysis_holder, "…loading…", placeholder=True)
            try:
                ok, data, _ = await asyncio.to_thread(
                    _post, backend, "/api/ai/pick_analysis",
                    {"game_id": game_id, "bet_type": p["bet_type"], "sport": sport},
                )
                if ok:
                    text = data.get("analysis") or "(no response)"
                    _analysis_cache[cache_key] = text
                    _render_analysis(analysis_holder, text)
                else:
                    err = data.get("error") or "AI analysis failed."
                    _render_analysis(analysis_holder, err, error=True)
            except Exception as exc:                                      # noqa: BLE001
                _render_analysis(analysis_holder, f"Error: {exc}", error=True)

        with ui.row().classes("w-full justify-end").style("margin-top: 4px;"):
            track_button.render(
                backend, game_id=game_id, sport=sport, size="sm", label="Track",
            )
            ui.button("Analyze", on_click=_on_analyze).props("no-caps unelevated dense") \
                .style(
                    f"background: {t.CARD}; color: {t.PRIMARY}; "
                    f"border: 1px solid {t.PRIMARY}; "
                    f"font-weight: 700; padding: 6px 14px; "
                    f"font-size: 11.5px; border-radius: {t.RADIUS_SM}; "
                    f"min-height: 0;"
                )


def _render_analysis(holder, text: str, *,
                     placeholder: bool = False, error: bool = False,
                     from_cache: bool = False) -> None:
    holder.clear()
    color = (
        t.TEXT_DIM2 if placeholder else
        t.NEG       if error       else t.TEXT
    )
    border = t.NEG if error else t.BORDER
    badge  = " (cached)" if from_cache else ""
    with holder:
        with ui.row().classes("w-full").style(
            f"background: {t.CARD}; border: 1px solid {border}; "
            f"border-radius: {t.RADIUS_SM}; padding: 10px 12px;"
        ):
            ui.label(f"{text}{badge}").style(
                f"font-size: 12.5px; color: {color}; line-height: 1.55; "
                f"white-space: pre-wrap; word-break: break-word;"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 4 -- Pitching matchup (MLB)  /  Starting Five (WNBA)
# ─────────────────────────────────────────────────────────────────────────────

def _section_pitching_or_lineup(ser: dict, sport: str) -> None:
    if sport == "wnba":
        _section_card(
            "STARTING FIVE",
            rows_renderer=_starting_five_placeholder,
        )
        return
    home_sp = ser.get("home_sp") or {}
    away_sp = ser.get("away_sp") or {}
    _section_card(
        "PITCHING MATCHUP",
        rows_renderer=lambda: _pitching_table(away_sp, home_sp,
                                              ser.get("away_team", "Away"),
                                              ser.get("home_team", "Home")),
    )


def _pitching_table(away_sp: dict, home_sp: dict, away_team: str, home_team: str) -> None:
    fields = [
        ("ERA",         "era",       "{:.2f}"),
        ("WHIP",        "whip",      "{:.2f}"),
        ("K rate",      "k_rate",    "{:.1%}"),
        ("BB / 9",      "bb9",       "{:.2f}"),
        ("Home ERA",    "era_home",  "{:.2f}"),
        ("Away ERA",    "era_away",  "{:.2f}"),
        ("Last 3 ERA",  "last3_era", "{:.2f}"),
        ("Days rest",   "rest",      "{}"),
        ("Hand",        "hand",      "{}"),
    ]
    # Header row -- pitcher names
    with ui.row().classes("items-center w-full").style(
        f"padding: 6px 0; gap: 12px; "
        f"border-bottom: 1px solid {t.BORDER};"
    ):
        ui.label("").style(f"flex: 0 0 35%;")
        ui.label(f"{away_team} (Away SP)").style(
            f"flex: 1; font-size: 11.5px; font-weight: 700; color: {t.TEXT};"
        )
        ui.label(f"{home_team} (Home SP)").style(
            f"flex: 1; font-size: 11.5px; font-weight: 700; color: {t.TEXT};"
        )
    for label, key, fmt in fields:
        av = away_sp.get(key)
        hv = home_sp.get(key)
        with ui.row().classes("items-center w-full").style(
            f"padding: 6px 0; gap: 12px; "
            f"border-bottom: 1px solid {t.BORDER_SOFT};"
        ):
            ui.label(label).style(
                f"flex: 0 0 35%; font-size: 12px; color: {t.TEXT_DIM};"
            )
            ui.label(_fmt(av, fmt)).style(
                f"flex: 1; font-size: 12.5px; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
            ui.label(_fmt(hv, fmt)).style(
                f"flex: 1; font-size: 12.5px; color: {t.TEXT}; "
                f"font-family: monospace;"
            )
    ui.label(
        "Last 3 start-by-start breakdown coming soon."
    ).style(
        f"font-size: 11px; color: {t.TEXT_DIM2}; "
        f"margin-top: 8px; font-style: italic;"
    )


def _starting_five_placeholder() -> None:
    ui.label(
        "Starting five + key player stats coming soon — requires WNBA Stats "
        "API integration."
    ).style(
        f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic;"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 5 -- Lineups (placeholder)
# ─────────────────────────────────────────────────────────────────────────────

def _section_lineups_placeholder(sport: str) -> None:
    if sport == "wnba":
        return  # Section 4 already covers WNBA player stats
    _section_card(
        "CONFIRMED LINEUPS",
        rows_renderer=lambda: ui.label(
            "Full batting orders with AVG / OBP / SLG and vs-LHP/RHP splits "
            "coming soon — wiring the existing BatterSplitsClient + "
            "LineupClient into the page response."
        ).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic; "
            f"line-height: 1.5;"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 6 -- Team Context (placeholder + the data that's already serialized)
# ─────────────────────────────────────────────────────────────────────────────

def _section_team_context_placeholder() -> None:
    _section_card(
        "TEAM CONTEXT",
        rows_renderer=lambda: ui.label(
            "Last-10 record, home/away splits, head-to-head, and current "
            "streak coming soon — pulling from the existing GameStore + "
            "UpsetCalculator helpers."
        ).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic; "
            f"line-height: 1.5;"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 7 -- Game Context (ballpark + weather + line movement + umpire)
# ─────────────────────────────────────────────────────────────────────────────

def _section_game_context(ser: dict) -> None:
    _section_card(
        "GAME CONTEXT",
        rows_renderer=lambda: _game_context_rows(ser),
    )


def _game_context_rows(ser: dict) -> None:
    park = ser.get("park_run_factor")
    wx   = ser.get("weather") or {}
    line_move = (ser.get("meta") or {}).get("line_movement")

    rows: list[tuple[str, str]] = []
    rows.append((
        "Ballpark run factor",
        f"{float(park):.2f}" if isinstance(park, (int, float)) else "—",
    ))
    if wx:
        temp = wx.get("temperature")
        wind = wx.get("wind_speed")
        wdir = wx.get("wind_direction")
        bits = []
        if isinstance(temp, (int, float)): bits.append(f"{temp:.0f}°F")
        if isinstance(wind, (int, float)): bits.append(f"wind {wind:.0f} mph")
        if wdir: bits.append(f"({wdir})")
        rows.append(("Weather", " ".join(bits) if bits else "—"))
    else:
        rows.append(("Weather", "—"))
    if isinstance(line_move, (int, float)) and line_move:
        sign = "+" if line_move > 0 else ""
        rows.append(("Line movement (vs opening)", f"{sign}{line_move:.2f}"))
    else:
        rows.append(("Line movement (vs opening)", "—"))
    rows.append(("Umpire data", "Coming soon"))

    for label, value in rows:
        with ui.row().classes("items-center w-full").style(
            f"padding: 6px 0; gap: 12px; "
            f"border-bottom: 1px solid {t.BORDER_SOFT};"
        ):
            ui.label(label).style(
                f"flex: 0 0 40%; font-size: 12px; color: {t.TEXT_DIM};"
            )
            color = t.TEXT_DIM2 if value in ("—", "Coming soon") else t.TEXT
            ui.label(value).style(
                f"flex: 1; font-size: 12.5px; color: {color}; "
                f"font-family: monospace;"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Section 8 -- Upset Factor (chaos score + components)
# ─────────────────────────────────────────────────────────────────────────────

def _section_upset_factor(ser: dict) -> None:
    upset = ser.get("upset_factor") or {}
    _section_card(
        "UPSET FACTOR",
        rows_renderer=lambda: _upset_rows(upset),
    )


def _upset_rows(upset: dict) -> None:
    score = upset.get("score")
    if score is None:
        ui.label("No upset-factor data for this game.").style(
            f"font-size: 12px; color: {t.TEXT_DIM};"
        )
        return
    # Big score header
    with ui.row().classes("items-center w-full").style(
        f"gap: 14px; padding: 4px 0;"
    ):
        ui.label("Chaos score").style(
            f"font-size: 11.5px; color: {t.TEXT_DIM};"
        )
        ui.label(f"{int(score)} / 10").style(
            f"font-size: 22px; font-weight: 800; color: {t.WARN}; "
            f"font-family: monospace;"
        )
        ui.label("Higher = more unpredictable.").style(
            f"font-size: 11px; color: {t.TEXT_DIM2}; "
            f"margin-left: auto;"
        )

    components = upset.get("components") or {}
    if not components:
        return
    pretty = {
        "run_scoring_var":      "Run scoring volatility",
        "pitching_var":         "Pitching volatility",
        "streak":               "Recent streak swing",
        "underdog_win_rate":    "Underdog upset rate this season",
        "blown_lead_rate":      "Blown leads recently",
        "h2h_divergence":       "Head-to-head divergence",
        "bullpen_volatility":   "Bullpen volatility",
        "pitcher_consistency":  "Starting pitcher inconsistency",
        "series_game":          "Series-game effect",
    }
    for key, label in pretty.items():
        if key not in components:
            continue
        val = components[key]
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = 0
        # 0-1 -> 0-100% colored bar
        pct = max(0, min(100, int(round(v * 100))))
        col = t.NEG if pct > 65 else (t.WARN if pct > 35 else t.POS)
        with ui.row().classes("items-center w-full").style(
            f"padding: 5px 0; gap: 10px;"
        ):
            ui.label(label).style(
                f"flex: 0 0 50%; font-size: 11.5px; color: {t.TEXT};"
            )
            ui.html(
                f'<div style="flex:1;height:6px;background:{t.CARD_HI};'
                f'border-radius:3px;overflow:hidden;">'
                f'<div style="width:{pct}%;height:100%;background:{col};"></div>'
                f'</div>'
            )
            ui.label(f"{pct}%").style(
                f"font-size: 11px; color: {col}; "
                f"font-family: monospace; min-width: 40px; text-align: right;"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _section_card(title: str, *, rows_renderer) -> None:
    with ui.column().classes("w-full").style(
        f"background: {t.CARD}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: {t.SPACE_MD}; gap: 8px;"
    ):
        ui.label(title).style(
            f"font-size: 12px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT};"
        )
        rows_renderer()


def _fmt_when(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %b %-d  %-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return iso[:16]


def _odds_str(o) -> str:
    if o is None:
        return "—"
    try:
        n = int(o)
    except Exception:                                                     # noqa: BLE001
        return str(o)
    return f"+{n}" if n > 0 else f"{n}"


def _fmt(v, template: str) -> str:
    if v is None or v == "":
        return "—"
    try:
        if "{:" in template and template.endswith("}"):
            return template.format(v)
        return template.format(v)
    except (TypeError, ValueError):
        return str(v)


def _post(backend, path: str, body: dict | None,
          *, method: str = "POST") -> tuple[bool, dict, int]:
    """In-process call to a Flask /api/ route -- same pattern as
    pages/admin.py and pages/ai_breakdown.py."""
    client = backend.app.test_client()
    fn     = client.post if method.upper() == "POST" else client.get
    try:
        resp = fn(path, json=body or {}) if method.upper() == "POST" else fn(path)
        try:
            data = resp.get_json(force=True, silent=True) or {}
        except Exception:                                                 # noqa: BLE001
            data = {}
        ok = resp.status_code < 400 and data.get("success", True) is not False
        return ok, data, resp.status_code
    except Exception as exc:                                              # noqa: BLE001
        return False, {"error": str(exc)}, 500
