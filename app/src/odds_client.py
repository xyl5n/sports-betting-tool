"""
The Odds API client — fetches baseball (MLB) and basketball (WNBA) markets only.
Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
import json
import sys
import requests
from typing import Optional
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


class OddsClient:
    def __init__(self, api_key: str, cache: Optional[Cache] = None):
        self.api_key = api_key
        self.cache = cache or Cache()
        self.session = requests.Session()

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

        sport_key must be an active Odds API sport key, e.g.:
          "baseball_mlb"      — MLB moneylines / run lines / totals
          "basketball_wnba"   — WNBA moneylines / spreads / totals
        """
        _log(f"get_odds(sport_key={sport_key!r}, markets={markets!r}, regions={regions!r})")
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
