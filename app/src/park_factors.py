"""
MLB ballpark run factors (user-calibrated values, normalized so 1.000 = league average).
>1 = hitter-friendly (more runs), <1 = pitcher-friendly (fewer runs).
The run_factor is used directly by the totals prediction model as a multiplier.

Live override: get_park_factors() attempts to pull current-season values
from pybaseball's park_factors() (FanGraphs source, 100-base) before
falling back to the static table below.  pybaseball returns 100-base
values (100 = league average) which we convert to 1.000-base for
model compatibility.  Failures (pybaseball not installed, FanGraphs
unreachable, parse error) are silent -- the static table below is
always available as a backstop.
"""
from __future__ import annotations

import time
from typing import Optional

# In-process memo so we only pay the pybaseball / FanGraphs round-trip
# once per process (per season).  Each entry is keyed by season int.
_LIVE_FACTOR_CACHE: dict[int, dict[str, tuple[float, float]]] = {}


def _fetch_pybaseball_park_factors(season: int) -> dict[str, tuple[float, float]]:
    """Return {team_name: (run_factor, hr_factor)} in 1.000-base from
    pybaseball.park_factors(season).  Returns {} on any failure.

    pybaseball.park_factors hits FanGraphs which publishes 100-base
    factors (100 = league average, 110 = 10% above average for runs).
    We divide by 100 to keep the rest of the pipeline on the same
    1.000-base scale the totals model expects.
    """
    cached = _LIVE_FACTOR_CACHE.get(season)
    if cached is not None:
        return cached
    try:
        import pybaseball  # noqa: PLC0415
    except ImportError:
        _LIVE_FACTOR_CACHE[season] = {}
        return {}
    try:
        df = pybaseball.park_factors(season)
    except Exception as exc:                                              # noqa: BLE001
        print(f"  [park_factors] pybaseball.park_factors({season}) "
              f"unavailable: {exc}")
        _LIVE_FACTOR_CACHE[season] = {}
        return {}

    out: dict[str, tuple[float, float]] = {}
    try:
        # FanGraphs columns vary by release.  Probe a few of the
        # commonly seen names and tolerate either a Basic column or
        # a 1yr / multi-year average.
        team_col = next(
            (c for c in ("Team", "Home Team", "team", "TeamName") if c in df.columns),
            None,
        )
        run_col = next(
            (c for c in ("Basic", "1yr", "5yr", "Park Factor", "PF") if c in df.columns),
            None,
        )
        hr_col = next(
            (c for c in ("HR as L", "HR", "HRPF", "hr_park_factor") if c in df.columns),
            None,
        )
        if not team_col or not run_col:
            _LIVE_FACTOR_CACHE[season] = {}
            return {}
        for _, row in df.iterrows():
            try:
                team = str(row[team_col])
                run = float(row[run_col]) / 100.0
                hr  = float(row[hr_col]) / 100.0 if hr_col else run
                if team and run > 0:
                    out[team] = (run, hr)
            except (TypeError, ValueError):
                continue
    except Exception as exc:                                              # noqa: BLE001
        print(f"  [park_factors] failed to parse pybaseball frame: {exc}")
        _LIVE_FACTOR_CACHE[season] = {}
        return {}

    _LIVE_FACTOR_CACHE[season] = out
    if out:
        print(f"  [park_factors] loaded {len(out)} parks from pybaseball "
              f"(season={season}) [SOURCE: pybaseball / FanGraphs]")
    return out


# (run_factor, hr_factor) — run_factor drives the totals model multiplier
_PARK: dict[str, tuple[float, float]] = {
    "Colorado Rockies":       (1.38, 1.40),   # Coors Field — altitude, extreme hitter park
    "Cincinnati Reds":        (1.12, 1.11),   # Great American Ball Park
    "Boston Red Sox":         (1.10, 0.96),   # Fenway Park — lots of hits, fewer HRs
    "Chicago Cubs":           (1.08, 1.07),   # Wrigley Field
    "Texas Rangers":          (1.07, 1.08),   # Globe Life Field
    "Houston Astros":         (1.06, 0.97),   # Minute Maid Park — Crawford Boxes
    "Atlanta Braves":         (1.05, 1.04),   # Truist Park
    "Milwaukee Brewers":      (1.05, 1.03),   # American Family Field
    "New York Yankees":       (1.04, 1.08),   # Yankee Stadium — short porch in right
    "Philadelphia Phillies":  (1.04, 1.06),   # Citizens Bank Park
    "Baltimore Orioles":      (1.03, 1.02),   # Camden Yards
    "Chicago White Sox":      (1.03, 1.04),   # Guaranteed Rate Field
    "Cleveland Guardians":    (1.02, 0.95),   # Progressive Field
    "Los Angeles Angels":     (1.02, 1.02),   # Angel Stadium
    "Arizona Diamondbacks":   (1.01, 1.03),   # Chase Field
    "Washington Nationals":   (1.00, 1.00),   # Nationals Park — neutral reference
    "Los Angeles Dodgers":    (0.99, 0.93),   # Dodger Stadium
    "New York Mets":          (0.99, 0.96),   # Citi Field
    "St. Louis Cardinals":    (0.98, 0.98),   # Busch Stadium
    "Pittsburgh Pirates":     (0.98, 0.97),   # PNC Park
    "Minnesota Twins":        (0.97, 0.99),   # Target Field
    "Detroit Tigers":         (0.97, 0.94),   # Comerica Park
    "Seattle Mariners":       (0.96, 0.93),   # T-Mobile Park
    "Oakland Athletics":      (0.96, 0.91),   # Oakland Coliseum
    "Tampa Bay Rays":         (0.95, 0.94),   # Tropicana Field (dome)
    "Toronto Blue Jays":      (0.95, 0.97),   # Rogers Centre (dome)
    "Kansas City Royals":     (0.94, 0.95),   # Kauffman Stadium
    "Miami Marlins":          (0.94, 0.89),   # loanDepot Park (dome)
    "San Diego Padres":       (0.93, 0.91),   # Petco Park
    "San Francisco Giants":   (0.92, 0.88),   # Oracle Park — pitcher-friendly
}

_NEUTRAL = (1.000, 1.000)


def _lookup_in_table(
    home_team: str,
    table: dict[str, tuple[float, float]],
) -> Optional[tuple[float, float]]:
    """Exact / substring / token-overlap match for `home_team` in
    `table`.  Returns None when nothing scored above 0 tokens.
    Shared between the live pybaseball table and the static fallback
    so both go through identical fuzzy matching."""
    if not table:
        return None
    if home_team in table:
        return table[home_team]
    home_lower = home_team.lower()
    for team, factors in table.items():
        if team.lower() in home_lower or home_lower in team.lower():
            return factors
    tokens = set(home_lower.split())
    best, best_n = None, 0
    for team, factors in table.items():
        n = len(tokens & set(team.lower().split()))
        if n > best_n:
            best, best_n = factors, n
    return best


def get_park_factors(
    home_team: str,
    season: Optional[int] = None,
) -> tuple[float, float]:
    """Return (run_factor, hr_factor) for the home team's ballpark in
    1.000-base.  Tries pybaseball.park_factors(season) first when a
    season is supplied; falls back to the hand-calibrated static
    table when pybaseball is unavailable or the team can't be found
    in the live data."""
    if season is not None:
        live = _fetch_pybaseball_park_factors(int(season))
        hit = _lookup_in_table(home_team, live)
        if hit is not None:
            return hit
    hit = _lookup_in_table(home_team, _PARK)
    return hit if hit is not None else _NEUTRAL
