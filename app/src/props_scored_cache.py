"""
props_scored_cache.py
=====================
Scored + enriched player-props cache.

Populated by the background scheduler (auto_props_refresh) after each
Tier-1 / Tier-2 raw-line refresh, so the props page can be a pure
cache reader and never block the event loop with synchronous
predict() calls.

Cache layout
------------
  - Local:    .cache/props_scored_mlb_{YYYY-MM-DD}.json   (Railway-ephemeral)
  - Supabase: app_cache row keyed "props_scored_mlb_{YYYY-MM-DD}" (durable)

Payload shape
-------------
    {
      "date":         "YYYY-MM-DD",
      "generated_at": "ISO-8601 UTC",
      "picks":        [<scored + enriched pick dicts>],
      "summary":      {scored, predict_err, deduped, kept},
    }

Each pick carries the model output (recommendation, confidence, edge,
predicted_value, ...) plus enrichments (opp_abbrev, opp_rank, summary
dict with season + L5/L10/L20 + H2H hit rates).

Module ownership
----------------
* score_today_props()  — write side.  Called by the scheduler.
* load_scored_props()  — read side.   Called by the page.

The page never imports anything else from this module; the scheduler
never imports load_scored_props().  Keeping the boundary clean ensures
"never compute on page load" is enforceable by inspection.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_ET        = ZoneInfo("America/New_York")
_CACHE_DIR = Path(".cache")


# ── Logging + paths ─────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """Tagged stderr line -- grep `PROPS-SCORE` in Railway logs to
    follow a scoring pass end-to-end."""
    print(f"PROPS-SCORE: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _cache_path(date_str: str) -> Path:
    return _CACHE_DIR / f"props_scored_mlb_{date_str}.json"


def _supabase_key(date_str: str) -> str:
    return f"props_scored_mlb_{date_str}"


# ── Cache I/O (local + Supabase) ────────────────────────────────────────────

def _read_local(date_str: str) -> Optional[dict]:
    path = _cache_path(date_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local read failed for {date_str}: {exc}")
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
        _log(f"local write failed for {date_str}: {exc}")
        return False


def _read_supabase(date_str: str) -> Optional[dict]:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_supabase_key(date_str))
        if not isinstance(row, dict):
            return None
        return row.get("data") if isinstance(row.get("data"), dict) else row
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase read failed for {date_str}: {exc}")
        return None


def _write_supabase(date_str: str, payload: dict) -> None:
    """Fire-and-forget Supabase write.  Failures are logged but never
    raise -- a slow Supabase never blocks the scoring pass."""
    try:
        from . import db
        if not db.is_supabase():
            return
        db.cache_set(_supabase_key(date_str), None, date_str, payload)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase write failed for {date_str}: {exc}")


# ── Public: page-side reader ────────────────────────────────────────────────

def load_scored_props() -> dict:
    """Return today's scored payload, or an empty-shape dict.

    Pure cache read.  Prefers local file (fast) over Supabase.  NEVER
    triggers a re-score -- if the cache is empty the caller decides
    how to render the empty state (the props page shows a "Props
    loading -- check back after 11 AM ET" message).

    Always returns a dict with at least the keys
    ``{"date", "generated_at", "picks"}`` so callers can iterate
    ``payload["picks"]`` without guards.
    """
    date_str = _today_et()
    payload = _read_local(date_str) or _read_supabase(date_str)
    if not payload:
        return {"date": date_str, "generated_at": None, "picks": []}
    # Mirror Supabase back to local so subsequent reads are file-cache fast.
    if not _cache_path(date_str).exists():
        _write_local(date_str, payload)
    # Defensive shape normalisation -- older payloads may be missing keys.
    payload.setdefault("date", date_str)
    payload.setdefault("generated_at", None)
    payload.setdefault("picks", [])
    return payload


# ── Public: scheduler-side scorer ───────────────────────────────────────────

_CONF_THRESHOLD = 0.55


def score_today_props() -> dict:
    """Score every prop in props_client's raw cache, enrich each
    surviving pick with summary stats + opp rank, persist to cache.

    Called by ``run_tier_1_refresh`` / ``run_tier_2_refresh`` after a
    successful raw-line fetch.  NEVER call this from a page render.

    Returns the persisted payload (also written to local + Supabase).
    On any total failure returns the prior cached payload so the page
    doesn't go blank.
    """
    started  = time.monotonic()
    date_str = _today_et()

    # Lazy imports to break the import cycle: props_client imports this
    # module (for the scheduler hook) and this module imports the model.
    try:
        from .props_client import (
            get_client, ALL_PITCHER_MARKETS, ALL_BATTER_MARKETS,
        )
        from .props_model            import predict
        from .player_profile_client  import (
            get_player_prop_summary,
            get_player_today_opponent,
            get_opp_rank_for_prop,
        )
    except Exception as exc:                                              # noqa: BLE001
        _log(f"import failed -- keeping prior cache: {exc}")
        return load_scored_props()

    raw_payload = get_client().get_today_props() or {}
    all_markets = raw_payload.get("markets") or {}
    all_bucket_markets = set(ALL_PITCHER_MARKETS) | set(ALL_BATTER_MARKETS)
    n_raw = sum(
        len(v or []) for k, v in all_markets.items() if k in all_bucket_markets
    )
    _log(f"start date={date_str} raw_props={n_raw}")

    # No raw lines yet -- keep whatever payload is already cached.  This
    # prevents an early-morning scheduler tick (raw cache still cold)
    # from wiping a good prior-day payload that the page is still
    # serving while we wait for today's lines to arrive.
    if n_raw == 0:
        _log("no raw props in cache -- skipping rescore (cache untouched)")
        return load_scored_props()

    # ── Classify each market's lines as main vs alt up front ───────────
    # Done before scoring so we can stamp every (player, market, line)
    # entry with its line_type as we go.  The classifier looks at the
    # raw over+under best_odds pair to decide which line is the book's
    # standard market line (close to even money) vs an inflated alt.
    from .props_line_classifier import classify_lines_for_market
    classifications: dict[str, dict[tuple, dict]] = {}
    for market, raw_market_props in all_markets.items():
        if market not in all_bucket_markets:
            continue
        classifications[market] = classify_lines_for_market(
            raw_market_props or []
        )

    # ── Score every prop, dedup by (player, market, line) ───────────────
    # Both sides (Over + Under) score independently; we keep whichever
    # the model is more confident in.  Matches what /api/analyze's
    # _collect_props does, just without the top-N truncation.
    by_pick: dict[tuple, dict] = {}
    n_scored = n_pred_err = 0
    for market, props in all_markets.items():
        if market not in all_bucket_markets:
            continue
        bucket = "pitcher" if market.startswith("pitcher_") else "batter"
        market_class = classifications.get(market, {})
        for p in (props or []):
            try:
                pred = predict(p)
                n_scored += 1
            except Exception:                                             # noqa: BLE001
                n_pred_err += 1
                continue
            try:
                line_f = float(p.get("line"))
            except (TypeError, ValueError):
                continue
            key   = (p.get("player_name", "?"), market, line_f)
            side  = (p.get("side") or "Over").strip().title()
            score = float(pred.get("confidence") or 0.0)
            class_info = market_class.get(
                (p.get("player_name", "") or "", line_f), {}
            )
            existing = by_pick.get(key)
            if existing is None or score > existing["confidence"]:
                by_pick[key] = {
                    "market":          market,
                    "bucket":          bucket,
                    "player":          p.get("player_name", "?"),
                    "team":            _team_for_prop(p),
                    "home_team":       p.get("home_team"),
                    "away_team":       p.get("away_team"),
                    "line":            p.get("line"),
                    "side":            side,
                    "best_odds":       p.get("best_odds"),
                    "best_book":       p.get("best_book"),
                    "recommendation":  pred.get("recommendation"),
                    "confidence":      score,
                    "edge":            float(pred.get("edge") or 0.0),
                    "model_prob":      float(pred.get("model_prob") or 0.0),
                    "source":          pred.get("source"),
                    "predicted_value": pred.get("predicted_value"),
                    "event_id":        p.get("event_id"),
                    "commence_time":   p.get("commence_time"),
                    # Classifier-stamped line type fields.  ``line_type``
                    # is "main" / "alt"; ``is_primary`` flags the single
                    # representative line for that (player, market).
                    "line_type":       class_info.get("line_type", "alt"),
                    "is_primary":      bool(class_info.get("is_primary")),
                    "over_odds":       class_info.get("over_odds"),
                    "under_odds":      class_info.get("under_odds"),
                    "_raw_prop":       p,   # only used during enrichment
                }

    # ── Group primaries vs alts per (player, market) ────────────────────
    # The primary row is the single chosen representative for each
    # (player, market) pair (main line when one exists, balanced alt
    # otherwise).  Non-primary rows for the same player+market are
    # attached to their primary as alt_picks so the UI can reveal them
    # behind the "Show Alt Lines" toggle.
    primaries: list[dict] = []
    alts_by_pm: dict[tuple[str, str], list[dict]] = {}
    for entry in by_pick.values():
        pm = (entry["player"], entry["market"])
        if entry.get("is_primary"):
            primaries.append(entry)
        else:
            alts_by_pm.setdefault(pm, []).append(entry)
    for pri in primaries:
        pm = (pri["player"], pri["market"])
        alts = alts_by_pm.get(pm, [])
        alts.sort(key=lambda r: float(r.get("line") or 0.0))
        # Trim each alt down to the columns the UI actually renders so
        # the persisted payload stays small.
        pri["alt_picks"] = [_slim_alt(a) for a in alts]

    # ── Filter primaries: confidence threshold + regression-edge sanity ─
    def _has_reg_edge(r: dict) -> bool:
        pv = r.get("predicted_value")
        if pv is None:
            return True
        try:
            lf = float(r["line"])
            if (r.get("side") or "Over").strip().title() == "Over":
                return pv >= lf + 0.5
            return pv <= lf - 0.5
        except (TypeError, ValueError):
            return True

    rows = [
        r for r in primaries
        if r["confidence"] >= _CONF_THRESHOLD and _has_reg_edge(r)
    ]
    rows.sort(key=lambda r: -r["confidence"])

    # ── Enrich each survivor with summary + opp rank ────────────────────
    # Each enrichment is backed by the per-player gamelog cache (Supabase
    # + local file), so warm caches keep this cheap.  On cold caches the
    # scheduler thread absorbs the latency -- the page never waits.
    for r in rows:
        try:
            opp = get_player_today_opponent(r["player"], r["_raw_prop"])
        except Exception:                                                 # noqa: BLE001
            opp = None
        r["opp_abbrev"] = opp
        try:
            r["summary"] = get_player_prop_summary(
                r["player"], r["market"], r["line"], r["side"],
                opp_abbrev=opp,
                is_pitcher=(r["bucket"] == "pitcher"),
            )
        except Exception:                                                 # noqa: BLE001
            r["summary"] = {}
        try:
            r["opp_rank"] = get_opp_rank_for_prop(opp, r["market"])
        except Exception:                                                 # noqa: BLE001
            r["opp_rank"] = None
        # Drop the raw-prop reference so the persisted payload stays
        # small and JSON-clean (the raw dict can carry deeply nested
        # bookmaker arrays that bloat the cache row).
        r.pop("_raw_prop", None)

    # ── Counts for the summary log ──────────────────────────────────────
    n_main_kept = sum(1 for r in rows if r.get("line_type") == "main")
    n_alt_kept  = len(rows) - n_main_kept
    n_alts_attached = sum(len(r.get("alt_picks") or []) for r in rows)

    payload = {
        "date":         date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "picks":        rows,
        "summary": {
            "scored":       n_scored,
            "predict_err":  n_pred_err,
            "deduped":      len(by_pick),
            "kept":         len(rows),
            "main_picks":   n_main_kept,
            "alt_picks":    n_alt_kept,
            "alts_attached": n_alts_attached,
        },
    }
    _write_local(date_str, payload)
    _write_supabase(date_str, payload)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    _log(
        f"done date={date_str} scored={n_scored} err={n_pred_err} "
        f"dedup={len(by_pick)} kept={len(rows)} "
        f"main={n_main_kept} alt={n_alt_kept} "
        f"alts_attached={n_alts_attached} elapsed={elapsed_ms}ms"
    )
    return payload


# ── Internal helpers ────────────────────────────────────────────────────────

def _team_for_prop(p: dict) -> str:
    """Compact 'AWAY @ HOME' label, copied from pages/props.py so the
    scheduler can build it without importing the page module."""
    home = (p.get("home_team") or "")[:3].upper()
    away = (p.get("away_team") or "")[:3].upper()
    if home and away:
        return f"{away} @ {home}"
    return home or away or ""


# Whitelist of fields the UI consumes when rendering an alt-line row
# under the primary card.  Trimming the rest keeps the persisted
# payload small (alt rows can number in the dozens per player on a
# full alt-market day).
_ALT_KEEP_FIELDS = (
    "market", "bucket", "player", "team",
    "line", "side", "line_type",
    "best_odds", "best_book", "over_odds", "under_odds",
    "recommendation", "confidence", "edge", "model_prob",
    "predicted_value", "source",
)


def _slim_alt(entry: dict) -> dict:
    """Return a copy of *entry* with only the fields the UI needs to
    render an alt-line sub-row.  Drops the heavy enrichment fields
    (summary, opp_rank, _raw_prop) because alts share the same player
    data as their primary -- the primary card already shows it."""
    return {k: entry[k] for k in _ALT_KEEP_FIELDS if k in entry}
