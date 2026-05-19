"""
Live-score data layer + per-card score block renderer.

The Flask backend already exposes proxies that talk to:

  /api/mlb/schedule?date=YYYY-MM-DD&hydrate=linescore     (MLB Stats API)
  /api/wnba/schedule?date=YYYY-MM-DD&hydrate=linescore    (ESPN, normalized)

Both return the same shape (the WNBA proxy reshapes ESPN into the MLB
shape) so this module treats them uniformly:

  {
    "dates": [
      {
        "games": [
          {
            "gamePk":       int,
            "teams": {
              "home": {"team": {"name": str}},
              "away": {"team": {"name": str}}
            },
            "status": {"abstractGameState": "Live"|"Final"|"Preview"},
            "linescore": {
              "currentInning":         int,
              "currentInningOrdinal":  str,
              "inningHalf":            "Top"|"Bottom"|None,
              "isTopInning":           bool,           (MLB only)
              "balls":                 int,            (MLB only)
              "strikes":               int,            (MLB only)
              "outs":                  int,            (MLB only)
              "displayClock":          str,            (WNBA only)
              "teams": {
                "home": {"runs": int},
                "away": {"runs": int}
              }
            }
          },
          ...
        ]
      }
    ]
  }

Two module-level dicts (one per sport) cache the most recent fetch
keyed by gamePk + a normalized team-name pair as a fallback key.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from nicegui import ui

from . import theme as t


_ET = ZoneInfo("America/New_York")

# Per-sport, module-level live cache.  Keys:
#   - int gamePk
#   - "AWAY|HOME" lowercased+stripped team-name fallback key
# Value: the raw game dict from the response (already MLB-shaped for WNBA).
_LIVE: dict[str, dict] = {"mlb": {}, "wnba": {}}
_LIVE_LOCK = threading.Lock()
_LIVE_TS:   dict[str, float] = {"mlb": 0.0, "wnba": 0.0}     # last successful fetch


# ── Fetch ──────────────────────────────────────────────────────────────────

def fetch_live(backend, sport: str) -> int:
    """Pull today's live schedule via the in-process Flask test client and
    populate the live cache for *sport*.  Returns the count of games stored.

    Safe to call repeatedly -- the backend itself has a 30-second TTL on
    linescore responses, so a 60s page poller barely touches the wire.

    Errors are swallowed: a missing schedule or network blip leaves the
    cache as-is.  Callers (the ui.timer) should ignore the return value.
    """
    sport = (sport or "mlb").lower()
    if sport not in _LIVE:
        return 0
    today = datetime.now(_ET).date().isoformat()
    path  = f"/api/{sport}/schedule?date={today}&hydrate=linescore"
    try:
        client = backend.app.test_client()
        resp   = client.get(path)
        if resp.status_code >= 400:
            return 0
        data = resp.get_json(force=True, silent=True) or {}
    except Exception as exc:                                              # noqa: BLE001
        print(f"[live_score] fetch_live({sport}) error: "
              f"{type(exc).__name__}: {exc}",
              flush=True, file=sys.stderr)
        return 0

    games_by_key: dict[str, dict] = {}
    for date_block in (data.get("dates") or []):
        for g in (date_block.get("games") or []):
            try:
                gpk = int(g.get("gamePk") or 0)
                if gpk:
                    games_by_key[str(gpk)] = g
                # team-name fallback key (lower + stripped, away|home)
                teams = g.get("teams") or {}
                away = ((teams.get("away") or {}).get("team") or {}).get("name", "")
                home = ((teams.get("home") or {}).get("team") or {}).get("name", "")
                if away and home:
                    games_by_key[_team_pair_key(away, home)] = g
            except Exception:                                             # noqa: BLE001
                continue

    with _LIVE_LOCK:
        _LIVE[sport] = games_by_key
        _LIVE_TS[sport] = datetime.now().timestamp()
    return len(games_by_key)


def lookup(sport: str, game_id: Optional[str | int],
           away_team: str, home_team: str) -> Optional[dict]:
    """Best-effort lookup.  Tries gamePk first, then the team-name pair.
    Returns the raw stats-API game dict, or None when no match."""
    sport = (sport or "mlb").lower()
    cache = _LIVE.get(sport, {})
    if not cache:
        return None
    # gamePk path -- works when the Odds API game id == MLB gamePk.  It
    # usually doesn't (Odds API ids are hashed), so this rarely hits; the
    # team-name fallback below is the real workhorse.
    if game_id is not None:
        hit = cache.get(str(game_id))
        if hit:
            return hit
    return cache.get(_team_pair_key(away_team, home_team))


def _team_pair_key(away: str, home: str) -> str:
    return f"{(away or '').strip().lower()}|{(home or '').strip().lower()}"


# ── Render: per-card score block ──────────────────────────────────────────

def state_of(live: Optional[dict]) -> str:
    """One of 'live', 'final', 'scheduled' from the stats-API game dict.
    Returns 'scheduled' when live is None."""
    if not live:
        return "scheduled"
    s = ((live.get("status") or {}).get("abstractGameState") or "").lower()
    if s == "live":
        return "live"
    if s == "final":
        return "final"
    return "scheduled"


def render_score_block(live: dict, sport: str) -> None:
    """Render the big-score area for an in-progress or completed game.

    Layout (centered, large numerals):

        AWAY  5   - 3  HOME
              ↑ 5th
              B 2 · S 1 · O 1     (MLB live only)
              4:23 · 2nd          (WNBA live only)

        AWAY  5   - 3  HOME       (final games, no detail line)
              FINAL

    Caller is responsible for the matchup row above (logos + team
    names + odds).  This block is the score + status detail line(s).
    """
    state = state_of(live)
    ls = (live or {}).get("linescore") or {}
    teams_ls = ls.get("teams") or {}
    home_score = ((teams_ls.get("home") or {}).get("runs"))
    away_score = ((teams_ls.get("away") or {}).get("runs"))

    # Center the score block; matchup row above already shows team names.
    with ui.column().classes("items-center w-full").style(
        f"gap: 4px; padding: 4px 0;"
    ):
        # Score row -- big bold numbers separated by an em-dash.
        with ui.row().classes("items-center").style("gap: 14px;"):
            ui.label(_score_text(away_score)).style(
                f"font-size: 32px; font-weight: 800; color: {t.TEXT}; "
                f"font-family: monospace; line-height: 1;"
            )
            ui.label("–").style(
                f"font-size: 24px; color: {t.TEXT_DIM2}; line-height: 1;"
            )
            ui.label(_score_text(home_score)).style(
                f"font-size: 32px; font-weight: 800; color: {t.TEXT}; "
                f"font-family: monospace; line-height: 1;"
            )

        # Detail line below the score: inning / count / quarter / "Final".
        detail = _detail_line(state, ls, sport)
        if detail:
            ui.html(detail)


def _score_text(v) -> str:
    if isinstance(v, (int, float)):
        return str(int(v))
    return "0"


def _detail_line(state: str, ls: dict, sport: str) -> str:
    """Return a raw HTML string for the detail line below the score.  Using
    ui.html() keeps the dot+arrow inline glyphs compact without nesting
    extra ui.label calls."""
    text_dim2 = t.TEXT_DIM2
    if state == "final":
        return (
            f'<div style="font-size:11px;font-weight:800;letter-spacing:.6px;'
            f'color:{t.TEXT_DIM};text-align:center;">FINAL</div>'
        )

    if state != "live":
        return ""

    sport = (sport or "mlb").lower()
    if sport == "mlb":
        # ↑5th  ·  B 2  ·  S 1  ·  O 1
        ordinal = ls.get("currentInningOrdinal") or ""
        is_top  = bool(ls.get("isTopInning"))
        if not ordinal:
            inn = int(ls.get("currentInning") or 0)
            ordinal = str(inn) if inn else ""
        half_arrow = ("↑" if is_top else "↓") if ordinal else ""
        balls   = ls.get("balls")
        strikes = ls.get("strikes")
        outs    = ls.get("outs")

        pieces: list[str] = []
        if ordinal:
            pieces.append(
                f'<span style="color:{t.TEXT};font-weight:700;">{half_arrow} {ordinal}</span>'
            )
        if isinstance(balls, int) and isinstance(strikes, int):
            pieces.append(
                f'<span style="color:{text_dim2};">B</span> '
                f'<span style="color:{t.TEXT};">{balls}</span>'
            )
            pieces.append(
                f'<span style="color:{text_dim2};">S</span> '
                f'<span style="color:{t.TEXT};">{strikes}</span>'
            )
        if isinstance(outs, int):
            pieces.append(
                f'<span style="color:{text_dim2};">O</span> '
                f'<span style="color:{t.TEXT};">{outs}</span>'
            )
        return (
            f'<div class="theme-mono" '
            f'style="font-size:12px;text-align:center;letter-spacing:.4px;">'
            + ' &middot; '.join(pieces) +
            f'</div>'
        )

    # WNBA
    clock = ls.get("displayClock") or ""
    period_ordinal = ls.get("currentInningOrdinal") or ""
    pieces: list[str] = []
    if clock:
        pieces.append(f'<span style="color:{t.TEXT};font-weight:700;">{clock}</span>')
    if period_ordinal:
        pieces.append(f'<span style="color:{t.TEXT_DIM};">{period_ordinal}</span>')
    if not pieces:
        return ""
    return (
        f'<div class="theme-mono" '
        f'style="font-size:12px;text-align:center;letter-spacing:.4px;">'
        + ' &middot; '.join(pieces) +
        f'</div>'
    )


# ── Live dot (used by the meta row) ────────────────────────────────────────

def render_live_dot() -> None:
    """Tiny pulsing green dot.  Pair with the LIVE label in a card's meta
    row.  CSS animation lives in components/theme.page_head_css."""
    ui.html(
        f'<span class="live-dot" '
        f'style="background:{t.POS};"></span>'
    )
