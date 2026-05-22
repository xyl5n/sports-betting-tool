"""
props_settlement.py
===================
Settle open MLB player-prop picks against the actual box score.

For each open row in props_pitcher_picks_history.json /
props_batter_picks_history.json where `result` is null, this module:

  1. Looks up the game's gamePk on statsapi.mlb.com.
  2. Pulls the full box score from
     statsapi.mlb.com/api/v1/game/{gamePk}/boxscore.
  3. Finds the player line (matched by name) and reads the stat that
     corresponds to the prop market.
  4. Compares actual stat vs prop line + side ("Over" / "Under") and
     records "win" / "loss" / "push".
  5. Calculates profit / loss at standard -110 juice unless the row
     stamped its own `american_odds` at placement.
  6. Writes the updated history file back to disk AND syncs the row
     to Supabase via cache_set so the per-bucket record survives a
     Railway redeploy.

Every step emits a PROPS-SETTLE stderr line so Railway captures the
full audit trail.  Greppable end-to-end: `PROPS-SETTLE`.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .props_model import PITCHER_HISTORY_PATH, BATTER_HISTORY_PATH

_BASE = "https://statsapi.mlb.com/api/v1"
_ET   = ZoneInfo("America/New_York")

# Standard prop juice -- most books offer player props at -110 / -110.
# The row carries `american_odds` when the placement code stamped it;
# falls back to this when the row predates the field.
_STANDARD_PROP_JUICE = -110


# ── Logging ─────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"PROPS-SETTLE: {msg}", flush=True, file=sys.stderr)


# ── Stat extraction ─────────────────────────────────────────────────────────

# Maps each market key to (player_section, stat_key).  player_section
# tells us which half of the box score's `players` dict to walk
# (pitchers live under teams[<side>].players[<id>].stats.pitching;
# batters under .stats.batting).  stat_key is the field inside that
# stat dict.  MLB Stats API field names are intentionally cryptic --
# the audit trail in the box score docs is the only source of truth.
_MARKET_TO_STAT: dict[str, tuple[str, str]] = {
    "pitcher_strikeouts":     ("pitching", "strikeOuts"),
    "pitcher_outs":           ("pitching", "outs"),
    "pitcher_hits_allowed":   ("pitching", "hits"),
    "pitcher_walks":          ("pitching", "baseOnBalls"),
    "pitcher_earned_runs":    ("pitching", "earnedRuns"),
    "batter_hits":            ("batting",  "hits"),
    "batter_total_bases":     ("batting",  "totalBases"),
    "batter_home_runs":       ("batting",  "homeRuns"),
    "batter_rbis":            ("batting",  "rbi"),
    "batter_walks":           ("batting",  "baseOnBalls"),
    "batter_runs_scored":     ("batting",  "runs"),
    "batter_strikeouts":      ("batting",  "strikeOuts"),
    "batter_stolen_bases":    ("batting",  "stolenBases"),
}


def _fetch(url: str, timeout: int = 10) -> Optional[dict]:
    """JSON GET with logging.  Returns None on any failure."""
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "sports-betting-ai/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  GET {url} -> HTTP {resp.status} ({ms}ms)")
        return json.loads(body)
    except urllib.error.HTTPError as exc:
        _log(f"  GET {url} -> HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        _log(f"  GET {url} -> network error {exc.reason!r}")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"  GET {url} -> {type(exc).__name__}: {exc}")
    return None


def _norm(name: str) -> str:
    """Aggressive name normalize for cross-source matching.  MLB Stats
    API returns "fullName" with the player's official spelling; The
    Odds API sometimes shortens it ("Tarik Skubal" vs "T. Skubal").
    Lowercased + alnum-only, then we substring-match in either
    direction so partials still link."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _find_player_stat(
    box: dict, player_name: str, section: str, stat_key: str,
) -> Optional[float]:
    """Walk the box-score teams[home|away].players[id].stats[section]
    structure for *player_name* and return *stat_key* as a float.
    None when the player or stat is missing."""
    needle = _norm(player_name)
    for side in ("home", "away"):
        team_block = (box.get("teams") or {}).get(side) or {}
        for _pid, pdata in (team_block.get("players") or {}).items():
            person = pdata.get("person") or {}
            full = person.get("fullName") or ""
            box_key = _norm(full)
            # Match if either name contains the other -- handles abbreviated
            # first names ("J. Smith" vs "John Smith") in either direction.
            if not box_key or not needle:
                continue
            if not (needle in box_key or box_key in needle):
                continue
            stats = (pdata.get("stats") or {}).get(section) or {}
            raw = stats.get(stat_key)
            if raw is None or raw == "":
                return None
            try:
                # `outs` can be returned as a string like "18.0"; cast
                # to float so the > comparison against a 0.5 line works.
                return float(raw)
            except (TypeError, ValueError):
                # `inningsPitched` etc. use "5.2" notation -- not
                # currently surfaced here but tolerate via str fallback.
                try:
                    return float(str(raw).replace(".2", ".67").replace(".1", ".33"))
                except (TypeError, ValueError):
                    return None
    return None


