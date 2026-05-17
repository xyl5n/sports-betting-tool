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

# ── LR hurdle / conditional reformulation ────────────────────────────────────
# The LR component of the run-line ensemble is now trained as a CONDITIONAL
# classifier on the home-won subset:
#
#     P(margin >= 2)  =  P(margin > 0)  *  P(margin >= 2  |  margin > 0)
#                        └ ml_lr_prob ─┘   └─── self.lr (this model) ───┘
#
# Because the conditional factor is in [0, 1], the composed LR run-line
# probability is, by construction, always <= the moneyline LR probability
# for the same game. Two independent classifiers can't enforce that — a
# multiplicative hurdle can.
#
# A schema flag on the persisted joblib (`lr_target_kind = "conditional"`)
# tells train_or_load() to invalidate any older "marginal" LR cache.
LR_TARGET_KIND_CONDITIONAL = "conditional"


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
        # Column subset XGB trains/predicts on (market-derived features removed).
        self._xgb_cols:  Optional[list[int]] = None
        self._xgb_names: Optional[list[str]] = None

    # ------------------------------------------------------------------
    # Training / loading
    # ------------------------------------------------------------------

    def train_or_load(self, stats_client, feature_builder, season: int,
                      force_retrain: bool = False,
                      high_change_team_ids: "set[int] | None" = None) -> str:
        if not force_retrain and self.model_path.exists():
            saved = joblib.load(self.model_path)
            # Verify saved model uses the current feature count.
            from .sports_config import MLB_FEATURES
            expected_n_feat = len(MLB_FEATURES)
            actual_n_feat   = getattr(saved.get("scaler"), "n_features_in_", expected_n_feat)
            # Old caches stored a "marginal" LR (independent classifier on margin>=2).
            # The new LR is a conditional/hurdle model — caches lacking the schema
            # flag must be retrained so the LR head is regenerated correctly.
            lr_kind_ok = saved.get("lr_target_kind") == LR_TARGET_KIND_CONDITIONAL
            # XGB was also reformulated to a conditional model -- reject pre-fix caches.
            xgb_kind_ok = saved.get("xgb_target_kind") == "conditional_cover_given_home_win"
            # XGB input kind must be "market_free" (the pure-confidence feature subset).
            xgb_input_ok = saved.get("xgb_input_kind") == "market_free"
            # NN must also be on the conditional target (or the explicit
            # marginal-fallback flag if there weren't enough home-win rows).
            # An unflagged cache predates the NN hurdle reformulation and
            # would silently let the ensemble produce P(cover) > P(win).
            nn_kind_ok  = saved.get("nn_target_kind") in (
                "conditional_cover_given_home_win",
                "marginal_cover_minus_1_5",
            )
            if saved.get("target_type") == "run_line" and "lr" in saved \
                    and actual_n_feat == expected_n_feat \
                    and lr_kind_ok and xgb_kind_ok and nn_kind_ok and xgb_input_ok:
                self.xgb              = saved["xgb"]
                self.lr               = saved["lr"]
                self.nn               = saved.get("nn")
                self.scaler           = saved["scaler"]
                self.nn_scaler        = saved.get("nn_scaler", StandardScaler())
                self.xgb_cv           = saved.get("xgb_cv")
                self.lr_cv            = saved.get("lr_cv")
                self.nn_val_accuracy  = saved.get("nn_val_accuracy")
                self._xgb_cols        = saved.get("xgb_cols")
                self._xgb_names       = saved.get("xgb_names")
                self.is_trained       = True
                self.lr_is_trained    = True
                self.nn_is_trained    = self.nn is not None
                xgb_s = f"{self.xgb_cv:.1%}" if self.xgb_cv else "N/A"
                lr_s  = f"{self.lr_cv:.1%}"  if self.lr_cv  else "N/A"
                nn_s  = f"{self.nn_val_accuracy:.1%}" if self.nn_val_accuracy else "N/A"
                return f"Loaded run line model (XGB CV: {xgb_s} | LR CV: {lr_s} | NN: {nn_s})"
            if actual_n_feat != expected_n_feat:
                print(f"  Run line: feature count changed ({actual_n_feat} -> {expected_n_feat}) -- retraining.")
            elif not lr_kind_ok:
                print("  Run line: LR target kind upgraded to conditional -- retraining.")
            elif not xgb_kind_ok:
                print("  Run line: XGB target kind upgraded to conditional cover -- retraining.")
            elif not nn_kind_ok:
                print("  Run line: NN target kind upgraded to conditional cover -- retraining.")
            elif not xgb_input_ok:
                print("  Run line: XGB input kind upgraded to market_free -- retraining.")
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
        X_rows, y_rows, y_ml_rows, game_team_pairs, skipped = [], [], [], [], 0

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
            # Track home-win labels in parallel — used to subset the conditional
            # XGB training data without touching the LR/NN training paths.
            y_ml_rows.append(1 if int(home_score) > int(away_score) else 0)
            game_team_pairs.append((home_id, away_id))

        n = len(X_rows)
        from .sports_config import MLB_FEATURES
        X = np.vstack(X_rows) if n > 0 else np.empty((0, len(MLB_FEATURES)), dtype=np.float32)
        y = np.array(y_rows)
        y_ml = np.array(y_ml_rows, dtype=np.int32)

        # ── Augment with enriched historical run-line labels + recency weights ─
        X_combined,  y_combined  = X, y
        y_ml_combined            = y_ml
        sample_weights           = None
        try:
            from .enriched_historical_data import (
                build_enriched_dataset, get_enriched_seasons,
            )
            from .recency_weights import compute_sample_weights, build_boost_mask

            X_hist, y_ml_hist, y_rl_hist, _ = build_enriched_dataset()
            seasons_hist                     = get_enriched_seasons()

            if len(y_rl_hist) >= 100:
                mask_prev = (seasons_hist == 2025)
                mask_old  = ~mask_prev

                X_hist_old  = X_hist[mask_old]
                y_rl_old    = y_rl_hist[mask_old]
                y_ml_old    = y_ml_hist[mask_old]

                X_hist_prev = X_hist[mask_prev]
                y_rl_prev   = y_rl_hist[mask_prev]
                y_ml_prev   = y_ml_hist[mask_prev]

                n_old  = len(y_rl_old)
                n_prev = len(y_rl_prev)

                X_combined    = np.vstack([X_hist_old, X_hist_prev, X])
                y_combined    = np.concatenate([y_rl_old, y_rl_prev, y])
                y_ml_combined = np.concatenate([y_ml_old, y_ml_prev, y_ml])

                boost_mask = (
                    build_boost_mask(game_team_pairs, set(high_change_team_ids))
                    if high_change_team_ids else None
                )
                sample_weights = compute_sample_weights(n_old, n_prev, n, boost_mask)

                boosted = int(boost_mask.sum()) if boost_mask is not None else 0
                print(
                    f"  Run line combined: {n_old:,} old + {n_prev:,} prev + {n} current "
                    f"= {len(y_combined):,} total  |  weights 15/25/60%"
                    + (f"  |  {boosted} boosted -> 75%" if boosted else "")
                )
        except Exception as exc:
            print(f"  Run line: historical unavailable ({exc}) -- current-season only")

        if len(y_combined) < 30:
            return f"Run line: insufficient data ({len(y_combined)} combined games)"

        X_scaled = self.scaler.fit_transform(X_combined)

        cv_fit_params = ({"sample_weight": sample_weights}
                         if sample_weights is not None else {})

        # ── XGB: CONDITIONAL P(margin >= 2 | home wins) on a PURE-CONFIDENCE
        # feature subset (market-derived columns removed) ────────────────────
        #
        # Two structural constraints, both enforced at the model level rather
        # than by post-processing:
        #
        #   1. P(covers) <= P(wins)
        #      Achieved by training only on home-win rows so the target is
        #      P(margin >= 2 | home wins) in [0, 1].  predict() then returns
        #      ml_prob_home * cond_prob, bounded above by ml_prob_home.
        #
        #   2. Confidence references no market signal.
        #      Achieved by stripping the odds-derived columns
        #      (home_implied_prob, run_line, line_movement) from the feature
        #      vector before fitting.  Edge against the market is computed
        #      separately downstream.
        from .sports_config import (
            MLB_XGB_CONFIDENCE_COLUMNS,
            MLB_XGB_CONFIDENCE_FEATURE_NAMES,
        )
        self._xgb_cols  = list(MLB_XGB_CONFIDENCE_COLUMNS)
        self._xgb_names = list(MLB_XGB_CONFIDENCE_FEATURE_NAMES)
        X_xgb_full      = X_scaled[:, self._xgb_cols]

        home_won_mask = (y_ml_combined == 1)
        X_cond  = X_xgb_full[home_won_mask]
        y_cond  = y_combined[home_won_mask]   # 1 iff margin >= 2 given home won
        sw_cond = (sample_weights[home_won_mask]
                   if sample_weights is not None else None)
        cv_fit_params_cond = ({"sample_weight": sw_cond}
                              if sw_cond is not None else {})

        self.xgb = xgb.XGBClassifier(**XGB_RUN_LINE_PARAMS)
        if len(y_cond) >= 30 and len(np.unique(y_cond)) >= 2:
            try:
                cv_scores = cross_val_score(
                    self.xgb, X_cond, y_cond, cv=5, scoring="accuracy",
                    fit_params=cv_fit_params_cond,
                )
            except TypeError:
                cv_scores = cross_val_score(
                    self.xgb, X_cond, y_cond, cv=5, scoring="accuracy",
                )
            self.xgb_cv = float(cv_scores.mean())
            self.xgb.fit(
                X_cond, y_cond,
                **({"sample_weight": sw_cond} if sw_cond is not None else {}),
            )
            print(f"  Run line XGB: CONDITIONAL P(margin>=2 | home wins), "
                  f"pure-confidence features only -- "
                  f"{len(y_cond):,} rows  base={y_cond.mean():.1%}  CV={self.xgb_cv:.1%}")
        else:
            # Degenerate: not enough home-win rows.  Train marginal as fallback
            # so predict() still returns something.  Still on the pure-confidence
            # feature subset.
            self.xgb.fit(
                X_xgb_full, y_combined,
                **({"sample_weight": sample_weights} if sample_weights is not None else {}),
            )
            self.xgb_cv = None
            print("  Run line XGB: fallback to marginal training (too few home-win rows)")

        # Attach the actual feature names XGB trained on (subset of MLB_FEATURES).
        try:
            self.xgb.get_booster().feature_names = list(self._xgb_names)
        except Exception:
            pass
        self.is_trained = True

        # ── LR: train CONDITIONAL P(margin >= 2 | home wins) ────────────────
        # Same hurdle reformulation as XGB above. Two independent classifiers
        # on margin>=2 vs margin>0 can produce P_rl > P_ml for the same game,
        # which is mathematically impossible. Training LR on the home-won
        # subset with label (margin >= 2) makes its raw output a number in
        # [0,1] that predict() multiplies by the moneyline LR's home_win_prob
        # to get the joint P(home covers -1.5). The product is, by
        # construction, always <= ml_lr_prob — no clip needed.
        self.lr = LogisticRegression(
            C=LR_RUN_LINE_C, max_iter=2000, solver="lbfgs", random_state=42,
        )
        if len(y_cond) >= 30 and len(np.unique(y_cond)) >= 2:
            try:
                lr_scores = cross_val_score(
                    self.lr, X_cond, y_cond, cv=5, scoring="accuracy",
                    fit_params=cv_fit_params_cond,
                )
            except TypeError:
                lr_scores = cross_val_score(self.lr, X_cond, y_cond, cv=5, scoring="accuracy")
            self.lr_cv = float(lr_scores.mean())
            self.lr.fit(
                X_cond, y_cond,
                **({"sample_weight": sw_cond} if sw_cond is not None else {}),
            )
            print(f"  Run line LR:  CONDITIONAL P(margin>=2 | home wins) on "
                  f"{len(y_cond):,} rows  base={y_cond.mean():.1%}  CV={self.lr_cv:.1%}")
        else:
            # Degenerate fallback — mirror the XGB fallback above so the model
            # still has a usable LR head when there are too few home-win rows.
            self.lr.fit(
                X_scaled, y_combined,
                **({"sample_weight": sample_weights} if sample_weights is not None else {}),
            )
            self.lr_cv = None
            print("  Run line LR:  fallback to marginal training (too few home-win rows)")
        self.lr_is_trained = True

        # ── NN: train CONDITIONAL P(margin >= 2 | home wins) ────────────────
        # Same hurdle reformulation as XGB and LR above.  Without this, the
        # NN sub-component would still let the ensemble's P(home covers -1.5)
        # exceed P(home wins) for the same game.  We pass the RAW (un-scaled)
        # X_combined subset because _train_nn fits its own nn_scaler.
        # predict() multiplies the NN output by the moneyline NN's
        # home-win probability so the joint NN P(home covers) is bounded
        # above by P(home wins) BY CONSTRUCTION.
        X_combined_cond = X_combined[home_won_mask]
        sw_combined_cond = (sample_weights[home_won_mask]
                            if sample_weights is not None else None)
        nn_target_kind = "conditional_cover_given_home_win"
        if len(y_cond) >= 30 and len(np.unique(y_cond)) >= 2:
            self._train_nn(X_combined_cond, y_cond, sw_combined_cond)
            if self.nn_is_trained:
                print(f"  Run line NN:  CONDITIONAL P(margin>=2 | home wins) on "
                      f"{len(y_cond):,} rows  base={y_cond.mean():.1%}  "
                      f"val={self.nn_val_accuracy:.1%}")
        else:
            # Degenerate fallback — mirror the XGB/LR fallbacks so the NN
            # still has a usable head if there are too few home-win rows.
            self._train_nn(X_combined, y_combined, sample_weights)
            nn_target_kind = "marginal_cover_minus_1_5"
            if self.nn_is_trained:
                print("  Run line NN:  fallback to marginal training "
                      "(too few home-win rows)")

        self.model_path.parent.mkdir(exist_ok=True)
        joblib.dump({
            "xgb": self.xgb, "lr": self.lr, "nn": self.nn,
            "scaler": self.scaler, "nn_scaler": self.nn_scaler,
            "xgb_cv": self.xgb_cv, "lr_cv": self.lr_cv,
            "nn_val_accuracy": self.nn_val_accuracy,
            "target_type":     "run_line",
            # XGB now models the conditional P(margin >= 2 | home wins);
            # predict() must multiply by the moneyline xgb_prob to recover
            # the joint P(home covers -1.5).  Old caches without this flag
            # are still marginal and will be detected as stale.
            "xgb_target_kind": "conditional_cover_given_home_win",
            # XGB also drops market-derived features (home_implied_prob,
            # run_line, line_movement) so the probability output is "pure
            # confidence" -- a team / pitcher / situation signal only.
            "xgb_input_kind":  "market_free",
            "xgb_cols":        self._xgb_cols,
            "xgb_names":       self._xgb_names,
            # LR follows the same hurdle structure as XGB — see the
            # LR_TARGET_KIND_CONDITIONAL constant. Old caches without this
            # flag are invalidated by train_or_load().
            "lr_target_kind":  LR_TARGET_KIND_CONDITIONAL,
            # NN now follows the same hurdle structure — predict() multiplies
            # its raw output by the moneyline NN's home-win probability so the
            # final NN P(home covers -1.5) is bounded above by P(home wins).
            "nn_target_kind":  nn_target_kind,
        }, self.model_path)

        nn_s = f"{self.nn_val_accuracy:.1%}" if self.nn_val_accuracy else "N/A"
        total_n = len(y_combined)
        return (f"Run line: XGB CV {self.xgb_cv:.1%} | LR CV {self.lr_cv:.1%} | "
                f"NN {nn_s} ({total_n:,} games)")

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, feature_vec: np.ndarray, game: dict,
                weights:        dict  | None = None,
                ml_prob_home:    float | None = None,
                ml_lr_prob_home: float | None = None,
                ml_nn_prob_home: float | None = None) -> Optional[dict]:
        """
        Return run line prediction dict, or None if model not trained.

        ml_prob_home    : moneyline P(home wins) from the XGB head of the
            BettingModel. Required to recover the joint P(home covers -1.5)
            from the conditional XGB output.
        ml_lr_prob_home : moneyline P(home wins) from the LR head of the
            BettingModel. Used to recover the joint LR run-line probability
            from the conditional LR output. Defaults to ml_prob_home when
            absent so legacy callers still work, then to league-average 0.54
            when both are absent.
        ml_nn_prob_home : moneyline P(home wins) from the NN head of the
            BettingModel. Used to recover the joint NN run-line probability
            from the conditional NN output. Defaults to ml_lr_prob_home then
            ml_prob_home when absent so legacy callers still work.

        All three composed outputs satisfy x_rl <= x_ml for their respective
        classifier (xgb / lr / nn) by construction, fixing the inconsistency
        where independent classifiers could produce rl conf > ml conf for the
        same picked team.  Since the ensemble is a weighted average of three
        bounded values, the ensemble's combined P(cover) is also bounded
        above by the moneyline ensemble's P(win).
        """
        if not self.is_trained:
            return None

        try:
            X = self.scaler.transform(feature_vec.reshape(1, -1))
        except Exception:
            return None

        # Both heads now model P(margin >= 2 | home wins) — conditional probs.
        # Both XGB and LR run on the pure-confidence feature subset (no market
        # columns) because the LR was also trained on that same subset
        # (X_cond = X_scaled[:, self._xgb_cols][home_won_mask] in _train).
        X_xgb = X[:, self._xgb_cols] if self._xgb_cols is not None else X
        xgb_cond = float(self.xgb.predict_proba(X_xgb)[0, 1])
        lr_cond  = (float(self.lr.predict_proba(X_xgb)[0, 1])
                    if self.lr_is_trained else xgb_cond)

        # Multiply each classifier's conditional output by THAT classifier's
        # moneyline probability so the per-classifier constraint
        # P_rl_x <= P_ml_x holds by construction (no clip).
        ml_xgb_p = float(ml_prob_home)    if ml_prob_home    is not None else 0.54
        ml_lr_p  = (float(ml_lr_prob_home) if ml_lr_prob_home is not None
                    else ml_xgb_p)
        ml_xgb_p = max(0.0, min(1.0, ml_xgb_p))
        ml_lr_p  = max(0.0, min(1.0, ml_lr_p))

        xgb_prob = ml_xgb_p * xgb_cond
        lr_prob  = ml_lr_p  * lr_cond if self.lr_is_trained else xgb_prob

        # ── Silent LR-only pick recorder (does not affect ensemble output) ────
        if self.lr_is_trained:
            try:
                from .lr_picks_tracker import record_lr_pick
                record_lr_pick(
                    sport        = "MLB",   # RunLineModel is MLB-only
                    home_team    = game.get("home_team", ""),
                    away_team    = game.get("away_team", ""),
                    game_date    = (game.get("commence_time") or "")[:10],
                    bet_type     = "run_line",
                    lr_prob_home = lr_prob,
                    game_id      = game.get("id") or game.get("game_id"),
                )
            except Exception:
                pass

        # ── Silent XGB-only pick recorder (does not affect ensemble output) ───
        # We record the JOINT probability (ml_p * cond_prob) so the recorded
        # value matches what the ensemble actually used.
        try:
            from .xgb_picks_tracker import record_classifier_pick
            record_classifier_pick(
                bet_type = "run_line",
                game     = game,
                xgb_prob = xgb_prob,
                sport    = "MLB",   # RunLineModel is MLB-only
            )
        except Exception:
            pass

        # NN models P(margin >= 2 | home wins) — same hurdle reformulation
        # as XGB/LR above. Compose with the moneyline NN's home-win prob to
        # recover the joint P(home covers -1.5).  Product is bounded above
        # by ml_nn_p, so the per-classifier inequality holds by construction.
        ml_nn_p = (float(ml_nn_prob_home) if ml_nn_prob_home is not None
                   else ml_lr_p)
        ml_nn_p = max(0.0, min(1.0, ml_nn_p))

        nn_prob: Optional[float] = None
        if self.nn_is_trained and self.nn is not None:
            try:
                # nn_scaler was fitted on RAW (unscaled) data during _train_nn();
                # pass the original feature_vec, NOT the already-scaler-transformed X.
                X_nn    = self.nn_scaler.transform(feature_vec.reshape(1, -1))
                nn_cond = float(self.nn.predict_proba(X_nn)[0, 1])
                nn_prob = ml_nn_p * nn_cond
            except Exception:
                nn_prob = None

        # ── NN-only pick logging (silent side-channel) ──────────────────
        # Mirrors the lr/xgb trackers above but writes to nn_picks_history;
        # never influences ensemble math or the recommended pick.
        if nn_prob is not None:
            try:
                from .nn_picks import record_nn_pick
                _ct   = str(game.get("commence_time") or "")
                _date = _ct[:10] if len(_ct) >= 10 else ""
                if _date:
                    record_nn_pick(
                        game_date = _date,
                        matchup   = f"{game.get('away_team','?')} @ {game.get('home_team','?')}",
                        sport     = "MLB",
                        bet_type  = "run_line",
                        nn_prob   = nn_prob,
                        nn_pick   = "home" if nn_prob >= 0.5 else "away",
                    )
            except Exception:
                pass

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

        # ── Step 1 → Step 2 separation ────────────────────────────────────────
        # `pick_prob` is the model's pure-confidence probability for the
        # picked side (no odds reference). `edge` is the separate Step-2
        # quantity. `confidence_tier` is a function of pick_prob ONLY.
        from .kelly import confidence_tier_from_prob
        conf_tier = confidence_tier_from_prob(pick_prob)

        return {
            "home_cover_prob":   combined,
            "xgb_prob":          xgb_prob,
            "lr_prob":           lr_prob,
            "nn_prob":           nn_prob,
            "effective_weights": eff_w,
            "models_agree":      models_agree,
            "conflict":          not models_agree,
            "side":            side,
            "pick_side":       side,                # alias for cross-model consistency
            "pick_team":       pick_team,
            "pick_prob":       pick_prob,
            "pick_odds":       pick_odds,
            "market_prob":     market_prob,
            "edge":            edge,
            "value_bet":       is_value,
            "confidence":      abs(combined - 0.5) * 2,
            "confidence_tier": conf_tier,           # Strong/Moderate/Low by pick_prob
            "run_line_point":  rl_point if rl_point is not None else -1.5,
            "run_line_home_odds": int(rl_home_odds) if rl_home_odds else -110,
            "run_line_away_odds": int(rl_away_odds) if rl_away_odds else -110,
        }

    def get_raw_model(self):
        return self.xgb

    def get_scaler(self):
        return self.scaler
