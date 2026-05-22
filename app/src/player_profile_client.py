"""
player_profile_client.py
========================
MLB Stats API client for the player profile page (/player/mlb/{player_id}).

Data sources
-----------
All data comes from the FREE MLB Stats API (statsapi.mlb.com — no key required):
  Person info:   GET /api/v1/people/{id}?hydrate=currentTeam
  Season stats:  GET /api/v1/people/{id}/stats?stats=season&season=YEAR&group=pitching|hitting
  Game log:      GET /api/v1/people/{id}/stats?stats=gameLog&season=YEAR&group=pitching|hitting
  Home/away:     GET /api/v1/people/{id}/stats?stats=statSplits&season=YEAR&group=pitching|hitting&sitCodes=h,a
  Name search:   GET /api/v1/people/search?names={encoded}&season=YEAR&sportId=1

Caching
-------
Game logs are cached in Supabase app_cache (key = "player_gamelog_{id}_{season}") AND
in a local JSON file (.cache/player_{id}_{season}.json) refreshed once per calendar day.
Player info is cached in-process only (module-level dict, reset per deploy).

The public functions never raise — they return sensible defaults on any error.
All log lines are prefixed PLAYER-CLIENT so they are easy to grep in Railway logs.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db as _db

_STATS_BASE     = "https://statsapi.mlb.com/api/v1"
_CACHE_DIR      = Path(".cache")
_HTTP_TIMEOUT   = 12
_HTTP_SLEEP     = 0.05
_CURRENT_SEASON = 2025

# Module-level in-process caches (reset each Railway deploy — that's fine)
_player_info_cache: dict[int, dict] = {}   # player_id -> info dict
_name_to_id_cache:  dict[str, int]  = {}   # lowercased name -> player_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"PLAYER-CLIENT: {msg}", file=sys.stderr, flush=True)


def _fetch_json(url: str, *, label: str, retries: int = 2) -> Optional[dict]:
    """GET *url*, parse JSON, return dict or None on any error."""
    time.sleep(_HTTP_SLEEP)
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "sports-betting-ai/1.0"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 or exc.code >= 500:
                wait = 2 ** attempt
                _log(f"{label} HTTP {exc.code} — retrying in {wait}s (attempt {attempt + 1}/{retries + 1})")
                time.sleep(wait)
                continue
            _log(f"{label} HTTP error {exc.code}: {exc}")
            return None
        except Exception as exc:
            last_exc = exc
            _log(f"{label} error (attempt {attempt + 1}/{retries + 1}): {exc}")
            if attempt < retries:
                time.sleep(2 ** attempt)
    _log(f"{label} all retries exhausted: {last_exc}")
    return None


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_ip(value) -> float:
    """Convert MLB innings-pitched string (e.g. '6.2') to a float (e.g. 6.667)."""
    if value is None or value == "":
        return 0.0
    try:
        s = str(value)
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + float(frac) / 3.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Player info
# ---------------------------------------------------------------------------

def get_player_info(player_id: int) -> dict:
    """Return basic biographical/roster information for *player_id*.

    Keys: id, name, first_name, last_name, position_code, position_name,
          team_abbrev, team_name, jersey_number, bats, throws, birth_date, active
    Returns a default dict with empty strings / False on any failure.
    """
    _default = {
        "id": player_id,
        "name": "",
        "first_name": "",
        "last_name": "",
        "position_code": "",
        "position_name": "",
        "team_abbrev": "",
        "team_name": "",
        "jersey_number": "",
        "bats": "",
        "throws": "",
        "birth_date": "",
        "active": False,
    }

    if player_id in _player_info_cache:
        return _player_info_cache[player_id]

    url  = f"{_STATS_BASE}/people/{player_id}?hydrate=currentTeam"
    data = _fetch_json(url, label=f"get_player_info({player_id})")
    if not data:
        return _default

    try:
        people = data.get("people") or []
        if not people:
            return _default
        p = people[0]

        pos   = p.get("primaryPosition") or {}
        team  = p.get("currentTeam") or {}
        bats  = (p.get("batSide") or {}).get("code", "")
        throws = (p.get("pitchHand") or {}).get("code", "")

        result = {
            "id":            int(p.get("id", player_id)),
            "name":          p.get("fullName", ""),
            "first_name":    p.get("firstName", ""),
            "last_name":     p.get("lastName", ""),
            "position_code": pos.get("code", ""),
            "position_name": pos.get("name", ""),
            "team_abbrev":   team.get("abbreviation", ""),
            "team_name":     team.get("name", ""),
            "jersey_number": str(p.get("primaryNumber", "")),
            "bats":          bats,
            "throws":        throws,
            "birth_date":    p.get("birthDate", ""),
            "active":        bool(p.get("active", False)),
        }
        _player_info_cache[player_id] = result
        return result
    except Exception as exc:
        _log(f"get_player_info({player_id}) parse error: {exc}")
        return _default


# ---------------------------------------------------------------------------
# Name search / slug resolution
# ---------------------------------------------------------------------------

def search_player_by_name(name: str) -> Optional[int]:
    """Search the MLB Stats API for *name* and return the player ID or None."""
    key = name.lower()
    if key in _name_to_id_cache:
        return _name_to_id_cache[key]

    encoded = urllib.parse.quote(name)
    url     = (
        f"{_STATS_BASE}/people/search"
        f"?names={encoded}&season={_CURRENT_SEASON}&sportId=1"
    )
    data = _fetch_json(url, label=f"search_player_by_name({name!r})")
    if not data:
        return None

    try:
        people = data.get("people") or []
        for person in people:
            full = (person.get("fullName") or "").lower()
            if key in full or full in key:
                pid = int(person["id"])
                _name_to_id_cache[key] = pid
                return pid
        if people:
            pid = int(people[0]["id"])
            _name_to_id_cache[key] = pid
            return pid
        return None
    except Exception as exc:
        _log(f"search_player_by_name({name!r}) parse error: {exc}")
        return None


def resolve_player_id(player_id_or_slug: str) -> Optional[int]:
    """Convert a raw URL segment to a numeric MLB player ID.

    Accepts a plain integer string ("592450") or a hyphenated name slug
    ("shohei-ohtani").  Returns None if the lookup fails.
    """
    if player_id_or_slug.isdigit():
        return int(player_id_or_slug)
    name = player_id_or_slug.replace("-", " ")
    return search_player_by_name(name)


# ---------------------------------------------------------------------------
# Season stats
# ---------------------------------------------------------------------------

def get_season_stats(player_id: int, *, is_pitcher: bool) -> dict:
    """Return current-season aggregate stats for *player_id*."""
    group = "pitching" if is_pitcher else "hitting"
    url   = (
        f"{_STATS_BASE}/people/{player_id}/stats"
        f"?stats=season&season={_CURRENT_SEASON}&group={group}"
    )
    data = _fetch_json(url, label=f"get_season_stats({player_id}, {group})")

    _empty_pitcher = {
        "era": 0.0, "whip": 0.0, "k9": 0.0, "bb9": 0.0,
        "ip": 0.0, "wins": 0, "losses": 0, "saves": 0,
        "games": 0, "games_started": 0,
        "strikeouts": 0, "walks": 0, "hits_allowed": 0,
    }
    _empty_batter = {
        "avg": 0.0, "obp": 0.0, "slg": 0.0, "ops": 0.0,
        "hr": 0, "rbi": 0, "runs": 0, "sb": 0,
        "hits": 0, "doubles": 0, "triples": 0,
        "ab": 0, "pa": 0, "strikeouts": 0, "walks": 0, "tb": 0,
    }

    if not data:
        return _empty_pitcher if is_pitcher else _empty_batter

    try:
        stats_list = data.get("stats") or []
        if not stats_list:
            return _empty_pitcher if is_pitcher else _empty_batter
        splits = stats_list[0].get("splits") or []
        if not splits:
            return _empty_pitcher if is_pitcher else _empty_batter
        st = splits[0].get("stat") or {}

        if is_pitcher:
            ip_val = _parse_ip(st.get("inningsPitched", "0"))
            k9 = round(float(st.get("strikeOuts", 0)) * 9 / max(ip_val, 0.01), 2)
            bb9 = round(float(st.get("baseOnBalls", 0)) * 9 / max(ip_val, 0.01), 2)
            return {
                "era":           float(st.get("era") or 0.0),
                "whip":          float(st.get("whip") or 0.0),
                "k9":            k9,
                "bb9":           bb9,
                "ip":            round(ip_val, 2),
                "wins":          int(st.get("wins") or 0),
                "losses":        int(st.get("losses") or 0),
                "saves":         int(st.get("saves") or 0),
                "games":         int(st.get("gamesPitched") or 0),
                "games_started": int(st.get("gamesStarted") or 0),
                "strikeouts":    int(st.get("strikeOuts") or 0),
                "walks":         int(st.get("baseOnBalls") or 0),
                "hits_allowed":  int(st.get("hits") or 0),
            }
        else:
            return {
                "avg":       float(st.get("avg") or 0.0),
                "obp":       float(st.get("obp") or 0.0),
                "slg":       float(st.get("slg") or 0.0),
                "ops":       float(st.get("ops") or 0.0),
                "hr":        int(st.get("homeRuns") or 0),
                "rbi":       int(st.get("rbi") or 0),
                "runs":      int(st.get("runs") or 0),
                "sb":        int(st.get("stolenBases") or 0),
                "hits":      int(st.get("hits") or 0),
                "doubles":   int(st.get("doubles") or 0),
                "triples":   int(st.get("triples") or 0),
                "ab":        int(st.get("atBats") or 0),
                "pa":        int(st.get("plateAppearances") or 0),
                "strikeouts":int(st.get("strikeOuts") or 0),
                "walks":     int(st.get("baseOnBalls") or 0),
                "tb":        int(st.get("totalBases") or 0),
            }
    except Exception as exc:
        _log(f"get_season_stats({player_id}) parse error: {exc}")
        return _empty_pitcher if is_pitcher else _empty_batter


# ---------------------------------------------------------------------------
# Home / away splits
# ---------------------------------------------------------------------------

def get_season_splits(player_id: int, *, is_pitcher: bool) -> dict:
    """Return home/away split stats for *player_id* in the current season."""
    group = "pitching" if is_pitcher else "hitting"
    url   = (
        f"{_STATS_BASE}/people/{player_id}/stats"
        f"?stats=statSplits&season={_CURRENT_SEASON}&group={group}&sitCodes=h,a"
    )
    data = _fetch_json(url, label=f"get_season_splits({player_id}, {group})")
    if not data:
        return {}

    try:
        stats_list = data.get("stats") or []
        if not stats_list:
            return {}
        splits = stats_list[0].get("splits") or []

        home_st: dict = {}
        away_st: dict = {}
        for split in splits:
            code = (split.get("split") or {}).get("code", "")
            st   = split.get("stat") or {}
            if code == "h":
                home_st = st
            elif code == "a":
                away_st = st

        if is_pitcher:
            return {
                "home_era": float(home_st.get("era") or 0.0),
                "away_era": float(away_st.get("era") or 0.0),
            }
        else:
            return {
                "home_avg": float(home_st.get("avg") or 0.0),
                "away_avg": float(away_st.get("avg") or 0.0),
                "home_ops": float(home_st.get("ops") or 0.0),
                "away_ops": float(away_st.get("ops") or 0.0),
            }
    except Exception as exc:
        _log(f"get_season_splits({player_id}) parse error: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Game log (dual-cached)
# ---------------------------------------------------------------------------

def _local_cache_path(player_id: int, season: int) -> Path:
    return _CACHE_DIR / f"player_{player_id}_{season}.json"


def _load_local_cache(player_id: int, season: int) -> Optional[list[dict]]:
    path = _local_cache_path(player_id, season)
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("date") == _today_str():
            return data.get("games") or []
        return None
    except Exception as exc:
        _log(f"_load_local_cache({player_id}, {season}) error: {exc}")
        return None


def _load_stale_local_cache(player_id: int, season: int) -> Optional[list[dict]]:
    """Return cached games regardless of date (fallback on API failure)."""
    path = _local_cache_path(player_id, season)
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("games") or []
    except Exception:
        return None


def _write_local_cache(player_id: int, season: int, games: list[dict]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "date":      _today_str(),
            "player_id": player_id,
            "season":    season,
            "games":     games,
        }
        path = _local_cache_path(player_id, season)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        _log(f"_write_local_cache({player_id}, {season}) error: {exc}")


def get_player_gamelog(
    player_id: int,
    season: int,
    *,
    is_pitcher: bool,
) -> list[dict]:
    """Return per-game log for *player_id* for *season*, cached aggressively.

    Pitchers: up to 30 most-recent games.
    Batters:  up to 50 most-recent games.
    """
    limit = 30 if is_pitcher else 50
    cache_key = f"player_gamelog_{player_id}_{season}"

    # 1. Local file cache
    local = _load_local_cache(player_id, season)
    if local is not None:
        return local[:limit]

    # 2. Supabase cache
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == _today_str():
            games = (row.get("data") or {}).get("games") or []
            _write_local_cache(player_id, season, games)
            return games[:limit]
    except Exception as exc:
        _log(f"Supabase cache_get({cache_key}) error: {exc}")

    # 3. Fetch from MLB Stats API
    group = "pitching" if is_pitcher else "hitting"
    url   = (
        f"{_STATS_BASE}/people/{player_id}/stats"
        f"?stats=gameLog&season={season}&group={group}"
    )
    data = _fetch_json(url, label=f"get_player_gamelog({player_id}, {season}, {group})")

    if not data:
        # Stale fallback
        stale = _load_stale_local_cache(player_id, season)
        if stale is not None:
            _log(f"Returning stale local cache for player {player_id} season {season}")
            return stale[:limit]
        try:
            row = _db.cache_get(cache_key)
            if row:
                games = (row.get("data") or {}).get("games") or []
                _log(f"Returning stale Supabase cache for player {player_id} season {season}")
                return games[:limit]
        except Exception:
            pass
        return []

    try:
        stats_list = data.get("stats") or [{}]
        raw_splits  = (stats_list[0] if stats_list else {}).get("splits") or []
        games: list[dict] = []

        for split in raw_splits:
            try:
                st          = split.get("stat") or {}
                date_str    = split.get("date", "")
                opp_info    = split.get("opponent") or {}
                opp_abbrev  = opp_info.get("abbreviation", "")
                team_info   = split.get("team") or {}
                team_abbrev = team_info.get("abbreviation", "")
                is_home     = bool(split.get("isHome", False))

                if is_pitcher:
                    ip_raw  = st.get("inningsPitched", "0.0")
                    ip_val  = _parse_ip(ip_raw)
                    park_team = team_abbrev if is_home else opp_abbrev
                    game = {
                        "date":           date_str,
                        "opp":            opp_abbrev,
                        "park_team":      park_team,
                        "is_home":        is_home,
                        "K":              int(st.get("strikeOuts") or 0),
                        "BB":             int(st.get("baseOnBalls") or 0),
                        "H":              int(st.get("hits") or 0),
                        "ER":             int(st.get("earnedRuns") or 0),
                        "IP_raw":         ip_raw,
                        "IP":             round(ip_val, 3),
                        "games_started":  int(st.get("gamesStarted") or 0),
                        "HR_allowed":     int(st.get("homeRuns") or 0),
                        "pitches_thrown": int(st.get("pitchesThrown") or 0),
                    }
                else:
                    game = {
                        "date":          date_str,
                        "opp":           opp_abbrev,
                        "is_home":       is_home,
                        "AB":            int(st.get("atBats") or 0),
                        "H":             int(st.get("hits") or 0),
                        "HR":            int(st.get("homeRuns") or 0),
                        "RBI":           int(st.get("rbi") or 0),
                        "R":             int(st.get("runs") or 0),
                        "BB":            int(st.get("baseOnBalls") or 0),
                        "SO":            int(st.get("strikeOuts") or 0),
                        "TB":            int(st.get("totalBases") or 0),
                        "SB":            int(st.get("stolenBases") or 0),
                        "PA":            int(st.get("plateAppearances") or 0),
                        "batting_order": int(st.get("battingOrder") or 0),
                    }
                games.append(game)
            except Exception as exc:
                _log(f"get_player_gamelog({player_id}) split parse error: {exc}")
                continue

        # Sort by date ascending, then return most recent *limit*
        games.sort(key=lambda g: g.get("date", ""))
        games = games[-limit:]

        # Persist to both caches
        _write_local_cache(player_id, season, games)
        try:
            _db.cache_set(
                cache_key,
                "mlb",
                _today_str(),
                {"games": games},
            )
        except Exception as exc:
            _log(f"Supabase cache_set({cache_key}) error: {exc}")

        return games

    except Exception as exc:
        _log(f"get_player_gamelog({player_id}, {season}) parse error: {exc}")
        # Stale fallback
        stale = _load_stale_local_cache(player_id, season)
        if stale is not None:
            return stale[:limit]
        try:
            row = _db.cache_get(cache_key)
            if row:
                return ((row.get("data") or {}).get("games") or [])[:limit]
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Today's prop prediction
# ---------------------------------------------------------------------------

def get_today_prop(player_name: str) -> Optional[dict]:
    """Return the highest-confidence prop prediction for *player_name* today.

    Resolution order
    ----------------
    1. Already-scored picks in ``data/daily_picks.json`` (``prop_picks`` list).
       These were scored by ``_collect_props()`` in the same worker process that
       ran analysis, so they reflect a consistent model state.  Reading them
       here avoids re-scoring and guarantees the side shown on the player page
       matches what the model selected.

    2. Live re-score fallback — used when the player is not in the top-5
       daily props or the daily_picks file is stale/absent.  Before calling
       ``predict()``, we inject the correct ``is_home`` from today's pitcher
       schedule so the model uses today's game context instead of the
       training-snapshot's (potentially stale) home/away value.  Without this
       injection the feature vector may differ from the one used on the Props
       page (which may have run on a different Railway worker with a different
       in-memory snapshot), causing the side to flip.

    Returns None if props are unavailable or the player has no line today.
    """
    name_lower = player_name.strip().lower()

    # ── Part 1: read from already-scored daily prop_picks ─────────────────────
    # Prefer the cached scored result so the player page shows exactly the
    # same pick that _collect_props() selected, without re-running predict().
    try:
        from .daily_picks import load_daily_picks
        daily     = load_daily_picks()
        prop_picks = (daily.get("picks") or {}).get("prop_picks") or []
        for pp in prop_picks:
            if (pp.get("player") or "").strip().lower() == name_lower:
                _log(
                    f"get_today_prop({player_name!r}): "
                    f"served from daily_picks cache "
                    f"({pp.get('market')} {pp.get('side')} {pp.get('line')} "
                    f"conf={pp.get('confidence')})"
                )
                return {
                    "market":          pp.get("market"),
                    "line":            pp.get("line"),
                    "side":            pp.get("side"),
                    "best_odds":       pp.get("best_odds"),
                    "recommendation":  pp.get("recommendation"),
                    "confidence":      pp.get("confidence"),
                    "predicted_value": pp.get("predicted_value"),
                }
    except Exception as exc:
        _log(f"get_today_prop({player_name!r}): daily_picks cache read failed: {exc}")

    # ── Part 2: live re-score with correct is_home from today's schedule ───────
    # Inject is_home into each prop dict before calling predict() so that
    # _build_reg_vector overrides the snapshot's stale training-time value
    # (props_model.py line: "if prop.get('is_home') is not None: vec[...] = ...").
    # This makes the feature vector consistent regardless of which Railway
    # worker handles the request or whether the pitcher snapshot is warm.
    try:
        from .props_client import get_client as _get_props_client
        from .props_model  import predict    as _predict
    except Exception:
        return None
    try:
        payload     = _get_props_client().get_today_props() or {}
        all_markets = payload.get("markets") or {}
        best: Optional[dict] = None
        for market, props in all_markets.items():
            for p in (props or []):
                if (p.get("player_name") or "").strip().lower() != name_lower:
                    continue
                # Inject today's is_home so the model feature vector is
                # consistent across workers.  For pitcher markets the schedule
                # lookup is cheap (pitcher_client caches the schedule).
                p = _inject_is_home(p)
                try:
                    pred = _predict(p)
                except Exception:
                    continue
                score = float(pred.get("confidence") or 0.0)
                if best is None or score > best["confidence"]:
                    best = {
                        "market":          market,
                        "line":            p.get("line"),
                        "side":            (p.get("side") or "Over").strip().title(),
                        "best_odds":       p.get("best_odds"),
                        "recommendation":  pred.get("recommendation"),
                        "confidence":      round(score, 4),
                        "predicted_value": pred.get("predicted_value"),
                    }
        return best
    except Exception as exc:
        _log(f"get_today_prop({player_name!r}) failed: {exc}")
        return None


def get_today_props_for_player(player_name: str) -> list[dict]:
    """Return EVERY prop prediction for *player_name* today, one per
    (market, line, side) combination, with the model's recommendation
    + confidence + predicted_value attached.

    Used by the player profile page so a starting pitcher with a
    strikeouts line AND an outs line AND a hits-allowed line shows
    three separate charts -- not just the highest-confidence one.

    Resolution order mirrors get_today_prop(), but instead of returning
    only the best entry it deduplicates by (market, line, side) and
    returns every match.

    Returns [] when no props are available for the player today.
    """
    name_lower = player_name.strip().lower()
    out: list[dict] = []
    seen: set[tuple] = set()

    def _key(entry: dict) -> tuple:
        return (
            entry.get("market") or "",
            entry.get("line"),
            (entry.get("side") or "").strip().lower(),
        )

    # ── Part 1: scored entries in daily_picks ─────────────────────────────────
    try:
        from .daily_picks import load_daily_picks
        daily      = load_daily_picks()
        prop_picks = (daily.get("picks") or {}).get("prop_picks") or []
        for pp in prop_picks:
            if (pp.get("player") or "").strip().lower() != name_lower:
                continue
            entry = {
                "market":          pp.get("market"),
                "line":            pp.get("line"),
                "side":            (pp.get("side") or "Over").strip().title(),
                "best_odds":       pp.get("best_odds"),
                "recommendation":  pp.get("recommendation"),
                "confidence":      pp.get("confidence"),
                "predicted_value": pp.get("predicted_value"),
                "source":          "daily_picks",
            }
            k = _key(entry)
            if k not in seen:
                seen.add(k)
                out.append(entry)
    except Exception as exc:
        _log(f"get_today_props_for_player({player_name!r}): daily_picks read failed: {exc}")

    # ── Part 2: every live prop, re-scored ────────────────────────────────────
    # Loops every (market, props) bucket from props_client and predicts
    # one row per side, so a player with Over+Under listings yields a single
    # consolidated entry per (market, line) keyed by the side the model
    # actually recommends (highest score wins between the two sides).
    try:
        from .props_client import (
            get_client as _get_props_client,
            ALL_PITCHER_MARKETS,
            ALL_BATTER_MARKETS,
        )
        from .props_model  import predict as _predict
    except Exception:
        return out

    try:
        payload     = _get_props_client().get_today_props() or {}
        all_markets = payload.get("markets") or {}
        # Bucket per (market, line) so over+under collapse into one entry,
        # keeping whichever side has the higher model confidence.  Matches
        # how pages/props.py renders dedup'd rows.
        per_line: dict[tuple, dict] = {}
        for market, props in all_markets.items():
            for p in (props or []):
                if (p.get("player_name") or "").strip().lower() != name_lower:
                    continue
                try:
                    line_f = float(p.get("line"))
                except (TypeError, ValueError):
                    continue
                p = _inject_is_home(p)
                try:
                    pred = _predict(p)
                except Exception:
                    continue
                score = float(pred.get("confidence") or 0.0)
                key   = (market, line_f)
                existing = per_line.get(key)
                if existing is None or score > float(existing["confidence"] or 0.0):
                    per_line[key] = {
                        "market":          market,
                        "line":            line_f,
                        "side":            (p.get("side") or "Over").strip().title(),
                        "best_odds":       p.get("best_odds"),
                        "best_book":       p.get("best_book"),
                        "recommendation":  pred.get("recommendation"),
                        "confidence":      round(score, 4),
                        "predicted_value": pred.get("predicted_value"),
                        "source":          "live",
                    }

        for entry in per_line.values():
            k = _key(entry)
            if k not in seen:
                seen.add(k)
                out.append(entry)
    except Exception as exc:
        _log(f"get_today_props_for_player({player_name!r}) live re-score failed: {exc}")

    # Sort by confidence DESC so the strongest pick lands first on the page.
    out.sort(key=lambda e: -float(e.get("confidence") or 0.0))
    _log(
        f"get_today_props_for_player({player_name!r}): "
        f"{len(out)} prop(s) "
        f"[markets={[e.get('market') for e in out]}]"
    )
    return out


def _inject_is_home(prop: dict) -> dict:
    """Return a shallow copy of *prop* with ``is_home`` set from today's
    pitcher schedule, or the original dict unchanged if the lookup fails.

    Setting ``is_home`` on the prop dict causes ``_build_reg_vector`` in
    props_model.py to override the snapshot's training-time ``is_home_i``
    with today's correct home/away context.

    Only active for pitcher markets (the field only moves the needle for
    pitchers; batters are handled via snapshot/league-median fallback).
    """
    market = prop.get("market") or ""
    if not market.startswith("pitcher_"):
        return prop

    player_name = (prop.get("player_name") or "").strip()
    commence    = (prop.get("commence_time") or "").strip()
    date_str    = commence[:10] if commence else None
    if not player_name or not date_str:
        return prop

    try:
        from .pitcher_inference_features import get_is_home_for_pitcher
        is_home = get_is_home_for_pitcher(player_name, date_str)
        if is_home is not None:
            _log(
                f"_inject_is_home: {player_name!r} {date_str} "
                f"is_home={is_home} (market={market})"
            )
            return dict(prop, is_home=is_home)
    except Exception as exc:
        _log(f"_inject_is_home({player_name!r}) failed: {exc}")
    return prop


# ---------------------------------------------------------------------------
# Player prop performance summary
# ---------------------------------------------------------------------------
#
# Both the player profile page (tabbed market view) and the props page
# (inline card summaries) need the same shape of summary: how often a
# player has hit the OVER/UNDER on a given line across recent windows
# and head-to-head vs the day's opponent.  Centralising the helper here
# keeps the math in one place and means the pages just consume a flat
# dict of numbers.

# Market -> gamelog stat key.  Mirrors _MARKET_TO_STAT in pages/player.py
# but lives in the data client so the props page can share it without
# importing from the page module.
_MARKET_TO_GAMELOG_STAT: dict[str, str] = {
    "pitcher_strikeouts":   "K",
    "pitcher_earned_runs":  "ER",
    "pitcher_hits_allowed": "H",
    "pitcher_walks":        "BB",
    "pitcher_outs":         "outs",
    "batter_hits":          "H",
    "batter_total_bases":   "TB",
    "batter_home_runs":     "HR",
    "batter_rbis":          "RBI",
    "batter_runs_scored":   "R",
    "batter_walks":         "BB",
    "batter_strikeouts":    "SO",
    "batter_stolen_bases":  "SB",
}

_EMPTY_SUMMARY: dict = {
    "stat_key":      None,
    "line":          None,
    "side":          None,
    "season_avg":    None,
    "season_games":  0,
    "last_5_avg":    None,
    "last_5_hits":   0,
    "last_5_games":  0,
    "last_10_avg":   None,
    "last_10_hits":  0,
    "last_10_games": 0,
    "last_20_avg":   None,
    "last_20_hits":  0,
    "last_20_games": 0,
    "h2h_avg":       None,
    "h2h_hits":      0,
    "h2h_games":     0,
}


def gamelog_stat_value(game: dict, stat_key: str) -> float:
    """Numeric value for *stat_key* in a single gamelog row.

    ``outs`` is derived from innings pitched (IP * 3 rounded) because the
    gamelog stores IP but not raw outs.  Missing fields return 0.0 so
    aggregations never trip on sparse rows.
    """
    if stat_key == "outs":
        ip = game.get("IP")
        return float(round((ip if ip is not None else 0.0) * 3))
    raw = game.get(stat_key)
    try:
        return float(raw if raw is not None else 0)
    except (TypeError, ValueError):
        return 0.0


def _hit_count(values: list[float], line, side: str) -> int:
    """Number of entries in *values* that hit the side vs the line.

    Equal-to-line is a push and does NOT count as a hit.  Matches how
    sportsbooks settle most player-prop wagers (push refunds, not graded
    as a win for either side).
    """
    if not values:
        return 0
    try:
        line_f = float(line)
    except (TypeError, ValueError):
        return 0
    s = (side or "Over").strip().lower()
    if s == "under":
        return sum(1 for v in values if v < line_f)
    return sum(1 for v in values if v > line_f)


def _avg(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def get_player_prop_summary(
    player_name: str,
    market: str,
    line,
    side: str = "Over",
    *,
    opp_abbrev: Optional[str] = None,
    is_pitcher: Optional[bool] = None,
    games: Optional[list[dict]] = None,
) -> dict:
    """Compute season + recent-window performance for a (player, market, line).

    Used by:
      - pages/props.py to render inline summary chips inside each pick card
      - pages/player.py (multi-tab market view) to show season/L5/L10/L20/H2H
        snapshots above each prop's chart

    *games* may be supplied by the caller to avoid a duplicate fetch; if
    omitted we resolve the player_id and pull the cached gamelog.
    """
    stat_key = _MARKET_TO_GAMELOG_STAT.get(market)
    if stat_key is None:
        return dict(_EMPTY_SUMMARY)

    if games is None:
        pid = search_player_by_name(player_name)
        if not pid:
            return dict(_EMPTY_SUMMARY, stat_key=stat_key, line=line, side=side)
        if is_pitcher is None:
            info = get_player_info(pid)
            is_pitcher = (info.get("position_code") or "") == "1"
        games = get_player_gamelog(pid, _CURRENT_SEASON, is_pitcher=is_pitcher) or []
        if is_pitcher:
            games = [g for g in games if g.get("games_started", 0) > 0]

    if not games:
        return dict(_EMPTY_SUMMARY, stat_key=stat_key, line=line, side=side)

    season_values = [gamelog_stat_value(g, stat_key) for g in games]
    last_5  = season_values[-5:]
    last_10 = season_values[-10:]
    last_20 = season_values[-20:]

    if opp_abbrev:
        opp_u = opp_abbrev.strip().upper()
        h2h_games = [g for g in games if (g.get("opp") or "").upper() == opp_u]
    else:
        h2h_games = []
    h2h_values = [gamelog_stat_value(g, stat_key) for g in h2h_games]

    return {
        "stat_key":      stat_key,
        "line":          line,
        "side":          side,
        "season_avg":    _avg(season_values),
        "season_games":  len(season_values),
        "season_hits":   _hit_count(season_values, line, side),
        "last_5_avg":    _avg(last_5),
        "last_5_hits":   _hit_count(last_5, line, side),
        "last_5_games":  len(last_5),
        "last_10_avg":   _avg(last_10),
        "last_10_hits":  _hit_count(last_10, line, side),
        "last_10_games": len(last_10),
        "last_20_avg":   _avg(last_20),
        "last_20_hits":  _hit_count(last_20, line, side),
        "last_20_games": len(last_20),
        "h2h_avg":       _avg(h2h_values),
        "h2h_hits":      _hit_count(h2h_values, line, side),
        "h2h_games":     len(h2h_values),
    }


# ---------------------------------------------------------------------------
# Team name -> abbrev + opponent helpers
# ---------------------------------------------------------------------------
#
# The Odds API returns full team names ("Atlanta Braves"); the MLB Stats
# API gamelog stores 3-letter abbreviations ("ATL").  Static map of all
# 30 teams keeps the cross-source join free of network calls.

_TEAM_NAME_TO_ABBREV: dict[str, str] = {
    "arizona diamondbacks":  "ARI",
    "atlanta braves":        "ATL",
    "baltimore orioles":     "BAL",
    "boston red sox":        "BOS",
    "chicago cubs":           "CHC",
    "chicago white sox":     "CWS",
    "cincinnati reds":       "CIN",
    "cleveland guardians":   "CLE",
    "colorado rockies":      "COL",
    "detroit tigers":        "DET",
    "houston astros":        "HOU",
    "kansas city royals":    "KC",
    "los angeles angels":    "LAA",
    "los angeles dodgers":   "LAD",
    "miami marlins":         "MIA",
    "milwaukee brewers":     "MIL",
    "minnesota twins":       "MIN",
    "new york mets":         "NYM",
    "new york yankees":      "NYY",
    "oakland athletics":     "OAK",
    "athletics":             "ATH",
    "philadelphia phillies": "PHI",
    "pittsburgh pirates":    "PIT",
    "san diego padres":      "SD",
    "san francisco giants":  "SF",
    "seattle mariners":      "SEA",
    "st. louis cardinals":   "STL",
    "st louis cardinals":    "STL",
    "tampa bay rays":        "TB",
    "texas rangers":         "TEX",
    "toronto blue jays":     "TOR",
    "washington nationals":  "WSH",
}


def team_name_to_abbrev(name: str) -> Optional[str]:
    """Best-effort lookup; returns None if the team isn't recognised."""
    if not name:
        return None
    return _TEAM_NAME_TO_ABBREV.get(name.strip().lower())


