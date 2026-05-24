"""
model_picks.py
==============
Per-model performance tracking.  Logs every individual model's pick (plus
the ensemble + the user-facing consensus) to the Supabase ``model_picks``
table whenever the models run, settles pending picks against final scores /
stat lines in the 15-minute cycle, and aggregates a per-model W/L table for
the admin page.

Best-effort throughout: when Supabase isn't configured every function is a
safe no-op, and a malformed analysis row is skipped rather than raised.

Deterministic ``pick_id`` (date|sport|game-or-prop|model|pick_type) means a
pick logged twice the same day is a no-op insert, so a settled result is
never overwritten.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

_ET = ZoneInfo("America/New_York")

# Market -> rolling-stat key (for naming regressor models + settling props).
_MARKET_STAT = {
    "pitcher_strikeouts": "K", "pitcher_earned_runs": "ER",
    "pitcher_hits_allowed": "H", "pitcher_walks": "BB", "pitcher_outs": "outs",
    "batter_hits": "H", "batter_total_bases": "TB", "batter_home_runs": "HR",
    "batter_rbis": "RBI", "batter_runs_scored": "R", "batter_walks": "BB",
    "batter_strikeouts": "SO",
}


def _log(msg: str) -> None:
    print(f"MODEL-PICKS: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(date, sport, ref, model, pick_type, side, *, odds=None,
         confidence=None, projected=None, line=None, prop_id=None) -> dict:
    # pick_id is a random uuid (table PK); de-duplication is enforced by the
    # UNIQUE (date, game_id, model_name, pick_type) constraint via upsert, so
    # logging the same pick twice a day is a no-op.  *ref* fills game_id (a
    # real game id for games, or the player|market|line key for props, so the
    # constraint distinguishes props too); prop_id additionally carries it.
    return {
        "pick_id":         str(uuid.uuid4()),
        "date":            date,
        "sport":           sport,
        "game_id":         ref,
        "prop_id":         prop_id,
        "model_name":      model,
        "pick_type":       pick_type,
        "pick_side":       side,
        "odds":            int(odds) if isinstance(odds, (int, float)) else None,
        "confidence":      round(float(confidence), 4) if isinstance(confidence, (int, float)) else None,
        "projected_value": round(float(projected), 3) if isinstance(projected, (int, float)) else None,
        "line":            float(line) if isinstance(line, (int, float)) else None,
        "result":          "pending",
        "settled_at":      None,
        "created_at":      _now_iso(),
    }


# ── Build rows from analysis results ─────────────────────────────────────────

def _ml_side(prob):
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None, None
    return ("home", p) if p >= 0.5 else ("away", 1.0 - p)


def game_rows(r: dict, sport: str, date: str) -> list[dict]:
    """Per-model + ensemble + consensus rows for one analyzed game."""
    g = r.get("game") or {}
    gid = str(g.get("id") or g.get("game_id") or "")
    if not gid:
        return []
    rows: list[dict] = []
    h_odds = g.get("h2h_home_odds")
    a_odds = g.get("h2h_away_odds")

    # Moneyline — individual models + ensemble + consensus.
    pred = r.get("prediction") or {}
    ml_models = [("xgb", pred.get("xgb_prob")), ("lr", pred.get("lr_prob"))]
    if sport == "mlb":
        ml_models.append(("nn", pred.get("nn_prob")))
    for mname, prob in ml_models:
        side, conf = _ml_side(prob)
        if side is None:
            continue
        rows.append(_row(date, sport, gid, f"{sport}_ml_{mname}", "ML", side,
                         odds=h_odds if side == "home" else a_odds, confidence=conf))
    side, conf = _ml_side(pred.get("home_win_prob"))
    if side is not None:
        odds = h_odds if side == "home" else a_odds
        rows.append(_row(date, sport, gid, "ensemble", "ML", side, odds=odds, confidence=conf))
        rows.append(_row(date, sport, gid, "consensus", "ML", side, odds=odds, confidence=conf))

    # Run line (MLB) / spread (WNBA) — per model + ensemble + consensus.
    rl = r.get("rl_pred") or r.get("spread_pred") or {}
    rl_pt = rl.get("run_line_point") if sport == "mlb" else rl.get("spread_line")
    rl_type = "RL" if sport == "mlb" else "Spread"
    if rl:
        rl_models = [("xgb", rl.get("xgb_prob")), ("lr", rl.get("lr_prob"))]
        if rl.get("nn_prob") is not None:
            rl_models.append(("nn", rl.get("nn_prob")))
        for mname, prob in rl_models:
            side, conf = _ml_side(prob)            # prob = P(home covers)
            if side is None:
                continue
            rows.append(_row(date, sport, gid, f"{sport}_rl_{mname}", rl_type, side,
                             odds=rl.get("pick_odds"), confidence=conf, line=rl_pt))
        # ensemble/consensus run-line pick (the side shown to the user)
        cside = rl.get("side") or ("home" if str(rl.get("pick_team", "")) == str(g.get("home_team")) else "away")
        if cside in ("home", "away"):
            for m in ("ensemble", "consensus"):
                rows.append(_row(date, sport, gid, m, rl_type, cside,
                                 odds=rl.get("pick_odds"),
                                 confidence=rl.get("pick_prob"), line=rl_pt))

    # Totals — per model (XGB, NN) + ensemble + consensus.
    tot = r.get("totals_pred") or {}
    line = tot.get("total_line")
    if tot and isinstance(line, (int, float)):
        for mname, pv in (("xgb", tot.get("xgb_pred")), ("nn", tot.get("nn_pred"))):
            if not isinstance(pv, (int, float)):
                continue
            side = "over" if pv > line else "under"
            rows.append(_row(date, sport, gid, f"{sport}_total_{mname}", "Total", side,
                             projected=pv, line=line))
        cdir = tot.get("direction")
        if cdir in ("over", "under"):
            for m in ("ensemble", "consensus"):
                rows.append(_row(date, sport, gid, m, "Total", cdir,
                                 confidence=tot.get("pick_prob"),
                                 projected=tot.get("predicted_total"), line=line))
    return rows


def prop_rows(p: dict, date: str) -> list[dict]:
    """Classifier + regressor + consensus rows for one scored prop pick."""
    market = p.get("market") or ""
    bucket = p.get("bucket") or ("pitcher" if market.startswith("pitcher_") else "batter")
    player = (p.get("player") or "").strip()
    line = p.get("line")
    side = (p.get("side") or "Over").title()
    if not player or not market:
        return []
    ref = f"{player}|{market}|{line}"
    sport = "mlb"
    conf = p.get("model_prob") or p.get("confidence")
    pv = p.get("predicted_value")
    rows = [
        _row(date, sport, ref, f"props_{bucket}_classifier", market, side,
             odds=p.get("best_odds"), confidence=conf, line=line, prop_id=ref),
    ]
    stat = _MARKET_STAT.get(market, market)
    if isinstance(pv, (int, float)) and isinstance(line, (int, float)):
        reg_side = "Over" if pv > line else "Under"
        rows.append(_row(date, sport, ref, f"props_{bucket}_reg_{stat}", market,
                         reg_side, projected=pv, line=line, prop_id=ref))
    rows.append(_row(date, sport, ref, "consensus", market, side,
                     odds=p.get("best_odds"), confidence=conf, projected=pv,
                     line=line, prop_id=ref))
    return rows


# ── Logging ──────────────────────────────────────────────────────────────────

def log_games(results: list, sport: str, date: Optional[str] = None) -> int:
    date = date or _today_et()
    rows: list[dict] = []
    for r in (results or []):
        try:
            rows.extend(game_rows(r, sport, date))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"game_rows failed: {exc}")
    return _insert(rows)


def log_props(picks: list, date: Optional[str] = None) -> int:
    date = date or _today_et()
    rows: list[dict] = []
    for p in (picks or []):
        try:
            rows.extend(prop_rows(p, date))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"prop_rows failed: {exc}")
    return _insert(rows)


def _insert(rows: list[dict]) -> int:
    if not rows:
        return 0
    try:
        from . import db
        return db.model_picks_insert(rows)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"insert failed: {exc}")
        return 0


def log_all(backend) -> dict:
    """Log every model's current picks for both sports + props.  Deduped, so
    safe to call on every analysis run / 15-minute cycle."""
    out = {"game": 0, "props": 0}
    try:
        for sport, attr in (("mlb", "_analysis_state"), ("wnba", "_wnba_analysis_state")):
            state = getattr(backend, attr, {}) or {}
            out["game"] += log_games(state.get("results") or [], sport)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"log_all games failed: {exc}")
    try:
        from .props_scored_cache import load_scored_props
        picks = (load_scored_props() or {}).get("picks") or []
        out["props"] = log_props(picks)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"log_all props failed: {exc}")
    if out["game"] or out["props"]:
        _log(f"logged picks -> game rows:{out['game']} prop rows:{out['props']}")
    return out


# ── Settlement ───────────────────────────────────────────────────────────────

def _grade_game(pick: dict, hs: int, as_: int) -> Optional[str]:
    """correct/incorrect/void for a game pick given final home/away scores."""
    side = pick.get("pick_side")
    ptype = pick.get("pick_type")
    margin = hs - as_                         # home minus away
    if ptype == "ML":
        if margin == 0:
            return "void"
        won = (margin > 0) if side == "home" else (margin < 0)
        return "correct" if won else "incorrect"
    if ptype in ("RL", "Spread"):
        pt = pick.get("line")
        if not isinstance(pt, (int, float)):
            return None
        # run_line_point is the HOME line (e.g. -1.5).  Home covers when
        # margin + home_line > 0; away covers when (-margin) + (-home_line) > 0.
        cover = (margin + pt) if side == "home" else (-margin - pt)
        if abs(cover) < 1e-9:
            return "void"
        return "correct" if cover > 0 else "incorrect"
    if ptype == "Total":
        line = pick.get("line")
        if not isinstance(line, (int, float)):
            return None
        total = hs + as_
        if total == line:
            return "void"
        over = total > line
        return "correct" if (over == (side == "over")) else "incorrect"
    return None


def _grade_prop(pick: dict, actual: float) -> Optional[str]:
    line = pick.get("line")
    if not isinstance(line, (int, float)) or actual is None:
        return None
    side = (pick.get("pick_side") or "Over").lower()
    if actual == line:
        return "void"
    over = actual > line
    return "correct" if (over == (side == "over")) else "incorrect"


def settle(today: Optional[str] = None, final_scores: Optional[dict] = None,
           stat_lookup=None) -> dict:
    """Settle today's pending model picks.

    *final_scores*: {game_id: (home_score, away_score)} for finished games.
    *stat_lookup*: callable(player_name, market, date) -> actual stat (float)
    or None.  Both are best-effort; a pick with no available result stays
    pending.  Returns a per-model settled-count summary.
    """
    today = today or _today_et()
    final_scores = final_scores or {}
    try:
        from . import db
        pending = db.model_picks_list(date_from=today, result="pending")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"settle: list failed: {exc}")
        return {}
    if not pending:
        return {}

    updated: list[dict] = []
    summary: dict[str, int] = {}
    for pick in pending:
        result = None
        if pick.get("pick_type") in ("ML", "RL", "Spread", "Total"):
            sc = final_scores.get(pick.get("game_id"))
            if sc:
                result = _grade_game(pick, int(sc[0]), int(sc[1]))
        else:  # prop
            if stat_lookup is not None:
                ref = pick.get("game_id") or ""        # "player|market|line"
                player = ref.split("|")[0] if "|" in ref else ref
                try:
                    actual = stat_lookup(player, pick.get("pick_type"), today)
                except Exception:                                         # noqa: BLE001
                    actual = None
                if actual is not None:
                    result = _grade_prop(pick, actual)
        if result:
            pick["result"] = result
            pick["settled_at"] = _now_iso()
            updated.append(pick)
            summary[pick.get("model_name", "?")] = summary.get(pick.get("model_name", "?"), 0) + 1

    if updated:
        try:
            from . import db
            db.model_picks_upsert(updated)
        except Exception as exc:                                          # noqa: BLE001
            _log(f"settle: upsert failed: {exc}")
        _log("settled per model -> " + ", ".join(f"{m}:{n}" for m, n in sorted(summary.items())))
    return summary


# ── Aggregation for the admin table ──────────────────────────────────────────

def performance(since_date: Optional[str] = None) -> dict:
    """Per-model W/L/Win%/Last10/AvgConf, sorted by win% desc.  *since_date*
    filters to picks on/after that ET date (None = all-time)."""
    try:
        from . import db
        picks = db.model_picks_list(date_from=since_date)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"performance list failed: {exc}")
        picks = []

    agg: dict[tuple, dict] = {}
    for p in picks:
        key = (p.get("model_name"), p.get("sport"), p.get("pick_type"))
        a = agg.setdefault(key, {
            "model_name": p.get("model_name"), "sport": p.get("sport"),
            "pick_type": p.get("pick_type"), "wins": 0, "losses": 0,
            "_conf": [], "_recent": [],
        })
        c = p.get("confidence")
        if isinstance(c, (int, float)):
            a["_conf"].append(float(c))
        res = (p.get("result") or "pending").lower()
        if res == "correct":
            a["wins"] += 1
            a["_recent"].append((p.get("settled_at") or "", "W"))
        elif res == "incorrect":
            a["losses"] += 1
            a["_recent"].append((p.get("settled_at") or "", "L"))

    rows: list[dict] = []
    for a in agg.values():
        w, l = a["wins"], a["losses"]
        total = w + l
        recent = [x[1] for x in sorted(a["_recent"], key=lambda t: t[0])[-10:]]
        rows.append({
            "model_name": a["model_name"], "sport": a["sport"],
            "pick_type": a["pick_type"], "wins": w, "losses": l,
            "win_pct": round(w / total * 100, 1) if total else None,
            "last10": "".join(recent) or "—",
            "avg_confidence": round(sum(a["_conf"]) / len(a["_conf"]), 3) if a["_conf"] else None,
            "settled": total,
        })
    rows.sort(key=lambda r: (r["win_pct"] if r["win_pct"] is not None else -1), reverse=True)
    return {"rows": rows, "updated_at": _now_iso()}
