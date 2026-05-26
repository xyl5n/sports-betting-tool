"""
AI-powered player matchup breakdown for the player profile page.

Generates a four-section breakdown (Matchup, Trends, Arsenal/Approach or
Plate Discipline, Game Script) with Groq (llama-3.1-8b-instant) via the
shared src/groq_client.py, fed only data already computed in the app:
rolling snapshot windows (r7/r14/r30/season), today's line + model
prediction, opponent rank vs the prop type, H2H game log, L5/L10/L20/season
hit rates, park factor, home/away splits, and pitcher handedness (batters).

Cached in Supabase app_cache keyed player_profile_{player_id}_{date}_{market}
so it generates once per player per market per day; subsequent same-day
loads serve from cache.  Every public path is best-effort: on ANY failure
(no key, network, bad JSON) get_breakdown returns None so the page shows
nothing instead of an error.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Active-market -> rolling-snapshot stat key (matches props_model windows).
_MARKET_STAT = {
    "pitcher_strikeouts":   "K",
    "pitcher_outs":         "IP",
    "pitcher_hits_allowed": "H",
    "pitcher_walks":        "BB",
    "pitcher_earned_runs":  "ER",
    "batter_hits":          "H",
    "batter_total_bases":   "TB",
    "batter_home_runs":     "HR",
    "batter_rbis":          "RBI",
    "batter_runs_scored":   "R",
    "batter_walks":         "BB",
    "batter_strikeouts":    "SO",
}

_SECTION_KEYS = ("verdict_tier", "verdict", "matchup", "trends", "approach", "game_script")

# The five badge tiers, in agree -> disagree order.  The AI returns one as
# "verdict_tier"; the badge is rendered from THAT (not from model confidence)
# so the badge and the written verdict can never point in opposite directions.
_VERDICT_TIERS = ("Strong Lean", "Lean", "Neutral", "Fade", "Strong Fade")
_TIER_COLOR = {
    "Strong Lean": "pos", "Lean": "pos",
    "Neutral": "warn",
    "Fade": "neg", "Strong Fade": "neg",
}


def verdict_label(confidence, edge=None) -> tuple[str, str]:
    """Map model confidence (the prob the picked side hits) to a verdict
    badge + colour token: Strong Lean / Lean (pos) · Neutral (warn) ·
    Fade / Strong Fade (neg)."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return ("Neutral", "warn")
    if c >= 0.62:
        return ("Strong Lean", "pos")
    if c >= 0.565:
        return ("Lean", "pos")
    if c >= 0.50:
        return ("Neutral", "warn")
    if c >= 0.45:
        return ("Fade", "neg")
    return ("Strong Fade", "neg")


