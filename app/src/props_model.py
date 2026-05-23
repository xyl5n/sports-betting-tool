"""
props_model.py
==============
Per-market prediction model for MLB player props.

Two classifier artifacts are loaded at module level:
  .cache/props_model_pitcher.joblib   (xgb classifier for pitcher markets)
  .cache/props_model_batter.joblib    (xgb classifier for batter markets)

Alongside those, per-stat XGBRegressor models produce a numeric
predicted value:
  .cache/props_model_pitcher_reg_{stat}.joblib  (K, ER, H, BB, outs)
  .cache/props_model_batter_reg_{stat}.joblib   (H, TB, HR, RBI, R, BB)

Feature names for the regression inference vector are loaded lazily
from .cache/props_reg_metadata.json (written by the training script).

Both classifiers are restored from Supabase on cold boot (the same
pattern src.model uses for the moneyline / run-line / totals joblibs).
When no trained artifact is available the predictor falls back to a
market-neutral heuristic that uses the prop line's implied probability
so the UI always renders something rather than blanking out.

Training lives in scripts/train_props_models.py (pybaseball-driven).
The runtime API here is read-only -- prediction + record tracking.

Logging
-------
Every predict() and settle() call emits a PROPS-MODEL / PROPS-SETTLE
stderr line so Railway captures the predictor's behaviour the same way
the existing model.py emits "model loaded" / "predict" lines.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

from .utils import _safe

_CACHE_DIR = Path(".cache")

# Classifier artifact paths.
PITCHER_MODEL_PATH = _CACHE_DIR / "props_model_pitcher.joblib"
BATTER_MODEL_PATH  = _CACHE_DIR / "props_model_batter.joblib"

# Per-stat XGBRegressor paths (trained alongside the classifiers).
_PITCHER_REG_STATS = ("K", "ER", "H", "BB", "outs")
_BATTER_REG_STATS  = ("H", "TB", "HR", "RBI", "R", "BB")

_PITCHER_REG_PATHS: dict[str, Path] = {
    s: _CACHE_DIR / f"props_model_pitcher_reg_{s}.joblib" for s in _PITCHER_REG_STATS
}
_BATTER_REG_PATHS: dict[str, Path] = {
    s: _CACHE_DIR / f"props_model_batter_reg_{s}.joblib" for s in _BATTER_REG_STATS
}

# Which regression stat does each prop market target?
# Value: (bucket, stat_key) — None when no regressor is available.
_MARKET_REG_KEY: dict[str, tuple[str, str]] = {
    "pitcher_strikeouts":   ("pitcher", "K"),
    "pitcher_earned_runs":  ("pitcher", "ER"),
    "pitcher_hits_allowed": ("pitcher", "H"),
    "pitcher_walks":        ("pitcher", "BB"),
    "pitcher_outs":         ("pitcher", "outs"),
    "batter_hits":          ("batter",  "H"),
    "batter_total_bases":   ("batter",  "TB"),
    "batter_home_runs":     ("batter",  "HR"),
    "batter_rbis":          ("batter",  "RBI"),
    "batter_runs_scored":   ("batter",  "R"),
    "batter_walks":         ("batter",  "BB"),
}

# Feature-name metadata written by the training script.
_REG_META_PATH = _CACHE_DIR / "props_reg_metadata.json"

# Per-player rolling-stat snapshots written by the training script.
# Inference looks these up by MLB player ID so the szn_*/r7_*/r14_* feature
# slots get real values instead of the prop line as a degenerate proxy.
_PITCHER_SNAPSHOTS_PATH = _CACHE_DIR / "pitcher_rolling_snapshots.json"
_BATTER_SNAPSHOTS_PATH  = _CACHE_DIR / "batter_rolling_snapshots.json"

# Per (season, team) opponent-context baselines (k_rate, woba, lhb_pct)
# computed from cached batter game logs.  Used by the pitcher inference path
# to look up the opposing batting team's offensive profile.
_TEAM_BASELINES_PATH = _CACHE_DIR / "team_baselines.json"

# Per-classifier picks history (mirrors xgb / lr / nn picks history
# in /api/admin/reset/model_record so the new sinks plug into the
# existing reset machinery).
PITCHER_HISTORY_PATH = _CACHE_DIR / "props_pitcher_picks_history.json"
BATTER_HISTORY_PATH  = _CACHE_DIR / "props_batter_picks_history.json"

# Park factor tables (same values as train_props_models.py — kept in
# sync manually; a mismatch shifts predictions slightly but not
# catastrophically because park factors are small continuous signals).
_PARK_K: dict[str, float] = {
    "ARI": 0.96, "ATL": 0.99, "BAL": 1.00, "BOS": 0.96, "CHC": 0.97,
    "CIN": 0.96, "CLE": 1.01, "COL": 0.88, "CWS": 0.99, "DET": 1.02,
    "HOU": 1.00, "KC":  1.02, "LAA": 1.01, "LAD": 1.04, "MIA": 1.00,
    "MIL": 1.00, "MIN": 1.00, "NYM": 1.03, "NYY": 0.94, "OAK": 1.04,
    "PHI": 0.95, "PIT": 1.04, "SD":  1.05, "SEA": 1.04, "SF":  1.05,
    "STL": 1.03, "TB":  1.03, "TEX": 0.97, "TOR": 0.99, "WSH": 1.00,
}
_PARK_H: dict[str, float] = {
    "ARI": 0.97, "ATL": 0.99, "BAL": 1.01, "BOS": 1.08, "CHC": 1.05,
    "CIN": 1.08, "CLE": 0.99, "COL": 1.25, "CWS": 1.00, "DET": 0.97,
    "HOU": 0.99, "KC":  0.97, "LAA": 1.01, "LAD": 0.93, "MIA": 0.98,
    "MIL": 1.00, "MIN": 1.00, "NYM": 0.95, "NYY": 1.05, "OAK": 0.96,
    "PHI": 1.07, "PIT": 0.95, "SD":  0.90, "SEA": 0.93, "SF":  0.89,
    "STL": 0.95, "TB":  0.97, "TEX": 1.02, "TOR": 0.99, "WSH": 1.00,
}
_PARK_HR: dict[str, float] = {
    "ARI": 1.05, "ATL": 1.08, "BAL": 1.10, "BOS": 1.12, "CHC": 1.08,
    "CIN": 1.35, "CLE": 0.95, "COL": 1.26, "CWS": 1.15, "DET": 0.85,
    "HOU": 0.95, "KC":  0.88, "LAA": 1.05, "LAD": 0.87, "MIA": 0.85,
    "MIL": 1.05, "MIN": 1.10, "NYM": 0.93, "NYY": 1.40, "OAK": 0.80,
    "PHI": 1.30, "PIT": 0.82, "SD":  0.72, "SEA": 0.82, "SF":  0.60,
    "STL": 0.90, "TB":  0.90, "TEX": 1.10, "TOR": 1.05, "WSH": 0.95,
}

# Full team name → 3-letter abbreviation used by _PARK_* tables above.
# props_client.py populates `home_team` with The Odds API's full name
# (e.g. "New York Yankees"), so a naive `.upper()[:3]` produced "NEW"
# and silently fell through to the 1.00 default — wiping out the park
# factor signal the model was trained on.  Map full names here.
TEAM_NAME_TO_ABBREV: dict[str, str] = {
    "Arizona Diamondbacks":  "ARI",
    "Atlanta Braves":        "ATL",
    "Baltimore Orioles":     "BAL",
    "Boston Red Sox":        "BOS",
    "Chicago Cubs":          "CHC",
    "Cincinnati Reds":       "CIN",
    "Cleveland Guardians":   "CLE",
    "Colorado Rockies":      "COL",
    "Chicago White Sox":     "CWS",
    "Detroit Tigers":        "DET",
    "Houston Astros":        "HOU",
    "Kansas City Royals":    "KC",
    "Los Angeles Angels":    "LAA",
    "Los Angeles Dodgers":   "LAD",
    "Miami Marlins":         "MIA",
    "Milwaukee Brewers":     "MIL",
    "Minnesota Twins":       "MIN",
    "New York Mets":         "NYM",
    "New York Yankees":      "NYY",
    "Oakland Athletics":     "OAK",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates":    "PIT",
    "San Diego Padres":      "SD",
    "Seattle Mariners":      "SEA",
    "San Francisco Giants":  "SF",
    "St. Louis Cardinals":   "STL",
    "Tampa Bay Rays":        "TB",
    "Texas Rangers":         "TEX",
    "Toronto Blue Jays":     "TOR",
    "Washington Nationals":  "WSH",
}

# Module-load invariant: every abbrev we map to must exist in all three
# park-factor tables.  A typo here (e.g. "WAS" vs "WSH") would silently
# revert that team to the 1.00 default again — assert so we fail loud.
assert set(TEAM_NAME_TO_ABBREV.values()) <= _PARK_K.keys(), \
    f"TEAM_NAME_TO_ABBREV maps to abbrevs missing from _PARK_K: " \
    f"{set(TEAM_NAME_TO_ABBREV.values()) - _PARK_K.keys()}"
assert set(TEAM_NAME_TO_ABBREV.values()) <= _PARK_H.keys(), \
    f"TEAM_NAME_TO_ABBREV maps to abbrevs missing from _PARK_H: " \
    f"{set(TEAM_NAME_TO_ABBREV.values()) - _PARK_H.keys()}"
assert set(TEAM_NAME_TO_ABBREV.values()) <= _PARK_HR.keys(), \
    f"TEAM_NAME_TO_ABBREV maps to abbrevs missing from _PARK_HR: " \
    f"{set(TEAM_NAME_TO_ABBREV.values()) - _PARK_HR.keys()}"


def _team_to_abbrev(home_team: str) -> str:
    """Normalize a home_team string to its 3-letter park-factor key.
    Accepts both full names ("New York Yankees") and already-abbreviated
    inputs ("NYY"); empty/unknown → "" so callers fall back to 1.00."""
    if not home_team:
        return ""
    s = home_team.strip()
    if s in TEAM_NAME_TO_ABBREV:
        return TEAM_NAME_TO_ABBREV[s]
    upper = s.upper()
    if upper in _PARK_K:
        return upper
    return ""


# ── Hardcoded feature name lists (must match train_props_models.py) ──────────
# These are duplicated here so inference never requires props_reg_metadata.json
# to be present on disk (the file is lost on every Railway redeploy).

_P_ROLL = ["K", "BB", "H", "ER", "IP", "k_per_9", "bb_per_9"]
_B_ROLL = [
    "H", "HR", "RBI", "R", "BB", "SO", "TB", "AB", "PA",
    "H_per_AB", "TB_per_AB", "HR_per_AB", "BB_per_PA", "SO_per_PA",
]

_PITCHER_FEATURE_NAMES: list[str] = (
    [f"szn_{c}" for c in _P_ROLL]   # 7
    + [f"r7_{c}"  for c in _P_ROLL] # 7
    + [f"r14_{c}" for c in _P_ROLL] # 7
    + ["is_home_i", "days_since_last_start", "ip_last_30d", "ballpark_factor_k"]  # 4
    + ["era_vs_lhb", "k_rate_vs_lhb", "era_vs_rhb", "k_rate_vs_rhb"]             # 4
    + [
        "opp_team_k_rate", "opp_team_woba", "opp_team_lhb_pct",                  # PR #2 opponent context
        "career_k_per_9", "career_bb_per_9", "career_ip",                        # PR #2 career baseline
    ]  # 6
    + [
        "lineup_avg_k_rate", "lineup_lhb_count", "lineup_rhb_count",
        "weather_temp", "weather_wind_speed", "weather_wind_dir_num",
        "time_of_day", "umpire_k_rate", "implied_total",
        "first_inning_k_pct", "pitch_mix_fastball_pct",
        "pitch_mix_breaking_pct", "pitch_mix_offspeed_pct",
    ]  # 13
)  # 7+7+7+4+4+6+13 = 48

_BATTER_FEATURE_NAMES: list[str] = (
    [f"szn_{c}" for c in _B_ROLL]   # 14
    + [f"r7_{c}"  for c in _B_ROLL] # 14
    + [f"r14_{c}" for c in _B_ROLL] # 14
    + ["r30_HR", "r30_BB"]          # 2  (PR3 sparse-count smoothing)
    + [
        "k_pct_7d", "k_pct_14d", "babip_7d", "babip_14d",
        "batting_order", "is_home_i", "ballpark_factor_hits", "ballpark_factor_hr",
        # PR3: opposing-pitcher context (real features, not placeholders).
        "opp_pitcher_szn_k_per_9", "opp_pitcher_szn_era", "opp_pitcher_throws_lhp",
    ]  # 11
    + ["ops_vs_lhp", "obp_vs_lhp", "slg_vs_lhp", "ops_vs_rhp", "obp_vs_rhp", "slg_vs_rhp"]  # 6
    + [
        "whiff_pct", "chase_pct", "hard_hit_rate", "barrel_rate", "sprint_speed",
        "platoon_matchup_flag", "weather_temp", "weather_wind_speed", "time_of_day",
        "ba_vs_breaking", "ba_vs_fastball", "ba_vs_offspeed",
        "h2h_career_ab", "h2h_career_avg", "h2h_career_k_rate", "implied_total",
    ]  # 16
)  # 14+14+14+2+11+6+16 = 77

# Neutral inference-time defaults for features that require live data
# (lineup, weather, umpire stats, etc.).  These match league-average
# values so missing context shifts predictions minimally.
_PITCHER_DEFAULTS: dict[str, float] = {
    "ip_last_30d":            30.0,
    "days_since_last_start":   5.0,
    "era_vs_lhb":              4.50,
    "k_rate_vs_lhb":           0.215,
    "era_vs_rhb":              4.50,
    "k_rate_vs_rhb":           0.215,
    # PR #2 opponent + career baseline neutrals (modern-era league averages)
    "opp_team_k_rate":         0.225,
    "opp_team_woba":           0.720,   # OPS proxy ≈ league-average wOBA
    "opp_team_lhb_pct":        0.420,
    "career_k_per_9":          8.50,
    "career_bb_per_9":         3.30,
    "career_ip":              50.0,
    "lineup_avg_k_rate":       0.220,
    "lineup_lhb_count":        4.0,
    "lineup_rhb_count":        5.0,
    "weather_temp":           72.0,
    "weather_wind_speed":      8.0,
    "weather_wind_dir_num":    0.0,
    "time_of_day":             1.0,  # 1 = day, 0 = night (league avg ~0.25 day)
    "umpire_k_rate":           0.215,
    "implied_total":           8.5,
    "first_inning_k_pct":      0.210,
    "pitch_mix_fastball_pct":  0.55,
    "pitch_mix_breaking_pct":  0.25,
    "pitch_mix_offspeed_pct":  0.20,
}

_BATTER_DEFAULTS: dict[str, float] = {
    "k_pct_7d":            0.230,
    "k_pct_14d":           0.230,
    "babip_7d":            0.295,
    "babip_14d":           0.295,
    "batting_order":       5.0,
    # PR3: 30-game windows for sparse counts.  League per-game means.
    "r30_HR":              0.13,
    "r30_BB":              0.36,
    "ops_vs_lhp":          0.720,
    "obp_vs_lhp":          0.315,
    "slg_vs_lhp":          0.405,
    "ops_vs_rhp":          0.720,
    "obp_vs_rhp":          0.315,
    "slg_vs_rhp":          0.405,
    # PR3: opposing-pitcher context.  League-average neutrals — used when
    # the inference resolver can't find a probable starter for the prop's
    # game.  Real values come from a (date, batter_team) snapshot lookup.
    "opp_pitcher_szn_k_per_9":  8.50,
    "opp_pitcher_szn_era":      4.30,
    "opp_pitcher_throws_lhp":   0.30,   # ~30% of MLB starters are LHP
    "whiff_pct":           0.245,
    "chase_pct":           0.295,
    "hard_hit_rate":       0.370,
    "barrel_rate":         0.075,
    "sprint_speed":       27.0,
    "platoon_matchup_flag": 0.0,
    "weather_temp":        72.0,
    "weather_wind_speed":   8.0,
    "time_of_day":          1.0,
    "ba_vs_breaking":      0.235,
    "ba_vs_fastball":      0.265,
    "ba_vs_offspeed":      0.255,
    "h2h_career_ab":       12.0,
    "h2h_career_avg":       0.255,
    "h2h_career_k_rate":    0.220,
    "implied_total":        8.5,
}


def _log(msg: str) -> None:
    print(f"PROPS-MODEL: {msg}", flush=True, file=sys.stderr)


def _log_settle(msg: str) -> None:
    print(f"PROPS-SETTLE: {msg}", flush=True, file=sys.stderr)


# ── American odds -> implied probability (no-vig) ───────────────────────────

def _american_to_prob(american) -> float:
    """+150 -> 0.40, -110 -> ~0.524.  No-vig adjustment is left to the
    caller because over/under pairs let us de-vig in pairs more
    accurately than per-side."""
    try:
        v = int(american)
    except (TypeError, ValueError):
        return 0.5
    if v > 0:
        return 100.0 / (v + 100.0)
    return abs(v) / (abs(v) + 100.0)


def _no_vig_pair(over_odds, under_odds) -> tuple[float, float]:
    """Return (over_prob, under_prob) with the bookmaker juice removed.
    Defaults to (0.5, 0.5) when either side is missing."""
    if over_odds is None or under_odds is None:
        return 0.5, 0.5
    op = _american_to_prob(over_odds)
    up = _american_to_prob(under_odds)
    total = op + up
    if total <= 0:
        return 0.5, 0.5
    return op / total, up / total


# ── Joblib + Supabase sync helpers ──────────────────────────────────────────

class _LoadedModel:
    """Lazy joblib loader so an import-time read can't crash the
    process if scikit-learn / xgboost aren't available yet."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded: object | None = None
        self._tried: bool = False

    def load(self) -> object | None:
        if self._tried:
            return self._loaded
        self._tried = True
        if not self.path.exists():
            _log(f"joblib {self.path} not on disk -- predictor will use heuristic")
            return None
        try:
            import joblib  # type: ignore
            self._loaded = joblib.load(self.path)
            # Log the loaded object's class so the props_pitcher /
            # props_batter joblibs can be confirmed at runtime as the
            # CalibratedClassifierCV wrapper rather than a raw
            # XGBClassifier.  Audit during the 100%-confidence
            # investigation traced the issue to the confidence formula
            # (not calibration), but keeping this log line makes
            # future calibration regressions trivial to spot.
            cls_name = type(self._loaded).__name__
            extra = ""
            try:
                inner = getattr(self._loaded, "estimator", None) \
                        or getattr(self._loaded, "base_estimator", None)
                if inner is not None:
                    extra = f" (inner={type(inner).__name__})"
            except Exception:                                             # noqa: BLE001
                pass
            _log(f"joblib loaded {self.path}  cls={cls_name}{extra}")
        except Exception as exc:                                          # noqa: BLE001
            _log(f"joblib load failed for {self.path}: {exc}")
            self._loaded = None
        return self._loaded


