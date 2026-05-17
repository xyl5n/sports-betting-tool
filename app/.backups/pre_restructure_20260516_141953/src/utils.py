"""
Shared utility helpers used across multiple src/ modules.

Centralises functions that were previously copy-pasted into:
  pitcher_client.py, bullpen_client.py, lineup_client.py,
  weather_client.py, features.py, mlb_features.py, wnba_features.py,
  enriched_historical_data.py

Import from here instead of defining locally:
  from .utils import _safe, _team_tokens, _fetch_url
"""
from __future__ import annotations

import json
import urllib.request


# ── Numeric coercion ──────────────────────────────────────────────────────────

def _safe(v, default: float = 0.0) -> float:
    """Safely coerce *v* to float, returning *default* on TypeError/ValueError/NaN."""
    try:
        f = float(v)
        return f if (f == f) else default  # NaN check: NaN != NaN
    except (TypeError, ValueError):
        return default


# ── String / team matching ────────────────────────────────────────────────────

def _team_tokens(name: str) -> set[str]:
    """Return the set of lower-case tokens in a team name for fuzzy matching."""
    return set(name.lower().split())


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 8) -> dict:
    """
    Minimal urllib GET returning a decoded JSON dict.
    Returns {} on any error so callers never need to handle exceptions.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def _fetch_url_ua(url: str, timeout: int = 12) -> dict:
    """
    Same as _fetch_url but sends a User-Agent header.
    Used by enriched_historical_data.py to avoid 403s from MLB Stats API.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sports-betting-ai/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}
