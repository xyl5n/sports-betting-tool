"""
Totals (over/under) model for MLB: XGBoost + Linear Regression + Neural Network ensemble.
Predicts combined runs scored; the posted O/U line is compared at inference.
All three models must predict the same direction (over/under) for a recommended bet.

Feature vector uses absolute values and sums (not diffs) since we care
about total scoring volume, not which team scores more.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import cross_val_score
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

_MODEL_PATH = Path(".cache/model_totals_mlb.joblib")

# σ ≈ 2.5 runs — logistic sharpness for probability estimation from margin
_SIGMA = 2.5
_K     = 0.6  # logistic steepness: P(over) = 1/(1+exp(-k*margin/σ))

TOTALS_FEATURES = [
    "combined_rpg",         # home RPG + away RPG (sum of both offences)
    "combined_rapg",        # home RAPG + away RAPG (sum of both defences)
    "combined_sp_era",      # home SP ERA + away SP ERA
    "home_sp_k_rate",       # home SP strikeout rate
    "away_sp_k_rate",       # away SP strikeout rate
    "park_run_factor",      # stadium run factor (>1 = hitter-friendly)
    "wind_speed",           # mph (affects ball flight)
    "combined_bullpen_era", # home bullpen ERA + away bullpen ERA
    "temperature",          # °F (cold = fewer runs)
]


def _prob_over(predicted_total: float, line: float) -> float:
    """Convert run-margin vs line to a win probability using logistic approximation."""
    margin = predicted_total - line
    return 1.0 / (1.0 + math.exp(-_K * margin / _SIGMA))


class TotalsModel:
    def __init__(self):
        self.model_path    = _MODEL_PATH
        self.xgb: Optional[xgb.XGBRegressor]  = None
        self.lr:  Optional[LinearRegression]   = None
        self.nn:  Optional[MLPRegressor]       = None
        self.scaler        = StandardScaler()
        self.nn_scaler     = StandardScaler()
        self.is_trained    = False
        self.lr_is_trained = False
        self.nn_is_trained = False
        self.xgb_rmse:     Optional[float] = None
        self.lr_rmse:      Optional[float] = None
        self.nn_rmse:      Optional[float] = None

    # ------------------------------------------------------------------
    # Training / loading
    # ------------------------------------------------------------------

    def train_or_load(self, stats_client, feature_builder, season: int,
                      force_retrain: bool = False) -> str:
        try:
            from . import model_cache_persist as _persist
            _persist.try_download(self.model_path)
        except Exception:                                                 # noqa: BLE001
            pass

        import sys as _sys
        if not force_retrain and self.model_path.exists():
            saved = joblib.load(self.model_path)
            n_feat = getattr(saved.get("scaler"), "n_features_in_", None)
            print(
                f"MODEL[totals_mlb]: cache feature count loaded={n_feat}",
                flush=True, file=_sys.stderr,
            )
            if saved.get("target_type") == "totals" and "lr" in saved:
                self.xgb           = saved["xgb"]
                self.lr            = saved["lr"]
                self.nn            = saved.get("nn")
                self.scaler        = saved["scaler"]
                self.nn_scaler     = saved.get("nn_scaler", StandardScaler())
                self.xgb_rmse      = saved.get("xgb_rmse")
                self.lr_rmse       = saved.get("lr_rmse")
                self.nn_rmse       = saved.get("nn_rmse")
                self.is_trained    = True
                self.lr_is_trained = True
                self.nn_is_trained = self.nn is not None
                xgb_s = f"{self.xgb_rmse:.2f}" if self.xgb_rmse else "N/A"
                nn_s  = f"{self.nn_rmse:.2f}"   if self.nn_rmse  else "N/A"
                print(
                    f"MODEL[totals_mlb]: LOADED FROM CACHE  features={n_feat}  "
                    f"XGB_RMSE={xgb_s}  NN_RMSE={nn_s}",
                    flush=True, file=_sys.stderr,
                )
                return f"Loaded totals model (XGB RMSE: {xgb_s} | NN RMSE: {nn_s} runs)"
            print(
                f"MODEL[totals_mlb]: cache present but target_type / lr field "
                f"missing -- RETRAINED FROM SCRATCH",
                flush=True, file=_sys.stderr,
            )
        else:
            reason = "force_retrain=True" if force_retrain else "no cache file (local or Supabase)"
            print(f"MODEL[totals_mlb]: RETRAINED FROM SCRATCH ({reason})",
                  flush=True, file=_sys.stderr)
        return self._train(stats_client, feature_builder, season)

    def _train_nn(
        self,
        X_unscaled:     np.ndarray,
        y:              np.ndarray,
        sample_weights: "np.ndarray | None" = None,
    ) -> None:
        """Train regression NN with its own scaler; propagate sample_weights if provided."""
        from sklearn.model_selection import train_test_split

        if sample_weights is not None:
            X_tr_raw, X_val_raw, y_tr, y_val, sw_tr, _ = train_test_split(
                X_unscaled, y, sample_weights, test_size=0.15, random_state=42,
            )
        else:
            X_tr_raw, X_val_raw, y_tr, y_val = train_test_split(
                X_unscaled, y, test_size=0.15, random_state=42
            )
            sw_tr = None

        self.nn_scaler = StandardScaler()
        X_nn_tr  = self.nn_scaler.fit_transform(X_tr_raw)
        X_nn_val = self.nn_scaler.transform(X_val_raw)

        self.nn = MLPRegressor(
            hidden_layer_sizes=(64, 32, 16),
            activation="relu",
            alpha=0.001,
            batch_size=128,
            max_iter=400,
            random_state=42,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=20,
        )
        print(f"  Totals NN training on {len(y):,} games…")
        fit_kw = {"sample_weight": sw_tr} if sw_tr is not None else {}
        self.nn.fit(X_nn_tr, y_tr, **fit_kw)

        val_preds    = self.nn.predict(X_nn_val)
        residuals    = val_preds - y_val
        self.nn_rmse = float(np.sqrt(np.mean(residuals ** 2)))
        self.nn_is_trained = True

    def _train(
        self,
        stats_client,
        feature_builder,
        season: int,
        high_change_team_ids: "set[int] | None" = None,
    ) -> str:
        try:
            completed = stats_client.get_completed_games(season)
        except Exception as exc:
            print(f"  Warning: could not fetch totals season {season} ({exc}) — historical only.")
            completed = []
        X_rows, y_rows, skipped = [], [], 0

        for game in completed:
            teams  = game.get("teams", {})
            scores = game.get("scores", {})
            home_id    = teams.get("home", {}).get("id")
            away_id    = teams.get("away", {}).get("id")
            home_score = scores.get("home", {}).get("total")
            away_score = scores.get("away", {}).get("total")

            if not all([home_id, away_id,
                        home_score is not None, away_score is not None]):
                skipped += 1
                continue

            vec = feature_builder.build_totals_training_row(home_id, away_id)
            if vec is None:
                skipped += 1
                continue

            total = int(home_score) + int(away_score)
            X_rows.append(vec)
            y_rows.append(float(total))

        n = len(X_rows)
        X = np.vstack(X_rows) if n > 0 else np.empty((0, 9), dtype=np.float32)
        y = np.array(y_rows)

        # ── Augment with enriched historical totals data + recency weights ────
        X_combined, y_combined = X, y
        sample_weights = None
        try:
            from .enriched_historical_data import get_enriched_totals_X_y, get_enriched_seasons
            from .recency_weights import compute_sample_weights

            X_hist_tot, y_hist_tot = get_enriched_totals_X_y()
            seasons_hist            = get_enriched_seasons()

            if len(y_hist_tot) >= 100:
                # Totals rows align 1-to-1 with moneyline rows (same games)
                mask_prev = (seasons_hist == 2025)
                mask_old  = ~mask_prev

                X_tot_old  = X_hist_tot[mask_old];  y_tot_old  = y_hist_tot[mask_old]
                X_tot_prev = X_hist_tot[mask_prev]; y_tot_prev = y_hist_tot[mask_prev]

                n_old  = len(y_tot_old)
                n_prev = len(y_tot_prev)

                X_combined = np.vstack([X_tot_old, X_tot_prev, X])
                y_combined = np.concatenate([y_tot_old, y_tot_prev, y])

                # Totals doesn't track team IDs per row so no per-team boost;
                # use standard season-level weighting only.
                sample_weights = compute_sample_weights(n_old, n_prev, n)

                print(f"  Totals combined: {n_old:,} old + {n_prev:,} prev + {n} current "
                      f"= {len(y_combined):,} total  |  weights 15/25/60%")
        except Exception as exc:
            print(f"  Totals: historical unavailable ({exc}) — current-season only")

        if len(y_combined) < 30:
            return f"Totals: insufficient data ({len(y_combined)} combined games)"

        X_scaled = self.scaler.fit_transform(X_combined)

        cv_fit_params = ({"sample_weight": sample_weights}
                         if sample_weights is not None else {})

        self.xgb = xgb.XGBRegressor(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            min_child_weight=5, gamma=0.5, reg_lambda=2.0,
            eval_metric="rmse", random_state=42,
        )
        try:
            xgb_cv = cross_val_score(
                self.xgb, X_scaled, y_combined, cv=5,
                scoring="neg_root_mean_squared_error",
                fit_params=cv_fit_params,
            )
        except TypeError:
            xgb_cv = cross_val_score(
                self.xgb, X_scaled, y_combined, cv=5,
                scoring="neg_root_mean_squared_error",
            )
        self.xgb_rmse = float(-xgb_cv.mean())
        self.xgb.fit(X_scaled, y_combined,
                     **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        self.is_trained = True

        self.lr = LinearRegression()
        try:
            lr_cv = cross_val_score(
                self.lr, X_scaled, y_combined, cv=5,
                scoring="neg_root_mean_squared_error",
                fit_params=cv_fit_params,
            )
        except TypeError:
            lr_cv = cross_val_score(
                self.lr, X_scaled, y_combined, cv=5,
                scoring="neg_root_mean_squared_error",
            )
        self.lr_rmse = float(-lr_cv.mean())
        self.lr.fit(X_scaled, y_combined,
                    **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        self.lr_is_trained = True

        self._train_nn(X_combined, y_combined, sample_weights)

        self.model_path.parent.mkdir(exist_ok=True)
        joblib.dump({
            "xgb": self.xgb, "lr": self.lr, "nn": self.nn,
            "scaler": self.scaler, "nn_scaler": self.nn_scaler,
            "xgb_rmse": self.xgb_rmse, "lr_rmse": self.lr_rmse,
            "nn_rmse": self.nn_rmse,
            "target_type": "totals",
        }, self.model_path)

        try:
            from . import model_cache_persist as _persist
            _persist.upload(self.model_path)
        except Exception:                                                 # noqa: BLE001
            pass

        total_n = len(y_combined)
        return (f"Totals: XGB RMSE {self.xgb_rmse:.2f} | LR RMSE {self.lr_rmse:.2f} | "
                f"NN RMSE {self.nn_rmse:.2f} runs ({total_n:,} games)")

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, totals_vec: np.ndarray, game: dict,
                weights: dict | None = None) -> Optional[dict]:
        """Return totals prediction dict, or None if not trained / no O/U line."""
        if not self.is_trained:
            return None

        total_line = game.get("total_line")
        over_odds  = game.get("over_odds")
        under_odds = game.get("under_odds")

        if total_line is None:
            return None

        try:
            X = self.scaler.transform(totals_vec.reshape(1, -1))
        except Exception:
            return None

        # Park factor is at feature index 5; apply as multiplier to raw predictions
        # Training data uses 1.0 (neutral), so raw predictions are neutral-park baselines.
        park_factor = float(totals_vec[5]) if len(totals_vec) > 5 else 1.0
        park_factor = max(0.80, min(1.60, park_factor))  # guard against bad values

        raw_xgb = float(self.xgb.predict(X)[0])
        raw_lr  = float(self.lr.predict(X)[0]) if self.lr_is_trained else raw_xgb

        nn_pred: Optional[float] = None
        if self.nn_is_trained and self.nn is not None:
            try:
                # nn_scaler was fitted on RAW (unscaled) data during _train_nn();
                # pass the original totals_vec, NOT the already-scaler-transformed X.
                X_nn    = self.nn_scaler.transform(totals_vec.reshape(1, -1))
                nn_pred = float(self.nn.predict(X_nn)[0])
            except Exception:
                nn_pred = None

        pred_xgb = raw_xgb * park_factor
        pred_lr  = raw_lr  * park_factor

        w = weights or {}
        line = float(total_line)

        # ── Silent XGB-only pick recorder (does not affect ensemble output) ───
        try:
            from .xgb_picks_tracker import record_totals_pick
            record_totals_pick(
                game            = game,
                predicted_total = pred_xgb,
                market_line     = line,
                sport           = "MLB",   # TotalsModel is MLB-only
            )
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        # ── Silent LR-only pick recorder for totals -- closes the gap where
        #    LR was tracked for ML + RL but not totals.  Same .cache/lr_picks_history.json
        #    file all three bet types write to; new entries get bet_type="totals"
        #    and a "line" field that settle_lr_pick reads to compute O/U
        #    correctness against (home+away).
        try:
            from .lr_picks_tracker import record_lr_pick_totals
            record_lr_pick_totals(
                sport            = "MLB",
                home_team        = game.get("home_team", ""),
                away_team        = game.get("away_team", ""),
                game_date        = (game.get("commence_time") or "")[:10],
                predicted_total  = pred_lr,
                market_line      = line,
                game_id          = game.get("id") or game.get("game_id"),
            )
        except Exception as _exc:
            logging.warning("Suppressed exception in %s: %s", __name__, _exc)

        if nn_pred is not None:
            pred_nn = nn_pred * park_factor
            # ── NN-only pick logging (silent side-channel) ────────────────
            # Records the NN's standalone over/under direction independent
            # of whatever the ensemble eventually returns.  Confidence is
            # derived from _prob_over so it matches the model's own scale.
            try:
                from .nn_picks import record_nn_pick
                _nn_dir       = "over" if pred_nn > line else "under"
                _p_over       = _prob_over(pred_nn, line)
                _nn_dir_prob  = _p_over if _nn_dir == "over" else (1.0 - _p_over)
                _ct           = str(game.get("commence_time") or "")
                _date         = _ct[:10] if len(_ct) >= 10 else ""
                if _date:
                    record_nn_pick(
                        game_date = _date,
                        matchup   = f"{game.get('away_team','?')} @ {game.get('home_team','?')}",
                        sport     = "MLB",
                        bet_type  = "totals",
                        nn_prob   = _nn_dir_prob,
                        nn_pick   = _nn_dir,
                        extra     = {"nn_run_total": round(float(pred_nn), 3),
                                     "line": line},
                    )
            except Exception as _exc:
                logging.warning("Suppressed exception in %s: %s", __name__, _exc)
            w_xgb   = float(w.get("xgb", 1 / 3))
            w_lr    = float(w.get("lr",  1 / 3))
            w_nn    = float(w.get("nn",  1 / 3))
            total_w = w_xgb + w_lr + w_nn
            if total_w > 0:
                combined = (pred_xgb * w_xgb + pred_lr * w_lr + pred_nn * w_nn) / total_w
                eff_w = {"xgb": w_xgb / total_w, "lr": w_lr / total_w, "nn": w_nn / total_w}
            else:
                combined = (pred_xgb + pred_lr + pred_nn) / 3.0
                eff_w = {"xgb": 1 / 3, "lr": 1 / 3, "nn": 1 / 3}
            models_agree = (pred_xgb > line) == (pred_lr > line) == (pred_nn > line)
        else:
            pred_nn = None
            w_xgb   = float(w.get("xgb", 0.5))
            w_lr    = float(w.get("lr",  0.5))
            total_w = w_xgb + w_lr
            if total_w > 0:
                combined = (pred_xgb * w_xgb + pred_lr * w_lr) / total_w
                eff_w = {"xgb": w_xgb / total_w, "lr": w_lr / total_w, "nn": 0.0}
            else:
                combined = (pred_xgb + pred_lr) / 2.0
                eff_w = {"xgb": 0.5, "lr": 0.5, "nn": 0.0}
            models_agree = (pred_xgb > line) == (pred_lr > line)

        # ── Diagnostic: individual model outputs before and after park factor ──
        matchup = f"{game.get('away_team','?')} @ {game.get('home_team','?')}"
        nn_raw_str  = f"{nn_pred / park_factor:.2f}" if nn_pred is not None else "N/A"
        nn_park_str = f"{nn_pred:.2f}"               if nn_pred is not None else "N/A"
        print(
            f"  [TOT diag] {matchup} | "
            f"park={park_factor:.3f}  line={line}  "
            f"XGB raw={raw_xgb:.2f}->park={pred_xgb:.2f}  "
            f"LR  raw={raw_lr:.2f}->park={pred_lr:.2f}  "
            f"NN  raw={nn_raw_str}->park={nn_park_str}  "
            f"combined={combined:.2f}  agree={models_agree}"
        )

        # ── Sanity cap: no MLB game in modern history has exceeded 30 combined runs ──
        # Cap at 25 to prevent inflated predictions from distorting direction/edge.
        MAX_REALISTIC_TOTAL = 25.0
        if combined > MAX_REALISTIC_TOTAL:
            print(
                f"  [TOT diag] {matchup} | "
                f"combined {combined:.2f} exceeds cap {MAX_REALISTIC_TOTAL} -- clamping"
            )
            combined = MAX_REALISTIC_TOTAL

        direction = "over" if combined > line else "under"
        prob_over = _prob_over(combined, line)

        if direction == "over":
            pick_prob  = prob_over
            pick_odds  = int(over_odds)  if over_odds  is not None else -110
        else:
            pick_prob  = 1.0 - prob_over
            pick_odds  = int(under_odds) if under_odds is not None else -110

        if pick_odds > 0:
            market_prob = 100 / (pick_odds + 100)
        else:
            market_prob = abs(pick_odds) / (abs(pick_odds) + 100)

        edge     = pick_prob - market_prob
        is_value = models_agree and edge >= 0.05 and pick_odds > -300

        # Feature importances for top-3 reasons
        top_reasons = []
        if self.xgb is not None:
            fi = self.xgb.feature_importances_
            idx_sorted = np.argsort(fi)[::-1][:3]
            top_reasons = [
                {"feature": TOTALS_FEATURES[i], "importance": float(fi[i])}
                for i in idx_sorted if i < len(TOTALS_FEATURES)
            ]

        # Assemble uncapped ensemble for inspection (the combined value is already capped above)
        _nn_for_raw = pred_nn if pred_nn is not None else pred_xgb
        _raw_avg    = (pred_xgb + pred_lr + _nn_for_raw) / 3.0 if pred_nn is not None \
                      else (pred_xgb + pred_lr) / 2.0

        # Pure-confidence tier (function of pick_prob alone, no odds reference).
        from .kelly import confidence_tier_from_prob
        conf_tier = confidence_tier_from_prob(pick_prob)

        return {
            "predicted_total":     round(combined, 2),
            "raw_predicted_total": round(_raw_avg, 2),   # pre-cap ensemble avg
            "xgb_pred":            round(pred_xgb, 2),
            "lr_pred":             round(pred_lr,  2),
            "nn_pred":             round(pred_nn,  2) if pred_nn is not None else None,
            "effective_weights":   eff_w,
            "total_line":          line,
            "direction":           direction,
            "pick_side":           direction,            # alias for cross-model consistency
            "models_agree":        models_agree,
            "conflict":            not models_agree,
            "pick_prob":           pick_prob,
            "pick_odds":           pick_odds,
            "market_prob":         market_prob,
            "edge":                edge,
            "value_bet":           is_value,
            "confidence":          min(abs(combined - line) / _SIGMA, 1.0),
            "confidence_tier":     conf_tier,            # Strong/Moderate/Low by pick_prob
            "over_odds":           int(over_odds)  if over_odds  is not None else -110,
            "under_odds":          int(under_odds) if under_odds is not None else -110,
            "park_run_factor":     round(park_factor, 3),
            "top_reasons":         top_reasons,
        }

    def get_raw_model(self):
        return self.xgb

    def get_scaler(self):
        return self.scaler
