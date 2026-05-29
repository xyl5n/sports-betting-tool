"""Server-rendered home page (NiceGUI -> HTML migration).

Serves index.html for both "/" and "/home-v2" with the home data injected
server-side via Jinja: EV Scan + Confidence carousels, the four model-record
stat chips, the bottom Model Performance row, today's News, and (per card)
settlement state so won/lost picks show their badge + P/L.

This module imports nothing from app.py.  app.py calls register(app, ...) and
passes the few backend helpers this page needs (snapshot readers, the
home_stats module, the news_feed module, and the Ledger class), which keeps
the dependency one-directional and avoids a circular import.
"""
from __future__ import annotations

import logging

from flask import render_template, request

_SPORTS = ("mlb", "wnba")

# NOTE on Track buttons: the home EV / Confidence cards are GAME picks
# (moneyline / run line / spread / totals), NOT player props.  The template's
# Track button therefore calls the existing trackPick(gameId, bet_type, sport)
# JS helper, which routes 'single' to /api/ledger/confirm/<id> and other game
# bet types to /api/ledger/track_prop -- the correct game-bet endpoints.  It
# deliberately does NOT use /api/props/track, which is for player props only.
# The view-model's `bet_type` is already the backend token trackPick expects.


# ── small formatting helpers ────────────────────────────────────────────────

def _pct_str(pct) -> str:
    """0.62 -> '62%'.  None / non-numeric -> '—'."""
    try:
        return f"{round(float(pct) * 100)}%"
    except (TypeError, ValueError):
        return "—"


def _tone(pct) -> str:
    """CSS tone class for a win rate: 'pos' (>=55%), 'neg' (<50%), '' otherwise.
    None -> '' (neutral)."""
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return ""
    if p >= 0.55:
        return "pos"
    if p < 0.50:
        return "neg"
    return ""


def _record(rec: dict | None) -> str:
    """{'wins':15,'losses':8} -> '15-8'."""
    rec = rec or {}
    return f"{int(rec.get('wins') or 0)}-{int(rec.get('losses') or 0)}"


# ── settlement lookup (mirrors pages/home.py) ───────────────────────────────

def _pick_result_index(Ledger) -> dict:
    """{(game_id, bet_type): history_row} from both ledger files, so a card's
    settlement state is one dict lookup.  Empty dict on any read error."""
    out: dict = {}
    if Ledger is None:
        return out
    for path in ("data/ledger.json", "data/wnba_ledger.json"):
        try:
            led = Ledger(path=path, starting_bankroll=1000.0)
        except Exception:                                                 # noqa: BLE001
            continue
        for h in (led.data.get("history") or []):
            gid = h.get("game_id")
            if not gid:
                continue
            bt = h.get("bet_type") or "single"
            out[(str(gid), str(bt))] = h
    return out


def _result_for_row(r: dict, result_index: dict) -> tuple[str, float, float]:
    """(result, pnl, stake) for an EV/Confidence row, or ('', 0, 0) when the
    pick has no ledger row (didn't make the daily top-5) / isn't settled."""
    gid = str(r.get("game_id") or "")
    if not gid:
        return ("", 0.0, 0.0)
    bt = (r.get("bet_type") or "single").lower()
    hist = result_index.get((gid, bt))
    if hist is None:
        return ("", 0.0, 0.0)
    return (
        (hist.get("result") or "").lower(),
        float(hist.get("model_pnl") or 0.0),
        float(hist.get("model_amount") or 0.0),
    )


# ── data gathering ──────────────────────────────────────────────────────────

def _sport_arg() -> str:
    sport = (request.args.get("sport") or "mlb").lower()
    return sport if sport in _SPORTS else "mlb"


def _collect_games(sport, read_daily_snapshot, snapshot_is_today) -> list:
    """Today's serialized games from the locked daily snapshot, tagged with
    _sport.  Filtered to `sport` when given.  [] on any failure."""
    games: list = []
    try:
        snap = read_daily_snapshot()
        if not snapshot_is_today(snap):
            return []
        for sp in _SPORTS:
            if sport and sp != sport:
                continue
            for g in ((snap.get(sp) or {}).get("results") or []):
                row = dict(g)
                row.setdefault("_sport", sp)
                games.append(row)
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
    return games