_pitcher_model = _LoadedModel(PITCHER_MODEL_PATH)
_batter_model  = _LoadedModel(BATTER_MODEL_PATH)

# Regression model lazy-loaders keyed by stat name.
_pitcher_reg_models: dict[str, _LoadedModel] = {
    s: _LoadedModel(p) for s, p in _PITCHER_REG_PATHS.items()
}
_batter_reg_models: dict[str, _LoadedModel] = {
    s: _LoadedModel(p) for s, p in _BATTER_REG_PATHS.items()
}

# ── Regression metadata (feature names) ─────────────────────────────────────

_reg_meta_cache: Optional[dict] = None


def _load_reg_meta() -> dict:
    """Lazily load props_reg_metadata.json once per process."""
    global _reg_meta_cache
    if _reg_meta_cache is not None:
        return _reg_meta_cache
    if not _REG_META_PATH.exists():
        _reg_meta_cache = {}
        return {}
    try:
        _reg_meta_cache = json.loads(_REG_META_PATH.read_text(encoding="utf-8"))
        _log(f"reg metadata loaded: {len(_reg_meta_cache.get('pitcher_feature_names', []))} "
             f"pitcher feats, {len(_reg_meta_cache.get('batter_feature_names', []))} batter feats")
    except Exception as exc:                                               # noqa: BLE001
        _log(f"reg metadata load failed: {exc}")
        _reg_meta_cache = {}
    return _reg_meta_cache


