"""
research_store.py
=================
Forward-only settled-prop history for the /research model-analytics dashboard.

Why this exists
---------------
The canonical settled store (``model_picks``) records a prop's win/loss but
NOT which Groq AI model produced its breakdown, nor the prop's edge -- those
live only on per-day caches (player_ai_breakdown / props_scored_cache) that
roll over nightly.  To power a per-model / per-edge leaderboard we capture
those two facts at scoring time into a dedicated, rollover-proof store and
settle them on the same 15-minute cycle the rest of settlement uses.

This is FORWARD-ONLY: historical props can't be attributed (the source caches
are gone), so the dashboard fills in from the day this ships onward.

Lifecycle (mirrors top_plays_tracker)
-------------------------------------
  record(scored_picks)  freeze model + edge + odds for each scored prop
                        (idempotent, dedup by a deterministic id)
  settle(stat_lookup)   grade pending rows against the same player-stat
                        lookup model_picks settlement uses; freeze units P/L
  rows()                read side for the dashboard
  aggregate(...)        pure group-by used by the page (unit-testable)

Storage: one app_cache row keyed "research_props_history" (the "history"
substring exempts it from the daily ET cache cleaner) + a local file mirror.
PostgREST only -- survives Railway redeploys.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_CACHE_KEY  = "research_props_history"     # contains "history" -> survives rollover
_LOCAL_PATH = Path(".cache/research_store.json")

_FLAT_STAKE = 1.0          # one unit per pick -- ROI denominator

_rows: list[dict] = []
_index: set[str] = set()
_loaded = False
_loaded_at: float = 0.0
_LOAD_TTL = 300.0


def _log(msg: str) -> None:
    print(f"RESEARCH-STORE: {msg}", flush=True, file=sys.stderr)


def _today_et() -> str:
    return datetime.now(_ET).date().isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Persistence ──────────────────────────────────────────────────────────────

def _load_from_supabase() -> Optional[list[dict]]:
    try:
        from . import db
        if not db.is_supabase():
            return None
        row = db.cache_get(_CACHE_KEY)
        if not isinstance(row, dict):
            return None
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        rows = data.get("rows") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase load failed: {exc}")
        return None


def _load_from_file() -> Optional[list[dict]]:
    try:
        if _LOCAL_PATH.exists():
            data = json.loads(_LOCAL_PATH.read_text(encoding="utf-8"))
            rows = data.get("rows") if isinstance(data, dict) else None
            return rows if isinstance(rows, list) else None
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local load failed: {exc}")
    return None


def _ensure_loaded() -> None:
    global _loaded, _loaded_at
    if _loaded and (time.time() - _loaded_at) <= _LOAD_TTL:
        return
    rows = _load_from_supabase()
    if rows is None:
        rows = _load_from_file()
    _rows.clear()
    _rows.extend(rows or [])
    _index.clear()
    _index.update(r.get("id") for r in _rows if r.get("id"))
    _loaded = True
    _loaded_at = time.time()


def reload() -> None:
    global _loaded
    _loaded = False
    _ensure_loaded()


def _save() -> None:
    payload = {"rows": _rows, "updated_at": _now_iso()}
    try:
        _LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local save failed: {exc}")
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_CACHE_KEY, None, _today_et(), payload)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase save failed: {exc}")


# ── Sizing ───────────────────────────────────────────────────────────────────

def _profit_units(odds) -> float:
    """Profit in units on a winning 1-unit bet at American *odds*."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    return round(_FLAT_STAKE * (o / 100.0) if o > 0 else _FLAT_STAKE * (100.0 / abs(o)), 4)


# ── Recording (freeze model + edge on first appearance) ───────────────────────

def _model_version_for(pick: dict) -> str:
    """The Groq model version (V1..V4) that produced this prop's breakdown,
    from the same cached breakdown the player page reads.  '' when none yet."""
    try:
        from . import player_ai_breakdown as _pab
        bd = _pab.peek_breakdown(pick) or {}
        return (bd.get("model_version") or "").strip()
    except Exception:                                                     # noqa: BLE001
        return ""


def _model_name(version: str) -> str:
    try:
        from .groq_models import model_name
        return model_name(version) or ""
    except Exception:                                                     # noqa: BLE001
        return ""


def _rec_id(date_str: str, player, market, side, line) -> str:
    return f"{date_str}:{player}:{market}:{side}:{line}"


