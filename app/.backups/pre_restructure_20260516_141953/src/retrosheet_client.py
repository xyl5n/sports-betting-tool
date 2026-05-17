"""
Retrosheet game log downloader and parser.

Game log ZIP files for each season are freely available from:
  https://www.retrosheet.org/gamelogs/gl{YEAR}.zip

Each file has 161 comma-separated fields per game.  We only need:
  index 0  : Date  (YYYYMMDD)
  index 3  : Visiting team  (3-char Retrosheet code)
  index 6  : Home team      (3-char Retrosheet code)
  index 9  : Visiting score (final)
  index 10 : Home score     (final)

Retrosheet data is provided free of charge.  Cite: www.retrosheet.org
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime
from pathlib import Path

import joblib
import requests

log = logging.getLogger(__name__)

_CACHE_DIR = Path(".cache/retrosheet")

# 3-char Retrosheet code → full team name (matches park_factors.py keys)
RETRO_TO_NAME: dict[str, str] = {
    "ANA": "Los Angeles Angels",
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHA": "Chicago White Sox",
    "CHN": "Chicago Cubs",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KCA": "Kansas City Royals",
    "LAN": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYA": "New York Yankees",
    "NYN": "New York Mets",
    "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SDN": "San Diego Padres",
    "SEA": "Seattle Mariners",
    "SFN": "San Francisco Giants",
    "SLN": "St. Louis Cardinals",
    "TBA": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WAS": "Washington Nationals",
}

# Retrosheet code → FanGraphs/pybaseball abbreviation (for joining team stats)
RETRO_TO_FG: dict[str, str] = {
    "ANA": "LAA", "ARI": "ARI", "ATL": "ATL", "BAL": "BAL",
    "BOS": "BOS", "CHA": "CWS", "CHN": "CHC", "CIN": "CIN",
    "CLE": "CLE", "COL": "COL", "DET": "DET", "HOU": "HOU",
    "KCA": "KC",  "LAN": "LAD", "MIA": "MIA", "MIL": "MIL",
    "MIN": "MIN", "NYA": "NYY", "NYN": "NYM", "OAK": "OAK",
    "PHI": "PHI", "PIT": "PIT", "SDN": "SD",  "SEA": "SEA",
    "SFN": "SF",  "SLN": "STL", "TBA": "TB",  "TEX": "TEX",
    "TOR": "TOR", "WAS": "WSH",
}

# FanGraphs abbreviation → Retrosheet code (reverse map, used by _load_pybaseball_stats)
FG_TO_RETRO: dict[str, str] = {v: k for k, v in RETRO_TO_FG.items()}


def get_season_gamelogs(season: int) -> list[dict]:
    """
    Return parsed Retrosheet game records for one season.
    Downloads from retrosheet.org once and caches to .cache/retrosheet/.
    Returns empty list if download fails.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"gl{season}.joblib"

    if cache_path.exists():
        return joblib.load(cache_path)

    url = f"https://www.retrosheet.org/gamelogs/gl{season}.zip"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; sports-research/1.0; "
            "retrosheet.org data consumer)"
        ),
        "Referer": "https://www.retrosheet.org/",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=45)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Retrosheet download failed for %d: %s", season, exc)
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            txt_files = [n for n in zf.namelist() if n.upper().endswith(".TXT")]
            if not txt_files:
                log.warning("No .TXT in Retrosheet zip for %d", season)
                return []
            raw = zf.read(txt_files[0]).decode("latin-1")
    except Exception as exc:
        log.warning("Failed to unzip Retrosheet for %d: %s", season, exc)
        return []

    games = _parse(raw, season)
    joblib.dump(games, cache_path)
    log.info("Retrosheet %d: cached %d games", season, len(games))
    return games


def _parse(content: str, season: int) -> list[dict]:
    games: list[dict] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip('"') for f in line.split(",")]
        if len(fields) < 11:
            continue
        try:
            away_code = fields[3]
            home_code = fields[6]
            away_runs = int(fields[9])
            home_runs = int(fields[10])
        except (ValueError, IndexError):
            continue

        if away_code not in RETRO_TO_NAME or home_code not in RETRO_TO_NAME:
            continue

        try:
            dt = datetime.strptime(fields[0], "%Y%m%d")
        except ValueError:
            continue

        games.append({
            "date":      dt.strftime("%Y-%m-%d"),
            "season":    season,
            "home_code": home_code,
            "away_code": away_code,
            "home_name": RETRO_TO_NAME[home_code],
            "away_name": RETRO_TO_NAME[away_code],
            "home_fg":   RETRO_TO_FG.get(home_code, home_code),
            "away_fg":   RETRO_TO_FG.get(away_code, away_code),
            "home_runs": home_runs,
            "away_runs": away_runs,
        })
    return games
