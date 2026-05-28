"""scheduler.py -- APScheduler job functions (PR #277 starter).

This module is the home for the APScheduler-driven background jobs.
Only the genuinely-cleanly-movable pieces live here today; the
remaining 7 job functions and the APScheduler bootstrap block are
documented in migration_log.txt under "deferred until prerequisite
extractions land" -- they have hard dependencies on app.py-private
helpers (Flask `app.test_client()`, `hydrate_state`, `_log_model_picks`,
`_supabase_cache_delete`, etc.) that the strict move-only rule
forbids us from following without rewriting call signatures.

The names this module exports are imported star-style by app.py:
    from scheduler import *

Direction:
    scheduler.py -> state.py, utils.py, src.*    (one-way down)
    app.py       -> scheduler.py                 (one-way down)

Never import app.py from here; that would create a cycle.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import urllib.request as _urlreq
from datetime import datetime, timedelta, timezone

# Credential redactor used by _eprint to keep API keys out of Railway
# logs even when an exception message embeds `?apiKey=...`.
from src.redact import redact as _redact

# State + utils star-imports: the cluster appended below in PR #278a
# (hydrate_state, _read_daily_snapshot, etc.) references _analysis_state,
# _SNAPSHOT_ENABLED, _today_et, _filter_stale_games, etc.  Star-importing
# keeps the call sites verbatim (zero rewrites) and the direction stays
# one-way down: scheduler.py -> state.py / utils.py / src.*.
from state import *  # noqa: F401,F403
from utils import *  # noqa: F401,F403

# Parallel reference to the same logger app.py owns.  Python's logging
# module is a process-wide registry keyed by name, so getLogger("sports_betting")
# returns the same singleton both modules use -- this is NOT a duplicate
# logger, it's a second name bound to the same object.  We need this here
# because _debug_print (moved in PR #279) references `_logger.debug(...)`
# verbatim, and the "move-only, no rewrites" rule forbids editing the
# call site.  Decision documented in migration_log.txt under PR #279.
_logger = logging.getLogger("sports_betting")

__all__ = [
    "_eprint",
    "_run_meta_consensus_job",
    "_run_personal_daily_limit_refresh",
    # PR #278a -- hydrate_state + _log_model_picks cluster
    "_read_daily_snapshot",
    "_snapshot_is_today",
    "hydrate_state",
    "_log_model_picks",
    # PR #279 -- settlement bridge + overnight AI + budget helpers
    "_today_et_str",
    "_debug_print",
    "_fetch_mlb_linescore_raw",
    "_persist_daily_budget",
    "_run_overnight_ai_gen",
    "_void_postponed_mlb_bets",
    "_completed_games_from_scores",
    "_grade_model_trackers",
    "_statsapi_pick_et_date",
    "_statsapi_date_window",
    "_statsapi_schedule_index",
    "_fetch_mlb_statsapi_scores",
]

# moved from app.py:613
def _eprint(*args, **kwargs) -> None:
    """Safe stderr print that never raises.

    Encodes with UTF-8 + errors='replace' so box-drawing chars, emoji, and any
    non-cp1252 characters can't crash the crash-handler on Windows terminals.
    Runs every message through the credential redactor so an HTTPError that
    embeds `?apiKey=...` in its message can't leak the key into Railway logs.
    Falls back to a no-op if stderr itself is unavailable.
    """
    try:
        msg = " ".join(_redact(a) for a in args) + kwargs.get("end", "\n")
        buf = getattr(sys.stderr, "buffer", None)
        if buf is not None:
            buf.write(msg.encode("utf-8", errors="replace"))
            buf.flush()
        else:
            sys.stderr.write(msg)
            sys.stderr.flush()
    except Exception:
        pass  # last resort — never let logging kill the app

# moved from app.py:9501
def _run_meta_consensus_job() -> dict:
    """APScheduler 8:30 AM ET job: one batched compound-beta review of today's
    scored props -> meta_consensus_today cache.  Best-effort; never raises."""
    try:
        from services import meta_consensus
        res = meta_consensus.run_meta_consensus()
        _eprint(
            f"META-CONSENSUS: done -- parsed={res.get('parsed', 0)}/"
            f"{res.get('prop_count', 0)} (model={res.get('model')})"
        )
        return res
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"META-CONSENSUS: job failed: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}")
        return {"error": f"{type(exc).__name__}: {exc}"}

# moved from app.py:9691
def _run_personal_daily_limit_refresh() -> None:
    """4 AM ET: take a fresh My Bets daily-limit snapshot off the current
    personal bankroll that morning (higher if the bankroll grew, lower if
    it shrank).  Sizes NEW bets only -- never an already-placed stake."""
    try:
        from src import supa_ledger as _sl
        limit = _sl.personal().refresh_daily_limit()
        _eprint(f"DAILY-LIMIT [personal]: refreshed to ${limit:.2f} "
                f"(20% of current bankroll)")
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"DAILY-LIMIT refresh failed: {type(exc).__name__}: {exc}")

# moved from app.py:722
def _read_daily_snapshot() -> dict:
    """Read daily snapshot file; return {} on any error.  Thread-safe."""
    if not _SNAPSHOT_ENABLED:
        return {}
    with _snapshot_lock:
        try:
            if not _DAILY_SNAPSHOT_FILE.exists():
                return {}
            raw = _DAILY_SNAPSHOT_FILE.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)
        except Exception as _e:
            print(f"SNAPSHOT read error (ignored): {_e}", flush=True, file=sys.stderr)
            return {}

# moved from app.py:739
def _snapshot_is_today(snap: dict) -> bool:
    """True if snapshot's date equals today in Eastern time."""
    if not _SNAPSHOT_ENABLED:
        return False
    try:
        return bool(snap) and snap.get("date") == _today_et()
    except Exception:
        return False

