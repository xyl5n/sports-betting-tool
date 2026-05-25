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


def _present(x) -> bool:
    """True only for a real, usable value -- filters out the None / 'n/a' /
    'None/None' placeholders so we never feed a 'missing' fact to Groq (which
    is what made it keep narrating 'data not available')."""
    if x is None:
        return False
    s = str(x).strip().lower()
    return s not in ("", "n/a", "none", "none/none", "0/0", "-", "+nan", "nan")


# Shared analyst framing -- mirrors the player-profile big-breakdown system
# prompt: an experienced bettor who INTERPRETS data for THIS matchup instead
# of reciting it.  The two hard rules below are what fix the reported output
# problems: (a) never echo the verdict label as if it were analysis, and
# (b) silently omit anything not given rather than saying it's unavailable.
_ANALYST_RULES = (
    "You are an experienced MLB betting analyst writing for a sharp bettor -- "
    "not a data reader. Reason ACROSS the facts to judge THIS specific matchup; "
    "connect each number to what it means here rather than listing it (e.g. "
    "'his 47% four-seam usage is exposed tonight because this lineup slugs .500 "
    "on fastballs', or 'the slider should travel since these hitters whiff a lot "
    "on breaking balls'). Name the two or three strongest factors for or against, "
    "call out any conflicting signal that warrants caution (e.g. strong model "
    "confidence but fading recent form, or a hitter-friendly park against a tough "
    "arsenal), and reference the similar-player comparison when it is given and "
    "relevant. Then give a clear directional read. "
    "HARD RULES: Never invent or assume a number -- use only the facts provided. "
    "If a fact is not in the list it is simply unknown: do NOT mention it, do NOT "
    "say anything is 'not available', 'unavailable', 'unknown', or 'not provided' "
    "-- just leave it out. Never write filler that merely restates the verdict "
    "label (e.g. 'this prop leans toward a lean', 'this is a neutral neutral'); "
    "say something substantive instead. "
    "DIRECTION: the facts state the model's pick (its side). Your directional "
    "read must MATCH that pick's side -- explain why the pick is right (or, if "
    "you genuinely disagree, say so plainly and once); never argue for the "
    "opposite side while implying you agree. NEVER use self-contradictory "
    "phrasing that mixes opposite verdict tiers, e.g. 'lean toward a fade', "
    "'fade the lean', or 'neutral lean' -- a lean means take the pick, a fade "
    "means take the other side; they are opposites, never combine them. "
    "Plain text only: no markdown, asterisks, headers, bullets, or dashes."
)


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


def _sp_fact(sp: dict, label: str, *, is_home: bool) -> str | None:
    """One starting-pitcher fact line -- season rate stats + recent form +
    arsenal + home/away ERA split -- including only what's actually present."""
    name = (sp or {}).get("full_name")
    if not name:
        return None
    bits: list[str] = []
    for key, suffix in (("era", "ERA"), ("whip", "WHIP"), ("k_per_9", "K/9")):
        if _present(sp.get(key)):
            bits.append(f"{_num(sp.get(key))} {suffix}")
    if _present(sp.get("last3_era")):
        bits.append(f"{_num(sp.get('last3_era'))} ERA over his last 3")
    w, l = sp.get("wins"), sp.get("losses")
    if w is not None and l is not None:
        bits.append(f"{w}-{l}")
    # Home/away ERA split (this start's venue) from the splits client.
    try:
        from . import ai_context as _aic
        from .pitcher_splits_client import get_pitcher_splits_client
        from .player_profile_client import _CURRENT_SEASON
        pid = sp.get("id") or sp.get("player_id") or _aic.resolve_player_id(name)
        if pid:
            sp_split = get_pitcher_splits_client().get_splits(int(pid), _CURRENT_SEASON) or {}
            venue_era = sp_split.get("home_era") if is_home else sp_split.get("away_era")
            if _present(venue_era):
                bits.append(f"{_num(venue_era)} ERA "
                            f"{'at home' if is_home else 'on the road'}")
    except Exception:                                                     # noqa: BLE001
        pass
    mix = _pitcher_mix_text(name)
    tail = f" — {mix.replace('Arsenal: ', 'throws ').rstrip('.')}" if mix else ""
    if not bits:
        return f"{label} starter {name}{tail}."
    return f"{label} starter {name}: " + ", ".join(bits) + tail + "."


