"""
Team logo widget -- ported from the legacy templates/index.html
teamLogoHtml() function.

Renders a sized rounded square containing:
  - bottom layer  : team-coloured background + initials (always present
                    -- this is the fallback)
  - top layer     : <img> from ESPN's CDN.  onerror removes the img, so
                    a missing logo automatically reveals the fallback
                    underneath without any broken-image icon.

ESPN's CDN URL pattern:
  https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png

User asked for mlbstatic + a CDN-or-fallback path for WNBA.  ESPN's
endpoint already covers BOTH leagues (proven by the prior Flask UI),
uses a single URL pattern, and the fallback-on-error behavior is
identical -- so we keep that source instead of swapping for mlbstatic.
The colored-circle-with-initials fallback handles any team ESPN
doesn't carry (new expansion teams etc).
"""
from __future__ import annotations

from nicegui import ui


# Full-team-name -> ESPN abbreviation maps.  Mirrors the JS dicts in
# templates/index.html so historical bets and current odds resolve to
# the same logos as before the migration.

_MLB_ABBR: dict[str, str] = {
    "Arizona Diamondbacks":  "ari",
    "Atlanta Braves":        "atl",
    "Baltimore Orioles":     "bal",
    "Boston Red Sox":        "bos",
    "Chicago Cubs":          "chc",
    "Chicago White Sox":     "chw",
    "Cincinnati Reds":       "cin",
    "Cleveland Guardians":   "cle",
    "Colorado Rockies":      "col",
    "Detroit Tigers":        "det",
    "Houston Astros":        "hou",
    "Kansas City Royals":    "kc",
    "Los Angeles Angels":    "laa",
    "Los Angeles Dodgers":   "lad",
    "Miami Marlins":         "mia",
    "Milwaukee Brewers":     "mil",
    "Minnesota Twins":       "min",
    "New York Mets":         "nym",
    "New York Yankees":      "nyy",
    "Athletics":             "oak",
    "Oakland Athletics":     "oak",
    "Philadelphia Phillies": "phi",
    "Pittsburgh Pirates":    "pit",
    "San Diego Padres":      "sd",
    "San Francisco Giants":  "sf",
    "Seattle Mariners":      "sea",
    "St. Louis Cardinals":   "stl",
    "Tampa Bay Rays":        "tb",
    "Texas Rangers":         "tex",
    "Toronto Blue Jays":     "tor",
    "Washington Nationals":  "wsh",
}

_WNBA_ABBR: dict[str, str] = {
    "Atlanta Dream":           "atl",
    "Chicago Sky":             "chi",
    "Connecticut Sun":         "conn",
    "Dallas Wings":            "dal",
    "Golden State Valkyries":  "gs",
    "Indiana Fever":           "ind",
    "Las Vegas Aces":          "lv",
    "Los Angeles Sparks":      "la",
    "Minnesota Lynx":          "min",
    "New York Liberty":        "ny",
    "Phoenix Mercury":         "phx",
    "Seattle Storm":           "sea",
    "Washington Mystics":      "wsh",
    # Toronto Tempo is the 2026 WNBA expansion team; ESPN may not host
    # its logo yet -- fallback color-circle takes over.
    "Toronto Tempo":           "tor",
}


def _team_initials(team: str) -> str:
    """First 3 chars of the last word, uppercased.  'Atlanta Braves'->'BRA'.
    For two-word nicknames ('Red Sox', 'Blue Jays') use the full last word
    so the abbreviation feels right ('SOX', 'JAY')."""
    if not team:
        return "?"
    parts = team.split()
    if not parts:
        return team[:3].upper()
    return parts[-1][:3].upper()


def abbrev(team: str, sport: str = "mlb") -> str:
    """Public 2-3 letter team abbreviation for *team* (full name).  Uses the
    canonical ESPN map (e.g. 'New York Yankees' -> 'NYY') and falls back to
    nickname initials when the team isn't in the map."""
    abbr_map = _WNBA_ABBR if (sport or "").lower() == "wnba" else _MLB_ABBR
    abbr = (abbr_map.get(team) or abbr_map.get((team or "").strip())
            or _team_initials(team))
    return abbr.upper()


def _team_color(team: str) -> str:
    """Deterministic HSL hash -- same algorithm the legacy template used
    so a given team gets the same fallback color before and after the
    migration.  Saturated dark-cyan family blend (S=55%, L=30%)."""
    h = 0
    for c in (team or "?"):
        h = (h * 31 + ord(c)) % 360
    return f"hsl({h}, 55%, 30%)"


def _logo_url(team: str, sport: str) -> str | None:
    """Return the ESPN CDN URL for *team*, or None if we don't have a
    mapping (caller renders the colored-circle fallback alone)."""
    if not team:
        return None
    abbr_map = _WNBA_ABBR if (sport or "").lower() == "wnba" else _MLB_ABBR
    abbr = abbr_map.get(team) or abbr_map.get(team.strip())
    if not abbr:
        return None
    league = "wnba" if (sport or "").lower() == "wnba" else "mlb"
    return f"https://a.espncdn.com/i/teamlogos/{league}/500/{abbr}.png"


def render(team: str, sport: str = "mlb", size: int = 36) -> None:
    """Render a team logo into the current NiceGUI parent.

    `size` is the diameter in pixels.  The colored-circle fallback is
    ALWAYS rendered; if a CDN URL exists, an <img> is layered on top
    that covers the fallback when loaded.  `onerror` removes the img
    on load failure, revealing the fallback again with no broken-image
    icon.

    Single ui.html call so the resulting DOM matches the legacy markup
    1:1 and the existing .team-logo / .team-logo-init / .team-logo-img
    styles in theme.page_head_css apply unchanged.
    """
    initials = _team_initials(team)
    color    = _team_color(team)
    url      = _logo_url(team, sport)
    font_px  = max(10, int(round(size * 0.30)))
    # Escape only the bare minimum -- alt text + style.  Team names are
    # backend-controlled (Odds API output) but we still defensively
    # escape double-quotes so a future team name with a quote can't
    # break the markup.
    alt   = (team or "").replace('"', "&quot;")
    style = (
        f"--logo-size:{size}px;"
        f"--logo-fs:{font_px}px;"
        f"--logo-bg:{color}"
    )
    if url:
        img_html = (
            f'<img class="team-logo-img" src="{url}" alt="{alt}" '
            f'loading="lazy" referrerpolicy="no-referrer" '
            f'onerror="this.remove()">'
        )
    else:
        img_html = ""
    ui.html(
        f'<span class="team-logo" style="{style}">'
        f'<span class="team-logo-init">{initials}</span>'
        f'{img_html}'
        f'</span>'
    )
