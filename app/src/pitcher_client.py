"""
Fetches probable MLB starting pitchers and their season stats via the
free MLB Stats API (statsapi.mlb.com — no key required).

Pipeline per game:
  1.  /schedule?sportId=1&hydrate=probablePitcher
        -> game's probable starter id + handedness + days-rest note
  2.  /people/{pid}/stats?stats=season&group=pitching&season={year}
        -> ERA, WHIP, W-L, raw counts used to compute K/9 + BB/9
  3.  /people/{pid}/stats?stats=homeAndAway&group=pitching&season={year}
        -> separate home and road ERA splits
  4.  /people/{pid}/stats?stats=gameLog&group=pitching&season={year}
        -> last-3-starts ERA (sum earnedRuns * 9 / sum innings)
  5.  /people/{pid}
        -> full display name
  6.  /teams/{team_id}
        -> three-letter abbreviation (DET, NYY, etc.)

Every step logs its label, URL, HTTP status, and a one-line summary of
what it parsed.  Failures still fall through to neutral defaults so the
model + UI don't crash, but the reason shows up in the deploy log
instead of disappearing into the silent _fetch_url default.

Usage:
    client = PitcherClient()
    data = client.get_starters_for_game(
        "New York Yankees", "Boston Red Sox", "2026-05-13",
    )
    # data = {"home": {...}, "away": {...}}
"""
from __future__ import annotations

import logging
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Optional

_BASE = "https://statsapi.mlb.com/api/v1"
_CACHE_FILE = Path(".cache/pitcher_cache.json")
_CACHE_TTL = 3600  # 1 hour

# Neutral baselines used when a pitcher's real data is unavailable.
# bb9 ~ league avg ~3.3 walks per 9; era_home/era_away mirror season ERA;
# last3_era equals season ERA so "recent form" diff is zero by default.
_NEUTRAL_PITCHER = {
    "era":         4.50,
    "whip":        1.30,
    "k_rate":      0.215,
    "k_per_9":     8.50,
    "bb9":         3.30,
    "era_home":    4.50,
    "era_away":    4.50,
    "last3_era":   4.50,
    "wins":        0,
    "losses":      0,
    "hand":        0,    # 0 = RHP, 1 = LHP
    "rest":        4,
    "full_name":   "",
    "team_abbrev": "",
}

# Shared helpers — imported from utils instead of defined locally
from .utils import _safe, _team_tokens, _fetch_url as _fetch  # noqa: E402


def _log(msg: str) -> None:
    """Tagged stderr line so deploy logs make MLB API failures visible.
    Single channel for all pitcher-pipeline diagnostics -- grep
    "[pitcher_client]" to see every fetch + parse step end-to-end."""
    print(f"[pitcher_client] {msg}", flush=True, file=sys.stderr)


def _fetch_with_log(
    url: str,
    label: str,
    timeout: int = 8,
    dump_body: bool = False,
) -> dict:
    """JSON GET with explicit failure logging.

    Returns {} on any error (so callers never need to handle exceptions),
    but unlike utils._fetch_url it tells the deploy log which endpoint
    blew up, how long it took, and what the underlying error was.  That
    was the silent-failure mode the user hit: every pitcher card shows
    N/A because every stats fetch returned {} and nothing said why.

    dump_body=True logs the first ~2KB of the raw JSON response so we
    can see exactly what MLB returned -- used by /schedule diagnostics
    when the probablePitcher field is missing from every game.
    """
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "sports-betting-ai/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            data = json.loads(body)
        ms = int((time.monotonic() - started) * 1000)
        if not data:
            _log(f"  {label}: HTTP 200 but empty body  url={url}  ({ms}ms)")
        elif dump_body:
            preview = body[:2000]
            truncated = "" if len(body) <= 2000 else f"  (truncated, total={len(body)} chars)"
            _log(f"  {label}: HTTP 200 ({ms}ms)  raw response preview:{truncated}")
            _log(f"    {preview}")
        return data if isinstance(data, dict) else {}
    except urllib.error.HTTPError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: HTTP {exc.code} {exc.reason}  url={url}  ({ms}ms)")
        return {}
    except urllib.error.URLError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: network error  reason={exc.reason!r}  url={url}  ({ms}ms)")
        return {}
    except json.JSONDecodeError as exc:
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: invalid JSON  msg={exc.msg!r}  url={url}  ({ms}ms)")
        return {}
    except Exception as exc:                                              # noqa: BLE001
        ms = int((time.monotonic() - started) * 1000)
        _log(f"  {label}: unexpected error  type={type(exc).__name__}  "
             f"msg={exc!s}  url={url}  ({ms}ms)")
        return {}