def record(scored_picks: list[dict]) -> int:
    """Freeze model + edge + odds for each newly-seen scored prop.  Idempotent
    (dedup by deterministic id).  Returns the number of NEW rows recorded."""
    _ensure_loaded()
    date_str = _today_et()
    new = 0
    for p in scored_picks or []:
        if not isinstance(p, dict):
            continue
        player = (p.get("player") or "").strip()
        market = p.get("market")
        if not player or not market:
            continue
        side = (p.get("side") or "Over").strip().title()
        try:
            line = float(p.get("line"))
        except (TypeError, ValueError):
            line = None
        rid = _rec_id(date_str, player, market, side, line)
        if rid in _index:
            continue
        version = _model_version_for(p)
        edge = p.get("edge")
        conf = p.get("confidence")
        if conf is None:
            conf = p.get("model_prob")
        _rows.append({
            "id":            rid,
            "date":          date_str,
            "sport":         "mlb",
            "player":        player,
            "prop_type":     market,
            "side":          side.upper(),
            "line":          line,
            "model_version": version or None,
            "model":         _model_name(version) or None,
            "edge":          round(float(edge), 4) if isinstance(edge, (int, float)) else None,
            "odds":          int(p.get("best_odds")) if p.get("best_odds") is not None else None,
            "confidence":    round(float(conf), 4) if isinstance(conf, (int, float)) else None,
            "result":        "pending",
            "units_pnl":     None,
            "recorded_at":   _now_iso(),
            "settled_at":    None,
        })
        _index.add(rid)
        new += 1
    if new:
        _save()
        _log(f"recorded {new} new prop(s); {len(_rows)} tracked total")
    return new


# ── Settlement (15-min cycle) ─────────────────────────────────────────────────

def settle(stat_lookup: Optional[Callable] = None) -> dict:
    """Grade every pending row against the same player-stat lookup model_picks
    settlement uses, freeze units P/L once, and persist.  Returns counts."""
    _ensure_loaded()
    if stat_lookup is None:
        return {"settled": 0, "wins": 0, "losses": 0, "voids": 0}
    from . import model_picks as _mp

    s = {"settled": 0, "wins": 0, "losses": 0, "voids": 0}
    changed = False
    for r in _rows:
        if (r.get("result") or "pending") != "pending":
            continue
        try:
            actual = stat_lookup(r.get("player"), r.get("prop_type"), r.get("date"))
        except TypeError:
            try:
                actual = stat_lookup(r.get("player"), r.get("prop_type"))
            except Exception:                                             # noqa: BLE001
                actual = None
        except Exception:                                                 # noqa: BLE001
            actual = None
        if actual is None:
            continue
        pick = {"bet_type": r.get("prop_type"), "pick_side": r.get("side"),
                "line": r.get("line")}
        result = _mp._grade_prop(pick, actual)
        if not result:
            continue
        if result == "win":
            r["units_pnl"] = _profit_units(r.get("odds"))
            s["wins"] += 1
        elif result in ("push", "void"):
            result = "void"
            r["units_pnl"] = 0.0
            s["voids"] += 1
        else:
            result = "loss"
            r["units_pnl"] = round(-_FLAT_STAKE, 4)
            s["losses"] += 1
        r["result"] = result
        r["settled_at"] = _now_iso()
        s["settled"] += 1
        changed = True
    if changed:
        _save()
        _log(f"settled {s['settled']} ({s['wins']}W/{s['losses']}L/{s['voids']}V)")
    return s


# ── Read side ──────────────────────────────────────────────────────────────--

def rows() -> list[dict]:
    _ensure_loaded()
    return list(_rows)


def distinct_prop_types() -> list[str]:
    _ensure_loaded()
    return sorted({r.get("prop_type") for r in _rows if r.get("prop_type")})


# ── Aggregation (pure -- unit-testable) ───────────────────────────────────────

_WINDOW_DAYS = {"7d": 7, "30d": 30}


def _settled_at_dt(r: dict) -> Optional[datetime]:
    iso = r.get("settled_at")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _in_window(r: dict, window: str, now: datetime) -> bool:
    if window in (None, "", "all"):
        return True
    dt = _settled_at_dt(r)
    if dt is None:
        return False
    if window == "season":
        return dt.astimezone(_ET).year == now.astimezone(_ET).year
    days = _WINDOW_DAYS.get(window)
    if days is None:
        return True
    return dt >= now - timedelta(days=days)


