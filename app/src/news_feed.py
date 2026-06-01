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
        "image":    str,   # 16:9 banner/thumbnail URL ("" when none found)
        "source":   str,   # always "ESPN"
    }

Cache: per-sport in-process dict keyed by sport slug, TTL = 300 s.
A lock-free design is intentional: the scheduler thread and the NiceGUI
event loop both read this module.  Occasional double-fetches on cache
miss are acceptable; the dict assignment is atomic in CPython.
"""
from __future__ import annotations

import concurrent.futures
import html as _html
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Optional

import requests

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


# Media RSS namespace ESPN uses for <media:content> / <media:thumbnail>.
_MRSS = "http://search.yahoo.com/mrss/"


def _extract_image(elem: ET.Element) -> str:
    """Pull a banner/thumbnail URL out of an RSS <item>.

    ESPN exposes article art in any of three shapes depending on the feed:
      * <media:thumbnail url="...">  (Media RSS)
      * <media:content   url="..." medium="image">
      * <enclosure url="..." type="image/jpeg">
    We try them in that order and return the first usable URL, or "" so the
    caller can fall back to a placeholder rather than a broken <img>.
    """
    # media:thumbnail / media:content (namespaced)
    for tag in (f"{{{_MRSS}}}thumbnail", f"{{{_MRSS}}}content"):
        for node in elem.iter(tag):
            url = (node.get("url") or "").strip()
            if url:
                return url
    # <enclosure type="image/*">
    for node in elem.iter("enclosure"):
        url  = (node.get("url") or "").strip()
        typ  = (node.get("type") or "").lower()
        if url and (typ.startswith("image/") or not typ):
            return url
    return ""


# ── og:image enrichment ─────────────────────────────────────────────────────
# ESPN's RSS feeds frequently omit <media:*>/<enclosure> art (the MLB feed
# carries none), so when an item has no inline image we scrape the article's
# Open Graph <meta property="og:image"> tag.  Scrapes run in parallel with a
# tight per-request timeout and are cached per-URL for the same TTL as the
# feed, so a hot page reload pays at most one scrape per article per 5 min.

# {url: {"ts": monotonic, "image": str}}  ("" image == scraped, none found)
_IMG_CACHE: dict[str, dict] = {}

_OG_SCRAPE_TIMEOUT = 1.5   # seconds, per HTTP request
_OG_MAX_WORKERS    = 8


class _OGImageParser(HTMLParser):
    """Minimal parser that captures the first <meta property="og:image">.

    Stops recording once found.  Also accepts the url/secure_url variants
    ESPN occasionally emits.  Never raises on malformed markup.
    """
    _WANTED = {"og:image", "og:image:url", "og:image:secure_url"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.image or tag != "meta":
            return
        a = dict(attrs)
        prop = (a.get("property") or a.get("name") or "").lower()
        if prop in self._WANTED:
            self.image = (a.get("content") or "").strip()


def _scrape_og_image(url: str) -> str:
    """Fetch *url* and return its og:image URL, or "" on any failure.

    The result (including "") is cached per-URL for _TTL seconds so repeated
    renders within the cache window never re-hit the network.  Silent: every
    failure mode (timeout, DNS, non-200, bad markup) maps to "".
    """
    if not url:
        return ""
    hit = _IMG_CACHE.get(url)
    if hit and (time.monotonic() - hit["ts"]) < _TTL:
        return hit["image"]

    image = ""
    try:
        resp = requests.get(
            url,
            timeout=_OG_SCRAPE_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; sports-betting-ai/1.0)"},
        )
        if resp.status_code == 200 and resp.text:
            parser = _OGImageParser()
            parser.feed(resp.text)
            image = parser.image
    except Exception:                                                      # noqa: BLE001
        image = ""

    _IMG_CACHE[url] = {"ts": time.monotonic(), "image": image}
    return image


def _enrich_images(items: list[dict]) -> None:
    """In-place: fill item['image'] for items lacking one via og:image scrape.

    Scrapes run concurrently in a thread pool, each request bounded by
    _OG_SCRAPE_TIMEOUT.  Items that already carry a media/enclosure image are
    left untouched and never scraped.  Any per-item failure leaves "" silently.
    """
    targets = [it for it in items if not it.get("image")]
    if not targets:
        return
    workers = min(_OG_MAX_WORKERS, len(targets))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            results = ex.map(_scrape_og_image, [it["link"] for it in targets])
            for it, img in zip(targets, results):
                if img:
                    it["image"] = img
    except Exception:                                                      # noqa: BLE001
        # A pool/scheduling failure must never break the feed.
        pass


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
            "image":    _extract_image(elem),
            "source":   "ESPN",
        })
    _enrich_images(items)
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
