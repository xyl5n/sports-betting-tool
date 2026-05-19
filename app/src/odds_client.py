"""
Odds client — primary source is SharpAPI, fallback is The Odds API.

SharpAPI docs:  https://docs.sharpapi.io/en/api-reference/overview
The Odds API:   https://the-odds-api.com/liveapi/guides/v4/

SharpAPI is used when SHARPAPI_KEY is present in env.  If it's absent or the
call fails, the client transparently retries via The Odds API (ODDS_API_KEY).
Both clients return the same normalised per-game dict so the rest of the app
never needs to know which source was used.
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
# SharpAPI client
# ---------------------------------------------------------------------------

class SharpApiClient:
    """Fetches pre-match odds from SharpAPI (https://api.sharpapi.io/api/v1).

    Returns the same normalised per-game dict as OddsClient so the two sources
    are interchangeable from the caller's perspective.
    """

    _BASE_URL = "https://api.sharpapi.io/api/v1"

    # Map Odds API sport keys → SharpAPI league values.
    SPORT_MAP: dict[str, str] = {
        "baseball_mlb":    "mlb",
        "basketball_wnba": "wnba",
    }

    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._BASE_URL}{path}"
        _log(f"[sharp] GET {url} params={params}")
        resp = self.session.get(url, params=params, timeout=15)
        _log(f"[sharp]   -> status={resp.status_code}  bytes={len(resp.content)}")
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason or ''} for url: {url}".strip(),
                response=resp,
            )
        data = resp.json()
        # SharpAPI wraps every response in {"success": true/false, "data": [...]}
        if not data.get("success", True):
            err = data.get("error", {})
            raise ValueError(
                f"SharpAPI error {err.get('code', '?')}: {err.get('message', data)}"
            )
        return data

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
        # SharpAPI is opt-in — read key from env at construction time so no
        # app.py changes are needed.  When present, get_odds() tries Sharp
        # first and only falls back to The Odds API on failure.
        _sharp_key = os.environ.get("SHARPAPI_KEY", "").strip()
        self._sharp: Optional[SharpApiClient] = (
            SharpApiClient(_sharp_key, self.cache) if _sharp_key else None
        )
        if self._sharp:
            _log("OddsClient: SharpAPI primary source configured (SHARPAPI_KEY present)")

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
    ) -> list[dict]:
        """Return upcoming games for *sport_key* with implied probabilities.

        Tries SharpAPI first (if SHARPAPI_KEY is set), then falls back to The
        Odds API.  Both return the same normalised per-game dict.

        sport_key must be an active Odds API sport key, e.g.:
          "baseball_mlb"      — MLB moneylines / run lines / totals
          "basketball_wnba"   — WNBA moneylines / spreads / totals
        """
        _log(f"get_odds(sport_key={sport_key!r}, markets={markets!r}, regions={regions!r})")

        # ── SharpAPI (primary) ────────────────────────────────────────────
        if self._sharp and sport_key in SharpApiClient.SPORT_MAP:
            try:
                result = self._sharp.get_odds(sport_key, markets, regions)
                _log(f"get_odds: SharpAPI returned {len(result)} games")
                return result
            except Exception as exc:
                _log(
                    f"get_odds: SharpAPI failed ({type(exc).__name__}: {exc}) "
                    "-- falling back to The Odds API"
                )

        # ── The Odds API (fallback / sole source when no Sharp key) ───────
        _log(f"get_odds: using The Odds API for sport_key={sport_key!r}")
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
            # Dump the first game with truncation so logs stay readable.
            try:
                first = json.dumps(raw[0], default=str)[:800]
            except Exception as exc:                                      # noqa: BLE001
                first = f"<could not serialize: {type(exc).__name__}: {exc}>"
            _log(f"  raw[0] (first 800 chars): {first}")
            # Plus a one-line summary across all games so we can see if the
            # API returned a sane slate even when individual games drop.
            try:
                summary = [
                    f"{(g.get('away_team') or '?')[:3]}@{(g.get('home_team') or '?')[:3]} "
                    f"{(g.get('commence_time') or '?')[:16]}"
                    for g in raw[:10]
                ]
                _log(f"  raw summary (first 10): {' | '.join(summary)}")
            except Exception:                                             # noqa: BLE001
                pass

        # Parse + count drops.  Track WHY each None came out so a 'parse
        # dropped every game' failure mode is diagnosable from logs alone.
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
                # _parse_game already records why via _last_drop_reason.
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