def _filtered(rows_in, *, models, sport, prop_type, window, now,
              prop_types=None) -> list[dict]:
    models_l = {m.lower() for m in models} if models and "all" not in models else None
    # Multi-select bet-type allow-list (UI redesign, Change 4).  When given,
    # it supersedes the single *prop_type* arg; rows whose market isn't in the
    # set are dropped.  Distinct markets still group separately downstream, so
    # multi-selected bet types stay as separate row groups in the table.
    ptypes = (set(prop_types)
              if prop_types and "all" not in prop_types else None)
    out = []
    for r in rows_in:
        if (r.get("result") or "pending") not in ("win", "loss", "void"):
            continue
        if sport and sport != "all" and (r.get("sport") or "").lower() != sport.lower():
            continue
        if ptypes is not None:
            if r.get("prop_type") not in ptypes:
                continue
        elif prop_type and prop_type != "all" and r.get("prop_type") != prop_type:
            continue
        if models_l is not None and (r.get("model") or "").lower() not in models_l:
            continue
        if not _in_window(r, window, now):
            continue
        out.append(r)
    return out


def _hot_streak(group_rows: list[dict]) -> int:
    """Current consecutive wins, counting back from the most recently settled
    pick.  A loss (not a void) breaks the streak."""
    ordered = sorted(
        (r for r in group_rows if r.get("result") in ("win", "loss")),
        key=lambda r: (r.get("settled_at") or ""), reverse=True,
    )
    streak = 0
    for r in ordered:
        if r.get("result") == "win":
            streak += 1
        else:
            break
    return streak


def _stats(group_rows: list[dict]) -> dict:
    wins = sum(1 for r in group_rows if r.get("result") == "win")
    losses = sum(1 for r in group_rows if r.get("result") == "loss")
    voids = sum(1 for r in group_rows if r.get("result") == "void")
    decided = wins + losses
    picks = wins + losses + voids
    win_pct = (wins / decided * 100.0) if decided else 0.0
    edges = [float(r["edge"]) for r in group_rows if isinstance(r.get("edge"), (int, float))]
    avg_edge = (sum(edges) / len(edges) * 100.0) if edges else 0.0
    # ROI: net units / units risked (1 unit per decided pick).
    net_units = sum(float(r.get("units_pnl") or 0.0)
                    for r in group_rows if r.get("result") in ("win", "loss"))
    roi = (net_units / decided * 100.0) if decided else 0.0
    return {
        "picks":    picks,
        "wins":     wins,
        "losses":   losses,
        "voids":    voids,
        "win_pct":  round(win_pct, 1),
        "avg_edge": round(avg_edge, 1),
        "roi":      round(roi, 1),
        "net_units": round(net_units, 2),
    }


def aggregate(rows_in: Optional[list[dict]] = None, *,
              models: Optional[list[str]] = None,
              sport: str = "all",
              prop_type: str = "all",
              prop_types: Optional[list[str]] = None,
              window: str = "all",
              now: Optional[datetime] = None) -> dict:
    """Group filtered settled rows by (model, prop_type).  Returns
    {kpis, table} where table rows carry picks/wins/win_pct/avg_edge/roi/streak.

    *prop_types* is a multi-select allow-list of market keys (UI Change 4);
    when given it supersedes the single *prop_type*.  Distinct markets group
    separately, so multi-selected bet types stay separated in the table.

    Pure: pass *rows_in* to test without I/O (defaults to the live store)."""
    if rows_in is None:
        rows_in = rows()
    now = now or datetime.now(timezone.utc)
    flt = _filtered(rows_in, models=models, sport=sport,
                    prop_type=prop_type, prop_types=prop_types,
                    window=window, now=now)

    groups: dict[tuple, list[dict]] = {}
    for r in flt:
        key = (r.get("model") or "—", r.get("prop_type") or "—")
        groups.setdefault(key, []).append(r)

    table = []
    for (model, ptype), grp in groups.items():
        st = _stats(grp)
        table.append({"model": model, "prop_type": ptype,
                      "streak": _hot_streak(grp), **st})

    kpis = _stats(flt)
    return {"kpis": kpis, "table": table, "n_settled": len(flt)}
