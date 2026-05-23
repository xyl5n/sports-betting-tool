"""
One game card -- matchup header + three bet boxes (ML / RL-Spread / Totals).

Takes a serialized game dict in the shape produced by app._serialize() /
app._serialize_wnba().  Tolerates missing fields so a half-populated
result still renders without crashing.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from nicegui import ui

from . import theme as t
from . import bet_box
from . import track_button
from . import team_logo
from . import live_score

_ET = ZoneInfo("America/New_York")


def render(g: dict, sport: str = "mlb", backend=None) -> None:
    """Render one game's card.  `g` is a _serialize() output dict.

    When `g["_no_model"]` is truthy the matchup is shown with market odds
    but the three bet-box row is replaced by an inline NO MODEL PICK
    notice.  This is how games involving teams missing from the model's
    training data (e.g. 2026 WNBA expansion teams) still appear on the
    Sports tab instead of vanishing silently.

    `backend` is the imported `app` module (passed through pages -> here).
    When provided, a Track button appears at the bottom of the card and
    posts to the in-process /api/.../ledger/confirm/<game_id> endpoint.
    When omitted (legacy call sites that haven't been updated), no Track
    button is rendered -- the card still works, just without tracking.
    """
    import sys as _sys
    # _data_source is stamped by /api/schedule -- one of:
    #   analysis_id              -- joined to analysis result via Odds API id
    #   analysis_team_date       -- joined via (home, away, et_date) fallback
    #   schedule_stub            -- schedule game with no analysis match
    #                               (this is the bug-symptom case)
    #   no_odds_cached_prediction  -- schedule game + pre-computed prediction
    #   no_odds_live_prediction    -- schedule game + on-demand prediction
    #   unknown                  -- card built outside the schedule endpoint
    # If you see schedule_stub for cards you expected to have odds, the
    # schedule->analysis merge missed -- check the SCHEDULE JOIN log in
    # app.py / the analyze route's ODDS FETCH COMPLETE log.
    print(
        f"[RENDER] game_card.render ENTER sport={sport} "
        f"game_id={g.get('game_id') or g.get('id')!r} "
        f"away={g.get('away_team')!r} home={g.get('home_team')!r} "
        f"no_odds={bool(g.get('_no_odds'))} "
        f"no_model={bool(g.get('_no_model'))} "
        f"data_source={g.get('_data_source') or 'unknown'!r}",
        flush=True, file=_sys.stderr,
    )
    sport = (sport or g.get("_sport") or "mlb").lower()
    is_mlb = sport == "mlb"

    # Live score lookup -- comes from components/live_score's cache,
    # populated by the per-page poller in pages/sport.py.  None when no
    # live data is available (offline backend, pre-game, etc.).
    live = live_score.lookup(
        sport,
        game_id=(g.get("game_id") or g.get("id")),
        away_team=g.get("away_team", ""),
        home_team=g.get("home_team", ""),
    )
    state = live_score.state_of(live)            # 'live' | 'final' | 'scheduled'

    with ui.column().style(
        f"background: {t.CARD}; "
        f"border: 1px solid {t.BORDER}; "
        f"border-radius: {t.RADIUS_MD}; "
        f"padding: {t.SPACE_MD}; "
        f"gap: {t.SPACE_SM}; width: 100%;"
    ):
        _meta_row(g, sport, state=state)
        _matchup_row(g, sport, state=state)
        # In-progress / completed games show the big centered score block
        # between the matchup row and the bet boxes.  Pre-game cards skip
        # the score block entirely so the layout stays compact.
        if state in ("live", "final") and live is not None:
            live_score.render_score_block(live, sport)
        if g.get("_no_odds"):
            # Schedule-driven slate: game exists upstream (MLB Stats /
            # ESPN) but The Odds API has no lines for it yet (or the
            # date is past the odds window).  Show the matchup +
            # commence time with a "No Odds Available" notice.
            _no_odds_row(g)
        elif g.get("_no_model"):
            _no_model_row(g)
        else:
            # Pass live so _bet_boxes can compute per-market W/L tints
            # for FINAL games.  Pre-game and live (in-progress) cards
            # get None back from _final_scores -> neutral boxes.
            _bet_boxes(g, is_mlb, live=live if state == "final" else None)
        if backend is not None and not g.get("_no_odds"):
            _track_row(backend, g, sport)


def _track_row(backend, g: dict, sport: str) -> None:
    """Bottom row: 'View Details →' link on the left, per-bet-type Track
    buttons on the right.

    MLB cards get three buttons (Track ML / Track RL / Track Total),
    each tracking only its own bet; a bet type already confirmed today
    shows a muted "✓" chip instead.  WNBA only supports moneyline
    tracking, so it keeps the single ML button.
    """
    gid = g.get("game_id") or g.get("id")
    is_mlb = (sport or "").lower() == "mlb"
    tracked = track_button.tracked_bet_types(backend, gid, sport) if gid else set()

    with ui.row().classes("items-center w-full").style(
        "gap: 6px; margin-top: 4px; flex-wrap: wrap;"
    ):
        if gid:
            ui.link("View Details →", f"/game/{sport}/{gid}").style(
                f"color: {t.PRIMARY}; text-decoration: none; "
                f"font-size: 11.5px; font-weight: 700; padding: 4px 0;"
            )
        ui.element("div").style("flex: 1;")    # spacer

        if g.get("_no_model"):
            track_button.render(
                backend, game_id=gid, sport=sport, size="sm", bet_type="ml",
                disabled_reason=(
                    "No model pick available for this matchup -- "
                    "tracking would record an empty bet."
                ),
            )
            return

        # Moneyline -- always available.
        track_button.render(
            backend, game_id=gid, sport=sport, size="sm", bet_type="ml",
            already_tracked=("single" in tracked),
        )
        # Run line + Total -- MLB only, and only when a pick exists.
        if is_mlb and g.get("run_line"):
            track_button.render(
                backend, game_id=gid, sport=sport, size="sm", bet_type="rl",
                already_tracked=("run_line" in tracked),
            )
        if is_mlb and g.get("totals"):
            track_button.render(
                backend, game_id=gid, sport=sport, size="sm", bet_type="total",
                already_tracked=("totals" in tracked),
            )


def _no_model_row(g: dict) -> None:
    """Inline notice that replaces the three bet boxes when the model
    couldn't generate a prediction for this matchup."""
    reason = g.get("_no_model_reason") or "No model pick available for this matchup."
    with ui.row().classes("w-full").style(
        f"background: {t.CARD_HI}; "
        f"border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 10px 12px; gap: 10px; align-items: center;"
    ):
        ui.label("NO MODEL PICK").style(
            f"flex-shrink: 0; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"color: {t.WARN}; "
            f"background: rgba(251, 191, 36, 0.12); "
            f"padding: 2px 7px; border-radius: 3px;"
        )
        ui.label(reason).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; "
            f"flex: 1; line-height: 1.4;"
        )