def _resolve_game_pk(prop_row: dict) -> Optional[int]:
    """Best-effort gamePk lookup.  Three paths:
       1. If the row stamped `game_pk` at placement, use it.
       2. If it stamped a date + home/away team names, hit
          /api/v1/schedule?date=... and find the matching game.
       3. Otherwise return None so the caller skips the row.
    """
    pk = prop_row.get("game_pk")
    if pk:
        try:
            return int(pk)
        except (TypeError, ValueError):
            pass

    commence_time = prop_row.get("commence_time") or ""
    home = (prop_row.get("home_team") or "").lower()
    away = (prop_row.get("away_team") or "").lower()
    if not commence_time:
        return None
    try:
        dt = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
        date_str = dt.astimezone(_ET).date().isoformat()
    except Exception:                                                     # noqa: BLE001
        return None

    sched = _fetch(
        f"{_BASE}/schedule?sportId=1&date={date_str}"
    )
    if not sched:
        return None
    for day in (sched.get("dates") or []):
        for g in (day.get("games") or []):
            teams = g.get("teams") or {}
            h_name = (((teams.get("home") or {}).get("team") or {}).get("name") or "").lower()
            a_name = (((teams.get("away") or {}).get("team") or {}).get("name") or "").lower()
            if home and away and h_name and a_name:
                if home in h_name and away in a_name:
                    return int(g.get("gamePk") or 0) or None
    return None


# ── P&L math ────────────────────────────────────────────────────────────────

def _american_to_decimal(american: int) -> float:
    """+150 -> 2.5, -110 -> 1.909.  Used for win-side payout math."""
    try:
        v = int(american)
    except (TypeError, ValueError):
        return 1.0
    return (1 + 100 / abs(v)) if v < 0 else (1 + v / 100)


def _settle_pnl(stake: float, american_odds: int, result: str) -> float:
    """Standard sportsbook math:
       win  -> +stake * (decimal - 1)
       loss -> -stake
       push -> 0
    """
    if result == "win":
        return round(stake * (_american_to_decimal(american_odds) - 1), 2)
    if result == "loss":
        return -round(stake, 2)
    return 0.0


# ── History I/O ─────────────────────────────────────────────────────────────

def _read_history_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                                              # noqa: BLE001
        _log(f"history read failed {path}: {exc}")
        return []
    return raw.get("picks") or []


