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
from typing import Optional
from zoneinfo import ZoneInfo

from nicegui import ui

from components import theme as t
from components import navbar, bottom_nav, team_logo, track_button, live_score


_ET = ZoneInfo("America/New_York")


def _log(msg: str) -> None:
    """Tagged stderr diagnostic line.  Grep `[game_detail]` in Railway
    logs to walk through a single matchup page render -- shows which
    branch _lookup_game took and exactly what pitcher data made it
    into _section_pitching_or_lineup."""
    import sys as _sys
    print(f"[game_detail] {msg}", flush=True, file=_sys.stderr)


def register(backend) -> None:
    def _render_detail(sport: str, game_id: str):
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
            if serialized:
                # Renders correctly whether `raw` is the full analysis
                # dict, the cache passthrough, or just a schedule stub.
                # When `_no_odds` is set the betting-lines section
                # swaps in Model Prediction probabilities instead of
                # the actual odds table.
                _section_header(backend, raw or serialized, serialized, sport)
                _section_betting_lines(serialized, sport)
                if raw:
                    _section_model_picks(backend, raw, serialized, sport, game_id)
                _section_pitching_or_lineup(serialized, sport)
                # Venue card sits right under the pitcher matchup so
                # the user reads ballpark + run factor as part of the
                # pitching context (MLB only -- WNBA doesn't have a
                # park-factor equivalent).
                if sport == "mlb":
                    _section_venue(serialized)
                _section_lineups_placeholder(sport)
                _section_team_context_placeholder()
                _section_game_context(serialized)
                _section_upset_factor(serialized)
            else:
                _not_found(sport, game_id)
        bottom_nav.render(active=t.TAB_SPORTS)

    @ui.page("/game/{sport}/{game_id}")
    def game_detail_page(sport: str, game_id: str):
        _render_detail(sport, game_id)

    # Matchup route alias -- per user spec the spelling is /matchup/...
    # for the new in-card "Matchup" pill.  Both routes point at the
    # same handler so existing links keep working.
    @ui.page("/matchup/{sport}/{game_id}")
    def matchup_page(sport: str, game_id: str):
        _render_detail(sport, game_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Game lookup + not-found
# ─────────────────────────────────────────────────────────────────────────────

def _norm_team_key(name) -> str:
    """Aggressive team-name normalization for cross-API matching.
    Lowercases and strips every non-alphanumeric so "LA Dodgers" and
    "Los Angeles Dodgers" land on the same key.  Mirrors the helper
    in pages/sport.py + the schedule endpoint's _team_key in app.py."""
    if not name:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _commence_et_date(commence_time) -> str:
    """ISO commence_time -> ET calendar date string ("YYYY-MM-DD").
    Returns "" on parse failure so unparseable rows don't collide on a
    single empty-date bucket."""
    if not commence_time:
        return ""
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        return dt.astimezone(_ET).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return ""


def _lookup_game(backend, sport: str, game_id: str) -> tuple[dict | None, dict | None]:
    """Return (raw_analysis_dict, serialized_game_dict) or (None, None).

    raw  -- nested {game, prediction, shap, meta, rl_pred, totals_pred,
            ...} from _analysis_state["results"][i] when the analyze
            pipeline just finished.  May be None when state was hydrated
            from data/{,wnba_}analysis_cache.json or daily_snapshot.json
            instead -- those store already-serialized flat dicts, and
            the raw nested view (with SHAP detail) isn't recoverable
            from them.
    ser  -- the flat serialized dict the UI renders.  Either freshly
            built via _serialize / _serialize_wnba (raw path) or the
            cache entry itself (passthrough path).
    """
    state    = backend._wnba_analysis_state if sport == "wnba" else backend._analysis_state
    results  = state.get("results") or []
    _log(
        f"_lookup_game sport={sport} game_id={game_id!r} "
        f"analysis_results={len(results)}"
    )

    # Match both shapes: raw rows expose the id at r["game"]["id"];
    # serialized rows expose it as r["game_id"] (or fall back to
    # r["id"]).  Without this dual-key lookup the page reports "not
    # found" for every game after a snapshot-hydrated boot.
    entry = next(
        (
            r for r in results
            if (r.get("game") or {}).get("id") == game_id
            or r.get("game_id") == game_id
            or r.get("id") == game_id
            or r.get("_schedule_id") == game_id
        ),
        None,
    )
    if entry is not None:
        _log(f"  matched by id -- entry has home_sp={bool(entry.get('home_sp'))} "
             f"away_sp={bool(entry.get('away_sp'))}")
    else:
        # Team-name + ET-date fallback.  The matchup link is built from
        # the slate's MLB statsapi gamePk (e.g. "824274") but the
        # analysis pipeline keys results by Odds API id (e.g.
        # "427339d860a9..."), so a pure id lookup always missed and
        # the page fell into the schedule-stub branch below which has
        # no pitcher data.  Walk the schedule for today (+/- 1 day) to
        # resolve the gamePk -> (home, away, et_date), then find the
        # analysis row that matches that composite key.
        match = _resolve_via_team_date(backend, sport, game_id, results)
        if match is not None:
            entry = match
            _log(f"  matched by team+date fallback -- entry has "
                 f"home_sp={bool(entry.get('home_sp'))} "
                 f"away_sp={bool(entry.get('away_sp'))}")

    if entry is None:
        # Schedule fallback -- schedule-only games (no odds yet, or
        # past/future dates outside the analysis window) won't be in
        # _analysis_state but ARE returned by /api/schedule.  Without
        # this fallback the detail page reports "not found" for every
        # card with _no_odds=True even though the card itself rendered
        # fine on the slate.
        _log(f"  no analysis match -- falling back to schedule stub")
        sched_entry = _lookup_in_schedule(backend, sport, game_id)
        if sched_entry is None:
            _log(f"  schedule fallback also empty -- returning (None, None)")
            return None, None
        # Schedule rows are already in serialized shape; raw=None so
        # the caller knows it can't render the SHAP-dependent picks
        # section.  The Model Prediction block in _section_betting_lines
        # picks up the slack via the _no_odds + _model_prediction keys.
        return None, sched_entry

    # Pre-serialized passthrough: skip the serialize call entirely (it
    # would KeyError on r["game"]) and use the cache entry as both
    # the "raw" and "ser" return values.  The raw view loses its SHAP
    # detail in this path -- callers that need SHAP for an explainer
    # section will see empty/missing fields, which the renderers
    # already handle gracefully.
    if "home_team" in entry and "away_team" in entry:
        return entry, dict(entry)

    raw = entry
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


def _resolve_via_team_date(
    backend, sport: str, game_id: str, results: list[dict],
) -> dict | None:
    """When the matchup URL carries a schedule gamePk that doesn't match
    any analysis row's id (different API id systems), walk the schedule
    to resolve gamePk -> (home_team, away_team, et_date), then scan
    analysis results for a row with the same composite key.

    Returns the matching analysis row or None.  Logs each step so
    Railway makes the resolution path visible -- "schedule resolved
    to LAD vs NYM 2026-05-21" vs "analysis row 0 LAD vs NYM matches".
    """
    sched_entry = _lookup_in_schedule(backend, sport, game_id)
    if not sched_entry:
        _log(f"  team+date: schedule lookup empty for game_id={game_id!r}")
        return None
    target_home = _norm_team_key(sched_entry.get("home_team"))
    target_away = _norm_team_key(sched_entry.get("away_team"))
    target_date = _commence_et_date(sched_entry.get("commence_time"))
    _log(
        f"  team+date target: home={target_home!r} away={target_away!r} "
        f"et_date={target_date!r}"
    )
    if not (target_home and target_away):
        return None

    for r in results:
        game = r.get("game") or {}
        r_home = game.get("home_team") or r.get("home_team") or ""
        r_away = game.get("away_team") or r.get("away_team") or ""
        r_ct   = game.get("commence_time") or r.get("commence_time") or ""
        r_date = _commence_et_date(r_ct)
        if (
            _norm_team_key(r_home) == target_home
            and _norm_team_key(r_away) == target_away
            and (not target_date or not r_date or r_date == target_date)
        ):
            _log(f"  team+date: matched analysis row {r_home} vs {r_away} {r_date}")
            return r
    _log(f"  team+date: no analysis row matched the composite key")
    return None


def _lookup_in_schedule(backend, sport: str, game_id: str) -> dict | None:
    """Fall back to /api/schedule/<sport> for a game id not present in
    the in-memory analysis cache.  Returns the serialized schedule row
    (the same shape game_card.render reads) or None.

    Walks today's schedule first, then yesterday's and tomorrow's --
    the matchup link in the slate may have been opened across a midnight
    boundary, and the schedule cache is keyed by date.
    """
    from datetime import datetime, timedelta
    try:
        client = backend.app.test_client()
        today = datetime.now(_ET).date()
        for offset in (0, -1, 1):
            d = (today + timedelta(days=offset)).isoformat()
            try:
                resp = client.get(f"/api/schedule/{sport}?date={d}")
                data = resp.get_json(force=True, silent=True) or {}
            except Exception:                                              # noqa: BLE001
                continue
            for g in (data.get("games") or []):
                gid = g.get("game_id") or g.get("id")
                if str(gid) == str(game_id):
                    return g
    except Exception:                                                     # noqa: BLE001
        return None
    return None


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
    # Tolerate two shapes:
    #   - raw analysis dict: nested {game: {away_team, home_team, ...}}
    #   - serialized / schedule passthrough: flat top-level keys
    # When the nested "game" key is missing fall back to the flat
    # values on `ser` so schedule-only matchup pages still show team
    # names + start time + venue instead of em-dashes.
    game = raw.get("game") or {}
    upset = raw.get("upset") or {}
    away_full = game.get("away_team") or ser.get("away_team") or "—"
    home_full = game.get("home_team") or ser.get("home_team") or "—"
    when = _fmt_when(
        game.get("commence_time") or ser.get("commence_time", "")
    )
    if isinstance(game.get("venue"), dict):
        venue = (game.get("venue") or {}).get("name") or "—"
    else:
        venue = (
            game.get("venue_name")
            or ser.get("venue_name")
            or ser.get("venue")
            or "—"
        )
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
    # Schedule-only games (no Odds API entry) get the Model Prediction
    # block instead of the empty odds table.  Per user spec: "if no odds
    # available show Model Prediction instead with the raw probability
    # percentages."
    if ser.get("_no_odds"):
        _section_card(
            "MODEL PREDICTION",
            rows_renderer=lambda: _model_prediction_rows(ser),
        )
        return

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


def _model_prediction_rows(ser: dict) -> None:
    """Render the model's raw probability estimates for moneyline,
    run-line/spread, and totals when no sportsbook lines are
    available.  Data comes from the schedule endpoint's
    _model_prediction sub-dict (set by app.py when the schedule
    short-circuits the no-odds path).  Missing keys render '—'."""
    pred = ser.get("_model_prediction") or {}
    home_team = ser.get("home_team", "Home")
    away_team = ser.get("away_team", "Away")

    rows: list[tuple[str, str]] = []

    # Moneyline -- both sides as raw % from the model.
    ml_h = pred.get("ml_prob_home")
    ml_a = pred.get("ml_prob_away")
    rows.append((
        f"Moneyline ({away_team})",
        f"{float(ml_a) * 100:.1f}%" if isinstance(ml_a, (int, float)) else "—",
    ))
    rows.append((
        f"Moneyline ({home_team})",
        f"{float(ml_h) * 100:.1f}%" if isinstance(ml_h, (int, float)) else "—",
    ))

    # Run-line / spread
    rl_team = pred.get("rl_pick_team")
    rl_prob = pred.get("rl_prob")
    rl_line = pred.get("rl_line")
    if rl_team and isinstance(rl_prob, (int, float)):
        line_s = f" {float(rl_line):+g}" if isinstance(rl_line, (int, float)) else ""
        rows.append((
            "Run Line" if sport_default_is_mlb(ser) else "Spread",
            f"{rl_team}{line_s}: {float(rl_prob) * 100:.1f}%",
        ))

    # Totals -- projected total + over/under direction relative to baseline.
    proj = pred.get("totals_projected")
    baseline = pred.get("totals_baseline")
    if isinstance(proj, (int, float)):
        base = float(baseline) if isinstance(baseline, (int, float)) else 9.0
        direction = "Over" if proj > base else ("Under" if proj < base else "Even")
        rows.append((
            "Projected Total",
            f"{float(proj):.1f}  ({direction} vs {base:g})",
        ))

    if not rows:
        ui.label("Model has no prediction for this matchup yet.").style(
            f"font-size: 12px; color: {t.TEXT_DIM}; font-style: italic;"
        )
        return

    with ui.column().classes("w-full").style("gap: 0;"):
        for label, value in rows:
            with ui.row().classes("items-center w-full").style(
                f"padding: 8px 0; gap: 12px; "
                f"border-bottom: 1px solid {t.BORDER_SOFT};"
            ):
                ui.label(label).style(
                    f"flex: 0 0 40%; font-size: 12px; color: {t.TEXT_DIM};"
                )
                ui.label(value).style(
                    f"flex: 1; font-size: 13px; color: {t.TEXT}; "
                    f"font-weight: 600; font-family: monospace;"
                )
    ui.label(
        "Estimates only — no sportsbook lines available."
    ).style(
        f"font-size: 11px; color: {t.TEXT_DIM2}; font-style: italic; "
        f"margin-top: 6px;"
    )


def sport_default_is_mlb(ser: dict) -> bool:
    """Heuristic used by the Model Prediction block to label the
    spread line.  MLB games carry park / pitcher fields; WNBA games
    don't.  Defaults to MLB when neither hint is present."""
    if ser.get("home_sp") or ser.get("away_sp") or ser.get("park_run_factor"):
        return True
    if ser.get("_sport") == "wnba":
        return False
    return True


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

# Per-process pitcher cache used by _section_pitching_or_lineup so
# multiple matchup page visits for the same game on the same ET date
# share one PitcherClient round-trip.  Keyed by (away_team, home_team,
# et_date); value is the {"home": {...}, "away": {...}} dict.  Cleared
# implicitly on container restart -- pitcher_client itself also has a
# 1-hour disk cache so even a cold container only re-hits MLB at most
# once per (pid, season).
_PITCHER_PAGE_CACHE: dict[tuple[str, str, str], dict] = {}


def _fetch_pitchers_direct(
    sport: str, ser: dict,
) -> tuple[dict, dict]:
    """Call pitcher_client.get_starters_for_game DIRECTLY rather than
    reading the serialized analysis row's home_sp / away_sp.  The
    analysis cache may be in the pre-PR-#84 shape (era / hand / rest /
    whip / k_rate only -- no full_name / team_abbrev / k_per_9 /
    era_home / era_away / last3_era / wins / losses) which is what
    used to leave the matchup page stuck on TBD.

    Returns (away_sp, home_sp) in the new full-field shape.  Empty
    dicts when pitcher_client can't resolve the game; the caller's
    _pitcher_card falls back to TBD + N/A for those.
    """
    if sport != "mlb":
        return {}, {}

    home_team    = (ser.get("home_team") or "").strip()
    away_team    = (ser.get("away_team") or "").strip()
    commence     = ser.get("commence_time") or ""
    # ET date keys the cache + drives the pitcher_client schedule call.
    game_date = ""
    if commence:
        try:
            dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
            game_date = dt.astimezone(_ET).date().isoformat()
        except Exception:                                                  # noqa: BLE001
            game_date = ""
    if not game_date:
        game_date = datetime.now(_ET).date().isoformat()

    if not (home_team and away_team):
        _log(
            f"  _fetch_pitchers_direct: missing team names "
            f"(home={home_team!r} away={away_team!r}) -- skipping fetch"
        )
        return {}, {}

    cache_key = (away_team.lower(), home_team.lower(), game_date)
    cached = _PITCHER_PAGE_CACHE.get(cache_key)
    if cached is not None:
        _log(
            f"  _fetch_pitchers_direct: PAGE CACHE HIT  "
            f"key={cache_key}  home={'YES' if cached.get('home') else 'NO'}  "
            f"away={'YES' if cached.get('away') else 'NO'}"
        )
        return cached.get("away") or {}, cached.get("home") or {}

    _log(
        f"  _fetch_pitchers_direct: calling pitcher_client  "
        f"home={home_team!r}  away={away_team!r}  date={game_date}"
    )
    try:
        from src.pitcher_client import get_pitcher_client
        data = get_pitcher_client().get_starters_for_game(
            home_team, away_team, game_date,
        )
    except Exception as exc:                                              # noqa: BLE001
        _log(f"  _fetch_pitchers_direct: ERROR  "
             f"{type(exc).__name__}: {exc}")
        return {}, {}

    home_sp = (data or {}).get("home") or {}
    away_sp = (data or {}).get("away") or {}
    _PITCHER_PAGE_CACHE[cache_key] = {"home": home_sp, "away": away_sp}
    _log(
        f"  _fetch_pitchers_direct: pitcher_client returned  "
        f"home.full_name={home_sp.get('full_name')!r}  "
        f"home.team_abbrev={home_sp.get('team_abbrev')!r}  "
        f"away.full_name={away_sp.get('full_name')!r}  "
        f"away.team_abbrev={away_sp.get('team_abbrev')!r}"
    )
    return away_sp, home_sp


def _section_pitching_or_lineup(ser: dict, sport: str) -> None:
    if sport == "wnba":
        _section_card(
            "STARTING FIVE",
            rows_renderer=_starting_five_placeholder,
        )
        return
    # Bypass ser["home_sp"] / ser["away_sp"] -- those carry whatever
    # shape the analysis cache was last written in, which may be the
    # pre-PR-#84 format without full_name / team_abbrev / k_per_9 /
    # era_home / era_away / last3_era / wins / losses.  Calling
    # pitcher_client directly guarantees the new full-field shape.
    away_sp, home_sp = _fetch_pitchers_direct(sport, ser)
    _log(
        f"PITCHING_DATA sport={sport}  "
        f"home_team={ser.get('home_team')!r}  away_team={ser.get('away_team')!r}  "
        f"_data_source={ser.get('_data_source')!r}"
    )
    _log(f"  HOME_SP keys={list(home_sp.keys())}  values={home_sp!r}")
    _log(f"  AWAY_SP keys={list(away_sp.keys())}  values={away_sp!r}")
    _section_card(
        "PITCHING MATCHUP",
        rows_renderer=lambda: _pitching_table(away_sp, home_sp,
                                              ser.get("away_team", "Away"),
                                              ser.get("home_team", "Home")),
    )


# Sanity ranges -- any pitcher stat that falls outside its plausible
# real-world band renders "N/A" instead of the bad number.  Locks the
# matchup page against ever showing another "2140%" artifact when the
# upstream pipeline regresses.
_STAT_BOUNDS: dict[str, tuple[float, float]] = {
    "era":       (0.0, 15.0),
    "era_home":  (0.0, 15.0),
    "era_away":  (0.0, 15.0),
    "last3_era": (0.0, 15.0),
    "k_per_9":   (0.0, 20.0),
    "bb9":       (0.0, 15.0),
    "whip":      (0.0, 4.0),
    "rest":      (0.0, 30.0),
}


def _sane(value, key: str) -> Optional[float]:
    """Coerce *value* to float and return it only when inside the
    plausible band for *key*.  None for missing / out-of-range so the
    caller can render "N/A"."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    lo, hi = _STAT_BOUNDS.get(key, (None, None))
    if lo is not None and (v < lo or v > hi):
        return None
    return v


def _fmt_stat(value, key: str, fmt: str) -> str:
    """Sanity-check + format a pitcher stat.  Returns "N/A" for
    missing / out-of-range values; otherwise applies *fmt*."""
    v = _sane(value, key)
    if v is None:
        return "N/A"
    try:
        return fmt.format(v)
    except (TypeError, ValueError):
        return "N/A"


def _pitching_table(away_sp: dict, home_sp: dict, away_team: str, home_team: str) -> None:
    """Side-by-side pitcher cards.  Each card opens with the pitcher's
    full name + a team-abbrev chip; the stat list below stacks
    vertically inside the card.  Both cards live inside .game-grid so
    they sit on one row at >768px and stack on mobile.
    """
    # .game-grid is the existing two-col -> one-col responsive grid
    # used by the slate page (theme.page_head_css).  Reusing it keeps
    # the breakpoint identical to the rest of the app.
    with ui.element("div").classes("game-grid w-full"):
        _pitcher_card(away_sp, side_label="AWAY", fallback_team=away_team)
        _pitcher_card(home_sp, side_label="HOME", fallback_team=home_team)


def _pitcher_card(sp: dict, side_label: str, fallback_team: str) -> None:
    """One pitcher's column: name + team-abbrev chip header on top,
    vertical stat list below.  Empty `sp` (no probable announced
    yet) renders TBD + N/A stats per spec."""
    sp = sp or {}
    pitcher_name = (sp.get("full_name") or "").strip()
    team_abbrev  = (sp.get("team_abbrev") or "").strip().upper()
    has_pitcher  = bool(pitcher_name)

    rows = [
        ("Season ERA",  "era"),
        ("Home ERA",    "era_home"),
        ("Away ERA",    "era_away"),
        ("Last 3 GS",   "last3_era"),
        ("WHIP",        "whip"),
        ("K/9",         "k_per_9"),
        ("BB/9",        "bb9"),
        ("Record",      "_record"),
        ("Days Rest",   "rest"),
    ]

    with ui.column().classes("w-full").style(
        f"background: {t.CARD_HI}; border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; padding: 14px 16px; gap: 10px; "
        f"min-width: 0;"
    ):
        # Side label -- AWAY / HOME, small + muted so the pitcher name
        # is the visual anchor of the card.
        ui.label(side_label + " STARTER").style(
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .8px; "
            f"color: {t.TEXT_DIM2};"
        )

        # Header: pitcher name (large) + team abbrev (small muted chip).
        # Falls back to "TBD" when the probable starter isn't yet
        # announced -- mirror the major sportsbooks' convention.
        with ui.row().classes("items-center w-full").style(
            "gap: 8px; flex-wrap: wrap;"
        ):
            if has_pitcher:
                _name_slug = pitcher_name.lower().replace(" ", "-")
                ui.link(pitcher_name, f"/player/mlb/{_name_slug}").style(
                    f"font-size: 17px; font-weight: 800; color: {t.TEXT}; "
                    f"letter-spacing: -.1px; min-width: 0; "
                    f"text-decoration: none;"
                ).tooltip("View player profile")
            else:
                ui.label("TBD").style(
                    f"font-size: 17px; font-weight: 800; color: {t.TEXT_DIM}; "
                    f"letter-spacing: -.1px;"
                )
            chip_text = team_abbrev or (_short_abbrev(fallback_team) or "—")
            ui.label(chip_text).style(
                f"font-size: 10.5px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM}; "
                f"background: {t.CARD}; "
                f"border: 1px solid {t.BORDER}; "
                f"padding: 2px 8px; border-radius: {t.RADIUS_PILL};"
            )
            # Handedness pill (LHP / RHP).  Hidden when missing so the
            # chip row stays tight.
            hand = sp.get("hand")
            if hand in ("LHP", "RHP"):
                ui.label(hand).style(
                    f"font-size: 10px; font-weight: 700; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2}; margin-left: 2px;"
                )

        # Stat list -- vertical, one row per stat.  All values pass
        # _fmt_stat / record helper so missing or out-of-band data
        # renders "N/A".
        with ui.column().classes("w-full").style("gap: 0; margin-top: 4px;"):
            for label, key in rows:
                if not has_pitcher:
                    value_str = "N/A"
                elif key == "_record":
                    value_str = _format_record(sp)
                elif key == "rest":
                    value_str = _fmt_stat(sp.get(key), key, "{:.0f}")
                elif key == "k_per_9":
                    value_str = _fmt_stat(sp.get(key), key, "{:.1f} K/9")
                elif key == "bb9":
                    value_str = _fmt_stat(sp.get(key), key, "{:.1f} BB/9")
                else:
                    value_str = _fmt_stat(sp.get(key), key, "{:.2f}")
                with ui.row().classes("items-center w-full").style(
                    f"padding: 7px 0; gap: 10px; "
                    f"border-bottom: 1px solid {t.BORDER_SOFT};"
                ):
                    ui.label(label).style(
                        f"flex: 1; font-size: 12px; color: {t.TEXT_DIM};"
                    )
                    ui.label(value_str).style(
                        f"font-size: 13px; color: {t.TEXT}; "
                        f"font-family: monospace; font-weight: 700;"
                    )


def _format_record(sp: dict) -> str:
    """Season W-L formatted as "4-2".  Returns "N/A" when neither value
    is present (a true 0-0 record falls through to "0-0" instead of
    N/A so opening-day starters still render correctly)."""
    w = sp.get("wins")
    l = sp.get("losses")
    if w is None and l is None:
        return "N/A"
    try:
        return f"{int(w or 0)}-{int(l or 0)}"
    except (TypeError, ValueError):
        return "N/A"


def _short_abbrev(team_name: str) -> str:
    """Best-effort fallback abbreviation when the pitcher pipeline
    didn't return one (e.g. /teams/{id} failed).  Builds initials
    from the leading-uppercase words ("Los Angeles Dodgers" -> "LAD").
    Returns "" so the caller can render an em-dash instead."""
    if not team_name:
        return ""
    parts = [p for p in str(team_name).split() if p]
    initials = "".join(p[0] for p in parts if p and p[0].isalpha())
    return initials[:3].upper() if initials else ""


def _section_venue(ser: dict) -> None:
    """Standalone Venue card under the pitcher matchup.  Shows the
    home ballpark name plus its run factor labeled as Hitter Friendly
    (>105), Pitcher Friendly (<95), or Neutral (95-105) -- the user-
    facing convention from FanGraphs' 100-base park factors."""
    park = ser.get("park_run_factor")
    venue = ser.get("venue_name") or "—"
    try:
        pv = int(round(float(park))) if park is not None else None
    except (TypeError, ValueError):
        pv = None
    if pv is None:
        tag, tag_color = "—", t.TEXT_DIM2
    elif pv > 105:
        tag, tag_color = "Hitter Friendly", t.POS
    elif pv < 95:
        tag, tag_color = "Pitcher Friendly", t.NEG
    else:
        tag, tag_color = "Neutral", t.TEXT_DIM

    def _rows() -> None:
        with ui.row().classes("items-center w-full").style(
            f"padding: 4px 0; gap: 12px;"
        ):
            with ui.column().style("flex: 1; min-width: 0; gap: 2px;"):
                ui.label("Ballpark").style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                    f"letter-spacing: .5px; font-weight: 700;"
                )
                ui.label(venue).style(
                    f"font-size: 14px; color: {t.TEXT}; font-weight: 700; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )
            with ui.column().style(
                "flex: 0 0 auto; gap: 2px; align-items: flex-end;"
            ):
                ui.label("Run Factor").style(
                    f"font-size: 10.5px; color: {t.TEXT_DIM2}; "
                    f"letter-spacing: .5px; font-weight: 700;"
                )
                ui.label(f"{pv}" if pv is not None else "N/A").style(
                    f"font-size: 22px; font-weight: 800; color: {t.TEXT}; "
                    f"font-family: monospace; letter-spacing: -.2px;"
                )
            ui.label(tag).style(
                f"flex: 0 0 auto; font-size: 11px; font-weight: 800; "
                f"letter-spacing: .5px; color: {tag_color}; "
                f"background: {t.CARD_HI}; "
                f"padding: 4px 10px; border-radius: {t.RADIUS_PILL};"
            )

    _section_card("VENUE", rows_renderer=_rows)


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
    wx   = ser.get("weather") or {}
    line_move = (ser.get("meta") or {}).get("line_movement")

    # Ballpark run factor lives in its own VENUE card above (MLB only).
    # Game Context now focuses on weather + line movement so the two
    # sections don't repeat the same number.
    rows: list[tuple[str, str]] = []
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
