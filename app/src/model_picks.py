"""
model_picks.py
==============
Single per-model performance store, backed entirely by the Supabase
``model_picks`` table (PostgREST only -- no JSON trackers, no direct
Postgres).  JSON files don't survive Railway redeploys, so Supabase is the
source of truth for every record shown on the home + props pages.

One row per model pick:
  model  : 'xgb' | 'lr' | 'nn' | 'combined'  (game models / ensemble)
           'pitcher' | 'batter'              (prop models)
  bet_type: 'ml' | 'rl' | 'total'            (game models + combined)
            <prop market>                    (pitcher / batter)
  sport  : 'mlb' | 'wnba' | ...   -- the SAME six stores exist per sport
           purely by filtering on this column (no per-sport code paths).

pick_id is deterministic (sport:model:bet_type:game_id[:player_name]) and is
the upsert key, so re-running a cycle never duplicates and never overwrites a
finished result (inserts ignore existing rows).  Settlement reads pending
rows from the table, grades them, and writes status='finished'.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta, date as _date
from zoneinfo import ZoneInfo
from typing import Optional

_ET = ZoneInfo("America/New_York")


def _log(msg: str) -> None:
    print(f"MODEL-PICKS: {msg}", flush=True, file=sys.stderr)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _et_date(iso: Optional[str]) -> str:
    """ET calendar date (YYYY-MM-DD) for an ISO timestamp (created_at is
    stored UTC; the history view browses by ET day)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).date().isoformat()
    except (TypeError, ValueError):
        return str(iso)[:10]


def date_range(preset: str) -> tuple[str, str]:
    """(start_date, end_date) inclusive ET date strings for a preset:
    'today' | 'yesterday' | '7d' | '30d'.  Unknown -> today only."""
    today = _date.fromisoformat(_today_et())
    if preset == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if preset == "7d":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if preset == "30d":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    return today.isoformat(), today.isoformat()   # 'today' / default


def _pid(sport, model, bet_type, game_id, player=None) -> str:
    base = f"{sport}:{model}:{bet_type}:{game_id}"
    return f"{base}:{player}" if player else base


def _row(sport, model, bet_type, game_id, side, *, confidence=None,
         line=None, player=None) -> dict:
    return {
        "pick_id":     _pid(sport, model, bet_type, game_id, player),
        "sport":       sport,
        "model":       model,
        "bet_type":    bet_type,
        "status":      "pending",
        "pick_side":   side,
        "line":        float(line) if isinstance(line, (int, float)) else None,
        "confidence":  round(float(confidence), 4) if isinstance(confidence, (int, float)) else None,
        "result":      None,
        "game_id":     str(game_id),
        "player_name": player,
        "created_at":  _now_iso(),
        "settled_at":  None,
    }


def _conf(prob):
    """Pick-side confidence from a home-win/cover probability (max of p,1-p)."""
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return None
    return p if p >= 0.5 else 1.0 - p


# ── Build pick rows from analysis results ────────────────────────────────────

