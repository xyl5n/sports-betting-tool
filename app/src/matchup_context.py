"""
matchup_context.py
==================
Backend data assembly for the game-detail (matchup) page's
"Confirmed Lineups" and "Team Context" sections.  It wires together the
clients that already exist in the app:

  - LineupClient          -> confirmation status + the confirmed batting
                             orders (id / name / order / position / bats).
  - BatterSplitsClient    -> probable lineup fallback (top batters by PA =
                             recent/season starters) when no confirmed
                             lineup is posted yet.
  - player_profile_client -> per-batter enrichment the two clients above
                             don't expose: handedness, position, season
                             AVG/OBP/SLG, and the vs-hand split
                             (PA / AVG / wOBA / ISO / K%).
  - UpsetCalculator       -> team gamelogs + streak + head-to-head, from
                             which L10 / home / away records are derived.

Everything degrades gracefully: a missing client, an unposted lineup, or a
slow MLB Stats API leaves the relevant field empty / dashed rather than
raising, so the page never crashes.  Assembled lineups are day-cached in
Supabase (Railway-compatible) since the per-batter enrichment is a handful
of API calls.
"""
from __future__ import annotations

import sys
from datetime import datetime
from typing import Optional

from . import db as _db
from . import player_profile_client as _ppc

# League-average baselines for the split arrows (kept in sync with the
# player-profile lineup section).
_LG_SPLIT = {"avg": 0.245, "woba": 0.315, "iso": 0.155, "k_pct": 22.5}


def _log(msg: str) -> None:
    print(f"MATCHUP-CTX: {msg}", flush=True, file=sys.stderr)


def _season_for(game_date: str) -> int:
    try:
        return int((game_date or "")[:4])
    except (TypeError, ValueError):
        return datetime.now().year


def _team_id(name: str) -> Optional[int]:
    try:
        from .upset import MLB_TEAM_IDS
        return MLB_TEAM_IDS.get(name)
    except Exception as exc:                                              # noqa: BLE001
        _log(f"team-id lookup failed for {name!r}: {exc}")
        return None


def split_arrow_dir(metric: str, value) -> Optional[bool]:
    """True = better than league average (for K%, lower is better),
    False = worse, None = unknown.  The page turns this into ▲/▼."""
    if not isinstance(value, (int, float)):
        return None
    base = _LG_SPLIT.get(metric)
    if base is None:
        return None
    return (value < base) if metric == "k_pct" else (value > base)


# ── FIX 1: Confirmed / probable lineups ─────────────────────────────────────

def get_matchup_lineups(home_team: str, away_team: str, game_date: str,
                        home_sp_hand, away_sp_hand) -> dict:
    """Assemble both teams' batting orders for the matchup page.

    *home_sp_hand* / *away_sp_hand* are the teams' OWN starters'
    handedness (0 = RHP, 1 = LHP).  A team's batters are split against the
    OPPOSING starter, so the home lineup uses ``away_sp_hand`` and vice
    versa.  Returns ``{"home": block, "away": block, "available": bool}``.
    """
    season = _season_for(game_date)
    home_id = _team_id(home_team)
    away_id = _team_id(away_team)

    confirmed_home: list[dict] = []
    confirmed_away: list[dict] = []
    try:
        from .lineup_client import get_lineup_client
        lc = get_lineup_client()
        lu = lc.get_lineups(home_team, away_team, game_date)
        lc.save()
        confirmed_home = lu.get("home") or []
        confirmed_away = lu.get("away") or []
    except Exception as exc:                                             # noqa: BLE001
        _log(f"lineup_client failed: {exc}")

    home_block = _build_team_block(
        home_id, confirmed_home, away_sp_hand, season, game_date,
    )
    away_block = _build_team_block(
        away_id, confirmed_away, home_sp_hand, season, game_date,
    )
    return {
        "home": home_block,
        "away": away_block,
        "available": bool(home_block.get("batters") or away_block.get("batters")),
    }