# ── Team baselines cache (PR #2 opponent context) ───────────────────────────
# Loaded once per process; maps "<season>:<team_abbrev>" to {team_k_rate,
# team_woba, team_lhb_pct}.  Inference looks up by the opposing batting team
# for the prop's game so pitcher predictions account for opponent quality.

_team_baselines_cache:    Optional[dict[str, dict[str, float]]] = None
_team_baselines_year_max: Optional[int] = None


def _load_team_baselines() -> dict[str, dict[str, float]]:
    """Lazily load team_baselines.json once per process.  Returns {}
    when the file is absent — the predictor's per-row defaults then apply."""
    global _team_baselines_cache, _team_baselines_year_max
    if _team_baselines_cache is not None:
        return _team_baselines_cache
    if not _TEAM_BASELINES_PATH.exists():
        _team_baselines_cache = {}
        return {}
    try:
        payload = json.loads(_TEAM_BASELINES_PATH.read_text(encoding="utf-8"))
        baselines = payload.get("team_baselines") or {}
        _team_baselines_cache = baselines
        # Remember the most-recent season we have data for; inference uses
        # this when the prop's actual season is missing (e.g. spring training
        # or opening week before season-specific data exists).
        years: list[int] = []
        for k in baselines.keys():
            try:
                years.append(int(k.split(":", 1)[0]))
            except (TypeError, ValueError):
                continue
        _team_baselines_year_max = max(years) if years else None
        _log(f"team baselines loaded: {len(baselines)} (season, team) buckets, "
             f"latest_year={_team_baselines_year_max}")
    except Exception as exc:                                               # noqa: BLE001
        _log(f"team baselines load failed: {exc}")
        _team_baselines_cache = {}
    return _team_baselines_cache


def _lookup_team_baseline(team_abbrev: str, season: int) -> dict[str, float]:
    """Return {team_k_rate, team_woba, team_lhb_pct} for the given
    (season, team).  Falls back to the latest available season when the
    requested year is missing; returns an empty dict only when there's
    nothing usable at all."""
    if not team_abbrev:
        return {}
    baselines = _load_team_baselines()
    if not baselines:
        return {}
    key = f"{season}:{team_abbrev}"
    if key in baselines:
        return baselines[key]
    # Fall back to most-recent season for that team.
    if _team_baselines_year_max is not None:
        fb_key = f"{_team_baselines_year_max}:{team_abbrev}"
        if fb_key in baselines:
            return baselines[fb_key]
    return {}


# ── Pitcher snapshot cache ──────────────────────────────────────────────────
# Mirror of the batter snapshot system below.  Loaded once per process and
# indexed two ways:
#   _pitcher_snapshots_by_id:   str(player_id) -> snapshot dict
#   _pitcher_snapshots_by_name: lower(player_name) -> snapshot dict
# The by-name index lets predict() skip the MLB API name→ID lookup when the
# prop's player_name matches a training-set name exactly.

_pitcher_snapshot_cache: Optional[dict] = None
_pitcher_snapshots_by_id:   dict[str, dict] = {}
_pitcher_snapshots_by_name: dict[str, dict] = {}
_pitcher_league_medians:    dict[str, float] = {}


def _load_pitcher_snapshots() -> dict:
    """Lazily load pitcher_rolling_snapshots.json once per process."""
    global _pitcher_snapshot_cache, _pitcher_snapshots_by_id
    global _pitcher_snapshots_by_name, _pitcher_league_medians
    if _pitcher_snapshot_cache is not None:
        return _pitcher_snapshot_cache
    if not _PITCHER_SNAPSHOTS_PATH.exists():
        _pitcher_snapshot_cache = {}
        return {}
    try:
        payload = json.loads(_PITCHER_SNAPSHOTS_PATH.read_text(encoding="utf-8"))
        players = payload.get("players") or {}
        _pitcher_snapshots_by_id = {str(k): v for k, v in players.items()}
        _pitcher_snapshots_by_name = {
            (v.get("name") or "").strip().lower(): v
            for v in _pitcher_snapshots_by_id.values()
            if (v.get("name") or "").strip()
        }
        _pitcher_league_medians = dict(payload.get("league_medians") or {})
        _pitcher_snapshot_cache = payload
        _log(
            f"pitcher snapshots loaded: {len(_pitcher_snapshots_by_id)} players, "
            f"{len(_pitcher_league_medians)} league medians"
        )
    except Exception as exc:                                               # noqa: BLE001
        _log(f"pitcher snapshot load failed: {exc}")
        _pitcher_snapshot_cache = {}
    return _pitcher_snapshot_cache


def _lookup_pitcher_snapshot(prop: dict) -> Optional[dict]:
    """Return the snapshot for the prop's pitcher, or None.

    Resolution order:
      1. Direct name match against snapshot index (cheap, in-process).
      2. MLB Stats API name search via player_profile_client (cached
         per-process; falls back gracefully if the module isn't importable).
    """
    _load_pitcher_snapshots()
    name = (prop.get("player_name") or "").strip()
    if not name:
        return None
    snap = _pitcher_snapshots_by_name.get(name.lower())
    if snap is not None:
        return snap
    # Fall back to MLB ID lookup, then snapshot-by-id.
    try:
        from .player_profile_client import search_player_by_name
        pid = search_player_by_name(name)
        if pid is not None:
            return _pitcher_snapshots_by_id.get(str(pid))
    except Exception:                                                      # noqa: BLE001
        pass
    return None


