"""
Tracks line movement for MLB games by caching the first-seen odds (opening line)
and computing the movement vs the current line at analysis time.

Cache: .cache/line_movement.json
  {game_id: {"opening_home_odds": int, "opening_away_odds": int, "ts": float}}

line_move = current_home_implied_prob - opening_home_implied_prob
  positive  → money moved toward home (home is more favored now than at open)
  negative  → money moved toward away
"""
from __future__ import annotations

import logging
import json
import time
from pathlib import Path
from typing import Optional

_CACHE_FILE = Path(".cache/line_movement.json")


def _implied(american_odds: int) -> float:
    """Convert American odds to vig-free implied probability."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def _load() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}


def _save(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


def record_and_get_movement(
    game_id: str,
    current_home_odds: Optional[int],
    current_away_odds: Optional[int],
) -> float:
    """
    Record opening odds the first time a game is seen.
    Returns line_move (current_home_implied - opening_home_implied).
    Returns 0.0 if odds are missing or this is the first observation.
    """
    if current_home_odds is None or current_away_odds is None:
        return 0.0

    data = _load()
    entry = data.get(game_id)

    if entry is None:
        # First time seeing this game — record as opening line
        data[game_id] = {
            "opening_home_odds": current_home_odds,
            "opening_away_odds": current_away_odds,
            "ts": time.time(),
        }
        _save(data)
        return 0.0

    # Compute movement
    open_implied = _implied(entry["opening_home_odds"])
    curr_implied  = _implied(current_home_odds)
    return round(curr_implied - open_implied, 4)


def purge_old_entries(max_age_days: int = 7) -> None:
    """Remove entries older than max_age_days to keep the cache tidy."""
    data = _load()
    cutoff = time.time() - max_age_days * 86400
    cleaned = {k: v for k, v in data.items() if v.get("ts", 0) > cutoff}
    if len(cleaned) < len(data):
        _save(cleaned)
