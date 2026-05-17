"""
Run line model for MLB: XGBoost + Logistic Regression + Neural Network ensemble.
Target: home team covers -1.5 run line (home wins by 2+ runs).
Uses the same 23-feature MLB vector as the moneyline model so both
models can be trained together on completed game data.
"""
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

_MODEL_PATH = Path(".cache/model_run_line_mlb.joblib")

# ── Run-line XGBoost hyperparameters ─────────────────────────────────────────
# Tuned independently from the moneyline model (see model.py).
# Run-line has a stronger underlying signal (~65% CV); keeping the conservative
# regularization (mcw=5, gamma=1.0) which still beat lower-reg variants here.
# n_estimators=100, max_depth=3 chosen by 5-fold CV grid sweep on the enriched
# historical dataset (8,934 rows) — see xgb_hp_search.py. The previous 200x4
# config was overfitting: CV 65.28% -> 65.78% with the smaller forest.
XGB_RUN_LINE_PARAMS = dict(
    n_estimators=100,
    max_depth=3,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=5,
    gamma=1.0,
    reg_lambda=2.0,
    eval_metric="logloss",
    random_state=42,
)

# ── Run-line Logistic Regression regularisation ──────────────────────────────
# Independent of BettingModel.LR_MONEYLINE_C — RL prefers strong regularisation.
# Tuned via 5-fold CV sweep over {0.01, 0.1, 0.5, 1.0, 2.0, 5.0} on the
# enriched historical dataset (see tune_lr.py). C=0.01 won decisively:
# 0.6604 vs C=1.0 baseline 0.6577 (+0.27 pp). The RL label is class-imbalanced
# (64/36 split), and heavy L2 prevents the LR from overfitting the majority class.
LR_RUN_LINE_C: float = 0.01