# ── Batter snapshot cache ───────────────────────────────────────────────────
# Loaded once per process and indexed two ways:
#   _batter_snapshots_by_id:   str(player_id) -> snapshot dict
#   _batter_snapshots_by_name: lower(player_name) -> snapshot dict
# The by-name index lets predict() skip the MLB API name→ID lookup when the
# prop's player_name matches a training-set name exactly.

_batter_snapshot_cache: Optional[dict] = None
_batter_snapshots_by_id:   dict[str, dict] = {}
_batter_snapshots_by_name: dict[str, dict] = {}
_batter_league_medians:    dict[str, float] = {}


def _load_batter_snapshots() -> dict:
    """Lazily load batter_rolling_snapshots.json once per process."""
    global _batter_snapshot_cache, _batter_snapshots_by_id
    global _batter_snapshots_by_name, _batter_league_medians
    if _batter_snapshot_cache is not None:
        return _batter_snapshot_cache
    if not _BATTER_SNAPSHOTS_PATH.exists():
        _batter_snapshot_cache = {}
        return {}
    try:
        payload = json.loads(_BATTER_SNAPSHOTS_PATH.read_text(encoding="utf-8"))
        players = payload.get("players") or {}
        _batter_snapshots_by_id = {str(k): v for k, v in players.items()}
        _batter_snapshots_by_name = {
            (v.get("name") or "").strip().lower(): v
            for v in _batter_snapshots_by_id.values()
            if (v.get("name") or "").strip()
        }
        _batter_league_medians = dict(payload.get("league_medians") or {})
        _batter_snapshot_cache = payload
        _log(
            f"batter snapshots loaded: {len(_batter_snapshots_by_id)} players, "
            f"{len(_batter_league_medians)} league medians"
        )
    except Exception as exc:                                               # noqa: BLE001
        _log(f"batter snapshot load failed: {exc}")
        _batter_snapshot_cache = {}
    return _batter_snapshot_cache


def _lookup_batter_snapshot(prop: dict) -> Optional[dict]:
    """Return the snapshot for the prop's batter, or None.

    Resolution order:
      1. Direct name match against snapshot index (cheap, in-process).
      2. MLB Stats API name search via player_profile_client (cached
         per-process; falls back gracefully if the module isn't importable).
    """
    _load_batter_snapshots()
    name = (prop.get("player_name") or "").strip()
    if not name:
        return None
    snap = _batter_snapshots_by_name.get(name.lower())
    if snap is not None:
        return snap
    # Fall back to MLB ID lookup, then snapshot-by-id.
    try:
        from .player_profile_client import search_player_by_name
        pid = search_player_by_name(name)
        if pid is not None:
            return _batter_snapshots_by_id.get(str(pid))
    except Exception:                                                      # noqa: BLE001
        pass
    return None


def _resolve_and_apply_opp_pitcher(
    prop: dict,
    vec: list[float],
    fn_idx: dict[str, int],
    *,
    snap: dict,
) -> None:
    """For batter props: resolve the opposing pitcher from the day's
    probable-starters schedule and stamp opp_pitcher_szn_k_per_9 /
    opp_pitcher_szn_era / opp_pitcher_throws_lhp into *vec* in-place.

    Resolution chain:
      1. Batter's team from their snapshot dict (`snap["team"]`, captured
         at training time).  Required to know which side of the matchup
         we're looking for the opposing starter on.
      2. Daily schedule via PitcherClient._get_schedule(date).  The
         schedule lists home_pitcher + away_pitcher per game with
         {fullName, id, pitchHand}.  Cached on disk by the client so this
         is in-memory after the first slate view.
      3. opp_pitcher = whichever side has team != batter's team.
      4. opp pitcher's snapshot (pitcher_rolling_snapshots) gives
         szn_k_per_9; szn_ER and szn_IP combine into ERA.

    Silently returns without modifying *vec* when any step misses; the
    league-average defaults already in *vec* (from _BATTER_DEFAULTS) are
    the correct fallback.  We don't want a missing schedule fetch to
    crash the predictor.
    """
    # Quick exit if the model wasn't trained with these features.
    feat_keys = ("opp_pitcher_szn_k_per_9", "opp_pitcher_szn_era", "opp_pitcher_throws_lhp")
    if not any(k in fn_idx for k in feat_keys):
        return

    # 1. Batter team from snapshot — required to pick the opposing side.
    batter_team = ((snap or {}).get("team") or "").strip().upper()
    if not batter_team:
        return  # let defaults stand

    # 2. Fetch daily schedule (cached on disk; in-memory after first call)
    commence = (prop.get("commence_time") or "").strip()
    if not commence:
        return
    date_str = commence[:10]
    schedule: list[dict] = []
    try:
        from .pitcher_client import get_pitcher_client
        schedule = get_pitcher_client()._get_schedule(date_str) or []  # noqa: SLF001
    except Exception:                                                          # noqa: BLE001
        return

    # 3. Find the game involving batter_team; opp_pitcher is the side != batter_team.
    opp_pid: Optional[int] = None
    opp_throws: Optional[str] = None
    for entry in schedule:
        home_team = (entry.get("home_team_abbr") or entry.get("home_team") or "").strip().upper()
        away_team = (entry.get("away_team_abbr") or entry.get("away_team") or "").strip().upper()
        if batter_team not in (home_team, away_team):
            continue
        if batter_team == home_team:
            opp = entry.get("away_pitcher") or {}
        else:
            opp = entry.get("home_pitcher") or {}
        opp_pid    = int(opp.get("id") or 0) or None
        opp_throws = ((opp.get("pitchHand") or {}).get("code") or "").strip().upper() \
                     if isinstance(opp.get("pitchHand"), dict) \
                     else (opp.get("pitchHand") or "").strip().upper()
        break

    # 4. Pull pitcher snapshot's szn_k_per_9 / szn_ER / szn_IP for k_per_9 + ERA
    if opp_pid is not None:
        try:
            _load_pitcher_snapshots()
            opp_snap = _pitcher_snapshots_by_id.get(str(opp_pid)) or {}
            opp_feats = (opp_snap.get("features") or {}) if isinstance(opp_snap, dict) else {}
        except Exception:                                                      # noqa: BLE001
            opp_feats = {}

        k9 = opp_feats.get("szn_k_per_9")
        if k9 is not None and "opp_pitcher_szn_k_per_9" in fn_idx:
            vec[fn_idx["opp_pitcher_szn_k_per_9"]] = float(k9)

        # ERA = szn_ER per game * 9 / szn_IP per game (both are rolling means).
        szn_er = opp_feats.get("szn_ER")
        szn_ip = opp_feats.get("szn_IP")
        if (
            szn_er is not None and szn_ip is not None
            and float(szn_ip) > 0.01
            and "opp_pitcher_szn_era" in fn_idx
        ):
            vec[fn_idx["opp_pitcher_szn_era"]] = float(szn_er) * 9.0 / float(szn_ip)

    # Handedness — independent of snapshot; the schedule itself carries it.
    if opp_throws in ("L", "R") and "opp_pitcher_throws_lhp" in fn_idx:
        vec[fn_idx["opp_pitcher_throws_lhp"]] = 1.0 if opp_throws == "L" else 0.0