def _no_odds_row(g: dict) -> None:
    """Inline block for games on the schedule but missing from The
    Odds API.  When the schedule endpoint attached a model-only
    prediction (g["_model_prediction"]), render three prediction
    sections: Predicted Winner, Run Line Prediction, Projected
    Total.  Otherwise fall back to a "No Odds Available" notice.

    Either path replaces the normal bet boxes AND the Track button
    -- there's no market to bet against, no edge to compute, no
    Kelly sizing to display, and nothing to record in the model
    pick history (no settlement possible without odds).
    """
    pred = g.get("_model_prediction") or {}
    if pred:
        _model_prediction_block(g, pred)
        return

    # Default fallback when the predictor stack isn't ready or the
    # game's teams aren't in the GameStore yet.
    with ui.row().classes("w-full").style(
        f"background: {t.CARD_HI}; "
        f"border: 1px dashed {t.BORDER}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 10px 12px; gap: 10px; align-items: center;"
    ):
        ui.label("NO ODDS AVAILABLE").style(
            f"flex-shrink: 0; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"color: {t.TEXT_DIM2}; "
            f"background: {t.CARD}; "
            f"padding: 2px 7px; border-radius: 3px; "
            f"border: 1px solid {t.BORDER};"
        )
        ui.label(
            "Game scheduled but no betting lines have been posted by "
            "the sportsbooks yet."
        ).style(
            f"font-size: 12px; color: {t.TEXT_DIM}; "
            f"flex: 1; line-height: 1.4;"
        )