# moved from app.py:749
def hydrate_state() -> tuple[int, int]:
    """Re-read today's analysis from disk and seed the in-memory
    _analysis_state / _wnba_analysis_state dicts.

    Call this at the start of every page render so:
      - cold containers (post-deploy) immediately have today's picks
      - any path that wrote to the cache files (scheduler, manual
        Run, external tool) is visible to the UI on the next page
        load WITHOUT requiring app restart
      - the in-memory dicts always reflect whichever cache file is
        newest on disk -- no stale-Python-state-vs-served-render skew

    Idempotent + safe to call concurrently.  Source-of-truth order:
      1. data/daily_snapshot.json (atomic write-once per ET day)
      2. data/analysis_cache.json / data/wnba_analysis_cache.json
         (legacy per-sport caches, written by /api/analyze)
      3. nothing -- leave state as-is, return zeros

    Returns (mlb_count, wnba_count) for caller logging."""

    try:
        snap = _read_daily_snapshot()
        is_today = _snapshot_is_today(snap)
    except Exception as exc:                                              # noqa: BLE001
        print(f"hydrate_state: snapshot read failed: {exc}",
              flush=True, file=sys.stderr)
        snap, is_today = {}, False

    import json as _json
    from pathlib import Path as _Path

    def _seed(state_dict, sport_key: str, cache_path: str) -> int:
        sp = (snap.get(sport_key) or {}) if is_today else {}
        results = sp.get("results")
        analyzed_at = sp.get("analyzed_at")

        if not results:
            try:
                p = _Path(cache_path)
                if p.exists():
                    payload = _json.loads(p.read_text(encoding="utf-8"))
                    if payload.get("date") == _today_et():
                        results = _filter_stale_games(
                            payload.get("results") or []
                        )
                        analyzed_at = payload.get("analyzed_at") or analyzed_at
            except Exception as exc:                                      # noqa: BLE001
                print(f"hydrate_state: {sport_key} cache read failed: {exc}",
                      flush=True, file=sys.stderr)

        if not results:
            return 0

        # Replace the list -- crucial that we assign a fresh list rather
        # than mutate in place, so any UI render that captured the old
        # results reference sees an empty view and the page's own
        # state_dict["results"] read on next render gets the new list.
        state_dict["results"] = list(results)
        if analyzed_at:
            try:
                state_dict["last_analyzed_at"] = datetime.fromisoformat(analyzed_at)
            except Exception:                                             # noqa: BLE001
                pass
        return len(results)

    mlb_n  = _seed(_analysis_state,      "mlb",  "data/analysis_cache.json")
    wnba_n = _seed(_wnba_analysis_state, "wnba", "data/wnba_analysis_cache.json")
    return mlb_n, wnba_n

