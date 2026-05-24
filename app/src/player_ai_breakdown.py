"""
AI-powered player matchup breakdown for the player profile page.

Generates a four-section breakdown (Matchup, Trends, Arsenal/Approach or
Plate Discipline, Game Script) with the Anthropic API
(claude-sonnet-4-20250514), fed only data already computed in the app:
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
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

_MODEL = "claude-sonnet-4-20250514"
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

_SECTION_KEYS = ("matchup", "trends", "approach", "game_script")


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
    return ctx


# ── Prompt + Anthropic call ─────────────────────────────────────────────────

def _system_prompt(is_pitcher: bool) -> str:
    approach_label = ("Arsenal/Approach (K/9, BB/9, FIP, pitch effectiveness)"
                      if is_pitcher else
                      "Plate Discipline (contact rate, power profile, walk rate, approach)")
    return (
        "You are a sharp, concise MLB betting analyst. Using ONLY the JSON data "
        "provided, write a four-section breakdown for this specific player prop. "
        "Do not invent any numbers — use only what is given, and omit a point if "
        "the data is missing. Write in plain, conversational sentences. "
        "ABSOLUTELY NO markdown, no bold or asterisks, no headers, no bullet "
        "points or dashes — just clean readable prose, 2-4 sentences per section.\n\n"
        "Return ONLY a JSON object (no prose around it) with exactly these string "
        "keys:\n"
        '  "matchup": how the player performs against today\'s specific opponent — '
        "reference H2H history if present, the opponent's rank versus this prop "
        "type, and home/away (and vs LHP/RHP for batters) splits.\n"
        '  "trends": whether the player is trending up or down — compare the r7, '
        "r14, r30 and season values for the active stat and flag any meaningful "
        "recent change.\n"
        f'  "approach": {approach_label}.\n'
        '  "game_script": situational factors for this prop — park factor, '
        "lineup/role context, and any other relevant situational notes."
    )


def _call_anthropic(system: str, user: str, max_tokens: int = 900) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return (msg.content[0].text or "").strip()
    except Exception as exc:                                                # noqa: BLE001
        _log(f"anthropic call failed: {type(exc).__name__}: {exc}")
        return None


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
    # Need at least one non-empty section to be worth showing.
    if not any(out.values()):
        return None
    return out


# ── Caching ──────────────────────────────────────────────────────────────────

def _cache_key(player_id, market: str) -> str:
    return f"player_profile_{player_id}_{_today_et()}_{market}"


def _cache_read(player_id, market: str) -> dict | None:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_cache_key(player_id, market))
        if isinstance(row, dict):
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(data, dict) and any(data.get(k) for k in _SECTION_KEYS):
                return {k: data.get(k, "") for k in _SECTION_KEYS}
    except Exception:                                                       # noqa: BLE001
        pass
    return None


def _cache_write(player_id, market: str, sections: dict) -> None:
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_cache_key(player_id, market), None, _today_et(), sections)
    except Exception:                                                       # noqa: BLE001
        pass


# ── Public entry point ───────────────────────────────────────────────────────

def get_breakdown(info, games, is_pitcher, prop, market, line_f,
                  summary, opp_abbrev) -> dict | None:
    """Return {matchup, trends, approach, game_script} for this player+market,
    from cache if present (once per player/market/day) else freshly generated.
    Returns None on any failure so the UI can render nothing."""
    try:
        player_id = info.get("id")
        if not player_id or not market:
            return None
        cached = _cache_read(player_id, market)
        if cached is not None:
            return cached

        ctx = _collect_context(info, games, is_pitcher, prop, market, line_f,
                               summary, opp_abbrev)
        user = ("Generate the breakdown for this prop. Data JSON:\n"
                + json.dumps(ctx, default=str))
        text = _call_anthropic(_system_prompt(is_pitcher), user)
        sections = _parse_sections(text)
        if sections is None:
            return None
        _cache_write(player_id, market, sections)
        return sections
    except Exception as exc:                                                # noqa: BLE001
        _log(f"get_breakdown failed: {type(exc).__name__}: {exc}")
        return None


def approach_label(is_pitcher: bool) -> str:
    return "ARSENAL & APPROACH" if is_pitcher else "PLATE DISCIPLINE"
