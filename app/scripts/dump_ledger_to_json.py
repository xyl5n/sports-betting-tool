"""
dump_ledger_to_json.py
======================
One-shot helper: pull the production props ledger from Supabase and write
it to disk as a JSON file the backtest harness can read via --bets-file.

Why this exists
---------------
Until the live ledger has enough settled bets to backtest against, every
PR delta is measured against synthetic bets built from training data
(see build_synthetic_pitcher_bets.py and build_synthetic_batter_bets.py).
Synthetic backtests are deterministic and useful for relative comparison
but they:

  * use fixed half-integer lines that aren't how sportsbooks actually
    set props
  * inherit a snapshot-leakage bias (snapshots cover all training
    seasons including the future of each backtest bet)
  * carry no information about which sportsbook lines we'd actually
    have been able to take, what odds were offered, or which props
    the EV scan would have surfaced

Real settled bets accumulate in Supabase app_cache under key=props_bets
as the tracked-bet pipeline runs in production.  This script gives us
a way to snapshot that ledger to a file so we can run honest backtests
locally (and stash dated copies for PR-to-PR comparison) without
needing the Supabase package installed.

Connection
----------
Uses urllib + PostgREST directly so the script works on any machine
with the env vars set, regardless of whether the supabase-py package
is installed (it has a transitive pyiceberg dep that's flaky to build
on Windows).  Reads SUPABASE_URL + SUPABASE_KEY from env; loads .env
via python-dotenv when available.

Output format
-------------
Matches what backtest_props_model._load_bets_from_file expects:

    {
        "generated_at":    "<utc ISO>",
        "source":          "supabase:app_cache[key=props_bets]",
        "n_bets":          <int>,
        "n_settled":       <int>,
        "market_prefix":   "all" | "pitcher" | "batter",
        "bets": [
            {
                "id":             "<uuid>",
                "market":         "pitcher_strikeouts",
                "player":         "Andrew Abbott",
                "team":           "CIN",
                "line":           5.5,
                "side":           "Over" | "Under",
                "odds":           -110,
                "commence_time":  "2025-08-15T19:35:00Z",
                "event_id":       "<id>",
                "actual_value":   7,
                "result":         "win" | "loss" | "push" | "void",
                "home_team":      "CIN",     # if PR6 backtest-fidelity is wired
                "away_team":      "NYY",
                "is_home":        true,
                ...
            },
            ...
        ],
    }

The 'bets' list is whatever the live ledger persisted -- we pass each
row through unchanged.  The backtest harness filters to bets with
result != "" (settled) at load time, but this dumper writes ALL bets
by default so you can also count placed-but-unsettled volume.  Use
--settled-only to filter at dump time.

Usage
-----
    # Default: dump everything to .cache/settled_props_ledger.json
    python app/scripts/dump_ledger_to_json.py

    # Only settled bets, only pitcher markets, custom output path
    python app/scripts/dump_ledger_to_json.py \
        --settled-only \
        --market-prefix pitcher \
        --output .cache/settled_pitcher_bets.json

    # Snapshot a dated copy for PR-to-PR comparison
    python app/scripts/dump_ledger_to_json.py \
        --output .cache/ledger_snapshot_$(date -u +%Y%m%d).json

All progress lines are prefixed [dump-ledger] for easy grepping.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_DIR     = Path(".cache")
_DEFAULT_OUT   = _CACHE_DIR / "settled_props_ledger.json"
_LOCAL_LEDGER  = Path("data/props_bets.json")   # fallback when Supabase unreachable
_LEDGER_KEY    = "props_bets"                   # app_cache row key
_HTTP_TIMEOUT  = 30


def _log(msg: str) -> None:
    print(f"[dump-ledger] {msg}", flush=True, file=sys.stderr)


# ── .env loader ─────────────────────────────────────────────────────────────

def _load_dotenv_if_present() -> None:
    """Best-effort load of .env so this script works from a plain shell.
    python-dotenv is optional; absence is fine when env vars are exported
    by the shell or by the Railway runtime."""
    try:
        from dotenv import load_dotenv   # type: ignore[import-not-found]
    except ImportError:
        return
    here = Path(__file__).resolve()
    for cand in (
        Path(".env"),
        Path("app/.env"),
        here.parent / ".env",                # app/scripts/.env (unlikely)
        here.parent.parent / ".env",         # app/.env
        here.parent.parent.parent / ".env",  # repo-root .env
    ):
        if cand.exists():
            load_dotenv(cand)
            _log(f"loaded env: {cand}")
            return


# ── URL sanitiser (mirrors src/db.py to avoid PGRST125 double-slash bug) ────

def _sanitize_url(raw: str) -> str:
    """Strip path / trailing slash so concatenation with `/rest/v1/...`
    yields a clean URL.  PostgREST returns PGRST125 on any malformed
    path -- including the obvious case where the URL ends with '/'
    and the SDK adds '/rest/v1' giving '//rest/v1'."""
    from urllib.parse import urlparse
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:                  # tolerate "abc.supabase.co" sans scheme
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw.rstrip("/")


# ── Supabase REST fetch ─────────────────────────────────────────────────────

def _fetch_from_supabase(url: str, key: str) -> Optional[list[dict]]:
    """Query the app_cache row keyed 'props_bets' and return the
    embedded bets list.  Returns None when the row doesn't exist or
    the request fails; that lets the caller fall back to the local
    file rather than crashing.

    The cache row shape can be one of:
        {"key": "...", "data": {"bets": [...]}, "updated_at": "..."}
        {"key": "...", "value": "<json-encoded-string>", ...}
    Both are handled here.
    """
    endpoint = f"{url}/rest/v1/app_cache?key=eq.{_LEDGER_KEY}&select=*"
    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Accept":        "application/json",
    }
    started = time.monotonic()
    try:
        req = urllib.request.Request(endpoint, headers=headers)
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode()
            rows = json.loads(body)
        ms = int((time.monotonic() - started) * 1000)
        _log(f"supabase: GET app_cache?key={_LEDGER_KEY} -> "
             f"HTTP {resp.status} ({ms}ms, {len(body)} bytes)")
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        err_body = ""
        try:
            err_body = exc.read().decode()[:300]
        except Exception:                                                  # noqa: BLE001
            pass
        _log(f"supabase: HTTP {exc.code} {exc.reason}  ({ms}ms)  body={err_body!r}")
        return None
    except urllib.error.URLError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"supabase: network error  reason={exc.reason!r} ({ms}ms)")
        return None
    except Exception as exc:                                               # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        _log(f"supabase: {type(exc).__name__}: {exc} ({ms}ms)")
        return None

    if not rows:
        _log(f"supabase: no row found for key={_LEDGER_KEY}")
        return []

    row  = rows[0]
    data = row.get("data") if isinstance(row.get("data"), (dict, list)) else row.get("value")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            _log(f"supabase: row value not JSON ({exc})")
            return None

    if isinstance(data, dict):
        bets = data.get("bets") or []
    elif isinstance(data, list):
        bets = data
    else:
        _log(f"supabase: unexpected row shape {type(data).__name__}")
        return None

    _log(f"supabase: parsed {len(bets)} bets from app_cache row")
    return bets if isinstance(bets, list) else None


# ── Local-file fallback (dev environments without Supabase) ────────────────

def _fetch_from_local_file() -> Optional[list[dict]]:
    """Last-resort source: read data/props_bets.json (the JSON-mode
    fallback the live ledger uses when Supabase is offline)."""
    if not _LOCAL_LEDGER.exists():
        _log(f"local file not found: {_LOCAL_LEDGER}")
        return None
    try:
        raw = json.loads(_LOCAL_LEDGER.read_text(encoding="utf-8"))
    except Exception as exc:                                               # noqa: BLE001
        _log(f"local file unreadable: {exc}")
        return None
    if isinstance(raw, dict):
        bets = raw.get("bets") or []
    elif isinstance(raw, list):
        bets = raw
    else:
        bets = []
    _log(f"local file: parsed {len(bets)} bets from {_LOCAL_LEDGER}")
    return bets if isinstance(bets, list) else None


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dump the production props ledger to a JSON file "
                    "the backtest harness can read via --bets-file.",
    )
    ap.add_argument(
        "--output", type=Path, default=_DEFAULT_OUT,
        help=f"Output JSON path (default: {_DEFAULT_OUT})",
    )
    ap.add_argument(
        "--market-prefix", default="all",
        choices=["all", "pitcher", "batter"],
        help="Filter bets to only those whose market starts with this prefix "
             "(default: all)",
    )
    ap.add_argument(
        "--settled-only", action="store_true",
        help="Drop bets where result is empty/null.  When set, the harness's "
             "own settled-only filter at load time becomes a no-op.",
    )
    ap.add_argument(
        "--source", choices=["auto", "supabase", "local"], default="auto",
        help="Where to read from.  'auto' tries Supabase first then falls "
             "back to data/props_bets.json (default: auto).",
    )
    ap.add_argument(
        "--require-bets", action="store_true",
        help="Exit non-zero when the source returned zero bets, instead of "
             "writing an empty file.  Useful in CI.",
    )
    args = ap.parse_args()

    _load_dotenv_if_present()
    started = time.monotonic()
    _log(f"=== dump start ===  source={args.source}  "
         f"market_prefix={args.market_prefix}  settled_only={args.settled_only}")

    # ── Choose source ───────────────────────────────────────────────────────
    bets: Optional[list[dict]] = None
    chosen_source = "none"

    if args.source in ("auto", "supabase"):
        url_raw = os.environ.get("SUPABASE_URL", "").strip()
        key     = os.environ.get("SUPABASE_KEY", "").strip()
        if not url_raw or not key:
            _log("supabase: SUPABASE_URL / SUPABASE_KEY not set")
        else:
            url = _sanitize_url(url_raw)
            if url != url_raw:
                _log(f"supabase: URL sanitised  {url_raw!r} -> {url!r}")
            bets = _fetch_from_supabase(url, key)
            if bets is not None:
                chosen_source = "supabase"

    if bets is None and args.source in ("auto", "local"):
        bets = _fetch_from_local_file()
        if bets is not None:
            chosen_source = "local"

    if bets is None:
        _log("could not read ledger from any configured source -- aborting")
        return 1

    n_raw = len(bets)
    _log(f"raw ledger size: {n_raw} bets")

    # ── Settled-only filter ─────────────────────────────────────────────────
    n_settled = sum(1 for b in bets if (b.get("result") or "").strip())
    if args.settled_only:
        bets = [b for b in bets if (b.get("result") or "").strip()]

    # ── Market-prefix filter ────────────────────────────────────────────────
    if args.market_prefix != "all":
        prefix = f"{args.market_prefix}_"
        before = len(bets)
        bets = [b for b in bets if (b.get("market") or "").startswith(prefix)]
        _log(f"market filter {prefix!r}: {before} -> {len(bets)} bets")

    # ── Sample summary by market for the log ────────────────────────────────
    by_market: dict[str, int] = {}
    by_result: dict[str, int] = {}
    for b in bets:
        m = b.get("market") or "?"
        by_market[m] = by_market.get(m, 0) + 1
        r = (b.get("result") or "unsettled").lower()
        by_result[r] = by_result.get(r, 0) + 1
    if by_market:
        _log("market breakdown: " + ", ".join(
            f"{m}={c}" for m, c in sorted(by_market.items())
        ))
    if by_result:
        _log("result breakdown: " + ", ".join(
            f"{r}={c}" for r, c in sorted(by_result.items())
        ))

    if not bets and args.require_bets:
        _log("zero bets after filtering -- exiting non-zero per --require-bets")
        return 1

    # ── Write the snapshot ─────────────────────────────────────────────────
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "source":         f"supabase:app_cache[key={_LEDGER_KEY}]"
                            if chosen_source == "supabase"
                            else f"local:{_LOCAL_LEDGER}",
        "market_prefix":  args.market_prefix,
        "settled_only":   args.settled_only,
        "n_bets":         len(bets),
        "n_settled_in_ledger": n_settled,
        "n_raw_ledger":   n_raw,
        "bets":           bets,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    size_kb = args.output.stat().st_size // 1024
    elapsed = time.monotonic() - started
    _log(f"wrote {args.output}  ({size_kb} KB, {len(bets)} bets, "
         f"source={chosen_source}, {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