def get_player_today_opponent(player_name: str, prop: dict) -> Optional[str]:
    """Return the 3-letter abbreviation of the player's opponent today,
    or None when we can't determine which side of the matchup they're on.

    The prop dict from props_client carries the full names of both teams
    (home_team, away_team).  We resolve the player's currentTeam name
    via the MLB Stats API and pick the OTHER side.
    """
    home_full = (prop.get("home_team") or "").strip()
    away_full = (prop.get("away_team") or "").strip()
    if not home_full or not away_full:
        return None
    pid = search_player_by_name(player_name)
    if not pid:
        return None
    info = get_player_info(pid)
    team_name = (info.get("team_name") or "").strip().lower()
    if not team_name:
        return None
    if team_name == home_full.lower():
        return team_name_to_abbrev(away_full)
    if team_name == away_full.lower():
        return team_name_to_abbrev(home_full)
    return None


# ---------------------------------------------------------------------------
# Opposing-team rank vs each prop market
# ---------------------------------------------------------------------------
#
# To answer "facing a team that's 28th in opposing K's allowed" we pull
# the league-wide team season stats once per day and rank.  For pitcher
# markets we rank teams by HOW MUCH OF THE STAT THEY GENERATE FACING
# A PITCHER (hitting-side stats); for batter markets we rank by HOW
# MUCH OF THE STAT THE TEAM ALLOWS (pitching-side stats).
#
# Rank 1 = MOST favorable for the OVER bettor (highest stat output /
# allowed).  Rank 30 = least favorable.