def _build_reg_vector(prop: dict, bucket: str) -> tuple[list[float], list[str]]:
    """Build a full-length feature vector for a regression or classifier call.

    Preference order for feature names:
      1. props_reg_metadata.json saved at training time (same process-lifetime
         cache as before)
      2. Hardcoded _PITCHER_FEATURE_NAMES / _BATTER_FEATURE_NAMES constants
         (always available, never requires a file on disk)

    Batter rolling stats (bucket == "batter"):
      Look up the batter's snapshot (latest engineered training row) by
      player_name → MLB ID and fill every szn_*/r7_*/r14_* feature with
      the snapshot's real value.  League-median fallback when the player
      has no snapshot (rookies / mid-season call-ups).  The old behaviour
      of stamping the prop line into every rolling slot was physically
      incoherent and made the model effectively blind at inference time.

    Pitcher rolling stats (bucket == "pitcher"):
      Same pattern as batter — snapshot lookup, then league median.  For
      pitchers not in the snapshot (rookies, mid-season debuts, or a
      Railway redeploy that lost the JSON before Supabase restore) the
      live-fetch helper in pitcher_inference_features.py hits MLB Stats
      API for fresh rolling stats as a last-ditch fallback before
      falling through to league-average defaults.  We never use the
      prop line as a proxy for pitcher rolling stats — the same
      argument that broke it for batters breaks it for pitchers.

    Always returns (vector, feature_names) — never (None, None).
    """
    # Prefer metadata from training file; fall back to hardcoded constants.
    meta = _load_reg_meta()
    fn_key = f"{bucket}_feature_names"
    feature_names: list[str] = (
        meta.get(fn_key)
        or (_PITCHER_FEATURE_NAMES if bucket == "pitcher" else _BATTER_FEATURE_NAMES)
    )

    defaults = _PITCHER_DEFAULTS if bucket == "pitcher" else _BATTER_DEFAULTS

    fn_idx = {name: i for i, name in enumerate(feature_names)}
    vec    = [0.0] * len(feature_names)

    line = float(prop.get("line") or 0.0)

    snapshot_feats: dict[str, float] = {}
    league_medians: dict[str, float] = {}
    snap_source = "none"
    if bucket == "batter":
        snap = _lookup_batter_snapshot(prop)
        if isinstance(snap, dict):
            snapshot_feats = snap.get("features") or {}
            snap_source = "player"
        league_medians = _batter_league_medians or {}
        if not snapshot_feats and league_medians:
            snap_source = "league_median"
    elif bucket == "pitcher":
        snap = _lookup_pitcher_snapshot(prop)
        if isinstance(snap, dict):
            snapshot_feats = snap.get("features") or {}
            snap_source = "player"
        league_medians = _pitcher_league_medians or {}
        if not snapshot_feats:
            # Snapshot miss -- try the live MLB Stats API enrichment for
            # rookies / mid-season debuts / post-retrain pitchers that
            # haven't made it into the snapshot yet.  Cached on disk so
            # the same pitcher isn't refetched per prop.
            try:
                from . import pitcher_inference_features as _pif  # noqa: PLC0415
                live_feats = _pif.enrich_pitcher_features(prop) or {}
                if live_feats:
                    snapshot_feats = live_feats
                    snap_source = "live_api"
            except Exception as exc:                                          # noqa: BLE001
                _log(f"live pitcher enrichment failed: {exc}")
        if not snapshot_feats and league_medians:
            snap_source = "league_median"

    # Rolling/season averages.
    # Both buckets: snapshot value, then league median, then line as last-ditch
    # fallback.  Line-as-proxy is only used when EVERY other source is missing.
    for fname in feature_names:
        if not fname.startswith(("szn_", "r7_", "r14_")):
            continue
        idx = fn_idx.get(fname)
        if idx is None:
            continue
        if fname in snapshot_feats:
            vec[idx] = float(snapshot_feats[fname])
        elif fname in league_medians:
            vec[idx] = float(league_medians[fname])
        else:
            vec[idx] = line

    # Pull non-rolling features from the snapshot when available (platoon
    # splits, BABIP, k_pct, batting_order, days_since_last_start, ip_last_30d,
    # is_home_i, ...).  These are more accurate than league-average defaults.
    if snapshot_feats:
        for fname, val in snapshot_feats.items():
            idx = fn_idx.get(fname)
            if idx is not None and vec[idx] == 0.0:
                vec[idx] = float(val)

    # is_home_i — prefer the prop payload's matchup-specific value when set,
    # otherwise leave whatever the snapshot wrote.  (Snapshot's is_home is
    # captured from the pitcher's last training-set start; for live props
    # the prop dict's is_home is authoritative.)
    if "is_home_i" in fn_idx and prop.get("is_home") is not None:
        vec[fn_idx["is_home_i"]] = float(bool(prop.get("is_home")))

    # Park factors — home_team drives the ballpark.  Odds API passes
    # full team names ("New York Yankees") so we normalize through the
    # TEAM_NAME_TO_ABBREV map before hitting the 3-letter tables.
    park_team = _team_to_abbrev(prop.get("home_team") or "")
    if "ballpark_factor_k" in fn_idx:
        vec[fn_idx["ballpark_factor_k"]] = _PARK_K.get(park_team, 1.0)
    if "ballpark_factor_hits" in fn_idx:
        vec[fn_idx["ballpark_factor_hits"]] = _PARK_H.get(park_team, 1.0)
    if "ballpark_factor_hr" in fn_idx:
        vec[fn_idx["ballpark_factor_hr"]] = _PARK_HR.get(park_team, 1.0)

    # ── PR3 opp-pitcher lookup (batter only) ──────────────────────────────
    # For a batter prop, the opposing pitcher is the probable starter for
    # whichever team ISN'T the batter's team.  Resolution path:
    #   1. Batter's team from their snapshot (snap_obj.team).
    #   2. Probable starters from PitcherClient's daily schedule cache
    #      (already populated for the prop's commence_time when the slate
    #      has been viewed).
    #   3. Match by team -> opp_pitcher_id + handedness.
    #   4. opp_pitcher_szn_k_per_9 / _era from pitcher_rolling_snapshots.
    #
    # Falls through to _BATTER_DEFAULTS neutrals when any step fails, so
    # missing schedule data degrades gracefully rather than crashing.
    if bucket == "batter":
        try:
            _resolve_and_apply_opp_pitcher(prop, vec, fn_idx, snap=snapshot_feats)
        except Exception as exc:                                              # noqa: BLE001
            _log(f"opp_pitcher resolution failed: {exc}")

    # ── PR #2 opponent-context lookup (pitcher only) ─────────────────────────
    # The opposing batting team is whichever side ISN'T the pitcher's team.
    # The pitcher's team comes from their snapshot; if missing, infer from
    # is_home + home_team/away_team.  Season comes from commence_time year.
    opp_lookup_done = False
    if bucket == "pitcher":
        # Determine pitcher's own team
        pitcher_team_raw = ""
        if isinstance(snap_source, str) and snap_source == "player":
            snap_obj = _lookup_pitcher_snapshot(prop) or {}
            pitcher_team_raw = (snap_obj.get("team") or "").strip().upper()
        # Resolve opp_team: the side that isn't the pitcher's team.
        home_abbrev = _team_to_abbrev(prop.get("home_team") or "")
        away_abbrev = _team_to_abbrev(prop.get("away_team") or "")
        opp_abbrev = ""
        if pitcher_team_raw and pitcher_team_raw in (home_abbrev, away_abbrev):
            opp_abbrev = away_abbrev if pitcher_team_raw == home_abbrev else home_abbrev
        elif prop.get("is_home") is True:
            opp_abbrev = away_abbrev
        elif prop.get("is_home") is False:
            opp_abbrev = home_abbrev
        # Season from commence_time
        commence = (prop.get("commence_time") or "").strip()
        try:
            season = int(commence[:4]) if commence else 0
        except (TypeError, ValueError):
            season = 0
        opp_stats = _lookup_team_baseline(opp_abbrev, season) if opp_abbrev else {}
        if opp_stats:
            opp_lookup_done = True
            for src_key, feat_key in (
                ("team_k_rate",  "opp_team_k_rate"),
                ("team_woba",    "opp_team_woba"),
                ("team_lhb_pct", "opp_team_lhb_pct"),
            ):
                idx = fn_idx.get(feat_key)
                if idx is not None and src_key in opp_stats:
                    vec[idx] = float(opp_stats[src_key])

    # Fill any remaining holes from league-average neutral defaults.
    for fname, default_val in defaults.items():
        idx = fn_idx.get(fname)
        if idx is not None and vec[idx] == 0.0:
            vec[idx] = default_val

    # NOTE: previously a per-call _log line announced
    #   "build_reg_vector batter: name='Aaron Judge' snapshot=... opp_baseline=..."
    # for every prop scored.  At ~3000 props per Tier-1 refresh that
    # exceeded Railway's 500 logs/sec limit and crowded out every other
    # signal.  The summary line at the end of each scoring pass (in
    # pages/props.py: "[PROPS-PAGE] ... scored N picks") is sufficient
    # to debug aggregate behaviour, and failure paths in this function
    # still log individually.

    return vec, feature_names