def game_rows(r: dict, sport: str) -> list[dict]:
    """xgb/lr/nn + combined rows for ml/rl/total for one analyzed game."""
    g = r.get("game") or {}
    gid = str(g.get("id") or g.get("game_id") or "")
    if not gid:
        return []
    home = g.get("home_team") or "home"
    away = g.get("away_team") or "away"
    rows: list[dict] = []

    def _team(prob):  # home-win prob -> picked team name + confidence
        try:
            p = float(prob)
        except (TypeError, ValueError):
            return None, None
        return (home if p >= 0.5 else away), (p if p >= 0.5 else 1.0 - p)

    # ── Moneyline ────────────────────────────────────────────────────────────
    pred = r.get("prediction") or {}
    for model, prob in (("xgb", pred.get("xgb_prob")), ("lr", pred.get("lr_prob")),
                        ("nn", pred.get("nn_prob"))):
        team, conf = _team(prob)
        if team:
            rows.append(_row(sport, model, "ml", gid, team, confidence=conf))
    team, conf = _team(pred.get("home_win_prob"))
    if team:
        rows.append(_row(sport, "combined", "ml", gid, team, confidence=conf))

    # ── Run line (MLB) / spread (WNBA) -- bet_type 'rl' for both ─────────────
    rl = r.get("rl_pred") or r.get("spread_pred") or {}
    home_line = rl.get("run_line_point") if sport == "mlb" else rl.get("spread_line")
    if rl:
        for model, prob in (("xgb", rl.get("xgb_prob")), ("lr", rl.get("lr_prob")),
                            ("nn", rl.get("nn_prob"))):
            try:
                p = float(prob)
            except (TypeError, ValueError):
                continue
            # prob = P(home covers); pick the side + that side's spread line.
            if p >= 0.5:
                team, ln = home, home_line
            else:
                team, ln = away, (-home_line if isinstance(home_line, (int, float)) else None)
            rows.append(_row(sport, model, "rl", gid, team,
                             confidence=p if p >= 0.5 else 1.0 - p, line=ln))
        # combined run-line pick (the side shown to the user)
        cside = rl.get("side")
        cteam = rl.get("pick_team") or (home if cside == "home" else away if cside == "away" else None)
        if cteam:
            is_home = str(cteam) == str(home) or cside == "home"
            ln = home_line if is_home else (-home_line if isinstance(home_line, (int, float)) else None)
            rows.append(_row(sport, "combined", "rl", gid, cteam,
                             confidence=rl.get("pick_prob"), line=ln))

    # ── Totals ───────────────────────────────────────────────────────────────
    tot = r.get("totals_pred") or {}
    line = tot.get("total_line")
    if tot and isinstance(line, (int, float)):
        for model, pv in (("xgb", tot.get("xgb_pred")), ("lr", tot.get("lr_pred")),
                          ("nn", tot.get("nn_pred"))):
            if not isinstance(pv, (int, float)):
                continue
            rows.append(_row(sport, model, "total", gid,
                             "OVER" if pv > line else "UNDER", line=line))
        cdir = (tot.get("direction") or "").lower()
        if cdir in ("over", "under"):
            rows.append(_row(sport, "combined", "total", gid, cdir.upper(),
                             confidence=tot.get("pick_prob"), line=line))
    return rows


def prop_rows(p: dict) -> list[dict]:
    """One pitcher/batter row for a scored prop pick.  bet_type = market,
    player_name = player; game_id ties it to the game (event id)."""
    market = p.get("market") or ""
    bucket = p.get("bucket") or ("pitcher" if market.startswith("pitcher_") else "batter")
    player = (p.get("player") or "").strip()
    if not player or not market:
        return []
    gid = str(p.get("event_id") or p.get("game_id") or f"{player}|{market}")
    side = (p.get("side") or "Over").upper()
    conf = p.get("model_prob") or p.get("confidence")
    return [_row("mlb", bucket, market, gid, side,
                 confidence=conf, line=p.get("line"), player=player)]


# ── Logging (write side) ─────────────────────────────────────────────────────

def log_games(results: list, sport: str) -> int:
    rows: list[dict] = []
    for r in (results or []):
        try:
            rows.extend(game_rows(r, sport))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"game_rows failed: {exc}")
    return _insert(rows)


def log_props(picks: list) -> int:
    rows: list[dict] = []
    for p in (picks or []):
        try:
            rows.extend(prop_rows(p))
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
    """Log every model's current picks for both sports + props.  Deduped by
    pick_id, so safe to call every analysis run / 15-minute cycle."""
    out = {"game": 0, "props": 0}
    try:
        for sport, attr in (("mlb", "_analysis_state"), ("wnba", "_wnba_analysis_state")):
            state = getattr(backend, attr, {}) or {}
            out["game"] += log_games(state.get("results") or [], sport)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"log_all games failed: {exc}")
    try:
        from .props_scored_cache import load_scored_props
        out["props"] = log_props((load_scored_props() or {}).get("picks") or [])
    except Exception as exc:                                              # noqa: BLE001
        _log(f"log_all props failed: {exc}")
    if out["game"] or out["props"]:
        _log(f"logged -> game rows:{out['game']} prop rows:{out['props']}")
    return out


# ── Noon re-check: replace beaten pending picks for unstarted games ──────────