def _view_pick(r: dict, result_index: dict) -> dict:
    """Flatten one enumerate_value_picks row into a template view-model,
    including settlement state (result / pnl / stake) and the trackBet kind."""
    edge = float(r.get("edge") or 0)
    prob = float(r.get("prob") or 0)
    result, pnl, stake = _result_for_row(r, result_index)
    return {
        "matchup":        r.get("matchup", ""),
        "pick":           r.get("pick", ""),
        "edge_pct":       round(edge * 100, 1),
        "confidence_pct": round(prob * 100),
        "odds":           r.get("odds"),
        "sport":          (r.get("sport") or "mlb").lower(),
        "game_id":        r.get("game_id"),
        "bet_type":       r.get("bet_type", "single"),  # backend token for trackPick
        # settlement
        "result":         result,                       # 'win'/'loss'/'push'/''
        "settled":        result in ("win", "loss"),
        "pnl":            round(pnl, 2),
        "stake":          round(stake, 2),
    }


def _ev_picks(games, home_stats, result_index, *, min_edge=0.05, limit=15) -> list:
    if home_stats is None:
        return []
    try:
        rows = home_stats.enumerate_value_picks(games, min_edge=min_edge)
        rows.sort(key=lambda r: float(r.get("edge") or 0), reverse=True)
        return [_view_pick(r, result_index) for r in rows[:limit]]
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
        return []


def _confidence_picks(games, home_stats, result_index, *, limit=10) -> list:
    if home_stats is None:
        return []
    try:
        rows = home_stats.enumerate_value_picks(games, min_edge=0.0001)
        rows.sort(key=lambda r: float(r.get("prob") or 0), reverse=True)
        return [_view_pick(r, result_index) for r in rows[:limit]]
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
        return []


def _news(sport, news_feed, *, max_items=10) -> list:
    if news_feed is None:
        return []
    try:
        return news_feed.fetch(sport, max_items=max_items)
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
        return []


def _stat_chips(home_stats) -> list:
    """The four top-of-home model-record chips.  All read model_picks via
    home_stats (which ignores its `backend` arg), so backend=None is safe.
    Every chip degrades to a neutral '—' when data / the import is missing."""
    if home_stats is None:
        return []
    try:
        overall   = home_stats.overall_record(None) or {}
        props     = home_stats.props_record(None) or {}
        best_game = home_stats.best_classifier(None)      # {model,pct} | None
        best_prop = home_stats.best_bet_type(None)        # {label,pct} | None
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
        return []

    chips = [
        {"label": "GAME MODELS", "main": _record(overall),
         "sub": _pct_str(overall.get("pct")), "tone": _tone(overall.get("pct"))},
        {"label": "PROPS MODELS", "main": _record(props),
         "sub": _pct_str(props.get("pct")), "tone": _tone(props.get("pct"))},
    ]
    if best_game:
        chips.append({
            "label": "BEST GAME MODEL",
            "main": str(best_game.get("model") or "—").upper(),
            "sub": _pct_str(best_game.get("pct")),
            "tone": _tone(best_game.get("pct")),
        })
    else:
        chips.append({"label": "BEST GAME MODEL", "main": "—",
                      "sub": "Insufficient data", "tone": ""})
    if best_prop:
        chips.append({
            "label": "BEST PROP MODEL",
            "main": str(best_prop.get("label") or "—").title(),
            "sub": _pct_str(best_prop.get("pct")),
            "tone": _tone(best_prop.get("pct")),
        })
    else:
        chips.append({"label": "BEST PROP MODEL", "main": "—",
                      "sub": "Insufficient data", "tone": ""})
    return chips


def _model_perf(home_stats) -> dict:
    """Bottom-of-home Model Performance row: combined model W/L + win%."""
    if home_stats is None:
        return {}
    try:
        mp = home_stats.model_performance(None) or {}
    except Exception as exc:                                              # noqa: BLE001
        logging.warning("Suppressed exception in %s: %s", __name__, exc)
        return {}
    return {
        "record": _record(mp),
        "pct_str": _pct_str(mp.get("pct")),
        "tone": _tone(mp.get("pct")),
        "settled": (int(mp.get("wins") or 0) + int(mp.get("losses") or 0)) > 0,
    }


# ── registration ────────────────────────────────────────────────────────────

def register(app, *, read_daily_snapshot, snapshot_is_today,
             home_stats, news_feed, Ledger):
    """Wire "/" and "/home-v2" onto `app`.  Dependencies are injected so this
    module never imports app.py."""

    def _render(sport: str):
        games = _collect_games(sport, read_daily_snapshot, snapshot_is_today)
        result_index = _pick_result_index(Ledger)
        return render_template(
            "index.html",
            home_v2=True,
            sport=sport,
            ev_picks=_ev_picks(games, home_stats, result_index),
            confidence_picks=_confidence_picks(games, home_stats, result_index),
            news_items=_news(sport, news_feed),
            stat_chips=_stat_chips(home_stats),
            model_perf=_model_perf(home_stats),
        )

    @app.route("/")
    def index():
        return _render(_sport_arg())

    @app.route("/home-v2")
    def home_v2():
        return _render(_sport_arg())