def restore_models_from_supabase() -> dict:
    """Mirror of the existing model joblib restore (see app.py boot
    flow).  Pulls the two props joblibs from Supabase app_cache when
    the local files are missing.  Idempotent.

    Returns a small status dict the boot health report can include.
    """
    out: dict = {}
    try:
        from . import db as _db
        if not _db.is_supabase():
            _log("Supabase off -- skipping joblib restore")
            return {"supabase": False}
    except Exception:                                                     # noqa: BLE001
        return {"supabase": False}

    import base64

    # Restore joblib model files (base64-encoded bytes in data["b64"]).
    all_pairs = [
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    ] + [
        (f"props_model_pitcher_reg_{s}", p)
        for s, p in _PITCHER_REG_PATHS.items()
    ] + [
        (f"props_model_batter_reg_{s}", p)
        for s, p in _BATTER_REG_PATHS.items()
    ]
    for key, path in all_pairs:
        if path.exists():
            out[key] = "local"
            continue
        try:
            from . import db as _db
            row = _db.cache_get(key)
            if not isinstance(row, dict):
                out[key] = "missing"
                _log(f"restore {key}: no Supabase row -- predictor will use heuristic")
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else row
            # Supabase stores the joblib bytes base64-encoded in data["b64"].
            b64 = (data or {}).get("b64")
            if not b64:
                out[key] = "no_b64"
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(b64))
            out[key] = "restored"
            _log(f"restore {key}: wrote {path} ({path.stat().st_size} bytes)")
        except Exception as exc:                                          # noqa: BLE001
            out[key] = f"error: {exc}"
            _log(f"restore {key} failed: {exc}")

    # Restore props_reg_metadata.json (stored as JSON in data["json"]).
    if not _REG_META_PATH.exists():
        try:
            from . import db as _db
            row = _db.cache_get("props_reg_metadata")
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                raw_json = (data or {}).get("json")
                if raw_json:
                    _REG_META_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _REG_META_PATH.write_text(
                        raw_json if isinstance(raw_json, str) else json.dumps(raw_json),
                        encoding="utf-8",
                    )
                    out["props_reg_metadata"] = "restored"
                    _log(f"restore props_reg_metadata: wrote {_REG_META_PATH}")
                else:
                    out["props_reg_metadata"] = "no_json"
            else:
                out["props_reg_metadata"] = "missing"
        except Exception as exc:                                          # noqa: BLE001
            out["props_reg_metadata"] = f"error: {exc}"
            _log(f"restore props_reg_metadata failed: {exc}")

    # Restore pitcher_rolling_snapshots.json -- same wire format as the
    # batter snapshot below (base64-encoded JSON pushed by training).
    if not _PITCHER_SNAPSHOTS_PATH.exists():
        try:
            from . import db as _db
            row = _db.cache_get("pitcher_rolling_snapshots")
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                b64 = (data or {}).get("b64")
                if b64:
                    _PITCHER_SNAPSHOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _PITCHER_SNAPSHOTS_PATH.write_bytes(base64.b64decode(b64))
                    out["pitcher_rolling_snapshots"] = "restored"
                    _log(
                        f"restore pitcher_rolling_snapshots: wrote "
                        f"{_PITCHER_SNAPSHOTS_PATH} "
                        f"({_PITCHER_SNAPSHOTS_PATH.stat().st_size} bytes)"
                    )
                else:
                    out["pitcher_rolling_snapshots"] = "no_b64"
            else:
                out["pitcher_rolling_snapshots"] = "missing"
        except Exception as exc:                                          # noqa: BLE001
            out["pitcher_rolling_snapshots"] = f"error: {exc}"
            _log(f"restore pitcher_rolling_snapshots failed: {exc}")

    # Restore team_baselines.json (PR #2 opponent-context lookup).
    # Wire format is identical to the snapshot files above (base64).
    if not _TEAM_BASELINES_PATH.exists():
        try:
            from . import db as _db
            row = _db.cache_get("team_baselines")
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                b64 = (data or {}).get("b64")
                if b64:
                    _TEAM_BASELINES_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _TEAM_BASELINES_PATH.write_bytes(base64.b64decode(b64))
                    out["team_baselines"] = "restored"
                    _log(
                        f"restore team_baselines: wrote {_TEAM_BASELINES_PATH} "
                        f"({_TEAM_BASELINES_PATH.stat().st_size} bytes)"
                    )
                else:
                    out["team_baselines"] = "no_b64"
            else:
                out["team_baselines"] = "missing"
        except Exception as exc:                                          # noqa: BLE001
            out["team_baselines"] = f"error: {exc}"
            _log(f"restore team_baselines failed: {exc}")

    # Restore batter_rolling_snapshots.json (training-script pushes it base64-
    # encoded alongside the joblibs, so the same b64 unwrap works).
    if not _BATTER_SNAPSHOTS_PATH.exists():
        try:
            from . import db as _db
            row = _db.cache_get("batter_rolling_snapshots")
            if isinstance(row, dict):
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                b64 = (data or {}).get("b64")
                if b64:
                    _BATTER_SNAPSHOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _BATTER_SNAPSHOTS_PATH.write_bytes(base64.b64decode(b64))
                    out["batter_rolling_snapshots"] = "restored"
                    _log(
                        f"restore batter_rolling_snapshots: wrote "
                        f"{_BATTER_SNAPSHOTS_PATH} "
                        f"({_BATTER_SNAPSHOTS_PATH.stat().st_size} bytes)"
                    )
                else:
                    out["batter_rolling_snapshots"] = "no_b64"
            else:
                out["batter_rolling_snapshots"] = "missing"
        except Exception as exc:                                          # noqa: BLE001
            out["batter_rolling_snapshots"] = f"error: {exc}"
            _log(f"restore batter_rolling_snapshots failed: {exc}")

    return out


def push_models_to_supabase() -> dict:
    """Counterpart of restore_models_from_supabase.  Called by the
    training script after a fresh joblib is written so the next worker
    boot can pull it down."""
    out: dict = {}
    try:
        from . import db as _db
        if not _db.is_supabase():
            return {"supabase": False}
    except Exception:                                                     # noqa: BLE001
        return {"supabase": False}

    import base64
    all_pairs = [
        ("props_model_pitcher", PITCHER_MODEL_PATH),
        ("props_model_batter",  BATTER_MODEL_PATH),
    ] + [
        (f"props_model_pitcher_reg_{s}", p)
        for s, p in _PITCHER_REG_PATHS.items()
    ] + [
        (f"props_model_batter_reg_{s}", p)
        for s, p in _BATTER_REG_PATHS.items()
    ]
    for key, path in all_pairs:
        if not path.exists():
            out[key] = "no_local_file"
            continue
        try:
            from . import db as _db
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            _db.cache_set(key, None, "models", {"b64": b64})
            out[key] = "pushed"
            _log(f"push {key}: uploaded {path} ({path.stat().st_size} bytes)")
        except Exception as exc:                                          # noqa: BLE001
            out[key] = f"error: {exc}"
            _log(f"push {key} failed: {exc}")

    # Push props_reg_metadata.json so cold boots can restore it.
    if _REG_META_PATH.exists():
        try:
            from . import db as _db
            raw = _REG_META_PATH.read_text(encoding="utf-8")
            _db.cache_set("props_reg_metadata", None, "models", {"json": raw})
            out["props_reg_metadata"] = "pushed"
            _log(f"push props_reg_metadata: uploaded {_REG_META_PATH}")
        except Exception as exc:                                          # noqa: BLE001
            out["props_reg_metadata"] = f"error: {exc}"
            _log(f"push props_reg_metadata failed: {exc}")

    # Push team_baselines.json (PR #2 opponent context).
    if _TEAM_BASELINES_PATH.exists():
        try:
            from . import db as _db
            b64 = base64.b64encode(_TEAM_BASELINES_PATH.read_bytes()).decode("ascii")
            _db.cache_set("team_baselines", None, "models", {"b64": b64})
            out["team_baselines"] = "pushed"
            _log(f"push team_baselines: uploaded {_TEAM_BASELINES_PATH}")
        except Exception as exc:                                          # noqa: BLE001
            out["team_baselines"] = f"error: {exc}"
            _log(f"push team_baselines failed: {exc}")

    # Push pitcher_rolling_snapshots.json -- same wire format as batter.
    if _PITCHER_SNAPSHOTS_PATH.exists():
        try:
            from . import db as _db
            b64 = base64.b64encode(_PITCHER_SNAPSHOTS_PATH.read_bytes()).decode("ascii")
            _db.cache_set("pitcher_rolling_snapshots", None, "models", {"b64": b64})
            out["pitcher_rolling_snapshots"] = "pushed"
            _log(f"push pitcher_rolling_snapshots: uploaded {_PITCHER_SNAPSHOTS_PATH}")
        except Exception as exc:                                          # noqa: BLE001
            out["pitcher_rolling_snapshots"] = f"error: {exc}"
            _log(f"push pitcher_rolling_snapshots failed: {exc}")

    # Push batter_rolling_snapshots.json as base64 so restore matches the
    # joblib code path.  Snapshots are small (~200 KB) so encoding overhead
    # is fine.
    if _BATTER_SNAPSHOTS_PATH.exists():
        try:
            from . import db as _db
            b64 = base64.b64encode(_BATTER_SNAPSHOTS_PATH.read_bytes()).decode("ascii")
            _db.cache_set("batter_rolling_snapshots", None, "models", {"b64": b64})
            out["batter_rolling_snapshots"] = "pushed"
            _log(f"push batter_rolling_snapshots: uploaded {_BATTER_SNAPSHOTS_PATH}")
        except Exception as exc:                                          # noqa: BLE001
            out["batter_rolling_snapshots"] = f"error: {exc}"
            _log(f"push batter_rolling_snapshots failed: {exc}")

    return out


