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
import re
import sys
import time
import unicodedata
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
_name_to_id_cache:  dict[str, Optional[int]] = {}   # lowercased name -> player_id (None = negative cache)


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

def _strip_accents(s: str) -> str:
    """'Ureña' -> 'Urena'.  NFKD decompose, drop combining marks."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _norm_name(s: str) -> str:
    """Accent/punctuation-insensitive form for comparison: lowercase,
    strip accents, drop everything but letters/digits/space, collapse
    spaces.  'J.T. Ginn' -> 'jt ginn', 'Walbert Ureña' -> 'walbert urena'."""
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _name_query_variants(name: str) -> list[str]:
    """Distinct query strings to try against the MLB search API before
    giving up: the original, an accent-stripped form ('Ureña'->'Urena'),
    a period-stripped form ('J.T.'->'JT'), and a dotted-initials form
    ('JT Ginn'->'J.T. Ginn')."""
    out: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    add(name)
    add(_strip_accents(name))                      # accent fallback
    add(name.replace(".", ""))                     # "J.T. Ginn" -> "JT Ginn"
    add(_strip_accents(name).replace(".", ""))

    # Dotted-initials fallback: a 2-3 letter all-caps first token like
    # "JT" -> "J.T." so "JT Ginn" also matches the API's "J.T. Ginn".
    parts = name.split()
    if parts and parts[0].isalpha() and parts[0].isupper() and 2 <= len(parts[0]) <= 3:
        dotted = ".".join(parts[0]) + "."
        add(" ".join([dotted, *parts[1:]]))
        add(_strip_accents(" ".join([dotted, *parts[1:]])))
    return out


def search_player_by_name(name: str) -> Optional[int]:
    """Search the MLB Stats API for *name* and return the player ID or None.

    Caches both hits AND misses in ``_name_to_id_cache`` (negative cache):
    a name that fails once is never retried for the rest of the process /
    scoring run, so an unresolvable name no longer fires dozens of repeated
    API calls (one per prop).  Failures log exactly once per name.

    Before giving up it tries several normalized query variants (accent-
    and period-insensitive) so e.g. "JT Ginn" matches the API's
    "J.T. Ginn" and "Walbert Urena" matches "Walbert Ureña"."""
    key = name.lower()
    if key in _name_to_id_cache:
        return _name_to_id_cache[key]            # hit OR negative-cache hit

    target_norm = _norm_name(name)
    fallback_pid: Optional[int] = None           # first result seen, last resort
    for variant in _name_query_variants(name):
        encoded = urllib.parse.quote(variant)
        url = (
            f"{_STATS_BASE}/people/search"
            f"?names={encoded}&season={_CURRENT_SEASON}&sportId=1"
        )
        data = _fetch_json(url, label=f"search_player_by_name({variant!r})")
        if not data:
            continue
        try:
            people = data.get("people") or []
        except Exception:                                                 # noqa: BLE001
            people = []
        # Accent/punctuation-insensitive substring match first.
        for person in people:
            full_norm = _norm_name(person.get("fullName") or "")
            if full_norm and (target_norm in full_norm or full_norm in target_norm):
                pid = int(person["id"])
                _name_to_id_cache[key] = pid
                return pid
        if fallback_pid is None and people:
            try:
                fallback_pid = int(people[0]["id"])
            except (KeyError, ValueError, TypeError):
                fallback_pid = None

    # No normalized match across any variant.  Fall back to the first
    # result the API returned (legacy behaviour) if there was one; else
    # negative-cache so this name isn't retried this run.  Either way the
    # outcome is cached and we log at most once per name.
    if fallback_pid is not None:
        _name_to_id_cache[key] = fallback_pid
        return fallback_pid
    _name_to_id_cache[key] = None
    _log(f"search_player_by_name({name!r}) -- no MLB match (negative-cached)")
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

def get_season_stats(player_id: int, *, is_pitcher: bool,
                     season: Optional[int] = None) -> dict:
    """Return season aggregate stats for *player_id* (defaults to the
    module's current season)."""
    group = "pitching" if is_pitcher else "hitting"
    url   = (
        f"{_STATS_BASE}/people/{player_id}/stats"
        f"?stats=season&season={season or _CURRENT_SEASON}&group={group}"
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
    force_refresh: bool = False,
) -> list[dict]:
    """Return per-game log for *player_id* for *season*, cached aggressively.

    Pitchers: up to 30 most-recent games.
    Batters:  up to 50 most-recent games.

    The caches are keyed by UTC calendar day, so a gamelog fetched in the
    morning (before that day's game) is reused all day -- which means a
    settlement pass would read a stale log that doesn't yet contain the
    just-completed game.  *force_refresh* skips both cache reads and pulls
    live from statsapi so settlement always sees the finished game; the
    fresh result is still written back to both caches.
    """
    limit = 30 if is_pitcher else 50
    cache_key = f"player_gamelog_{player_id}_{season}"

    if not force_refresh:
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
                # The gameLog `opponent` object usually carries only
                # id/name/link (no abbreviation), so fall back to mapping
                # the full team name -> abbrev (fixes the OPP column showing
                # dashes for every row).
                opp_abbrev  = (opp_info.get("abbreviation")
                               or team_name_to_abbrev(opp_info.get("name", ""))
                               or "")
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

    PURE CACHE READER.  This function MUST NOT call ``predict()`` -- the
    Bubba Chandler / Michael Harris II side-flip bug was caused by the
    player page independently re-scoring a prop and getting a different
    side than the props page (different feature snapshot per worker
    process).  The scored cache populated by the scheduler is the
    single source of truth; both pages now read from it.

    Resolution order
    ----------------
    1. ``src.props_scored_cache.load_scored_props()`` -- every primary
       pick the scheduler has scored above the 55% confidence + reg-edge
       threshold.  This is what the props page reads, so the side here
       is guaranteed to match.
    2. ``data/daily_picks.json`` prop_picks -- top-5 by confidence from
       the most recent /api/analyze run.  Used as a fallback for the
       window between Railway deploys (scored cache empty) and the
       first scheduler tick.
    3. ``None`` -- no cached pick for the player today; the hero card
       suppresses the TODAY chip entirely rather than fabricating one.
    """
    name_norm = _norm_name(player_name)

    # ── 1. Scored cache (single source of truth, matches /props page) ─────
    try:
        from .props_scored_cache import load_scored_props
        cached = load_scored_props() or {}
        best: Optional[dict] = None
        for pick in (cached.get("picks") or []):
            if _norm_name(pick.get("player") or "") != name_norm:
                continue
            score = float(pick.get("confidence") or 0.0)
            if best is None or score > float(best.get("confidence") or 0.0):
                best = _scored_cache_to_entry(pick)
        if best is not None:
            # Verbose log so a future side-flip report can be traced to
            # the exact cache row.  Shows every field that could disagree
            # between /props and /player rendering: side (what /props
            # displays), recommendation (what /player used to display
            # before the predict() fix), confidence + ev_pct + line_type.
            _log(
                f"get_today_prop({player_name!r}) scored_cache READ: "
                f"market={best.get('market')} "
                f"side={best.get('side')!r} "
                f"line={best.get('line')} "
                f"recommendation={best.get('recommendation')!r} "
                f"conf={best.get('confidence')} "
                f"ev_pct={best.get('ev_pct')} "
                f"line_type={best.get('line_type')!r}"
            )
            return best
    except Exception as exc:
        _log(f"get_today_prop({player_name!r}): scored_cache read failed: {exc}")

    # ── 2. daily_picks fallback (covers post-deploy gap before next tick) ─
    try:
        from .daily_picks import load_daily_picks
        daily     = load_daily_picks()
        prop_picks = (daily.get("picks") or {}).get("prop_picks") or []
        for pp in prop_picks:
            if _norm_name(pp.get("player") or "") == name_norm:
                _log(
                    f"get_today_prop({player_name!r}): served from daily_picks "
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
                    "ev_pct":          _calc_ev(
                        pp.get("confidence"), pp.get("best_odds"),
                    ),
                }
    except Exception as exc:
        _log(f"get_today_prop({player_name!r}): daily_picks read failed: {exc}")

    # ── 3. No cached pick for this player -- bail without fabricating ─────
    return None


def _scored_cache_to_entry(pick: dict) -> dict:
    """Project a scored_cache row into the dict shape the player page
    consumes.  Same fields the props page reads, plus the
    ``source="scored_cache"`` marker so log lines can attribute where
    the pick came from.

    Carries through the matchup/time fields (home_team, away_team,
    team, event_id, commence_time) so the player-page info row can
    show OPP + GAME TIME without a fresh schedule fetch -- they were
    being dropped here, which is why those fields rendered as dashes.
    """
    return {
        "market":          pick.get("market"),
        "line":            pick.get("line"),
        "side":            pick.get("side"),
        "best_odds":       pick.get("best_odds"),
        "best_book":       pick.get("best_book"),
        "recommendation":  pick.get("recommendation"),
        "confidence":      pick.get("confidence"),
        "predicted_value": pick.get("predicted_value"),
        "edge":            pick.get("edge"),
        "model_prob":      pick.get("model_prob"),
        "ev_pct":          pick.get("ev_pct"),
        "line_type":       pick.get("line_type", "main"),
        "is_primary":      bool(pick.get("is_primary", True)),
        # Matchup + schedule context (used by the player-page info row).
        "home_team":       pick.get("home_team"),
        "away_team":       pick.get("away_team"),
        "team":            pick.get("team"),
        "event_id":        pick.get("event_id"),
        "commence_time":   pick.get("commence_time"),
        "source":          "scored_cache",
    }


def _calc_ev(confidence, american_odds):
    """Thin wrapper around props_ev.calc_ev_pct that never raises --
    EV is best-effort metadata for the UI, not load-bearing."""
    try:
        from .props_ev import calc_ev_pct
        return calc_ev_pct(confidence, american_odds)
    except Exception:                                                     # noqa: BLE001
        return None


def get_today_props_for_player(player_name: str) -> list[dict]:
    """Return EVERY prop prediction for *player_name* today.

    PURE CACHE READER -- never calls ``predict()`` (see the docstring
    on ``get_today_prop`` for why; same Bubba Chandler / Michael
    Harris II side-flip story).  Reads from the same scored cache
    the /props page reads so the side, confidence and EV displayed
    here are guaranteed to match.

    Resolution order
    ----------------
    1. ``src.props_scored_cache.load_scored_props()`` -- every primary
       pick above threshold for the player.  One entry per
       ``(player, market)`` since the scheduler already dedupes.
    2. ``data/daily_picks.json`` -- fills markets the scored cache
       doesn't cover (rare; mostly the window after a Railway redeploy
       before the first scheduler tick).
    3. ``[]`` -- no cached pick for this player today; the page falls
       back to its "no props posted yet" empty state instead of
       fabricating one with an independent predict() call.
    """
    name_norm = _norm_name(player_name)
    today = _today_str()
    out: list[dict] = []
    markets_seen: set[str] = set()

    # ── 1. Scored cache (canonical) ───────────────────────────────────────
    try:
        from .props_scored_cache import load_scored_props
        cached = load_scored_props() or {}
        for pick in (cached.get("picks") or []):
            if _norm_name(pick.get("player") or "") != name_norm:
                continue
            commence = (pick.get("commence_time") or "")[:10]
            if commence and commence != today:
                continue
            entry = _scored_cache_to_entry(pick)
            out.append(entry)
            markets_seen.add(entry.get("market") or "")
            # Per-pick debug log so a future side-flip report can be
            # diagnosed by tailing Railway logs as the player page
            # loads.  Logs every field that the /player UI consumes,
            # alongside the canonical ``side`` so disagreements with
            # /props are obvious at first glance.
            _log(
                f"get_today_props_for_player({player_name!r}) "
                f"scored_cache READ: "
                f"market={entry.get('market')} "
                f"side={entry.get('side')!r} "
                f"line={entry.get('line')} "
                f"recommendation={entry.get('recommendation')!r} "
                f"conf={entry.get('confidence')} "
                f"ev_pct={entry.get('ev_pct')} "
                f"line_type={entry.get('line_type')!r}"
            )
    except Exception as exc:
        _log(
            f"get_today_props_for_player({player_name!r}): "
            f"scored_cache read failed: {exc}"
        )

    # ── 2. daily_picks fallback for any market the scored cache missed ───
    try:
        from .daily_picks import load_daily_picks
        daily      = load_daily_picks()
        prop_picks = (daily.get("picks") or {}).get("prop_picks") or []
        for pp in prop_picks:
            if _norm_name(pp.get("player") or "") != name_norm:
                continue
            market_name = pp.get("market") or ""
            if market_name in markets_seen:
                continue
            entry = {
                "market":          market_name,
                "line":            pp.get("line"),
                "side":            (pp.get("side") or "Over").strip().title(),
                "best_odds":       pp.get("best_odds"),
                "recommendation":  pp.get("recommendation"),
                "confidence":      pp.get("confidence"),
                "predicted_value": pp.get("predicted_value"),
                "source":          "daily_picks",
                # daily_picks rows are pre-classifier; assume main since
                # they survived top-5 dedup.  If the line was actually
                # alt, the next scored_cache write overrides.
                "line_type":       "main",
                "is_primary":      True,
                "ev_pct":          _calc_ev(
                    pp.get("confidence"), pp.get("best_odds"),
                ),
            }
            out.append(entry)
            markets_seen.add(market_name)
    except Exception as exc:
        _log(
            f"get_today_props_for_player({player_name!r}): "
            f"daily_picks read failed: {exc}"
        )

    # Sort by confidence DESC so the strongest pick lands first.
    out.sort(key=lambda e: -float(e.get("confidence") or 0.0))
    _log(
        f"get_today_props_for_player({player_name!r}): "
        f"{len(out)} prop(s) "
        f"[query_norm={name_norm!r} "
        f"markets={[e.get('market') for e in out]} "
        f"sources={[e.get('source') for e in out]}]"
    )
    return out


def get_today_raw_lines_for_player(player_name: str) -> dict:
    """Return EVERY raw book line for *player_name* today, keyed by market.

    Unlike get_today_props_for_player (which reads the SCORED cache and so
    only surfaces picks that cleared the confidence + edge threshold), this
    reads the RAW props cache that props_client populates straight from The
    Odds API.  It backs the player page's "ALL PROP MARKETS" skeleton, which
    must show a line whenever any book posted one -- even for markets the
    model scored poorly or skipped entirely.

    Returns ``{market_key: {line, side, best_odds, best_book, n_books}}`` with
    one representative (main-line) entry per market: the Over side carried by
    the most books (tie-break: most favourable odds), falling back to Under.
    Accent/punctuation-insensitive name match (same _norm_name the resolver
    uses).  Empty dict when nothing is cached for the player today.

    PURE CACHE READER -- never calls The Odds API; reads the same local /
    Supabase props cache the /props page and the 15-min cycle share.
    """
    name_norm = _norm_name(player_name)
    today = _today_str()
    try:
        from .props_client import get_client
        raw = get_client().get_today_props() or {}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_today_raw_lines_for_player({player_name!r}): "
             f"raw cache read failed: {exc}")
        return {}

    def _rank(e: dict) -> tuple:
        return (
            1 if (e.get("side") or "").title() == "Over" else 0,
            len(e.get("all_books") or []),
            e.get("best_odds") if isinstance(e.get("best_odds"), int) else -10 ** 9,
        )

    out: dict[str, dict] = {}
    for market_key, entries in (raw.get("markets") or {}).items():
        cands = [
            e for e in (entries or [])
            if _norm_name(e.get("player_name") or "") == name_norm
            and e.get("line") is not None
            and (not (e.get("commence_time") or "")
                 or (e.get("commence_time") or "")[:10] == today)
        ]
        if not cands:
            continue
        best = max(cands, key=_rank)
        out[market_key] = {
            "line":      best.get("line"),
            "side":      (best.get("side") or "Over").title(),
            "best_odds": best.get("best_odds"),
            "best_book": best.get("best_book"),
            "n_books":   len(best.get("all_books") or []),
        }
    _log(
        f"get_today_raw_lines_for_player({player_name!r}): "
        f"{len(out)} market(s) [query_norm={name_norm!r} markets={sorted(out)}]"
    )
    return out


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


# Reverse map (abbrev -> canonical full name) for UI labels like the OPP box.
_ABBREV_TO_TEAM_NAME: dict[str, str] = {}
for _full, _ab in _TEAM_NAME_TO_ABBREV.items():
    _ABBREV_TO_TEAM_NAME.setdefault(_ab, _full.title())
# Canonical display overrides where .title() isn't quite right.
_ABBREV_TO_TEAM_NAME.update({
    "STL": "St. Louis Cardinals", "CWS": "Chicago White Sox",
    "ATH": "Athletics", "OAK": "Oakland Athletics",
})


def team_abbrev_to_name(abbrev: str) -> Optional[str]:
    """Full team name for a 3-letter abbreviation, or None if unknown."""
    if not abbrev:
        return None
    return _ABBREV_TO_TEAM_NAME.get(abbrev.strip().upper())


# MLB Stats API team ids (stable constants) -- used to pull a team's recent
# schedule for the "recent pitchers vs team" list.
_ABBREV_TO_TEAM_ID: dict[str, int] = {
    "LAA": 108, "ARI": 109, "BAL": 110, "BOS": 111, "CHC": 112, "CIN": 113,
    "CLE": 114, "COL": 115, "DET": 116, "HOU": 117, "KC": 118, "LAD": 119,
    "WSH": 120, "NYM": 121, "OAK": 133, "ATH": 133, "PIT": 134, "SD": 135,
    "SEA": 136, "SF": 137, "STL": 138, "TB": 139, "TEX": 140, "TOR": 141,
    "MIN": 142, "PHI": 143, "ATL": 144, "CWS": 145, "MIA": 146, "NYY": 147,
    "MIL": 158,
}


def get_recent_pitchers_vs_team(opp_abbrev: str, *, limit: int = 5,
                                days_back: int = 60) -> list[dict]:
    """The most-recent OPPOSING starting pitchers a team faced.

    Pulls the team's recent completed games from the MLB Stats schedule
    (same statsapi the gamelog uses; hydrate=probablePitcher), and for each
    returns the OTHER side's starter -- i.e. a pitcher who pitched against
    *opp_abbrev*.  Newest first, up to *limit*.  Cached daily in Supabase.

    Returns [{player_id, name, date, is_home_pitcher}].  Empty list when the
    team is unknown or the schedule fetch fails -- the caller shows a clear
    'not available' note rather than a blank box.
    """
    ab = (opp_abbrev or "").strip().upper()
    tid = _ABBREV_TO_TEAM_ID.get(ab)
    if not tid:
        return []
    cache_key = f"recent_opp_pitchers_{ab}_{_today_str()}"
    try:
        row = _db.cache_get(cache_key)
        if isinstance(row, dict) and row.get("date") == _today_str():
            data = (row.get("data") or {}).get("pitchers")
            if isinstance(data, list):
                return data[:limit]
    except Exception:                                                     # noqa: BLE001
        pass

    from datetime import date as _date, timedelta as _td
    end = _date.today()
    start = end - _td(days=days_back)
    url = (f"{_STATS_BASE}/schedule?sportId=1&teamId={tid}"
           f"&startDate={start.isoformat()}&endDate={end.isoformat()}"
           f"&hydrate=probablePitcher")
    data = _fetch_json(url, label=f"recent_opp_pitchers({ab})")
    if not data:
        return []

    out: list[dict] = []
    for d in (data.get("dates") or []):
        for g in (d.get("games") or []):
            state = (g.get("status") or {}).get("abstractGameState")
            if state != "Final":
                continue
            teams = g.get("teams") or {}
            for side in ("home", "away"):
                blk = teams.get(side) or {}
                if (blk.get("team") or {}).get("id") != tid:
                    continue
                other = teams.get("away" if side == "home" else "home") or {}
                pp = other.get("probablePitcher") or {}
                pid, name = pp.get("id"), pp.get("fullName")
                if pid and name:
                    out.append({
                        "player_id":      int(pid),
                        "name":           name,
                        "date":           (g.get("gameDate") or d.get("date") or "")[:10],
                        # the opposing starter is on the non-opp side; he was
                        # home iff opp was the away team (side == "away").
                        "is_home_pitcher": side == "away",
                    })
                break
    out.sort(key=lambda x: x["date"], reverse=True)
    out = out[:max(limit, 10)]   # cache a few extra; caller slices to limit
    try:
        _db.cache_set(cache_key, "mlb", _today_str(), {"pitchers": out})
    except Exception:                                                     # noqa: BLE001
        pass
    return out[:limit]



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
# Matchup data: opposing lineup (for pitchers) + batter-vs-pitcher H2H
# ---------------------------------------------------------------------------
#
# Both fetchers degrade gracefully: any network/parse failure or
# not-yet-posted lineup returns an "available: False" payload + a
# human note, never raises.  Assembled results are cached for the day
# (local file + Supabase) so the per-batter stat fan-out only happens
# once per game.

def _find_todays_game(prop: dict, player_name: str) -> Optional[dict]:
    """Resolve the player's game today from the schedule (lineups +
    probable pitchers hydrated) and return a normalised dict, or None.

    Shape::
        {
          "game_pk":     int,
          "date":        "YYYY-MM-DD",
          "player_side": "home" | "away",
          "opp_side":    "home" | "away",
          "lineups":     {"home": [player...], "away": [player...]},
          "pitchers":    {"home": {...}|None, "away": {...}|None},
        }

    Each lineup *player* is ``{id, name, position, hand, order}``; each
    pitcher is ``{id, name, hand}``.
    """
    commence = (prop.get("commence_time") or "").strip()
    date_str = commence[:10] if commence else _today_str()
    home_full = (prop.get("home_team") or "").strip()
    away_full = (prop.get("away_team") or "").strip()
    if not home_full or not away_full:
        return None

    # Which side is the player on?
    pid = search_player_by_name(player_name)
    if not pid:
        return None
    info = get_player_info(pid)
    team_name = (info.get("team_name") or "").strip().lower()
    if team_name == home_full.lower():
        player_side, opp_side = "home", "away"
    elif team_name == away_full.lower():
        player_side, opp_side = "away", "home"
    else:
        return None

    url = (
        f"{_STATS_BASE}/schedule?sportId=1&date={date_str}"
        f"&hydrate=lineups,probablePitcher(note,pitchHand)"
    )
    data = _fetch_json(url, label=f"_find_todays_game({date_str})")
    if not data:
        return None

    def _parse_lineup(players: list) -> list[dict]:
        out: list[dict] = []
        for i, p in enumerate(players or []):
            out.append({
                "id":       p.get("id"),
                "name":     p.get("fullName") or "",
                "position": (p.get("primaryPosition") or {}).get("abbreviation") or "",
                "hand":     (p.get("batSide") or {}).get("code") or "",
                "order":    i + 1,
            })
        return out

    def _parse_pitcher(side_block: dict) -> Optional[dict]:
        pp = side_block.get("probablePitcher") or {}
        if not pp.get("id"):
            return None
        return {
            "id":   pp.get("id"),
            "name": pp.get("fullName") or "",
            "hand": (pp.get("pitchHand") or {}).get("code") or "",
        }

    try:
        for day in data.get("dates") or []:
            for game in day.get("games") or []:
                teams = game.get("teams") or {}
                g_home = ((teams.get("home") or {}).get("team") or {}).get("name", "")
                g_away = ((teams.get("away") or {}).get("team") or {}).get("name", "")
                if g_home.lower() != home_full.lower() or g_away.lower() != away_full.lower():
                    continue
                lineups = game.get("lineups") or {}
                return {
                    "game_pk":     game.get("gamePk"),
                    "date":        date_str,
                    "player_side": player_side,
                    "opp_side":    opp_side,
                    "lineups": {
                        "home": _parse_lineup(lineups.get("homePlayers")),
                        "away": _parse_lineup(lineups.get("awayPlayers")),
                    },
                    "pitchers": {
                        "home": _parse_pitcher(teams.get("home") or {}),
                        "away": _parse_pitcher(teams.get("away") or {}),
                    },
                }
    except Exception as exc:                                              # noqa: BLE001
        _log(f"_find_todays_game parse error: {exc}")
    return None


# Per-market lineup stat: (column label, season_stats key, formatter).
# The formatter turns the raw season_stats dict into a display string.
def _lineup_stat_spec(market: str):
    def _rate(num_key: str):
        def fmt(s: dict) -> str:
            pa = float(s.get("pa") or 0)
            n  = float(s.get(num_key) or 0)
            return f"{(n / pa * 100):.0f}%" if pa else "—"
        return fmt

    def _contact(s: dict) -> str:
        pa = float(s.get("pa") or 0)
        k  = float(s.get("strikeouts") or 0)
        return f"{((pa - k) / pa * 100):.0f}%" if pa else "—"

    def _avg3(key: str):
        def fmt(s: dict) -> str:
            v = s.get(key)
            return f"{float(v):.3f}".lstrip("0") if v else "—"
        return fmt

    if market == "pitcher_strikeouts":
        return ("K%", _rate("strikeouts"))
    if market == "pitcher_outs":
        return ("Contact%", _contact)
    if market == "pitcher_walks":
        return ("BB%", _rate("walks"))
    if market == "pitcher_earned_runs":
        return ("OPS", _avg3("ops"))
    # pitcher_hits_allowed + default
    return ("AVG", _avg3("avg"))


def get_opposing_lineup(prop: dict, player_name: str, market: str) -> dict:
    """Return the opposing team's batting order with the stat relevant
    to *market* for a pitcher's matchup view.

    Returns::
        {"available": bool, "note": str, "stat_label": str,
         "batters": [{name, position, hand, stat}]}

    Cached per (game_pk, market) for the ET day -- the per-batter
    season-stat fan-out (up to 9 calls) runs once, then reads are a
    single cache hit.
    """
    stat_label, stat_fmt = _lineup_stat_spec(market)
    empty = {"available": False, "note": "Lineup not posted yet.",
             "stat_label": stat_label, "batters": []}

    game = _find_todays_game(prop, player_name)
    if not game or not game.get("game_pk"):
        return dict(empty, note="Game not found on today's schedule.")

    opp_lineup = (game.get("lineups") or {}).get(game["opp_side"]) or []
    if not opp_lineup:
        return empty   # lineup not posted yet

    cache_key = f"lineup_{game['game_pk']}_{game['opp_side']}_{market}"
    today = _today_str()
    # Local + Supabase day cache
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today:
            cached = (row.get("data") or {})
            if cached.get("batters"):
                return cached
    except Exception:                                                     # noqa: BLE001
        pass

    batters: list[dict] = []
    for p in opp_lineup[:9]:
        bid = p.get("id")
        stat_str = "—"
        if bid:
            try:
                s = get_season_stats(int(bid), is_pitcher=False)
                stat_str = stat_fmt(s)
            except Exception:                                             # noqa: BLE001
                stat_str = "—"
        batters.append({
            "name":     p.get("name") or "—",
            "position": p.get("position") or "—",
            "hand":     p.get("hand") or "—",
            "stat":     stat_str,
        })

    result = {"available": True, "note": "", "stat_label": stat_label,
              "batters": batters}
    try:
        _db.cache_set(cache_key, "mlb", today, result)
    except Exception:                                                     # noqa: BLE001
        pass
    return result


def get_batter_vs_pitcher(prop: dict, player_name: str) -> dict:
    """Career head-to-head aggregate of the batter vs today's opposing
    starting pitcher (MLB Stats API ``vsPlayer`` stat type).

    Returns::
        {"available": bool, "note": str, "pitcher_name": str,
         "pitcher_hand": str, "ab": int, "h": int, "avg": str,
         "obp": str, "slg": str, "ops": str, "hr": int, "so": int,
         "bb": int, "games": int}

    Cached per (batter_id, pitcher_id) for the day.  Per-game H2H
    splits aren't exposed by a clean MLB endpoint, so this surfaces
    the career aggregate -- which is exactly the "Limited H2H data"
    fallback content (career AB / AVG / OPS) the UI shows.
    """
    empty = {"available": False, "note": "No opposing starter announced yet.",
             "pitcher_name": "", "pitcher_hand": ""}

    game = _find_todays_game(prop, player_name)
    if not game:
        return dict(empty, note="Game not found on today's schedule.")
    pitcher = (game.get("pitchers") or {}).get(game["opp_side"])
    if not pitcher or not pitcher.get("id"):
        return empty

    batter_id = search_player_by_name(player_name)
    if not batter_id:
        return dict(empty, note="Could not resolve batter.",
                    pitcher_name=pitcher.get("name", ""),
                    pitcher_hand=pitcher.get("hand", ""))

    cache_key = f"bvp_{batter_id}_{pitcher['id']}"
    today = _today_str()
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today and (row.get("data") or {}).get("available") is not None:
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    url = (
        f"{_STATS_BASE}/people/{batter_id}/stats"
        f"?stats=vsPlayer&opposingPlayerId={pitcher['id']}&group=hitting"
    )
    data = _fetch_json(url, label=f"get_batter_vs_pitcher({batter_id} vs {pitcher['id']})")
    out = {
        "available":    False,
        "note":         "No prior plate appearances vs this pitcher.",
        "pitcher_name": pitcher.get("name", ""),
        "pitcher_hand": pitcher.get("hand", ""),
        "ab": 0, "h": 0, "hr": 0, "so": 0, "bb": 0, "games": 0,
        "avg": "—", "obp": "—", "slg": "—", "ops": "—",
    }
    try:
        # vsPlayer returns career + per-season splits; the split with
        # the most plate appearances is the career total.
        splits = ((data or {}).get("stats") or [{}])[0].get("splits") or []
        best = None
        for sp in splits:
            st = sp.get("stat") or {}
            ab = int(st.get("atBats") or 0)
            if best is None or ab > best[0]:
                best = (ab, st)
        if best and best[0] > 0:
            st = best[1]
            def _fmt3(v):
                try:
                    return f"{float(v):.3f}".lstrip("0") or ".000"
                except (TypeError, ValueError):
                    return "—"
            out.update({
                "available": True,
                "note":      "",
                "ab":        int(st.get("atBats") or 0),
                "h":         int(st.get("hits") or 0),
                "hr":        int(st.get("homeRuns") or 0),
                "so":        int(st.get("strikeOuts") or 0),
                "bb":        int(st.get("baseOnBalls") or 0),
                "games":     int(st.get("gamesPlayed") or 0),
                "avg":       _fmt3(st.get("avg")),
                "obp":       _fmt3(st.get("obp")),
                "slg":       _fmt3(st.get("slg")),
                "ops":       _fmt3(st.get("ops")),
            })
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_batter_vs_pitcher parse error: {exc}")

    try:
        _db.cache_set(cache_key, "mlb", today, out)
    except Exception:                                                     # noqa: BLE001
        pass
    return out


def get_today_opposing_pitcher(prop: dict, player_name: str) -> Optional[dict]:
    """Resolve today's opposing *starting* pitcher for *player_name*.

    Returns ``{"id", "name", "hand"}`` (the probable starter on the
    other side of the matchup) or ``None`` when the game / probable
    pitcher isn't on today's schedule yet.  Wraps the private
    ``_find_todays_game`` so other modules don't reach into privates.
    """
    try:
        game = _find_todays_game(prop, player_name)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"get_today_opposing_pitcher error: {exc}")
        return None
    if not game:
        return None
    pitcher = (game.get("pitchers") or {}).get(game.get("opp_side") or "")
    if not pitcher or not pitcher.get("id"):
        return None
    return {"id": pitcher.get("id"), "name": pitcher.get("name", ""),
            "hand": pitcher.get("hand", "")}


def _batter_split_vs_hand(batter_id: int, sit_code: str,
                          season: Optional[int] = None) -> Optional[dict]:
    """Hitting split vs a pitcher handedness for *batter_id*.

    *sit_code* is ``"vr"`` (vs RHP) or ``"vl"`` (vs LHP).  Returns
    ``{pa, avg, woba, iso, k_pct}`` (floats) or None.  MLB Stats API
    doesn't expose wOBA, so it's estimated from the split's component
    stats with standard linear weights; ISO = SLG - AVG."""
    if not batter_id:
        return None
    season = season or _CURRENT_SEASON
    url = (
        f"{_STATS_BASE}/people/{batter_id}/stats"
        f"?stats=statSplits&group=hitting&sitCodes={sit_code}&season={season}"
    )
    data = _fetch_json(url, label=f"_batter_split_vs_hand({batter_id},{sit_code})")
    if not data:
        return None
    try:
        splits = (data.get("stats") or [{}])[0].get("splits") or []
        if not splits:
            return None
        st = splits[0].get("stat") or {}

        def _i(k):  # safe int
            try:
                return int(st.get(k) or 0)
            except (TypeError, ValueError):
                return 0

        def _f(k):  # safe float (MLB returns avg/slg as strings like ".287")
            v = st.get(k)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        pa  = _i("plateAppearances")
        ab  = _i("atBats")
        h   = _i("hits")
        d2  = _i("doubles")
        t3  = _i("triples")
        hr  = _i("homeRuns")
        bb  = _i("baseOnBalls")
        ibb = _i("intentionalWalks")
        hbp = _i("hitByPitch")
        sf  = _i("sacFlies")
        so  = _i("strikeOuts")

        avg = _f("avg")
        if avg is None:
            avg = (h / ab) if ab else 0.0
        slg = _f("slg")
        if slg is None:
            slg = ((h - d2 - t3 - hr) + 2 * d2 + 3 * t3 + 4 * hr) / ab if ab else 0.0
        iso = max(0.0, slg - avg)

        singles = max(0, h - d2 - t3 - hr)
        ubb = max(0, bb - ibb)
        woba_num = (0.69 * ubb + 0.72 * hbp + 0.89 * singles
                    + 1.27 * d2 + 1.62 * t3 + 2.10 * hr)
        woba_den = ab + (bb - ibb) + sf + hbp
        woba = (woba_num / woba_den) if woba_den else 0.0
        k_pct = (so / pa * 100.0) if pa else 0.0
        return {"pa": pa, "avg": avg, "woba": woba, "iso": iso, "k_pct": k_pct}
    except Exception as exc:                                              # noqa: BLE001
        _log(f"_batter_split_vs_hand({batter_id}) parse error: {exc}")
        return None


def get_opposing_lineup_basic(prop: dict, player_name: str,
                              pitcher_hand: str = "") -> dict:
    """Expected opposing batting order for a *pitcher's* matchup view.

    Each batter carries season AVG/OBP/SLG plus their split vs the
    pitcher's handedness (PA / AVG / wOBA / ISO / K%).  *pitcher_hand* is
    'L'/'R' (the viewing pitcher's throwing hand); the split uses the
    batters-vs-that-hand situational code.  Cached per game_pk + hand for
    the ET day.

    Shape::
        {"available", "note", "split_label",
         "batters": [{order, name, position, hand, avg, obp, slg,
                      split_pa, split_avg, split_woba, split_iso, split_k_pct}]}
    """
    empty = {"available": False, "note": "Lineup not posted yet.",
             "split_label": "", "batters": []}
    game = _find_todays_game(prop, player_name)
    if not game or not game.get("game_pk"):
        return dict(empty, note="Game not found on today's schedule.")

    opp_side = game.get("opp_side") or ""
    lineup = (game.get("lineups") or {}).get(opp_side) or []
    if not lineup:
        return dict(empty, note="Opposing lineup not posted yet.")

    ph = (pitcher_hand or "").upper()
    sit = "vl" if ph == "L" else "vr"          # batters vs LHP / vs RHP
    split_label = "vs LHP" if ph == "L" else "vs RHP"

    cache_key = f"lineup_basic_{game['game_pk']}_{opp_side}_{sit}"
    today = _today_str()
    try:
        row = _db.cache_get(cache_key)
        if row and row.get("date") == today and (row.get("data") or {}).get("available"):
            return row["data"]
    except Exception:                                                     # noqa: BLE001
        pass

    def _fmt3(v) -> str:
        try:
            return f"{float(v):.3f}".lstrip("0") or ".000"
        except (TypeError, ValueError):
            return "—"

    batters: list[dict] = []
    for b in lineup[:9]:
        bid = b.get("id")
        ss = get_season_stats(bid, is_pitcher=False) if bid else {}
        spl = _batter_split_vs_hand(bid, sit) if bid else None
        batters.append({
            "order":    b.get("order"),
            "name":     b.get("name") or "",
            "position": b.get("position") or "",
            "hand":     b.get("hand") or "",
            "avg":      _fmt3(ss.get("avg")),
            "obp":      _fmt3(ss.get("obp")),
            "slg":      _fmt3(ss.get("slg")),
            "split_pa":    (spl or {}).get("pa"),
            "split_avg":   (spl or {}).get("avg"),
            "split_woba":  (spl or {}).get("woba"),
            "split_iso":   (spl or {}).get("iso"),
            "split_k_pct": (spl or {}).get("k_pct"),
        })

    out = {"available": bool(batters), "note": "",
           "split_label": split_label, "batters": batters}
    try:
        _db.cache_set(cache_key, "mlb", today, out)
    except Exception:                                                     # noqa: BLE001
        pass
    return out


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


def _fetch_team_pitching_relief(season: int) -> list[dict]:
    """League-wide team RELIEF (bullpen) pitching aggregate for *season*,
    via the statSplits ``sitCodes=rp`` (relief-pitcher) situational split.

    Same ``[{"abbrev", "stat"}]`` shape as _fetch_team_stats.  Returns an
    empty list on failure or if the endpoint doesn't surface the relief
    split, so callers can fall back to the full-staff aggregate.
    """
    url = (
        f"{_STATS_BASE}/teams/stats"
        f"?stats=statSplits&group=pitching&sitCodes=rp"
        f"&season={season}&sportIds=1"
    )
    data = _fetch_json(url, label=f"_fetch_team_pitching_relief({season})")
    if not data:
        return []
    try:
        out: list[dict] = []
        for block in (data.get("stats") or []):
            for sp in (block.get("splits") or []):
                team = sp.get("team") or {}
                abbrev = team.get("abbreviation") or team.get("triCode") or ""
                if not abbrev:
                    continue
                out.append({"abbrev": abbrev.upper(), "stat": sp.get("stat") or {}})
        return out
    except Exception as exc:                                                  # noqa: BLE001
        _log(f"_fetch_team_pitching_relief({season}) parse error: {exc}")
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