class RunLineModel:
    def __init__(self):
        self.model_path    = _MODEL_PATH
        self.xgb: Optional[xgb.XGBClassifier] = None
        self.lr:  Optional[LogisticRegression] = None
        self.nn:  Optional[MLPClassifier]      = None
        self.scaler        = StandardScaler()
        self.nn_scaler     = StandardScaler()
        self.is_trained    = False
        self.lr_is_trained = False
        self.nn_is_trained = False
        self.xgb_cv:       Optional[float] = None
        self.lr_cv:        Optional[float] = None
        self.nn_val_accuracy: Optional[float] = None

    # ------------------------------------------------------------------
    # Training / loading
    # ------------------------------------------------------------------

    def train_or_load(self, stats_client, feature_builder, season: int,
                      force_retrain: bool = False,
                      high_change_team_ids: "set[int] | None" = None) -> str:
        if not force_retrain and self.model_path.exists():
            saved = joblib.load(self.model_path)
            # Verify saved model uses the current feature count (24)
            expected_n_feat = 24
            actual_n_feat   = getattr(saved.get("scaler"), "n_features_in_", expected_n_feat)
            if saved.get("target_type") == "run_line" and "lr" in saved \
                    and actual_n_feat == expected_n_feat:
                self.xgb              = saved["xgb"]
                self.lr               = saved["lr"]
                self.nn               = saved.get("nn")
                self.scaler           = saved["scaler"]
                self.nn_scaler        = saved.get("nn_scaler", StandardScaler())
                self.xgb_cv           = saved.get("xgb_cv")
                self.lr_cv            = saved.get("lr_cv")
                self.nn_val_accuracy  = saved.get("nn_val_accuracy")
                self.is_trained       = True
                self.lr_is_trained    = True
                self.nn_is_trained    = self.nn is not None
                xgb_s = f"{self.xgb_cv:.1%}" if self.xgb_cv else "N/A"
                lr_s  = f"{self.lr_cv:.1%}"  if self.lr_cv  else "N/A"
                nn_s  = f"{self.nn_val_accuracy:.1%}" if self.nn_val_accuracy else "N/A"
                return f"Loaded run line model (XGB CV: {xgb_s} | LR CV: {lr_s} | NN: {nn_s})"
            if actual_n_feat != expected_n_feat:
                print(f"  Run line: feature count changed ({actual_n_feat} → {expected_n_feat}) — retraining.")
        return self._train(stats_client, feature_builder, season, high_change_team_ids)

    def _train_nn(
        self,
        X_unscaled:     np.ndarray,
        y:              np.ndarray,
        sample_weights: "np.ndarray | None" = None,
    ) -> None:
        """Train NN with its own scaler; propagate sample_weights if provided."""
        from sklearn.model_selection import train_test_split

        if sample_weights is not None:
            X_tr_raw, X_val_raw, y_tr, y_val, sw_tr, _ = train_test_split(
                X_unscaled, y, sample_weights,
                test_size=0.15, random_state=42, stratify=y,
            )
        else:
            X_tr_raw, X_val_raw, y_tr, y_val = train_test_split(
                X_unscaled, y, test_size=0.15, random_state=42, stratify=y
            )
            sw_tr = None

        self.nn_scaler = StandardScaler()
        X_nn_tr  = self.nn_scaler.fit_transform(X_tr_raw)
        X_nn_val = self.nn_scaler.transform(X_val_raw)

        self.nn = MLPClassifier(
            hidden_layer_sizes=(128, 64, 32),
            activation="relu",
            alpha=0.001,
            batch_size=256,
            max_iter=400,
            random_state=42,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=20,
        )
        print(f"  Run line NN training on {len(y):,} games…")
        fit_kw = {"sample_weight": sw_tr} if sw_tr is not None else {}
        self.nn.fit(X_nn_tr, y_tr, **fit_kw)
        self.nn_val_accuracy = float(self.nn.score(X_nn_val, y_val))
        self.nn_is_trained   = True

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
            print(f"  Warning: could not fetch run-line season {season} ({exc}) — historical only.")
            completed = []
        X_rows, y_rows, game_team_pairs, skipped = [], [], [], 0

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

            vec = feature_builder.build_training_row(home_id, away_id)
            if vec is None:
                skipped += 1
                continue

            margin = int(home_score) - int(away_score)
            X_rows.append(vec)
            y_rows.append(1 if margin > 1.5 else 0)
            game_team_pairs.append((home_id, away_id))

        n = len(X_rows)
        X = np.vstack(X_rows) if n > 0 else np.empty((0, 24), dtype=np.float32)
        y = np.array(y_rows)

        # ── Augment with enriched historical run-line labels + recency weights ─
        X_combined, y_combined = X, y
        sample_weights = None
        try:
            from .enriched_historical_data import (
                build_enriched_dataset, get_enriched_seasons,
            )
            from .recency_weights import compute_sample_weights, build_boost_mask

            X_hist, _, y_rl_hist, _ = build_enriched_dataset()
            seasons_hist             = get_enriched_seasons()

            if len(y_rl_hist) >= 100:
                mask_prev = (seasons_hist == 2025)
                mask_old  = ~mask_prev

                X_hist_old  = X_hist[mask_old];   y_rl_old  = y_rl_hist[mask_old]
                X_hist_prev = X_hist[mask_prev];  y_rl_prev = y_rl_hist[mask_prev]

                n_old  = len(y_rl_old)
                n_prev = len(y_rl_prev)

                X_combined = np.vstack([X_hist_old, X_hist_prev, X])
                y_combined = np.concatenate([y_rl_old, y_rl_prev, y])

                boost_mask = (
                    build_boost_mask(game_team_pairs, set(high_change_team_ids))
                    if high_change_team_ids else None
                )
                sample_weights = compute_sample_weights(n_old, n_prev, n, boost_mask)

                boosted = int(boost_mask.sum()) if boost_mask is not None else 0
                print(
                    f"  Run line combined: {n_old:,} old + {n_prev:,} prev + {n} current "
                    f"= {len(y_combined):,} total  |  weights 15/25/60%"
                    + (f"  |  {boosted} boosted → 75%" if boosted else "")
                )
        except Exception as exc:
            print(f"  Run line: historical unavailable ({exc}) — current-season only")

        if len(y_combined) < 30:
            return f"Run line: insufficient data ({len(y_combined)} combined games)"

        X_scaled = self.scaler.fit_transform(X_combined)

        cv_fit_params = ({"sample_weight": sample_weights}
                         if sample_weights is not None else {})

        self.xgb = xgb.XGBClassifier(**XGB_RUN_LINE_PARAMS)
        try:
            cv_scores = cross_val_score(
                self.xgb, X_scaled, y_combined, cv=5, scoring="accuracy",
                fit_params=cv_fit_params,
            )
        except TypeError:
            cv_scores = cross_val_score(self.xgb, X_scaled, y_combined, cv=5, scoring="accuracy")
        self.xgb_cv = float(cv_scores.mean())
        self.xgb.fit(X_scaled, y_combined,
                     **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        # Attach feature names so SHAP / get_score() show real names, not f0..f23.
        try:
            from .sports_config import MLB_FEATURES
            self.xgb.get_booster().feature_names = list(MLB_FEATURES)
        except Exception:
            pass
        self.is_trained = True

        self.lr = LogisticRegression(
            C=LR_RUN_LINE_C, max_iter=2000, solver="lbfgs", random_state=42,
        )
        try:
            lr_scores = cross_val_score(
                self.lr, X_scaled, y_combined, cv=5, scoring="accuracy",
                fit_params=cv_fit_params,
            )
        except TypeError:
            lr_scores = cross_val_score(self.lr, X_scaled, y_combined, cv=5, scoring="accuracy")
        self.lr_cv = float(lr_scores.mean())
        self.lr.fit(X_scaled, y_combined,
                    **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        self.lr_is_trained = True

        self._train_nn(X_combined, y_combined, sample_weights)

        self.model_path.parent.mkdir(exist_ok=True)
        joblib.dump({
            "xgb": self.xgb, "lr": self.lr, "nn": self.nn,
            "scaler": self.scaler, "nn_scaler": self.nn_scaler,
            "xgb_cv": self.xgb_cv, "lr_cv": self.lr_cv,
            "nn_val_accuracy": self.nn_val_accuracy,
            "target_type": "run_line",
        }, self.model_path)

        nn_s = f"{self.nn_val_accuracy:.1%}" if self.nn_val_accuracy else "N/A"
        total_n = len(y_combined)
        return (f"Run line: XGB CV {self.xgb_cv:.1%} | LR CV {self.lr_cv:.1%} | "
                f"NN {nn_s} ({total_n:,} games)")

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, feature_vec: np.ndarray, game: dict,
                weights: dict | None = None) -> Optional[dict]:
        """Return run line prediction dict, or None if model not trained."""
        if not self.is_trained:
            return None

        try:
            X = self.scaler.transform(feature_vec.reshape(1, -1))
        except Exception:
            return None

        xgb_prob = float(self.xgb.predict_proba(X)[0, 1])
        lr_prob  = float(self.lr.predict_proba(X)[0, 1]) if self.lr_is_trained else xgb_prob

        nn_prob: Optional[float] = None
        if self.nn_is_trained and self.nn is not None:
            try:
                # nn_scaler was fitted on RAW (unscaled) data during _train_nn();
                # pass the original feature_vec, NOT the already-scaler-transformed X.
                X_nn    = self.nn_scaler.transform(feature_vec.reshape(1, -1))
                nn_prob = float(self.nn.predict_proba(X_nn)[0, 1])
            except Exception:
                nn_prob = None

        # ── Diagnostic: individual model probabilities ─────────────────────────
        matchup = f"{game.get('away_team','?')} @ {game.get('home_team','?')}"
        nn_str  = f"{nn_prob:.3f}" if nn_prob is not None else "N/A"
        print(
            f"  [RL diag] {matchup} | "
            f"XGB={xgb_prob:.3f}  LR={lr_prob:.3f}  NN={nn_str}"
        )

        w = weights or {}
        if nn_prob is not None:
            w_xgb   = float(w.get("xgb", 1 / 3))
            w_lr    = float(w.get("lr",  1 / 3))
            w_nn    = float(w.get("nn",  1 / 3))
            total_w = w_xgb + w_lr + w_nn
            if total_w > 0:
                combined = (xgb_prob * w_xgb + lr_prob * w_lr + nn_prob * w_nn) / total_w
                eff_w = {"xgb": w_xgb / total_w, "lr": w_lr / total_w, "nn": w_nn / total_w}
            else:
                combined = (xgb_prob + lr_prob + nn_prob) / 3.0
                eff_w = {"xgb": 1 / 3, "lr": 1 / 3, "nn": 1 / 3}
            probs    = [xgb_prob >= 0.5, lr_prob >= 0.5, nn_prob >= 0.5]
            models_agree = all(probs) or not any(probs)
        else:
            w_xgb   = float(w.get("xgb", 0.5))
            w_lr    = float(w.get("lr",  0.5))
            total_w = w_xgb + w_lr
            if total_w > 0:
                combined = (xgb_prob * w_xgb + lr_prob * w_lr) / total_w
                eff_w = {"xgb": w_xgb / total_w, "lr": w_lr / total_w, "nn": 0.0}
            else:
                combined = (xgb_prob + lr_prob) / 2.0
                eff_w = {"xgb": 0.5, "lr": 0.5, "nn": 0.0}
            models_agree = (xgb_prob >= 0.5) == (lr_prob >= 0.5)

        rl_home_odds = game.get("run_line_home_odds")
        rl_away_odds = game.get("run_line_away_odds")
        rl_point     = game.get("run_line_point", -1.5)

        if combined >= 0.5:
            side      = "home"
            pick_prob = combined
            pick_odds = int(rl_home_odds) if rl_home_odds is not None else -110
            pick_team = game.get("home_team", "")
        else:
            side      = "away"
            pick_prob = 1.0 - combined
            pick_odds = int(rl_away_odds) if rl_away_odds is not None else -110
            pick_team = game.get("away_team", "")

        if pick_odds > 0:
            market_prob = 100 / (pick_odds + 100)
        else:
            market_prob = abs(pick_odds) / (abs(pick_odds) + 100)

        edge     = pick_prob - market_prob
        is_value = models_agree and edge >= 0.05 and pick_odds > -300

        # ── Diagnostic: post-combination scores ───────────────────────────────
        print(
            f"  [RL diag] {matchup} | "
            f"combined={combined:.3f}  side={side}  "
            f"pick_prob={pick_prob:.3f}  market_prob={market_prob:.3f}  "
            f"edge={edge:+.3f}  agree={models_agree}  value={is_value}  "
            f"confidence={abs(combined - 0.5) * 2:.3f}"
        )

        return {
            "home_cover_prob":   combined,
            "xgb_prob":          xgb_prob,
            "lr_prob":           lr_prob,
            "nn_prob":           nn_prob,
            "effective_weights": eff_w,
            "models_agree":      models_agree,
            "conflict":          not models_agree,
            "side":            side,
            "pick_team":       pick_team,
            "pick_prob":       pick_prob,
            "pick_odds":       pick_odds,
            "market_prob":     market_prob,
            "edge":            edge,
            "value_bet":       is_value,
            "confidence":      abs(combined - 0.5) * 2,
            "run_line_point":  rl_point if rl_point is not None else -1.5,
            "run_line_home_odds": int(rl_home_odds) if rl_home_odds else -110,
            "run_line_away_odds": int(rl_away_odds) if rl_away_odds else -110,
        }

    def get_raw_model(self):
        return self.xgb

    def get_scaler(self):
        return self.scaler
