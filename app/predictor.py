"""predictor.py -- no-odds game prediction (Phase C, PR #289).

When a game has no live betting odds yet, these functions run the trained
models directly to produce a prediction, and cache the day's no-odds
predictions to disk + Supabase.  Full BFS closure: 4 functions, 185 lines.

ARCHITECTURE NOTE (documented backwards edge):
    This module imports `from scheduler import *` because the cluster
    references THREE names that currently live in scheduler.py:
        _eprint                   (redaction-aware stderr logger; PR #277)
        _fetch_raw_schedule       (schedule fetcher; PR #281)
        _ensure_no_odds_predictor (lazy model loader; PR #284)
    That makes predictor.py (low-level prediction) depend on scheduler.py
    (high-level job orchestration) -- an inversion.  It is CYCLE-FREE
    (verified: no satellite module references the 4 predictor functions),
    but it is a smell.  A future cleanup could relocate _eprint to a
    low-level logging module, _fetch_raw_schedule to a data-fetch module,
    and _ensure_no_odds_predictor into predictor.py itself, which would
    invert the edge back to the natural direction.  Under the move-only
    rule there is no cleaner path today.

Direction (no cycles):
    state/utils/serializer -> scheduler -> predictor -> app
    predictor.py imports utils + scheduler; it NEVER imports app.py.

The no-odds-predictor state dicts (_no_odds_predictor,
_no_odds_predictor_failed) live in state.py (PR #284) and are consumed
by scheduler._ensure_no_odds_predictor -- the predictor cluster itself
does not reference them directly.
"""
from __future__ import annotations

from utils import *      # noqa: F401,F403  (_no_odds_predictions_cache_key)
from scheduler import *  # noqa: F401,F403  (_eprint, _fetch_raw_schedule,
                         #                    _ensure_no_odds_predictor)

__all__ = [
    "_predict_no_odds_game",
    "_read_no_odds_predictions",
    "_write_no_odds_predictions",
    "_prefetch_no_odds_predictions",
]

# moved from app.py:1287
def _predict_no_odds_game(sport: str, g: dict) -> dict | None:
    """Run the model on one no-odds game.  Returns:
        {ml_prob_home, ml_prob_away, rl_pick, rl_prob, rl_line,
         totals_pred, totals_direction, totals_baseline}
    or None when prediction fails (frontend falls back to the
    "No Odds Available" notice in that case).

    No edge / Kelly sizing -- there's no market to compare against.
    Result is NOT recorded in any tracker file since there's nothing
    to settle (no odds means no bet was ever placed)."""
    sport = sport.lower()
    _matchup = f"{g.get('away_team','?')} @ {g.get('home_team','?')}"
    pred = _ensure_no_odds_predictor(sport)
    if pred is None:
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- predictor unavailable")
        return None
    fb, ml_model, rl_model, totals_model = pred
    if not ml_model or not getattr(ml_model, "is_trained", False):
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- ML model not trained")
        return None

    # Inject a neutral implied prob + zero spread so build_for_game
    # doesn't penalize the no-odds game vs ones that came in with
    # market signal.  The model's xgb_input_kind="market_free" head
    # ignores these anyway; this just stops downstream code from
    # KeyError'ing on the optional fields.
    g_for_features = dict(g)
    g_for_features.setdefault("home_implied_prob", 0.5)
    g_for_features.setdefault("spread", 0.0)
    try:
        built = fb.build_for_game(g_for_features)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"build_for_game raised {type(exc).__name__}: {exc}")
        return None
    if built is None:
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"build_for_game returned None (team unresolved or no team stats)")
        return None
    feature_vec, meta = built

    try:
        ml = ml_model.predict(feature_vec, weights=None, game_meta=g_for_features)
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"NO-ODDS PREDICT [{sport.upper()}] {_matchup}: skip -- "
                f"ml predict raised {type(exc).__name__}: {exc}")
        return None
    home_prob = float(ml.get("home_win_prob") or 0.5)

    out = {
        "ml_prob_home": round(home_prob, 4),
        "ml_prob_away": round(1.0 - home_prob, 4),
    }

    # Run line / spread -- neutral line so the model picks its favored side
    # against a market-free baseline.  MLB: -1.5 (the standard run line).
    # WNBA: -2.5 (a typical small spread).  The probability returned is
    # P(home covers); pick_team flips for the underdog branch.
    if rl_model and getattr(rl_model, "is_trained", False):
        try:
            g_rl = dict(g_for_features)
            if sport == "mlb":
                g_rl.setdefault("run_line_point", -1.5)
            else:
                g_rl.setdefault("spread", -2.5)
            rl = rl_model.predict(
                feature_vec, g_rl, weights=None,
                ml_prob_home    = ml.get("xgb_prob"),
                ml_lr_prob_home = ml.get("lr_prob"),
                ml_nn_prob_home = ml.get("nn_prob"),
            )
            if rl:
                out["rl_pick_team"] = rl.get("pick_team") or (
                    g.get("home_team") if rl.get("side") == "home" else g.get("away_team")
                )
                out["rl_pick_side"] = rl.get("side")
                out["rl_prob"]      = round(float(rl.get("pick_prob") or 0.0), 4)
                out["rl_line"]      = -1.5 if sport == "mlb" else -2.5
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: RL predict skipped: {exc}")

    # Totals -- pass a baseline line so the totals model emits a direction
    # (the actual displayed value is the projected raw total).
    if totals_model and getattr(totals_model, "is_trained", False):
        try:
            g_tot = dict(g_for_features)
            baseline = 9.0 if sport == "mlb" else 160.0
            g_tot.setdefault("total_line", baseline)
            g_tot.setdefault("over_odds",  -110)
            g_tot.setdefault("under_odds", -110)
            totals_vec = fb.build_totals_from_meta(meta) if hasattr(fb, "build_totals_from_meta") else None
            if totals_vec is not None:
                tot = totals_model.predict(totals_vec, g_tot, weights=None)
                if tot:
                    out["totals_projected"] = float(tot.get("predicted_total") or 0)
                    out["totals_direction"] = (tot.get("direction") or "").lower()
                    out["totals_baseline"]  = baseline
        except Exception as exc:                                          # noqa: BLE001
            _eprint(f"NO-ODDS PREDICT [{sport.upper()}]: totals predict skipped: {exc}")

    return out