def _model_prediction_block(g: dict, pred: dict) -> None:
    """Three model-only prediction sections that replace the bet
    boxes when no odds exist.  Styled with a dashed PRIMARY border +
    a 'MODEL PREDICTION' badge so the user can't mistake these for
    actual sportsbook lines.

    Sections:
      1. Predicted Winner -- home% vs away% from the moneyline model
      2. Run Line / Spread -- which side covers + probability against
         a synthetic line (-1.5 MLB / -2.5 WNBA, the standard slabs)
      3. Projected Total -- model's raw point/run estimate + an
         Over/Under tag relative to a neutral baseline (9 MLB / 160 WNBA)
    """
    # Outer container: dashed PRIMARY border + faint purple bg so the
    # block reads as "estimate" not "live market line".  No box-shadow
    # hover glow (the card itself already has one).
    with ui.column().classes("w-full").style(
        f"background: rgba({t.PRIMARY_R}, {t.PRIMARY_G}, {t.PRIMARY_B}, 0.04); "
        f"border: 1px dashed {t.PRIMARY}; "
        f"border-radius: {t.RADIUS_SM}; "
        f"padding: 10px 12px; gap: 8px;"
    ):
        # Header chip
        with ui.row().classes("items-center w-full").style("gap: 6px;"):
            ui.label("MODEL PREDICTION").style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
                f"color: {t.PRIMARY}; "
                f"background: rgba({t.PRIMARY_R}, {t.PRIMARY_G}, {t.PRIMARY_B}, 0.12); "
                f"padding: 2px 7px; border-radius: 3px;"
            )
            ui.label("no betting lines available -- estimates only").style(
                f"font-size: 10px; color: {t.TEXT_DIM2}; "
                f"font-style: italic;"
            )

        # 1) Predicted Winner -- two short labels side-by-side
        home_team = _short(g.get("home_team", ""))
        away_team = _short(g.get("away_team", ""))
        home_p = float(pred.get("ml_prob_home") or 0.5) * 100
        away_p = float(pred.get("ml_prob_away") or 0.5) * 100
        with ui.column().style("gap: 2px; width: 100%;"):
            ui.label("PREDICTED WINNER").style(
                f"font-size: 9px; font-weight: 700; letter-spacing: .5px; "
                f"color: {t.TEXT_DIM2};"
            )
            with ui.row().classes("items-center justify-between w-full").style("gap: 8px;"):
                ui.label(f"{home_team} {home_p:.0f}%").style(
                    f"font-size: 13px; font-weight: 700; "
                    f"color: {t.POS if home_p > 50 else t.TEXT};"
                )
                ui.label("vs").style(
                    f"font-size: 11px; color: {t.TEXT_DIM2};"
                )
                ui.label(f"{away_team} {away_p:.0f}%").style(
                    f"font-size: 13px; font-weight: 700; "
                    f"color: {t.POS if away_p > 50 else t.TEXT};"
                )

        # 2) Run Line / Spread -- only when the RL/spread head returned
        if pred.get("rl_pick_team") and pred.get("rl_prob") is not None:
            rl_team = _short(pred["rl_pick_team"])
            rl_line = pred.get("rl_line") or -1.5
            rl_prob = float(pred["rl_prob"]) * 100
            line_str = f"{rl_line:+g}" if rl_line is not None else ""
            with ui.column().style("gap: 2px; width: 100%;"):
                ui.label("RUN LINE PREDICTION").style(
                    f"font-size: 9px; font-weight: 700; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                ui.label(f"{rl_team} {line_str} at {rl_prob:.0f}%").style(
                    f"font-size: 13px; font-weight: 700; color: {t.TEXT};"
                )

        # 3) Projected Total -- only when totals head returned
        if pred.get("totals_projected") is not None:
            proj = float(pred["totals_projected"])
            baseline = float(pred.get("totals_baseline") or 9.0)
            direction = "Over" if proj > baseline else ("Under" if proj < baseline else "Even")
            dir_color = (
                t.POS if direction == "Over" else
                t.NEG if direction == "Under" else
                t.TEXT_DIM
            )
            with ui.column().style("gap: 2px; width: 100%;"):
                ui.label("PROJECTED TOTAL").style(
                    f"font-size: 9px; font-weight: 700; letter-spacing: .5px; "
                    f"color: {t.TEXT_DIM2};"
                )
                with ui.row().classes("items-baseline").style("gap: 6px;"):
                    ui.label(f"{proj:.1f}").style(
                        f"font-size: 18px; font-weight: 800; color: {t.TEXT}; "
                        f"font-family: monospace; letter-spacing: -.2px;"
                    )
                    ui.label(f"projected (vs {baseline:g} baseline)").style(
                        f"font-size: 10.5px; color: {t.TEXT_DIM};"
                    )
                    ui.label(direction.upper()).style(
                        f"font-size: 10px; font-weight: 800; letter-spacing: .5px; "
                        f"color: {dir_color}; margin-left: auto;"
                    )


