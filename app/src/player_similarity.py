"""
player_similarity.py
====================
Prop-market-specific player similarity via KMeans clustering.

Players are similar in the CONTEXT of a prop market, not globally:
Luis Arraez and Chandler Simpson cluster together for home-run props
(both low power) but not for hits props.  So we build a separate
feature vector + cluster assignment per market.

Pipeline (recompute_clusters, run nightly in the 3 AM prefetch job):
  1. Load the rolling snapshots already on disk
     (.cache/{batter,pitcher}_rolling_snapshots.json).
  2. For each market, pull the market-specific feature subset for every
     eligible player -- those with >=20 PA (batters) / >=20 IP (~5
     starts, pitchers) OR who appear in today's props.
  3. Z-score standardise the columns, run KMeans (n=8 clusters), then for
     each player rank the OTHER members of its cluster by cosine
     similarity and keep the closest few.
  4. Persist one row per market to Supabase keyed by
     ``similarity_{market}_{date}`` (plus a combined local JSON for fast
     reads).

Read side (load_clusters / get_similar_players) is a pure cache read used
by the player profile + props pages -- it never recomputes.

The clustering is intentionally approximate: only the rolling stats we
already have feed it; advanced Statcast metrics (launch angle, chase
rate, swinging-strike rate, ...) aren't in the snapshots, so each market
uses the closest available proxies.  Loosely-similar players still
surface, with their cosine-similarity score shown.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(".cache")
_CLUSTERS_PATH = _CACHE_DIR / "player_similarity_clusters.json"

# Number of KMeans clusters per market (the brief specifies 8; capped at
# the eligible-player count for tiny markets).
_N_CLUSTERS = 8

# Eligibility (Step 2 of the brief).  The rolling snapshots store PER-GAME
# rates, not cumulative season totals, so "20 PA / 5 starts" is applied via
# the closest available signals: a regular hitter's per-game PA, and a
# starter's innings over the trailing 30 days (~5 starts).  Players in
# today's props are always included regardless.
_MIN_BATTER_PA_PER_GAME = 2.0
_MIN_PITCHER_IP_30D      = 20.0

# How many in-cluster neighbours to precompute + store per player (the UI
# shows up to 5).
_TOP_N = 6


def _log(msg: str) -> None:
    print(f"SIMILARITY: {msg}", flush=True, file=sys.stderr)


def _today_key_date() -> str:
    """ET calendar date (YYYY-MM-DD).  Matches the app's canonical day so
    the ``similarity_{market}_{date}`` keys line up with the ET-based
    daily cache cleaner (which only spares rows whose date == today)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return datetime.now(timezone.utc).date().isoformat()


def _supabase_key(market: str, date: str) -> str:
    return f"similarity_{market}_{date}"


# ── Per-market feature sets ─────────────────────────────────────────────────
# Each list uses ONLY fields confirmed present in the rolling snapshots
# (advanced Statcast inputs the brief mentions -- swinging-strike rate,
# fastball velo, GB%, hard-hit%, launch angle, pull%, chase rate -- aren't
# captured there, so the closest available proxies stand in).
_MARKET_FEATURES: dict[str, list[str]] = {
    # Batter
    "batter_hits":        ["szn_H_per_AB", "szn_BB_per_PA", "szn_SO_per_PA",
                           "babip_14d"],
    "batter_total_bases": ["szn_TB_per_AB", "szn_HR_per_AB",
                           "slg_vs_rhp", "slg_vs_lhp"],
    "batter_rbis":        ["batting_order", "szn_TB_per_AB",
                           "obp_vs_rhp", "slg_vs_rhp"],
    "batter_runs_scored": ["batting_order", "szn_BB_per_PA",
                           "obp_vs_rhp", "obp_vs_lhp"],
    "batter_home_runs":   ["szn_HR_per_AB", "szn_TB_per_AB",
                           "ballpark_factor_hr", "slg_vs_rhp"],
    "batter_walks":       ["szn_BB_per_PA", "szn_SO_per_PA", "r14_BB_per_PA"],
    "batter_strikeouts":  ["szn_SO_per_PA", "k_pct_14d", "szn_BB_per_PA"],
    # Pitcher
    "pitcher_strikeouts":   ["szn_k_per_9", "career_k_per_9",
                             "k_rate_vs_rhb", "k_rate_vs_lhb", "szn_bb_per_9"],
    "pitcher_outs":         ["ip_last_30d", "szn_IP", "r14_IP",
                             "days_since_last_start"],
    "pitcher_earned_runs":  ["era_vs_rhb", "era_vs_lhb", "career_fip", "szn_ER"],
    "pitcher_walks":        ["szn_bb_per_9", "career_bb_per_9"],
    "pitcher_hits_allowed": ["szn_H", "career_fip", "szn_k_per_9"],
}

