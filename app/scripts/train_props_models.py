"""
train_props_models.py
=====================
Standalone training script for the MLB player-prop models.

Pulls per-game logs from the free MLB Stats API
(statsapi.mlb.com) -- pybaseball / FanGraphs was returning 403s and
the per-game gameLog endpoint on MLB Stats gives us strictly more
data (every appearance, not season aggregates).

Pipeline
--------
1. List every active MLB player for the season via
   /api/v1/sports/1/players?season=<year>.  Bucket by
   primaryPosition.code -- "1" is pitcher, everything else is a
   position player for the hitter dataset.

2. Pitcher training set: for each pitcher with >= 5 starts, fetch
   /api/v1/people/{pid}/stats?stats=gameLog&group=pitching
   &season=<year>.  Extract per-game IP, H, ER, BB, K, opponent
   team, isHome, gameDate.

3. Batter training set: for each batter with >= 20 PA, fetch
   /api/v1/people/{pid}/stats?stats=gameLog&group=hitting
   &season=<year>.  Extract per-game H, HR, RBI, R, BB, SO, TB,
   AB, opponent, isHome, gameDate.  Lineup position comes in via
   stat.battingOrder when present; falls back to 0.

4. Build rolling features per row:
     * SEASON-to-date averages of every numeric stat (mean of all
       games strictly BEFORE this one, so the row's own outcome
       doesn't leak into its features).
     * 7-game rolling averages (mean of the immediately preceding
       7 games, also leak-free).

5. Label rows against typical prop lines:
     pitcher:  K >= 6   (proxy for pitcher_strikeouts >= 5.5 line)
     batter:   H >= 1   (proxy for batter_hits >= 0.5 line)

6. Train XGBoost classifiers with 5-fold CV.  Log per-fold + OOF
   accuracy + log-loss.

7. Save .cache/props_model_pitcher.joblib and
   .cache/props_model_batter.joblib, then push base64'd copies to
   Supabase via src.props_model.push_models_to_supabase().

Caching
-------
The raw per-game rows are cached to .cache/props_training_data_<year>.json
so a re-run skips the ~1000 HTTP calls to MLB Stats.  Delete the file
to force a refresh.

Usage
-----
    python app/scripts/train_props_models.py --season 2025
    python app/scripts/train_props_models.py --season 2025 --refresh-data

Logging
-------
Every step prints `PROPS-TRAIN: ...` to stderr.  Progress lines fire
every 25 players during the fetch loops so the terminal makes it
obvious work is happening.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

_CACHE_DIR    = Path(".cache")
_TRAIN_DIR    = _CACHE_DIR / "props_train"
_STATS_BASE   = "https://statsapi.mlb.com/api/v1"
# Throttle between MLB Stats calls so we don't antagonize a free API.
# Empirically ~10 req/s is fine; 50ms keeps us at ~20/s with a buffer.
_HTTP_SLEEP   = 0.05
_HTTP_TIMEOUT = 15

# Threshold for inclusion in the training set.  Players with fewer
# than this many appearances generate too few rows to be useful and
# their inclusion bloats the fetch time linearly.
_MIN_PITCHER_STARTS = 5
_MIN_BATTER_PA      = 20


def _log(msg: str) -> None:
    print(f"PROPS-TRAIN: {msg}", flush=True, file=sys.stderr)


# ── HTTP helper ─────────────────────────────────────────────────────────────

def _fetch_json(url: str, *, label: str = "", retries: int = 2) -> Optional[dict]:
    """GET *url* and return parsed JSON.  Retries on transient errors with
    exponential backoff.  Returns None on permanent failure (caller logs
    its own skip line)."""
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "sports-betting-ai/1.0"},
            )
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                _log(f"  {label} HTTP {exc.code} -- retry in {delay}s")
                time.sleep(delay); delay *= 2
                continue
            _log(f"  {label} HTTP {exc.code} -- giving up")
            return None
        except urllib.error.URLError as exc:
            if attempt < retries:
                _log(f"  {label} network -- retry in {delay}s ({exc.reason})")
                time.sleep(delay); delay *= 2
                continue
            return None
        except Exception as exc:                                          # noqa: BLE001
            _log(f"  {label} {type(exc).__name__}: {exc}")
            return None
    return None


# ── Player list (one call, returns everyone) ────────────────────────────────

def fetch_players_for_season(season: int) -> list[dict]:
    """Return [{id, name, position_code, team_abbrev}] for every active
    MLB player in *season*.  One HTTP call regardless of player count."""
    url = f"{_STATS_BASE}/sports/1/players?season={season}"
    _log(f"fetching player list for {season}: {url}")
    data = _fetch_json(url, label=f"players({season})")
    if not data:
        _log(f"  player list fetch FAILED -- aborting")
        return []
    out: list[dict] = []
    for p in (data.get("people") or []):
        try:
            pos = ((p.get("primaryPosition") or {}).get("code") or "").strip()
            team = ((p.get("currentTeam") or {}).get("abbreviation") or "")
            out.append({
                "id":            int(p.get("id") or 0),
                "name":          p.get("fullName") or "",
                "position_code": pos,
                "team_abbrev":   team,
            })
        except (TypeError, ValueError):
            continue
    _log(f"  parsed {len(out)} players")
    return out


# ── Per-pitcher game log ────────────────────────────────────────────────────

def _parse_ip(value) -> float:
    """MLB Stats API returns innings as "5.2" where .1 = 1/3, .2 = 2/3.
    Convert to a true float so K/9, BB/9 math works."""
    if value is None or value == "":
        return 0.0
    try:
        s = str(value)
        whole, frac = s.split(".") if "." in s else (s, "0")
        return float(whole) + (float(frac) / 3.0)
    except (TypeError, ValueError):
        return 0.0


def fetch_pitcher_game_log(pid: int, season: int) -> list[dict]:
    """One call per pitcher.  Returns chronological list of:
        {date, opp_team, is_home, IP, H, ER, BB, K, games_started}
    Empty list when the pitcher has no appearances or the call fails.
    """
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=gameLog&group=pitching&season={season}"
    )
    data = _fetch_json(url, label=f"pitcher gameLog pid={pid}")
    if not data:
        return []
    rows: list[dict] = []
    for grp in (data.get("stats") or []):
        for split in (grp.get("splits") or []):
            st = split.get("stat") or {}
            opp = ((split.get("opponent") or {}).get("abbreviation")
                   or (split.get("opponent") or {}).get("name") or "")
            game_date = split.get("date") or ""
            try:
                is_home = bool(split.get("isHome"))
            except Exception:                                              # noqa: BLE001
                is_home = False
            try:
                gs = int(st.get("gamesStarted") or 0)
            except (TypeError, ValueError):
                gs = 0
            try:
                k = int(st.get("strikeOuts") or 0)
                bb = int(st.get("baseOnBalls") or 0)
                h = int(st.get("hits") or 0)
                er = int(st.get("earnedRuns") or 0)
            except (TypeError, ValueError):
                continue
            ip = _parse_ip(st.get("inningsPitched"))
            rows.append({
                "date":           game_date,
                "opp_team":       opp,
                "is_home":        is_home,
                "IP":             ip,
                "H":              h,
                "ER":             er,
                "BB":             bb,
                "K":              k,
                "games_started":  gs,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# ── Per-batter game log ─────────────────────────────────────────────────────

def fetch_batter_game_log(pid: int, season: int) -> list[dict]:
    """One call per batter.  Returns chronological list of:
        {date, opp_team, is_home, AB, H, HR, RBI, R, BB, SO, TB, PA,
         batting_order}
    Empty list when the batter has no appearances or the call fails.
    """
    url = (
        f"{_STATS_BASE}/people/{pid}/stats"
        f"?stats=gameLog&group=hitting&season={season}"
    )
    data = _fetch_json(url, label=f"batter gameLog pid={pid}")
    if not data:
        return []
    rows: list[dict] = []
    for grp in (data.get("stats") or []):
        for split in (grp.get("splits") or []):
            st = split.get("stat") or {}
            opp = ((split.get("opponent") or {}).get("abbreviation")
                   or (split.get("opponent") or {}).get("name") or "")
            game_date = split.get("date") or ""
            try:
                is_home = bool(split.get("isHome"))
            except Exception:                                              # noqa: BLE001
                is_home = False
            try:
                ab  = int(st.get("atBats") or 0)
                h   = int(st.get("hits") or 0)
                hr  = int(st.get("homeRuns") or 0)
                rbi = int(st.get("rbi") or 0)
                r   = int(st.get("runs") or 0)
                bb  = int(st.get("baseOnBalls") or 0)
                so  = int(st.get("strikeOuts") or 0)
                tb  = int(st.get("totalBases") or 0)
                pa  = int(st.get("plateAppearances") or (ab + bb))
            except (TypeError, ValueError):
                continue
            # battingOrder lives on the split when MLB Stats includes
            # it (3-digit format "100" = leadoff, "200" = 2-hole etc).
            # Falls back to 0 when not present.
            order_raw = split.get("battingOrder") or st.get("battingOrder")
            try:
                batting_order = int(order_raw) // 100 if order_raw else 0
            except (TypeError, ValueError):
                batting_order = 0
            rows.append({
                "date":          game_date,
                "opp_team":      opp,
                "is_home":       is_home,
                "AB":            ab,
                "H":             h,
                "HR":            hr,
                "RBI":           rbi,
                "R":             r,
                "BB":            bb,
                "SO":            so,
                "TB":            tb,
                "PA":            pa,
                "batting_order": batting_order,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


# ── Collection driver (with disk cache) ────────────────────────────────────

def collect_training_data(season: int, *, refresh: bool = False) -> dict:
    """Walk every player, fetch their game log, return the combined
    dataset.  Cached to .cache/props_training_data_<season>.json so a
    re-run skips the network entirely (pass refresh=True to force
    re-fetch)."""
    _TRAIN_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"props_training_data_{season}.json"
    if cache_path.exists() and not refresh:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            _log(
                f"training data cache HIT: {cache_path}  "
                f"pitchers={len(data.get('pitchers') or [])}  "
                f"batters={len(data.get('batters') or [])}"
            )
            return data
        except Exception as exc:                                          # noqa: BLE001
            _log(f"cache read failed ({exc}) -- re-fetching")

    started = time.monotonic()
    players = fetch_players_for_season(season)
    if not players:
        return {"season": season, "pitchers": [], "batters": []}

    # Bucket by primary position.  MLB Stats codes pitchers as "1";
    # everything else is a position player (catchers / infielders /
    # outfielders / DH all bat).  Two-way players show up with their
    # primary position; the secondary stat group is empty so the fetch
    # just returns no rows for the wrong bucket.
    pitchers_ids = [p for p in players if p["position_code"] == "1"]
    batters_ids  = [p for p in players if p["position_code"] not in ("1", "")]
    _log(
        f"bucketed: {len(pitchers_ids)} pitcher(s), "
        f"{len(batters_ids)} batter(s)"
    )

    pitcher_payload: list[dict] = []
    for i, p in enumerate(pitchers_ids, 1):
        if i % 25 == 0 or i == len(pitchers_ids):
            _log(f"  pitcher progress: {i}/{len(pitchers_ids)}  (kept so far: {len(pitcher_payload)})")
        rows = fetch_pitcher_game_log(p["id"], season)
        time.sleep(_HTTP_SLEEP)
        starts = sum(1 for r in rows if r.get("games_started"))
        if starts < _MIN_PITCHER_STARTS:
            continue
        pitcher_payload.append({
            "id":          p["id"],
            "name":        p["name"],
            "team":        p["team_abbrev"],
            "games":       rows,
        })
    _log(f"pitcher dataset: {len(pitcher_payload)} player(s) kept "
         f"(min {_MIN_PITCHER_STARTS} starts)")

    batter_payload: list[dict] = []
    for i, p in enumerate(batters_ids, 1):
        if i % 25 == 0 or i == len(batters_ids):
            _log(f"  batter progress: {i}/{len(batters_ids)}  (kept so far: {len(batter_payload)})")
        rows = fetch_batter_game_log(p["id"], season)
        time.sleep(_HTTP_SLEEP)
        pa = sum(int(r.get("PA") or 0) for r in rows)
        if pa < _MIN_BATTER_PA:
            continue
        batter_payload.append({
            "id":          p["id"],
            "name":        p["name"],
            "team":        p["team_abbrev"],
            "games":       rows,
        })
    _log(f"batter dataset: {len(batter_payload)} player(s) kept "
         f"(min {_MIN_BATTER_PA} PA)")

    payload = {
        "season":     season,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "pitchers":   pitcher_payload,
        "batters":    batter_payload,
    }
    try:
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _log(f"training data cached to {cache_path}")
    except Exception as exc:                                              # noqa: BLE001
        _log(f"cache write failed: {exc}")
    elapsed = time.monotonic() - started
    _log(f"collection complete in {elapsed:.1f}s")
    return payload


# ── Feature engineering (leak-free rolling + season averages) ───────────────

def _build_pitcher_dataset(payload: dict):
    """Build (X, y, feature_names) for pitcher_strikeouts >= 6 binary
    label.  Features for each row: season-to-date averages of K, BB,
    H, ER, IP, K/9, BB/9, plus 7-game rolling averages of the same.
    Both are computed STRICTLY from games BEFORE the row so the label
    can't leak into its features.
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _log("pandas/numpy missing -- aborting pitcher build")
        return None, None, None
    rows: list[dict] = []
    for p in (payload.get("pitchers") or []):
        games = p.get("games") or []
        if not games:
            continue
        df = pd.DataFrame(games).sort_values("date").reset_index(drop=True)
        # K/9 and BB/9 per-row (avoid div-by-zero for relief outings)
        df["k_per_9"]  = (df["K"]  * 9.0 / df["IP"].clip(lower=0.01)).fillna(0.0)
        df["bb_per_9"] = (df["BB"] * 9.0 / df["IP"].clip(lower=0.01)).fillna(0.0)
        # Season-to-date: shift(1) so row N's "season avg" is mean of rows 0..N-1
        stats = ["K", "BB", "H", "ER", "IP", "k_per_9", "bb_per_9"]
        for c in stats:
            df[f"szn_{c}"] = df[c].shift(1).expanding().mean()
            df[f"r7_{c}"]  = df[c].shift(1).rolling(window=7, min_periods=2).mean()
        df = df.dropna(subset=[f"szn_{stats[0]}", f"r7_{stats[0]}"])
        if df.empty:
            continue
        df["label"]      = (df["K"] >= 6).astype(int)
        df["is_home_i"]  = df["is_home"].astype(int)
        feat_cols = (
            [f"szn_{c}" for c in stats]
            + [f"r7_{c}"  for c in stats]
            + ["is_home_i"]
        )
        for _, row in df.iterrows():
            rows.append({
                **{c: float(row[c]) for c in feat_cols},
                "label": int(row["label"]),
            })
    if not rows:
        _log("pitcher dataset empty after feature build")
        return None, None, None
    df_all = pd.DataFrame(rows)
    feature_names = [c for c in df_all.columns if c != "label"]
    X = df_all[feature_names].fillna(0).to_numpy(dtype=float)
    y = df_all["label"].to_numpy(dtype=int)
    _log(
        f"pitcher features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}"
    )
    return X, y, feature_names