def _better(new_conf, old_conf) -> bool:
    """True only when the noon pick is genuinely better (strictly higher
    confidence).  A new pick with no confidence is never 'better'; an old
    pick with no confidence is always beaten by one that has confidence."""
    if not isinstance(new_conf, (int, float)):
        return False
    if not isinstance(old_conf, (int, float)):
        return True
    return float(new_conf) > float(old_conf)


def reconcile_noon(backend, started_fn) -> dict:
    """Noon re-check.  For each pick the noon analysis produces:

      - game already STARTED (started_fn -> True): locked.  Never replaced or
        removed, so win/loss tracking stays honest.
      - game NOT started, no prior pending row: add it (new pick).
      - game NOT started, a pending row exists: replace it ONLY when the noon
        pick is strictly better (higher confidence) -- delete the old pending
        row and insert the new one; otherwise keep the 8 AM pick.

    A pending pick is never dropped without a replacement.  *started_fn* is
    ``started_fn(sport, dict) -> bool`` (dict carries commence_time + teams);
    pass live_score's detector so 'started' matches everywhere.  Returns a
    per-(sport, model) summary {kept, replaced, locked}.
    """
    try:
        from . import db
        existing = {r.get("pick_id"): r for r in db.model_picks_list()}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"reconcile: list failed: {exc}")
        existing = {}

    summary: dict[tuple, dict] = {}
    to_delete: list[str] = []
    to_insert: list[dict] = []

    def _acct(sport, model, key):
        summary.setdefault((sport, model),
                           {"kept": 0, "replaced": 0, "locked": 0})[key] += 1

    def _one(new: dict, started: bool) -> None:
        sport, model, pid = new["sport"], new["model"], new["pick_id"]
        if started:
            _acct(sport, model, "locked")
            return
        old = existing.get(pid)
        if old is None:
            to_insert.append(new)
            _acct(sport, model, "kept")
            return
        if (old.get("status") or "pending").lower() != "pending":
            _acct(sport, model, "locked")        # already settled -> leave it
            return
        if _better(new.get("confidence"), old.get("confidence")):
            to_delete.append(pid)
            to_insert.append(new)
            _acct(sport, model, "replaced")
        else:
            _acct(sport, model, "kept")

    for sport, attr in (("mlb", "_analysis_state"), ("wnba", "_wnba_analysis_state")):
        state = getattr(backend, attr, {}) or {}
        for r in (state.get("results") or []):
            g = r.get("game") or {}
            try:
                started = bool(started_fn(sport, g))
            except Exception:                                            # noqa: BLE001
                started = False
            for new in game_rows(r, sport):
                _one(new, started)

    try:
        from .props_scored_cache import load_scored_props
        for p in ((load_scored_props() or {}).get("picks") or []):
            try:
                started = bool(started_fn("mlb", p))
            except Exception:                                            # noqa: BLE001
                started = False
            for new in prop_rows(p):
                _one(new, started)
    except Exception as exc:                                             # noqa: BLE001
        _log(f"reconcile props failed: {exc}")

    if to_delete:
        try:
            from . import db
            db.model_picks_delete(to_delete)
        except Exception as exc:                                         # noqa: BLE001
            _log(f"reconcile delete failed: {exc}")
    if to_insert:
        _insert(to_insert)

    _log("noon reconcile -> " + "; ".join(
        f"{sp}/{m}: kept={v['kept']} replaced={v['replaced']} locked={v['locked']}"
        for (sp, m), v in sorted(summary.items())
    ))
    return summary


# ── Settlement (pending -> finished) ─────────────────────────────────────────

def _grade_game(pick: dict, sc: dict) -> Optional[str]:
    """win/loss/void for a game pick against a final score row
    {home_team, away_team, home_score, away_score}."""
    ht, at = sc.get("home_team"), sc.get("away_team")
    hs, as_ = sc.get("home_score"), sc.get("away_score")
    if not isinstance(hs, (int, float)) or not isinstance(as_, (int, float)):
        return None
    bt = pick.get("bet_type")
    side = pick.get("pick_side")
    if bt == "ml":
        if hs == as_:
            return "void"
        winner = ht if hs > as_ else at
        return "win" if side == winner else "loss"
    if bt == "rl":
        line = pick.get("line")
        if not isinstance(line, (int, float)):
            return None
        if side == ht:
            margin = hs - as_
        elif side == at:
            margin = as_ - hs
        else:
            return None
        cover = margin + line                 # line = picked team's spread
        if abs(cover) < 1e-9:
            return "void"
        return "win" if cover > 0 else "loss"
    if bt == "total":
        line = pick.get("line")
        if not isinstance(line, (int, float)):
            return None
        total = hs + as_
        if total == line:
            return "void"
        over = total > line
        return "win" if (over == (side == "OVER")) else "loss"
    return None