# Per-market source: ("hitting"|"pitching", stat field on the splits[].stat object)
_MARKET_TO_TEAM_STAT: dict[str, tuple[str, str]] = {
    "pitcher_strikeouts":   ("hitting",  "strikeOuts"),
    "pitcher_outs":         ("hitting",  "atBats"),       # more ABs faced = more outs in play
    "pitcher_hits_allowed": ("hitting",  "hits"),
    "pitcher_walks":        ("hitting",  "baseOnBalls"),
    "pitcher_earned_runs":  ("hitting",  "runs"),
    "batter_hits":          ("pitching", "hits"),
    "batter_total_bases":   ("pitching", "totalBases"),
    "batter_home_runs":     ("pitching", "homeRuns"),
    "batter_rbis":          ("pitching", "earnedRuns"),
    "batter_runs_scored":   ("pitching", "runs"),
    "batter_walks":         ("pitching", "baseOnBalls"),
    "batter_strikeouts":    ("pitching", "strikeOuts"),
}

_TEAM_RANKS_CACHE_KEY = "team_prop_ranks_{season}"


def _fetch_team_stats(group: str, season: int) -> list[dict]:
    """Fetch the league-wide team aggregate for *group* in *season*.

    Returns a list of {"abbrev": str, "stat": dict} entries, one per
    team.  Empty list on any failure.
    """
    url = (
        f"{_STATS_BASE}/teams/stats"
        f"?stats=season&group={group}&season={season}&sportIds=1"
    )
    data = _fetch_json(url, label=f"_fetch_team_stats({group}, {season})")
    if not data:
        return []
    try:
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        out: list[dict] = []
        for sp in splits:
            team = sp.get("team") or {}
            abbrev = team.get("abbreviation") or team.get("triCode") or ""
            if not abbrev:
                # Some endpoint variants drop abbreviation -- skip silently;
                # the rank table just won't include that team.
                continue
            stat = sp.get("stat") or {}
            out.append({"abbrev": abbrev.upper(), "stat": stat})
        return out
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"_fetch_team_stats({group}, {season}) parse error: {exc}")
        return []