def _build_team_block(team_id: Optional[int], confirmed_players: list[dict],
                      opp_hand, season: int, game_date: str) -> dict:
    sit = "vl" if opp_hand == 1 else "vr"
    split_label = "vs LHP" if opp_hand == 1 else "vs RHP"
    empty = {"available": False, "confirmed": False, "source": "none",
             "split_label": split_label, "batters": []}

    cache_key = f"matchup_lineup_{game_date}_{team_id}_{sit}"
    try:
        row = _db.cache_get(cache_key)
        if (row and row.get("date") == game_date
                and (row.get("data") or {}).get("available")):
            return row["data"]
    except Exception:                                                    # noqa: BLE001
        pass

    confirmed = bool(confirmed_players)
    if confirmed:
        roster = [{
            "id":       p.get("id"),
            "name":     p.get("name") or "",
            "order":    p.get("order"),
            "position": p.get("position") or "",
            "bats":     p.get("bats") or "",
        } for p in confirmed_players if p.get("id")]
        source = "confirmed"
    else:
        # Probable lineup: BatterSplitsClient's top batters by PA stand in
        # for "recent games started" (it skips pitchers, sorts by PA desc).
        tops: list[dict] = []
        if team_id:
            try:
                from .batter_splits_client import get_batter_splits_client
                bsc = get_batter_splits_client()
                tops = bsc.get_top_batters(team_id, season)
                bsc.save()
            except Exception as exc:                                     # noqa: BLE001
                _log(f"batter_splits failed (team={team_id}): {exc}")
        roster = [{
            "id":       b.get("batter_id"),
            "name":     b.get("name") or "",
            "order":    i + 1,
            "position": "",
            "bats":     "",
        } for i, b in enumerate(tops) if b.get("batter_id")]
        source = "probable" if roster else "none"

    batters: list[dict] = []
    for r in roster:
        pid = r["id"]
        info: dict = {}
        ss: dict = {}
        spl: Optional[dict] = None
        try:
            info = _ppc.get_player_info(int(pid)) or {}
        except Exception:                                                # noqa: BLE001
            info = {}
        try:
            ss = _ppc.get_season_stats(int(pid), is_pitcher=False, season=season) or {}
        except Exception:                                                # noqa: BLE001
            ss = {}
        try:
            spl = _ppc._batter_split_vs_hand(int(pid), sit, season=season)
        except Exception:                                                # noqa: BLE001
            spl = None
        batters.append({
            "order":       r.get("order"),
            "name":        r.get("name") or info.get("name") or "",
            "position":    r.get("position") or info.get("position_code") or "",
            "hand":        r.get("bats") or info.get("bats") or "",
            "avg":         ss.get("avg"),
            "obp":         ss.get("obp"),
            "slg":         ss.get("slg"),
            "split_pa":    (spl or {}).get("pa"),
            "split_avg":   (spl or {}).get("avg"),
            "split_woba":  (spl or {}).get("woba"),
            "split_iso":   (spl or {}).get("iso"),
            "split_k_pct": (spl or {}).get("k_pct"),
        })

    out = {"available": bool(batters), "confirmed": confirmed,
           "source": source, "split_label": split_label, "batters": batters}
    if not batters:
        return empty
    try:
        _db.cache_set(cache_key, "mlb", game_date, out)
    except Exception:                                                    # noqa: BLE001
        pass
    return out


# ── FIX 2: Team context (L10 / streak / home / away / H2H) ──────────────────

def get_team_context(home_team: str, away_team: str, game_date: str) -> dict:
    """Per-team L10 record, current streak, season home + away records, and
    the season head-to-head record, from UpsetCalculator's gamelog/streak/
    H2H helpers.  Every field independently falls back to a dash."""
    season = _season_for(game_date)
    home_id = _team_id(home_team)
    away_id = _team_id(away_team)

    home_ctx = _dash_ctx()
    away_ctx = _dash_ctx()
    h2h = "—"
    available = False

    uc = None
    try:
        from .upset import UpsetCalculator
        uc = UpsetCalculator(season=season)
    except Exception as exc:                                             # noqa: BLE001
        _log(f"UpsetCalculator init failed: {exc}")
        return {"home": home_ctx, "away": away_ctx, "h2h": h2h, "available": False}

    home_games = _safe_gamelog(uc, home_id)
    away_games = _safe_gamelog(uc, away_id)
    home_ctx = _ctx_from_games(uc, home_games)
    away_ctx = _ctx_from_games(uc, away_games)
    available = bool(home_games or away_games)

    try:
        if home_id and away_id:
            hw, aw = uc._get_h2h(home_id, away_id)
            if (hw + aw) > 0:
                h2h = f"{hw}-{aw}"
    except Exception as exc:                                             # noqa: BLE001
        _log(f"h2h failed: {exc}")

    return {"home": home_ctx, "away": away_ctx, "h2h": h2h, "available": available}


def _dash_ctx() -> dict:
    return {"l10": "—", "streak": "—", "home": "—", "away": "—"}


def _safe_gamelog(uc, team_id: Optional[int]) -> list[dict]:
    if not team_id:
        return []
    try:
        return uc._get_team_gamelog(team_id) or []
    except Exception as exc:                                             # noqa: BLE001
        _log(f"gamelog failed (team={team_id}): {exc}")
        return []


def _ctx_from_games(uc, games: list[dict]) -> dict:
    out = _dash_ctx()
    if not games:
        return out
    try:
        last10 = games[-10:]
        w = sum(1 for g in last10 if g.get("won"))
        out["l10"] = f"{w}-{len(last10) - w}"
    except Exception:                                                    # noqa: BLE001
        pass
    try:
        s = uc._get_streak(games)
        out["streak"] = f"W{s}" if s > 0 else (f"L{-s}" if s < 0 else "—")
    except Exception:                                                    # noqa: BLE001
        pass
    try:
        hg = [g for g in games if g.get("is_home")]
        if hg:
            w = sum(1 for g in hg if g.get("won"))
            out["home"] = f"{w}-{len(hg) - w}"
    except Exception:                                                    # noqa: BLE001
        pass
    try:
        ag = [g for g in games if not g.get("is_home")]
        if ag:
            w = sum(1 for g in ag if g.get("won"))
            out["away"] = f"{w}-{len(ag) - w}"
    except Exception:                                                    # noqa: BLE001
        pass
    return out