def _meta_row(g: dict, sport: str, state: str = "scheduled") -> None:
    when = _fmt_et(g.get("commence_time", ""))
    gid  = g.get("game_id") or g.get("id")
    with ui.row().classes("items-center w-full").style("gap: 8px;"):
        ui.label(sport.upper()).style(
            f"background: {t.CARD_HI}; color: {t.TEXT}; "
            f"font-size: 9.5px; font-weight: 800; letter-spacing: .5px; "
            f"padding: 2px 7px; border-radius: {t.RADIUS_PILL};"
        )
        ui.label(when).style(f"font-size: 12px; color: {t.TEXT_DIM};")
        # Matchup pill -- per user spec: "add a Matchup button on every
        # game card in the Full Slate page on the same line as the game
        # time and date, positioned near the VS or score area in the
        # center of the card. The button should be small and unobtrusive,
        # styled as a subtle pill or chip labeled Matchup."  Sits in the
        # middle of the meta row (between time on the left and the
        # LIVE/FINAL chip on the right) via no margin overrides.
        if gid:
            ui.link("Matchup", f"/matchup/{sport}/{gid}").style(
                f"background: {t.CARD_HI}; color: {t.PRIMARY}; "
                f"border: 1px solid {t.BORDER}; "
                f"font-size: 10px; font-weight: 700; letter-spacing: .3px; "
                f"padding: 2px 9px; border-radius: {t.RADIUS_PILL}; "
                f"text-decoration: none; line-height: 1.4;"
            )
        # LIVE indicator: pulsing dot + label.  Pushes to the right side
        # of the meta row via margin-left:auto so the time stays adjacent
        # to the sport chip.
        if state == "live":
            with ui.row().classes("items-center").style(
                f"gap: 4px; margin-left: auto;"
            ):
                live_score.render_live_dot()
                ui.label("LIVE").style(
                    f"font-size: 9.5px; font-weight: 800; letter-spacing: .8px; "
                    f"color: {t.POS};"
                )
        elif state == "final":
            ui.label("FINAL").style(
                f"font-size: 9.5px; font-weight: 800; letter-spacing: .8px; "
                f"color: {t.TEXT_DIM}; margin-left: auto;"
            )


