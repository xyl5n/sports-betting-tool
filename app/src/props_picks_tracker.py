"""
props_picks_tracker.py
======================
Append-only history of every player-prop pick the user tracks from the
Props page -- the props analogue of xgb_picks_tracker / lr_picks_tracker.

Why this exists (the bug it fixes)
----------------------------------
The previous tracker (src/props_ledger.py) saved its Supabase row with
``date = today`` as the app_cache date column.  The daily stale-cache
cleaner (db.cache_delete_stale) deletes every row whose ``date`` !=
today, so the entire props history was being purged at the ET date
rollover -- tracked props vanished overnight.

This module fixes persistence two ways:
  1. Saves to Supabase under the key ``props_picks_history``.  The
     cleaner is taught (db.cache_delete_stale) to PRESERVE any key
     containing "history", so the row survives date rollovers.
  2. Mirrors to ``.cache/props_picks_history.json`` (same directory +
     ``{"picks": [...]}`` shape as the game trackers) so the boot
     health report can read it and so dev environments without
     Supabase still persist within a deploy.

Entry structure (one tracked pick)
-----------------------------------
    {
      "id":              "<uuid>",
      "player":          str,
      "market":          str,                 # e.g. "pitcher_strikeouts"
      "line":            float,
      "side":            "Over" | "Under",
      "confidence":      float,               # 0..1
      "predicted_value": float | None,
      "odds":            int | None,          # American
      "date":            "YYYY-MM-DD",        # game date
      "game":            str,                 # matchup label
      "team":            str,
      "event_id":        str | None,
      "commence_time":   str | None,
      "result":          "pending" | "won" | "lost" | "void",
      "actual_value":    float | None,        # filled at settlement
      "model_pnl":       float,               # bankroll delta on settle
      "recorded_at":     ISO-8601 UTC,
      "settled_at":      ISO-8601 UTC | None,
    }

Bankroll
--------
Props use a flat 1-unit (=$10) stake.  On settle:
  won  -> +stake * payout_multiplier (from American odds)
  lost -> -stake
  void ->  0
The running bankroll = _STARTING_BANKROLL + sum(model_pnl) and is
persisted alongside the picks list.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Same .cache/ directory + naming convention the game trackers use.
_LOCAL_PATH = Path(".cache/props_picks_history.json")
# Supabase key contains "history" so cache_delete_stale preserves it.
_CACHE_KEY  = "props_picks_history"

_STARTING_BANKROLL = 1000.0
_FLAT_STAKE        = 10.0

# Wait this many seconds past game start before attempting settlement
# (covers extra innings + stat-reporting lag).
_SETTLE_DELAY_SECS = 6 * 3600

# Market key -> (game-log field, is_pitcher).  "pitcher_outs" is special
# (actual = round(IP * 3)).  Mirrors props_ledger._MARKET_STAT.
_MARKET_STAT: dict[str, tuple[str, bool]] = {
    "pitcher_strikeouts":   ("K",   True),
    "pitcher_outs":         ("IP",  True),
    "pitcher_hits_allowed": ("H",   True),
    "pitcher_walks":        ("BB",  True),
    "pitcher_earned_runs":  ("ER",  True),
    "batter_hits":          ("H",   False),
    "batter_total_bases":   ("TB",  False),
    "batter_home_runs":     ("HR",  False),
    "batter_rbis":          ("RBI", False),
    "batter_runs_scored":   ("R",   False),
    "batter_walks":         ("BB",  False),
    "batter_strikeouts":    ("SO",  False),
    "batter_stolen_bases":  ("SB",  False),
}


def _log(msg: str) -> None:
    print(f"PROPS-PICKS: {msg}", flush=True, file=sys.stderr)


# ── Module-level state ──────────────────────────────────────────────────────

_picks: list[dict] = []
_loaded = False


# ── Persistence ─────────────────────────────────────────────────────────────

def _load_from_supabase() -> Optional[list[dict]]:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_CACHE_KEY)
        if isinstance(row, dict):
            data  = row.get("data") if isinstance(row.get("data"), dict) else row
            picks = data.get("picks") if isinstance(data, dict) else None
            if isinstance(picks, list):
                _log(f"loaded {len(picks)} picks from Supabase")
                return picks
    except Exception as exc:                                              # noqa: BLE001
        _log(f"Supabase load failed: {exc}")
    return None


def _load_from_file() -> Optional[list[dict]]:
    try:
        if _LOCAL_PATH.exists():
            raw   = json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
            picks = raw.get("picks") or []
            if isinstance(picks, list):
                _log(f"loaded {len(picks)} picks from local file")
                return picks
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local file load failed: {exc}")
    return None


def _save() -> None:
    """Write a full snapshot to BOTH Supabase + local file.  The
    Supabase ``date`` column is stamped with today so the row reads as
    fresh, but the "history" key keeps it safe from the daily cleaner
    regardless."""
    payload = {"picks": _picks, "bankroll": get_bankroll()}
    # Supabase
    try:
        from . import db
        if db.is_supabase():
            today = datetime.now(timezone.utc).date().isoformat()
            db.cache_set(_CACHE_KEY, "mlb", today, payload)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"Supabase save failed: {exc}")
    # Local file
    try:
        _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local file save failed (non-fatal): {exc}")


def _ensure_loaded() -> None:
    global _picks, _loaded
    if _loaded:
        return
    picks = _load_from_supabase()
    if picks is None:
        picks = _load_from_file()
    _picks  = picks or []
    _loaded = True


def reload() -> None:
    """Force re-read from storage -- call before every page render so a
    pick tracked on another worker is visible."""
    global _loaded
    _loaded = False
    _ensure_loaded()


# ── Track (write) ───────────────────────────────────────────────────────────

def _is_dup(player: str, market: str, line: float, side: str,
            event_id: Optional[str]) -> bool:
    side_n = (side or "").strip().title()
    for p in _picks:
        if p.get("result") not in (None, "pending"):
            continue   # settled picks don't block re-tracking
        if (p.get("player") == player
                and p.get("market") == market
                and abs(float(p.get("line") or 0) - float(line)) < 0.01
                and (p.get("side") or "").strip().title() == side_n
                and (event_id is None or p.get("event_id") == event_id)):
            return True
    return False


def record_prop_pick(
    *,
    player:          str,
    market:          str,
    line:            float,
    side:            str,
    odds:            Optional[int],
    confidence:      float,
    predicted_value: Optional[float],
    team:            str,
    event_id:        Optional[str],
    commence_time:   Optional[str],
) -> Optional[str]:
    """Append a new tracked prop pick with result="pending" and persist
    immediately.  Returns the new pick id, or None when it's a duplicate
    of an already-open pick."""
    _ensure_loaded()
    if _is_dup(player, market, line, side, event_id):
        return None
    side_n   = (side or "Over").strip().title()
    game_dt  = (commence_time or "")[:10]
    pick_id  = str(uuid.uuid4())
    _picks.append({
        "id":              pick_id,
        "player":          player,
        "market":          market,
        "line":            float(line) if line is not None else None,
        "side":            side_n,
        "confidence":      round(float(confidence or 0), 4),
        "predicted_value": (round(float(predicted_value), 2)
                            if predicted_value is not None else None),
        "odds":            int(odds) if odds is not None else None,
        "date":            game_dt,
        "game":            team or "",      # matchup label from the card
        "team":            team or "",
        "event_id":        event_id,
        "commence_time":   commence_time,
        "result":          "pending",
        "actual_value":    None,
        "model_pnl":       0.0,
        "recorded_at":     datetime.now(timezone.utc).isoformat(),
        "settled_at":      None,
    })
    _save()
    _log(f"tracked {pick_id} -- {player} {side_n} {line} ({market}) [pending]")
    return pick_id


# ── Settlement ──────────────────────────────────────────────────────────────

def _payout_multiplier(odds: Optional[int]) -> float:
    """Profit per $1 staked at American *odds*.  Defaults to even money
    (1.0) when odds are missing/odd-shaped so a win still books +stake."""
    if odds is None:
        return 1.0
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return 1.0
    if o >= 100:
        return o / 100.0
    if o <= -100:
        return 100.0 / abs(o)
    return 1.0


def settle_pending() -> dict:
    """Resolve every pending pick whose game has finished against the
    player's actual box-score stat (via the MLB Stats API game log),
    update result + model_pnl + bankroll, and persist.

    Returns a summary dict:
        {settled, won, lost, void, pnl, bankroll, still_pending}
    """
    _ensure_loaded()
    pending = [p for p in _picks if (p.get("result") or "pending") == "pending"]
    if not pending:
        return {"settled": 0, "won": 0, "lost": 0, "void": 0,
                "pnl": 0.0, "bankroll": get_bankroll(), "still_pending": 0}

    now_ts = datetime.now(timezone.utc).timestamp()
    season = datetime.now(timezone.utc).year
    _player_id_cache: dict[str, Optional[int]] = {}

    n_won = n_lost = n_void = 0
    pnl_total = 0.0
    n_settled = 0

    for pick in pending:
        commence = pick.get("commence_time")
        if commence:
            try:
                game_ts = datetime.fromisoformat(
                    str(commence).replace("Z", "+00:00")
                ).timestamp()
                if now_ts < game_ts + _SETTLE_DELAY_SECS:
                    continue   # game not finished yet
            except Exception:                                             # noqa: BLE001
                pass

        market = pick.get("market", "")
        stat_info = _MARKET_STAT.get(market)
        if stat_info is None:
            _log(f"settle: unknown market {market!r} for {pick['id']} -- skipping")
            continue
        stat_key, is_pitcher = stat_info

        player    = pick.get("player", "")
        line      = float(pick.get("line") or 0)
        side      = (pick.get("side") or "Over").strip().title()
        game_date = (commence or "")[:10]

        if player not in _player_id_cache:
            try:
                from .player_profile_client import search_player_by_name
                _player_id_cache[player] = search_player_by_name(player)
            except Exception as exc:                                      # noqa: BLE001
                _log(f"settle: player id lookup failed for {player!r}: {exc}")
                _player_id_cache[player] = None
        player_id = _player_id_cache[player]
        if player_id is None:
            continue

        try:
            from .player_profile_client import get_player_gamelog
            games = get_player_gamelog(player_id, season, is_pitcher=is_pitcher)
        except Exception as exc:                                          # noqa: BLE001
            _log(f"settle: gamelog fetch failed for {player!r}: {exc}")
            continue

        matching = [g for g in games if g.get("date", "")[:10] == game_date]
        if not matching:
            continue   # box score not reported yet -- retry next pass
        game = matching[-1]

        try:
            if market == "pitcher_outs":
                actual = round(float(game.get("IP") or 0) * 3)
            else:
                actual = float(game.get(stat_key, 0) or 0)
        except (TypeError, ValueError):
            continue

        if actual > line:
            result = "won" if side == "Over" else "lost"
        elif actual < line:
            result = "lost" if side == "Over" else "won"
        else:
            result = "void"

        # Bankroll delta
        if result == "won":
            model_pnl = round(_FLAT_STAKE * _payout_multiplier(pick.get("odds")), 2)
            n_won += 1
        elif result == "lost":
            model_pnl = -_FLAT_STAKE
            n_lost += 1
        else:
            model_pnl = 0.0
            n_void += 1

        pick["result"]       = result
        pick["actual_value"] = actual
        pick["model_pnl"]    = model_pnl
        pick["settled_at"]   = datetime.now(timezone.utc).isoformat()
        pnl_total += model_pnl
        n_settled += 1
        _log(
            f"settle: {player} {side} {line} ({market}) | actual={actual} "
            f"-> {result.upper()}  pnl=${model_pnl:+.2f}"
        )

    if n_settled:
        _save()

    return {
        "settled":       n_settled,
        "won":           n_won,
        "lost":          n_lost,
        "void":          n_void,
        "pnl":           round(pnl_total, 2),
        "bankroll":      get_bankroll(),
        "still_pending": sum(1 for p in _picks
                             if (p.get("result") or "pending") == "pending"),
    }


# ── Queries ─────────────────────────────────────────────────────────────────

def get_open() -> list[dict]:
    _ensure_loaded()
    out = [p for p in _picks if (p.get("result") or "pending") == "pending"]
    out.sort(key=lambda p: p.get("recorded_at", ""), reverse=True)
    return out


def get_history() -> list[dict]:
    _ensure_loaded()
    out = [p for p in _picks if (p.get("result") or "pending") != "pending"]
    out.sort(key=lambda p: p.get("settled_at", ""), reverse=True)
    return out


def get_all() -> list[dict]:
    _ensure_loaded()
    return list(_picks)


def remove_pick(pick_id: str) -> bool:
    """Delete a single tracked prop pick by id from the in-memory list and
    persist (Supabase + local).  Returns True if a pick was removed."""
    global _picks
    _ensure_loaded()
    before = len(_picks)
    _picks = [p for p in _picks if p.get("id") != pick_id]
    if len(_picks) == before:
        return False
    _save()
    return True


def update_pick(
    pick_id: str,
    *,
    odds=None,
    line=None,
    amount=None,      # noqa: ARG001 (props are flat-stake; accepted + ignored)
    actual_payout=None,
    notes=None,
) -> Optional[dict]:
    """Edit fields on a single tracked prop pick and persist.  Only the
    provided fields are changed.  Props use a flat-stake tracker (no
    personal bankroll), so editing never mutates a bankroll.  Returns the
    updated pick, or None if not found."""
    _ensure_loaded()
    pick = next((p for p in _picks if p.get("id") == pick_id), None)
    if pick is None:
        return None
    if odds is not None:
        try:
            pick["odds"] = int(odds)
        except (TypeError, ValueError):
            pass
    if line is not None:
        try:
            pick["line"] = float(line)
        except (TypeError, ValueError):
            pass
    if notes is not None:
        pick["notes"] = str(notes)
    if actual_payout is not None:
        try:
            pick["actual_payout"] = round(float(actual_payout), 2)
        except (TypeError, ValueError):
            pass
    _save()
    return pick


def add_manual_pick(
    *,
    player: str,
    market: str,
    line: float,
    side: str,
    odds=None,
    confidence: float = 0.0,
    predicted_value=None,
    team: str = "",
    event_id=None,
    commence_time=None,
    notes=None,
) -> Optional[str]:
    """Thin wrapper over record_prop_pick for the manual Add-Bet flow that
    also stamps an optional note.  Returns the new pick id (or None if the
    pick is a duplicate)."""
    pid = record_prop_pick(
        player=player, market=market, line=line, side=side, odds=odds,
        confidence=confidence, predicted_value=predicted_value,
        team=team, event_id=event_id, commence_time=commence_time,
    )
    if pid is not None and notes:
        _ensure_loaded()
        pick = next((p for p in _picks if p.get("id") == pid), None)
        if pick is not None:
            pick["notes"] = str(notes)
            _save()
    return pid


def get_record() -> dict:
    _ensure_loaded()
    won  = sum(1 for p in _picks if p.get("result") == "won")
    lost = sum(1 for p in _picks if p.get("result") == "lost")
    void = sum(1 for p in _picks if p.get("result") == "void")
    pend = sum(1 for p in _picks if (p.get("result") or "pending") == "pending")
    total = won + lost
    return {
        "wins":   won,
        "losses": lost,
        "voids":  void,
        "open":   pend,
        "total":  total,
        "pct":    (won / total) if total else None,
    }


def get_bankroll() -> float:
    _ensure_loaded()
    return round(_STARTING_BANKROLL + sum(
        float(p.get("model_pnl") or 0.0) for p in _picks
    ), 2)


def get_summary() -> dict:
    """For the boot health report -- count + pending + file presence."""
    _ensure_loaded()
    size = _LOCAL_PATH.stat().st_size if _LOCAL_PATH.exists() else 0
    return {
        "count":    len(_picks),
        "pending":  sum(1 for p in _picks if (p.get("result") or "pending") == "pending"),
        "settled":  sum(1 for p in _picks if (p.get("result") or "pending") != "pending"),
        "bankroll": get_bankroll(),
        "path":     str(_LOCAL_PATH),
        "exists":   _LOCAL_PATH.exists(),
        "size":     size,
    }
