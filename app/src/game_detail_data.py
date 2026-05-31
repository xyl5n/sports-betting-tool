"""
game_detail_data.py — game lookup + view-model shaping for the /matchup
Flask page.

Extracts the 3-strategy resolver from pages/game_detail.py:_lookup_game so
the Flask /matchup port and the legacy NiceGUI page can share one
implementation.  `backend` is the imported app module (passed in, never
imported here, so this stays import-cycle free -- app.py imports this
module via src.game_detail_data, not the other way around).

The resolver is the most fragile piece in the whole page.  Production
URLs carry the schedule statsapi gamePk (e.g. "824274") while analysis
rows are keyed by the Odds API id (e.g. "427339d860a9..."), so a pure
id lookup misses most of the time and the team+date fallback (strategy
b) is what actually fires.  Port preserved verbatim -- no
simplification.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


# ── Helpers (mirror pages/game_detail.py byte-for-byte) ────────────────────

def _norm_team_key(name) -> str:
    """Aggressive team-name normalization for cross-API matching.
    'LA Dodgers' and 'Los Angeles Dodgers' land on the same key."""
    if not name:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _commence_et_date(commence_time) -> str:
    """ISO commence_time -> ET 'YYYY-MM-DD'.  Empty string on parse failure
    so unparseable rows don't collide on the same empty-date bucket."""
    if not commence_time:
        return ""
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        return dt.astimezone(_ET).date().isoformat()
    except Exception:                                                      # noqa: BLE001
        return ""


# ── 3-strategy resolver ────────────────────────────────────────────────────

def _lookup_in_schedule(backend, sport: str, game_id: str) -> Optional[dict]:
    """Strategy (c): fall back to /api/schedule/<sport> for a game id not
    present in the in-memory analysis cache.  Walks today + yesterday +
    tomorrow because the matchup link in the slate may have been opened
    across a midnight boundary and the schedule cache is keyed by date."""
    try:
        client = backend.app.test_client()
        today = datetime.now(_ET).date()
        for offset in (0, -1, 1):
            d = (today + timedelta(days=offset)).isoformat()
            try:
                resp = client.get(f"/api/schedule/{sport}?date={d}")
                data = resp.get_json(force=True, silent=True) or {}
            except Exception:                                              # noqa: BLE001
                continue
            for g in (data.get("games") or []):
                gid = g.get("game_id") or g.get("id")
                if str(gid) == str(game_id):
                    return g
    except Exception:                                                      # noqa: BLE001
        return None
    return None


def _resolve_via_team_date(backend, sport: str, game_id: str,
                           results: list[dict]) -> Optional[dict]:
    """Strategy (b): the matchup URL carries the schedule statsapi gamePk
    but analysis rows are keyed by Odds API id -- the IDs never agree, so
    a pure id lookup misses most of the time.  This is the fallback that
    actually fires in production: resolve the gamePk -> (home, away,
    et_date) via /api/schedule, then scan analysis results for a row with
    the matching composite key.  Port preserved verbatim."""
    sched_entry = _lookup_in_schedule(backend, sport, game_id)
    if not sched_entry:
        return None
    target_home = _norm_team_key(sched_entry.get("home_team"))
    target_away = _norm_team_key(sched_entry.get("away_team"))
    target_date = _commence_et_date(sched_entry.get("commence_time"))
    if not (target_home and target_away):
        return None
    for r in results:
        game = r.get("game") or {}
        r_home = game.get("home_team") or r.get("home_team") or ""
        r_away = game.get("away_team") or r.get("away_team") or ""
        r_ct   = game.get("commence_time") or r.get("commence_time") or ""
        r_date = _commence_et_date(r_ct)
        if (
            _norm_team_key(r_home) == target_home
            and _norm_team_key(r_away) == target_away
            and (not target_date or not r_date or r_date == target_date)
        ):
            return r
    return None


def resolve_game(backend, sport: str,
                 game_id: str) -> tuple[Optional[dict], Optional[dict]]:
    """Return (raw_analysis_dict, serialized_game_dict) or (None, None).

    Three strategies in order:
      (a) Id match in _analysis_state / _wnba_analysis_state results.
          Tries r['game']['id'], r['game_id'], r['id'], r['_schedule_id']
          to handle every shape the cache has been written in.
      (b) Team-name + ET-date fallback via _resolve_via_team_date -- the
          one that actually fires in production because URLs use the
          statsapi gamePk and analysis rows use the Odds API id.
      (c) Schedule stub via _lookup_in_schedule -- catches games that
          appear on /api/schedule (e.g. no odds yet, or outside today's
          analysis window) but aren't in _analysis_state.  Returns
          (None, serialized) so the caller knows the SHAP-dependent
          picks section won't render.

    Port of pages/game_detail.py:_lookup_game (lines 147-240) -- shape
    + branching identical, only the import/state-access path is via
    `backend` instead of the local module."""
    sport = (sport or "mlb").lower()
    state = (backend._wnba_analysis_state if sport == "wnba"
             else backend._analysis_state)
    results = state.get("results") or []

    # (a) id match
    entry = next(
        (r for r in results
         if (r.get("game") or {}).get("id") == game_id
         or r.get("game_id") == game_id
         or r.get("id") == game_id
         or r.get("_schedule_id") == game_id),
        None,
    )

    # (b) team + date fallback
    if entry is None:
        entry = _resolve_via_team_date(backend, sport, game_id, results)

    # (c) schedule stub
    if entry is None:
        sched_entry = _lookup_in_schedule(backend, sport, game_id)
        if sched_entry is None:
            return None, None
        return None, sched_entry

    # Pre-serialized passthrough: skip the serialize call entirely (it
    # would KeyError on r['game']) and use the cache entry as both the
    # raw and ser return values.  Loses SHAP detail in this path -- the
    # renderers already handle gracefully.
    if "home_team" in entry and "away_team" in entry:
        return entry, dict(entry)

    raw = entry
    try:
        bankroll = float(state.get("bankroll") or (1000 if sport == "wnba" else 250))
        path = f"data/{'wnba_ledger' if sport == 'wnba' else 'ledger'}.json"
        ledger = backend.Ledger(path=path, starting_bankroll=bankroll)
        s_bank = ledger.data.get("personal_starting_bankroll", bankroll)
        if sport == "wnba":
            ser = backend._serialize_wnba(raw, bankroll, s_bank)
        else:
            ser = backend._serialize(raw, bankroll, "mlb", s_bank)
    except Exception:                                                      # noqa: BLE001
        ser = {}
    return raw, ser


# ── Display formatters used by app.py's view model ─────────────────────────

def fmt_when(iso: str) -> str:
    """ISO commence_time -> 'Sat May 31  7:05 PM ET'."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(_ET)
        return dt.strftime("%a %b %-d  %-I:%M %p ET")
    except Exception:                                                      # noqa: BLE001
        return str(iso)[:16]


def odds_str(o) -> str:
    """American odds -> '+120' / '-110' / '—'."""
    if not isinstance(o, (int, float)):
        return "—"
    n = int(o)
    return f"+{n}" if n > 0 else str(n)
