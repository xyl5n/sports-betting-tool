"""
props_ledger.py
===============
Tracks player-prop bets placed from the Props page.

Unlike the game Ledger, props bets don't manage bankroll deductions —
they are purely a tracking and record-keeping tool.  Confidence and
predicted value are stored alongside each bet so the user can review
the model's reasoning after settlement.

Persistence
-----------
Primary:   Supabase app_cache row (key="props_bets") — survives Railway redeploys.
Fallback:  data/props_bets.json — used in dev environments without Supabase.

Each save() call writes a full snapshot: {"bets": [...]} so stale rows
self-heal on the next write.  No migrations needed.

Settlement
----------
settle_open_bets() iterates open bets whose game_time has passed and
tries to resolve the result by fetching the player's game log from the
MLB Stats API (via player_profile_client).  Bets whose game is not yet
in the log stay open and are retried on the next call.

Stat mapping (market → game-log key):
  pitcher_strikeouts   → K
  pitcher_outs         → IP * 3   (outs recorded)
  pitcher_hits_allowed → H
  pitcher_walks        → BB
  pitcher_earned_runs  → ER
  batter_hits          → H
  batter_total_bases   → TB
  batter_home_runs     → HR
  batter_rbis          → RBI
  batter_runs_scored   → R
  batter_walks         → BB
  batter_strikeouts    → SO
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_KEY  = "props_bets"
_LOCAL_PATH = Path("data/props_bets.json")

# Seconds after the scheduled game start before we attempt auto-settlement.
# 6 h covers extra-inning games and typical stat-reporting lag.
_SETTLE_DELAY_SECS = 6 * 3600

# How many days back to consider a game log entry "recent enough" to settle.
_SETTLE_WINDOW_DAYS = 3

# Market key → (game-log field, is_pitcher)
# "outs" is a special case computed from IP.
_MARKET_STAT: dict[str, tuple[str, bool]] = {
    "pitcher_strikeouts":   ("K",   True),
    "pitcher_outs":         ("IP",  True),   # actual = round(IP * 3)
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
}


def _log(msg: str) -> None:
    print(f"PROPS-LEDGER: {msg}", flush=True, file=sys.stderr)


# ── Persistence helpers ──────────────────────────────────────────────────────

def _load_from_supabase() -> list[dict]:
    try:
        from . import db
        if not db.is_supabase():
            return []
        row = db.cache_get(_CACHE_KEY)
        if isinstance(row, dict):
            data = row.get("data") or row
            bets = data.get("bets") if isinstance(data, dict) else None
            if isinstance(bets, list):
                _log(f"loaded {len(bets)} bets from Supabase")
                return bets
    except Exception as exc:                                                # noqa: BLE001
        _log(f"Supabase load failed: {exc}")
    return []


def _load_from_file() -> list[dict]:
    try:
        if _LOCAL_PATH.exists():
            raw = json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
            bets = raw.get("bets") or []
            _log(f"loaded {len(bets)} bets from local file")
            return bets
    except Exception as exc:                                                # noqa: BLE001
        _log(f"local file load failed: {exc}")
    return []


def _save_to_supabase(bets: list[dict]) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_CACHE_KEY, "mlb", today, {"bets": bets})
            _log(f"saved {len(bets)} bets to Supabase")
    except Exception as exc:                                                # noqa: BLE001
        _log(f"Supabase save failed: {exc}")


def _save_to_file(bets: list[dict]) -> None:
    try:
        _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_PATH.write_text(
            json.dumps({"bets": bets}, indent=2), encoding="utf-8"
        )
    except Exception as exc:                                                # noqa: BLE001
        _log(f"local file save failed (non-fatal): {exc}")


# ── PropsLedger ──────────────────────────────────────────────────────────────

class PropsLedger:
    """Manages the lifecycle of tracked player-prop bets."""

    def __init__(self) -> None:
        self._bets: list[dict] = []
        self._loaded: bool     = False

    # ── loading ───────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._bets  = _load_from_supabase() or _load_from_file()
        self._loaded = True

    def reload(self) -> None:
        """Force re-read from storage (useful for fresh page loads)."""
        self._loaded = False
        self._ensure_loaded()

    # ── saving ────────────────────────────────────────────────────────────────

    def save(self) -> None:
        _save_to_supabase(self._bets)
        _save_to_file(self._bets)

    # ── queries ───────────────────────────────────────────────────────────────

    def has_prop_bet(
        self,
        player: str,
        market: str,
        line: float,
        side: str,
        event_id: Optional[str] = None,
    ) -> bool:
        """Return True when this exact prop pick is already tracked."""
        self._ensure_loaded()
        for b in self._bets:
            if b.get("result"):          # settled bets don't block re-tracking
                continue
            if (
                b.get("player") == player
                and b.get("market") == market
                and abs(float(b.get("line") or 0) - float(line)) < 0.01
                and (b.get("side") or "").strip().title() == (side or "").strip().title()
                and (event_id is None or b.get("event_id") == event_id)
            ):
                return True
        return False

    def get_open_bets(self) -> list[dict]:
        self._ensure_loaded()
        bets = [b for b in self._bets if not b.get("result")]
        bets.sort(key=lambda b: b.get("placed_at", ""), reverse=True)
        return bets

    def get_history(self) -> list[dict]:
        self._ensure_loaded()
        bets = [b for b in self._bets if b.get("result")]
        bets.sort(key=lambda b: b.get("settled_at", ""), reverse=True)
        return bets

    def get_all(self) -> list[dict]:
        self._ensure_loaded()
        return list(self._bets)

    # ── writes ────────────────────────────────────────────────────────────────

    def add_prop_bet(
        self,
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
    ) -> str:
        """Add a new prop bet and persist.  Returns the generated bet ID."""
        self._ensure_loaded()
        bet_id = str(uuid.uuid4())
        self._bets.append({
            "id":              bet_id,
            "player":          player,
            "market":          market,
            "line":            float(line) if line is not None else None,
            "side":            (side or "Over").strip().title(),
            "odds":            int(odds) if odds is not None else None,
            "confidence":      round(float(confidence or 0), 4),
            "predicted_value": round(float(predicted_value), 2)
                               if predicted_value is not None else None,
            "team":            team or "",
            "event_id":        event_id,
            "commence_time":   commence_time,
            "placed_at":       datetime.now(timezone.utc).isoformat(),
            "sport":           "mlb",
            "result":          None,
            "actual_value":    None,
            "settled_at":      None,
        })
        self.save()
        _log(f"added bet {bet_id} — {player} {side} {line} ({market})")
        return bet_id

    def settle_bet(
        self,
        bet_id:       str,
        result:       str,
        actual_value: Optional[float] = None,
    ) -> bool:
        """Manually settle a single bet.  result: 'win'|'loss'|'void'."""
        self._ensure_loaded()
        for b in self._bets:
            if b["id"] == bet_id:
                b["result"]       = result.lower()
                b["actual_value"] = actual_value
                b["settled_at"]   = datetime.now(timezone.utc).isoformat()
                self.save()
                _log(
                    f"settled {bet_id} ({b.get('player')} "
                    f"{b.get('side')} {b.get('line')}) → {result.upper()}"
                    + (f" (actual={actual_value})" if actual_value is not None else "")
                )
                return True
        return False

    # ── auto-settlement ───────────────────────────────────────────────────────

    def settle_open_bets(self) -> list[dict]:
        """Attempt to auto-settle every open bet whose game time has passed.

        For each open bet:
          1. Skip if the game start + _SETTLE_DELAY_SECS is still in the future.
          2. Look up the player's MLB ID (cached in-call to avoid redundant API hits).
          3. Fetch the player's game log for the current season.
          4. Find the game log row whose date matches the bet's game date.
          5. Compare actual stat to the prop line and settle win/loss.

        Bets whose game log row is not yet available stay open and will be
        retried on the next call.

        Returns the list of newly settled bets.
        """
        self._ensure_loaded()
        open_bets = self.get_open_bets()
        if not open_bets:
            return []

        now_ts  = datetime.now(timezone.utc).timestamp()
        season  = datetime.now(timezone.utc).year
        settled: list[dict] = []

        # Cache player_id lookups within this call to avoid redundant API hits.
        _player_id_cache: dict[str, Optional[int]] = {}

        for bet in open_bets:
            # Skip bets whose game hasn't finished yet.
            commence = bet.get("commence_time")
            if commence:
                try:
                    game_ts = datetime.fromisoformat(
                        str(commence).replace("Z", "+00:00")
                    ).timestamp()
                    if now_ts < game_ts + _SETTLE_DELAY_SECS:
                        continue
                except Exception:
                    pass   # unknown format — attempt settlement anyway

            player  = bet.get("player", "")
            market  = bet.get("market", "")
            line    = float(bet.get("line") or 0)
            side    = (bet.get("side") or "Over").strip().title()
            game_date = (commence or "")[:10]   # YYYY-MM-DD

            # Market → stat field mapping
            stat_info = _MARKET_STAT.get(market)
            if stat_info is None:
                _log(f"settle: unknown market {market!r} for bet {bet['id']} — skipping")
                continue
            stat_key, is_pitcher = stat_info

            # Resolve player ID
            if player not in _player_id_cache:
                try:
                    from .player_profile_client import search_player_by_name
                    _player_id_cache[player] = search_player_by_name(player)
                except Exception as exc:                                    # noqa: BLE001
                    _log(f"settle: player ID lookup failed for {player!r}: {exc}")
                    _player_id_cache[player] = None

            player_id = _player_id_cache[player]
            if player_id is None:
                _log(f"settle: cannot resolve player ID for {player!r} — skipping")
                continue

            # Fetch game log
            try:
                from .player_profile_client import get_player_gamelog
                games = get_player_gamelog(player_id, season, is_pitcher=is_pitcher)
            except Exception as exc:                                        # noqa: BLE001
                _log(f"settle: gamelog fetch failed for {player!r}: {exc}")
                continue

            # Find the game matching the bet date
            matching = [
                g for g in games
                if g.get("date", "")[:10] == game_date
            ]
            if not matching:
                _log(
                    f"settle: no game log entry for {player!r} on {game_date} — "
                    "leaving open (game may not be reported yet)"
                )
                continue

            game = matching[-1]   # take the latest if somehow multiple on the same date

            # Extract actual stat value
            try:
                if market == "pitcher_outs":
                    # Outs = inningsPitched × 3 (IP is stored as decimal, e.g. 6.2 = 6⅔)
                    ip = float(game.get("IP") or 0)
                    actual = round(ip * 3)
                else:
                    actual = float(game.get(stat_key, 0) or 0)
            except (TypeError, ValueError) as exc:
                _log(f"settle: stat parse error for {player!r} {stat_key}: {exc}")
                continue

            # Win if: Over and actual > line, or Under and actual < line
            # Push  if actual == line (integer line with no .5 safety)
            if actual > line:
                result = "win"  if side == "Over" else "loss"
            elif actual < line:
                result = "loss" if side == "Over" else "win"
            else:
                result = "void"   # exact push on integer line

            _log(
                f"SETTLE-AUDIT: {player} {side} {line} ({market}) | "
                f"actual={actual} → {result.upper()}"
            )
            if self.settle_bet(bet["id"], result, actual):
                settled.append({**bet, "result": result, "actual_value": actual})

        return settled

    # ── aggregate stats ───────────────────────────────────────────────────────

    def get_record(self) -> dict:
        """Return aggregate W/L/void/pending counts across all bets."""
        self._ensure_loaded()
        wins   = sum(1 for b in self._bets if (b.get("result") or "") == "win")
        losses = sum(1 for b in self._bets if (b.get("result") or "") == "loss")
        voids  = sum(1 for b in self._bets if (b.get("result") or "") == "void")
        open_n = sum(1 for b in self._bets if not b.get("result"))
        total  = wins + losses
        return {
            "wins":    wins,
            "losses":  losses,
            "voids":   voids,
            "open":    open_n,
            "total":   total,
            "pct":     (wins / total) if total else None,
        }


# ── Module-level singleton ───────────────────────────────────────────────────
# Instantiated once per process; the Flask/NiceGUI layer uses this directly.

_ledger: Optional[PropsLedger] = None


def get_props_ledger() -> PropsLedger:
    """Return the module-level PropsLedger, creating it on first access."""
    global _ledger
    if _ledger is None:
        _ledger = PropsLedger()
    return _ledger
