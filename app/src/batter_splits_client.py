"""
Top-5 batters by plate appearances for each team, with vs-L and vs-R
splits (AVG, OBP, SLG, K%) from the free MLB Stats API.

Composite consumers (Lineup Vulnerability Score) need the average OPS of
the team's top-5 hitters against the opposing starting pitcher's
handedness, so this module exposes:

    get_team_top_batters(team_id, season)
        -> [{batter_id, name, pa, vs_l: {...}, vs_r: {...}}, ...]

    get_team_lvs_inputs(team_id, season, opp_hand)
        -> [{ops, k_rate}, ...]   length up to 5

Live cache:        .cache/batter_splits_live.json   (TTL 24h)
Historical cache:  .cache/mlb_hist_bsplits_{team}_{yr}.json  (TTL 1yr)
"""
from __future__ import annotations

import logging
import json
import time
from pathlib import Path
from typing import Optional

from .utils import _safe, _fetch_url_ua

_BASE = "https://statsapi.mlb.com/api/v1"
_LIVE_CACHE_FILE = Path(".cache/batter_splits_live.json")
_LIVE_CACHE_TTL  = 86400   # 24h
_HIST_CACHE_DIR  = Path(".cache")
_HIST_CACHE_TTL  = 365 * 86400

_NEUTRAL_BAT_SPLIT = {
    "avg":    0.245,
    "obp":    0.315,
    "slg":    0.395,
    "k_rate": 0.220,
}


def _ops(stat: dict) -> float:
    """OPS = OBP + SLG; falls back if either side is missing."""
    obp = _safe(stat.get("obp"), _NEUTRAL_BAT_SPLIT["obp"])
    slg = _safe(stat.get("slg"), _NEUTRAL_BAT_SPLIT["slg"])
    return obp + slg


# ── Live cache (single JSON, daily TTL) ──────────────────────────────────────

def _load_live_cache() -> dict:
    try:
        if _LIVE_CACHE_FILE.exists():
            raw = json.loads(_LIVE_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _LIVE_CACHE_TTL:
                return raw
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}


def _save_live_cache(data: dict) -> None:
    try:
        _LIVE_CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _LIVE_CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# ── Historical per-(team, season) cache ──────────────────────────────────────

def _hist_cache_path(team_id: int, season: int) -> Path:
    return _HIST_CACHE_DIR / f"mlb_hist_bsplits_{team_id}_{season}.json"