def _build_batter_dataset(payload: dict):
    """Build (X, y, feature_names) for batter_hits >= 1 binary label.
    Same rolling-7 + season-to-date treatment as pitchers.
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        _log("pandas/numpy missing -- aborting batter build")
        return None, None, None
    rows: list[dict] = []
    for p in (payload.get("batters") or []):
        games = p.get("games") or []
        if not games:
            continue
        df = pd.DataFrame(games).sort_values("date").reset_index(drop=True)
        # Per-row rate stats so rolling averages are normalized to ABs.
        df["H_per_AB"]   = (df["H"]   / df["AB"].clip(lower=1)).fillna(0.0)
        df["TB_per_AB"]  = (df["TB"]  / df["AB"].clip(lower=1)).fillna(0.0)
        df["HR_per_AB"]  = (df["HR"]  / df["AB"].clip(lower=1)).fillna(0.0)
        df["BB_per_PA"]  = (df["BB"]  / df["PA"].clip(lower=1)).fillna(0.0)
        df["SO_per_PA"]  = (df["SO"]  / df["PA"].clip(lower=1)).fillna(0.0)
        stats = [
            "H", "HR", "RBI", "R", "BB", "SO", "TB", "AB", "PA",
            "H_per_AB", "TB_per_AB", "HR_per_AB", "BB_per_PA", "SO_per_PA",
        ]
        for c in stats:
            df[f"szn_{c}"] = df[c].shift(1).expanding().mean()
            df[f"r7_{c}"]  = df[c].shift(1).rolling(window=7, min_periods=2).mean()
        df = df.dropna(subset=[f"szn_{stats[0]}", f"r7_{stats[0]}"])
        if df.empty:
            continue
        df["label"]     = (df["H"] >= 1).astype(int)
        df["is_home_i"] = df["is_home"].astype(int)
        feat_cols = (
            [f"szn_{c}" for c in stats]
            + [f"r7_{c}"  for c in stats]
            + ["batting_order", "is_home_i"]
        )
        for _, row in df.iterrows():
            rows.append({
                **{c: float(row[c]) for c in feat_cols},
                "label": int(row["label"]),
            })
    if not rows:
        _log("batter dataset empty after feature build")
        return None, None, None
    df_all = pd.DataFrame(rows)
    feature_names = [c for c in df_all.columns if c != "label"]
    X = df_all[feature_names].fillna(0).to_numpy(dtype=float)
    y = df_all["label"].to_numpy(dtype=int)
    _log(
        f"batter features: {X.shape[0]} rows × {X.shape[1]} cols  "
        f"positive_rate={y.mean():.3f}"
    )
    return X, y, feature_names


# ── Fit + save (unchanged from prior version) ───────────────────────────────

def _train_and_save(X, y, out_path: Path, *, label: str) -> Optional[float]:
    if X is None or y is None or len(X) < 20:
        _log(f"{label}: not enough data ({0 if X is None else len(X)} rows) -- skipping train")
        return None
    try:
        from sklearn.model_selection import StratifiedKFold
        from sklearn.metrics         import accuracy_score, log_loss
        import xgboost as xgb
        import joblib
        import numpy as np
    except ImportError as exc:
        _log(f"{label}: missing dependency ({exc}) -- aborting")
        return None
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros(len(y))
    for fold, (tr, te) in enumerate(skf.split(X, y), 1):
        clf = xgb.XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            objective="binary:logistic", eval_metric="logloss",
            use_label_encoder=False, verbosity=0,
        )
        clf.fit(X[tr], y[tr])
        proba = clf.predict_proba(X[te])[:, 1]
        oof[te] = proba
        fold_acc = accuracy_score(y[te], (proba >= 0.5).astype(int))
        fold_ll  = log_loss(y[te], proba, labels=[0, 1])
        _log(f"{label} fold {fold}: acc={fold_acc:.3f}  log_loss={fold_ll:.3f}")
    oof_acc = accuracy_score(y, (oof >= 0.5).astype(int))
    oof_ll  = log_loss(y, oof, labels=[0, 1])
    _log(f"{label} OOF: acc={oof_acc:.3f}  log_loss={oof_ll:.3f}")
    final = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="binary:logistic", eval_metric="logloss",
        use_label_encoder=False, verbosity=0,
    )
    final.fit(X, y)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, out_path)
    _log(f"{label} model saved: {out_path}")
    return float(oof_acc)


# ── Driver ──────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--refresh-data", action="store_true",
                    help="Ignore cached training data and re-fetch")
    ap.add_argument("--skip-pitcher", action="store_true")
    ap.add_argument("--skip-batter",  action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="Skip the Supabase upload step")
    args = ap.parse_args()

    started = time.monotonic()
    summary: dict = {"season": args.season}

    payload = collect_training_data(args.season, refresh=args.refresh_data)

    if not args.skip_pitcher:
        X, y, _ = _build_pitcher_dataset(payload)
        acc = _train_and_save(
            X, y, Path(".cache/props_model_pitcher.joblib"), label="pitcher",
        )
        summary["pitcher_oof_acc"] = acc

    if not args.skip_batter:
        X, y, _ = _build_batter_dataset(payload)
        acc = _train_and_save(
            X, y, Path(".cache/props_model_batter.joblib"), label="batter",
        )
        summary["batter_oof_acc"] = acc

    if not args.no_push:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from src.props_model import push_models_to_supabase
            summary["supabase_push"] = push_models_to_supabase()
        except Exception as exc:                                          # noqa: BLE001
            summary["supabase_push"] = f"error: {exc}"

    elapsed = time.monotonic() - started
    _log(f"DONE in {elapsed:.1f}s  summary={json.dumps(summary, default=str)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