# ── Prediction API ──────────────────────────────────────────────────────────

# PR: removed the 0.10/0.90 probability squash and the 0.85 confidence cap.
# These were belt-and-suspenders guardrails put in place before the
# CalibratedClassifierCV wrapper was reliable -- they prevented the model
# from ever expressing high conviction even when the isotonic-calibrated
# probability legitimately said so.  With retrained PR3 artifacts where
# every saved joblib IS the calibrated wrapper (verified in
# scripts/train_props_models.py:1900: `joblib.dump(calibrated, out_path)`),
# the calibration map already enforces realistic frequencies and the
# extra clamping just truncated the tails of the distribution.
#
# Constants kept for backward compat with any external caller / test that
# imports them; _squash_prob is now a pass-through so removing the
# function entirely doesn't break those.
_PROB_LO: float = 0.0    # was 0.10 -- no lower clamp
_PROB_HI: float = 1.0    # was 0.90 -- no upper clamp
_CONF_CAP: float = 1.0   # was 0.85 -- displayed confidence can saturate at 1.0


def _squash_prob(p: float) -> float:
    """PR no-op shim.  Previously clamped to [_PROB_LO, _PROB_HI] = [0.10,
    0.90] to dampen XGBoost's tendency to push raw probabilities to the
    extremes.  CalibratedClassifierCV (isotonic, baked into PR3 artifacts)
    now does that natively -- this function passes the input through so
    confidence values reflect real statistical calibration rather than an
    artificial floor / ceiling.  Kept (rather than deleted) so external
    callers don't break; predict() / predict_pair() no longer call it.
    """
    return float(p)


def _poisson_over_prob(lam: float, line: float) -> float:
    """P(X > line) where X ~ Poisson(lam).

    The principled way to convert a count-regressor's mean prediction into
    an over-probability for a sportsbook line.  Half-integer lines (1.5,
    2.5, ...) are unambiguous.  Whole-integer lines return P(X > line) —
    pushes at X == line are excluded, matching how sportsbooks settle
    (push is a separate outcome from over/under).

    Args:
      lam:  Poisson mean (= regressor's predicted_value).
      line: Sportsbook line.

    Returns:
      Probability in [0, 1].  Returns 0.0 when lam <= 0.
    """
    if lam <= 0:
        return 0.0
    import math
    k = int(math.floor(line))
    # P(X <= k) accumulated term-by-term to avoid factorial overflow.
    # term[i] = term[i-1] * lam / i; seed term[0] = exp(-lam).
    term = math.exp(-lam)
    cdf  = term
    for i in range(1, k + 1):
        term *= lam / i
        cdf  += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _compute_raw_over_prob(
    prop: dict, bucket: str, *, predicted_value: Optional[float] = None,
) -> tuple[float, str]:
    """Return (raw_over_prob, source) before squashing or side-flip.

    PR2 fix for the "classifier P(H>=1) used as over_prob for every batter
    market" bug.  For batter markets with a working regressor: convert the
    predicted_value (= Poisson mean) into P(over) via _poisson_over_prob so
    each market gets its own line-aware probability.  For pitcher markets
    and the rare batter case where the regressor doesn't return a value:
    fall back to the classifier's raw predict_proba output.

    Args:
      prop: The prop dict.
      bucket: "batter" or "pitcher".
      predicted_value: Optional pre-computed regressor mean.  Pass this in
        when the caller already ran the regressor (e.g. predict_pair) so
        we don't double-run it.

    Returns:
      (raw_p, source) where source is "poisson", "joblib", or "heuristic".
    """
    raw_p  = _american_to_prob(prop.get("best_odds"))
    source = "heuristic"

    # ── Poisson PMF path (batter markets with a regressor) ───────────────
    if bucket == "batter":
        pv = predicted_value if predicted_value is not None else _run_regressor(prop, bucket)
        if pv is not None:
            try:
                line = float(prop.get("line") or 0.0)
                raw_p  = _poisson_over_prob(float(pv), line)
                source = "poisson"
                return raw_p, source
            except (TypeError, ValueError) as exc:
                _log(f"poisson over_prob failed for {prop.get('market')}: {exc} -- classifier")

    # ── Classifier path (pitcher, or batter fallback) ────────────────────
    model = (_pitcher_model if bucket == "pitcher" else _batter_model).load()
    if model is not None:
        try:
            import numpy as np  # noqa: PLC0415
            vec, _ = _build_reg_vector(prop, bucket)
            X = np.array([vec], dtype=float)
            if hasattr(model, "predict_proba"):
                proba = model.predict_proba(X)[0]
                raw_p = float(proba[1]) if len(proba) > 1 else float(proba[0])
            else:
                raw_p = float(model.predict(X)[0])
            source = "joblib"
        except Exception as exc:                                              # noqa: BLE001
            _log(f"joblib predict failed for {bucket}: {exc} -- heuristic")

    return raw_p, source


def _bucket_for_market(market: str) -> str:
    return "pitcher" if (market or "").startswith("pitcher_") else "batter"


def _run_regressor(prop: dict, bucket: str) -> "Optional[float]":
    """Run the per-stat regression model and return a predicted numeric value,
    or None when no regressor is available for this market."""
    predicted_value: Optional[float] = None
    reg_info = _MARKET_REG_KEY.get(prop.get("market", ""))
    if reg_info is None:
        return None
    reg_bucket, reg_stat = reg_info
    reg_loaders = _pitcher_reg_models if reg_bucket == "pitcher" else _batter_reg_models
    reg_model = reg_loaders.get(reg_stat, _LoadedModel(Path("_nonexistent_"))).load()
    if reg_model is None:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        vec, _ = _build_reg_vector(prop, reg_bucket)
        X_reg = np.array([vec], dtype=float)
        predicted_value = round(float(reg_model.predict(X_reg)[0]), 2)
    except Exception as exc:                                                # noqa: BLE001
        _log(f"regression predict failed for {prop.get('market')}: {exc}")
    return predicted_value


def _feature_vector_for_prop(prop: dict) -> list[float]:
    """Minimal feature vector used by the joblib classifiers.  Picks up
    only the fields available in the prop payload (line, odds, no-vig
    market prob).  When a richer feature set is wanted, extend this
    function AND the training script's matching builder."""
    line = float(prop.get("line") or 0.0)
    odds = float(prop.get("best_odds") or -110)
    market_prob = _american_to_prob(odds)
    # All_books spread = best - worst odds.  Wider spreads mean more
    # disagreement among books = noisier line.
    book_odds = [
        float(b.get("odds"))
        for b in (prop.get("all_books") or [])
        if isinstance(b.get("odds"), (int, float))
    ]
    spread = (max(book_odds) - min(book_odds)) if book_odds else 0.0
    return [line, odds, market_prob, spread, float(len(book_odds))]


