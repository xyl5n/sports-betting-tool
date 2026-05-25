"""
Supabase-only ledgers for the Model and My Bets systems.

Two fully independent systems, never mixed -- each has its own bankroll
pool and its own frozen-stake bet store (see db.py ledger_* accessors and
db/migrations/2026_ledger_rebuild.sql):

    system "model"     one combined $1000 pool across all sports
    system "personal"  the My Bets bankroll ($166.55 fresh start)

Core contract (the bug this rebuild fixes):
  * A bet's STAKE is frozen the instant it is placed.  Bankroll edits,
    a new day, or the daily-limit recompute never alter an already-placed
    or settled bet's stake.  Bankroll feeds (a) the daily limit at 4 AM ET
    and (b) the sizing of NEW bets during the day -- nothing else.
  * Placement: size -> deduct from the pool -> write the bet with the
    stake, odds and side frozen.
  * Settlement (15-minute cycle): move active -> settled and apply the
    bankroll movement exactly once:
        WIN   bankroll += stake + profit   (profit from American odds)
        LOSS  bankroll unchanged           (stake already out)
        PUSH/VOID  bankroll += stake        (stake returned untouched)

All storage is Supabase via PostgREST.  No local JSON, nothing in the repo,
so values survive Railway redeploys.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from . import db

_logger = logging.getLogger(__name__)

# Fresh-start starting bankrolls.  Seeded once into Supabase if the pool
# row is absent; never overwrites a live balance.
STARTING_BANKROLL = {
    "model":    1000.0,
    "personal": 166.55,
}

# My Bets daily budget (the model has NO daily cap -- $1000 is only the
# sizing basis).  The limit is a fresh 4 AM ET snapshot off the current
# bankroll: total staked across the day stays under 20%, single bet 5%.
DAILY_BUDGET_TOTAL_PCT   = 0.20
DAILY_BUDGET_MAX_BET_PCT = 0.05

_ET = timezone(timedelta(hours=-4))   # ET (matches the app's existing offset)


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def american_profit(stake: float, odds) -> float:
    """Profit (NOT including the returned stake) on a winning bet of
    *stake* at American *odds*.  +100 -> stake; -150 -> stake*100/150."""
    try:
        s = float(stake)
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return s * (o / 100.0) if o > 0 else s * (100.0 / abs(o))


def seed_starting_bankrolls() -> dict:
    """Seed each pool's starting bankroll ONLY if its row is absent.
    Idempotent + redeploy-safe (never overwrites a live balance).
    Returns {system: seeded_bool}."""
    out = {}
    for system, start in STARTING_BANKROLL.items():
        out[system] = db.ledger_pool_seed_if_absent(system, start)
    return out


class Ledger:
    """One betting system's ledger (system='model' or 'personal')."""

    def __init__(self, system: str):
        if system not in ("model", "personal"):
            raise ValueError(f"unknown ledger system: {system!r}")
        self.system = system

    # ── bankroll ────────────────────────────────────────────────────────────
    def _pool(self) -> dict:
        return db.ledger_pool_get(self.system) or {}

    def bankroll(self) -> float:
        return float(self._pool().get("current_balance") or 0.0)

    def starting(self) -> float:
        return float(self._pool().get("starting_balance")
                     or STARTING_BANKROLL[self.system])

    def _set_balance(self, new_balance: float) -> None:
        db.ledger_pool_upsert(self.system,
                              {"current_balance": round(float(new_balance), 2)})

    def set_bankroll(self, amount: float, *, reset_starting: bool = True) -> bool:
        """Admin 'Set Bankroll': write the real balance to current_balance
        (and reset starting_balance to match so P/L re-bases off the new
        number).  Always sends current_balance, so it can never trip the
        NOT NULL constraint, and it persists to the pool the UI reads from."""
        amount = round(float(amount), 2)
        fields = {"current_balance": amount}
        if reset_starting:
            fields["starting_balance"] = amount
        return db.ledger_pool_upsert(self.system, fields)

    # ── placement (freeze stake, deduct once) ─────────────────────────────────
    def place(self, *, bet_id: str, sport: str, bet_type: str, selection: str,
              odds, stake: float, kind: str = "game",
              game_id: Optional[str] = None, player_name: Optional[str] = None,
              meta: Optional[dict] = None) -> Optional[dict]:
        """Place a bet: stake frozen, deducted from the pool, written active.

        Idempotent on bet_id -- a pick re-seen the same day is NOT
        re-placed and the bankroll is NOT deducted twice.  Returns the
        stored row, or None if it already existed / Supabase is off."""
        if not db.is_supabase() or not bet_id:
            return None
        if db.ledger_bet_exists(self.system, bet_id):
            return None
        try:
            stake = round(float(stake), 2)
        except (TypeError, ValueError):
            return None
        if stake <= 0:
            return None
        row = {
            "bet_id":      bet_id,
            "placed_date": _today_et(),
            "sport":       (sport or "").lower(),
            "kind":        kind,
            "bet_type":    bet_type,
            "selection":   selection,
            "odds":        int(odds) if odds is not None else None,
            "stake":       stake,
            "status":      "active",
            "result":      None,
            "profit":      None,
            "game_id":     str(game_id) if game_id is not None else None,
            "player_name": player_name,
            "meta":        meta or {},
            "placed_at":   _now_iso(),
        }
        if not db.ledger_bet_insert(self.system, row):
            return None
        # Deduct the frozen stake from the pool exactly once.
        self._set_balance(self.bankroll() - stake)
        return row

    # ── settlement (apply bankroll movement once) ─────────────────────────────
    def settle(self, bet: dict, result: str) -> Optional[dict]:
        """Grade an active bet and move it to settled.  *result* is one of
        win / loss / push / void (grading happens in the caller against
        final scores / stats).  Applies the bankroll movement once and
        freezes profit.  No-op if the bet is already settled."""
        if (bet.get("status") or "active") != "active":
            return None
        result = (result or "").lower()
        stake  = float(bet.get("stake") or 0.0)
        odds   = bet.get("odds")
        if result == "win":
            profit = round(american_profit(stake, odds), 2)
            self._set_balance(self.bankroll() + stake + profit)
        elif result in ("push", "void"):
            profit = 0.0
            self._set_balance(self.bankroll() + stake)   # stake returned
        else:                                             # loss
            result = "loss"
            profit = round(-stake, 2)                     # stake stays out
        patch = {
            "status":     "settled",
            "result":     result,
            "profit":     profit,
            "settled_at": _now_iso(),
        }
        db.ledger_bet_update(self.system, bet["bet_id"], patch)
        return {**bet, **patch}

    # ── reads ─────────────────────────────────────────────────────────────────
    def active_bets(self) -> list[dict]:
        return db.ledger_bets_list(self.system, status="active")

    def settled_bets(self) -> list[dict]:
        return db.ledger_bets_list(self.system, status="settled")

    # ── My Bets daily limit (4 AM ET snapshot off current bankroll) ───────────
    def daily_limit(self) -> dict:
        """Today's spending limit for the personal system.  Refreshes the
        snapshot if it is stale (not yet taken today), so the limit tracks
        the bankroll that morning.  Sizes NEW bets only -- never an
        already-placed stake."""
        pool = self._pool()
        today = _today_et()
        if pool.get("daily_limit_date") != today:
            self.refresh_daily_limit()
            pool = self._pool()
        bankroll = self.bankroll()
        # Always derive from the current saved bankroll as a floor: if the
        # stored snapshot is missing/zero (e.g. it never persisted on an
        # older build) but a real bankroll exists, the limit must still show
        # 20% of it rather than $0.
        stored = float(pool.get("daily_limit") or 0.0)
        total = stored if stored > 0 else round(bankroll * DAILY_BUDGET_TOTAL_PCT, 2)
        spent = sum(float(b.get("stake") or 0.0)
                    for b in self.active_bets() + self.settled_bets()
                    if b.get("placed_date") == today)
        return {
            "bankroll":    bankroll,
            "total":       round(total, 2),
            "max_per_bet": round(bankroll * DAILY_BUDGET_MAX_BET_PCT, 2),
            "spent":       round(spent, 2),
            "remaining":   round(max(0.0, total - spent), 2),
            "date":        pool.get("daily_limit_date"),
        }

    def refresh_daily_limit(self) -> float:
        """Take a fresh daily-limit snapshot off the CURRENT bankroll and
        store it (called at 4 AM ET, and lazily if missing for today).

        Seeds the pool first so this daily-limit-only write always patches an
        existing row (current_balance stays populated) instead of attempting
        a partial INSERT that would violate the NOT NULL constraint."""
        db.ledger_pool_seed_if_absent("personal", STARTING_BANKROLL["personal"])
        limit = round(self.bankroll() * DAILY_BUDGET_TOTAL_PCT, 2)
        db.ledger_pool_upsert("personal", {
            "daily_limit":      limit,
            "daily_limit_date": _today_et(),
        })
        return limit


def model() -> Ledger:
    return Ledger("model")


def personal() -> Ledger:
    return Ledger("personal")