def _write_history_file(path: Path, picks: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"picks": picks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:                                              # noqa: BLE001
        _log(f"history write failed {path}: {exc}")


def _sync_history_to_supabase(bucket: str, picks: list[dict]) -> None:
    """Mirror the per-bucket history file to Supabase app_cache so a
    Railway redeploy can't wipe today's settled records.  Same shape
    as the props_client cache rows: key={props_{bucket}_history},
    payload={picks: [...]}."""
    try:
        from . import db as _db
        if not _db.is_supabase():
            return
        from datetime import date
        _db.cache_set(
            f"props_{bucket}_history",
            None,
            date.today().isoformat(),
            {"picks": picks},
        )
        _log(
            f"synced props_{bucket}_history to Supabase "
            f"({len(picks)} pick(s))"
        )
    except Exception as exc:                                              # noqa: BLE001
        _log(f"supabase sync failed for {bucket}: {exc}")


# ── Public entry point ─────────────────────────────────────────────────────

def settle_open_prop_picks() -> dict:
    """Walk both history files, settle every row with result=None
    whose game has a completed box score, and persist.

    Returns a summary {pitcher: {settled, wins, losses, pushes},
                       batter: same shape}.  All counts are zero when
    no history exists or all rows are already settled.
    """
    summary: dict = {}
    for bucket, path in (
        ("pitcher", PITCHER_HISTORY_PATH),
        ("batter",  BATTER_HISTORY_PATH),
    ):
        rows = _read_history_file(path)
        if not rows:
            summary[bucket] = {"settled": 0, "wins": 0, "losses": 0, "pushes": 0}
            continue

        # Box-score cache so multiple props on the same game share
        # one HTTP fetch.
        boxscore_cache: dict[int, dict] = {}

        n_settled = n_w = n_l = n_p = 0
        for row in rows:
            if (row.get("result") or "").lower() in ("win", "loss", "push"):
                continue

            market = (row.get("market") or "").strip()
            stat_route = _MARKET_TO_STAT.get(market)
            if stat_route is None:
                _log(
                    f"  skip {bucket}/{row.get('player_name')!r} {market}: "
                    f"no stat mapping"
                )
                continue
            section, stat_key = stat_route

            pk = _resolve_game_pk(row)
            if not pk:
                _log(
                    f"  skip {bucket}/{row.get('player_name')!r} {market}: "
                    f"no gamePk resolved (game may not be final yet)"
                )
                continue

            box = boxscore_cache.get(pk)
            if box is None:
                box = _fetch(f"{_BASE}/game/{pk}/boxscore") or {}
                boxscore_cache[pk] = box
            if not box:
                _log(
                    f"  skip {bucket}/{row.get('player_name')!r} {market}: "
                    f"no boxscore returned for gamePk={pk}"
                )
                continue

            stat_value = _find_player_stat(
                box, row.get("player_name") or "", section, stat_key,
            )
            if stat_value is None:
                _log(
                    f"  skip {bucket}/{row.get('player_name')!r} {market}: "
                    f"player or stat not in boxscore (game may still be live)"
                )
                continue

            line = row.get("line")
            side = (row.get("side") or "Over").strip().title()
            try:
                line_f = float(line)
            except (TypeError, ValueError):
                _log(
                    f"  skip {bucket}/{row.get('player_name')!r} {market}: "
                    f"unparseable line {line!r}"
                )
                continue

            if stat_value > line_f:
                result = "win"  if side == "Over"  else "loss"
            elif stat_value < line_f:
                result = "loss" if side == "Over"  else "win"
            else:
                result = "push"

            odds  = int(row.get("american_odds") or _STANDARD_PROP_JUICE)
            stake = float(row.get("stake") or 1.0)
            pnl   = _settle_pnl(stake, odds, result)

            row["result"]      = result
            row["actual_stat"] = stat_value
            row["pnl"]         = pnl
            row["settled_at"]  = datetime.now(_ET).isoformat()
            row["game_pk"]     = pk

            n_settled += 1
            if   result == "win":  n_w += 1
            elif result == "loss": n_l += 1
            else:                  n_p += 1

            _log(
                f"  {bucket} {row.get('player_name')!r} {market} "
                f"line={line_f} side={side} actual={stat_value} "
                f"-> {result.upper()}  P&L ${pnl:+.2f}  gamePk={pk}"
            )

        if n_settled:
            _write_history_file(path, rows)
            _sync_history_to_supabase(bucket, rows)

        summary[bucket] = {
            "settled": n_settled,
            "wins":    n_w,
            "losses":  n_l,
            "pushes":  n_p,
        }
        _log(
            f"BUCKET {bucket}: settled={n_settled}  "
            f"W={n_w}  L={n_l}  P={n_p}"
        )

    _log(f"COMPLETE summary={summary}")
    return summary
