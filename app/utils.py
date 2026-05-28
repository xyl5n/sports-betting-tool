"""utils.py -- pure helper functions lifted from app.py.

Every function here has zero coupling to Flask, the scheduler, or any
mutable module-global in app.py.  They're safe to call from any module.

Two helpers depend on state-namespace constants (_MLB_TEAM_NORM and
_QUARTER_ORDINAL); those are imported from `state` rather than passed
in so the call sites in app.py can keep their bare-name invocations.

DO NOT add anything that needs `_analysis_state`, `_logger`, `_eprint`,
the Flask app, or any other app.py-specific machinery -- those helpers
stay in app.py until a separate decomposition step decides otherwise.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

# Two moved helpers need names from state.py:
#   _norm_team_name      -> _MLB_TEAM_NORM
#   _wnba_period_ordinal -> _QUARTER_ORDINAL
from state import _MLB_TEAM_NORM, _QUARTER_ORDINAL

__all__ = [
    "_ai_daily_counter_key", "_today_et", "_game_et_date",
    "_filter_stale_games", "_strip_markdown_fences", "_format_odds",
    "_py", "_correlation_impl_prob", "_espn_state_to_mlb_state",
    "_wnba_period_ordinal", "_et_date_of", "_schedule_is_postponed",
    "_schedule_priority", "_schedule_cache_key", "_team_key",
    "_no_odds_predictions_cache_key", "_match_result_id",
    "_find_analysis_row", "_american_to_prob", "_fmt_odds", "_fmt_pct",
    "_norm_team_name", "_statsapi_norm_team", "_to_float", "_norm_team",
    "_team_pair",
]

# moved from app.py:398
def _ai_daily_counter_key() -> str:
    return f"ai_calls:{_today_et()}"

# moved from app.py:728
def _today_et() -> str:
    """Return today's date string in US/Eastern (handles DST automatically)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        # Fallback for environments without zoneinfo: approximate with UTC-4 (EDT)
        return datetime.now(timezone(timedelta(hours=-4))).date().isoformat()