def predict(prop: dict) -> dict:
    """Return the model's call for a single prop.

    Output shape:
        {
          recommendation:  "Over" | "Under" | "Pass",
          confidence:      float,          # 0..1, no artificial cap
          model_prob:      float,          # calibrated P(this side)
          market_prob:     float,          # de-vigged P(this side) from the line
          edge:            float,          # model_prob - market_prob, signed
          source:          "joblib" | "heuristic",
          predicted_value: float | None,   # numeric stat prediction (regressor)
        }

    Probability calibration
    -----------------------
    Raw XGBoost probabilities are no longer squashed to [0.10, 0.90].
    The retrained PR3 artifacts save the CalibratedClassifierCV wrapper
    (see scripts/train_props_models.py:1900) and isotonic calibration is
    what we trust for realistic frequencies now.  The previous 0.85
    confidence cap is also gone -- displayed confidence can saturate at
    1.0 when the calibrated probability + de-vigged market disagree
    that strongly.

    Side symmetry
    -------------
    P(Over) is computed once and the Under side complements it, so
    predict(over_prop).model_prob + predict(under_prop).model_prob == 1.0
    exactly when both props share the same underlying line.  Use
    predict_pair() to make this invariant explicit and avoid a second
    model call.
    """
    bucket = _bucket_for_market(prop.get("market", ""))
    # market_prob = _american_to_prob(this side's odds).  Already aligned
    # to the prop's side: for Over odds it's P_market(over wins), for
    # Under odds it's P_market(under wins).  No flip below.
    market_prob = _american_to_prob(prop.get("best_odds"))

    # Compute regressor predicted_value once so we don't run it twice
    # (Poisson over_prob path + the predicted_value field in the result).
    predicted_value = _run_regressor(prop, bucket)

    # PR2: batter markets now derive over_prob from the regressor's mean via
    # Poisson PMF, so each line (0.5, 1.5, 2.5) gets a market-correct
    # probability rather than the classifier's P(H>=1) for every market.
    # Pitcher markets and batter-no-regressor cases still use the classifier.
    raw_p, source = _compute_raw_over_prob(prop, bucket, predicted_value=predicted_value)

    # No squash -- raw P(Over) from the calibrated classifier (or
    # Poisson-PMF regressor) is what we use.  Clamp only to [0, 1] in
    # case the calibrator extrapolated slightly outside that range
    # (isotonic regression can do this at the boundaries).
    over_prob = max(0.0, min(1.0, float(raw_p)))

    # Align both probabilities to the prop's actual side BEFORE computing
    # edge.  Bug-fix history:
    #   v1: flipped both over_prob AND market_prob on Under-side.  Wrong --
    #       market_prob was already this-side from _american_to_prob, so the
    #       second flip corrupted it to the OTHER side.
    #   v2 (now): only flip over_prob (P_model from "over" -> "this side").
    #       market_prob stays as-is.  Edge is now P_model(this) - P_market(this)
    #       and is correctly signed in BOTH branches.
    side = (prop.get("side") or "Over").strip().title()
    if side == "Under":
        over_prob = 1.0 - over_prob   # now = P_model(under)

    edge = over_prob - market_prob

    # Positive edge -> model favors THIS side -> bet THIS side.  Old code
    # hardcoded "Over"/"Under" labels here, which left Under-side picks
    # with recommendation="Over" when their edge was positive (and vice
    # versa) -- the visible cross-page side flip on /props vs
    # /player/<id>.
    other_side = "Under" if side == "Over" else "Over"
    if   edge >  0.03: recommendation = side
    elif edge < -0.03: recommendation = other_side
    else:               recommendation = "Pass"

    # Confidence = the model's natural win probability for the side it
    # actually recommends (clamped to [0.50, 1.0]).
    #
    # Why we changed the formula
    # --------------------------
    # The previous ``abs(edge) * 2.0 + 0.50`` formula saturated at 1.0
    # whenever the model-market gap reached 0.25, which a calibrated
    # isotonic classifier hits on most balanced-juice props.  The slate
    # ended up dominated by 100% picks even when the model's actual win
    # probability was 65-75% -- misleading on the card and silently
    # inflating EV calcs (which feed off this same number).
    #
    # New formula uses ``over_prob`` after the flip, which is
    # P_model(this side).  When the model recommends THIS side, that's
    # the win probability of the pick we're showing.  When the model
    # recommends the OTHER side, the recommended-side probability is
    # 1 - over_prob -- the dedup in score_today_props prefers entries
    # where side==recommendation so the displayed confidence is the
    # recommended-side probability in either case.  Floor at 0.50 so a
    # "Pass" with near-coin-flip probability still looks reasonable on
    # the small admin overview tooltip.
    if recommendation == side:
        confidence = over_prob
    elif recommendation == other_side:
        confidence = 1.0 - over_prob
    else:  # Pass -- show whichever side has the higher model_prob
        confidence = max(over_prob, 1.0 - over_prob)
    confidence = max(0.50, min(1.0, confidence))

    return {
        "recommendation":  recommendation,
        "confidence":      round(confidence, 4),
        "model_prob":      round(over_prob, 4),
        "market_prob":     round(market_prob, 4),
        "edge":            round(edge, 4),
        "source":          source,
        "predicted_value": predicted_value,
    }


def predict_pair(over_prop: dict, under_prop: dict) -> tuple[dict, dict]:
    """Score both sides of a prop with a single model call.

    Guarantees over_result["model_prob"] + under_result["model_prob"] == 1.0
    exactly — the Under result is derived by complementing the Over probability,
    not by an independent model call.

    Market probabilities are de-vigged per-side using each prop's own
    best_odds so the market_prob pair also sums to 1.0 correctly.

    Use this instead of two separate predict() calls whenever both sides of
    the same line are available.
    """
    bucket = _bucket_for_market(
        over_prop.get("market") or under_prop.get("market") or ""
    )

    # ── Regression (shared; keyed off the Over prop) — run FIRST so the ──
    # Poisson PMF path in _compute_raw_over_prob can reuse the result.
    predicted_value = _run_regressor(over_prop, bucket)

    # ── Model: single call, P(Over) direction ────────────────────────────
    # PR2: routes through _compute_raw_over_prob which uses Poisson PMF for
    # batter markets when a regressor is available, classifier otherwise.
    raw_over, source = _compute_raw_over_prob(
        over_prop, bucket, predicted_value=predicted_value,
    )
    # No squash -- calibrated artifact handles probability shaping.
    # Clamp only to [0, 1] in case isotonic regression extrapolated
    # slightly past the boundary; the Under-side complement below then
    # automatically stays in [0, 1] too.
    raw_over_prob = max(0.0, min(1.0, float(raw_over)))

    # ── Market: no-vig per side ───────────────────────────────────────────
    mkt_over_raw  = _american_to_prob(over_prop.get("best_odds"))
    mkt_under_raw = _american_to_prob(under_prop.get("best_odds"))
    total_mkt = mkt_over_raw + mkt_under_raw
    if total_mkt > 0:
        mkt_over  = mkt_over_raw  / total_mkt
        mkt_under = mkt_under_raw / total_mkt
    else:
        mkt_over  = 0.5
        mkt_under = 0.5

    # ── Under is the exact complement ────────────────────────────────────
    raw_under_prob = 1.0 - raw_over_prob

    def _make(model_p: float, market_p: float, side: str) -> dict:
        edge = model_p - market_p
        # Same fix as predict(): positive edge means model favors THIS
        # side, so recommend THIS side -- the old code hardcoded "Over"
        # / "Under" labels here regardless of which side was being
        # scored, which gave Under-side props an "Over" recommendation
        # when their edge was positive.
        other_side = "Under" if side == "Over" else "Over"
        if   edge >  0.03: rec = side
        elif edge < -0.03: rec = other_side
        else:               rec = "Pass"
        # Confidence = recommended-side win probability (see predict()
        # for the rationale -- ``abs(edge)*2 + 0.5`` saturated at 1.0
        # for any reasonable calibrated edge and turned the slate into
        # an all-100% list).
        if   rec == side:        conf = model_p
        elif rec == other_side:  conf = 1.0 - model_p
        else:                    conf = max(model_p, 1.0 - model_p)
        conf = max(0.50, min(1.0, conf))
        return {
            "recommendation":  rec,
            "confidence":      round(conf, 4),
            "model_prob":      round(model_p, 4),
            "market_prob":     round(market_p, 4),
            "edge":            round(edge, 4),
            "source":          source,
            "predicted_value": predicted_value,
        }

    return (
        _make(raw_over_prob,  mkt_over,  "Over"),
        _make(raw_under_prob, mkt_under, "Under"),
    )


# ── Record tracking ─────────────────────────────────────────────────────────

def _read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw.get("picks") or []
    except Exception:                                                     # noqa: BLE001
        return []


def get_record(bucket: str) -> dict:
    """Return {wins, losses, pct, total} for the pitcher or batter
    props model.  Reads the local per-bucket picks history file.
    """
    path = PITCHER_HISTORY_PATH if bucket == "pitcher" else BATTER_HISTORY_PATH
    rows = _read_history(path)
    wins = sum(1 for r in rows if (r.get("result") or "").lower() == "win")
    losses = sum(1 for r in rows if (r.get("result") or "").lower() == "loss")
    total = wins + losses
    pct = (wins / total) if total else None
    return {"wins": wins, "losses": losses, "total": total, "pct": pct}
