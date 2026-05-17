"""
Historical MLB training dataset from Retrosheet game logs and pybaseball.

Provides ~6 000–7 000 game-level training rows across the last three MLB
seasons, feature-aligned with the 23-element MLB_FEATURES vector.

Features filled from historical data:
  • Rolling team stats  : net_run_diff, rpg_diff, rapg_diff, win_pct_diff,
                          home_away_split_diff, last10_diff
  • Hits per game       : hits_diff  (from pybaseball team batting, season-level)
  • Bullpen ERA proxy   : bullpen_era_diff  (from pybaseball team pitching ERA)
  • Park run factor     : park_run_factor   (static lookup)

Features set to league-average neutral baselines (model learns zero = reference):
  home_implied_prob=0.54, run_line=-1.5, all SP columns, errors_diff,
  wind_speed, wind_direction, bullpen_fatigue_diff, lineup_confirmed,
  line_movement

Cached at .cache/historical_mlb_dataset.joblib; rebuilt every 7 days.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np

log = logging.getLogger(__name__)

_CACHE_PATH  = Path(".cache/historical_mlb_dataset.joblib")
_CACHE_DAYS  = 7
_MIN_GAMES   = 10      # minimum prior games per team before using a row
_SEASONS     = (2022, 2023, 2024)
_N_FEATURES  = 23      # must match len(MLB_FEATURES)


# ── Per-team rolling-stats tracker ───────────────────────────────────────────

class _TeamSeason:
    def __init__(self) -> None:
        self.g  = self.w  = 0
        self.hg = self.hw = 0     # home games / home wins
        self.ag = self.aw = 0     # away games / away wins
        self.rs = self.ra = 0     # runs scored / allowed (cumulative)
        self.last10: deque = deque(maxlen=10)

    def stats(self) -> dict:
        def _pct(n: int, d: int) -> float:
            return n / d if d > 0 else 0.5

        return {
            "games":        self.g,
            "win_pct":      _pct(self.w,  self.g),
            "home_win_pct": _pct(self.hw, self.hg),
            "away_win_pct": _pct(self.aw, self.ag),
            "rpg":          self.rs / self.g if self.g > 0 else 4.5,
            "rapg":         self.ra / self.g if self.g > 0 else 4.5,
            "last10":       sum(self.last10) / len(self.last10) if self.last10 else 0.5,
        }

    def update(self, runs_scored: int, runs_allowed: int, *, is_home: bool) -> None:
        won = runs_scored > runs_allowed
        self.g  += 1
        self.rs += runs_scored
        self.ra += runs_allowed
        self.last10.append(1 if won else 0)
        if won:
            self.w += 1
        if is_home:
            self.hg += 1
            if won:
                self.hw += 1
        else:
            self.ag += 1
            if won:
                self.aw += 1


# ── pybaseball stats loader ───────────────────────────────────────────────────

def _load_pybaseball_stats(
    seasons: tuple[int, ...],
) -> tuple[dict, dict]:
    """
    Returns (batting_lu, pitching_lu) keyed by (retro_code, season).
    batting_lu  → {"hpg": hits_per_game}
    pitching_lu → {"era": float}
    Gracefully returns empty dicts if pybaseball is not installed or fails.
    """
    from .retrosheet_client import FG_TO_RETRO

    try:
        import pybaseball  # noqa: PLC0415
    except ImportError:
        log.info("pybaseball not installed — hits_diff and bullpen ERA will use neutral values")
        return {}, {}

    batting_lu:  dict = {}
    pitching_lu: dict = {}

    try:
        tb = pybaseball.team_batting(min(seasons), max(seasons))
        for _, row in tb.iterrows():
            fg = str(row.get("teamIDfg") or "")
            rc = FG_TO_RETRO.get(fg)
            if not rc:
                continue
            season = int(row.get("Season", 0))
            g = float(row.get("G", 0) or 0) or 162.0
            h = float(row.get("H", 0) or 0)
            batting_lu[(rc, season)] = {"hpg": h / g}
    except Exception as exc:
        log.warning("pybaseball team_batting error: %s", exc)

    try:
        tp = pybaseball.team_pitching(min(seasons), max(seasons))
        for _, row in tp.iterrows():
            fg = str(row.get("teamIDfg") or "")
            rc = FG_TO_RETRO.get(fg)
            if not rc:
                continue
            season = int(row.get("Season", 0))
            era    = float(row.get("ERA", 4.20) or 4.20)
            pitching_lu[(rc, season)] = {"era": era}
    except Exception as exc:
        log.warning("pybaseball team_pitching error: %s", exc)

    log.info(
        "pybaseball: %d batting rows, %d pitching rows",
        len(batting_lu), len(pitching_lu),
    )
    return batting_lu, pitching_lu


# ── Feature vector assembly ───────────────────────────────────────────────────

def _build_vec(
    hs: dict,
    as_: dict,
    h_bat: dict,
    a_bat: dict,
    h_era: float,
    a_era: float,
    park_run: float,
) -> np.ndarray:
    """Assemble 23-element MLB_FEATURES vector from historical data."""
    h_rpg  = hs["rpg"];  a_rpg  = as_["rpg"]
    h_rapg = hs["rapg"]; a_rapg = as_["rapg"]
    h_hpg  = h_bat.get("hpg", 8.5)
    a_hpg  = a_bat.get("hpg", 8.5)

    return np.array([
        # ── Team statistics ──────────────────────────────────────────────────
        (h_rpg - h_rapg) - (a_rpg - a_rapg),    # net_run_diff
        h_rpg  - a_rpg,                           # rpg_diff
        h_rapg - a_rapg,                          # rapg_diff
        hs["win_pct"]      - as_["win_pct"],       # win_pct_diff
        hs["home_win_pct"] - as_["away_win_pct"],  # home_away_split_diff
        hs["last10"]       - as_["last10"],         # last10_diff
        h_hpg - a_hpg,                             # hits_diff
        0.0,                                        # errors_diff  (neutral)
        0.54,                                       # home_implied_prob (neutral)
        -1.5,                                       # run_line  (neutral)
        # ── Starting pitcher — all neutral ───────────────────────────────────
        0.0,    # sp_era_diff
        0.0,    # sp_whip_diff
        0.0,    # sp_k_rate_diff
        4.0,    # home_sp_rest
        4.0,    # away_sp_rest
        0.0,    # sp_hand_adv
        # ── Ballpark ─────────────────────────────────────────────────────────
        park_run,   # park_run_factor  (real value from static lookup)
        0.0,        # wind_speed  (neutral)
        0.0,        # wind_direction  (neutral)
        # ── Bullpen ERA proxy ─────────────────────────────────────────────────
        a_era - h_era,  # bullpen_era_diff  (positive = home bullpen better)
        0.0,             # bullpen_fatigue_diff  (neutral)
        # ── Lineup & market — neutral ─────────────────────────────────────────
        0.0,    # lineup_confirmed
        0.0,    # line_movement
    ], dtype=np.float32)


# ── Main entry point ──────────────────────────────────────────────────────────

def build_historical_dataset(
    seasons: tuple[int, ...] = _SEASONS,
    force_rebuild: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (X, y) numpy arrays for historical MLB games.

    X : (n, 23) float32 aligned with MLB_FEATURES
    y : (n,)    int32   1 = home team won

    Tries the enriched dataset (real pitcher values) first; falls back to the
    basic Retrosheet-only dataset when enrichment is unavailable.
    Returns empty arrays when no historical data can be loaded.
    """
    # ── Attempt enriched dataset (real SP ERA/WHIP/K-rate/rest) ──────────────
    try:
        from .enriched_historical_data import get_enriched_X_y
        X_e, y_e = get_enriched_X_y(seasons, force_rebuild)
        if len(y_e) >= 100:
            log.info("Historical dataset (enriched): %d games", len(y_e))
            return X_e, y_e
    except Exception as exc:
        log.warning("Enriched dataset unavailable (%s) — using basic Retrosheet data", exc)

    # ── Basic fallback ────────────────────────────────────────────────────────
    _CACHE_PATH.parent.mkdir(exist_ok=True)

    if not force_rebuild and _CACHE_PATH.exists():
        age = (datetime.now() - datetime.fromtimestamp(
            _CACHE_PATH.stat().st_mtime
        )).days
        if age < _CACHE_DAYS:
            saved = joblib.load(_CACHE_PATH)
            n = len(saved.get("y", []))
            log.info("Historical dataset from cache: %d games", n)
            return saved["X"], saved["y"]

    from .retrosheet_client import get_season_gamelogs
    from .park_factors import get_park_factors

    print("  Downloading Retrosheet game logs and pybaseball stats…")
    batting_lu, pitching_lu = _load_pybaseball_stats(seasons)

    X_rows: list[np.ndarray] = []
    y_rows: list[int]         = []
    total_skipped = 0

    for season in seasons:
        games = get_season_gamelogs(season)
        if not games:
            print(f"  Retrosheet {season}: download failed — skipping")
            continue

        print(f"  Retrosheet {season}: {len(games)} raw game records")
        games.sort(key=lambda g: g["date"])

        trackers: dict[str, _TeamSeason] = {}
        season_rows = 0

        for game in games:
            hc = game["home_code"]
            ac = game["away_code"]

            if hc not in trackers:
                trackers[hc] = _TeamSeason()
            if ac not in trackers:
                trackers[ac] = _TeamSeason()

            ht = trackers[hc]
            at = trackers[ac]

            hs  = ht.stats()
            as_ = at.stats()

            # Always update trackers; skip row if too early in season
            home_runs = game["home_runs"]
            away_runs = game["away_runs"]
            ht.update(home_runs, away_runs, is_home=True)
            at.update(away_runs, home_runs, is_home=False)

            if hs["games"] < _MIN_GAMES or as_["games"] < _MIN_GAMES:
                total_skipped += 1
                continue

            h_bat = batting_lu.get((hc, season), {})
            a_bat = batting_lu.get((ac, season), {})
            h_era = pitching_lu.get((hc, season), {}).get("era", 4.20)
            a_era = pitching_lu.get((ac, season), {}).get("era", 4.20)
            park_run, _ = get_park_factors(game["home_name"])

            vec = _build_vec(hs, as_, h_bat, a_bat, h_era, a_era, park_run)
            X_rows.append(vec)
            y_rows.append(1 if home_runs > away_runs else 0)
            season_rows += 1

        print(f"  Retrosheet {season}: {season_rows} usable training rows")

    if not X_rows:
        print("  No historical data available — neural network will not be trained")
        empty_X = np.empty((0, _N_FEATURES), dtype=np.float32)
        return empty_X, np.array([], dtype=np.int32)

    X = np.vstack(X_rows).astype(np.float32)
    y = np.array(y_rows, dtype=np.int32)

    joblib.dump({"X": X, "y": y, "built_at": datetime.now().isoformat()}, _CACHE_PATH)
    print(
        f"  Historical dataset: {len(y)} games across {len(seasons)} seasons "
        f"({total_skipped} skipped — early-season rows)"
    )
    return X, y