def _hist_cache_get(team_id: int, season: int) -> Optional[list]:
    p = _hist_cache_path(team_id, season)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > _HIST_CACHE_TTL:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _hist_cache_set(team_id: int, season: int, value: list) -> None:
    try:
        _HIST_CACHE_DIR.mkdir(exist_ok=True)
        _hist_cache_path(team_id, season).write_text(json.dumps(value), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


# ── API fetchers ─────────────────────────────────────────────────────────────

def _fetch_roster_with_pa(team_id: int, season: int, top_n: int = 5) -> list[dict]:
    """
    Return the top-N batters by plate appearances on the given team for the
    season:  [{batter_id, name, pa}, ...]  sorted desc.
    """
    # Hydrate stats inline so we get PA without N batter-level calls.
    url = (f"{_BASE}/teams/{team_id}/roster?rosterType=fullSeason"
           f"&season={season}"
           f"&hydrate=stats(group=hitting,type=season,season={season})")
    data = _fetch_url_ua(url)
    out: list[dict] = []
    for member in data.get("roster", []):
        person = member.get("person", {}) or {}
        pid    = person.get("id")
        name   = person.get("fullName", "")
        if not pid:
            continue
        # Skip pitchers — they'd dilute the top-5 with rare-PA entries.
        if (member.get("position", {}) or {}).get("code") == "1":
            continue
        pa = 0.0
        for s in (person.get("stats") or []):
            for split in s.get("splits", []) or []:
                pa_v = _safe(split.get("stat", {}).get("plateAppearances"), 0.0)
                if pa_v > pa:
                    pa = pa_v
        if pa > 0:
            out.append({"batter_id": pid, "name": name, "pa": pa})

    out.sort(key=lambda r: r["pa"], reverse=True)
    return out[:top_n]


def _fetch_batter_lr_splits(batter_id: int, season: int) -> dict:
    """
    Return {"vs_l": {...}, "vs_r": {...}} for a batter's split lines.
    Each side has avg, obp, slg, k_rate; neutral defaults on miss.
    """
    url = (f"{_BASE}/people/{batter_id}/stats"
           f"?stats=statSplits&group=hitting&season={season}&sportId=1"
           f"&sitCodes=vl,vr")
    data = _fetch_url_ua(url)
    vs_l = dict(_NEUTRAL_BAT_SPLIT)
    vs_r = dict(_NEUTRAL_BAT_SPLIT)
    for grp in data.get("stats", []):
        for split in grp.get("splits", []):
            code = split.get("split", {}).get("code", "")
            st   = split.get("stat", {})
            avg  = _safe(st.get("avg"), None)
            obp  = _safe(st.get("obp"), None)
            slg  = _safe(st.get("slg"), None)
            so   = _safe(st.get("strikeOuts"), 0.0)
            pa   = _safe(st.get("plateAppearances"), 0.0)
            k_rate = (so / pa) if pa > 0 else None
            tgt = vs_l if code == "vl" else (vs_r if code == "vr" else None)
            if tgt is None:
                continue
            if avg is not None:    tgt["avg"]    = avg
            if obp is not None:    tgt["obp"]    = obp
            if slg is not None:    tgt["slg"]    = slg
            if k_rate is not None: tgt["k_rate"] = k_rate
    return {"vs_l": vs_l, "vs_r": vs_r}


def _build_team_top5(team_id: int, season: int) -> list[dict]:
    """Combine roster top-5 with per-batter LR splits."""
    out: list[dict] = []
    for batter in _fetch_roster_with_pa(team_id, season, top_n=5):
        splits = _fetch_batter_lr_splits(batter["batter_id"], season)
        out.append({**batter, **splits})
    return out


# ── Public API ───────────────────────────────────────────────────────────────

class BatterSplitsClient:
    """Daily-cached top-5 batters + L/R splits for live prediction."""

    def __init__(self):
        self._cache = _load_live_cache()
        self._dirty = False

    def get_top_batters(self, team_id: Optional[int], season: int) -> list[dict]:
        if not team_id:
            return []
        key = f"t_{team_id}_{season}"
        if key in self._cache:
            return self._cache[key]
        try:
            result = _build_team_top5(team_id, season)
        except Exception:
            result = []
        self._cache[key] = result
        self._dirty = True
        return result

    def get_lvs_inputs(
        self, team_id: Optional[int], season: int, opp_hand: int
    ) -> list[dict]:
        """
        For LVS: return [{ops, k_rate}, ...] of top-5 batters' splits against
        the opposing starter's handedness (opp_hand: 1 = LHP, 0 = RHP).
        """
        side = "vs_l" if opp_hand == 1 else "vs_r"
        rows: list[dict] = []
        for b in self.get_top_batters(team_id, season):
            split = b.get(side) or _NEUTRAL_BAT_SPLIT
            rows.append({
                "ops":    _ops(split),
                "k_rate": _safe(split.get("k_rate"), _NEUTRAL_BAT_SPLIT["k_rate"]),
            })
        return rows

    def save(self) -> None:
        if self._dirty:
            _save_live_cache(self._cache)
            self._dirty = False


def get_historical_top_batters(team_id: Optional[int], season: int) -> list[dict]:
    """Historical path with per-(team, season) disk cache."""
    if not team_id:
        return []
    cached = _hist_cache_get(team_id, season)
    if cached is not None:
        return cached
    try:
        result = _build_team_top5(team_id, season)
    except Exception:
        result = []
    _hist_cache_set(team_id, season, result)
    return result


def lvs_inputs_from_batters(batters: list[dict], opp_hand: int) -> list[dict]:
    """Pure helper: extract OPS + K% against opp_hand from a cached batter list."""
    side = "vs_l" if opp_hand == 1 else "vs_r"
    out: list[dict] = []
    for b in batters:
        split = b.get(side) or _NEUTRAL_BAT_SPLIT
        out.append({
            "ops":    _ops(split),
            "k_rate": _safe(split.get("k_rate"), _NEUTRAL_BAT_SPLIT["k_rate"]),
        })
    return out


_client: Optional[BatterSplitsClient] = None


def get_batter_splits_client() -> BatterSplitsClient:
    global _client
    if _client is None:
        _client = BatterSplitsClient()
    return _client