def _matchup_row(g: dict, sport: str, state: str = "scheduled") -> None:
    """Matchup row layout depends on state:

      scheduled  -- team name on each side, opening odds below, "VS"
                     separator in the middle (per spec).
      live/final -- team name only; the big score block below carries
                     the numbers.  Odds for in-progress games are stale
                     and would compete with the live score for attention.
    """
    away_full = g.get("away_team", "—") or "—"
    home_full = g.get("home_team", "—") or "—"
    away   = _short(away_full)
    home   = _short(home_full)
    a_odds = _odds_str(g.get("away_odds"))
    h_odds = _odds_str(g.get("home_odds"))
    show_odds = state == "scheduled"
    separator = "VS" if state == "scheduled" else "–"
    with ui.row().classes("items-center w-full").style("gap: 10px;"):
        # Away: logo on the left of the name column.
        team_logo.render(away_full, sport=sport, size=36)
        with ui.column().style("flex: 1; gap: 2px; min-width: 0;"):
            ui.label(away).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            if show_odds:
                ui.label(a_odds).style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
                )
        ui.label(separator).style(
            f"color: {t.TEXT_DIM2}; font-size: 11px; font-weight: 700; "
            f"letter-spacing: .8px;"
        )
        # Home: name column on the left of the logo (text right-aligned).
        with ui.column().style("flex: 1; gap: 2px; text-align: right; "
                                "align-items: flex-end; min-width: 0;"):
            ui.label(home).style(
                f"font-size: 14px; font-weight: 700; color: {t.TEXT}; "
                f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
            )
            if show_odds:
                ui.label(h_odds).style(
                    f"font-size: 11px; color: {t.TEXT_DIM}; font-family: monospace;"
                )
        team_logo.render(home_full, sport=sport, size=36)


def _final_scores(live: dict | None) -> tuple[int, int] | None:
    """Pull (home_runs, away_runs) from a FINAL linescore.  Returns None
    if live data is missing or doesn't have both teams' totals."""
    if not live:
        return None
    ls = live.get("linescore") or {}
    teams_ls = ls.get("teams") or {}
    h = (teams_ls.get("home") or {}).get("runs")
    a = (teams_ls.get("away") or {}).get("runs")
    if h is None or a is None:
        return None
    try:
        return int(h), int(a)
    except (TypeError, ValueError):
        return None


def _ml_result(g: dict, scores: tuple[int, int] | None) -> str | None:
    """Moneyline win/loss for the picked team given the final score."""
    if scores is None:
        return None
    home_runs, away_runs = scores
    if home_runs == away_runs:
        # Baseball / basketball don't end in ties, but defend against
        # a partial / corrupted linescore by treating it as push.
        return "push"
    pick_team = g.get("pick_team")
    if not pick_team:
        return None
    home_won = home_runs > away_runs
    picked_home = pick_team == g.get("home_team")
    return "win" if (picked_home == home_won) else "loss"


def _spread_result(rl: dict, scores: tuple[int, int] | None) -> str | None:
    """Run-line / spread result.  Uses the same convention Ledger.settle()
    follows: prop_line is from the home team's perspective, margin =
    home_runs - away_runs, and `side` is "home" or "away" for the pick."""
    if scores is None:
        return None
    home_runs, away_runs = scores
    line = rl.get("run_line_point", rl.get("spread_line"))
    if line is None:
        return None
    try:
        prop_line = float(line)
    except (TypeError, ValueError):
        return None
    side = rl.get("side")
    margin = home_runs - away_runs
    if   margin >  prop_line: return "win"  if side == "home" else "loss"
    elif margin <  prop_line: return "loss" if side == "home" else "win"
    else:                     return "push"