# moved from app.py:1605
def _read_no_odds_predictions(sport: str, date_str: str) -> dict[str, dict]:
    """Return the cached {game_id: prediction} dict for one date.
    Empty dict if not cached / read fails."""
    try:
        from src import db as _db
        row = _db.cache_get(_no_odds_predictions_cache_key(sport, date_str))
        if row and isinstance(row.get("data"), dict):
            preds = row["data"].get("predictions")
            if isinstance(preds, dict):
                return preds
    except Exception:                                                     # noqa: BLE001
        pass
    return {}

# moved from app.py:1620
def _write_no_odds_predictions(sport: str, date_str: str,
                                preds: dict[str, dict]) -> bool:
    """Persist {game_id: prediction} for one date to Supabase.

    Uses the "no_odds" date sentinel so cache_delete_stale (which
    prunes rows where date != today_et) leaves this row alone --
    future-date predictions stay valid as the calendar advances."""
    try:
        from src import db as _db
        if not _db.is_supabase():
            return False
        return bool(_db.cache_set(
            _no_odds_predictions_cache_key(sport, date_str),
            sport, "no_odds",
            {"date": date_str, "predictions": preds},
        ))
    except Exception:                                                     # noqa: BLE001
        return False

# moved from app.py:1640
def _prefetch_no_odds_predictions(sport: str, date_str: str) -> dict:
    """Run _predict_no_odds_game on every game in the sport's schedule
    for date_str, then persist the {game_id: prediction} map.

    Called on-demand by the schedule endpoint (and previously by the
    midnight reset).  NOTE: the nightly cycle's JOB 3 deliberately does
    NOT call this -- the 3 AM prefetch is schedule-only (no model
    scoring); the real predictions come from the 8 AM analysis run.
    When invoked, it leaves predictions ready for every scheduled game
    so a page load doesn't wait on the GameStore + model load before
    seeing cards with Predicted Winner / Run Line / Projected Total.

    Per-game failures are skipped (logged with the matchup).  An
    empty schedule returns {} without writing to Supabase."""
    games = _fetch_raw_schedule(sport, date_str)
    if not games:
        return {}

    out: dict[str, dict] = {}
    skipped = 0
    for g in games:
        gid = str(g.get("id") or "")
        if not gid:
            skipped += 1
            continue
        try:
            pred = _predict_no_odds_game(sport, g)
        except Exception as exc:                                          # noqa: BLE001
            _eprint(
                f"NO-ODDS PREFETCH [{sport.upper()}] {g.get('away_team','?')} "
                f"@ {g.get('home_team','?')}: {type(exc).__name__}: {exc}"
            )
            skipped += 1
            continue
        if pred is None:
            skipped += 1
            continue
        out[gid] = pred

    if out:
        wrote = _write_no_odds_predictions(sport, date_str, out)
        _eprint(
            f"NO-ODDS PREFETCH [{sport.upper()}] {date_str}: "
            f"predicted {len(out)} game(s), skipped {skipped}, "
            f"supabase_write={wrote}"
        )
    else:
        _eprint(
            f"NO-ODDS PREFETCH [{sport.upper()}] {date_str}: "
            f"0 predictions (skipped {skipped}; predictor may not "
            f"be loadable yet)"
        )
    return out
