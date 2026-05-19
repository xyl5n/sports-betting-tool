"""
Odds client — primary source is The Odds API, fallback is SharpAPI.

The Odds API: https://the-odds-api.com/liveapi/guides/v4/
SharpAPI:     https://docs.sharpapi.io/en/api-reference/overview

The Odds API (ODDS_API_KEY) is always tried first.  If the call raises and
SHARPAPI_KEY is also set, SharpAPI is contacted as a fallback.  Both clients
return the same normalised per-game dict so the rest of the app never needs
to know which source produced the result.
"""
import json
import os
import sys
from collections import defaultdict
from typing import Optional

import requests

from .cache import Cache

BASE_URL = "https://api.the-odds-api.com/v4"


def _redact_url(url: str) -> str:
    """Strip apiKey from any URL before logging."""
    try:
        from .redact import redact
        return redact(url)
    except Exception:                                                     # noqa: BLE001
        # Manual fallback: replace anything after `apiKey=` up to `&` or end.
        import re
        return re.sub(r"(apiKey=)[^&]*", r"\1REDACTED", url)


def _log(msg: str) -> None:
    """Diagnostic log line.  Tagged so it's easy to grep in Railway logs."""
    print(f"[odds_client] {msg}", flush=True, file=sys.stderr)


def _american_to_prob(american: int) -> float:
    """Convert American moneyline to raw implied probability (0-1)."""
    if american > 0:
        return 100 / (american + 100)
    return abs(american) / (abs(american) + 100)


def _remove_vig(home_prob: float, away_prob: float) -> tuple[float, float]:
    """Strip bookmaker vig so probabilities sum to 1."""
    total = home_prob + away_prob
    return home_prob / total, away_prob / total


# ---------------------------------------------------------------------------
# Daily Supabase odds cache
# ---------------------------------------------------------------------------
# Backed by the `app_cache` table introduced in PR #31 (src/db.py).  Caches
# the final normalised list of games for one (sport_key, markets, regions)
# triple, keyed by today's ET date.  Empty results are NOT cached so a
# 2 AM call that returns 0 games doesn't poison the entire day -- the next
# run will retry and pick up the lines once books post them.
#
# Cache key shape:  odds_daily:<sport_key>:<markets>:<regions>
# Cache row:        {data: [games], date: YYYY-MM-DD, sport: sport_key}

def _today_et_date() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        from datetime import date
        return date.today().isoformat()


def _daily_cache_key(sport_key: str, markets: str, regions: str) -> str:
    return f"odds_daily:{sport_key}:{markets}:{regions}"


def _read_daily_odds_cache(sport_key: str, markets: str, regions: str) -> Optional[list[dict]]:
    """Return today's cached games for this sport, or None on miss / error.

    Bypasses entirely if Supabase isn't configured (src.db falls back to
    JSON mode and cache_get returns None).
    """
    try:
        from . import db
        row = db.cache_get(_daily_cache_key(sport_key, markets, regions))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"[daily-cache] read error (ignored): {type(exc).__name__}: {exc}")
        return None
    if not row:
        return None
    today = _today_et_date()
    row_date = row.get("date")
    if row_date != today:
        _log(f"[daily-cache] miss for {sport_key!r}: cached date={row_date!r} "
             f"!= today_et={today!r}")
        return None
    data = row.get("data")
    if not isinstance(data, list):
        _log(f"[daily-cache] miss for {sport_key!r}: data field is not a list "
             f"(type={type(data).__name__})")
        return None
    _log(f"[daily-cache] HIT for {sport_key!r}: {len(data)} games "
         f"(date={today}, NO API call needed)")
    return data


def _write_daily_odds_cache(
    sport_key: str, markets: str, regions: str, games: list[dict],
) -> None:
    """Write today's games to the Supabase daily cache.  No-op if Supabase
    isn't configured.  Errors are swallowed -- the caller already has the
    games in hand and a failed cache write should never block analysis."""
    if not games:
        _log(f"[daily-cache] skip write for {sport_key!r}: empty games list "
             f"(would poison today's cache; will retry on next run)")
        return
    try:
        from . import db
        ok = db.cache_set(
            _daily_cache_key(sport_key, markets, regions),
            sport_key,
            _today_et_date(),
            games,
        )
        if ok:
            _log(f"[daily-cache] wrote {len(games)} games for {sport_key!r} "
                 f"(date={_today_et_date()})")
        else:
            _log(f"[daily-cache] write returned False for {sport_key!r} "
                 f"(Supabase not configured or call failed)")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"[daily-cache] write error (ignored): {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# SharpAPI client