_BATTER_MARKETS = {m for m in _MARKET_FEATURES if m.startswith("batter_")}


def _is_batter_market(market: str) -> bool:
    return market in _BATTER_MARKETS


# ── Snapshot loading ────────────────────────────────────────────────────────

def _load_snapshot_players(is_pitcher: bool) -> tuple[dict, dict]:
    """Return ({pid: snapshot_entry}, league_medians).  Reads the local
    JSON directly; falls back to props_model's loader (which can restore
    from Supabase) when the file is missing."""
    path = _CACHE_DIR / (
        "pitcher_rolling_snapshots.json" if is_pitcher
        else "batter_rolling_snapshots.json"
    )
    payload: dict = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"snapshot read failed ({path.name}): {exc}")
    if not payload:
        try:
            from . import props_model as _pm
            payload = (_pm._load_pitcher_snapshots() if is_pitcher
                       else _pm._load_batter_snapshots()) or {}
        except Exception as exc:                                          # noqa: BLE001
            _log(f"snapshot loader fallback failed: {exc}")
            payload = {}
    players = {str(k): v for k, v in (payload.get("players") or {}).items()}
    medians = dict(payload.get("league_medians") or {})
    return players, medians


def _todays_prop_names() -> set[str]:
    """Lowercased player names appearing in today's scored props, so they
    can be clustered even below the PA/IP threshold (Step 2 of the brief).
    Best-effort -- empty set when the scored cache isn't available."""
    try:
        from . import props_scored_cache as _psc
        picks = (_psc.load_scored_props() or {}).get("picks") or []
    except Exception as exc:                                              # noqa: BLE001
        _log(f"today's-props lookup failed: {exc}")
        return set()
    names: set[str] = set()
    for p in picks:
        nm = (p.get("player_name") or p.get("player") or "").strip().lower()
        if nm:
            names.add(nm)
    return names


def _eligible(entry: dict, is_pitcher: bool, today_names: set[str]) -> bool:
    if (entry.get("name") or "").strip().lower() in today_names:
        return True
    feats = entry.get("features") or {}
    if is_pitcher:
        return float(feats.get("ip_last_30d") or 0) >= _MIN_PITCHER_IP_30D
    return float(feats.get("szn_PA") or 0) >= _MIN_BATTER_PA_PER_GAME


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None     # NaN guard
    except (TypeError, ValueError):
        return None


# ── Recompute (write side) ──────────────────────────────────────────────────

def recompute_clusters() -> dict:
    """Rebuild every market's KMeans clusters + cosine neighbour lists from
    the rolling snapshots and persist them (one Supabase row per market,
    keyed ``similarity_{market}_{date}``, plus a combined local file).

    Best-effort: any failure leaves the prior cache intact.  Returns a
    summary dict ``{ok, summary: {market: n_players}, generated_at}``."""
    try:
        import numpy as np
    except Exception as exc:                                              # noqa: BLE001
        _log(f"numpy unavailable -- skipping recompute: {exc}")
        return {"ok": False, "error": "numpy unavailable"}

    batter_players, _bm  = _load_snapshot_players(is_pitcher=False)
    pitcher_players, _pm = _load_snapshot_players(is_pitcher=True)
    if not batter_players and not pitcher_players:
        _log("no snapshots on disk -- skipping recompute (cache untouched)")
        return {"ok": False, "error": "no snapshots"}

    today = _today_key_date()
    today_names = _todays_prop_names()

    markets_out: dict[str, dict] = {}
    summary: dict[str, int] = {}

    for market, feat_keys in _MARKET_FEATURES.items():
        is_pitcher = not _is_batter_market(market)
        players = pitcher_players if is_pitcher else batter_players

        # Collect eligible players + raw vectors (skip players missing
        # more than half the market's features).
        ids: list[str] = []
        names: list[str] = []
        teams: list[str] = []
        rows: list[list[float]] = []
        for pid, entry in players.items():
            if not _eligible(entry, is_pitcher, today_names):
                continue
            feats = entry.get("features") or {}
            present = [k for k in feat_keys if _num(feats.get(k)) is not None]
            if len(present) < max(1, (len(feat_keys) + 1) // 2):
                continue
            # Missing single features -> 0.0; post-standardisation that
            # reads as "league average" since columns are z-scored.
            vec: list[float] = []
            for k in feat_keys:
                v = _num(feats.get(k))
                vec.append(v if v is not None else 0.0)
            ids.append(str(pid))
            names.append(entry.get("name") or "")
            teams.append(entry.get("team") or "")
            rows.append(vec)

        per_player = _cluster_market(np, rows, ids, names, teams)
        markets_out[market] = {"players": per_player}
        summary[market] = len(per_player)
        _write_market(market, today, {"players": per_player})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date":         today,
        "markets":      markets_out,
    }
    _write_local(payload)
    reload()
    _log("recompute complete -- " + ", ".join(
        f"{m}:{n}" for m, n in summary.items()
    ))
    return {"ok": True, "summary": summary, "generated_at": payload["generated_at"]}


