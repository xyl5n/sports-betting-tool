"""
props_client.py
===============
MLB player-prop fetcher and storage layer.

Three-tier fetch model to stay under The Odds API's 500/day cap.

Tier 1 (live, every 15 min during 11 AM–11 PM ET)
    pitcher_strikeouts, pitcher_outs, batter_hits, batter_total_bases

Tier 2 (once per day at analyze time)
    batter_home_runs, batter_rbis, pitcher_hits_allowed, pitcher_walks,
    pitcher_earned_runs, batter_walks

Tier 3 (on-demand only)
    Everything else: alternate markets, batter_runs_scored,
    batter_stolen_bases, pitcher_record_a_win, etc.

Storage
-------
Every successful fetch writes to BOTH:
  - .cache/props_mlb_{YYYY-MM-DD}.json  (Railway-ephemeral local cache)
  - Supabase app_cache row keyed `props_mlb_{YYYY-MM-DD}`

On boot the client restores from Supabase when the local file is missing
so a Railway redeploy doesn't lose today's props.

Logging
-------
Every fetch / parse / write emits a `PROPS-FETCH` stderr line with the
tier, market, status, and game count.  Grep `PROPS-FETCH` in Railway
logs to walk through any refresh end-to-end.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_BASE_URL = "https://api.the-odds-api.com/v4"
_ET       = ZoneInfo("America/New_York")

# Local cache file lives under .cache/ -- same convention pitcher_client
# and odds_client use.  Railway redeploys wipe it; Supabase is the
# durable copy.
_CACHE_DIR = Path(".cache")


# ── Tier definitions ────────────────────────────────────────────────────────

TIER_1_MARKETS: tuple[str, ...] = (
    "pitcher_strikeouts",
    "pitcher_outs",
    "batter_hits",
    "batter_total_bases",
)

TIER_2_MARKETS: tuple[str, ...] = (
    "batter_home_runs",
    "batter_rbis",
    "pitcher_hits_allowed",
    "pitcher_walks",
    "pitcher_earned_runs",
    "batter_walks",
)

TIER_3_MARKETS: tuple[str, ...] = (
    "batter_runs_scored",
    "batter_stolen_bases",
    "pitcher_record_a_win",
    "batter_strikeouts",
    "pitcher_strikeouts_alternate",
    "batter_hits_alternate",
    "batter_total_bases_alternate",
    "batter_home_runs_alternate",
)

ALL_PITCHER_MARKETS: frozenset[str] = frozenset(
    m for m in (*TIER_1_MARKETS, *TIER_2_MARKETS, *TIER_3_MARKETS)
    if m.startswith("pitcher_")
)
ALL_BATTER_MARKETS: frozenset[str] = frozenset(
    m for m in (*TIER_1_MARKETS, *TIER_2_MARKETS, *TIER_3_MARKETS)
    if m.startswith("batter_")
)


# ── Logging + low-level fetch ───────────────────────────────────────────────

def _log(msg: str) -> None:
    """Tagged stderr line.  Grep `PROPS-FETCH` in Railway logs to walk
    every fetch + parse + write step."""
    print(f"PROPS-FETCH: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _cache_path(date_str: str) -> Path:
    return _CACHE_DIR / f"props_mlb_{date_str}.json"


def _supabase_key(date_str: str) -> str:
    return f"props_mlb_{date_str}"


def _fetch_with_log(url: str, label: str, timeout: int = 12) -> tuple[int, dict | list]:
    """JSON GET with explicit failure logging.  Returns (status_code,
    parsed) -- (-1, {}) on network/parse failure.  Matches the
    pitcher_client pattern so the diagnostic shape is consistent.
    """
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "sports-betting-ai/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            data = json.loads(body)
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: HTTP {resp.status} ({ms}ms, {len(body)} bytes)")
        return resp.status, data
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: HTTP {exc.code} {exc.reason} ({ms}ms)")
        return exc.code, {}
    except urllib.error.URLError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: network error reason={exc.reason!r} ({ms}ms)")
        return -1, {}
    except json.JSONDecodeError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: invalid JSON {exc.msg} ({ms}ms)")
        return -1, {}
    except Exception as exc:                                              # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: unexpected {type(exc).__name__}: {exc} ({ms}ms)")
        return -1, {}


# ── Cache I/O (local + Supabase) ────────────────────────────────────────────

def _read_local(date_str: str) -> dict | None:
    path = _cache_path(date_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local cache read failed for {date_str}: {exc}")
        return None


def _write_local(date_str: str, payload: dict) -> bool:
    path = _cache_path(date_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local cache write failed for {date_str}: {exc}")
        return False


def _read_supabase(date_str: str) -> dict | None:
    try:
        from . import db as _db
        if not _db.is_supabase():
            return None
        row = _db.cache_get(_supabase_key(date_str))
        if not isinstance(row, dict):
            return None
        # cache_get returns the wrapper row -- the actual payload sits
        # under either `data` (current Supabase schema) or at the top
        # level for older rows.
        return row.get("data") if isinstance(row.get("data"), dict) else row
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase cache read failed for {date_str}: {exc}")
        return None


def _write_supabase(date_str: str, payload: dict) -> None:
    """Fire-and-forget Supabase write.  Failures are logged but never
    raise -- a slow Supabase never blocks the fetch loop."""
    try:
        from . import db as _db
        if not _db.is_supabase():
            return
        _db.cache_set(_supabase_key(date_str), None, date_str, payload)
        _log(f"supabase wrote key={_supabase_key(date_str)} "
             f"(markets={len((payload or {}).get('markets') or {})})")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase cache write failed for {date_str}: {exc}")


def restore_from_supabase_if_missing() -> bool:
    """Boot-time helper.  If the local cache for today is missing but
    Supabase has a copy, pull it down so the props page renders
    immediately without waiting for the next 15-min refresh.

    Returns True iff a restore actually happened.
    """
    date_str = _today_et()
    if _cache_path(date_str).exists():
        return False
    payload = _read_supabase(date_str)
    if not payload:
        _log(f"boot restore: no Supabase row for {date_str} -- starting empty")
        return False
    ok = _write_local(date_str, payload)
    _log(
        f"boot restore: pulled {_supabase_key(date_str)} from Supabase "
        f"({len((payload or {}).get('markets') or {})} markets) "
        f"-- local write {'ok' if ok else 'FAILED'}"
    )
    return ok


# ── Per-market fetch + parse ────────────────────────────────────────────────

def _fetch_events_for_today(api_key: str) -> list[dict]:
    """List of upcoming/in-progress MLB events.  Required because the
    /odds/{eventId}/odds endpoint is per-event.  Returns [] on failure.
    """
    url = f"{_BASE_URL}/sports/baseball_mlb/events?apiKey={api_key}"
    status, data = _fetch_with_log(url, label="events list")
    if status != 200 or not isinstance(data, list):
        return []
    today = _today_et()
    out: list[dict] = []
    for ev in data:
        try:
            ct = ev.get("commence_time", "")
            d = datetime.fromisoformat(ct.replace("Z", "+00:00")).astimezone(_ET).date().isoformat()
            if d == today:
                out.append(ev)
        except Exception:                                                 # noqa: BLE001
            continue
    _log(f"events list: {len(out)} games for {today}")
    return out


def _fetch_market_for_event(
    api_key: str, event_id: str, market: str,
    *, regions: str = "us",
) -> list[dict]:
    """Fetch one market's lines for one game.  Returns the bookmakers
    payload (list of dicts with markets + outcomes), [] on failure.
    """
    url = (
        f"{_BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
        f"?apiKey={api_key}&regions={regions}&markets={market}"
        f"&oddsFormat=american"
    )
    status, data = _fetch_with_log(url, label=f"event={event_id} market={market}")
    if status != 200 or not isinstance(data, dict):
        return []
    return data.get("bookmakers") or []


def _flatten_market_to_props(
    event: dict, market_key: str, bookmakers: list[dict],
) -> list[dict]:
    """Collapse The Odds API's bookmaker[].markets[].outcomes[] tree
    into one dict per (player, line, side) with the best available
    odds across books.  Output shape:

        {
          event_id, commence_time, home_team, away_team,
          market: "pitcher_strikeouts" | "batter_hits" | ...,
          player_name: str,
          line:  float,
          side:  "Over" | "Under",
          best_odds: int,      # American
          best_book: str,
          all_books: [{book, odds}, ...],
        }
    """
    out: list[dict] = []
    # Keyed by (player, line, side) so we can compare odds across books.
    bucket: dict[tuple[str, float, str], dict] = {}
    for book in bookmakers:
        bname = book.get("key") or book.get("title") or ""
        for m in (book.get("markets") or []):
            if m.get("key") != market_key:
                continue
            for o in (m.get("outcomes") or []):
                try:
                    player = (o.get("description") or o.get("name") or "").strip()
                    side   = (o.get("name") or "").strip().title()
                    line   = float(o.get("point")) if o.get("point") is not None else None
                    odds   = int(o.get("price")) if o.get("price") is not None else None
                except (TypeError, ValueError):
                    continue
                if not (player and side in ("Over", "Under")
                        and line is not None and odds is not None):
                    continue
                key = (player, line, side)
                entry = bucket.get(key)
                if entry is None:
                    entry = {
                        "event_id":      event.get("id"),
                        "commence_time": event.get("commence_time"),
                        "home_team":     event.get("home_team"),
                        "away_team":     event.get("away_team"),
                        "market":        market_key,
                        "player_name":   player,
                        "line":          line,
                        "side":          side,
                        "best_odds":     odds,
                        "best_book":     bname,
                        "all_books":     [{"book": bname, "odds": odds}],
                    }
                    bucket[key] = entry
                else:
                    entry["all_books"].append({"book": bname, "odds": odds})
                    # Best odds = most favorable for the bettor
                    # (largest positive, least-negative negative).
                    if odds > entry["best_odds"]:
                        entry["best_odds"] = odds
                        entry["best_book"] = bname
    out.extend(bucket.values())
    return out


# ── Tier driver ─────────────────────────────────────────────────────────────

class PropsClient:
    """Stateful fetcher.  One instance per worker; cache lives on disk
    so the next instance picks up where this one left off.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        import os
        self.api_key = api_key or os.environ.get("ODDS_API_KEY", "")
        if not self.api_key:
            _log("WARNING: ODDS_API_KEY not set -- all fetches will no-op")

    # ── Public API ────────────────────────────────────────────────────────

    def fetch_tier_1(self) -> dict:
        """Run the Tier 1 refresh.  Returns the merged payload dict."""
        return self._fetch_markets(TIER_1_MARKETS, label="TIER1")

    def fetch_tier_2(self) -> dict:
        """Run the Tier 2 refresh.  Returns the merged payload dict."""
        return self._fetch_markets(TIER_2_MARKETS, label="TIER2")

    def fetch_tier_3(self, markets: Optional[tuple[str, ...]] = None) -> dict:
        """On-demand fetch -- caller picks which Tier-3 markets to hit
        (or pass None for all)."""
        return self._fetch_markets(markets or TIER_3_MARKETS, label="TIER3")

    def get_today_props(self) -> dict:
        """Read today's cached props.  Prefers local cache, falls back
        to Supabase.  Returns the empty-shape dict when nothing exists
        so callers can iterate `.get("markets") or {}` without guards.
        """
        date_str = _today_et()
        payload = _read_local(date_str) or _read_supabase(date_str)
        if not payload:
            return {"date": date_str, "markets": {}, "fetched_at": None}
        if not _cache_path(date_str).exists():
            _write_local(date_str, payload)
        return payload

    # ── Internals ─────────────────────────────────────────────────────────

    def _fetch_markets(self, markets: tuple[str, ...], *, label: str) -> dict:
        """Run a fetch pass over *markets*.  Merges into today's cache
        in place: existing markets are overwritten, untouched markets
        keep their last-known props (so a Tier 1 refresh doesn't wipe
        the Tier 2 batter_home_runs lines)."""
        if not self.api_key:
            _log(f"{label}: no ODDS_API_KEY -- aborting fetch")
            return self.get_today_props()
        date_str = _today_et()
        _log(f"{label} start  date={date_str}  markets={list(markets)}")

        events = _fetch_events_for_today(self.api_key)
        if not events:
            _log(f"{label}: 0 events for {date_str} -- nothing to fetch")
            return self.get_today_props()

        payload = self.get_today_props()
        all_markets: dict[str, list[dict]] = payload.get("markets") or {}

        n_props_total = 0
        for market in markets:
            market_props: list[dict] = []
            for ev in events:
                event_id = ev.get("id")
                if not event_id:
                    continue
                books = _fetch_market_for_event(self.api_key, event_id, market)
                if not books:
                    continue
                market_props.extend(_flatten_market_to_props(ev, market, books))
            all_markets[market] = market_props
            n_props_total += len(market_props)
            _log(f"{label}: market={market} -> {len(market_props)} prop(s)")

        payload = {
            "date":        date_str,
            "tier":        label,
            "markets":     all_markets,
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }
        _write_local(date_str, payload)
        _write_supabase(date_str, payload)
        _log(
            f"{label} complete  date={date_str}  "
            f"markets={len(all_markets)}  total_props={n_props_total}"
        )
        return payload


# ── Module-level singleton + scheduler entrypoints ──────────────────────────

_client: Optional[PropsClient] = None


def get_client() -> PropsClient:
    global _client
    if _client is None:
        _client = PropsClient()
    return _client


def run_tier_1_refresh() -> None:
    """APScheduler callback.  Every 15 min during 11 AM–11 PM ET."""
    try:
        get_client().fetch_tier_1()
    except Exception as exc:                                              # noqa: BLE001
        import traceback
        _log(
            f"TIER1 FAILED (scheduler swallow): {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )


def run_tier_2_refresh() -> None:
    """Called once per day at /api/analyze time.  Tier 2 markets only."""
    try:
        get_client().fetch_tier_2()
    except Exception as exc:                                              # noqa: BLE001
        import traceback
        _log(
            f"TIER2 FAILED (analyze swallow): {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