# ---------------------------------------------------------------------------

class SharpApiClient:
    """Fetches pre-match odds from SharpAPI (https://api.sharpapi.io/api/v1).

    Returns the same normalised per-game dict as OddsClient so the two sources
    are interchangeable from the caller's perspective.
    """

    _BASE_URL = "https://api.sharpapi.io/api/v1"

    # Map Odds API sport keys → SharpAPI league values.
    # SharpAPI uses UPPERCASE league identifiers ("MLB", "WNBA", "NBA", "NFL"
    # ...) rather than lowercase or namespaced strings like "basketball_wnba".
    # If a future SharpAPI update changes the convention, the boot probe in
    # app.py logs the /leagues response so the right value is one log line
    # away.
    SPORT_MAP: dict[str, str] = {
        "baseball_mlb":    "MLB",
        "basketball_wnba": "WNBA",
    }

    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._BASE_URL}{path}"
        _log(f"[sharp] GET {url} params={params}  auth=X-API-Key header")
        resp = self.session.get(url, params=params, timeout=15)
        _log(f"[sharp]   -> status={resp.status_code}  bytes={len(resp.content)}  "
             f"final_url={resp.url}")
        # Print the raw body (truncated) regardless of success / failure so we
        # can see EXACTLY what SharpAPI is returning -- the symptom 'status=200
        # with 0 rows' is almost always explained by an error field or a hint
        # buried in the body that a status code alone won't surface.
        try:
            body_preview = resp.text[:2000]
        except Exception as exc:                                          # noqa: BLE001
            body_preview = f"<could not decode body: {type(exc).__name__}: {exc}>"
        _log(f"[sharp]   body (first 2000 chars): {body_preview}")
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason or ''} for url: {url}".strip(),
                response=resp,
            )
        try:
            data = resp.json()
        except Exception as exc:                                          # noqa: BLE001
            raise ValueError(
                f"SharpAPI: response was not valid JSON: {type(exc).__name__}: {exc}"
            )
        # SharpAPI wraps every response in {"success": true/false, "data": [...]}
        if not data.get("success", True):
            err = data.get("error", {})
            raise ValueError(
                f"SharpAPI error {err.get('code', '?')}: {err.get('message', data)}"
            )
        return data

    # ------------------------------------------------------------------
    # Endpoint + auth-style discovery probe
    # ------------------------------------------------------------------

    def probe_endpoints(self) -> list[dict]:
        """One-shot discovery probe.  Hits the likely list-style endpoints
        (`/sports`, `/leagues`, `/competitions`) with two auth styles, plus
        a handful of `/odds?league=...` variations covering the common
        identifier conventions.

        Returns a list of {endpoint, auth, status, sample} dicts.  Body of
        each response is captured (truncated) so the caller can inspect what
        identifiers SharpAPI actually accepts.

        The expected use: trigger this once from /admin -> Diagnostics
        ("Probe SharpAPI"), inspect the rows, then update SPORT_MAP +
        endpoint path / auth style to whatever combo actually works.
        """
        results: list[dict] = []

        # Endpoints worth probing.  Some return sport / league lists, the
        # others reuse `/odds` with different league strings to see which
        # SharpAPI accepts.
        endpoints = [
            ("GET /sports",                        "/sports",       {}),
            ("GET /leagues",                       "/leagues",      {}),
            ("GET /competitions",                  "/competitions", {}),
            ("GET /odds  league=mlb",              "/odds",         {"league": "mlb"}),
            ("GET /odds  league=MLB",              "/odds",         {"league": "MLB"}),
            ("GET /odds  league=baseball_mlb",     "/odds",         {"league": "baseball_mlb"}),
            ("GET /odds  league=baseball-mlb",     "/odds",         {"league": "baseball-mlb"}),
            ("GET /odds  league=baseball",         "/odds",         {"league": "baseball"}),
            ("GET /odds  sport=mlb",               "/odds",         {"sport": "mlb"}),
            ("GET /odds  sport=baseball",          "/odds",         {"sport": "baseball"}),
            ("GET /odds  league=wnba",             "/odds",         {"league": "wnba"}),
            ("GET /odds  league=WNBA",             "/odds",         {"league": "WNBA"}),
            ("GET /odds  league=basketball_wnba",  "/odds",         {"league": "basketball_wnba"}),
        ]

        # Two auth styles -- the existing X-API-Key header and the common
        # Authorization Bearer header.  If neither works, the SharpAPI
        # account may need a different scheme (e.g. ?api_key= query); add
        # more variants here once we know what fails.
        auth_styles = [
            ("X-API-Key header",        {"X-API-Key": self.api_key},
             None),
            ("Authorization: Bearer",   {"Authorization": f"Bearer {self.api_key}"},
             None),
            ("?apiKey query param",     {},
             {"apiKey": self.api_key}),
        ]

        sess = requests.Session()
        for label, path, extra_params in endpoints:
            for auth_label, headers, extra_qs in auth_styles:
                url = f"{self._BASE_URL}{path}"
                params = dict(extra_params)
                if extra_qs:
                    params.update(extra_qs)
                try:
                    resp = sess.get(
                        url, params=params, headers=headers, timeout=8,
                    )
                    body = (resp.text or "")[:600]
                    results.append({
                        "endpoint":   label,
                        "auth":       auth_label,
                        "status":     resp.status_code,
                        "ok":         resp.ok,
                        "bytes":      len(resp.content),
                        "sample":     body,
                    })
                except Exception as exc:                                  # noqa: BLE001
                    results.append({
                        "endpoint": label,
                        "auth":     auth_label,
                        "status":   "ERROR",
                        "ok":       False,
                        "bytes":    0,
                        "sample":   f"{type(exc).__name__}: {exc}",
                    })
        return results

    def get_odds(
        self,
        sport_key: str,
        markets: str = "h2h,spreads,totals",
        regions: str = "us",
    ) -> list[dict]:
        """Return upcoming games for *sport_key* — same shape as OddsClient."""
        league = self.SPORT_MAP.get(sport_key)
        if league is None:
            raise ValueError(f"SharpAPI: unsupported sport_key {sport_key!r}")

        cache_key = f"sharp_odds_{sport_key}_{markets}_{regions}"
        cached = self.cache.get(cache_key, ttl=900)
        if cached is not None:
            _log(f"[sharp] cache HIT  cache_key={cache_key!r}  count={len(cached)}")
            return cached

        _log(f"[sharp] cache MISS cache_key={cache_key!r} -- calling SharpAPI")
        raw = self._get("/odds", {
            "league": league,
            "market": "main",   # "main" covers moneyline + spread + totals
            "live":   "false",
            "limit":  200,
        })

        rows = raw.get("data") or []
        _log(f"[sharp] raw rows={len(rows)} for league={league!r}")
        if not rows:
            _log("[sharp] WARN: no rows returned from SharpAPI")

        parsed = self._assemble_games(rows)
        self.cache.set(cache_key, parsed)
        return parsed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _assemble_games(self, rows: list[dict]) -> list[dict]:
        """Group flat SharpAPI rows by event_id into per-game dicts."""
        # Bucket rows by event — preserve insertion order (first sportsbook wins).
        event_meta: dict[str, dict] = {}
        event_rows: dict[str, list[dict]] = defaultdict(list)

        for row in rows:
            if row.get("is_live"):
                continue
            eid = row.get("event_id") or row.get("id")
            if not eid:
                continue
            if eid not in event_meta:
                event_meta[eid] = {
                    "id":            eid,
                    "commence_time": row.get("event_start_time", ""),
                    "home_team":     row.get("home_team", ""),
                    "away_team":     row.get("away_team", ""),
                }
            event_rows[eid].append(row)

        result: list[dict] = []
        drop_reasons: dict[str, int] = {}

        for eid, meta in event_meta.items():
            game = self._build_game(meta, event_rows[eid])
            if game is None:
                why = meta.get("_drop_reason", "missing h2h odds")
                drop_reasons[why] = drop_reasons.get(why, 0) + 1
                continue
            result.append(game)

        _log(f"[sharp] assembled {len(result)} games from {len(event_meta)} events")
        if drop_reasons:
            _log(f"[sharp] drop reasons: "
                 f"{ {k: v for k, v in sorted(drop_reasons.items(), key=lambda kv: -kv[1])} }")
        return result

    @staticmethod
    def _build_game(meta: dict, rows: list[dict]) -> Optional[dict]:
        """Populate odds fields from a list of same-event rows."""
        home = meta["home_team"]
        away = meta["away_team"]

        h2h_home = h2h_away = None
        rl_home = rl_away = rl_point = None
        over = under = total = None

        for row in rows:
            mtype = (row.get("market_type") or "").lower()
            sel   = (row.get("selection")   or "").lower()
            price = row.get("odds_american")
            line  = row.get("line")

            if mtype in ("moneyline", "h2h"):
                if sel == "home" and h2h_home is None:
                    h2h_home = price
                elif sel == "away" and h2h_away is None:
                    h2h_away = price

            elif mtype in ("spread", "run_line", "runline"):
                if sel == "home" and rl_home is None:
                    rl_home  = price
                    rl_point = line
                elif sel == "away" and rl_away is None:
                    rl_away = price

            elif mtype in ("total", "totals", "over_under"):
                if sel == "over" and over is None:
                    over  = price
                    total = line
                elif sel == "under" and under is None:
                    under = price

        if h2h_home is None or h2h_away is None:
            meta["_drop_reason"] = (
                f"h2h missing (home={home!r} h2h_home={h2h_home}, "
                f"away={away!r} h2h_away={h2h_away})"
            )
            return None

        raw_home = _american_to_prob(h2h_home)
        raw_away = _american_to_prob(h2h_away)
        home_prob, away_prob = _remove_vig(raw_home, raw_away)

        return {
            "id":                meta["id"],
            "commence_time":     meta["commence_time"],
            "home_team":         home,
            "away_team":         away,
            "h2h_home_odds":     h2h_home,
            "h2h_away_odds":     h2h_away,
            "home_implied_prob": round(home_prob, 4),
            "away_implied_prob": round(away_prob, 4),
            "spread":            rl_point,
            "run_line_home_odds": rl_home,
            "run_line_away_odds": rl_away,
            "run_line_point":    rl_point,
            "over_odds":         over,
            "under_odds":        under,
            "total_line":        total,
        }