def _grade_prop(pick: dict, actual) -> Optional[str]:
    line = pick.get("line")
    if not isinstance(line, (int, float)) or actual is None:
        return None
    side = (pick.get("pick_side") or "OVER").upper()
    if actual == line:
        return "void"
    return "win" if ((actual > line) == (side == "OVER")) else "loss"


def settle(final_scores: Optional[dict] = None, stat_lookup=None) -> dict:
    """Move pending picks whose game has finished to status='finished' with a
    graded result.  *final_scores*: {game_id: {home_team, away_team,
    home_score, away_score}}.  *stat_lookup*: callable(player, market) ->
    actual stat or None.  Returns {model_name: settled_count}.

    Selects unsettled rows by RESULT being empty (not by status) and across
    ALL dates, so rows that somehow lost their 'pending' status -- or were
    logged on a prior day -- are never stranded.  Always emits a SETTLE-RESULT
    log line (even on 0 matches) so the cause is visible in Railway logs."""
    final_scores = final_scores or {}
    try:
        from . import db
        all_rows = db.model_picks_list()
    except Exception as exc:                                              # noqa: BLE001
        _log(f"settle: list failed: {exc}")
        return {}

    pending = [r for r in (all_rows or [])
               if not (r.get("result") or "").strip()]
    if not pending:
        _log(f"SETTLE-RESULT: checked=0 unsettled (total rows={len(all_rows or [])}) "
             f"-- nothing to settle")
        return {}

    updated: list[dict] = []
    summary: dict[str, int] = {}
    g_checked = g_found = g_graded = p_checked = p_graded = 0
    unmatched_gids: list[str] = []
    for pick in pending:
        result = None
        if pick.get("player_name"):                       # prop
            p_checked += 1
            if stat_lookup is not None:
                # The pick's ET slate date (from created_at) tells the lookup
                # which season + game date to grade against, so a backlog of
                # props from earlier days / a 2026 season settles instead of
                # only matching games dated "today".
                pick_date = _et_date(pick.get("created_at")) or None
                try:
                    actual = stat_lookup(pick.get("player_name"),
                                         pick.get("bet_type"), pick_date)
                except TypeError:
                    # A stat_lookup that doesn't accept the date arg (older
                    # callers) -- fall back to the 2-arg form.
                    actual = stat_lookup(pick.get("player_name"),
                                         pick.get("bet_type"))
                except Exception:                                         # noqa: BLE001
                    actual = None
                if actual is not None:
                    result = _grade_prop(pick, actual)
            if result:
                p_graded += 1
        else:                                             # game
            g_checked += 1
            gid = str(pick.get("game_id") or "").strip()
            # Normalised lookup first, then the raw stored key as a fallback.
            sc = final_scores.get(gid) or final_scores.get(pick.get("game_id"))
            if sc:
                g_found += 1
                result = _grade_game(pick, sc)
                if result:
                    g_graded += 1
            elif len(unmatched_gids) < 5:
                unmatched_gids.append(gid)
        if result:
            pick["result"] = result
            pick["status"] = "finished"
            pick["settled_at"] = _now_iso()
            updated.append(pick)
            summary[pick.get("model", "?")] = summary.get(pick.get("model", "?"), 0) + 1

    if updated:
        try:
            from . import db
            db.model_picks_upsert(updated)
        except Exception as exc:                                          # noqa: BLE001
            _log(f"settle: upsert failed: {exc}")
        _log("settled per model -> " + ", ".join(f"{m}:{n}" for m, n in sorted(summary.items())))

    # ALWAYS-on result line so a 0-settle pass is diagnosable in Railway logs.
    _log(
        f"SETTLE-RESULT: checked={len(pending)} (games={g_checked} props={p_checked}) | "
        f"final_scores={len(final_scores)} | "
        f"game_id_matched={g_found} games_graded={g_graded} props_graded={p_graded} | "
        f"settled={len(updated)}"
    )
    # Targeted hints when a whole class fails to match -- these pinpoint the
    # root cause without flooding the log with one line per pick.
    if g_checked and g_found == 0 and final_scores:
        _log(f"SETTLE-DEBUG: NO game_id matched a score id -- "
             f"sample pick game_ids={unmatched_gids} | "
             f"sample score ids={list(final_scores)[:5]}")
    if p_checked and p_graded == 0:
        _log("SETTLE-DEBUG: NO prop graded -- stat_lookup returned None for every "
             "prop (player game-log not found, or no game on the pick's slate date "
             "in that season; see the per-pick MODEL-PICKS: STAT-LOOKUP lines for "
             "the exact season+date queried and what was returned).")
    return summary