def _compute_ranks_for_market(
    market: str,
    team_hitting: list[dict],
    team_pitching: list[dict],
) -> dict[str, int]:
    """Return {abbrev: rank} for *market*.  Rank 1 = most favorable for
    the OVER bettor (highest stat output for the opposing-team side).
    """
    spec = _MARKET_TO_TEAM_STAT.get(market)
    if spec is None:
        return {}
    group, stat_field = spec
    source = team_hitting if group == "hitting" else team_pitching
    rows: list[tuple[str, float]] = []
    for r in source:
        try:
            val = float(r["stat"].get(stat_field) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        rows.append((r["abbrev"], val))
    # Descending: best opportunity first (most K's batters strike out,
    # most hits pitchers allow, etc.)
    rows.sort(key=lambda kv: -kv[1])
    return {abbrev: i + 1 for i, (abbrev, _) in enumerate(rows)}


def get_team_prop_ranks(season: Optional[int] = None) -> dict[str, dict[str, int]]:
    """Return {abbrev: {market: rank}} for every team + supported market.

    Cached for 24h in Supabase + local file under
    ``team_prop_ranks_{season}``.  On any failure the previous day's
    snapshot is reused.
    """
    season = season or _CURRENT_SEASON
    cache_key = _TEAM_RANKS_CACHE_KEY.format(season=season)
    local_path = _CACHE_DIR / f"{cache_key}.json"
    today = _today_str()

    # 1. Local cache (today)
    try:
        if local_path.exists():
            payload = json.loads(local_path.read_text(encoding="utf-8"))
            if payload.get("date") == today and isinstance(payload.get("ranks"), dict):
                return payload["ranks"]
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"get_team_prop_ranks local cache read failed: {exc}")

    # 2. Supabase cache (today)
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today:
            ranks = (row.get("data") or {}).get("ranks") or {}
            if ranks:
                try:
                    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    local_path.write_text(
                        json.dumps({"date": today, "ranks": ranks}),
                        encoding="utf-8",
                    )
                except Exception:                                             # noqa: BLE001
                    pass
                return ranks
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"get_team_prop_ranks supabase read failed: {exc}")

    # 3. Live fetch
    team_hitting  = _fetch_team_stats("hitting",  season)
    team_pitching = _fetch_team_stats("pitching", season)
    if not team_hitting and not team_pitching:
        # 4. Stale local fallback
        try:
            if local_path.exists():
                payload = json.loads(local_path.read_text(encoding="utf-8"))
                if isinstance(payload.get("ranks"), dict):
                    return payload["ranks"]
        except Exception:                                                     # noqa: BLE001
            pass
        return {}

    ranks: dict[str, dict[str, int]] = {}
    for market in _MARKET_TO_TEAM_STAT:
        per_market = _compute_ranks_for_market(market, team_hitting, team_pitching)
        for abbrev, rank in per_market.items():
            ranks.setdefault(abbrev, {})[market] = rank

    # Persist
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        local_path.write_text(
            json.dumps({"date": today, "ranks": ranks}),
            encoding="utf-8",
        )
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"get_team_prop_ranks local write failed: {exc}")
    try:
        _db.cache_set(cache_key, "mlb", today, {"ranks": ranks})
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"get_team_prop_ranks supabase write failed: {exc}")

    _log(f"get_team_prop_ranks computed for {len(ranks)} teams, season={season}")
    return ranks


def get_opp_rank_for_prop(opp_abbrev: Optional[str], market: str) -> Optional[int]:
    """Convenience: rank of *opp_abbrev* for *market*, or None.  Returns
    a value 1..N where 1 = most favorable matchup for the OVER bettor."""
    if not opp_abbrev:
        return None
    table = get_team_prop_ranks()
    return (table.get(opp_abbrev.upper()) or {}).get(market)


def opp_rank_label(rank: Optional[int], total: int = 30) -> str:
    """Format an integer rank as '28th of 30'.  Returns '—' when None."""
    if rank is None:
        return "—"
    suffix = "th"
    if rank % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(rank % 10, "th")
    return f"{rank}{suffix} of {total}"
