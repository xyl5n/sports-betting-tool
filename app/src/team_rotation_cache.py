"""
team_rotation_cache.py
======================
Per-team quadrant data for the Team Rotation chart on the home page.

Computes season vs. recent-14-day performance for three metrics:

  ml  — straight-up win %
  ats — run-line cover % (proxy: team wins by 2+ runs; covers -1.5)
  ou  — over % (proxy: total runs > 8.5, the typical MLB line)

Data sources
------------
MLB  statsapi.mlb.com — free, keyless, official MLB Stats API
       /standings          → season W/L for ML
       /schedule           → game-by-game scores for ATS + O/U

WNBA site.api.espn.com/apis/v2/sports/basketball/wnba/standings
       Only ML is reliable; ATS/O/U fall back to ML win% for WNBA
       because WNBA spread/total data is not freely available.

Cache: in-process dict, one entry per (sport, metric, date), TTL 1 h.
A stale entry is served on fetch failure so the chart stays visible.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_TTL             = 3600          # 1-hour in-process cache
_OU_THRESHOLD    = 8.5           # MLB typical total; "over" if combined > this
_MIN_SEASON_GAMES = 5            # skip teams with fewer season games

_STATS_BASE      = "https://statsapi.mlb.com/api/v1"
_ESPN_WNBA_STANDINGS = (
    "https://site.api.espn.com/apis/v2/sports/basketball/wnba/standings"
)

_CACHE: dict[str, dict] = {}


def _log(msg: str) -> None:
    print(f"ROTATION: {msg}", flush=True, file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; sports-betting-ai/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:                                               # noqa: BLE001
        _log(f"fetch failed {url}: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _n_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _current_season() -> int:
    return datetime.now(timezone.utc).year


# ---------------------------------------------------------------------------
# MLB — standings (ML season record)
# ---------------------------------------------------------------------------

def _fetch_mlb_standings() -> dict[str, dict]:
    """Return {team_id_str: {name, wins, losses}} from MLB Stats API standings."""
    url = (
        f"{_STATS_BASE}/standings"
        f"?leagueId=103,104&season={_current_season()}"
    )
    data = _fetch_json(url)
    if not data:
        return {}

    result: dict[str, dict] = {}
    try:
        for div in (data.get("records") or []):
            for tr in (div.get("teamRecords") or []):
                team = tr.get("team") or {}
                tid  = str(team.get("id", ""))
                if not tid:
                    continue
                wins   = int(tr.get("wins")   or 0)
                losses = int(tr.get("losses") or 0)
                result[tid] = {
                    "name":   team.get("name") or "",
                    "wins":   wins,
                    "losses": losses,
                    "games":  wins + losses,
                }
    except Exception as exc:                                               # noqa: BLE001
        _log(f"MLB standings parse error: {exc}")
    return result


# ---------------------------------------------------------------------------
# MLB — abbreviation lookup (team_id → abbr)
# The standing endpoint returns only {id, name}; abbreviations come from
# /teams which is a separate light call.
# ---------------------------------------------------------------------------

_MLB_ID_TO_ABBR: dict[str, str] = {}   # populated on first fetch


def _fetch_mlb_abbr_map() -> dict[str, str]:
    """Fetch {team_id_str: abbr} once; cached in _MLB_ID_TO_ABBR."""
    global _MLB_ID_TO_ABBR
    if _MLB_ID_TO_ABBR:
        return _MLB_ID_TO_ABBR

    url = f"{_STATS_BASE}/teams?sportId=1&activeStatus=Y&fields=teams,id,abbreviation"
    data = _fetch_json(url)
    if not data:
        return _MLB_ID_TO_ABBR

    try:
        for team in (data.get("teams") or []):
            tid  = str(team.get("id", ""))
            abbr = (team.get("abbreviation") or "").upper()
            if tid and abbr:
                _MLB_ID_TO_ABBR[tid] = abbr
    except Exception as exc:                                               # noqa: BLE001
        _log(f"MLB abbr map parse error: {exc}")

    _log(f"loaded {len(_MLB_ID_TO_ABBR)} MLB team abbreviations")
    return _MLB_ID_TO_ABBR


# ---------------------------------------------------------------------------
# MLB — schedule (game-by-game scores for ATS + O/U)
# ---------------------------------------------------------------------------

def _fetch_mlb_games(start_date: str, end_date: str) -> list[dict]:
    """Fetch completed MLB regular-season games in [start_date, end_date].

    Returns list of {home_id, away_id, home_score, away_score} dicts.
    Uses minimal fields so the response stays under ~200 KB even for a
    full 60-day window (~900 games).
    """
    url = (
        f"{_STATS_BASE}/schedule"
        f"?sportId=1&startDate={start_date}&endDate={end_date}&gameType=R"
        f"&fields=dates,date,games,status,abstractGameState,teams,home,away,team,id,score"
    )
    data = _fetch_json(url, timeout=15)
    if not data:
        return []

    games: list[dict] = []
    try:
        for date_block in (data.get("dates") or []):
            for g in (date_block.get("games") or []):
                status = g.get("status") or {}
                if status.get("abstractGameState") != "Final":
                    continue
                teams = g.get("teams") or {}
                home  = teams.get("home") or {}
                away  = teams.get("away") or {}
                hs    = home.get("score")
                as_   = away.get("score")
                if hs is None or as_ is None:
                    continue
                try:
                    hs, as_ = int(hs), int(as_)
                except (TypeError, ValueError):
                    continue
                ht = home.get("team") or {}
                at = away.get("team") or {}
                games.append({
                    "home_id":    str(ht.get("id", "")),
                    "away_id":    str(at.get("id", "")),
                    "home_score": hs,
                    "away_score": as_,
                })
    except Exception as exc:                                               # noqa: BLE001
        _log(f"MLB schedule parse error: {exc}")
    return games


# ---------------------------------------------------------------------------
# Compute per-team ML / ATS / O/U records from raw game results
# ---------------------------------------------------------------------------

def _compute_game_records(games: list[dict]) -> dict[str, dict]:
    """Accumulate ML / ATS / O/U counters per team_id from a game list.

    ATS proxy: a team "covers -1.5" when the margin is ≥ 2 in their
    favour.  This is the standard run-line definition for the home team;
    we apply it symmetrically (away team also needs to win by 2+ to
    "cover as a favourite").  This is a *proxy* — we don't have actual
    spread data.

    O/U: total combined runs > 8.5 = "over".  Both teams in a game see
    the same O/U outcome.

    Returns {team_id_str: {ml_w, ml_l, ats_w, ats_l, ou_w, ou_l}}.
    """
    records: dict[str, dict] = {}

    def _rec(tid: str) -> dict:
        if tid not in records:
            records[tid] = {"ml_w": 0, "ml_l": 0,
                            "ats_w": 0, "ats_l": 0,
                            "ou_w": 0, "ou_l": 0}
        return records[tid]

    for g in games:
        hs  = g["home_score"]
        as_ = g["away_score"]
        hid = g["home_id"]
        aid = g["away_id"]
        if not hid or not aid:
            continue

        hr = _rec(hid)
        ar = _rec(aid)

        # ML
        if hs > as_:
            hr["ml_w"] += 1; ar["ml_l"] += 1
        elif as_ > hs:
            ar["ml_w"] += 1; hr["ml_l"] += 1
        # Ties are theoretically impossible in MLB but handled as no-op.

        # ATS (run-line proxy: win by ≥ 2 = cover)
        margin = hs - as_
        if margin >= 2:
            hr["ats_w"] += 1; ar["ats_l"] += 1
        else:
            ar["ats_w"] += 1; hr["ats_l"] += 1

        # O/U
        total = hs + as_
        if total > _OU_THRESHOLD:
            hr["ou_w"] += 1; ar["ou_w"] += 1
        else:
            hr["ou_l"] += 1; ar["ou_l"] += 1

    return records


# ---------------------------------------------------------------------------
# win% helper
# ---------------------------------------------------------------------------

def _pct(w: int, l: int) -> Optional[float]:
    g = w + l
    return (w / g) if g else None


# ---------------------------------------------------------------------------
# Public API — MLB
# ---------------------------------------------------------------------------

def _build_mlb(metric: str) -> list[dict]:
    season_start = f"{_current_season()}-03-20"
    today        = _today_str()
    recent_start = _n_days_ago(14)

    standings = _fetch_mlb_standings()
    abbr_map  = _fetch_mlb_abbr_map()

    if not standings:
        return []

    recent_games = _fetch_mlb_games(recent_start, today)
    recent_by_id = _compute_game_records(recent_games)

    # Full-season game log needed only for ATS / O/U
    if metric in ("ats", "ou"):
        season_games = _fetch_mlb_games(season_start, today)
        season_by_id = _compute_game_records(season_games)
    else:
        season_by_id = {}

    _log(
        f"MLB {metric}: {len(standings)} teams, "
        f"{len(recent_games)} recent games, "
        f"{len(season_by_id)} teams in season game log"
    )

    out: list[dict] = []
    for tid, s in standings.items():
        abbr = abbr_map.get(tid, "")
        if not abbr or s["games"] < _MIN_SEASON_GAMES:
            continue

        # ── Season performance ──────────────────────────────────────────
        if metric == "ml":
            szn_w, szn_l = s["wins"], s["losses"]
        else:
            sr = season_by_id.get(tid) or {}
            if metric == "ats":
                szn_w, szn_l = sr.get("ats_w", 0), sr.get("ats_l", 0)
            else:
                szn_w, szn_l = sr.get("ou_w",  0), sr.get("ou_l",  0)

        szn_pct = _pct(szn_w, szn_l)
        if szn_pct is None:
            continue

        # ── Recent performance (last 14 days) ───────────────────────────
        rr = recent_by_id.get(tid) or {}
        if metric == "ml":
            rec_w, rec_l = rr.get("ml_w", 0), rr.get("ml_l", 0)
        elif metric == "ats":
            rec_w, rec_l = rr.get("ats_w", 0), rr.get("ats_l", 0)
        else:
            rec_w, rec_l = rr.get("ou_w", 0), rr.get("ou_l", 0)

        rec_pct = _pct(rec_w, rec_l)
        if rec_pct is None:
            # No games in last 14 days — fall back to season figure so the
            # team still appears on the chart (at the diagonal).
            rec_pct = szn_pct
            rec_w   = szn_w
            rec_l   = szn_l

        out.append({
            "abbr":  abbr,
            "name":  s["name"],
            "x":     round(rec_pct, 4),
            "y":     round(szn_pct, 4),
            "szn_w": szn_w,
            "szn_l": szn_l,
            "rec_w": rec_w,
            "rec_l": rec_l,
        })

    return out


# ---------------------------------------------------------------------------
# Public API — WNBA
# ---------------------------------------------------------------------------

def _build_wnba(metric: str) -> list[dict]:
    """Build WNBA rotation data from ESPN standings.

    ATS and O/U fall back to ML win% for WNBA — no free spread/total
    data source is available for WNBA.  The axis label in the chart
    notes the fallback.
    """
    data = _fetch_json(_ESPN_WNBA_STANDINGS)
    if not data:
        return []

    out: list[dict] = []
    try:
        for child in (data.get("children") or []):
            for entry in child.get("standings", {}).get("entries") or []:
                team = entry.get("team") or {}
                abbr = (team.get("abbreviation") or "").upper()
                name = team.get("displayName") or abbr
                if not abbr:
                    continue

                # Build stat lookup
                stat_by_name: dict[str, dict] = {
                    s["name"]: s
                    for s in (entry.get("stats") or [])
                    if isinstance(s, dict) and s.get("name")
                }

                def _sv(key: str, default=0.0):
                    s = stat_by_name.get(key) or {}
                    return s.get("value") or default

                wins   = int(_sv("wins"))
                losses = int(_sv("losses"))
                games  = wins + losses
                if games < 2:
                    continue

                szn_pct = _pct(wins, losses)
                if szn_pct is None:
                    continue

                # "Last Ten Games" displayValue is "W-L" e.g. "7-3"
                rec_w = rec_l = 0
                l10 = (stat_by_name.get("Last Ten Games") or {}).get("displayValue") or ""
                if isinstance(l10, str) and "-" in l10:
                    parts = l10.split("-", 1)
                    try:
                        rec_w, rec_l = int(parts[0].strip()), int(parts[1].strip())
                    except (ValueError, IndexError):
                        pass

                rec_pct = _pct(rec_w, rec_l) if (rec_w + rec_l) > 0 else szn_pct
                if rec_pct is None:
                    rec_pct = szn_pct

                out.append({
                    "abbr":  abbr,
                    "name":  name,
                    "x":     round(rec_pct, 4),
                    "y":     round(szn_pct, 4),
                    "szn_w": wins,
                    "szn_l": losses,
                    "rec_w": rec_w,
                    "rec_l": rec_l,
                    # Flag so the chart can note WNBA ATS/OU are ML proxies
                    "ml_proxy": metric in ("ats", "ou"),
                })
    except Exception as exc:                                               # noqa: BLE001
        _log(f"WNBA standings parse error: {exc}")

    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_rotation_data(sport: str = "mlb", metric: str = "ml") -> list[dict]:
    """Return per-team quadrant points for *sport* and *metric*.

    Each dict in the returned list:
        abbr    str    team abbreviation (e.g. "NYY")
        name    str    full team name
        x       float  recent-14d performance, 0–1 (→ x-axis)
        y       float  season performance, 0–1   (→ y-axis)
        szn_w   int    season wins  (for the selected metric)
        szn_l   int    season losses
        rec_w   int    recent wins
        rec_l   int    recent losses
        ml_proxy bool  True when ATS/OU data is unavailable and ML was used

    Returns [] on any unrecoverable error.  A stale cached entry is
    returned instead of [] when the cache has data but the fresh fetch
    failed.
    """
    sport  = (sport  or "mlb").lower()
    metric = (metric or "ml").lower()
    if metric not in ("ml", "ats", "ou"):
        metric = "ml"

    cache_key = f"{sport}_{metric}_{_today_str()}"
    entry     = _CACHE.get(cache_key)
    if entry and (time.monotonic() - entry["ts"]) < _TTL:
        return entry["data"]

    try:
        if sport == "wnba":
            data = _build_wnba(metric)
        else:
            data = _build_mlb(metric)
    except Exception as exc:                                               # noqa: BLE001
        _log(f"get_rotation_data({sport}, {metric}) failed: {exc}")
        data = []

    if data:
        _CACHE[cache_key] = {"ts": time.monotonic(), "data": data}
        _log(f"cached {len(data)} {sport}/{metric} teams")
    elif entry:
        _log(f"fetch failed — serving stale cache for {cache_key}")
        return entry["data"]

    return data