def _bullpen_fact(home: str, away: str, commence: Optional[str]) -> str | None:
    """Both bullpens' season ERA + recent workload (fatigue), or None."""
    try:
        from .bullpen_client import get_bullpen_client
        date10 = (commence or "")[:10] or None
        bp = get_bullpen_client().get_bullpen_for_game(home, away, date10) or {}
        parts = []
        for side, team in (("away", away), ("home", home)):
            d = bp.get(side) or {}
            if _present(d.get("era")):
                fat = d.get("fatigue")
                fat_s = (f", {fat} relief apps in the last 5 days"
                         if isinstance(fat, int) else "")
                parts.append(f"{team} bullpen {_num(d.get('era'))} ERA{fat_s}")
        return "Bullpens: " + "; ".join(parts) + "." if parts else None
    except Exception:                                                     # noqa: BLE001
        return None


def _weather_fact(home: str, commence: Optional[str]) -> str | None:
    """Game-time weather (skipped for domes / when unavailable)."""
    try:
        from .player_matchup import get_weather
        w = get_weather(home, commence) or {}
        if not w.get("available") or w.get("dome"):
            return None
        bits = []
        if _present(w.get("temperature")):
            bits.append(f"{_num(w.get('temperature'))}°F")
        if w.get("conditions") and w["conditions"] != "—":
            bits.append(str(w["conditions"]))
        if _present(w.get("wind_speed")):
            wd = w.get("wind_dir") if w.get("wind_dir") not in (None, "—") else ""
            bits.append(f"wind {_num(w.get('wind_speed'))} mph {wd}".strip())
        return "Weather: " + ", ".join(bits) + "." if bits else None
    except Exception:                                                     # noqa: BLE001
        return None