# ---------------------------------------------------------------------------
# The Odds API client (now acts as fallback)
# ---------------------------------------------------------------------------

class OddsClient:
    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = requests.Session()
        # SharpAPI is opt-in fallback.  The Odds API is the primary source
        # (last edit).  get_odds() tries The Odds API first; SharpAPI is
        # only contacted if the primary call raises and SHARPAPI_KEY is set.
        _sharp_key = os.environ.get("SHARPAPI_KEY", "").strip()
        self._sharp: Optional[SharpApiClient] = (
            SharpApiClient(_sharp_key, self.cache) if _sharp_key else None
        )
        if self._sharp:
            _log("OddsClient: SharpAPI fallback configured (SHARPAPI_KEY present)")

    def _get(self, path: str, params: dict) -> dict | list:
        params["apiKey"] = self.api_key
        url = f"{BASE_URL}{path}"
        _log(f"GET {_redact_url(url)} params={ {k: v for k, v in params.items() if k != 'apiKey'} }")
        resp = self.session.get(url, params=params, timeout=15)
        _log(f"  -> status={resp.status_code}  bytes={len(resp.content)}  "
             f"final_url={_redact_url(resp.url)}")
        if not resp.ok:
            # The default HTTPError message embeds resp.url verbatim, which
            # includes `?apiKey=...`.  Strip the key before raising so the
            # exception (and any traceback that surfaces it) is safe to log.
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason or ''} for url: "
                f"{_redact_url(resp.url)}".strip(),
                response=resp,
            )
        self._log_quota(resp)
        return resp.json()

    @staticmethod
    def _log_quota(resp: requests.Response) -> None:
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        if remaining != "?":
            _log(f"  quota used={used}, remaining={remaining}")

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_odds(
        self,
        sport_key: str,
        markets: str = "h2h,spreads,totals",
        regions: str = "us",
        force_refresh: bool = False,
    ) -> list[dict]:
        """Return upcoming games for *sport_key* with implied probabilities.

        Source order (changed per request): **The Odds API is primary**;
        SharpAPI is the fallback when SHARPAPI_KEY is set and The Odds API
        raises.  Both return the same normalised per-game dict so callers
        don't care which path produced the result.

        Daily Supabase cache
        --------------------
        Before either source is contacted, look up today's results in the
        `app_cache` table (key `odds_daily:<sport>:<markets>:<regions>`).
        On a hit we skip the live call entirely -- this is what drops the
        upstream usage to ~1 API call per sport per day.

        Non-empty results returned from a live call are written back to
        the daily cache so the next analyze in the same day is free.
        Empty results are NOT cached (otherwise an early-morning "no lines
        posted yet" outcome would lock the cache to 0 games for the
        remainder of the day).

        force_refresh=True bypasses the cache read (cache write still happens
        on success so subsequent calls today benefit).

        sport_key must be an active Odds API sport key, e.g.:
          "baseball_mlb"      — MLB moneylines / run lines / totals
          "basketball_wnba"   — WNBA moneylines / spreads / totals
        """
        _log(f"get_odds(sport_key={sport_key!r}, markets={markets!r}, "
             f"regions={regions!r}, force_refresh={force_refresh})")

        # ── Daily Supabase cache (primary fast-path) ─────────────────────
        if not force_refresh:
            cached = _read_daily_odds_cache(sport_key, markets, regions)
            if cached is not None:
                return cached
        else:
            _log(f"get_odds: force_refresh=True, bypassing daily Supabase cache")

        # ── The Odds API (primary) ───────────────────────────────────────
        primary_exc: Optional[Exception] = None
        try:
            result = self._fetch_via_the_odds_api(sport_key, markets, regions)
            _log(f"get_odds: The Odds API returned {len(result)} games (primary)")
            _write_daily_odds_cache(sport_key, markets, regions, result)
            return result
        except Exception as exc:                                          # noqa: BLE001
            primary_exc = exc
            _log(f"get_odds: The Odds API failed -- "
                 f"{type(exc).__name__}: {exc} -- trying SharpAPI fallback")

        # ── SharpAPI (fallback) ──────────────────────────────────────────
        if self._sharp and sport_key in SharpApiClient.SPORT_MAP:
            try:
                result = self._sharp.get_odds(sport_key, markets, regions)
                _log(f"get_odds: SharpAPI returned {len(result)} games (fallback)")
                _write_daily_odds_cache(sport_key, markets, regions, result)
                return result
            except Exception as exc:                                      # noqa: BLE001
                _log(f"get_odds: SharpAPI fallback also failed -- "
                     f"{type(exc).__name__}: {exc}")

        # Both sources exhausted -- re-raise the primary error so the caller
        # sees the most important failure mode (The Odds API) rather than
        # whatever SharpAPI returned second.
        raise primary_exc

    # ------------------------------------------------------------------
    # Source implementations
    # ------------------------------------------------------------------

    def _fetch_via_the_odds_api(
        self, sport_key: str, markets: str, regions: str,
    ) -> list[dict]:
        """Fetch + parse upcoming games from The Odds API.

        Honors the existing in-memory 15-min TTL cache (separate from the
        daily Supabase cache handled by the caller).  Returns the parsed
        list; raises on HTTP / parse failure so the caller can fall back."""
        cache_key = f"odds_{sport_key}_{markets}_{regions}"
        cached = self.cache.get(cache_key, ttl=900)  # 15-min TTL
        if cached is not None:
            _log(f"  cache HIT  cache_key={cache_key!r}  cached_count={len(cached)}")
            return cached
        _log(f"  cache MISS cache_key={cache_key!r} -- calling API")

        raw = self._get(f"/sports/{sport_key}/odds/", {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
        })

        # Telemetry on the raw API response BEFORE any parsing / filtering.
        if not isinstance(raw, list):
            _log(f"  WARN raw response is not a list: type={type(raw).__name__}  value={raw!r}")
            raw = []
        _log(f"  raw_count={len(raw)} games returned by API "
             f"(no client-side date / commence_time filter applied)")
        if raw:
            try:
                first = json.dumps(raw[0], default=str)[:800]
            except Exception as exc:                                      # noqa: BLE001
                first = f"<could not serialize: {type(exc).__name__}: {exc}>"
            _log(f"  raw[0] (first 800 chars): {first}")
            try:
                summary = [
                    f"{(g.get('away_team') or '?')[:3]}@{(g.get('home_team') or '?')[:3]} "
                    f"{(g.get('commence_time') or '?')[:16]}"
                    for g in raw[:10]
                ]
                _log(f"  raw summary (first 10): {' | '.join(summary)}")
            except Exception:                                             # noqa: BLE001
                pass

        # Parse + count drops.  Aggregate _last_drop_reason for diagnosability.
        parsed: list[dict] = []
        drop_reasons: dict[str, int] = {}
        for g in raw:
            try:
                out = self._parse_game(g)
            except Exception as exc:                                      # noqa: BLE001
                drop_reasons[f"_parse_game raised {type(exc).__name__}"] = \
                    drop_reasons.get(f"_parse_game raised {type(exc).__name__}", 0) + 1
                continue
            if out is None:
                why = getattr(self, "_last_drop_reason", "unknown")
                drop_reasons[why] = drop_reasons.get(why, 0) + 1
                continue
            parsed.append(out)

        _log(f"  parsed_count={len(parsed)} (parse step kept {len(parsed)}/{len(raw)})")
        if drop_reasons:
            _log(f"  parse drop reasons: "
                 f"{ {k: v for k, v in sorted(drop_reasons.items(), key=lambda kv: -kv[1])} }")

        self.cache.set(cache_key, parsed)
        return parsed

    def get_scores(self, sport_key: str, days_from: int = 3) -> list[dict]:
        """Return recently completed games (free tier = 3 days)."""
        cache_key = f"scores_{sport_key}_{days_from}"
        cached = self.cache.get(cache_key, ttl=3600)
        if cached is not None:
            return cached

        raw = self._get(f"/sports/{sport_key}/scores/", {"daysFrom": days_from})
        completed = [g for g in raw if g.get("completed")]
        self.cache.set(cache_key, completed)
        return completed

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_game(self, game: dict) -> Optional[dict]:
        """Flatten a raw Odds API game into a simpler structure.

        Records a self._last_drop_reason string when returning None so the
        caller can aggregate drop reasons across the slate.
        """
        self._last_drop_reason = None
        try:
            home = game["home_team"]
            away = game["away_team"]
        except KeyError as exc:
            self._last_drop_reason = f"missing field {exc.args[0]!r} in raw game"
            return None

        h2h_home_odds = h2h_away_odds = None
        spread = None
        rl_home_odds = rl_away_odds = rl_point = None
        over_odds = under_odds = total_line = None
        bookmaker_count = len(game.get("bookmakers") or [])

        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market["key"] == "h2h" and h2h_home_odds is None:
                    outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
                    h2h_home_odds = outcomes.get(home)
                    h2h_away_odds = outcomes.get(away)
                elif market["key"] == "spreads" and spread is None:
                    for o in market["outcomes"]:
                        if o["name"] == home:
                            spread        = o.get("point")
                            rl_home_odds  = o.get("price")
                            rl_point      = o.get("point")
                        elif o["name"] == away:
                            rl_away_odds  = o.get("price")
                elif market["key"] == "totals" and total_line is None:
                    for o in market["outcomes"]:
                        if o["name"] == "Over":
                            over_odds  = o.get("price")
                            total_line = o.get("point")
                        elif o["name"] == "Under":
                            under_odds = o.get("price")

        if h2h_home_odds is None or h2h_away_odds is None:
            if bookmaker_count == 0:
                self._last_drop_reason = "no bookmakers in game.bookmakers[]"
            elif h2h_home_odds is None and h2h_away_odds is None:
                self._last_drop_reason = (
                    f"h2h market missing for both teams "
                    f"(home={home!r}, away={away!r}, {bookmaker_count} bookmakers)"
                )
            elif h2h_home_odds is None:
                self._last_drop_reason = f"h2h missing for home team {home!r}"
            else:
                self._last_drop_reason = f"h2h missing for away team {away!r}"
            return None

        raw_home = _american_to_prob(h2h_home_odds)
        raw_away = _american_to_prob(h2h_away_odds)
        home_prob, away_prob = _remove_vig(raw_home, raw_away)

        return {
            "id": game["id"],
            "commence_time": game["commence_time"],
            "home_team": home,
            "away_team": away,
            "h2h_home_odds":    h2h_home_odds,
            "h2h_away_odds":    h2h_away_odds,
            "home_implied_prob": round(home_prob, 4),
            "away_implied_prob": round(away_prob, 4),
            "spread":            spread,           # moneyline spread point
            "run_line_home_odds": rl_home_odds,    # ATS home odds (usually -115)
            "run_line_away_odds": rl_away_odds,    # ATS away odds
            "run_line_point":    rl_point,         # -1.5 (home favored by 1.5)
            "over_odds":         over_odds,        # O/U over odds
            "under_odds":        under_odds,       # O/U under odds
            "total_line":        total_line,       # posted O/U number (e.g. 8.5)
        }
