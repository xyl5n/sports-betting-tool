"""
ensemble_store.py
=================
Single source of truth for today's final ensemble picks.

After all three models vote and produce a final pick for each game the
serialized result dict is written here.  The file resets automatically
whenever a new calendar day (US/Eastern) is detected.

Schema of data/ensemble_picks_today.json
-----------------------------------------
{
  "date":  "2025-05-17",          # ET date string
  "picks": {
    "mlb":  [ <serialized result dict>, ... ],
    "wnba": [ <serialized result dict>, ... ]
  }
}

Public API
----------
  save(picks, sport)        — overwrite today's picks for *sport*
  get_picks(sport=None)     — return list for *sport*, or {"mlb":…,"wnba":…}
  load()                    — return the raw JSON dict ({"date":…,"picks":…})
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal

_FILE = Path("data/ensemble_picks_today.json")
_logger = logging.getLogger(__name__)

Sport = Literal["mlb", "wnba"]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _today_et() -> str:
    """Return today's date string in US/Eastern time (UTC-4 during DST)."""
    # Simple approximation: ET = UTC-5 (EST) / UTC-4 (EDT).
    # We use UTC-4 (EDT) from Mar–Nov and UTC-5 (EST) Dec–Feb.
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    offset = -4 if 3 <= month <= 11 else -5
    et_now = now_utc + timedelta(hours=offset)
    return et_now.date().isoformat()


def _empty(date: str) -> dict:
    return {"date": date, "picks": {"mlb": [], "wnba": []}}


def _read() -> dict:
    """Read the file from disk; return an empty structure if missing or stale."""
    today = _today_et()
    if not _FILE.exists():
        return _empty(today)
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        if data.get("date") != today:
            return _empty(today)
        return data
    except Exception as exc:
        _logger.warning("ensemble_store: failed to read %s: %s", _FILE, exc)
        return _empty(today)


def _write(data: dict) -> None:
    _FILE.parent.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> dict:
    """Return the raw ensemble file dict: ``{"date": …, "picks": {"mlb": […], "wnba": […]}}``."""
    return _read()


def save(picks: list[dict], sport: Sport) -> None:
    """
    Overwrite today's picks for *sport* with *picks* (list of serialized
    result dicts) and persist to disk.
    """
    data = _read()
    data["picks"][sport] = picks
    _write(data)
    _logger.info("ensemble_store: saved %d %s picks for %s", len(picks), sport, data["date"])


def get_picks(sport: Sport | None = None) -> list[dict] | dict:
    """
    Return today's picks.

    Parameters
    ----------
    sport : ``"mlb"`` | ``"wnba"`` | ``None``
        When *sport* is given return the list for that sport.
        When *None* return the full ``{"mlb": […], "wnba": […]}`` dict.
    """
    data = _read()
    if sport is None:
        return data["picks"]
    return data["picks"].get(sport, [])
