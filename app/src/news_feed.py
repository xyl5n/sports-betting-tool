"""
news_feed.py
============
ESPN RSS headline fetcher with 5-minute in-process cache.

Supported sports and their feed URLs:

    mlb   https://www.espn.com/espn/rss/mlb/news
    nba   https://www.espn.com/espn/rss/nba/news
    nfl   https://www.espn.com/espn/rss/nfl/news
    nhl   https://www.espn.com/espn/rss/nhl/news
    wnba  → NBA feed (no WNBA-specific ESPN RSS)

Each item dict:
    {
        "title":    str,   # headline, HTML-safe
        "link":     str,   # canonical article URL
        "time_ago": str,   # "42m ago" / "3h ago" / "2d ago"
        "source":   str,   # always "ESPN"
    }

Cache: per-sport in-process dict keyed by sport slug, TTL = 300 s.
A lock-free design is intentional: the scheduler thread and the NiceGUI
event loop both read this module.  Occasional double-fetches on cache
miss are acceptable; the dict assignment is atomic in CPython.
"""
from __future__ import annotations

import html as _html
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

_FEEDS: dict[str, str] = {
    "mlb":  "https://www.espn.com/espn/rss/mlb/news",
    "nba":  "https://www.espn.com/espn/rss/nba/news",
    "nfl":  "https://www.espn.com/espn/rss/nfl/news",
    "nhl":  "https://www.espn.com/espn/rss/nhl/news",
    "wnba": "https://www.espn.com/espn/rss/nba/news",  # no WNBA-specific feed
}

_TTL = 300  # seconds

# {sport: {"ts": float, "items": list[dict]}}
_CACHE: dict[str, dict] = {}


def _log(msg: str) -> None:
    print(f"NEWS-FEED: {msg}", flush=True, file=sys.stderr)


def _time_ago(pub_date_str: str) -> str:
    """Parse an RFC 2822 pubDate into a human-relative string.

    Outputs: "Xm ago" (< 1 h), "Xh ago" (< 24 h), "Xd ago" (≥ 1 d).
    Returns an empty string on any parse failure so the caller can omit
    the field gracefully rather than showing a broken timestamp.
    """
    if not pub_date_str:
        return ""
    try:
        dt   = parsedate_to_datetime(pub_date_str)
        diff = int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        diff = max(0, diff)
        if diff < 3600:
            return f"{max(1, diff // 60)}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:                                                      # noqa: BLE001
        return ""


def _parse_items(xml_bytes: bytes) -> list[dict]:
    """Parse RSS 2.0 bytes and return a list of item dicts.

    ElementTree automatically unescapes XML entities (&amp; → &, etc.),
    so titles are re-escaped with html.escape() before being returned so
    callers can embed them safely in HTML without double-escaping.
    """
    root  = ET.fromstring(xml_bytes)
    items = []
    for elem in root.iter("item"):
        title = (elem.findtext("title") or "").strip()
        link  = (elem.findtext("link")  or "").strip()
        pub   = (elem.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        # Normalise: strip trailing query strings ESPN sometimes appends
        # (e.g. ?ex_cid=espnrss) so links look clean.
        clean_link = link.split("?")[0] if "?" in link else link
        items.append({
            "title":    _html.escape(title),
            "link":     clean_link,
            "time_ago": _time_ago(pub),
            "source":   "ESPN",
        })
    return items


def fetch(sport: str = "mlb", max_items: int = 10) -> list[dict]:
    """Return up to *max_items* headline dicts for *sport*.

    Serves from the 5-minute in-process cache when available.  On cache
    miss issues one synchronous HTTP GET (timeout = 5 s) to the ESPN RSS
    endpoint.  All errors return an empty list so the caller can render
    a graceful empty state without guarding every call.
    """
    slug = (sport or "mlb").lower()
    url  = _FEEDS.get(slug, _FEEDS["mlb"])

    entry = _CACHE.get(slug)
    if entry and (time.monotonic() - entry["ts"]) < _TTL:
        return entry["items"][:max_items]

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; sports-betting-ai/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_bytes = resp.read()
        items = _parse_items(xml_bytes)
        _CACHE[slug] = {"ts": time.monotonic(), "items": items}
        _log(f"fetched {len(items)} items for '{slug}'")
        return items[:max_items]
    except Exception as exc:                                               # noqa: BLE001
        _log(f"fetch failed for '{slug}': {type(exc).__name__}: {exc}")
        # Stale cache is better than nothing — serve it if present.
        if entry:
            return entry["items"][:max_items]
        return []


def cached_at(sport: str = "mlb") -> Optional[float]:
    """Return the monotonic timestamp of the most recent successful
    fetch for *sport*, or None if never fetched."""
    entry = _CACHE.get((sport or "mlb").lower())
    return entry["ts"] if entry else None
