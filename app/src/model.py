"""
Three-model ensemble: XGBoost + Logistic Regression + Neural Network.

XGBoost and LR train on the current-season API-Sports feature vectors.
The Neural Network (MLPClassifier, 3 hidden layers) trains on a combined
dataset of ~7 000 historical game rows (Retrosheet + pybaseball) PLUS the
current-season data, giving it far more signal on team-stat features.
NN training only runs for MLB (historical data is MLB-only).

predict() returns probabilities from each model independently plus a
combined average.  `models_agree` is True only when all available models
pick the same winner (unanimous vote); conflicted games should be skipped.
"""
from pathlib import Path
from typing import Optional, Protocol

import joblib
import numpy as np
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

from .sports_config import SportConfig

_MLB_ODDS_KEY = "baseball_mlb"

# ── Moneyline XGBoost hyperparameters ────────────────────────────────────────
# Tuned independently from the run-line model (see run_line_model.py).
# Lower min_child_weight / gamma vs. run-line because the moneyline signal is
# weaker (~58% vs. ~65% CV) and was being over-regularized.
# n_estimators=100, max_depth=3 chosen by 5-fold CV grid sweep on the enriched
# historical dataset (8,934 rows) — see xgb_hp_search.py. The previous 200x4
# config was overfitting: CV 58.65% -> 59.30% with the smaller forest.
XGB_MONEYLINE_PARAMS = dict(
    n_estimators=100,
    max_depth=3,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    min_child_weight=2,
    gamma=0.3,
    reg_lambda=2.0,
    eval_metric="logloss",
    random_state=42,
)

# ── Moneyline Logistic Regression regularisation ─────────────────────────────
# Independent of RunLineModel._LR_C — the two targets prefer different C.
# Tuned via 5-fold CV sweep over {0.01, 0.1, 0.5, 1.0, 2.0, 5.0} on the
# enriched historical dataset (see tune_lr.py). C=2.0 won; the gain over
# the prior C=1.0 default is small but consistent (0.5957 vs 0.5955 CV).
LR_MONEYLINE_C: float = 2.0


class _StatsClient(Protocol):
    def get_completed_games(self, season: int) -> list[dict]: ...


class _FeatureBuilder(Protocol):
    def build_training_row(self, home_id: int, away_id: int) -> Optional[np.ndarray]: ...