def _log(msg: str) -> None:
    print(f"[player-ai] {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _round(x, n: int = 2):
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return None


# ── Context assembly (all best-effort) ──────────────────────────────────────

def _snapshot_windows(prop: dict, market: str, is_pitcher: bool) -> dict:
    """r7 / r14 / r30 / season values for the active stat (+ pitcher rates)."""
    out: dict = {}
    try:
        from . import props_model as _pm
        snap = (_pm._lookup_pitcher_snapshot(prop) if is_pitcher
                else _pm._lookup_batter_snapshot(prop))
        feats = (snap or {}).get("features") or {}
        stat = _MARKET_STAT.get(market)
        if stat:
            for w in ("r7", "r14", "r30", "szn"):
                v = feats.get(f"{w}_{stat}")
                if v is not None:
                    out[w if w != "szn" else "season"] = _round(v)
        if is_pitcher:
            for rate in ("k_per_9", "bb_per_9"):
                for w in ("r7", "r14", "szn"):
                    v = feats.get(f"{w}_{rate}")
                    if v is not None:
                        out[f"{'season' if w == 'szn' else w}_{rate}"] = _round(v)
    except Exception:                                                       # noqa: BLE001
        pass
    return out


def _home_away_splits(games: list, market: str, is_pitcher: bool) -> dict:
    try:
        from .player_profile_client import gamelog_stat_value
        from . import props_model as _pm  # noqa: F401  (ensure importable)
        stat_key = _MARKET_STAT.get(market) or ("K" if is_pitcher else "H")
        home = [gamelog_stat_value(g, stat_key) for g in games if g.get("is_home")]
        away = [gamelog_stat_value(g, stat_key) for g in games if not g.get("is_home")]
        def avg(xs):
            return _round(sum(xs) / len(xs)) if xs else None
        return {"home_avg": avg(home), "home_games": len(home),
                "away_avg": avg(away), "away_games": len(away)}
    except Exception:                                                       # noqa: BLE001
        return {}


def _pitcher_hand_for_batter(prop: dict, player_name: str) -> str | None:
    try:
        from .player_profile_client import get_batter_vs_pitcher
        data = get_batter_vs_pitcher(prop, player_name) or {}
        h = (data.get("pitcher_hand") or "").strip()
        return h or None
    except Exception:                                                       # noqa: BLE001
        return None


def _prune(obj):
    """Recursively drop missing values so the JSON we hand Groq contains only
    real facts.  Removes None, empty strings/containers, and the 'None/None'
    placeholder hit-rate strings -- otherwise the model narrates them back as
    'not available', which is exactly the clutter we're trying to kill."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            pv = _prune(v)
            if pv is None:
                continue
            if isinstance(pv, str) and pv.strip().lower() in (
                    "", "none", "none/none", "n/a", "0/0", "nan"):
                continue
            if isinstance(pv, (dict, list)) and not pv:
                continue
            out[k] = pv
        return out
    if isinstance(obj, list):
        return [p for p in (_prune(x) for x in obj) if p is not None]
    return obj


def _collect_context(info, games, is_pitcher, prop, market, line_f,
                     summary, opp_abbrev) -> dict:
    s = summary or {}
    ctx = {
        "player":          info.get("name"),
        "position":        "pitcher" if is_pitcher else "batter",
        "team":            info.get("team_abbrev") or info.get("team_name"),
        "bats":            info.get("bats"),
        "market":          market,
        "line":            line_f,
        "side":            (prop.get("side") or "Over"),
        # The model's actual pick (side + line) the verdict is judging — so
        # the AI knows which direction "Lean" vs "Fade" point.
        "model_pick":      f"{(prop.get('side') or 'Over')} {line_f}",
        "model_confidence": _round(prop.get("confidence"), 3),
        "model_predicted_value": _round(prop.get("predicted_value")),
        "opponent":        opp_abbrev,
        "home_team":       prop.get("home_team"),
        "away_team":       prop.get("away_team"),
        "hit_rates": {
            "L5":     f"{s.get('last_5_hits')}/{s.get('last_5_games')}",
            "L10":    f"{s.get('last_10_hits')}/{s.get('last_10_games')}",
            "L20":    f"{s.get('last_20_hits')}/{s.get('last_20_games')}",
            "season": f"{s.get('season_hits')}/{s.get('season_games')}",
        },
        "averages": {
            "L5":     _round(s.get("last_5_avg")),
            "L10":    _round(s.get("last_10_avg")),
            "L20":    _round(s.get("last_20_avg")),
            "season": _round(s.get("season_avg")),
        },
        "h2h_vs_opponent": {
            "avg":   _round(s.get("h2h_avg")),
            "hits":  s.get("h2h_hits"),
            "games": s.get("h2h_games"),
        },
        "rolling_windows":  _snapshot_windows(prop, market, is_pitcher),
        "home_away_splits": _home_away_splits(games, market, is_pitcher),
    }
    # Opponent rank vs this prop type (1 = toughest matchup).
    try:
        from .player_profile_client import get_opp_rank_for_prop
        ctx["opponent_rank_vs_stat"] = get_opp_rank_for_prop(opp_abbrev, market)
    except Exception:                                                       # noqa: BLE001
        ctx["opponent_rank_vs_stat"] = prop.get("opp_rank")
    # Park factor.
    try:
        from .park_factors import get_park_factors
        run_f, hr_f = get_park_factors(prop.get("home_team") or "")
        ctx["park_run_factor"] = _round(run_f, 3)
        ctx["park_hr_factor"]  = _round(hr_f, 3)
    except Exception:                                                       # noqa: BLE001
        pass
    # Opposing pitcher handedness (batters only).
    if not is_pitcher:
        hand = _pitcher_hand_for_batter(prop, info.get("name") or "")
        if hand:
            ctx["opposing_pitcher_hand"] = hand

    # ── Deeper analytical signals: pitch mix / batter-vs-pitch / Statcast
    #    percentiles / similar-player cluster.  All best-effort + cached.
    try:
        from . import ai_context as _aic
        pid = info.get("id")
        sims = _aic.similar_players(market, info.get("name") or "", limit=4)
        if sims:
            ctx["similar_players"] = [
                {"name": s.get("name"), "team": s.get("team"),
                 "similarity": _round(s.get("score"), 3)} for s in sims]
        pcts = _aic.percentile_facts(pid, is_pitcher)
        if pcts:
            ctx["statcast_percentiles"] = pcts
        if is_pitcher:
            mix = _aic.pitch_mix(pid)
            if mix:
                ctx["pitch_arsenal"] = _aic.pitch_mix_payload(mix)
        else:
            opp_pid = None
            try:
                from .player_profile_client import get_today_opposing_pitcher
                opp = get_today_opposing_pitcher(prop, info.get("name") or "") or {}
                opp_pid = opp.get("id")
                if opp.get("name"):
                    ctx["opposing_pitcher"] = {
                        "name": opp.get("name"), "hand": opp.get("hand")}
            except Exception:                                             # noqa: BLE001
                opp_pid = None
            mix = _aic.pitch_mix(opp_pid)
            if mix:
                ctx["opposing_pitcher_arsenal"] = _aic.pitch_mix_payload(mix)
            bvp = _aic.batter_vs_pitch(pid, opp_pid)
            if bvp and bvp.get("rows"):
                ctx["batter_vs_pitch_type"] = [
                    {"pitch": r.get("pitch"), "avg": r.get("avg"),
                     "slg": r.get("slg"), "k_pct": r.get("k_pct"),
                     "faced": r.get("faced")}
                    for r in bvp["rows"] if r.get("faced")]
    except Exception as exc:                                             # noqa: BLE001
        _log(f"breakdown enrichment failed: {type(exc).__name__}: {exc}")
    # Strip every missing/placeholder value so the model only ever sees real
    # facts (no None, no 'None/None' hit rates) -- this is what stops it from
    # narrating data as 'not available'.
    return _prune(ctx)


# ── Prompt + Anthropic call ─────────────────────────────────────────────────

def _system_prompt(is_pitcher: bool, pick_side: str = "Over",
                   line=None, market_label: str = "") -> str:
    pick_side = (pick_side or "Over").strip().title()
    opp_side  = "Under" if pick_side == "Over" else "Over"
    line_str  = f"{line:g}" if isinstance(line, (int, float)) else str(line or "")
    pick_str  = f"{pick_side} {line_str}".strip()
    approach_label = ("Arsenal/Approach (K/9, BB/9, FIP, pitch effectiveness)"
                      if is_pitcher else
                      "Plate Discipline (contact rate, power profile, walk rate, approach)")
    mech = ("how the pitcher's own arsenal (pitch mix %, velocity) and Statcast "
            "percentiles shape this strikeout/outs/ER projection"
            if is_pitcher else
            "whether the mechanical matchup favors the batter or the pitcher — use "
            "the opposing pitcher's pitch mix together with the batter-vs-pitch-type "
            "splits (e.g. a pitcher who throws 45% sliders against a batter hitting "
            ".180 on breaking balls is a bad matchup) and the batter's Statcast "
            "percentiles")
    return (
        "You are an experienced MLB betting analyst, not a data reader. Using ONLY "
        "the JSON data provided, reason ACROSS the signals to judge this specific "
        "player prop — connect each number to what it means for THIS matchup (e.g. "
        "'his 47% four-seam usage is exposed here because this lineup slugs .500 on "
        "fastballs') rather than restating it. Identify the strongest factors for "
        "and against, and proactively flag conflicting signals that warrant caution "
        "(e.g. high model confidence but poor recent form, or a favorable park but a "
        "tough pitch-mix matchup). When the data supports it, cross-reference the "
        "similar-player cluster. HARD RULES: do not invent numbers — use only what "
        "is given; if a fact is not in the JSON it is unknown, so simply omit it and "
        "NEVER say anything is 'not available', 'unavailable', or 'unknown'. Plain "
        "conversational sentences only: ABSOLUTELY NO markdown, asterisks, headers, "
        "bullets or dashes — 2-4 sentences per section.\n\n"
        f"THE PICK YOU ARE JUDGING: the model's pick is {pick_str} "
        f"{market_label}. Your job is a single directional call on THAT pick.\n"
        "TIER DEFINITIONS (relative to the pick side):\n"
        f'  "Strong Lean" / "Lean" = you AGREE with the model — take the {pick_side}.\n'
        f'  "Fade" / "Strong Fade" = you DISAGREE — take the {opp_side} instead.\n'
        '  "Neutral" = no strong directional view either way.\n'
        "CONSISTENCY RULES (critical):\n"
        "  - The verdict_tier and the verdict text MUST point the SAME way. If the "
        f"tier is a Lean, the text argues FOR the {pick_side}; if a Fade, the text "
        f"argues FOR the {opp_side}; if Neutral, the text explains the wash.\n"
        "  - NEVER write contradictory phrasing that mixes opposite tiers (e.g. "
        "'lean toward a fade', 'fade the lean', 'neutral lean'). Pick ONE direction "
        "and argue only that.\n"
        "  - Do not echo the tier word as filler; give the actual reasoning.\n\n"
        "Return ONLY a JSON object (no prose around it) with exactly these string "
        "keys:\n"
        '  "verdict_tier": EXACTLY one of "Strong Lean", "Lean", "Neutral", "Fade", '
        '"Strong Fade" — your single directional call on the pick above, per the '
        "definitions. You may disagree with the model's own confidence; this tier "
        "is the source of truth for both the badge and the text.\n"
        '  "verdict": 2-3 sentences that argue the SAME direction as verdict_tier '
        f"(Lean -> argue for the {pick_side}; Fade -> argue for the {opp_side}; "
        "Neutral -> explain the wash) and say why. This is the headline opinion and "
        "must never contradict verdict_tier.\n"
        '  "matchup": how the player fares against today\'s specific opponent — H2H '
        "history if present, the opponent's rank versus this prop type, home/away "
        "(and vs LHP/RHP) splits, and the opposing pitcher's arsenal where relevant.\n"
        '  "trends": whether the player is trending up or down — compare r7 / r14 / '
        "r30 / season for the active stat and flag any meaningful recent change.\n"
        f'  "approach": {approach_label}. Also assess {mech}.\n'
        '  "game_script": situational factors (park, lineup/role) AND a directional '
        "close that AGREES with verdict_tier — restate, in plain words, the same "
        "for/against call the tier makes."
    )


def _call_groq(system: str, user: str, max_tokens: int = 900,
               prefer: str = "V4") -> tuple:
    """Generate the breakdown via the budget-aware multi-model client.
    Returns (text, version_label) -- the version is the model that actually
    produced it (after any cascade).  None text on any failure."""
    try:
        from .groq_models import generate
        return generate(f"{system}\n\n{user}", prefer=prefer, max_tokens=max_tokens)
    except Exception as exc:                                                # noqa: BLE001
        _log(f"groq call failed: {type(exc).__name__}: {exc}")
        return None, None


def _parse_sections(text: str | None) -> dict | None:
    if not text:
        return None
    raw = text.strip()
    # Tolerate a fenced ```json block.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        obj = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    out = {k: (str(obj.get(k)).strip() if obj.get(k) else "") for k in _SECTION_KEYS}
    # Normalise verdict_tier to one of the five canonical labels (case/space
    # tolerant); blank it if the model returned something off-menu so the
    # renderer falls back cleanly rather than badging a bogus tier.
    tier_raw = (out.get("verdict_tier") or "").strip().lower()
    out["verdict_tier"] = next(
        (t for t in _VERDICT_TIERS if t.lower() == tier_raw), "")
    # Need at least one non-empty narrative section to be worth showing.
    if not any(out.get(k) for k in _SECTION_KEYS if k != "verdict_tier"):
        return None
    return out


def tier_color(tier: str) -> str:
    """Colour token (pos|warn|neg) for a verdict tier; warn for unknown."""
    return _TIER_COLOR.get((tier or "").strip(), "warn")


# ── AI-vs-model agreement (single source for the outline colour + the Top
#    Plays eligibility gate) ─────────────────────────────────────────────────
# The verdict_tier is defined RELATIVE TO THE MODEL'S PICK:
#   Lean / Strong Lean  -> AI backs the model's side      -> AGREE
#   Fade / Strong Fade  -> AI leans the opposite side     -> DISAGREE
#   Neutral / unknown   -> no clear directional agreement -> NEUTRAL
def agreement(tier: str | None) -> str:
    """'agree' | 'disagree' | 'neutral' for a verdict tier."""
    tt = (tier or "").strip()
    if tt in ("Lean", "Strong Lean"):
        return "agree"
    if tt in ("Fade", "Strong Fade"):
        return "disagree"
    return "neutral"


def agreement_outline_token(tier: str | None) -> str:
    """Outline colour token by AI-vs-model agreement:
    'pos' (green=agree) · 'neg' (red=disagree) · 'none' (neutral/no-colour)."""
    return {"agree": "pos", "disagree": "neg"}.get(agreement(tier), "none")


def agrees_with_model(tier: str | None) -> bool:
    """True only when the AI clearly agrees (Lean / Strong Lean) -- the Top
    Plays eligibility gate.  Fade / Strong Fade / Neutral / unknown -> False."""
    return agreement(tier) == "agree"


# ── Caching ──────────────────────────────────────────────────────────────────

# In-process mirror of generated breakdowns, keyed by the same cache key.
# The props list polls each card's breakdown to populate "as they arrive";
# without this, 150+ cards would each hit Supabase on every poll.  The
# background breakdown queue runs in this same process, so once it generates
# a breakdown the page reads it from memory instead of re-querying Supabase.
_MEM_CACHE: dict[str, dict] = {}


def _cache_key(player_id, market: str) -> str:
    return f"player_profile_{player_id}_{_today_et()}_{market}"


def _cache_read(player_id, market: str) -> dict | None:
    key = _cache_key(player_id, market)
    mem = _MEM_CACHE.get(key)
    if mem is not None:
        return mem
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(key)
        if isinstance(row, dict):
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(data, dict) and any(data.get(k) for k in _SECTION_KEYS):
                sections = {k: data.get(k, "") for k in _SECTION_KEYS}
                sections["model_version"] = data.get("model_version") or ""
                _MEM_CACHE[key] = sections
                return sections
    except Exception:                                                       # noqa: BLE001
        pass
    return None


def _cache_write(player_id, market: str, sections: dict) -> None:
    _MEM_CACHE[_cache_key(player_id, market)] = dict(sections)
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_cache_key(player_id, market), None, _today_et(), sections)
    except Exception:                                                       # noqa: BLE001
        pass


def peek_breakdown(pick: dict) -> dict | None:
    """Read-only lookup of a pick's cached breakdown (memory first, then a
    single Supabase read).  NEVER generates -- used by the props list cards
    to render a breakdown the moment it exists without triggering 150
    on-render Groq calls."""
    pid = pick.get("player_id")
    market = pick.get("market")
    if not pid:
        pid = resolve_player_id_for_pick(pick)
    if not pid or not market:
        return None
    return _cache_read(pid, market)


def peek_breakdown_mem(pick: dict) -> dict | None:
    """Memory-only lookup -- no Supabase.  Used for cheap repeat polling once
    the first (Supabase-backed) read has happened; the in-process breakdown
    queue populates _MEM_CACHE as it generates."""
    pid = pick.get("player_id")
    market = pick.get("market")
    if not pid or not market:
        return None
    return _MEM_CACHE.get(_cache_key(pid, market))


def resolve_player_id_for_pick(pick: dict):
    try:
        from .player_profile_client import search_player_by_name
        return pick.get("player_id") or search_player_by_name(pick.get("player") or "")
    except Exception:                                                       # noqa: BLE001
        return pick.get("player_id")


# ── Background breakdown queue (props list) ──────────────────────────────────
# Generates a breakdown for every current prop via the SAME generate_for_pick
# path the player page uses, sequentially with the existing 150 ms spacing.
# Cached breakdowns are skipped, so once a day's slate is generated this is a
# cheap no-op.  Lock-guarded so concurrent page loads launch it only once.
_bd_queue_lock = threading.Lock()
_BD_QUEUE_DELAY = 0.15   # 150 ms between real Groq calls (matches the AI queue)


def launch_breakdown_queue(picks: list[dict]) -> None:
    """Fire-and-forget: generate breakdowns for all *picks* (deduped by
    player+market) on a daemon thread.  No-op if a queue is already running.
    Best-effort -- never raises into the caller."""
    if not picks:
        return
    if not _bd_queue_lock.acquire(blocking=False):
        return  # a queue is already running this process

    def _run() -> None:
        generated = cached = failed = 0
        try:
            seen: set = set()
            for p in picks:
                key = (p.get("player"), p.get("market"))
                if not all(key) or key in seen:
                    continue
                seen.add(key)
                try:
                    status = generate_for_pick(p)
                except Exception:                                          # noqa: BLE001
                    status = "failed"
                if status == "generated":
                    generated += 1
                    time.sleep(_BD_QUEUE_DELAY)       # pace only real Groq calls
                elif status == "cached":
                    cached += 1
                else:
                    failed += 1
            _log(f"breakdown queue done: {generated} generated, "
                 f"{cached} cached, {failed} failed ({len(seen)} props)")
        finally:
            try:
                _bd_queue_lock.release()
            except Exception:                                              # noqa: BLE001
                pass

    threading.Thread(target=_run, daemon=True).start()





# ── Public entry point ───────────────────────────────────────────────────────

def get_breakdown(info, games, is_pitcher, prop, market, line_f,
                  summary, opp_abbrev, *, force: bool = False,
                  prefer: str = "V4") -> dict | None:
    """Return {matchup, trends, approach, game_script, model_version} for this
    player+market, from cache if present else freshly generated on *prefer*
    (V4/8B for Pass-1 volume, V1/70B for Pass-2 top props).  Returns None on
    failure so the UI can render nothing.

    force=True bypasses the cache read and regenerates + overwrites (Pass 2
    re-run on 70B, or the 'Force AI Refresh' admin button)."""
    try:
        player_id = info.get("id")
        if not player_id or not market:
            return None
        cached = _cache_read(player_id, market) if not force else None
        if cached is not None:
            return cached

        ctx = _collect_context(info, games, is_pitcher, prop, market, line_f,
                               summary, opp_abbrev)
        try:
            from pages.props import _short_market as _sm
            mlabel = _sm(market)
        except Exception:                                                 # noqa: BLE001
            mlabel = (market or "").replace("_", " ")
        user = ("Generate the breakdown for this prop. Data JSON:\n"
                + json.dumps(ctx, default=str))
        text, version = _call_groq(
            _system_prompt(is_pitcher, pick_side=(prop.get("side") or "Over"),
                           line=line_f, market_label=mlabel),
            user, prefer=prefer,
        )
        sections = _parse_sections(text)
        if sections is None:
            return None
        sections["model_version"] = version or ""   # which model produced it
        _cache_write(player_id, market, sections)
        return sections
    except Exception as exc:                                                # noqa: BLE001
        _log(f"get_breakdown failed: {type(exc).__name__}: {exc}")
        return None


def approach_label(is_pitcher: bool) -> str:
    return "ARSENAL & APPROACH" if is_pitcher else "PLATE DISCIPLINE"


def has_breakdown(player_id, market: str) -> bool:
    """True if a breakdown is already cached for this player+market today."""
    return _cache_read(player_id, market) is not None


def generate_for_pick(pick: dict, *, force: bool = False,
                      prefer: str = "V4") -> str:
    """On-demand: ensure a player breakdown exists for a scored prop pick.
    Assembles the same context the player page feeds get_breakdown() (player
    info + gamelog + summary + opponent) and generates if not already cached.
    Returns 'cached' / 'generated' / 'failed'.  Best-effort -- never raises.

    prefer selects the model (V4/8B volume, V1/70B for Pass-2 top props).
    force=True bypasses the cache and regenerates + overwrites (Pass 2 / the
    'Force AI Refresh' admin button)."""
    try:
        player = pick.get("player")
        market = pick.get("market")
        if not player or not market:
            return "failed"
        from .player_profile_client import (
            get_player_info, get_player_gamelog, get_player_prop_summary,
            get_player_today_opponent, search_player_by_name, _CURRENT_SEASON,
        )
        player_id = pick.get("player_id") or search_player_by_name(player)
        if not player_id:
            return "failed"
        if not force and _cache_read(player_id, market) is not None:
            return "cached"

        info = get_player_info(int(player_id)) or {}
        if not info.get("id"):
            info = {**info, "id": int(player_id), "name": player}
        is_pitcher = (pick.get("bucket") == "pitcher") or \
            ((info.get("position_code") or "") == "1")
        games = get_player_gamelog(int(player_id), _CURRENT_SEASON, is_pitcher=is_pitcher) or []
        if is_pitcher:
            games = [g for g in games if g.get("games_started", 0) > 0]

        try:
            line_f = float(pick.get("line"))
        except (TypeError, ValueError):
            line_f = None
        opp = pick.get("opp_abbrev") or get_player_today_opponent(player, pick)
        summary = pick.get("summary")
        if not isinstance(summary, dict):
            summary = get_player_prop_summary(
                player, market, pick.get("line"), pick.get("side") or "Over",
                opp_abbrev=opp, is_pitcher=is_pitcher, games=games,
            )

        bd = get_breakdown(info, games, is_pitcher, pick, market, line_f,
                           summary, opp, force=force, prefer=prefer)
        return "generated" if bd else "failed"
    except Exception as exc:                                              # noqa: BLE001
        _log(f"generate_for_pick failed: {type(exc).__name__}: {exc}")
        return "failed"
