"""
Supabase data-access layer with JSON-file fallback.

Connection lifecycle
--------------------
At import time we read SUPABASE_URL + SUPABASE_KEY from the
environment.  If either is missing, contains the .env.example
placeholder text, OR the connection / health-check fails: we fall
back to JSON-only mode for the rest of the process lifetime and
no Supabase calls are made.

Read / write semantics ("dual-write")
-------------------------------------
- READS:   prefer Supabase; on any error return None / empty list
           and the caller falls back to its local JSON file.
- WRITES:  attempt Supabase; ALWAYS still write to the JSON file
           as a hot backup.  If Supabase fails the write is logged
           and silently swallowed -- the app never crashes from a
           failed Supabase op.

Every public function is best-effort and never raises.  Callers can
assume "data already wrote to JSON; Supabase is bonus."

The actual JSON-file writes happen in the caller (Ledger / etc.) --
this module only handles the Supabase side.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Any

_logger = logging.getLogger(__name__)

# ── Connection state (set once at import time, never mutated again) ──
_client: Optional[Any] = None
_mode: str = "json"         # "supabase" once a healthy connection is established
_init_done: bool = False


def _is_placeholder(value: str) -> bool:
    """Treat the .env.example sentinel strings as 'not configured'."""
    v = (value or "").strip().lower()
    return (
        not v
        or v.startswith("your_")
        or v == "supabase_url"
        or v == "supabase_key"
    )


def _sanitize_url(raw: str) -> str:
    """Normalize SUPABASE_URL to a bare https://<project>.supabase.co.

    supabase-py builds REST paths by string-concatenating `/rest/v1` onto
    the URL we pass in.  A trailing slash (or any path component) creates
    a double slash like `https://x.supabase.co//rest/v1/bets`, which
    PostgREST rejects with PGRST125 "Invalid path specified in request
    URL".  Strip any path/query/fragment back to scheme + host so the
    catenation always yields a clean URL.
    """
    from urllib.parse import urlparse
    raw = (raw or "").strip()
    if not raw:
        return raw
    if "://" not in raw:                  # tolerate "abc.supabase.co" without scheme
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw.rstrip("/")


def _init() -> None:
    """One-shot connection attempt.  Idempotent."""
    global _client, _mode, _init_done
    if _init_done:
        return
    _init_done = True

    url = _sanitize_url(os.getenv("SUPABASE_URL", ""))
    key = os.getenv("SUPABASE_KEY", "").strip()
    if _is_placeholder(url) or _is_placeholder(key):
        _logger.info("Supabase not configured (env vars missing/placeholder); JSON-only mode")
        return

    try:
        from supabase import create_client  # type: ignore[import-not-found]
    except Exception as exc:                # noqa: BLE001 — broad on purpose
        # The `supabase` pip package isn't installed.  Stay in JSON mode silently
        # so dev environments without it boot fine.
        _logger.info("supabase package not importable (%s); JSON-only mode", exc)
        return

    try:
        client = create_client(url, key)
        # Health-check.  If the bets table doesn't exist yet, this still
        # surfaces a known error message that helps the user.
        client.table("bets").select("id").limit(1).execute()
        globals()["_client"] = client
        globals()["_mode"] = "supabase"
        _logger.info("Supabase connected; dual-write enabled (url=%s)", url)
    except Exception as exc:                # noqa: BLE001
        _logger.warning(
            "Supabase unreachable (%s); JSON-only mode. "
            "If tables don't exist yet, run app/db/schema.sql in the SQL editor. "
            "If PGRST125, the URL was malformed -- the value we tried was %r.",
            exc, url,
        )


# ── model_picks schema migration ──────────────────────────────────────────────
# Idempotent DDL that creates the model-performance table with the correct
# schema and repairs an older/partial table (the legacy "model_picks" from the
# original schema had id/bet_type/teams/pick/... and is missing confidence /
# result / model_name etc.).  ADD COLUMN IF NOT EXISTS backfills the new
def ensure_model_picks_schema() -> None:
    """Probe the model_picks table for the restructured schema (pick_id /
    model / status columns).  PostgREST cannot run DDL, so if the columns are
    missing this just logs a clear reminder to run the migration by hand --
    db/migrations/2026_model_picks_restructure.sql -- in the Supabase SQL
    editor.  Never raises."""
    if _mode != "supabase" or _client is None:
        return
    try:
        _client.table("model_picks").select(
            "pick_id,model,status,player_name").limit(1).execute()
        return  # already on the restructured schema
    except Exception:                                                     # noqa: BLE001
        _logger.warning(
            "model_picks table is missing the restructured columns "
            "(pick_id/model/status). Run "
            "db/migrations/2026_model_picks_restructure.sql in the Supabase "
            "SQL editor — per-model tracking stays empty until then."
        )


_init()
ensure_model_picks_schema()


def is_supabase() -> bool:
    """True iff a healthy Supabase connection is in use."""
    return _mode == "supabase"


def status() -> dict:
    """Diagnostic summary — surfaced on /api/health and at startup."""
    return {
        "mode":      _mode,
        "supabase":  _mode == "supabase",
        "url_set":   bool(os.getenv("SUPABASE_URL") and not _is_placeholder(os.getenv("SUPABASE_URL", ""))),
        "key_set":   bool(os.getenv("SUPABASE_KEY") and not _is_placeholder(os.getenv("SUPABASE_KEY", ""))),
    }


# ════════════════════════════════════════════════════════════════
#  bets
# ════════════════════════════════════════════════════════════════

def upsert_bet(bet: dict) -> bool:
    """Upsert one bet row.  Returns True on success.  Never raises."""
    if not is_supabase():
        return False
    try:
        _client.table("bets").upsert(_serialize_bet(bet), on_conflict="id").execute()
        return True
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase upsert_bet(%s) failed: %s", bet.get("id"), exc)
        return False


def upsert_bets_bulk(bets: list[dict]) -> int:
    """Upsert many bets in one round-trip.  Returns count written.  Never raises."""
    if not is_supabase() or not bets:
        return 0
    try:
        rows = [_serialize_bet(b) for b in bets]
        _client.table("bets").upsert(rows, on_conflict="id").execute()
        return len(rows)
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase upsert_bets_bulk(%d) failed: %s", len(bets), exc)
        return 0


def delete_bet(bet_id: str) -> bool:
    if not is_supabase():
        return False
    try:
        _client.table("bets").delete().eq("id", bet_id).execute()
        return True
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase delete_bet(%s) failed: %s", bet_id, exc)
        return False


def list_bets(
    sport: Optional[str] = None,
    settled: Optional[bool] = None,
    limit: int = 1000,
) -> list[dict]:
    """Read bets from Supabase.  Returns [] on any error (caller falls back to JSON)."""
    if not is_supabase():
        return []
    try:
        q = _client.table("bets").select("*")
        if sport is not None:
            q = q.eq("sport", sport.lower())
        if settled is not None:
            q = q.eq("settled", bool(settled))
        return q.order("placed_at", desc=True).limit(limit).execute().data or []
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase list_bets failed: %s", exc)
        return []


def delete_bets_bulk(
    sport:     Optional[str] = None,
    confirmed: Optional[bool] = None,
) -> int:
    """Delete bets matching the given filters in one round-trip.  Used by
    the /api/admin/reset/* endpoints to wipe the model or personal slate
    out of Supabase to mirror the local ledger truncation.

    Returns the number of rows deleted (0 on Supabase off / error)."""
    if not is_supabase():
        return 0
    try:
        q = _client.table("bets").delete()
        if sport is not None:
            q = q.eq("sport", sport.lower())
        if confirmed is not None:
            q = q.eq("confirmed", bool(confirmed))
        resp = q.execute()
        return len(resp.data or [])
    except Exception as exc:                # noqa: BLE001
        _logger.warning(
            "supabase delete_bets_bulk(sport=%s, confirmed=%s) failed: %s",
            sport, confirmed, exc,
        )
        return 0


# ════════════════════════════════════════════════════════════════
#  bankroll
# ════════════════════════════════════════════════════════════════

def upsert_bankroll(sport: str, row: dict) -> bool:
    if not is_supabase():
        return False
    try:
        row = {**row, "sport": (sport or "").lower()}
        _client.table("bankroll").upsert(row, on_conflict="sport").execute()
        return True
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase upsert_bankroll(%s) failed: %s", sport, exc)
        return False


def get_bankroll(sport: str) -> Optional[dict]:
    if not is_supabase():
        return None
    try:
        r = (
            _client.table("bankroll")
            .select("*")
            .eq("sport", (sport or "").lower())
            .limit(1)
            .execute()
        )
        return (r.data or [None])[0]
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase get_bankroll(%s) failed: %s", sport, exc)
        return None


# ════════════════════════════════════════════════════════════════
#  records
# ════════════════════════════════════════════════════════════════

def upsert_records_bulk(rows: list[dict]) -> int:
    """Each row needs sport, bet_type, plus wins/losses/pushes/units_won."""
    if not is_supabase() or not rows:
        return 0
    try:
        _client.table("records").upsert(rows, on_conflict="sport,bet_type").execute()
        return len(rows)
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase upsert_records_bulk(%d) failed: %s", len(rows), exc)
        return 0


def list_records(sport: Optional[str] = None) -> list[dict]:
    if not is_supabase():
        return []
    try:
        q = _client.table("records").select("*")
        if sport is not None:
            q = q.eq("sport", sport.lower())
        return q.execute().data or []
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase list_records failed: %s", exc)
        return []


def delete_records(sport: Optional[str] = None) -> int:
    """Clear W/L tally rows from the `records` table.  Used by Reset
    Model Record so the dashboard's per-(sport, bet_type) counters
    go back to 0-0 across both local + Supabase.

    Returns rows deleted (0 on Supabase off / error)."""
    if not is_supabase():
        return 0
    try:
        q = _client.table("records").delete()
        if sport is not None:
            q = q.eq("sport", sport.lower())
        else:
            # Supabase requires a where-clause on bulk deletes -- use
            # a tautology that always matches.  Filtering by sport in
            # the typed call above is the common path; this branch
            # only runs for the cross-sport wipe.
            q = q.neq("sport", "__never__")
        resp = q.execute()
        return len(resp.data or [])
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase delete_records(sport=%s) failed: %s", sport, exc)
        return 0


def delete_model_history(
    sport: Optional[str] = None,
    model: Optional[str] = None,
) -> int:
    """Clear rows from the `model_history` table -- the Supabase mirror
    of .cache/xgb_picks_history.json, .cache/lr_picks_history.json, and
    data/nn_picks_history.json.  Used by Reset Model Record so the
    per-classifier history is wiped across local + Supabase.

    Filters:
      sport  optional 'mlb' / 'wnba' -- defaults to all sports
      model  optional 'xgb' / 'lr' / 'nn' -- defaults to all classifiers

    Returns the number of rows the call reports deleting (0 on Supabase
    off / error).  Same tautology guard as delete_records so a no-filter
    call still satisfies Supabase's required where-clause.
    """
    if not is_supabase():
        return 0
    try:
        q = _client.table("model_history").delete()
        if sport is not None:
            q = q.eq("sport", sport.lower())
        if model is not None:
            q = q.eq("model", model.lower())
        if sport is None and model is None:
            q = q.neq("model", "__never__")
        resp = q.execute()
        return len(resp.data or [])
    except Exception as exc:                # noqa: BLE001
        _logger.warning(
            "supabase delete_model_history(sport=%s, model=%s) failed: %s",
            sport, model, exc,
        )
        return 0


def delete_bankroll(sport: str) -> bool:
    """Drop the bankroll row for `sport`.  Reset-bankroll re-upserts
    a fresh row after this; deleting first guarantees a clean slate
    even if the schema gains new columns later."""
    if not is_supabase():
        return False
    try:
        _client.table("bankroll").delete().eq("sport", (sport or "").lower()).execute()
        return True
    except Exception as exc:                # noqa: BLE001
        _logger.warning("supabase delete_bankroll(%s) failed: %s", sport, exc)
        return False


# ════════════════════════════════════════════════════════════════
#  app_cache (generic key/value persistence -- snapshot + analysis
#  caches mirror to this table so they survive Railway redeploys).
#
#  Schema (run this in the Supabase SQL editor once):
#
#     create table if not exists app_cache (
#       key        text primary key,
#       sport      text,
#       date       text,
#       data       jsonb,
#       updated_at timestamptz default now()
#     );
#     create index if not exists app_cache_date_idx on app_cache (date);
#
#  Keys used by app.py today:
#     "daily_snapshot"          (sport=null, contains both MLB + WNBA)
#     "analysis_cache:mlb"      (sport="mlb")
#     "analysis_cache:wnba"     (sport="wnba")
# ════════════════════════════════════════════════════════════════

def cache_get(key: str) -> Optional[dict]:
    """Read one cache row.  Returns the full row dict (key/sport/date/data/
    updated_at) or None if not found, Supabase off, or any error."""
    if _mode != "supabase" or _client is None:
        return None
    try:
        resp = _client.table("app_cache").select("*").eq("key", key).limit(1).execute()
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_get(%s) failed: %s", key, exc)
        return None


def cache_set(key: str, sport: Optional[str], date: str, data: dict) -> bool:
    """Upsert one cache row.  Returns True on success, False on any error
    or when Supabase isn't configured.  Never raises -- callers can ignore
    the return when the local-file write is the source of truth."""
    if _mode != "supabase" or _client is None:
        return False
    try:
        row = {"key": key, "sport": sport, "date": date, "data": data}
        _client.table("app_cache").upsert(row, on_conflict="key").execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_set(%s) failed: %s", key, exc)
        return False


# ── model_picks (single per-model performance store) ──────────────────────────
# Schema (Supabase table "model_picks" -- see
# db/migrations/2026_model_picks_restructure.sql):
#   pick_id text PRIMARY KEY, sport text, model text, bet_type text,
#   status text default 'pending', pick_side text, line numeric,
#   confidence numeric, result text, game_id text, player_name text,
#   created_at timestamptz default now(), settled_at timestamptz
# pick_id is deterministic (sport:model:bet_type:game_id[:player_name]) so the
# same pick re-logged in a cycle is a no-op insert.

def model_picks_insert(rows: list[dict]) -> int:
    """Insert NEW rows only -- existing pick_ids are ignored so a finished
    result is never reset to pending.  Returns the number of rows sent."""
    if _mode != "supabase" or _client is None or not rows:
        return 0
    try:
        (_client.table("model_picks")
         .upsert(rows, on_conflict="pick_id", ignore_duplicates=True)
         .execute())
        return len(rows)
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_picks_insert(%d rows) failed: %s", len(rows), exc)
        return 0


def model_picks_upsert(rows: list[dict]) -> int:
    """Upsert full rows by pick_id (used by settlement to write back
    result / status / settled_at).  Returns rows sent."""
    if _mode != "supabase" or _client is None or not rows:
        return 0
    try:
        _client.table("model_picks").upsert(rows, on_conflict="pick_id").execute()
        return len(rows)
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_picks_upsert(%d rows) failed: %s", len(rows), exc)
        return 0


def model_picks_list(sport: Optional[str] = None,
                     model: Optional[str] = None,
                     status: Optional[str] = None) -> list[dict]:
    """List model picks, optionally filtered by sport / model / status."""
    if _mode != "supabase" or _client is None:
        return []
    try:
        q = _client.table("model_picks").select("*")
        if sport:
            q = q.eq("sport", sport)
        if model:
            q = q.eq("model", model)
        if status:
            q = q.eq("status", status)
        return q.execute().data or []
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_picks_list failed: %s", exc)
        return []


def delete_model_picks(
    sport: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
) -> int:
    """Delete rows from the model_picks table (PostgREST only), optionally
    filtered by sport / model / status.  model_picks is the single
    canonical store the home + props record / win-percentage cards read
    from, so the admin reset buttons delete from here rather than
    truncating the old per-classifier picks-history JSON files.

    A no-filter call wipes the model's entire tracked history; it
    satisfies Supabase's required where-clause via the same tautology
    guard used by delete_records / delete_model_history.

    Returns rows deleted (0 on Supabase off / error)."""
    if not is_supabase():
        return 0
    try:
        q = _client.table("model_picks").delete()
        if sport is not None:
            q = q.eq("sport", sport.lower())
        if model is not None:
            q = q.eq("model", model.lower())
        if status is not None:
            q = q.eq("status", status.lower())
        if sport is None and model is None and status is None:
            q = q.neq("pick_id", "__never__")
        resp = q.execute()
        return len(resp.data or [])
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning(
            "supabase delete_model_picks(sport=%s, model=%s, status=%s) failed: %s",
            sport, model, status, exc,
        )
        return 0


def model_picks_delete(pick_ids: list[str]) -> int:
    """Delete model_picks rows by pick_id (used by the noon re-check to
    replace a beaten pending pick).  Returns the number requested."""
    if _mode != "supabase" or _client is None or not pick_ids:
        return 0
    try:
        _client.table("model_picks").delete().in_("pick_id", list(pick_ids)).execute()
        return len(pick_ids)
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("model_picks_delete(%d) failed: %s", len(pick_ids), exc)
        return 0


# ── personal_bets (the My Bets ledger -- one JSON blob per sport) ─────────────
# Dedicated, durable home for the personal-bet ledger so it survives Railway
# redeploys / PR merges (the local JSON files get reset on every deploy).
# Schema (db/migrations/2026_personal_bets.sql):
#   sport text PRIMARY KEY, data jsonb, updated_at timestamptz default now()
# Kept entirely separate from model_picks: this drives only the personal
# bankroll + tracked-bet display, never model performance.

def personal_bets_get(sport: str) -> Optional[dict]:
    """The stored ledger JSON for *sport*, or None when absent / Supabase off."""
    if _mode != "supabase" or _client is None:
        return None
    try:
        resp = (_client.table("personal_bets").select("data")
                .eq("sport", (sport or "mlb").lower()).limit(1).execute())
        rows = resp.data or []
        data = rows[0].get("data") if rows else None
        return data if isinstance(data, dict) else None
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("personal_bets_get(%s) failed: %s", sport, exc)
        return None


def personal_bets_set(sport: str, data: dict) -> bool:
    """Upsert the full ledger JSON for *sport*.  Returns True on success."""
    if _mode != "supabase" or _client is None:
        return False
    try:
        from datetime import datetime as _dt, timezone as _tz
        _client.table("personal_bets").upsert(
            {"sport": (sport or "mlb").lower(), "data": data,
             "updated_at": _dt.now(_tz.utc).isoformat()},
            on_conflict="sport",
        ).execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("personal_bets_set(%s) failed: %s", sport, exc)
        return False


def cache_delete(key: str) -> bool:
    """Delete one cache row by key.  Returns True on success."""
    if _mode != "supabase" or _client is None:
        return False
    try:
        _client.table("app_cache").delete().eq("key", key).execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_delete(%s) failed: %s", key, exc)
        return False


def cache_delete_stale(today: str) -> int:
    """Delete every cache row whose `date` field doesn't equal *today*,
    EXCEPT durable history rows whose key contains "history" (e.g.
    ``props_picks_history``).  Those are append-only records that must
    survive the daily ET rollover -- without the carve-out the daily
    purge wiped the entire props pick history overnight.

    Returns the number of rows the call reports deleting (0 if Supabase
    off or on error).  Caller passes today's ET date string."""
    if _mode != "supabase" or _client is None:
        return 0
    try:
        resp = (
            _client.table("app_cache")
            .delete()
            .neq("date", today)
            .not_.ilike("key", "%history%")
            .execute()
        )
        return len(resp.data or [])
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_delete_stale(today=%s) failed: %s", today, exc)
        return 0


def cache_delete_keys_like(substrings: list[str]) -> tuple[int, list[str]]:
    """Delete every app_cache row whose `key` contains any of the given
    substrings (case-insensitive).  Returns (count, deleted_keys).

    Used by /api/admin/model/reset to wipe stray pick / snapshot /
    analysis cache rows whose exact keys we don't track centrally (e.g.
    keys added by future code paths).  Two-step impl: list matching
    keys first so we can return them for the audit log, then delete
    each in turn.

    Substring match uses ilike with `%foo%` wildcards.  Empty inputs
    short-circuit to (0, []).
    """
    if _mode != "supabase" or _client is None:
        return 0, []
    cleaned = [s for s in (substrings or []) if isinstance(s, str) and s.strip()]
    if not cleaned:
        return 0, []

    deleted_keys: list[str] = []
    try:
        # Two-pass: list keys (one query per substring, ORed together
        # via Python set), then delete in one batched call.
        seen: set[str] = set()
        for sub in cleaned:
            try:
                resp = (
                    _client.table("app_cache")
                    .select("key")
                    .ilike("key", f"%{sub}%")
                    .execute()
                )
            except Exception as exc:                                       # noqa: BLE001
                _logger.warning(
                    "cache_delete_keys_like list(sub=%s) failed: %s", sub, exc,
                )
                continue
            for row in (resp.data or []):
                k = row.get("key")
                if k:
                    seen.add(str(k))
        if not seen:
            return 0, []
        # Delete each key individually -- supabase-py doesn't expose a
        # single batched delete-by-list across rows, and the row count
        # is small enough that per-key delete stays under a second.
        for k in sorted(seen):
            try:
                _client.table("app_cache").delete().eq("key", k).execute()
                deleted_keys.append(k)
            except Exception as exc:                                       # noqa: BLE001
                _logger.warning("cache_delete_keys_like del(%s) failed: %s", k, exc)
        return len(deleted_keys), deleted_keys
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_delete_keys_like failed: %s", exc)
        return len(deleted_keys), deleted_keys


def cache_list_all(limit: int = 2000) -> list[dict]:
    """Return every app_cache row (key, sport, date, updated_at, data) for
    the admin Data Explorer.  Empty list when Supabase is off or on error.
    Callers that only need metadata should size + drop ``data`` themselves
    so big base64 model blobs aren't shipped to the browser."""
    if _mode != "supabase" or _client is None:
        return []
    try:
        resp = (
            _client.table("app_cache")
            .select("key, sport, date, data, updated_at")
            .limit(limit)
            .execute()
        )
        return list(resp.data or [])
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("cache_list_all failed: %s", exc)
        return []


# ════════════════════════════════════════════════════════════════
#  Serialization helpers
# ════════════════════════════════════════════════════════════════

def _serialize_bet(bet: dict) -> dict:
    """Map a Ledger bet dict → Supabase `bets` row.

    Standard columns map 1:1; everything else lands in `meta` so no
    field is lost.  Idempotent — calling it twice on the same input
    yields the same row.
    """
    home = bet.get("home_team", "")
    away = bet.get("away_team", "")
    teams = f"{away} @ {home}" if (home and away) else ""

    # Human-readable pick string for the `pick` column.
    bet_type = bet.get("bet_type", "single")
    pick_str = bet.get("bet_team") or ""
    prop_line = bet.get("prop_line")
    if bet_type == "run_line" and prop_line is not None:
        sign = "+" if prop_line > 0 else ""
        pick_str = f"{pick_str} {sign}{prop_line}".strip()
    elif bet_type == "totals":
        side = (bet.get("bet_side") or "").upper()
        if prop_line is not None:
            pick_str = f"{side} {prop_line}".strip()
        else:
            pick_str = side or pick_str
    elif bet_type == "parlay":
        pick_str = bet.get("parlay_name") or pick_str

    placed = bet.get("placed_at", "") or ""
    settled_at = bet.get("settled_at") or None
    commence = bet.get("commence_time") or ""
    date = (commence or placed)[:10] if (commence or placed) else ""

    dollar_amount = bet.get("confirmed_amount") or bet.get("model_amount") or 0.0
    edge = bet.get("edge") or 0.0

    # Units: requires starting bankroll.  Ledger doesn't store it on the bet
    # itself; we leave units NULL here and let the records-aggregator fill
    # it in if needed.  Existing UI doesn't depend on this field.
    units = bet.get("units")

    # Everything not promoted to a column lands in meta so we lose nothing.
    PROMOTED = {
        "id", "sport", "bet_type", "home_team", "away_team",
        "bet_team", "bet_side", "american_odds", "placed_at",
        "commence_time", "edge", "confidence_tier", "result",
        "confirmed_amount", "model_amount", "settled_at",
    }
    meta = {k: v for k, v in bet.items() if k not in PROMOTED}

    return {
        "id":              bet["id"],
        "date":            date,
        "sport":           (bet.get("sport") or "").lower(),
        "bet_type":        bet_type,
        "teams":           teams,
        "pick":            pick_str,
        "odds":            bet.get("american_odds"),
        "dollar_amount":   round(float(dollar_amount), 2) if dollar_amount is not None else None,
        "units":           units,
        "confidence_tier": bet.get("confidence_tier"),
        "edge_percentage": round(float(edge) * 100, 2) if edge is not None else None,
        "result":          bet.get("result"),
        "settled":         bool(bet.get("result")),
        "placed_at":       placed or None,
        "settled_at":      settled_at,
        "meta":            meta,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Rebuilt ledgers (PostgREST only) -- Model + My Bets, fully independent.
#
#  Bankroll lives ONLY here (never in a repo file), so it survives redeploys.
#  Each system has a single bankroll pool row + a frozen-stake bet store:
#     system "model"    -> model_bankroll_pool   / model_ledger_bets
#     system "personal" -> personal_bankroll_pool / personal_ledger_bets
#  A placed bet's stake is written once and never recalculated.
# ════════════════════════════════════════════════════════════════════════════

_LEDGER_POOL_TABLE = {
    "model":    "model_bankroll_pool",
    "personal": "personal_bankroll_pool",
}
_LEDGER_BET_TABLE = {
    "model":    "model_ledger_bets",
    "personal": "personal_ledger_bets",
}


def ledger_pool_get(system: str) -> Optional[dict]:
    """Return the bankroll pool row for 'model' / 'personal', or None."""
    table = _LEDGER_POOL_TABLE.get(system)
    if not is_supabase() or table is None:
        return None
    try:
        resp = _client.table(table).select("*").eq("id", system).limit(1).execute()
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_pool_get(%s) failed: %s", system, exc)
        return None


def ledger_pool_upsert(system: str, fields: dict) -> bool:
    """Persist a partial set of pool columns (current_balance /
    starting_balance / daily_limit / daily_limit_date) onto the singleton
    row (id=system).

    MUST NOT use PostgREST upsert (INSERT ... ON CONFLICT): Postgres
    evaluates the INSERT tuple's NOT NULL constraints *before* resolving the
    conflict, so a partial write that omits current_balance (e.g. the
    daily-limit recalc, which sends only daily_limit/daily_limit_date)
    inserts current_balance=NULL and throws
    `null value in column "current_balance" violates not-null constraint`
    even when the row already exists.  Therefore:
      * row exists -> UPDATE only (a patch can never trip NOT NULL),
      * row absent -> a complete INSERT, filling the NOT NULL balance
        columns from whatever was supplied so even a partial first write
        still creates a valid row.
    """
    table = _LEDGER_POOL_TABLE.get(system)
    if not is_supabase() or table is None:
        return False
    try:
        from datetime import datetime as _dt, timezone as _tz
        patch = dict(fields or {})
        patch["updated_at"] = _dt.now(_tz.utc).isoformat()

        if ledger_pool_get(system) is not None:
            _client.table(table).update(patch).eq("id", system).execute()
            return True

        # No row yet -> complete INSERT.  current_balance / starting_balance
        # are NOT NULL; backfill each from the other so a daily-limit-only
        # first write can't crash (it shouldn't happen -- callers seed first
        # -- but stay safe).
        row = {"id": system, **patch}
        cb, sb = row.get("current_balance"), row.get("starting_balance")
        if cb is None and sb is None:
            _logger.warning(
                "ledger_pool_upsert(%s): no existing row and no balance "
                "supplied; skipping insert to avoid NOT NULL violation", system)
            return False
        row["current_balance"]  = cb if cb is not None else sb
        row["starting_balance"] = sb if sb is not None else row["current_balance"]
        _client.table(table).insert(row).execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_pool_upsert(%s) failed: %s", system, exc)
        return False


def ledger_pool_seed_if_absent(system: str, starting: float) -> bool:
    """Create the pool row with current=starting=*starting* ONLY when no
    row exists yet.  Never overwrites a live balance -- this is what lets
    bankrolls survive redeploys.  Returns True iff a row was seeded."""
    if not is_supabase() or _LEDGER_POOL_TABLE.get(system) is None:
        return False
    if ledger_pool_get(system) is not None:
        return False
    return ledger_pool_upsert(system, {
        "current_balance":  round(float(starting), 2),
        "starting_balance": round(float(starting), 2),
    })


def ledger_bet_insert(system: str, row: dict) -> bool:
    """Insert one frozen bet row (upsert on bet_id so a re-run is a no-op,
    never re-deducting).  The caller deducts the stake from the pool."""
    table = _LEDGER_BET_TABLE.get(system)
    if not is_supabase() or table is None or not row:
        return False
    try:
        _client.table(table).upsert(row, on_conflict="bet_id",
                                    ignore_duplicates=True).execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_bet_insert(%s, %s) failed: %s",
                        system, row.get("bet_id"), exc)
        return False


def ledger_bet_exists(system: str, bet_id: str) -> bool:
    """True iff a bet with this id is already in the store (so placement
    can skip re-deducting an already-placed pick)."""
    table = _LEDGER_BET_TABLE.get(system)
    if not is_supabase() or table is None or not bet_id:
        return False
    try:
        resp = _client.table(table).select("bet_id").eq("bet_id", bet_id).limit(1).execute()
        return bool(resp.data)
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_bet_exists(%s, %s) failed: %s", system, bet_id, exc)
        return False


def ledger_bets_list(system: str, status: Optional[str] = None) -> list[dict]:
    """List bets for a system, optionally filtered to 'active' / 'settled'."""
    table = _LEDGER_BET_TABLE.get(system)
    if not is_supabase() or table is None:
        return []
    try:
        q = _client.table(table).select("*")
        if status:
            q = q.eq("status", status)
        return q.order("placed_at", desc=True).execute().data or []
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_bets_list(%s, %s) failed: %s", system, status, exc)
        return []


def ledger_bet_update(system: str, bet_id: str, fields: dict) -> bool:
    """Patch a single bet row (used by settlement to write result / profit /
    status / settled_at).  Never touches `stake` -- frozen at placement."""
    table = _LEDGER_BET_TABLE.get(system)
    if not is_supabase() or table is None or not bet_id:
        return False
    try:
        patch = {k: v for k, v in (fields or {}).items() if k != "stake"}
        _client.table(table).update(patch).eq("bet_id", bet_id).execute()
        return True
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_bet_update(%s, %s) failed: %s", system, bet_id, exc)
        return False


def ledger_bets_wipe(system: str, sport: Optional[str] = None) -> int:
    """Delete bet rows for a system (fresh-start wipe).  When *sport* is
    given, only that sport's rows are removed (the bankroll pool is combined
    across sports, so a single-sport wipe can only scope the bets); otherwise
    every row for the system is deleted.  Returns rows the call reports
    deleting."""
    table = _LEDGER_BET_TABLE.get(system)
    if not is_supabase() or table is None:
        return 0
    try:
        q = _client.table(table).delete()
        if sport:
            q = q.eq("sport", sport.lower())
        else:
            q = q.neq("bet_id", "__never__")
        resp = q.execute()
        return len(resp.data or [])
    except Exception as exc:                                              # noqa: BLE001
        _logger.warning("ledger_bets_wipe(%s, sport=%s) failed: %s", system, sport, exc)
        return 0