def _totals_result(totals: dict, scores: tuple[int, int] | None) -> str | None:
    """Over/under result against the final combined score."""
    if scores is None:
        return None
    home_runs, away_runs = scores
    line = totals.get("total_line")
    if line is None:
        return None
    try:
        prop_line = float(line)
    except (TypeError, ValueError):
        return None
    direction = (totals.get("direction") or "over").lower()
    total = home_runs + away_runs
    if   total >  prop_line: return "win"  if direction == "over" else "loss"
    elif total <  prop_line: return "loss" if direction == "over" else "win"
    else:                    return "push"


def _bet_boxes(g: dict, is_mlb: bool, live: dict | None = None) -> None:
    rl = g.get("run_line") if is_mlb else g.get("spread_pick")
    totals = g.get("totals") or {}

    # FINAL games only -- pre-game and in-progress games pass live=None
    # (or a non-final live row) so every result resolves to None below
    # and the boxes render with the default neutral tint.
    scores = _final_scores(live)

    with ui.row().classes("w-full bet-boxes").style("gap: 6px;"):
        # Moneyline -- always present
        bet_box.render(
            label="MONEYLINE",
            pick=_short(g.get("pick_team")) if g.get("pick_team") else None,
            prob=g.get("pick_prob"),
            edge=g.get("pick_edge"),
            odds=g.get("pick_odds"),
            is_value=bool(g.get("value_pick")),
            result=_ml_result(g, scores),
        )

        # Run Line (MLB) / Spread (WNBA)
        if rl:
            line = rl.get("run_line_point") if is_mlb else rl.get("spread_line")
            line_str = ""
            if line is not None:
                pt = float(line)
                if rl.get("side") != "home":
                    pt = -pt
                line_str = f" {pt:+g}"
            pick_str = _short(rl.get("pick_team", "")) + line_str if rl.get("pick_team") else None
            bet_box.render(
                label="RUN LINE" if is_mlb else "SPREAD",
                pick=pick_str,
                prob=rl.get("pick_prob"),
                edge=rl.get("edge"),
                odds=rl.get("pick_odds"),
                is_value=bool(rl.get("value_bet")),
                result=_spread_result(rl, scores),
            )
        else:
            bet_box.render(
                label="RUN LINE" if is_mlb else "SPREAD",
                pick=None, prob=None, edge=None, odds=None, is_value=False,
            )

        # Totals
        if totals and totals.get("total_line") is not None:
            direction = (totals.get("direction") or "over").upper()
            pick_str = f"{direction} {totals.get('total_line')}"
            odds = (
                totals.get("over_odds") if direction == "OVER"
                else totals.get("under_odds")
            )
            bet_box.render(
                label="TOTALS",
                pick=pick_str,
                prob=totals.get("pick_prob"),
                edge=totals.get("edge"),
                odds=odds,
                is_value=bool(totals.get("value_bet")),
                result=_totals_result(totals, scores),
            )
        else:
            bet_box.render(
                label="TOTALS",
                pick=None, prob=None, edge=None, odds=None, is_value=False,
            )


# ── Small helpers ───────────────────────────────────────────────────────────

def _short(name: str | None) -> str:
    if not name:
        return "—"
    parts = name.split()
    if not parts:
        return name
    # "San Francisco Giants" -> "SF Giants" -- match the legacy UI's pattern
    if len(parts) >= 3:
        initials = "".join(p[0] for p in parts[:-1] if p[0].isupper())
        return f"{initials} {parts[-1]}" if initials else name
    return name


def _odds_str(o) -> str:
    if o is None:
        return "—"
    try:
        n = int(o)
    except Exception:                                                     # noqa: BLE001
        return str(o)
    return f"+{n}" if n > 0 else f"{n}"


def _fmt_et(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %-I:%M %p ET")
    except Exception:                                                     # noqa: BLE001
        return iso[:16]
