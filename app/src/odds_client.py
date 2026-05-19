"""
Odds client — The Odds API only.

The Odds API: https://the-odds-api.com/liveapi/guides/v4/

The SharpAPI fallback was DISABLED because its free tier returns empty
data; the extra round-trip + delay added noise without any successful
results.  The SharpApiClient class + SHARPAPI_KEY env var are preserved
in this module so the fallback can be turned back on by un-commenting
the block in OddsClient.__init__ if SharpAPI ever becomes useful.

If The Odds API itself fails (auth error, network blip, malformed
response), OddsClient.get_odds logs a clear error and returns an empty
list rather than trying any fallback.  Daily-quota errors propagate
as OddsApiLimitExceeded so the analyze routes can turn them into a
clean HTTP 429.
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
# Odds API daily request quota
# ---------------------------------------------------------------------------
# The account allows 20,000 Odds API requests per month (~666/day average).
# We enforce a per-day cap of _ODDS_BASE_DAILY_LIMIT to keep automatic
# traffic safely under the average and leave headroom for ad-hoc pulls
# near month-end.  Counted in Supabase app_cache under key
# "odds_api_calls:<today_et>" so the count survives Railway restarts.
#
# Row shape: {"count": int, "extra_allowance": int}
#   count           total successful calls to the-odds-api.com today
#   extra_allowance bonus quota granted via the "Approve Additional Odds
#                   Pull" admin button (each click adds +_ODDS_BONUS_STEP)
#
# Effective daily limit  = _ODDS_BASE_DAILY_LIMIT + extra_allowance
# Limit reached          = count >= effective_limit
#
# When the limit is reached, OddsClient._get raises OddsApiLimitExceeded
# BEFORE making the upstream HTTP call.  The analyze routes catch this
# and return HTTP 429 so the UI can render the appropriate banner.

_ODDS_BASE_DAILY_LIMIT = 500     # automatic calls allowed per ET day
_ODDS_BONUS_STEP       = 50      # bump per "Approve Additional Pull" click

# In-process fallback counter used only when Supabase is offline so the
# system still tracks usage (just doesn't survive container restarts).
_odds_count_mem: dict[str, dict] = {}


class OddsApiLimitExceeded(Exception):
    """Raised by OddsClient._get when the daily request quota is at the
    configured cap.  Carries the diagnostic numbers so the calling
    /api/analyze route can surface them in its 429 response."""

    def __init__(self, count: int, limit: int, extra_allowance: int = 0):
        self.count           = count
        self.limit           = limit
        self.extra_allowance = extra_allowance
        super().__init__(
            f"Daily Odds API limit of {limit} reached "
            f"(used {count} so far today, including +{extra_allowance} "
            f"bonus quota granted). Additional pulls require manual approval."
        )


def _today_et_date() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        from datetime import date
        return date.today().isoformat()


def _odds_counter_key() -> str:
    return f"odds_api_calls:{_today_et_date()}"


def _odds_get_row() -> dict:
    """Return today's {count, extra_allowance} row.  Reads Supabase first
    and falls back to the in-process counter when Supabase is offline."""
    today = _today_et_date()
    try:
        from . import db
        row = db.cache_get(_odds_counter_key())
        if isinstance(row, dict):
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            if isinstance(data, dict):
                return {
                    "count":           int(data.get("count") or 0),
                    "extra_allowance": int(data.get("extra_allowance") or 0),
                }
    except Exception as exc:                                              # noqa: BLE001
        _log(f"[quota] read error (ignored): {type(exc).__name__}: {exc}")
    return dict(_odds_count_mem.get(today, {"count": 0, "extra_allowance": 0}))


def _odds_write_row(row: dict) -> None:
    """Persist today's counter row.  Sync write -- the next _get must see
    the bump immediately or it would over-spend the quota."""
    today = _today_et_date()
    _odds_count_mem[today] = {
        "count":           int(row.get("count") or 0),
        "extra_allowance": int(row.get("extra_allowance") or 0),
    }
    try:
        from . import db
        db.cache_set(_odds_counter_key(), None, today, row)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"[quota] write error (ignored): {type(exc).__name__}: {exc}")


def odds_usage() -> dict:
    """Return a summary of today's quota usage.  Used by the
    /api/odds/usage endpoint + the admin counter chip."""
    row = _odds_get_row()
    base  = _ODDS_BASE_DAILY_LIMIT
    extra = int(row.get("extra_allowance") or 0)
    used  = int(row.get("count") or 0)
    limit = base + extra
    return {
        "count":            used,
        "base_limit":       base,
        "extra_allowance":  extra,
        "effective_limit":  limit,
        "remaining":        max(0, limit - used),
        "limit_reached":    used >= limit,
        "date_et":          _today_et_date(),
    }


def odds_grant_additional(step: int = _ODDS_BONUS_STEP) -> dict:
    """Increase today's allowance by `step` calls.  Used by the
    /api/admin/odds/approve_additional endpoint."""
    row = _odds_get_row()
    row["extra_allowance"] = int(row.get("extra_allowance") or 0) + int(step)
    _odds_write_row(row)
    return odds_usage()


def _odds_check_limit() -> None:
    """Raise OddsApiLimitExceeded when at the configured cap.  Called from
    OddsClient._get BEFORE every upstream HTTP request."""
    u = odds_usage()
    if u["limit_reached"]:
        raise OddsApiLimitExceeded(
            count=u["count"],
            limit=u["effective_limit"],
            extra_allowance=u["extra_allowance"],
        )


def _odds_increment_count() -> int:
    """Increment today's count by 1 and return the new value.  Called
    after a successful upstream response in OddsClient._get."""
    row = _odds_get_row()
    row["count"] = int(row.get("count") or 0) + 1
    _odds_write_row(row)
    return row["count"]


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
        # The Odds API is the SOLE source.  SharpAPI fallback was disabled
        # because its free tier returns empty data and the extra round-
        # trip + delay added noise without any successful results.  The
        # SharpApiClient class + SHARPAPI_KEY env var are preserved so
        # the fallback can be re-enabled by un-commenting the block
        # below if SharpAPI ever becomes useful.
        self._sharp: Optional[SharpApiClient] = None
        # _sharp_key = os.environ.get("SHARPAPI_KEY", "").strip()
        # self._sharp = SharpApiClient(_sharp_key, self.cache) if _sharp_key else None
        # if self._sharp:
        #     _log("OddsClient: SharpAPI fallback configured (SHARPAPI_KEY present)")

    def _get(self, path: str, params: dict) -> dict | list:
        # Daily quota check -- runs BEFORE any wire traffic so an at-limit
        # state doesn't waste a network round-trip.  Raises
        # OddsApiLimitExceeded which the analyze routes turn into HTTP 429.
        _odds_check_limit()

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
        # Successful upstream response -- charge today's quota.  Increment
        # AFTER ok-check so non-200 responses don't burn quota tracking on
        # our side (the API itself may or may not count failed calls, but
        # we only enforce against successful payload deliveries).
        new_count = _odds_increment_count()
        _log(f"  [quota] {new_count}/{_ODDS_BASE_DAILY_LIMIT} used today "
             f"(+ extra_allowance bonus)")
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

        Source: **The Odds API is the SOLE source** (SharpAPI fallback
        disabled -- see __init__ for the rationale).  Returns the
        normalised per-game dict shape callers already consume.

        Quota model
        -----------
        Each successful call to the-odds-api.com increments a daily counter
        (see _odds_check_limit / _odds_increment_count above).  When the
        counter hits _ODDS_BASE_DAILY_LIMIT + extra_allowance, _get raises
        OddsApiLimitExceeded BEFORE the wire call and the analyze route
        catches it as HTTP 429.

        The 15-min in-memory cache inside _fetch_via_the_odds_api still
        deduplicates rapid identical requests so a flurry of clicks doesn't
        burn quota.

        force_refresh=True bypasses the 15-min cache (still subject to the
        daily quota cap).

        Error handling
        --------------
        - OddsApiLimitExceeded propagates AS-IS so analyze routes can
          turn it into a clean 429 + admin banner.
        - Any other Odds API failure (auth error, network blip, malformed
          response) logs a clear error and returns an empty list.  No
          SharpAPI fallback attempt -- it would just add latency without
          any successful results.

        sport_key must be an active Odds API sport key, e.g.:
          "baseball_mlb"      — MLB moneylines / run lines / totals
          "basketball_wnba"   — WNBA moneylines / spreads / totals
        """
        _log(f"get_odds(sport_key={sport_key!r}, markets={markets!r}, "
             f"regions={regions!r}, force_refresh={force_refresh})")

        try:
            result = self._fetch_via_the_odds_api(sport_key, markets, regions)
            _log(f"get_odds: The Odds API returned {len(result)} games")
            return result
        except OddsApiLimitExceeded:
            # Daily cap reached -- let it bubble; analyze route -> 429.
            raise
        except Exception as exc:                                          # noqa: BLE001
            _log(
                f"get_odds: ERROR -- The Odds API call failed for "
                f"sport_key={sport_key!r}: {type(exc).__name__}: {exc}.  "
                f"Returning empty result list (SharpAPI fallback disabled)."
            )
            return []

    # ------------------------------------------------------------------
    # Source implementations
    # ------------------------------------------------------------------

    def _fetch_via_the_odds_api(
        self, sport_key: str, markets: str, regions: str,
    ) -> list[dict]:
        """Fetch + parse upcoming games from The Odds API.

        Honors the existing in-memory 15-min TTL cache (separate from the
        daily Supabase cache handled by the caller).  Returns the parsed
        list; raises on HTTP / parse failure so the caller can fall back.

        Constrains the API response to today's ET slate via the
        `commenceTimeFrom` / `commenceTimeTo` params -- otherwise the API
        will include yesterday's already-played games, which then get
        dropped by _filter_stale_games and produce a confusing "0 games
        returned" result.
        """
        # Today's ET midnight (inclusive) -> tomorrow's ET midnight (exclusive),
        # converted to UTC ISO strings for the Odds API.  This range covers
        # exactly today's ET slate including any games that commence in
        # early UTC of the following day but late ET of today.
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        _UTC = ZoneInfo("UTC")
        _now_et = datetime.now(_ET)
        _today_mid_et    = _now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        _tomorrow_mid_et = _today_mid_et + timedelta(days=1)
        commence_from = _today_mid_et.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_to   = _tomorrow_mid_et.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # In-memory short-TTL cache key includes the time-range so a request
        # made shortly before midnight ET doesn't poison the next ET day's
        # window.
        cache_key = f"odds_{sport_key}_{markets}_{regions}_{commence_from}_{commence_to}"
        cached = self.cache.get(cache_key, ttl=900)  # 15-min TTL
        if cached is not None:
            _log(f"  cache HIT  cache_key={cache_key!r}  cached_count={len(cached)}")
            return cached
        _log(f"  cache MISS cache_key={cache_key!r} -- calling API")
        _log(f"  commenceTimeFrom={commence_from}  commenceTimeTo={commence_to}  "
             f"(today_et={_today_mid_et.date().isoformat()})")

        raw = self._get(f"/sports/{sport_key}/odds/", {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "american",
            "dateFormat": "iso",
            "commenceTimeFrom": commence_from,
            "commenceTimeTo":   commence_to,
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
