"""
player_similarity.py
====================
Prop-market-specific player similarity.

Players are similar in the CONTEXT of a prop market, not globally:
Luis Arraez and Chandler Simpson cluster together for home-run props
(both low power) but not for hits props.  So we build a separate
feature vector + neighbour list per market.

Pipeline (recompute_clusters, run nightly in the 3 AM prefetch job):
  1. Load the rolling snapshots already on disk
     (.cache/{batter,pitcher}_rolling_snapshots.json).
  2. For each market, pull the market-specific feature subset for every
     eligible player (>=20 PA batters, >=20 IP pitchers ~ 5 starts).
  3. Z-score standardise the columns, compute pairwise Euclidean
     distance, take each player's nearest neighbours, and assign a
     coarse KMeans cluster label.
  4. Persist {market: {players: {pid: {name, team, cluster, similar:
     [{id, name, team, score}]}}}} to
     .cache/player_similarity_clusters.json + Supabase.

Read side (load_clusters / get_similar_players) is a pure cache read
used by the player profile + props pages -- never recomputes.

The clustering is intentionally approximate: only the rolling stats we
already have feed it, advanced Statcast metrics (launch angle, chase
rate, ...) are not available.  Loosely-similar players still surface,
with their similarity score shown.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(".cache")
_CLUSTERS_PATH = _CACHE_DIR / "player_similarity_clusters.json"
_CACHE_KEY = "player_similarity_clusters"

# Eligibility thresholds (Step 2 of the brief).
_MIN_BATTER_PA = 20.0
_MIN_PITCHER_IP = 20.0   # ~5 starts

# How many neighbours to precompute + store per player.
_TOP_N = 6


def _log(msg: str) -> None:
    print(f"SIMILARITY: {msg}", flush=True, file=sys.stderr)


# ── Per-market feature sets ─────────────────────────────────────────────────
# Each list uses ONLY fields confirmed present in the rolling
# snapshots.  Markets the snapshots can't characterise are omitted
# (callers get an empty similar-list, which the UI renders as "not
# enough data").
_MARKET_FEATURES: dict[str, list[str]] = {
    # Batter
    "batter_hits":        ["szn_H_per_AB", "szn_BB_per_PA", "szn_SO_per_PA",
                           "szn_k_pct", "babip_14d"],
    "batter_total_bases": ["szn_TB_per_AB", "szn_HR_per_AB",
                           "slg_vs_rhp", "slg_vs_lhp"],
    "batter_rbis":        ["batting_order", "szn_TB_per_AB",
                           "obp_vs_rhp", "slg_vs_rhp"],
    "batter_runs_scored": ["batting_order", "szn_BB_per_PA",
                           "obp_vs_rhp", "obp_vs_lhp"],
    "batter_home_runs":   ["szn_HR_per_AB", "szn_TB_per_AB",
                           "ballpark_factor_hr", "slg_vs_rhp"],
    "batter_walks":       ["szn_BB_per_PA", "szn_SO_per_PA", "szn_k_pct"],
    "batter_strikeouts":  ["szn_SO_per_PA", "szn_k_pct", "szn_BB_per_PA"],
    # Pitcher
    "pitcher_strikeouts":   ["szn_k_per_9", "career_k_per_9",
                             "k_rate_vs_rhb", "k_rate_vs_lhb"],
    "pitcher_outs":         ["ip_last_30d", "days_since_last_start",
                             "szn_k_per_9"],
    "pitcher_earned_runs":  ["szn_ER", "career_fip", "era_vs_rhb", "era_vs_lhb"],
    "pitcher_walks":        ["szn_bb_per_9", "career_bb_per_9"],
    "pitcher_hits_allowed": ["szn_H", "career_fip", "szn_k_per_9"],
}

_BATTER_MARKETS = {m for m in _MARKET_FEATURES if m.startswith("batter_")}


def _is_batter_market(market: str) -> bool:
    return market in _BATTER_MARKETS


# ── Snapshot loading ────────────────────────────────────────────────────────

def _load_snapshot_players(is_pitcher: bool) -> tuple[dict, dict]:
    """Return ({pid: snapshot_entry}, league_medians).  Reads the local
    JSON directly; falls back to props_model's loader (which can
    restore from Supabase) when the file is missing."""
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


def _eligible(entry: dict, is_pitcher: bool) -> bool:
    feats = entry.get("features") or {}
    if is_pitcher:
        return float(feats.get("szn_IP") or 0) >= _MIN_PITCHER_IP
    return float(feats.get("szn_PA") or 0) >= _MIN_BATTER_PA


# ── Recompute (write side) ──────────────────────────────────────────────────

def recompute_clusters() -> dict:
    """Rebuild every market's similarity neighbour lists from the
    rolling snapshots and persist them.  Returns a summary dict.
    Best-effort: any failure leaves the prior cache intact."""
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
            if not _eligible(entry, is_pitcher):
                continue
            feats = entry.get("features") or {}
            present = [k for k in feat_keys if _num(feats.get(k)) is not None]
            if len(present) < max(1, (len(feat_keys) + 1) // 2):
                continue
            # Fill missing single features with 0.0 (post-standardisation
            # this reads as "league average" since columns are z-scored).
            vec = [(_num(feats.get(k)) if _num(feats.get(k)) is not None else 0.0)
                   for k in feat_keys]
            ids.append(str(pid))
            names.append(entry.get("name") or "")
            teams.append(entry.get("team") or "")
            rows.append(vec)

        if len(rows) < 4:
            markets_out[market] = {"players": {}}
            summary[market] = 0
            continue

        X = np.asarray(rows, dtype=float)
        # Z-score standardise per column (guard zero variance).
        mean = X.mean(axis=0)
        std = X.std(axis=0)
        std[std == 0] = 1.0
        Z = (X - mean) / std

        # Pairwise Euclidean distances.
        # (n x n) via broadcasting -- n is small (hundreds at most).
        diff = Z[:, None, :] - Z[None, :, :]
        dist = np.sqrt((diff * diff).sum(axis=2))

        # Coarse KMeans cluster label (best-effort; the neighbour list
        # is what the UI shows, the label is stored for completeness).
        labels = _kmeans_labels(Z, len(rows))

        per_player: dict[str, dict] = {}
        for i, pid in enumerate(ids):
            order = np.argsort(dist[i])
            similar: list[dict] = []
            for j in order:
                if j == i:
                    continue
                d = float(dist[i][j])
                score = round(1.0 / (1.0 + d), 4)   # (0, 1]; 1 = identical
                similar.append({
                    "id":    ids[j],
                    "name":  names[j],
                    "team":  teams[j],
                    "score": score,
                })
                if len(similar) >= _TOP_N:
                    break
            per_player[pid] = {
                "name":    names[i],
                "team":    teams[i],
                "cluster": int(labels[i]) if labels is not None else -1,
                "similar": similar,
            }

        markets_out[market] = {"players": per_player}
        summary[market] = len(per_player)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets":      markets_out,
    }
    _write(payload)
    _log(f"recompute complete -- " + ", ".join(
        f"{m}:{n}" for m, n in summary.items()
    ))
    return {"ok": True, "summary": summary, "generated_at": payload["generated_at"]}


def _kmeans_labels(Z, n_rows):
    """Return KMeans labels, or None on any failure (the neighbour list
    doesn't depend on this)."""
    try:
        from sklearn.cluster import KMeans
        import numpy as np  # noqa: F401
        k = max(2, min(8, n_rows // 10))
        km = KMeans(n_clusters=k, n_init=4, random_state=0)
        return km.fit_predict(Z)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"kmeans skipped: {exc}")
        return None


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None     # NaN guard
    except (TypeError, ValueError):
        return None


# ── Persistence ─────────────────────────────────────────────────────────────

def _write(payload: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CLUSTERS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"local write failed: {exc}")
    try:
        from . import db
        if db.is_supabase():
            today = datetime.now(timezone.utc).date().isoformat()
            db.cache_set(_CACHE_KEY, "mlb", today, payload)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase write failed: {exc}")


_cache: Optional[dict] = None


def load_clusters() -> dict:
    """Pure cache read: local file first, then Supabase.  Returns
    {"markets": {...}} (possibly empty).  Process-cached after first
    read."""
    global _cache
    if _cache is not None:
        return _cache
    payload: dict = {}
    if _CLUSTERS_PATH.exists():
        try:
            payload = json.loads(_CLUSTERS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:                                          # noqa: BLE001
            _log(f"local read failed: {exc}")
    if not payload:
        try:
            from . import db
            row = db.cache_get(_CACHE_KEY)
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                if isinstance(data, dict) and data.get("markets"):
                    payload = data
                    try:
                        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        _CLUSTERS_PATH.write_text(json.dumps(payload), encoding="utf-8")
                    except Exception:                                     # noqa: BLE001
                        pass
        except Exception as exc:                                          # noqa: BLE001
            _log(f"supabase read failed: {exc}")
    if not payload:
        payload = {"markets": {}}
    _cache = payload
    return payload


def reload() -> None:
    """Drop the process cache so the next load_clusters re-reads."""
    global _cache
    _cache = None


# ── Read API ────────────────────────────────────────────────────────────────

def get_similar_players(market: str, player_name: str, limit: int = 5) -> list[dict]:
    """Return up to *limit* players most similar to *player_name* for
    *market*, each ``{id, name, team, score}`` (score 0..1).  Pure
    cache lookup -- empty list when the player or market isn't
    clustered yet."""
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
