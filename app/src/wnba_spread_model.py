"""
WNBA point spread model: XGBoost + Linear Regression ensemble (regression).
Target: predicted home margin (home_score − away_score).
At inference, compare predicted margin to the posted spread line.
P(home covers) = sigmoid(k * (predicted_margin − spread_line) / sigma_margin)

Uses the 17-feature WNBA vector (expanded from 15 to include trend_diff and
college_adj_diff).  Training uses the recency-weighting scheme from
recency_weights.py: 60 / 25 / 15 % for current / previous / older seasons,
with a 75 % boost for teams that changed win-rate by > 15 pp.

Model path: .cache/model_spread_wnba.joblib
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

_MODEL_PATH = Path(".cache/model_spread_wnba.joblib")

# Typical WNBA margin standard deviation in points
_SIGMA = 8.5
# Logistic steepness
_K = 0.5


def _prob_cover(predicted_margin: float, spread_line: float) -> float:
    """
    Convert (predicted_margin − spread_line) to P(home covers) via
    a logistic approximation calibrated to WNBA scoring variance.
    """
    margin = predicted_margin - spread_line
    return 1.0 / (1.0 + math.exp(-_K * margin / _SIGMA))


class WNBASpreadModel:
    """
    Two-model regression ensemble (XGBoost + LinearRegression) that predicts
    the home team's margin of victory and compares it to the posted spread.
    """

    def __init__(self) -> None:
        self.model_path: Path = _MODEL_PATH
        self.xgb: Optional[xgb.XGBRegressor] = None
        self.lr: Optional[LinearRegression] = None
        self.scaler: StandardScaler = StandardScaler()
        self.is_trained: bool = False
        self.lr_is_trained: bool = False
        self.xgb_rmse: Optional[float] = None
        self.lr_rmse: Optional[float] = None

    # ------------------------------------------------------------------
    # Training / loading
    # ------------------------------------------------------------------

    def train_or_load(
        self,
        stats_client,
        feature_builder,
        season: int,
        force_retrain: bool = False,
    ) -> str:
        """
        Load a previously saved model if it exists, matches target_type, and
        was built with the current 17-feature schema.  Otherwise trains from
        scratch with recency weighting.  Returns a human-readable status string.
        """
        try:
            from . import model_cache_persist as _persist
            _persist.try_download(self.model_path)
        except Exception:                                                 # noqa: BLE001
            pass

        import sys as _sys
        if not force_retrain and self.model_path.exists():
            try:
                saved = joblib.load(self.model_path)
                # Require 17 features and the updated target_type tag
                n_feat = len(saved["scaler"].mean_) if (
                    "scaler" in saved and hasattr(saved["scaler"], "mean_")
                ) else None
                scaler_ok = n_feat == 17
                print(
                    f"MODEL[spread_wnba]: cache feature count loaded={n_feat}  "
                    f"expected=17  match={scaler_ok}",
                    flush=True, file=_sys.stderr,
                )
                if saved.get("target_type") == "wnba_spread_v2" and "lr" in saved and scaler_ok:
                    self.xgb = saved["xgb"]
                    self.lr = saved["lr"]
                    self.scaler = saved["scaler"]
                    self.xgb_rmse = saved.get("xgb_rmse")
                    self.lr_rmse = saved.get("lr_rmse")
                    self.is_trained = True
                    self.lr_is_trained = True
                    xgb_s = f"{self.xgb_rmse:.2f}" if self.xgb_rmse is not None else "N/A"
                    lr_s = f"{self.lr_rmse:.2f}" if self.lr_rmse is not None else "N/A"
                    print(
                        f"MODEL[spread_wnba]: LOADED FROM CACHE  features=17  "
                        f"XGB_RMSE={xgb_s}  LR_RMSE={lr_s}",
                        flush=True, file=_sys.stderr,
                    )
                    return (
                        f"Loaded WNBA spread model (17-feat, recency-weighted) "
                        f"(XGB RMSE: {xgb_s} pts | LR RMSE: {lr_s} pts)"
                    )
            except Exception as exc:                                      # noqa: BLE001
                print(f"MODEL[spread_wnba]: cache read failed ({type(exc).__name__}: {exc}) "
                      f"-- RETRAINED FROM SCRATCH", flush=True, file=_sys.stderr)
        else:
            reason = "force_retrain=True" if force_retrain else "no cache file (local or Supabase)"
            print(f"MODEL[spread_wnba]: RETRAINED FROM SCRATCH ({reason})",
                  flush=True, file=_sys.stderr)

        return self._train(stats_client, feature_builder, season)

    def _train(self, stats_client, feature_builder, season: int) -> str:
        """
        Train on 3 seasons of completed games using recency weighting
        (60 / 25 / 15 % for current / previous / older seasons).
        """
        from .recency_weights import (
            compute_sample_weights,
            build_boost_mask,
            format_weight_shift_summary,
        )

        # Collect all completed games tagged by season
        all_games = stats_client.get_completed_games_with_season()

        X_rows: list[np.ndarray] = []
        y_rows: list[float] = []
        season_tags: list[int] = []
        game_team_pairs: list[tuple[int, int]] = []

        for game, s in all_games:
            teams  = game.get("teams", {})
            scores = game.get("scores", {})
            home_id    = teams.get("home", {}).get("id")
            away_id    = teams.get("away", {}).get("id")
            home_score = scores.get("home", {}).get("total")
            away_score = scores.get("away", {}).get("total")

            if not all([home_id, away_id,
                        home_score is not None, away_score is not None]):
                continue

            vec = feature_builder.build_training_row(home_id, away_id)
            if vec is None:
                continue

            margin = float(int(home_score) - int(away_score))
            X_rows.append(vec)
            y_rows.append(margin)
            season_tags.append(s)
            game_team_pairs.append((home_id, away_id))

        n = len(X_rows)
        if n < 30:
            return f"Spread: insufficient data ({n} games)"

        # Split into season buckets: old (≤season-2), prev (season-1), current
        n_old     = sum(1 for s in season_tags if s <= season - 2)
        n_prev    = sum(1 for s in season_tags if s == season - 1)
        n_current = sum(1 for s in season_tags if s == season)

        # Detect high-change teams for the dynamic boost
        high_change_ids = stats_client.find_high_change_wnba_teams()
        current_pairs   = [p for p, s in zip(game_team_pairs, season_tags) if s == season]
        boost_mask      = build_boost_mask(current_pairs, high_change_ids)

        sample_weight = compute_sample_weights(n_old, n_prev, n_current, boost_mask)

        print(f"  [wnba spread] Training rows: old={n_old}  prev={n_prev}  current={n_current}")

        # Reorder rows so they are [old | prev | current] as required by compute_sample_weights
        order = (
            [i for i, s in enumerate(season_tags) if s <= season - 2]
            + [i for i, s in enumerate(season_tags) if s == season - 1]
            + [i for i, s in enumerate(season_tags) if s == season]
        )
        X = np.vstack([X_rows[i] for i in order])
        y = np.array([y_rows[i] for i in order], dtype=np.float32)

        X_scaled = self.scaler.fit_transform(X)

        # XGBoost regressor
        self.xgb = xgb.XGBRegressor(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=5,
            reg_lambda=2.0,
            eval_metric="rmse",
            random_state=42,
        )
        try:
            xgb_cv = cross_val_score(
                self.xgb, X_scaled, y, cv=5,
                scoring="neg_root_mean_squared_error",
                fit_params={"sample_weight": sample_weight},
            )
        except TypeError:
            xgb_cv = cross_val_score(
                self.xgb, X_scaled, y, cv=5,
                scoring="neg_root_mean_squared_error",
            )
        self.xgb_rmse = float(-xgb_cv.mean())
        self.xgb.fit(X_scaled, y, sample_weight=sample_weight)
        self.is_trained = True

        # Linear Regression
        self.lr = LinearRegression()
        try:
            lr_cv = cross_val_score(
                self.lr, X_scaled, y, cv=5,
                scoring="neg_root_mean_squared_error",
                fit_params={"sample_weight": sample_weight},
            )
        except TypeError:
            lr_cv = cross_val_score(
                self.lr, X_scaled, y, cv=5,
                scoring="neg_root_mean_squared_error",
            )
        self.lr_rmse = float(-lr_cv.mean())
        self.lr.fit(X_scaled, y, sample_weight=sample_weight)
        self.lr_is_trained = True

        self.model_path.parent.mkdir(exist_ok=True)
        joblib.dump(
            {
                "xgb":                self.xgb,
                "lr":                 self.lr,
                "scaler":             self.scaler,
                "xgb_rmse":          self.xgb_rmse,
                "lr_rmse":           self.lr_rmse,
                "target_type":       "wnba_spread_v2",
                "sample_weight_info": {
                    "n_old": n_old, "n_prev": n_prev, "n_current": n_current,
                    "n_boosted": int(boost_mask.sum()),
                },
            },
            self.model_path,
        )

        try:
            from . import model_cache_persist as _persist
            _persist.upload(self.model_path)
        except Exception:                                                 # noqa: BLE001
            pass

        return (
            f"Spread: XGB RMSE {self.xgb_rmse:.2f} pts | "
            f"LR RMSE {self.lr_rmse:.2f} pts  "
            f"({n} games: {n_current} current / {n_prev} prev / {n_old} old)"
        )

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        feature_vec: np.ndarray,
        game: dict,
        weights: Optional[dict] = None,
        ml_prob_home:    Optional[float] = None,
        ml_lr_prob_home: Optional[float] = None,
        ml_nn_prob_home: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Return a spread prediction dict, or None if the model is not trained.

        *feature_vec* must be the 15-feature WNBA moneyline/spread vector.
        *game* should contain run_line_point (or spread), run_line_home_odds,
        run_line_away_odds.

        ml_prob_home / ml_lr_prob_home / ml_nn_prob_home are accepted only for
        call-site parity with RunLineModel.predict() so a single unified call
        site can drive the spread/run-line "RL slot" for both sports.  They are
        intentionally ignored here: unlike the MLB run-line model (a
        *conditional* hurdle P(cover | win) that must be multiplied by the
        moneyline P(home wins) to recover the joint probability), this WNBA
        model predicts the point margin directly, so there is no conditional
        probability to anchor to the moneyline head.  Without this parameter
        parity the unified caller in scheduler._rerun_single_game raised
        TypeError, which had forced WNBA spread predictions to be skipped
        entirely on the 15-minute odds-refresh rerun.
        """
        if not self.is_trained:
            return None

        try:
            X = self.scaler.transform(feature_vec.reshape(1, -1))
        except Exception:
            return None

        xgb_pred = float(self.xgb.predict(X)[0])
        lr_pred = (
            float(self.lr.predict(X)[0])
            if self.lr_is_trained and self.lr is not None
            else xgb_pred
        )

        # Ensemble weights
        w = weights or {}
        w_xgb = float(w.get("xgb", 0.5))
        w_lr = float(w.get("lr", 0.5))
        total_w = w_xgb + w_lr
        if total_w > 0:
            predicted_margin = (xgb_pred * w_xgb + lr_pred * w_lr) / total_w
            eff_w = {"xgb": w_xgb / total_w, "lr": w_lr / total_w}
        else:
            predicted_margin = (xgb_pred + lr_pred) / 2.0
            eff_w = {"xgb": 0.5, "lr": 0.5}

        # The spread line is the home team's spread (e.g. -5.5 means home favored by 5.5)
        spread_line = float(
            game.get("run_line_point") or game.get("spread") or 0.0
        )

        prob_cover = _prob_cover(predicted_margin, spread_line)

        # Conflict detection: both models must agree on direction relative to spread
        models_agree = (xgb_pred - spread_line > 0) == (lr_pred - spread_line > 0)

        spread_home_odds = game.get("run_line_home_odds")
        spread_away_odds = game.get("run_line_away_odds")

        if prob_cover >= 0.5:
            side = "home"
            pick_prob = prob_cover
            pick_odds = int(spread_home_odds) if spread_home_odds is not None else -110
            pick_team = game.get("home_team", "")
        else:
            side = "away"
            pick_prob = 1.0 - prob_cover
            pick_odds = int(spread_away_odds) if spread_away_odds is not None else -110
            pick_team = game.get("away_team", "")

        if pick_odds > 0:
            market_prob = 100.0 / (pick_odds + 100.0)
        else:
            market_prob = abs(pick_odds) / (abs(pick_odds) + 100.0)

        edge = pick_prob - market_prob
        is_value = models_agree and edge >= 0.05 and pick_odds > -300

        return {
            "predicted_margin": round(predicted_margin, 2),
            "xgb_pred": round(xgb_pred, 2),
            "lr_pred": round(lr_pred, 2),
            "effective_weights": eff_w,
            "spread_line": spread_line,
            "side": side,
            "pick_team": pick_team,
            "pick_prob": round(pick_prob, 4),
            "pick_odds": pick_odds,
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "value_bet": is_value,
            "conflict": not models_agree,
            "models_agree": models_agree,
            "confidence": round(abs(prob_cover - 0.5) * 2, 4),
            "spread_home_odds": int(spread_home_odds) if spread_home_odds is not None else -110,
            "spread_away_odds": int(spread_away_odds) if spread_away_odds is not None else -110,
        }

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_raw_model(self) -> Optional[xgb.XGBRegressor]:
        return self.xgb

    def get_scaler(self) -> StandardScaler:
        return self.scaler
