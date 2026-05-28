"""
Fetches game-time weather for MLB stadiums via the Open-Meteo API
(free, no API key required — https://open-meteo.com/).

Returns wind_speed (mph) and temperature (°F) for the hour closest to
the game's scheduled start time.
"""
from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Shared helpers — imported from utils instead of defined locally
from .utils import _fetch_url as _fetch  # noqa: E402

_BASE = "https://api.open-meteo.com/v1/forecast"
_CACHE_FILE = Path(".cache/weather_cache.json")
_CACHE_TTL = 1800  # 30 minutes — weather changes

# Neutral weather values for dome stadiums or when the API is unavailable.
# Defined at module level so get_game_weather() doesn't re-create on every call.
_NEUTRAL_WEATHER = {"wind_speed": 0.0, "wind_direction": 0.0, "temperature": 72.0}

# Stadium coordinates (lat, lon) keyed by canonical team name
_STADIUMS: dict[str, tuple[float, float]] = {
    "Arizona Diamondbacks":   (33.4453, -112.0667),
    "Atlanta Braves":         (33.8907, -84.4677),
    "Baltimore Orioles":      (39.2838, -76.6218),
    "Boston Red Sox":         (42.3467, -71.0972),
    "Chicago Cubs":           (41.9484, -87.6553),
    "Chicago White Sox":      (41.8300, -87.6339),
    "Cincinnati Reds":        (39.0975, -84.5061),
    "Cleveland Guardians":    (41.4962, -81.6852),
    "Colorado Rockies":       (39.7559, -104.9942),
    "Detroit Tigers":         (42.3390, -83.0485),
    "Houston Astros":         (29.7572, -95.3556),
    "Kansas City Royals":     (39.0517, -94.4803),
    "Los Angeles Angels":     (33.8003, -117.8827),
    "Los Angeles Dodgers":    (34.0739, -118.2400),
    "Miami Marlins":          (25.7781, -80.2197),
    "Milwaukee Brewers":      (43.0283, -87.9712),
    "Minnesota Twins":        (44.9817, -93.2775),
    "New York Mets":          (40.7571, -73.8458),
    "New York Yankees":       (40.8296, -73.9262),
    "Oakland Athletics":      (37.7516, -122.2005),
    "Philadelphia Phillies":  (39.9061, -75.1665),
    "Pittsburgh Pirates":     (40.4469, -80.0057),
    "San Diego Padres":       (32.7073, -117.1566),
    "San Francisco Giants":   (37.7786, -122.3893),
    "Seattle Mariners":       (47.5914, -122.3326),
    "St. Louis Cardinals":    (38.6226, -90.1928),
    "Tampa Bay Rays":         (27.7682, -82.6534),
    "Texas Rangers":          (32.7512, -97.0832),
    "Toronto Blue Jays":      (43.6414, -79.3894),
    "Washington Nationals":   (38.8730, -77.0074),
}

# Dome/retractable stadiums where weather is less relevant
_DOMES: set[str] = {
    "Arizona Diamondbacks",
    "Houston Astros",
    "Miami Marlins",
    "Minnesota Twins",
    "Tampa Bay Rays",
    "Toronto Blue Jays",
    "Seattle Mariners",   # retractable
    "Milwaukee Brewers",  # retractable
    "Texas Rangers",      # retractable Globe Life Field
}


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _CACHE_TTL:
                return raw
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


def _team_coords(home_team: str) -> Optional[tuple[float, float]]:
    if home_team in _STADIUMS:
        return _STADIUMS[home_team]
    home_lower = home_team.lower()
    for team, coords in _STADIUMS.items():
        if team.lower() in home_lower or home_lower in team.lower():
            return coords
    tokens = set(home_lower.split())
    best, best_n = None, 0
    for team, coords in _STADIUMS.items():
        n = len(tokens & set(team.lower().split()))
        if n > best_n:
            best, best_n = coords, n
    return best


def get_game_weather(
    home_team: str,
    commence_time_utc: str,
) -> dict:
    """
    Return {"wind_speed": float, "temperature": float} for game time.
    wind_speed is in mph. temperature is in °F.
    Returns neutral values (0, 72) if unavailable or game is in a dome.
    """
    if home_team in _DOMES:
        return _NEUTRAL_WEATHER

    coords = _team_coords(home_team)
    if coords is None:
        return _NEUTRAL_WEATHER

    lat, lon = coords

    try:
        game_utc = datetime.fromisoformat(commence_time_utc.replace("Z", "+00:00"))
    except Exception:
        return _NEUTRAL_WEATHER

    cache_key = f"wx_{lat:.2f}_{lon:.2f}_{game_utc.date().isoformat()}"
    cache = _load_cache()
    hourly = cache.get(cache_key)

    if hourly is None:
        date_str = game_utc.date().isoformat()
        url = (
            f"{_BASE}?latitude={lat}&longitude={lon}"
            f"&hourly=windspeed_10m,winddirection_10m,temperature_2m"
            f"&windspeed_unit=mph&temperature_unit=fahrenheit"
            f"&timezone=UTC&start_date={date_str}&end_date={date_str}"
        )
        data = _fetch(url)
        hourly_raw = data.get("hourly", {})
        times = hourly_raw.get("time", [])
        winds = hourly_raw.get("windspeed_10m", [])
        dirs  = hourly_raw.get("winddirection_10m", [])
        temps = hourly_raw.get("temperature_2m", [])
        if not times:
            return _NEUTRAL_WEATHER
        hourly = {"times": times, "winds": winds, "dirs": dirs, "temps": temps}
        cache[cache_key] = hourly
        _save_cache(cache)

    times = hourly.get("times", [])
    winds = hourly.get("winds", [])
    dirs  = hourly.get("dirs", [])
    temps = hourly.get("temps", [])

    if not times:
        return _NEUTRAL_WEATHER

    # Find hour closest to game time
    best_idx, best_delta = 0, float("inf")
    for i, t_str in enumerate(times):
        try:
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
            delta = abs((t - game_utc).total_seconds())
            if delta < best_delta:
                best_delta, best_idx = delta, i
        except Exception:
            continue

    wind = float(winds[best_idx]) if best_idx < len(winds) and winds[best_idx] is not None else 0.0
    wdir = float(dirs[best_idx])  if best_idx < len(dirs)  and dirs[best_idx]  is not None else 0.0
    temp = float(temps[best_idx]) if best_idx < len(temps) and temps[best_idx] is not None else 72.0
    return {"wind_speed": wind, "wind_direction": wdir, "temperature": temp}