# ── Aggregation (read side) ──────────────────────────────────────────────────

def _all() -> list[dict]:
    try:
        from . import db
        return db.model_picks_list()
    except Exception:                                                     # noqa: BLE001
        return []


def _tally(rows) -> dict:
    w = sum(1 for r in rows if (r.get("result") or "").lower() == "win")
    l = sum(1 for r in rows if (r.get("result") or "").lower() == "loss")
    total = w + l
    return {"wins": w, "losses": l, "pct": (w / total) if total else None}


def store_record(sport: str, model: str, bet_type: Optional[str] = None,
                 rows: Optional[list] = None) -> dict:
    """Finished W/L/pct for one store (sport+model[+bet_type])."""
    rows = rows if rows is not None else _all()
    sel = [
        r for r in rows
        if r.get("sport") == sport and r.get("model") == model
        and (bet_type is None or r.get("bet_type") == bet_type)
        and (r.get("status") or "").lower() == "finished"
    ]
    return _tally(sel)


def models_record(sport: str, models: list, rows: Optional[list] = None) -> dict:
    """Finished W/L/pct aggregated across several models for a sport."""
    rows = rows if rows is not None else _all()
    sel = [
        r for r in rows
        if r.get("sport") == sport and r.get("model") in models
        and (r.get("status") or "").lower() == "finished"
    ]
    return _tally(sel)


_PRETTY_GAME = {"xgb": "XGBoost", "lr": "Logistic Regression", "nn": "Neural Net"}


def best_game_model(sport: str = "mlb", min_settled: int = 1,
                    rows: Optional[list] = None) -> Optional[dict]:
    """The xgb/lr/nn model with the highest finished win% for *sport*."""
    rows = rows if rows is not None else _all()
    best = None
    for m in ("xgb", "lr", "nn"):
        rec = store_record(sport, m, rows=rows)
        total = rec["wins"] + rec["losses"]
        if total < min_settled or rec["pct"] is None:
            continue
        cand = {"model": _PRETTY_GAME[m], "wins": rec["wins"],
                "losses": rec["losses"], "total": total,
                "correct": rec["wins"], "pct": rec["pct"]}
        if best is None or cand["pct"] > best["pct"]:
            best = cand
    return best


def best_prop_model(sport: str = "mlb", min_settled: int = 1,
                    rows: Optional[list] = None) -> Optional[dict]:
    """Pitcher vs batter -- whichever has the higher finished win%."""
    rows = rows if rows is not None else _all()
    best = None
    for m, label in (("pitcher", "Pitcher"), ("batter", "Batter")):
        rec = store_record(sport, m, rows=rows)
        total = rec["wins"] + rec["losses"]
        if total < min_settled or rec["pct"] is None:
            continue
        cand = {"label": label, "wins": rec["wins"], "losses": rec["losses"],
                "pct": rec["pct"]}
        if best is None or cand["pct"] > best["pct"]:
            best = cand
    return best


def prop_records(sport: str = "mlb", rows: Optional[list] = None) -> dict:
    """{'pitcher': {...}, 'batter': {...}} finished records for the props page."""
    rows = rows if rows is not None else _all()
    return {
        "pitcher": store_record(sport, "pitcher", rows=rows),
        "batter":  store_record(sport, "batter", rows=rows),
    }