def _cluster_market(np, rows, ids, names, teams) -> dict:
    """KMeans(n=8) + within-cluster cosine ranking for one market.  Returns
    ``{pid: {name, team, cluster, similar: [{id, name, team, score}]}}``."""
    if len(rows) < 4:
        return {}
    X = np.asarray(rows, dtype=float)

    # Z-score standardise per column (guard zero variance).
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    Z = (X - mean) / std

    labels = _kmeans_labels(Z, len(rows))
    if labels is None:
        labels = np.zeros(len(rows), dtype=int)   # single-cluster fallback

    # Cosine similarity on the standardised vectors.
    norms = np.linalg.norm(Z, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    Zn = Z / norms
    cos = Zn @ Zn.T

    per_player: dict[str, dict] = {}
    for i, pid in enumerate(ids):
        # Candidates = OTHER members of the same cluster, ranked by cosine.
        same = [j for j in range(len(ids)) if j != i and labels[j] == labels[i]]
        same.sort(key=lambda j: cos[i][j], reverse=True)
        similar = [{
            "id":    ids[j],
            "name":  names[j],
            "team":  teams[j],
            "score": round(float(max(0.0, min(1.0, cos[i][j]))), 4),
        } for j in same[:_TOP_N]]
        per_player[pid] = {
            "name":    names[i],
            "team":    teams[i],
            "cluster": int(labels[i]),
            "similar": similar,
        }
    return per_player


def _kmeans_labels(Z, n_rows):
    """KMeans cluster labels (n=8, capped at n_rows), or None on failure."""
    try:
        from sklearn.cluster import KMeans
        k = min(_N_CLUSTERS, n_rows)
        if k < 2:
            return None
        km = KMeans(n_clusters=k, n_init=10, random_state=0)
        return km.fit_predict(Z)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"kmeans skipped: {exc}")
        return None


# ── Persistence ─────────────────────────────────────────────────────────────

def _write_market(market: str, date: str, data: dict) -> None:
    """Upsert one market's clusters to Supabase as ``similarity_{market}_{date}``."""
    try:
        from . import db
        if db.is_supabase():
            db.cache_set(_supabase_key(market, date), "mlb", date, data)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase write failed [{market}]: {exc}")


def _write_local(payload: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CLUSTERS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local write failed: {exc}")


_cache: Optional[dict] = None


def load_clusters() -> dict:
    """Pure cache read: combined local file first, else assemble from the
    per-market ``similarity_{market}_{date}`` Supabase rows for today.
    Returns ``{"markets": {...}}`` (possibly empty).  Process-cached."""
    global _cache
    if _cache is not None:
        return _cache

    payload: dict = {}
    if _CLUSTERS_PATH.exists():
        try:
            payload = json.loads(_CLUSTERS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"local read failed: {exc}")

    if not (payload.get("markets") if isinstance(payload, dict) else None):
        payload = _assemble_from_supabase()

    if not payload or not payload.get("markets"):
        payload = {"markets": {}}
    _cache = payload
    return payload


def _assemble_from_supabase() -> dict:
    """Read today's per-market similarity rows from Supabase and assemble
    the combined ``{"markets": {...}}`` shape (also rewrites the local
    file).  Empty dict when none are present."""
    try:
        from . import db
        if not db.is_supabase():
            return {}
        today = _today_key_date()
        markets: dict[str, dict] = {}
        for market in _MARKET_FEATURES:
            row = db.cache_get(_supabase_key(market, today))
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(data, dict) and data.get("players"):
                markets[market] = {"players": data["players"]}
        if not markets:
            return {}
        payload = {"date": today, "markets": markets}
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _CLUSTERS_PATH.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:                                                 # noqa: BLE001
            pass
        return payload
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase assemble failed: {exc}")
        return {}


def reload() -> None:
    """Drop the process cache so the next load_clusters re-reads."""
    global _cache
    _cache = None


# ── Read API ────────────────────────────────────────────────────────────────

def get_similar_players(market: str, player_name: str, limit: int = 5) -> list[dict]:
    """Return up to *limit* players in *player_name*'s cluster for *market*,
    each ``{id, name, team, score}`` (cosine score 0..1, descending).  Pure
    cache lookup -- empty list when the player or market isn't clustered."""
    if not market or not player_name:
        return []
    clusters = load_clusters()
    market_data = (clusters.get("markets") or {}).get(market) or {}
    players = market_data.get("players") or {}
    if not players:
        return []
    target = player_name.strip().lower()
    for _pid, entry in players.items():
        if (entry.get("name") or "").strip().lower() == target:
            return list(entry.get("similar") or [])[:limit]
    return []