class BettingModel:
    def __init__(self, sport: SportConfig):
        self.sport          = sport
        self.model_path     = Path(f".cache/model_{sport.odds_key}.joblib")
        self.xgb:            Optional[xgb.XGBClassifier]  = None
        self.lr:             Optional[LogisticRegression]  = None
        self.nn:             Optional[MLPClassifier]       = None
        self.scaler          = StandardScaler()
        self.nn_scaler:      Optional[StandardScaler]      = None
        self.is_trained      = False
        self.lr_is_trained   = False
        self.nn_is_trained   = False
        self.cv_accuracy:     Optional[float] = None
        self.lr_cv_accuracy:  Optional[float] = None
        self.nn_val_accuracy: Optional[float] = None   # held-out val accuracy

    # ------------------------------------------------------------------
    # Training / loading
    # ------------------------------------------------------------------

    def train_or_load(
        self,
        stats_client: _StatsClient,
        feature_builder: _FeatureBuilder,
        season: int,
        force_retrain: bool = False,
    ) -> str:
        if not force_retrain and self.model_path.exists():
            saved = joblib.load(self.model_path)
            loaded_scaler = saved["scaler"]
            expected = len(self.sport.feature_names)
            actual   = getattr(loaded_scaler, "n_features_in_", expected)

            if actual != expected:
                print(f"  Feature count changed ({actual} → {expected}) — retraining.")
                return self._train(stats_client, feature_builder, season)

            if "lr" not in saved:
                print("  LR model missing from saved file — retraining.")
                return self._train(stats_client, feature_builder, season)

            # For MLB: retrain if the NN hasn't been added yet
            if self.sport.odds_key == _MLB_ODDS_KEY and "nn_scaler" not in saved:
                print("  Neural network not in saved model — retraining to add it.")
                return self._train(stats_client, feature_builder, season)

            self.xgb          = saved["xgb"]
            self.lr           = saved["lr"]
            self.nn           = saved.get("nn")
            self.nn_scaler    = saved.get("nn_scaler")
            self.scaler       = loaded_scaler
            self.cv_accuracy     = saved.get("cv_accuracy")
            self.lr_cv_accuracy  = saved.get("lr_cv_accuracy")
            self.nn_val_accuracy = saved.get("nn_val_accuracy")
            self.is_trained    = True
            self.lr_is_trained = self.lr is not None
            self.nn_is_trained = self.nn is not None

            xgb_s = f"{self.cv_accuracy:.1%}"     if self.cv_accuracy     else "N/A"
            lr_s  = f"{self.lr_cv_accuracy:.1%}"  if self.lr_cv_accuracy  else "N/A"
            status = f"Loaded saved {self.sport.name} model (XGB CV: {xgb_s} | LR CV: {lr_s}"
            if self.nn_is_trained:
                nn_s = f"{self.nn_val_accuracy:.1%}" if self.nn_val_accuracy else "N/A"
                status += f" | NN val: {nn_s}"
            status += ")"
            return status

        return self._train(stats_client, feature_builder, season)

    def _train(
        self,
        stats_client: _StatsClient,
        feature_builder: _FeatureBuilder,
        season: int,
        high_change_team_ids: "set[int] | None" = None,
    ) -> str:
        """
        Train all three models.

        high_change_team_ids : set of team IDs whose current-season win-rate
            differs from the previous season by > 15 pp.  Games involving these
            teams in the current season receive the boosted 75 % weight instead
            of the standard 60 % weight.
        """
        print(f"  Collecting {self.sport.name} completed games for training…")
        try:
            completed = stats_client.get_completed_games(season)
        except Exception as exc:
            print(f"  Warning: could not fetch {self.sport.name} season {season} ({exc})")
            print("  Proceeding with historical data only (no current-season rows).")
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

            X_rows.append(vec)
            y_rows.append(1 if int(home_score) > int(away_score) else 0)
            game_team_pairs.append((home_id, away_id))

        n = len(X_rows)
        print(f"  Built {n} current-season training samples ({skipped} skipped)")

        X = np.vstack(X_rows) if n > 0 else np.empty((0, len(self.sport.feature_names)), dtype=np.float32)
        y = np.array(y_rows)

        # ── For MLB: augment with enriched historical data + recency weights ──
        X_combined, y_combined = X, y
        sample_weights = None

        if self.sport.odds_key == _MLB_ODDS_KEY:
            try:
                from .enriched_historical_data import get_enriched_X_y, get_enriched_seasons
                from .recency_weights import compute_sample_weights, build_boost_mask

                X_hist, y_hist = get_enriched_X_y()
                seasons_hist   = get_enriched_seasons()

                if len(y_hist) >= 100:
                    # Split historical rows into old (≤2024) and previous-season (2025)
                    mask_prev = (seasons_hist == 2025)
                    mask_old  = ~mask_prev

                    X_hist_old  = X_hist[mask_old];   y_hist_old  = y_hist[mask_old]
                    X_hist_prev = X_hist[mask_prev];  y_hist_prev = y_hist[mask_prev]

                    n_old  = len(y_hist_old)
                    n_prev = len(y_hist_prev)

                    # Layout: [old | prev (2025) | current (2026)]
                    X_combined = np.vstack([X_hist_old, X_hist_prev, X])
                    y_combined = np.concatenate([y_hist_old, y_hist_prev, y])

                    # Build boost mask for high-change teams in current season
                    boost_mask = (
                        build_boost_mask(game_team_pairs, set(high_change_team_ids))
                        if high_change_team_ids else None
                    )

                    sample_weights = compute_sample_weights(n_old, n_prev, n, boost_mask)

                    boosted = int(boost_mask.sum()) if boost_mask is not None else 0
                    print(
                        f"  Combined: {n_old:,} old + {n_prev:,} prev-season + {n} current "
                        f"= {len(y_combined):,} total  |  "
                        f"weights 15/25/60%"
                        + (f"  |  {boosted} current rows boosted → 75%" if boosted else "")
                    )
            except Exception as exc:
                print(f"  Warning: enriched historical unavailable ({exc}) — "
                      f"training on current-season only (no recency weighting)")

        # ── Final training size check ─────────────────────────────────────────
        n_total_combined = len(y_combined)
        if n_total_combined < self.sport.min_training_games:
            self.is_trained = self.lr_is_trained = self.nn_is_trained = False
            return (
                f"Insufficient {self.sport.name} training data "
                f"({n_total_combined} combined games < {self.sport.min_training_games}). "
                f"Heuristic fallback active."
            )

        X_scaled = self.scaler.fit_transform(X_combined)

        # ── XGBoost (moneyline) ──────────────────────────────────────────────
        self.xgb = xgb.XGBClassifier(**XGB_MONEYLINE_PARAMS)
        cv_fit_params = ({"sample_weight": sample_weights}
                         if sample_weights is not None else {})
        try:
            xgb_cv = cross_val_score(
                self.xgb, X_scaled, y_combined, cv=5, scoring="accuracy",
                fit_params=cv_fit_params,
            )
        except TypeError:
            # Older sklearn versions don't support fit_params in cross_val_score
            xgb_cv = cross_val_score(self.xgb, X_scaled, y_combined, cv=5, scoring="accuracy")
        self.cv_accuracy = float(xgb_cv.mean())
        self.xgb.fit(X_scaled, y_combined,
                     **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        # Attach feature names so SHAP / get_score() show real names, not f0..f23.
        self.xgb.get_booster().feature_names = list(self.sport.feature_names)
        self.is_trained = True

        # ── Logistic Regression ───────────────────────────────────────────────
        self.lr = LogisticRegression(
            C=LR_MONEYLINE_C, max_iter=2000, solver="lbfgs", random_state=42,
        )
        try:
            lr_cv = cross_val_score(
                self.lr, X_scaled, y_combined, cv=5, scoring="accuracy",
                fit_params=cv_fit_params,
            )
        except TypeError:
            lr_cv = cross_val_score(self.lr, X_scaled, y_combined, cv=5, scoring="accuracy")
        self.lr_cv_accuracy = float(lr_cv.mean())
        self.lr.fit(X_scaled, y_combined,
                    **({"sample_weight": sample_weights} if sample_weights is not None else {}))
        self.lr_is_trained = True

        # ── Neural Network (MLB only — requires historical volume) ────────────
        self.nn           = None
        self.nn_scaler    = None
        self.nn_is_trained = False
        self.nn_val_accuracy = None
        nn_status = ""

        if self.sport.odds_key == _MLB_ODDS_KEY:
            nn_status = self._train_nn(X_combined, y_combined, sample_weights)

        # ── Persist ───────────────────────────────────────────────────────────
        self.model_path.parent.mkdir(exist_ok=True)
        joblib.dump(
            {
                "xgb":             self.xgb,
                "lr":              self.lr,
                "nn":              self.nn,
                "nn_scaler":       self.nn_scaler,
                "scaler":          self.scaler,
                "cv_accuracy":     self.cv_accuracy,
                "lr_cv_accuracy":  self.lr_cv_accuracy,
                "nn_val_accuracy": self.nn_val_accuracy,
            },
            self.model_path,
        )

        status = (
            f"Trained {self.sport.name} model on {n} games | "
            f"XGB CV: {self.cv_accuracy:.1%} | LR CV: {self.lr_cv_accuracy:.1%}"
        )
        if nn_status:
            status += f" | {nn_status}"
        return status

    def _train_nn(
        self,
        X_combined:     np.ndarray,
        y_combined:     np.ndarray,
        sample_weights: "np.ndarray | None" = None,
    ) -> str:
        """
        Train a 2-layer MLP wrapped in isotonic probability calibration on
        pre-combined (historical + current-season) data.  Uses a separate
        StandardScaler so XGB/LR scaling is unaffected.

        Keeps the best model from the 80/20 validation split — no full-data
        refit.  The base MLP uses its own internal early-stopping split
        inside each calibration fold.

        sample_weights: if provided, split alongside X/y and passed to fit().
        """
        if len(y_combined) < 100:
            return "NN skipped (insufficient data)"

        # Widen base (n, 24) → (n, 30) by appending NN-only player-level
        # extras.  XGB/LR were already fitted on the unwidened matrix above
        # and use the shared `self.scaler`; this widening is NN-only.
        from .nn_player_features import widen_to_nn_features
        X_nn = widen_to_nn_features(X_combined).astype(np.float32)

        self.nn_scaler = StandardScaler()
        X_all_scaled   = self.nn_scaler.fit_transform(X_nn)

        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.model_selection import train_test_split

        # 80/20 split for hold-out validation accuracy; propagate weights
        if sample_weights is not None:
            X_tr, X_val, y_tr, y_val, sw_tr, _ = train_test_split(
                X_all_scaled, y_combined, sample_weights,
                test_size=0.20, random_state=42, stratify=y_combined,
            )
        else:
            X_tr, X_val, y_tr, y_val = train_test_split(
                X_all_scaled, y_combined, test_size=0.20, random_state=42,
                stratify=y_combined,
            )
            sw_tr = None

        base_mlp = MLPClassifier(
            hidden_layer_sizes=(64, 32),
            activation="relu",
            alpha=0.01,
            batch_size=256,
            learning_rate="adaptive",
            max_iter=400,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=20,
            random_state=42,
        )
        self.nn = CalibratedClassifierCV(
            estimator=base_mlp,
            method="isotonic",
            cv=5,
        )

        print(
            f"  Training calibrated neural network "
            f"(isotonic, 5-fold) on {len(y_tr):,} games | val on {len(y_val):,}…"
        )
        fit_kw = {"sample_weight": sw_tr} if sw_tr is not None else {}
        self.nn.fit(X_tr, y_tr, **fit_kw)

        self.nn_val_accuracy = float(np.mean(self.nn.predict(X_val) == y_val))
        self.nn_is_trained   = True

        return f"NN val: {self.nn_val_accuracy:.1%} ({len(y_combined):,} games, calibrated)"

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, feature_vec: np.ndarray, weights: dict | None = None) -> dict:
        """
        Returns:
          home_win_prob     — weighted ensemble probability (primary output)
          xgb_prob          — XGBoost probability
          lr_prob           — Logistic Regression probability
          nn_prob           — Neural Network probability (None if not trained)
          models_agree      — True when ALL available models pick the same winner
          effective_weights — normalised weights actually used {xgb, lr, nn}
          confidence        — combined |prob - 0.5| * 2
          method            — descriptor string

        weights: dict with keys "xgb", "lr", "nn" (floats, need not sum to 1).
                 Defaults to equal weighting when None or when total_w == 0.
        """
        try:
            X = self.scaler.transform(feature_vec.reshape(1, -1))
            scaled_ok = True
        except Exception:
            scaled_ok = False

        # XGBoost
        if scaled_ok and self.is_trained and self.xgb is not None:
            xgb_prob   = float(self.xgb.predict_proba(X)[0, 1])
            xgb_method = "xgboost"
        else:
            xgb_prob   = _heuristic_prob(feature_vec, self.sport)
            xgb_method = "heuristic"

        # Logistic Regression
        if scaled_ok and self.lr_is_trained and self.lr is not None:
            lr_prob   = float(self.lr.predict_proba(X)[0, 1])
            lr_method = "logistic"
        else:
            lr_prob   = _heuristic_prob(feature_vec, self.sport)
            lr_method = "heuristic"

        # Neural Network
        nn_prob:   Optional[float] = None
        nn_method  = "n/a"
        if self.nn_is_trained and self.nn is not None and self.nn_scaler is not None:
            try:
                from .nn_player_features import widen_to_nn_features
                # Live _assemble() returns a 30-col vector; training-row paths
                # return 24.  Widen here so the NN scaler always sees its
                # expected feature count regardless of caller.
                v_nn      = widen_to_nn_features(feature_vec).reshape(1, -1)
                X_nn      = self.nn_scaler.transform(v_nn)
                raw_prob  = float(self.nn.predict_proba(X_nn)[0, 1])
                # Hard clip prevents saturated NN outputs from blowing up the
                # ensemble even when calibration leaves a tail near 0/1.
                nn_prob   = float(np.clip(raw_prob, 0.05, 0.95))
                nn_method = "neural_net"
            except Exception:
                nn_prob = None

        # Weighted ensemble probability and consensus
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
            xgb_home = xgb_prob >= 0.5
            lr_home  = lr_prob  >= 0.5
            nn_home  = nn_prob  >= 0.5
            models_agree = (xgb_home == lr_home == nn_home)
            method_str   = f"ensemble ({xgb_method}+{lr_method}+{nn_method})"
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
            method_str   = f"ensemble ({xgb_method}+{lr_method})"

        return {
            "home_win_prob":      combined,
            "xgb_prob":           xgb_prob,
            "lr_prob":            lr_prob,
            "nn_prob":            nn_prob,
            "effective_weights":  eff_w,
            "confidence":         abs(combined - 0.5) * 2,
            "xgb_confidence":     abs(xgb_prob - 0.5) * 2,
            "lr_confidence":      abs(lr_prob  - 0.5) * 2,
            "nn_confidence":      abs(nn_prob  - 0.5) * 2 if nn_prob is not None else None,
            "models_agree":       models_agree,
            "method":             method_str,
        }

    def get_raw_model(self):
        return self.xgb

    def get_scaler(self):
        return self.scaler


# ------------------------------------------------------------------
# Heuristic fallback
# ------------------------------------------------------------------

def _heuristic_prob(vec: np.ndarray, sport: SportConfig) -> float:
    normalised = vec / (sport.heuristic_stds + 1e-6)
    logit      = float(np.dot(normalised, sport.heuristic_weights))
    logit     += sport.home_field_logit
    return float(np.clip(1.0 / (1.0 + np.exp(-logit)), 0.05, 0.95))