def _in_range(row: dict, start: Optional[str], end: Optional[str]) -> bool:
    """True when the row's ET created_at date is within [start, end] (each
    inclusive; None = unbounded on that side)."""
    if not start and not end:
        return True
    d = _et_date(row.get("created_at"))
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def history(sport: str, model: str, start_date: str, end_date: str) -> dict:
    """One model store's picks for an ET date range.  Returns
    ``{"record": {wins, losses, voids, pct}, "picks": [rows newest-first]}``.
    Pending picks are included in ``picks`` but excluded from the record."""
    try:
        from . import db
        rows = db.model_picks_list(sport=sport, model=model)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"history list failed: {exc}")
        rows = []
    rows = [r for r in rows if _in_range(r, start_date, end_date)]
    rows.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)   # newest first

    fin = [r for r in rows if (r.get("status") or "").lower() == "finished"]
    w = sum(1 for r in fin if (r.get("result") or "").lower() == "win")
    l = sum(1 for r in fin if (r.get("result") or "").lower() == "loss")
    v = sum(1 for r in fin if (r.get("result") or "").lower() == "void")
    total = w + l
    return {
        "record": {"wins": w, "losses": l, "voids": v,
                   "pct": (w / total) if total else None},
        "picks": rows,
    }


def performance(since_date: Optional[str] = None,
                until_date: Optional[str] = None) -> dict:
    """Per-(sport, model, bet_type) table for the admin Model Performance
    section: W / L / Win% / Last 10 / Avg Confidence, sorted by win% desc.
    *since_date* / *until_date* (YYYY-MM-DD ET) bound created_at when given."""
    rows = _all()
    if since_date or until_date:
        rows = [r for r in rows if _in_range(r, since_date, until_date)]
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("model"), r.get("sport"), r.get("bet_type"))
        a = agg.setdefault(key, {"model_name": r.get("model"), "sport": r.get("sport"),
                                 "pick_type": r.get("bet_type"), "wins": 0,
                                 "losses": 0, "_conf": [], "_recent": []})
        c = r.get("confidence")
        if isinstance(c, (int, float)):
            a["_conf"].append(float(c))
        res = (r.get("result") or "").lower()
        if res == "win":
            a["wins"] += 1
            a["_recent"].append((r.get("settled_at") or "", "W"))
        elif res == "loss":
            a["losses"] += 1
            a["_recent"].append((r.get("settled_at") or "", "L"))
    out: list[dict] = []
    for a in agg.values():
        w, l = a["wins"], a["losses"]
        total = w + l
        recent = [x[1] for x in sorted(a["_recent"], key=lambda t: t[0])[-10:]]
        out.append({
            "model_name": a["model_name"], "sport": a["sport"],
            "pick_type": a["pick_type"], "wins": w, "losses": l,
            "win_pct": round(w / total * 100, 1) if total else None,
            "last10": "".join(recent) or "—",
            "avg_confidence": round(sum(a["_conf"]) / len(a["_conf"]), 3) if a["_conf"] else None,
            "settled": total,
        })
    out.sort(key=lambda r: (r["win_pct"] if r["win_pct"] is not None else -1), reverse=True)
    return {"rows": out, "updated_at": _now_iso()}


def store_summary_counts(rows: Optional[list] = None) -> list[str]:
    """Per (sport, model) lines for the SETTLE-SUMMARY log: pending + W/L."""
    rows = rows if rows is not None else _all()
    keys: dict[tuple, dict] = {}
    for r in rows:
        k = (r.get("sport"), r.get("model"))
        a = keys.setdefault(k, {"pending": 0, "w": 0, "l": 0})
        st = (r.get("status") or "").lower()
        if st == "pending":
            a["pending"] += 1
        elif st == "finished":
            res = (r.get("result") or "").lower()
            if res == "win":
                a["w"] += 1
            elif res == "loss":
                a["l"] += 1
    out = []
    for (sport, model), a in sorted(keys.items(), key=lambda x: (x[0][0] or "", x[0][1] or "")):
        out.append(f"{sport}/{model}: pending={a['pending']} {a['w']}W-{a['l']}L")
    return out