def _load_disk_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            raw = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) < _CACHE_TTL:
                return raw
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)
    return {}


def _save_disk_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        data["_ts"] = time.time()
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as _exc:
        logging.warning("Suppressed exception in %s: %s", __name__, _exc)


def _parse_rest(note: str) -> int:
    """Extract days of rest from MLB Stats note string like '3 Days Rest'."""
    m = re.search(r"(\d+)\s*[Dd]ay", note or "")
    return int(m.group(1)) if m else 4


def _parse_innings_pitched(value) -> float:
    """MLB Stats API returns inningsPitched as a string like "123.1" where
    .1 = 1/3 inning, .2 = 2/3 inning.  Parse to a true float so K/9 + BB/9
    math is correct.  Returns 0.0 on any failure."""
    if value is None or value == "":
        return 0.0
    try:
        s = str(value)
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + (float(frac) / 3.0)
    except (TypeError, ValueError):
        return 0.0


def _current_season(game_date: Optional[str]) -> int:
    """ET-aware season inference.  Most callers pass game_date so use its
    year directly; only the live "today's slate" path falls back to the
    container clock."""
    if game_date:
        try:
            return int(str(game_date)[:4])
        except (TypeError, ValueError):
            pass
    return date.today().year


class PitcherClient:
    """Caches pitcher data for today to avoid repeated API calls."""

    def __init__(self):
        self._cache = _load_disk_cache()
        self._dirty = False

    # ── Public API ────────────────────────────────────────────────────────────

    def get_starters_for_game(
        self,
        home_team: str,
        away_team: str,
        game_date: Optional[str] = None,
        commence_time: Optional[str] = None,
    ) -> dict:
        """
        Return pitcher feature dict for one game:
        {
            "home": {era, whip, k_rate, k_per_9, bb9, era_home, era_away,
                     last3_era, wins, losses, hand, rest, full_name,
                     team_abbrev},
            "away": {...},
        }
        Returns neutral values for any unavailable fields.

        Doubleheader-safe (BUG 1): when two schedule entries match the same
        team pair on the same date, they're sorted by start time ascending and
        the one whose start time is closest to *commence_time* is chosen, so
        game 1 (earlier slot) and game 2 (later slot) never get swapped.  With
        no commence_time the earliest slot (game 1) is used.
        """
        date_str = game_date or date.today().isoformat()
        season   = _current_season(date_str)
        schedule = self._get_schedule(date_str)

        home_stats = away_stats = None

        _log(f"get_starters_for_game home={home_team!r} away={away_team!r} "
             f"date={date_str} commence={commence_time!r} "
             f"season={season}  schedule_entries={len(schedule)}")

        # Collect ALL schedule entries for this team pair (a doubleheader
        # yields two), sorted by their start time ascending.
        matches = [
            e for e in schedule
            if len(_team_tokens(e.get("home_name", "")) & _team_tokens(home_team)) >= 1
            and len(_team_tokens(e.get("away_name", "")) & _team_tokens(away_team)) >= 1
        ]
        matches.sort(key=lambda e: (e.get("game_date") or "", e.get("game_number") or 0))

        entry = None
        if len(matches) == 1:
            entry = matches[0]
        elif len(matches) > 1:
            # Doubleheader: pick the slot whose start time is closest to the
            # requested commence_time; fall back to the earliest (game 1).
            entry = matches[0]
            if commence_time:
                def _ts(v):
                    try:
                        from datetime import datetime as _dt
                        return _dt.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
                    except Exception:                                     # noqa: BLE001
                        return None
                target = _ts(commence_time)
                if target is not None:
                    scored = [(abs((_ts(e.get("game_date")) or target) - target), e)
                              for e in matches]
                    scored.sort(key=lambda x: x[0])
                    entry = scored[0][1]
            _log(f"  doubleheader: {len(matches)} entries for this pair -> "
                 f"chose game_pk={entry.get('game_pk')} "
                 f"start={entry.get('game_date')!r} (game {entry.get('game_number')})")

        if entry is not None:
            _log(f"  matched schedule entry game_pk={entry.get('game_pk')}  "
                 f"sched_home={entry.get('home_name')!r}  "
                 f"sched_away={entry.get('away_name')!r}")
            home_stats = self._pitcher_stats(
                entry.get("home_pitcher"), entry.get("home_team_id"), season,
            )
            away_stats = self._pitcher_stats(
                entry.get("away_pitcher"), entry.get("away_team_id"), season,
            )

        if home_stats is None and away_stats is None:
            _log(f"  no matching schedule entry found for "
                 f"home={home_team!r} away={away_team!r} -- returning neutral")

        return {
            "home": home_stats or dict(_NEUTRAL_PITCHER),
            "away": away_stats or dict(_NEUTRAL_PITCHER),
        }

    def save(self) -> None:
        if self._dirty:
            _save_disk_cache(self._cache)
            self._dirty = False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_schedule(self, date_str: str) -> list[dict]:
        """One /schedule call per ET date.  Pulls probable pitchers AND
        the team-side ids so we can later resolve each club's three-letter
        abbreviation.  Cached on disk so a refresh tick on the slate
        page doesn't re-hit MLB.

        Diagnostic-heavy version: logs the exact URL, the raw response
        shape, whether each game carries `probablePitcher`, and -- when
        the standard hydrate string returns 0 probables -- retries with
        the wider `hydrate=teams,probablePitcher,person` form so the
        Railway logs make it obvious whether the API itself is dropping
        the pitcher data or if our parsing is.
        """
        cache_key = f"sched_{date_str}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        primary_url = (
            f"{_BASE}/schedule?sportId=1&date={date_str}"
            f"&hydrate=probablePitcher(note,pitchHand)"
        )
        _log(f"_get_schedule date={date_str}: PRIMARY URL = {primary_url}")
        data = _fetch_with_log(
            primary_url,
            label=f"schedule date={date_str} (primary)",
            dump_body=True,
        )

        # Top-level diagnostic dump: which keys came back, how many
        # dates, how many games per date.  Helps catch the case where
        # MLB returns an envelope without the `dates` array (e.g. an
        # error envelope with `messageNumber`).
        top_keys = list(data.keys()) if isinstance(data, dict) else []
        dates_block = data.get("dates", []) if isinstance(data, dict) else []
        n_games_total = sum(len(d.get("games") or []) for d in dates_block)
        _log(
            f"  primary response: top_keys={top_keys!r} "
            f"dates_count={len(dates_block)} games_total={n_games_total}"
        )

        # Per-game audit: which games actually carry a probablePitcher
        # object on each side.  This is the exact field the rest of the
        # pipeline keys off.
        entries = []
        with_probable = 0
        without_probable = 0
        for day in dates_block:
            for game in (day.get("games") or []):
                teams      = game.get("teams") or {}
                home       = teams.get("home") or {}
                away       = teams.get("away") or {}
                home_pp    = home.get("probablePitcher")
                away_pp    = away.get("probablePitcher")
                home_name  = (home.get("team") or {}).get("name", "")
                away_name  = (away.get("team") or {}).get("name", "")
                game_pk    = game.get("gamePk")
                home_keys  = list(home.keys())
                away_keys  = list(away.keys())
                _log(
                    f"    game_pk={game_pk}  {away_name} @ {home_name}  "
                    f"home.probablePitcher={'YES' if home_pp else 'NO'}  "
                    f"away.probablePitcher={'YES' if away_pp else 'NO'}  "
                    f"home_keys={home_keys}  away_keys={away_keys}"
                )
                if home_pp:
                    _log(
                        f"      home.probablePitcher = "
                        f"id={home_pp.get('id')!r}  "
                        f"fullName={home_pp.get('fullName')!r}  "
                        f"hand={(home_pp.get('pitchHand') or {}).get('code')!r}"
                    )
                if away_pp:
                    _log(
                        f"      away.probablePitcher = "
                        f"id={away_pp.get('id')!r}  "
                        f"fullName={away_pp.get('fullName')!r}  "
                        f"hand={(away_pp.get('pitchHand') or {}).get('code')!r}"
                    )
                if home_pp:
                    with_probable += 1
                else:
                    without_probable += 1
                if away_pp:
                    with_probable += 1
                else:
                    without_probable += 1
                entries.append({
                    "game_pk":      game_pk,
                    "game_date":    game.get("gameDate"),     # ISO start time
                    "game_number":  game.get("gameNumber"),   # 1/2 on doubleheaders
                    "home_name":    home_name,
                    "away_name":    away_name,
                    "home_team_id": (home.get("team") or {}).get("id"),
                    "away_team_id": (away.get("team") or {}).get("id"),
                    "home_pitcher": home_pp,
                    "away_pitcher": away_pp,
                })

        _log(
            f"  primary parse: {len(entries)} games  "
            f"with_probable_slots={with_probable} "
            f"without_probable_slots={without_probable}"
        )

        # Fallback hydrate -- when the primary returned zero probable
        # pitcher objects across the entire slate we retry with the
        # wider `hydrate=teams,probablePitcher,person` form per spec.
        # Same parse loop with the same diagnostic depth so we can tell
        # which form (if either) MLB is actually responding to.
        if entries and with_probable == 0:
            fallback_url = (
                f"{_BASE}/schedule?sportId=1&date={date_str}"
                f"&hydrate=teams,probablePitcher,person"
            )
            _log(
                f"  primary returned 0 probable pitchers -- retrying with "
                f"FALLBACK URL = {fallback_url}"
            )
            fb_data = _fetch_with_log(
                fallback_url,
                label=f"schedule date={date_str} (fallback)",
                dump_body=True,
            )
            fb_top = list(fb_data.keys()) if isinstance(fb_data, dict) else []
            fb_dates = fb_data.get("dates", []) if isinstance(fb_data, dict) else []
            _log(f"  fallback response: top_keys={fb_top!r} "
                 f"dates_count={len(fb_dates)}")
            fb_entries = []
            fb_with_probable = 0
            for day in fb_dates:
                for game in (day.get("games") or []):
                    teams = game.get("teams") or {}
                    home  = teams.get("home") or {}
                    away  = teams.get("away") or {}
                    home_pp = home.get("probablePitcher")
                    away_pp = away.get("probablePitcher")
                    home_name = (home.get("team") or {}).get("name", "")
                    away_name = (away.get("team") or {}).get("name", "")
                    _log(
                        f"    [fb] game_pk={game.get('gamePk')}  "
                        f"{away_name} @ {home_name}  "
                        f"home.pp={'YES' if home_pp else 'NO'}  "
                        f"away.pp={'YES' if away_pp else 'NO'}"
                    )
                    if home_pp: fb_with_probable += 1
                    if away_pp: fb_with_probable += 1
                    fb_entries.append({
                        "game_pk":      game.get("gamePk"),
                        "home_name":    home_name,
                        "away_name":    away_name,
                        "home_team_id": (home.get("team") or {}).get("id"),
                        "away_team_id": (away.get("team") or {}).get("id"),
                        "home_pitcher": home_pp,
                        "away_pitcher": away_pp,
                    })
            _log(
                f"  fallback parse: {len(fb_entries)} games  "
                f"with_probable_slots={fb_with_probable}"
            )
            # Only swap if the fallback actually produced probable
            # objects -- otherwise the primary's entries (with their
            # empty probable slots) are still the better data so the
            # rest of the pipeline can at least pick up team names.
            if fb_with_probable > 0:
                _log("  using FALLBACK results -- replacing primary entries")
                entries = fb_entries

        _log(f"  schedule date={date_str}: returning {len(entries)} games")
        self._cache[cache_key] = entries
        self._dirty = True
        return entries

    def _pitcher_stats(
        self,
        pitcher_info: Optional[dict],
        team_id: Optional[int],
        season: int,
    ) -> Optional[dict]:
        """Run all 6 fetches end-to-end for one probable starter.

        Each step is wrapped + logged independently so a partial outage
        (e.g. homeAndAway endpoint times out but season + gameLog
        succeed) still produces useful real data for the fields that
        did fetch, with neutral fallback only for what actually failed.
        Cache is invalidated if season ERA never resolved -- we'd rather
        retry than serve "N/A" for an hour.
        """
        if not pitcher_info:
            return None
        pid = pitcher_info.get("id")
        if not pid:
            return None

        cache_key = f"p_{pid}_{season}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        _log(f"_pitcher_stats pid={pid} team_id={team_id} season={season}")

        # 1) Season stats: ERA + WHIP + W-L + raw counts for K/9 + BB/9.
        season_stats  = self._fetch_season_stats(pid, season)
        # 2) Home / away ERA splits.
        ha_splits     = self._fetch_home_away_splits(pid, season)
        # 3) Last-3-starts ERA from the game log.
        last3_era_val = self._fetch_last3_era(pid, season)
        # 4) Display name from /people/{pid}.  Falls back to whatever
        #    the schedule already returned for fullName.
        full_name     = self._fetch_full_name(pid) or (
            pitcher_info.get("fullName") or ""
        )
        # 5) Three-letter team abbreviation.
        team_abbrev   = self._fetch_team_abbrev(team_id) if team_id else ""

        hand_code = "R"
        if isinstance(pitcher_info.get("pitchHand"), dict):
            hand_code = pitcher_info["pitchHand"].get("code", "R") or "R"

        era      = season_stats.get("era")
        result = {
            "era":         era if era is not None else _NEUTRAL_PITCHER["era"],
            "whip":        season_stats.get("whip", _NEUTRAL_PITCHER["whip"]),
            "k_rate":      season_stats.get("k_rate", _NEUTRAL_PITCHER["k_rate"]),
            "k_per_9":     season_stats.get("k_per_9", _NEUTRAL_PITCHER["k_per_9"]),
            "bb9":         season_stats.get("bb9", _NEUTRAL_PITCHER["bb9"]),
            "era_home":    ha_splits.get("home", era if era is not None
                                          else _NEUTRAL_PITCHER["era_home"]),
            "era_away":    ha_splits.get("away", era if era is not None
                                          else _NEUTRAL_PITCHER["era_away"]),
            "last3_era":   last3_era_val if last3_era_val is not None else (
                era if era is not None else _NEUTRAL_PITCHER["last3_era"]
            ),
            "wins":        int(season_stats.get("wins")   or 0),
            "losses":      int(season_stats.get("losses") or 0),
            "hand":        1 if hand_code == "L" else 0,
            "rest":        _parse_rest(pitcher_info.get("note", "")),
            "full_name":   full_name,
            "team_abbrev": team_abbrev,
        }

        _log(
            f"  pid={pid} RESULT name={full_name!r} team={team_abbrev!r} "
            f"era={result['era']} whip={result['whip']} "
            f"k/9={result['k_per_9']} bb/9={result['bb9']} "
            f"home_era={result['era_home']} away_era={result['era_away']} "
            f"last3_era={result['last3_era']} "
            f"record={result['wins']}-{result['losses']}"
        )

        # Cache only when the season fetch produced a real ERA.  An
        # all-neutral row would otherwise persist for an hour and the
        # user would see "N/A" until the cache expired naturally.
        if era is not None:
            self._cache[cache_key] = result
            self._dirty = True
        else:
            _log(f"  pid={pid} -- not caching (season ERA missing); "
                 f"next request will retry the fetch")
        return result

    # ── Per-endpoint fetchers (each logs its own outcome) ──────────────────

    def _fetch_season_stats(self, pid: int, season: int) -> dict:
        """`stats=season&group=pitching` -> ERA, WHIP, wins, losses, plus
        the raw counts used to compute K/9 + BB/9.  K/9 = strikeOuts *
        9 / inningsPitched; BB/9 = baseOnBalls * 9 / inningsPitched.
        MLB Stats API also exposes pre-computed `strikeoutsPer9Inn` /
        `walksPer9Inn` -- we prefer those when present and fall back
        to the raw division.
        """
        url = (f"{_BASE}/people/{pid}/stats"
               f"?stats=season&group=pitching&season={season}")
        data = _fetch_with_log(url, label=f"season pid={pid}")
        out: dict = {}
        for grp in data.get("stats", []):
            for split in grp.get("splits", []):
                st = split.get("stat", {})
                if not isinstance(st, dict):
                    continue
                out["era"]    = _safe(st.get("era"),  None)
                out["whip"]   = _safe(st.get("whip"), None)
                out["wins"]   = _safe(st.get("wins"),   None)
                out["losses"] = _safe(st.get("losses"), None)
                k  = _safe(st.get("strikeOuts"),  0)
                bb = _safe(st.get("baseOnBalls"), 0)
                ip = _parse_innings_pitched(st.get("inningsPitched"))
                bf = _safe(st.get("battersFaced"), 0)
                # K/9 + BB/9 -- prefer pre-computed if MLB API returned them.
                pre_k9  = _safe(st.get("strikeoutsPer9Inn"), None)
                pre_bb9 = _safe(st.get("walksPer9Inn"),      None)
                out["k_per_9"] = (
                    pre_k9 if pre_k9 is not None
                    else (k * 9.0 / ip if ip > 0 else None)
                )
                out["bb9"] = (
                    pre_bb9 if pre_bb9 is not None
                    else (bb * 9.0 / ip if ip > 0 else None)
                )
                # K rate (strikeouts per batter faced) -- still used by
                # the moneyline feature builder.
                out["k_rate"] = (k / bf) if bf > 0 else None
                _log(f"  season pid={pid} parsed: era={out['era']} "
                     f"whip={out['whip']} k/9={out['k_per_9']} "
                     f"bb/9={out['bb9']} ip={ip} k={k} bb={bb} "
                     f"record={out['wins']}-{out['losses']}")
                return out
        _log(f"  season pid={pid}: no splits in response -- using neutrals")
        return out

    def _fetch_home_away_splits(self, pid: int, season: int) -> dict:
        """`stats=homeAndAway&group=pitching` -> separate ERA for the
        pitcher's home and road games.  Returns {"home": ..., "away":
        ...} (either key may be missing if the pitcher hasn't pitched
        in that venue type yet)."""
        url = (f"{_BASE}/people/{pid}/stats"
               f"?stats=homeAndAway&group=pitching&season={season}")
        data = _fetch_with_log(url, label=f"homeAndAway pid={pid}")
        out: dict = {}
        for grp in data.get("stats", []):
            for split in grp.get("splits", []):
                if not isinstance(split, dict):
                    continue
                # MLB exposes split.isHome=true|false on each row.
                is_home = bool(split.get("isHome"))
                era_val = _safe((split.get("stat") or {}).get("era"), None)
                if era_val is None:
                    continue
                out["home" if is_home else "away"] = float(era_val)
        if out:
            _log(f"  homeAndAway pid={pid}: home_era={out.get('home')} "
                 f"away_era={out.get('away')}")
        else:
            _log(f"  homeAndAway pid={pid}: no splits parsed (pitcher may "
                 f"not have logged both venue types yet)")
        return out

    def _fetch_last3_era(self, pid: int, season: int) -> Optional[float]:
        """`stats=gameLog&group=pitching` -> chronological list of every
        appearance this season.  Take the most recent 3 starts, sum
        earnedRuns + inningsPitched, return ERA = ER * 9 / IP across
        the window.  None when the pitcher has fewer than 3 starts on
        record."""
        url = (f"{_BASE}/people/{pid}/stats"
               f"?stats=gameLog&group=pitching&season={season}")
        data = _fetch_with_log(url, label=f"gameLog pid={pid}")
        starts: list[tuple[str, float, float]] = []
        for grp in data.get("stats", []):
            for split in grp.get("splits", []):
                if not isinstance(split, dict):
                    continue
                st = split.get("stat") or {}
                try:
                    gs = int(st.get("gamesStarted", 0) or 0)
                except (TypeError, ValueError):
                    gs = 0
                if gs <= 0:
                    continue
                er = _safe(st.get("earnedRuns"), 0.0)
                ip = _parse_innings_pitched(st.get("inningsPitched"))
                game_date = split.get("date", "") or ""
                if ip > 0 and game_date:
                    starts.append((game_date, float(er), ip))
        starts.sort(key=lambda r: r[0])
        if len(starts) < 3:
            _log(f"  gameLog pid={pid}: only {len(starts)} starts found, "
                 f"need 3 -- returning None")
            return None
        window   = starts[-3:]
        total_er = sum(r[1] for r in window)
        total_ip = sum(r[2] for r in window)
        era3     = (total_er * 9.0 / total_ip) if total_ip > 0 else None
        _log(f"  gameLog pid={pid}: last3 dates="
             f"{[r[0] for r in window]}  total_er={total_er} "
             f"total_ip={total_ip}  -> last3_era={era3}")
        return era3

    def _fetch_full_name(self, pid: int) -> str:
        """`/people/{pid}` -> fullName for the matchup-page header."""
        url  = f"{_BASE}/people/{pid}"
        data = _fetch_with_log(url, label=f"people pid={pid}")
        for person in data.get("people", []):
            name = person.get("fullName") or person.get("nameFirstLast") or ""
            if name:
                _log(f"  people pid={pid}: fullName={name!r}")
                return str(name)
        _log(f"  people pid={pid}: no fullName in response")
        return ""

    def _fetch_team_abbrev(self, team_id: int) -> str:
        """`/teams/{team_id}` -> three-letter abbreviation (DET, NYY).
        Cached so the same abbreviation isn't re-fetched once per game
        on a 15-game slate."""
        cache_key = f"team_abbrev_{team_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        url  = f"{_BASE}/teams/{team_id}"
        data = _fetch_with_log(url, label=f"teams team_id={team_id}")
        abbrev = ""
        for team in data.get("teams", []):
            abbrev = (team.get("abbreviation") or "").upper()
            if abbrev:
                break
        if abbrev:
            _log(f"  teams team_id={team_id}: abbreviation={abbrev!r}")
            self._cache[cache_key] = abbrev
            self._dirty = True
        else:
            _log(f"  teams team_id={team_id}: no abbreviation in response")
        return abbrev


# ── Module-level singleton ───────────────────────────────────────────────────

_client: Optional[PitcherClient] = None


def get_pitcher_client() -> PitcherClient:
    global _client
    if _client is None:
        _client = PitcherClient()
    return _client