# moved from app.py:10198
def _log_model_picks() -> None:
    """Log every individual model's current picks (+ ensemble + consensus)
    to the model_picks table.  Deduped per model/game/day, so safe to call
    on every analysis run and every 15-minute cycle (PART 1/2)."""
    try:
        from src import model_picks as _mp
        _mp.log_games(_analysis_state.get("results") or [], "mlb")
        _mp.log_games(_wnba_analysis_state.get("results") or [], "wnba")
        from src.props_scored_cache import load_scored_props
        _scored = (load_scored_props() or {}).get("picks") or []
        _mp.log_props(_scored)
        # Forward-only research history: freeze each scored prop's AI model +
        # edge + odds now (while the per-day caches are still warm) so the
        # /research leaderboard can attribute settled results later.
        try:
            from src import research_store as _rst
            _rst.record(_scored)
        except Exception as _rx:                                            # noqa: BLE001
            _eprint(f"RESEARCH-STORE: record failed: {type(_rx).__name__}: {_rx}")
    except Exception as exc:                                               # noqa: BLE001
        _eprint(f"MODEL-PICKS: log failed: {type(exc).__name__}: {exc}")

# moved from app.py:3217
def _debug_print(msg: str) -> None:
    """Print to stdout and append to log file with timestamp.  Messages
    are redacted so a leaked URL or env-var secret can't end up in the
    debug log file or stdout."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {_redact(msg)}"
    _logger.debug("%s", line)
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)

# moved from app.py:3231
def _today_et_str() -> str:
    """Return today's date in America/New_York as YYYY-MM-DD."""
    try:
        # zoneinfo is stdlib in Python 3.9+
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        # fallback: UTC offset -5 (close enough for date purposes)
        return (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d")

# moved from app.py:3242
def _fetch_mlb_linescore_raw(date_str: str) -> dict:
    """
    Direct fetch from MLB Stats API (bypasses in-memory cache).
    Returns {gamePk: game_dict} for every game on date_str.
    """
    import urllib.request as _urlreq
    import urllib.error  as _urlerr
    url = (f"{_MLB_STATS_BASE}/schedule"
           f"?sportId=1&date={date_str}&hydrate=linescore")
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SportsBettingApp/1.0"})
        with _urlreq.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        live_map: dict = {}
        for d in data.get("dates", []):
            for g in d.get("games", []):
                live_map[int(g["gamePk"])] = g
        return live_map
    except _urlerr.URLError as exc:
        _debug_print(f"[live-debug] URLError fetching MLB Stats: {exc}")
        return {}
    except Exception as exc:
        _debug_print(f"[live-debug] Error fetching MLB Stats: {exc}")
        return {}

# moved from app.py:7150
def _persist_daily_budget(bankroll: float) -> dict:
    """Recompute the daily budget off *bankroll* and persist it for today
    (ET) so the My Bets banner + budget gate reflect a bankroll change
    immediately.  Returns the budget dict."""
    from src.ledger import compute_daily_budget
    budget = compute_daily_budget(bankroll)
    try:
        from src import db as _db
        if _db.is_supabase():
            _db.cache_set("daily_budget", None, _today_et(), budget)
    except Exception as _e:                                                # noqa: BLE001
        _eprint(f"daily budget persist failed: {_e}")
    return budget

# moved from app.py:9789
def _run_overnight_ai_gen() -> None:
    """3:30 AM ET -- two-pass overnight AI pre-generation so every pick has a
    breakdown before the user wakes.  Runs right after JOB 3 (3 AM prefetch).

    Pass 1: game breakdowns on 70B (V3) + ALL props on 8B (V2).
    Pass 2: top agreeing, high-confidence props re-run on 70B (V3).
    Budget-aware cascading + spacing live in src/groq_models.  Daytime only
    re-runs on line movement after this."""
    _eprint("OVERNIGHT AI: starting two-pass pre-generation (V3 games + V2 props, "
            "then V3 top props)")
    try:
        hydrate_state()
    except Exception as _he:                                              # noqa: BLE001
        _eprint(f"OVERNIGHT AI: hydrate failed: {_he}")
    try:
        from src import ai_summaries
        game_results = (
            [("mlb",  r) for r in (_analysis_state.get("results") or [])]
            + [("wnba", r) for r in (_wnba_analysis_state.get("results") or [])]
        )
        summary = ai_summaries.run_overnight_generation(game_results)
        _eprint(f"OVERNIGHT AI: done -- {summary}")
    except Exception as _exc:                                             # noqa: BLE001
        _eprint(f"OVERNIGHT AI: FAILED: {type(_exc).__name__}: {_exc}\n"
                f"{traceback.format_exc()}")

# moved from app.py:9854
def _void_postponed_mlb_bets() -> list:
    """
    Check MLB Stats API for postponed games today. For each open MLB bet
    matching a postponed game, void it (return stake, result='void').
    Returns list of voided bet entries.
    """
    voided: list = []
    try:
        date_str = _today_et_str()
        live_map = _fetch_mlb_linescore_raw(date_str)
    except Exception as _e:
        _eprint(f"AUTO-SETTLE: could not fetch MLB linescore for postponed check: {_e}")
        return voided

    # Collect postponed matchups as normalised (away, home) tuples
    postponed: list = []
    for _pk, _g in live_map.items():
        try:
            detail = _g.get("status", {}).get("detailedState", "")
            if detail == "Postponed":
                away = _g["teams"]["away"]["team"]["name"]
                home = _g["teams"]["home"]["team"]["name"]
                postponed.append((_norm_team_name(away), _norm_team_name(home)))
        except Exception:
            continue

    if not postponed:
        return voided

    # Load MLB ledger and void matching open bets
    try:
        _ldr = Ledger(path="data/ledger.json", starting_bankroll=250)
    except Exception as _e:
        _eprint(f"AUTO-SETTLE: could not load MLB ledger for void: {_e}")
        return voided

    remaining: list = []
    changed = False
    for bet in _ldr.data.get("open_bets", []):
        b_away = _norm_team_name(bet.get("away_team", ""))
        b_home = _norm_team_name(bet.get("home_team", ""))
        is_postponed = any(
            (b_away in pa or pa in b_away) and (b_home in ph or ph in b_home)
            for pa, ph in postponed
        )
        if is_postponed:
            # Return stake to both bankrolls
            model_amt = bet.get("model_amount", 0.0)
            conf_amt  = bet.get("confirmed_amount", 0.0)
            limit_hit = bet.get("limit_reached", False)
            if not limit_hit:
                if model_amt > 0:
                    _ldr.data["model_bankroll"] = round(
                        _ldr.data["model_bankroll"] + model_amt, 2)
                if bet.get("confirmed") and conf_amt > 0:
                    _ldr.data["personal_bankroll"] = round(
                        _ldr.data["personal_bankroll"] + conf_amt, 2)
            voided_entry = {
                **bet,
                "result":        "void",
                "model_pnl":     0.0,
                "confirmed_pnl": 0.0,
                "settled_at":    datetime.now(timezone.utc).isoformat(),
                "void_reason":   "postponed",
            }
            _ldr.data.setdefault("history", []).append(voided_entry)
            voided.append(voided_entry)
            changed = True
        else:
            remaining.append(bet)

    if changed:
        _ldr.data["open_bets"] = remaining
        _ldr.save()
    return voided

# moved from app.py:10031
def _completed_games_from_scores(scores: list) -> list[dict]:
    """Normalize Odds API score rows into {id, home_team, away_team,
    home_score, away_score, total_runs, game_date} for the tracker grader.
    Only completed games with both scores are returned."""
    out: list[dict] = []
    for s in (scores or []):
        if not isinstance(s, dict) or not s.get("completed"):
            continue
        gid = str(s.get("id") or "")
        ht, at = s.get("home_team"), s.get("away_team")
        hs = as_ = None
        for nm in (s.get("scores") or []):
            if not isinstance(nm, dict):
                continue
            try:
                sc = int(nm.get("score"))
            except (TypeError, ValueError):
                continue
            if nm.get("name") == ht:
                hs = sc
            elif nm.get("name") == at:
                as_ = sc
        if hs is None or as_ is None:
            continue
        out.append({
            "id":         gid,
            "home_team":  ht,
            "away_team":  at,
            "home_score": hs,
            "away_score": as_,
            "total_runs": hs + as_,
            "game_date":  (s.get("commence_time") or "")[:10],
        })
    return out

# moved from app.py:10067
def _grade_model_trackers(oc, sport_keys: list[str], scores_by_sport=None) -> dict:
    """Grade all pending XGB/LR/NN tracker picks against completed games.
    Returns {'xgb': n, 'lr': n, 'nn': n} newly graded.

    *scores_by_sport* may be a {sport_key: [score rows]} map pre-fetched
    earlier in the same cycle; when present it's reused instead of calling
    get_scores again (avoids a duplicate Odds API call)."""
    graded = {"xgb": 0, "lr": 0, "nn": 0}
    for sk in sport_keys:
        if scores_by_sport is not None and sk in scores_by_sport:
            scores = scores_by_sport.get(sk) or []
        else:
            try:
                scores = oc.get_scores(sport_key=sk, days_from=3) or []
            except Exception:                                               # noqa: BLE001
                continue
        games = _completed_games_from_scores(scores)
        if not games:
            continue
        try:
            from src import xgb_picks_tracker as _xgb
            graded["xgb"] += _xgb.settle_picks(games)
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE xgb error: {_e}")
        try:
            from src import lr_picks_tracker as _lr
            for g in games:
                graded["lr"] += _lr.settle_lr_pick(g["id"], g["home_score"], g["away_score"])
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE lr error: {_e}")
        try:
            from src import nn_picks as _nn
            graded["nn"] += _nn.settle_completed_games(games)
        except Exception as _e:                                             # noqa: BLE001
            _eprint(f"TRACKER-GRADE nn error: {_e}")
    return graded

# moved from app.py:10241
def _statsapi_pick_et_date(iso):
    """ET calendar date (YYYY-MM-DD) for a model_picks created_at timestamp
    (stored UTC) -- the game is the ET day the pick was logged.  None on
    parse failure."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=-4))).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return None

# moved from app.py:10256
def _statsapi_date_window(base_iso):
    """[base-1, base, base+1] ET date strings, tolerating UTC/ET rollover and
    games logged the morning before a late start."""
    try:
        from datetime import date as _date
        b = _date.fromisoformat(base_iso)
        return [(b + timedelta(days=o)).isoformat() for o in (0, -1, 1)]
    except Exception:                                                     # noqa: BLE001
        return [base_iso]

# moved from app.py:10267
def _statsapi_schedule_index(date_iso: str) -> dict:
    """Final MLB games for one ET date from the free statsapi.mlb.com schedule
    (hydrate=linescore), indexed by normalised team name ->
    {home_team, away_team, home_score, away_score, gamePk}.  Cached 1h.
    No API key, no day-window limit."""
    now = time.time()
    hit = _STATSAPI_BRIDGE_CACHE.get(date_iso)
    if hit and (now - hit[0]) < _STATSAPI_BRIDGE_TTL:
        return hit[1]
    idx: dict = {}
    url = (f"{_MLB_STATS_BASE}/schedule?sportId=1&date={date_iso}"
           f"&hydrate=linescore")
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "sports-betting-ai/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _eprint(f"SETTLE-STATSAPI: schedule fetch failed for {date_iso}: "
                f"{type(exc).__name__}: {exc}")
        _STATSAPI_BRIDGE_CACHE[date_iso] = (now, {})
        return {}
    for d in (data.get("dates") or []):
        for g in (d.get("games") or []):
            if ((g.get("status") or {}).get("abstractGameState")) != "Final":
                continue
            teams = g.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            ht = (home.get("team") or {}).get("name")
            at = (away.get("team") or {}).get("name")
            hs, as_ = home.get("score"), away.get("score")
            if ht is None or at is None or hs is None or as_ is None:
                continue
            info = {"home_team": ht, "away_team": at,
                    "home_score": int(hs), "away_score": int(as_),
                    "gamePk": g.get("gamePk")}
            idx[_statsapi_norm_team(ht)] = info
            idx[_statsapi_norm_team(at)] = info
    _STATSAPI_BRIDGE_CACHE[date_iso] = (now, idx)
    return idx

# moved from app.py:10308
def _fetch_mlb_statsapi_scores(game_picks: list) -> dict:
    """Resolve final scores for stale GAME model_picks rows from the free
    statsapi.mlb.com schedule -- the fallback for picks whose game is older
    than the Odds API /scores 3-day window.

    model_picks stores ``game_id`` = the Odds API event id (32-char hex), which
    statsapi does NOT understand (it keys games by a 6-digit gamePk), so a bare
    id cannot be turned into a gamePk.  We bridge via the data the pick row DOES
    carry: the picked TEAM (``pick_side``) plus the ET day it was logged
    (``created_at``).  That date's statsapi schedule yields the gamePk, both
    team names and the final score; we match the pick's team and return the
    score keyed by the ORIGINAL Odds API ``game_id`` so settle()'s
    ``final_scores.get(pick["game_id"])`` lookup hits.

    *game_picks* must be GAME rows (no ``player_name``) -- props are graded via
    the stat lookup, not scores, so the caller filters them out.  Returns
    ``{odds_api_game_id: {home_team, away_team, home_score, away_score}}`` with
    team names taken from the picks' own ``pick_side`` strings where they map,
    so _grade_game's exact-name comparison succeeds.
    """
    # Group rows by Odds API game_id; gather the (Odds API) team names the
    # picks used + a representative logged date.
    by_gid: dict = {}
    for pick in (game_picks or []):
        gid = str(pick.get("game_id") or "").strip()
        if not gid:
            continue
        grp = by_gid.setdefault(gid, {"teams": set(), "date": None})
        side = (pick.get("pick_side") or "").strip()
        if side and side.upper() not in ("OVER", "UNDER"):  # ml/rl carry a team
            grp["teams"].add(side)
        if grp["date"] is None:
            grp["date"] = _statsapi_pick_et_date(pick.get("created_at"))

    out: dict = {}
    for gid, grp in by_gid.items():
        teams, base_date = grp["teams"], grp["date"]
        if not teams or not base_date:
            continue                          # totals-only group / no date -> skip
        info = match_team = None
        for date_iso in _statsapi_date_window(base_date):
            idx = _statsapi_schedule_index(date_iso)
            for tm in teams:
                hit = idx.get(_statsapi_norm_team(tm))
                if hit:
                    info, match_team = hit, tm
                    break
            if info:
                break
        if not info:
            continue
        # Use the picks' own (Odds API) team strings where they map back to the
        # statsapi game, so _grade_game's `side == ht/at` succeeds; fall back to
        # the statsapi names for the side the picks never referenced.
        norm_to_pick = {_statsapi_norm_team(tm): tm for tm in teams}
        home_name = norm_to_pick.get(_statsapi_norm_team(info["home_team"]),
                                     info["home_team"])
        away_name = norm_to_pick.get(_statsapi_norm_team(info["away_team"]),
                                     info["away_team"])
        out[gid] = {
            "home_team":  home_name,
            "away_team":  away_name,
            "home_score": info["home_score"],
            "away_score": info["away_score"],
        }
        _eprint(
            f"SETTLE-STATSAPI: resolved game_id={gid} via team={match_team!r} "
            f"date={base_date} -> gamePk={info.get('gamePk')} "
            f"{away_name} {info['away_score']} @ {home_name} {info['home_score']}"
        )
    return out
