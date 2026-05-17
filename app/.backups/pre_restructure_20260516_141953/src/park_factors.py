"""
MLB ballpark run factors (user-calibrated values, normalized so 1.000 = league average).
>1 = hitter-friendly (more runs), <1 = pitcher-friendly (fewer runs).
The run_factor is used directly by the totals prediction model as a multiplier.
"""

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


def get_park_factors(home_team: str) -> tuple[float, float]:
    """Return (run_factor, hr_factor) for the home team's ballpark."""
    if home_team in _PARK:
        return _PARK[home_team]
    # Fuzzy: try matching on last one or two words
    home_lower = home_team.lower()
    for team, factors in _PARK.items():
        if team.lower() in home_lower or home_lower in team.lower():
            return factors
    # Token overlap fallback
    tokens = set(home_lower.split())
    best, best_n = _NEUTRAL, 0
    for team, factors in _PARK.items():
        n = len(tokens & set(team.lower().split()))
        if n > best_n:
            best, best_n = factors, n
    return best