# moved from app.py:738
def _game_et_date(commence_time: str) -> str:
    """Return YYYY-MM-DD in ET for a game's commence_time ISO string."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return ""

# moved from app.py:748
def _filter_stale_games(games: list) -> list:
    """Drop games whose ET date is strictly before today (yesterday's leftovers)."""
    today = _today_et()
    return [g for g in games if _game_et_date(g.get("commence_time", "")) >= today]

# moved from app.py:1124
def _strip_markdown_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from a Claude response."""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    return text.strip()

# moved from app.py:1133
def _format_odds(odds_value) -> str:
    """Format an American odds value as a signed string like '+140' or '-200'."""
    if isinstance(odds_value, (int, float)):
        return f"{int(odds_value):+d}"
    return str(odds_value or "n/a")

# moved from app.py:1358
# ── Serialization helpers ─────────────────────────────────────────────────────

def _py(obj):
    """Recursively convert numpy scalars / arrays to plain Python types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _py(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_py(v) for v in obj]
    return obj

# moved from app.py:1956
# ── Correlation validation ────────────────────────────────────────────────────

def _correlation_impl_prob(odds: int) -> float:
    """American odds → raw implied probability (no vig removal)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)

# moved from app.py:2447
def _espn_state_to_mlb_state(state: str, completed: bool) -> str:
    """Map ESPN status.type.state → MLB abstractGameState vocabulary."""
    if completed or state == "post":
        return "Final"
    if state == "in":
        return "Live"
    return "Preview"

# moved from app.py:2456
def _wnba_period_ordinal(period: int) -> str:
    """1..4 → '1st'..'4th'; 5+ → 'OT', 'OT2', etc.  Matches MLB's currentInningOrdinal role."""
    if not period:
        return ""
    if period in _QUARTER_ORDINAL:
        return _QUARTER_ORDINAL[period]
    return "OT" if period == 5 else f"OT{period - 4}"

# moved from app.py:2610
# ── Full schedule view: arbitrary date, all games (with or without odds) ─────
# Used by pages/sport.py's date-nav UI.  Returns a normalized envelope
# joining the schedule fetch (MLB Stats API / ESPN scoreboard) with any
# model picks the analyze pipeline produced for the same game.
#
# Cache strategy:
#   - Local Cache  (file-backed, in-memory): 1-hour TTL same as the
#     existing per-sport schedule proxies.
#   - Supabase app_cache: 30-day TTL via the "schedule:<sport>:<date>"
#     key.  The "date" column on app_cache is set to the literal
#     "schedule" string (not the YYYY-MM-DD) so cache_delete_stale
#     (which prunes rows where date != today_et) leaves these alone.
#     Past-date schedules persist indefinitely so historical browsing
#     stays available across Railway restarts.
#
# Picks join:
#   - When date == today_et:  pull from in-memory _analysis_state /
#     _wnba_analysis_state which carries the freshest model picks.
#   - When date < today_et:   join against ledger history (settled
#     bets) so the game card can show the result + P/L.
#   - When date > today_et:   no picks (future-dated -- no analysis
#     has run yet).

def _et_date_of(iso: str) -> str:
    """Return the ET calendar date (YYYY-MM-DD) of an ISO timestamp,
    or '' on failure.  Used to group schedule games by the day they're
    actually played in Eastern time."""
    if not iso:
        return ""
    try:
        from zoneinfo import ZoneInfo as _ZI
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone(_ZI("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return str(iso)[:10]

# moved from app.py:2624
def _schedule_is_postponed(e: dict) -> bool:
    ds = (e.get("detailed_status") or "").lower()
    return "postpon" in ds or (e.get("coded_status") or "") in ("D", "DR", "PR")

# moved from app.py:2629
def _schedule_priority(e: dict) -> int:
    """Higher = the entry we'd rather keep when two represent the same
    game.  Postponed twins lose to a live/final/rescheduled entry."""
    if _schedule_is_postponed(e):
        return 0
    if e.get("is_live"):
        return 4
    st = (e.get("status") or "")
    ds = (e.get("detailed_status") or "").lower()
    if st == "Final" or "final" in ds:
        return 3
    if e.get("rescheduled_from"):
        return 2
    return 1

# moved from app.py:2764
def _schedule_cache_key(sport: str, date_str: str) -> str:
    return f"schedule:{sport}:{date_str}"

# moved from app.py:2844
def _team_key(name: str) -> str:
    """Normalize a team name for cross-API matching.  MLB Stats API and
    The Odds API both return the official full name ("Los Angeles
    Dodgers") so a lowercase + whitespace squash is enough in 99% of
    cases.  Returns "" for falsy input so two unknown teams don't
    collide on the empty string."""
    if not name:
        return ""
    return " ".join(str(name).lower().split())

# moved from app.py:3368
# ── No-odds predictions cache ────────────────────────────────────────────────
# Per-game model predictions for the no-odds path, persisted in Supabase
# app_cache so they survive Railway restarts AND so the schedule endpoint
# can serve them without re-running the (slow) GameStore + model load on
# every request.  Midnight reset pre-populates this for the new ET day's
# entire slate; the schedule endpoint also writes back on-demand for any
# game it predicts that isn't in the cache yet.

def _no_odds_predictions_cache_key(sport: str, date_str: str) -> str:
    return f"no_odds_predictions:{sport}:{date_str}"

# moved from app.py:7376
def _match_result_id(r: dict, game_id: str) -> bool:
    """True when *r* identifies the analysis result for *game_id*,
    regardless of whether it's a raw nested dict (r["game"]["id"]) or
    a flat serialized passthrough (r["game_id"] / r["id"] /
    r["_schedule_id"]).  Centralized so every /api/ledger/* +
    /api/ai/pick_analysis route can match the same way -- the bare
    r["game"]["id"] form raised KeyError("game") whenever results were
    hydrated from the daily snapshot's flat shape.
    """
    if not isinstance(r, dict):
        return False
    g_id = (r.get("game") or {}).get("id") if isinstance(r.get("game"), dict) else None
    return (
        g_id == game_id
        or r.get("game_id") == game_id
        or r.get("id") == game_id
        or r.get("_schedule_id") == game_id
    )

# moved from app.py:7396
def _find_analysis_row(state: dict, game_id: str) -> dict | None:
    """Locate the analysis row for *game_id* in *state* and return it
    normalized to the nested shape downstream routes expect.

    When the matched row is already nested (has r["game"] and
    r["prediction"]) we return it untouched.  When it's a flat
    serialized passthrough (snapshot hydration path) we synthesize
    minimal `game` and `prediction` sub-dicts from the flat fields so
    code that does `raw["game"]["home_team"]` keeps working.  Without
    this, every /api/ledger/* call on a snapshot-hydrated worker
    crashed with KeyError('game').
    """
    results = (state or {}).get("results") or []
    raw = next((r for r in results if _match_result_id(r, game_id)), None)
    if raw is None:
        return None
    # Already in the nested raw shape -- pass through untouched.
    if isinstance(raw.get("game"), dict) and isinstance(raw.get("prediction"), dict):
        return raw
    # Flat passthrough: rebuild the minimal nested view from top-level
    # serialized fields so the rest of the route can continue.  Copy
    # rather than mutate so we don't poison the in-memory cache for
    # other readers.
    out = dict(raw)
    if not isinstance(out.get("game"), dict):
        # Re-derive home_implied_prob from the away_odds + home_odds
        # pair when we have them; the route uses it for edge math.
        home_odds = raw.get("home_odds")
        away_odds = raw.get("away_odds")
        implied = raw.get("home_implied_prob")
        if implied is None and isinstance(home_odds, (int, float)) \
                and isinstance(away_odds, (int, float)):
            try:
                ho = _american_to_prob(int(home_odds))
                ao = _american_to_prob(int(away_odds))
                if ho + ao > 0:
                    implied = ho / (ho + ao)
            except Exception:                                              # noqa: BLE001
                implied = None
        out["game"] = {
            "id":                raw.get("game_id") or raw.get("id"),
            "home_team":         raw.get("home_team"),
            "away_team":         raw.get("away_team"),
            "commence_time":     raw.get("commence_time"),
            "h2h_home_odds":     home_odds,
            "h2h_away_odds":     away_odds,
            "home_implied_prob": implied if implied is not None else 0.5,
            "total_line":        (raw.get("totals") or {}).get("total_line"),
        }
    if not isinstance(out.get("prediction"), dict):
        # Best-effort: derive home_win_prob from the moneyline pick
        # fields the serializer left at the top level.
        pick_team  = raw.get("pick_team")
        pick_prob  = raw.get("pick_prob")
        home_team  = raw.get("home_team")
        if isinstance(pick_prob, (int, float)) and pick_team and home_team:
            picked_home = pick_team == home_team
            home_win = float(pick_prob) if picked_home else 1.0 - float(pick_prob)
        else:
            home_win = 0.5
        out["prediction"] = {"home_win_prob": home_win}
    return out

# moved from app.py:7460
def _american_to_prob(american: int) -> float:
    """American moneyline -> raw implied probability (0-1).  Local mirror
    of odds_client._american_to_prob so the helper above doesn't need
    to import the larger module."""
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)

# moved from app.py:8805
def _fmt_odds(o) -> str:
    """+150 / -110 style.  '?' when missing / unparseable."""
    if o is None or o == "":
        return "?"
    try:
        n = int(o)
    except (TypeError, ValueError):
        return str(o)
    return f"+{n}" if n > 0 else str(n)

# moved from app.py:8816
def _fmt_pct(p) -> str:
    try:
        return f"{float(p) * 100:.1f}%"
    except (TypeError, ValueError):
        return "?"

# moved from app.py:10225
def _norm_team_name(name: str) -> str:
    return _MLB_TEAM_NORM.get(name, name).strip().lower()

# moved from app.py:10639
                                      # schedule on repeated Force Settlement.


def _statsapi_norm_team(name) -> str:
    """Lowercase + strip non-alphanumerics so Odds API and statsapi team
    names land on the same key ('LA Dodgers' == 'Los Angeles Dodgers')."""
    if not name:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())

# moved from app.py:11325
def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

# moved from app.py:11317
def _norm_team(name) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())

# moved from app.py:11321
def _team_pair(away, home) -> str:
    return f"{_norm_team(away)}|{_norm_team(home)}"
