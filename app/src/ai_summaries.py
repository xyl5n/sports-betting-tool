"""
AI summary generation + caching for game cards and prop cards.

Pipeline (run as a background job so it never blocks page loads):

  Step 1  Game summaries  -- 2-3 sentences per game pick, generated first
                             (triggered with the 8 AM analysis run).  ALL
                             game summaries finish before props start.
  Step 2  Prop summaries  -- 1-2 sentences per scored prop, processed in
                             DESCENDING confidence order until every prop
                             has one.

Caching + invalidation (Supabase app_cache, one aggregate row per kind):
  A summary is reused as-is unless the underlying pick changed:
    * prop:  line changed, side flipped, or projected value moved > 0.1
    * game:  the pick flipped, OR the starting pitcher changed
  When a pick's fingerprint changes we regenerate (overwriting the old
  summary) on the next queue run; otherwise the cached text is always
  served and never regenerated.

Rate limiting: Groq calls run sequentially with a 150 ms gap between them.
Progress is logged every 10 generated summaries; a final line logs totals.

All public functions are best-effort -- failures are swallowed so the UI
and the scheduler never break because of a missing summary.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Aggregate app_cache rows -- {pick_key: {"summary": str, "fp": {...},
# "updated_at": iso}}.  Date == today so the row is fresh per day; it
# survives same-day Railway redeploys (date matches the cleaner's "today").
_GAME_CACHE_KEY = "ai_game_summaries"
_PROP_CACHE_KEY = "ai_prop_summaries"

_DELAY_S   = 0.15      # 150 ms between Groq calls (free-tier friendly)
_LOG_EVERY = 10

# In-process working copy + read cache (the scheduler job and the page
# render share one process on Railway).  Reads reload from Supabase at most
# once per _READ_TTL seconds.
_STORE: dict[str, dict] = {"game": {}, "prop": {}}
_LOADED_TS: dict[str, float] = {"game": 0.0, "prop": 0.0}
_READ_TTL = 60.0

_queue_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[ai-summaries] {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(kind: str) -> str:
    return _GAME_CACHE_KEY if kind == "game" else _PROP_CACHE_KEY


# ── Cache I/O ───────────────────────────────────────────────────────────────

def _load(kind: str, force: bool = False) -> dict:
    """Return the {pick_key: entry} dict for *kind*, reloading from Supabase
    at most once per _READ_TTL seconds."""
    now = time.monotonic()
    if not force and _STORE.get(kind) and (now - _LOADED_TS.get(kind, 0.0)) < _READ_TTL:
        return _STORE[kind]
    try:
        from . import db
        if db.is_supabase():
            row = db.cache_get(_cache_key(kind))
            data = None
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(data, dict) and isinstance(data.get("summaries"), dict):
                _STORE[kind] = data["summaries"]
            elif isinstance(data, dict):
                # tolerate a bare {pick_key: entry} payload
                _STORE[kind] = {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"load({kind}) failed: {exc}")
    _LOADED_TS[kind] = now
    return _STORE.setdefault(kind, {})


def _flush(kind: str) -> None:
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_cache_key(kind), None, _today_et(),
                         {"summaries": _STORE.get(kind, {})})
    except Exception as exc:                                              # noqa: BLE001
        _log(f"flush({kind}) failed: {exc}")


def _have_supabase() -> bool:
    try:
        from . import db
        return db.is_supabase()
    except Exception:                                                     # noqa: BLE001
        return False


# ── Formatting helpers ──────────────────────────────────────────────────────

def _pct(x) -> str:
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def _odds(x) -> str:
    try:
        n = int(x)
        return f"+{n}" if n > 0 else str(n)
    except (TypeError, ValueError):
        return "n/a"


def _signed(x) -> str:
    try:
        return f"{float(x):+g}"
    except (TypeError, ValueError):
        return ""


def _num(x) -> str:
    try:
        return f"{float(x):.1f}"
    except (TypeError, ValueError):
        return "n/a"


_MARKET_LABEL = {
    "pitcher_strikeouts": "strikeouts", "pitcher_outs": "outs recorded",
    "pitcher_hits_allowed": "hits allowed", "pitcher_walks": "walks allowed",
    "pitcher_earned_runs": "earned runs", "batter_hits": "hits",
    "batter_total_bases": "total bases", "batter_home_runs": "home runs",
    "batter_rbis": "RBIs", "batter_runs_scored": "runs", "batter_walks": "walks",
    "batter_strikeouts": "strikeouts", "batter_stolen_bases": "stolen bases",
}


def _market_label(m: str) -> str:
    return _MARKET_LABEL.get(m or "", (m or "").replace("_", " "))


# ── Game summaries ──────────────────────────────────────────────────────────

def _game_id(g: dict) -> str:
    return str(g.get("game_id") or g.get("id") or "")


def _game_fp(g: dict) -> dict:
    rl  = g.get("run_line") or {}
    tot = g.get("totals") or {}
    hsp = g.get("home_sp") or {}
    asp = g.get("away_sp") or {}
    return {
        "pick":     g.get("pick_team"),
        "rl":       rl.get("pick_team"),
        "rl_pt":    rl.get("run_line_point"),
        "tot_dir":  tot.get("direction"),
        "tot_line": tot.get("total_line"),
        "hsp":      hsp.get("full_name"),
        "asp":      asp.get("full_name"),
    }


def _game_prompt(sport: str, g: dict) -> str:
    away = g.get("away_team") or "Away"
    home = g.get("home_team") or "Home"
    facts: list[str] = [f"Matchup: {away} at {home}."]

    if g.get("pick_team"):
        facts.append(
            f"Model moneyline pick: {g.get('pick_team')} at {_odds(g.get('pick_odds'))}, "
            f"confidence {_pct(g.get('pick_prob'))}, edge {_pct(g.get('pick_edge'))}."
        )
    rl = g.get("run_line") or {}
    if rl.get("pick_team") and rl.get("value_bet"):
        facts.append(
            f"Run line value: {rl.get('pick_team')} {_signed(rl.get('run_line_point'))} "
            f"(confidence {_pct(rl.get('pick_prob'))})."
        )
    tot = g.get("totals") or {}
    if tot.get("total_line") and tot.get("value_bet"):
        facts.append(
            f"Total value: {(tot.get('direction') or '').title()} {tot.get('total_line')} "
            f"(confidence {_pct(tot.get('pick_prob'))})."
        )
    if (sport or "").lower() == "mlb":
        asp = g.get("away_sp") or {}
        hsp = g.get("home_sp") or {}
        if asp.get("full_name") or hsp.get("full_name"):
            facts.append(
                f"Pitchers: {asp.get('full_name') or 'TBD'} "
                f"({_num(asp.get('era'))} ERA, {_num(asp.get('k_per_9'))} K/9) vs "
                f"{hsp.get('full_name') or 'TBD'} "
                f"({_num(hsp.get('era'))} ERA, {_num(hsp.get('k_per_9'))} K/9)."
            )
    return (
        "You are a concise sports-betting analyst. In 2-3 sentences explain why "
        "the model favors its pick for this game. Use ONLY the facts provided; do "
        "not invent any numbers. Keep it under 60 words.\n" + " ".join(facts)
    )


# ── Prop summaries ──────────────────────────────────────────────────────────

def _prop_key(r: dict) -> str:
    return f"{r.get('player')}|{r.get('market')}"


def _prop_fp(r: dict) -> dict:
    return {
        "line": r.get("line"),
        "side": (r.get("side") or "").title(),
        "pv":   r.get("predicted_value"),
    }


def _prop_fp_matches(old: dict, new: dict) -> bool:
    """Reuse the cached prop summary unless line changed, side flipped, or the
    projected value moved by more than 0.1."""
    if not isinstance(old, dict):
        return False
    try:
        if (old.get("line")) != (new.get("line")):
            return False
        if (old.get("side") or "") != (new.get("side") or ""):
            return False
        op, np_ = old.get("pv"), new.get("pv")
        if (op is None) != (np_ is None):
            return False
        if op is not None and abs(float(op) - float(np_)) > 0.1:
            return False
        return True
    except (TypeError, ValueError):
        return False


def _prop_prompt(r: dict) -> str:
    s = r.get("summary") or {}

    def hr(n: int) -> str:
        h = s.get(f"last_{n}_hits"); g = s.get(f"last_{n}_games")
        return f"L{n} {h}/{g}" if g else f"L{n} n/a"

    facts = [
        f"{r.get('player')} {(r.get('side') or '').title()} {r.get('line')} "
        f"{_market_label(r.get('market'))}.",
        f"Model confidence {_pct(r.get('confidence'))}, projected {_num(r.get('predicted_value'))}.",
        f"Hit rates {hr(5)}, {hr(10)}, {hr(20)}; season avg {_num(s.get('season_avg'))}.",
    ]
    if r.get("opp_abbrev"):
        rank = r.get("opp_rank")
        rank_s = f"#{rank}" if rank is not None else "n/a"
        facts.append(f"Opponent {r.get('opp_abbrev')} ranks {rank_s} vs this stat (1=toughest).")
    return (
        "You are a concise sports-betting analyst. In 1-2 sentences explain why "
        "the model likes this player prop. Use ONLY the facts provided; do not "
        "invent any numbers. Keep it under 40 words.\n" + " ".join(facts)
    )


# ── Generation steps ────────────────────────────────────────────────────────

def _generate_games(game_results: list[tuple]) -> dict:
    """game_results: list of (sport, serialized_game_dict).  Returns counts."""
    from .groq_client import generate_summary
    store = _load("game", force=True)
    done = generated = cached = 0
    total = len(game_results)
    for sport, g in game_results:
        gid = _game_id(g)
        if not gid or not g.get("pick_team"):
            continue
        key = f"{sport}:{gid}"
        fp  = _game_fp(g)
        old = store.get(key)
        if isinstance(old, dict) and old.get("summary") and old.get("fp") == fp:
            cached += 1
            continue
        text = generate_summary(_game_prompt(sport, g), max_tokens=160)
        time.sleep(_DELAY_S)
        if text:
            store[key] = {"summary": text, "fp": fp, "updated_at": _now_iso()}
            generated += 1
        done += 1
        if done % _LOG_EVERY == 0:
            _flush("game")
            _log(f"games: {done} processed, {total - done} remaining, "
                 f"{cached} from cache, {generated} generated")
    _flush("game")
    _log(f"GAMES DONE: {total} picks | {generated} generated | {cached} cached")
    return {"generated": generated, "cached": cached, "total": total}


def _generate_props() -> dict:
    from .groq_client import generate_summary
    try:
        from .props_scored_cache import load_scored_props
        picks = list((load_scored_props() or {}).get("picks") or [])
    except Exception as exc:                                              # noqa: BLE001
        _log(f"props load failed: {exc}")
        return {"generated": 0, "cached": 0, "total": 0}

    # Highest-confidence picks get summaries first.
    picks.sort(key=lambda r: -float(r.get("confidence") or 0.0))
    store = _load("prop", force=True)
    total = len(picks)
    done = generated = cached = 0
    for r in picks:
        key = _prop_key(r)
        fp  = _prop_fp(r)
        old = store.get(key)
        if isinstance(old, dict) and old.get("summary") and _prop_fp_matches(old.get("fp"), fp):
            cached += 1
            continue
        text = generate_summary(_prop_prompt(r), max_tokens=120)
        time.sleep(_DELAY_S)
        if text:
            store[key] = {"summary": text, "fp": fp, "updated_at": _now_iso()}
            generated += 1
        done += 1
        if done % _LOG_EVERY == 0:
            _flush("prop")
            _log(f"props: {done} processed, {total - done} remaining, "
                 f"{cached} from cache, {generated} generated")
    _flush("prop")
    _log(f"PROPS DONE: {total} picks | {generated} generated | {cached} cached")
    return {"generated": generated, "cached": cached, "total": total}


def run_summary_queue(game_results: list[tuple] | None = None,
                      do_games: bool = True, do_props: bool = True) -> None:
    """Blocking queue: games first (all complete), then props in descending
    confidence.  Guarded so only one queue runs at a time."""
    if not _queue_lock.acquire(blocking=False):
        _log("queue already running -- skipping this trigger")
        return
    try:
        if not _have_supabase():
            _log("Supabase not configured -- summaries disabled (nothing persisted)")
            return
        if do_games and game_results:
            _generate_games(game_results)
        if do_props:
            _generate_props()
    except Exception as exc:                                              # noqa: BLE001
        _log(f"queue error: {type(exc).__name__}: {exc}")
    finally:
        _queue_lock.release()


def launch_summary_queue(game_results: list[tuple] | None = None,
                         do_games: bool = True, do_props: bool = True) -> None:
    """Fire-and-forget: run the queue on a daemon thread so it never blocks
    the scheduler callback or a page load."""
    try:
        threading.Thread(
            target=run_summary_queue,
            kwargs={"game_results": game_results, "do_games": do_games, "do_props": do_props},
            daemon=True,
        ).start()
    except Exception as exc:                                              # noqa: BLE001
        _log(f"launch failed: {exc}")


# ── UI read helpers (never generate) ────────────────────────────────────────

def get_game_summary(sport: str, game: dict) -> str | None:
    try:
        gid = _game_id(game)
        if not gid:
            return None
        entry = _load("game").get(f"{(sport or 'mlb').lower()}:{gid}")
        if isinstance(entry, dict):
            return entry.get("summary") or None
    except Exception:                                                     # noqa: BLE001
        pass
    return None


def get_prop_summary(pick: dict) -> str | None:
    try:
        entry = _load("prop").get(_prop_key(pick))
        if isinstance(entry, dict):
            return entry.get("summary") or None
    except Exception:                                                     # noqa: BLE001
        pass
    return None