def _game_prompt(sport: str, g: dict) -> str:
    away = g.get("away_team") or "Away"
    home = g.get("home_team") or "Home"
    pick = g.get("pick_team")
    other = (away if pick == home else home) if pick else None
    commence = g.get("commence_time")
    facts: list[str] = [f"Matchup: {away} at {home}."]

    if pick:
        facts.append(
            f"Model moneyline pick: {pick} at {_odds(g.get('pick_odds'))}, "
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
        for sp, lbl, is_home in ((g.get("away_sp") or {}, away, False),
                                 (g.get("home_sp") or {}, home, True)):
            fact = _sp_fact(sp, lbl, is_home=is_home)
            if fact:
                facts.append(fact)
        bp = _bullpen_fact(home, away, commence)
        if bp:
            facts.append(bp)
        wx = _weather_fact(home, commence)
        if wx:
            facts.append(wx)
        try:
            from .park_factors import get_park_factors
            run_f, hr_f = get_park_factors(home)
            facts.append(f"Park factors at {home}: run {run_f:.2f}, HR {hr_f:.2f} "
                         f"(1.00 = neutral).")
        except Exception:                                                 # noqa: BLE001
            pass
    # Pass through any extra signals the analysis row already carries.
    for key, label in (("h2h", "Season head-to-head"), ("bullpen", "Bullpen"),
                       ("team_ranks", "Team offensive ranks"),
                       ("home_away", "Home/away splits")):
        v = g.get(key)
        if isinstance(v, str) and v.strip():
            facts.append(f"{label}: {v.strip()}.")

    tier_block = ""
    if pick and other:
        tier_block = (
            f"\nThe model's pick is {pick} (moneyline). TIER DEFINITIONS relative "
            f"to that pick: \"Strong Lean\"/\"Lean\" = AGREE, back {pick}; "
            f"\"Fade\"/\"Strong Fade\" = DISAGREE, side with {other}; \"Neutral\" "
            f"= no strong edge. The verdict_tier and the summary MUST point the "
            f"same way (Lean -> argue for {pick}; Fade -> argue for {other}); "
            f"never mix opposite tiers ('lean toward a fade')."
        )

    return (
        _ANALYST_RULES
        + " Reason like an analyst across the FULL game context -- starting "
        "pitching (arsenal, recent form, home/away split), bullpens, weather, "
        "park, and team/offensive context -- and connect the specific factors "
        "that drive THIS game to the pick rather than reciting them."
        + tier_block
        + "\n\nReturn ONLY a JSON object with exactly two string keys:\n"
        '  "verdict_tier": EXACTLY one of "Strong Lean", "Lean", "Neutral", '
        '"Fade", "Strong Fade" per the definitions above.\n'
        '  "summary": 3-5 plain-text sentences (no markdown) that argue the SAME '
        "direction as verdict_tier -- lead with the single biggest edge, weigh "
        "the opposing starter/bullpen/park/weather against it, and close with a "
        "clear read.\n\nFACTS: " + " ".join(facts)
    )


def _parse_game_summary(text: str | None) -> dict | None:
    """Parse the game JSON {verdict_tier, summary}.  Tolerates a fenced
    ```json block and plain (non-JSON) text -- in which case the whole thing
    is the summary with an empty tier.  Reuses the prop fix's five canonical
    tiers so badge + text share one vocabulary.  None when there's no summary."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    summary, tier = "", ""
    try:
        import json as _json
        obj = _json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        summary = str(obj.get("summary") or "").strip()
        tier = str(obj.get("verdict_tier") or "").strip()
    except Exception:                                                     # noqa: BLE001
        summary = raw                       # plain-text fallback
    try:
        from .player_ai_breakdown import _VERDICT_TIERS
        tl = tier.lower()
        tier = next((t for t in _VERDICT_TIERS if t.lower() == tl), "")
    except Exception:                                                     # noqa: BLE001
        tier = ""
    if not summary:
        return None
    return {"summary": summary, "verdict_tier": tier}


def _pitcher_mix_text(name: str) -> str:
    """'throws Slider 38% 87mph, ...' for a starter, or '' if unavailable."""
    try:
        from . import ai_context as _aic
        mix = _aic.pitch_mix(_aic.resolve_player_id(name or ""))
        txt = _aic.pitch_mix_text(mix)
        return txt.replace("Arsenal: ", "throws ") if txt else ""
    except Exception:                                                     # noqa: BLE001
        return ""


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


def _opposing_pitcher_id(r: dict):
    """Best-effort MLB id of the pitcher a batter prop faces today."""
    try:
        from .player_profile_client import get_today_opposing_pitcher
        prop = {
            "home_team": r.get("home_team"), "away_team": r.get("away_team"),
            "team": r.get("team"), "commence_time": r.get("commence_time"),
            "event_id": r.get("event_id"),
        }
        opp = get_today_opposing_pitcher(prop, r.get("player") or "") or {}
        return opp.get("id")
    except Exception:                                                     # noqa: BLE001
        return None


def _prop_prompt(r: dict) -> str:
    s = r.get("summary") or {}
    market = r.get("market") or ""
    player = r.get("player") or ""
    is_pitcher_market = market.startswith("pitcher_")

    facts = [
        f"{player} {(r.get('side') or '').title()} {r.get('line')} "
        f"{_market_label(market)}."
    ]

    # Model read -- always include confidence; projection only when present.
    if _present(r.get("confidence")):
        line = f"Model confidence {_pct(r.get('confidence'))}"
        if _present(r.get("predicted_value")):
            line += f", projecting {_num(r.get('predicted_value'))}"
        facts.append(line + ".")

    # Recent form -- only windows that actually have games, plus season avg.
    rates = [f"L{n} {s.get(f'last_{n}_hits')}/{s.get(f'last_{n}_games')}"
             for n in (5, 10, 20) if s.get(f"last_{n}_games")]
    if _present(s.get("season_avg")):
        rates.append(f"season avg {_num(s.get('season_avg'))}")
    if rates:
        facts.append("Recent form (times the line was hit): " + ", ".join(rates) + ".")

    # Head-to-head vs today's opponent.
    if s.get("h2h_games"):
        facts.append(
            f"Career vs {r.get('opp_abbrev') or 'this opponent'}: "
            f"{s.get('h2h_hits')}/{s.get('h2h_games')} games over the line"
            + (f", {_num(s.get('h2h_avg'))} average" if _present(s.get("h2h_avg")) else "")
            + "."
        )

    # Opponent rank vs this stat -- recompute fresh if the row didn't carry it.
    rank = r.get("opp_rank")
    if rank is None and r.get("opp_abbrev"):
        try:
            from .player_profile_client import get_opp_rank_for_prop
            rank = get_opp_rank_for_prop(r.get("opp_abbrev"), market)
        except Exception:                                                 # noqa: BLE001
            rank = None
    if r.get("opp_abbrev") and rank is not None:
        facts.append(
            f"Opponent {r.get('opp_abbrev')} ranks #{rank} of 30 against this stat "
            f"(1 = the toughest matchup for the over, 30 = the softest)."
        )

    # Deeper signals (best-effort, cached): similar-player cluster + the
    # pitch-mix matchup (pitcher's own arsenal, or for a batter prop the
    # opposing arsenal + how this batter hits those pitch types).
    try:
        from . import ai_context as _aic
        st = _aic.similar_text(_aic.similar_players(market, player, limit=4),
                               _market_label(market))
        if st:
            facts.append(st)
        if is_pitcher_market:
            mt = _aic.pitch_mix_text(_aic.pitch_mix(_aic.resolve_player_id(player)))
            if mt:
                facts.append("His " + mt[0].lower() + mt[1:])
        else:
            opp_pid = _opposing_pitcher_id(r)
            if opp_pid:
                mt = _aic.pitch_mix_text(_aic.pitch_mix(opp_pid))
                if mt:
                    facts.append("Opposing pitcher's " + mt[0].lower() + mt[1:])
                bt = _aic.batter_vs_pitch_text(
                    _aic.batter_vs_pitch(_aic.resolve_player_id(player), opp_pid))
                if bt:
                    facts.append(bt)
    except Exception:                                                     # noqa: BLE001
        pass

    return (
        _ANALYST_RULES
        + " Write 3-5 sentences on this player prop. When an arsenal is given, "
        "explicitly weigh the pitch-type matchup (e.g. heavy slider usage against "
        "a hitter who struggles on breaking balls favors the under), and tie the "
        "opponent rank and recent form to a concrete read on the over or under.\n\n"
        "FACTS: " + " ".join(facts)
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
        # Regenerate only when missing.  Significant-change invalidation is
        # driven explicitly by the 15-min cycle (invalidate_game), so a
        # cached summary is kept until something material actually changed --
        # minor drift never triggers a regeneration.
        if isinstance(old, dict) and old.get("summary"):
            cached += 1
            continue
        parsed = _parse_game_summary(
            generate_summary(_game_prompt(sport, g), max_tokens=340))
        time.sleep(_DELAY_S)
        if parsed:
            store[key] = {"summary": parsed["summary"],
                          "verdict_tier": parsed["verdict_tier"],
                          "fp": fp, "updated_at": _now_iso()}
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
        # Regenerate only when missing.  The 15-min cycle calls
        # invalidate_prop() for SIGNIFICANT changes (line > 1.0, side flip,
        # projection-gap > 0.5); minor drift keeps the cached summary.
        if isinstance(old, dict) and old.get("summary"):
            cached += 1
            continue
        text = generate_summary(_prop_prompt(r), max_tokens=230)
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


def get_game_verdict_tier(sport: str, game: dict) -> str | None:
    """The AI's verdict tier for this game's moneyline pick (one of the five
    canonical tiers), or None.  Drives the Top Plays badge so badge + text
    share a single AI determination -- same approach as the prop verdict."""
    try:
        gid = _game_id(game)
        if not gid:
            return None
        entry = _load("game").get(f"{(sport or 'mlb').lower()}:{gid}")
        if isinstance(entry, dict):
            return entry.get("verdict_tier") or None
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


# ── Explicit invalidation (driven by the 15-min change-detection cycle) ──────
# Deleting an entry makes the NEXT summary batch regenerate it (the generators
# now regenerate only when an entry is missing).  Used after a model re-run so
# only picks that actually changed get a fresh Groq summary.

def ensure_game_summary(sport: str, g: dict, *, force: bool = False) -> str:
    """Generate the game-pick summary for one game if it isn't already
    cached.  Returns 'cached' / 'generated' / 'failed' / 'skipped'.  Does NOT
    sleep -- the caller paces calls (150 ms).  Used by the on-demand admin
    'Run AI Analysis' job for live progress + skip counts.

    force=True bypasses the cached-skip and regenerates + overwrites (the
    'Force AI Refresh' admin button)."""
    if not _have_supabase():
        return "skipped"
    gid = _game_id(g)
    if not gid or not g.get("pick_team"):
        return "skipped"
    sport = (sport or "mlb").lower()
    key = f"{sport}:{gid}"
    store = _load("game")
    old = store.get(key)
    if not force and isinstance(old, dict) and old.get("summary"):
        return "cached"
    from .groq_client import generate_summary
    parsed = _parse_game_summary(
        generate_summary(_game_prompt(sport, g), max_tokens=340))
    if not parsed:
        return "failed"
    store[key] = {"summary": parsed["summary"],
                  "verdict_tier": parsed["verdict_tier"],
                  "fp": _game_fp(g), "updated_at": _now_iso()}
    _flush("game")
    return "generated"


def ensure_prop_summary(r: dict, *, force: bool = False) -> str:
    """Generate the prop summary for one pick if it isn't already cached.
    Returns 'cached' / 'generated' / 'failed' / 'skipped'.  No internal sleep.

    force=True bypasses the cached-skip and regenerates + overwrites (the
    'Force AI Refresh' admin button)."""
    if not _have_supabase():
        return "skipped"
    player, market = r.get("player"), r.get("market")
    if not player or not market:
        return "skipped"
    key = _prop_key(r)
    store = _load("prop")
    old = store.get(key)
    if not force and isinstance(old, dict) and old.get("summary"):
        return "cached"
    from .groq_client import generate_summary
    text = generate_summary(_prop_prompt(r), max_tokens=230)
    if not text:
        return "failed"
    store[key] = {"summary": text, "fp": _prop_fp(r), "updated_at": _now_iso()}
    _flush("prop")
    return "generated"


def invalidate_game(sport: str, game_id) -> bool:
    """Drop the cached game summary for {sport}:{game_id} so it regenerates
    next batch.  Returns True if an entry was removed."""
    try:
        key = f"{(sport or 'mlb').lower()}:{game_id}"
        store = _load("game")
        if key in store:
            del store[key]
            _flush("game")
            return True
    except Exception as exc:                                              # noqa: BLE001
        _log(f"invalidate_game({sport}:{game_id}) failed: {exc}")
    return False


def invalidate_prop(player: str, market: str) -> bool:
    """Drop the cached prop summary for player|market so it regenerates next
    batch.  Returns True if an entry was removed."""
    try:
        key = f"{player}|{market}"
        store = _load("prop")
        if key in store:
            del store[key]
            _flush("prop")
            return True
    except Exception as exc:                                              # noqa: BLE001
        _log(f"invalidate_prop({player}|{market}) failed: {exc}")
    return False


# ── Per-bet-type game analysis (AI Analysis section on the matchup page) ──────

def _game_bets_prompt(sport: str, g: dict) -> str:
    away = g.get("away_team") or "Away"
    home = g.get("home_team") or "Home"
    facts: list[str] = [f"Matchup: {away} at {home}."]
    if g.get("pick_team"):
        facts.append(
            f"Model moneyline lean: {g.get('pick_team')} at {_odds(g.get('pick_odds'))}, "
            f"confidence {_pct(g.get('pick_prob'))}, edge {_pct(g.get('pick_edge'))}."
        )
    rl = g.get("run_line") or {}
    if rl.get("pick_team"):
        facts.append(
            f"Run line: model leans {rl.get('pick_team')} "
            f"{_signed(rl.get('run_line_point'))} (confidence {_pct(rl.get('pick_prob'))})."
        )
    tot = g.get("totals") or {}
    if tot.get("total_line") is not None:
        facts.append(
            f"Total line {tot.get('total_line')}; model projects "
            f"{_num(tot.get('predicted_total'))} runs (lean "
            f"{(tot.get('direction') or '').title() or 'n/a'})."
        )
    if (sport or "").lower() == "mlb":
        for sp, lbl in ((g.get("away_sp") or {}, away), (g.get("home_sp") or {}, home)):
            fact = _sp_fact(sp, lbl)
            if fact:
                facts.append(fact)
        try:
            from .park_factors import get_park_factors
            rf, hf = get_park_factors(home)
            facts.append(f"Park factors at {home}: run {rf:.2f}, HR {hf:.2f} "
                         f"(1.00 = neutral).")
        except Exception:                                                 # noqa: BLE001
            pass
    for key, label in (("h2h", "Season head-to-head"), ("bullpen", "Bullpens"),
                       ("team_ranks", "Team offensive ranks"),
                       ("home_away", "Home/away splits")):
        v = g.get(key)
        if isinstance(v, str) and v.strip():
            facts.append(f"{label}: {v.strip()}.")

    return (
        _ANALYST_RULES
        + " Return ONLY a JSON object with exactly these three string keys, each "
        "2-3 sentences that interpret the matchup rather than recap numbers:\n"
        '  "moneyline": weigh the starting-pitcher edge and offensive context, '
        "name the key factor and the main risk, and commit to a side.\n"
        '  "run_line": judge which team is likelier to win (or stay within) by '
        "multiple runs, and why.\n"
        '  "run_total": tie the park, both arsenals, and the pitchers\' form to '
        "the scoring environment, then commit to over or under.\n\nFACTS: "
        + " ".join(facts)
    )


def _parse_bet_analysis(text: str | None) -> dict:
    if not text:
        return {}
    import json as _json
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    try:
        obj = _json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, _json.JSONDecodeError):
        return {}
    keys = ("moneyline", "run_line", "run_total")
    out = {k: (str(obj.get(k)).strip() if obj.get(k) else "") for k in keys}
    return out if any(out.values()) else {}


def get_game_bet_analysis(sport: str, g: dict) -> dict:
    """Return {moneyline, run_line, run_total} -- 2-3 sentence Groq analyses
    per bet type, cached per game per day.  Empty dict on failure."""
    gid = _game_id(g)
    today = _today_et()
    cache_key = f"ai_game_bets_{(sport or 'mlb').lower()}_{gid}" if gid else None
    if cache_key:
        try:
            from . import db
            if db.is_supabase():
                row = db.cache_get(cache_key)
                if isinstance(row, dict):
                    d = row.get("data") if isinstance(row.get("data"), dict) else row
                    if (isinstance(d, dict) and d.get("date") == today
                            and isinstance(d.get("analysis"), dict)):
                        return d["analysis"]
        except Exception:                                                 # noqa: BLE001
            pass
    try:
        from .groq_client import generate_summary
        text = generate_summary(_game_bets_prompt(sport, g), max_tokens=400)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"game-bets analysis failed: {exc}")
        return {}
    out = _parse_bet_analysis(text)
    if out and cache_key:
        try:
            from . import db
            if db.is_supabase():
                db.cache_set(cache_key, None, today, {"date": today, "analysis": out})
        except Exception:                                                 # noqa: BLE001
            pass
    return out
