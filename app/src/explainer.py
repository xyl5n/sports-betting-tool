"""
SHAP-based prediction explainer — sport-agnostic.

Uses TreeExplainer for the XGBoost model (exact SHAP values, fast).
Falls back to heuristic attributions when no model is trained.
"""
from typing import Optional
import numpy as np
import shap

from .sports_config import SportConfig


class PredictionExplainer:
    def __init__(self, sport: SportConfig):
        self.sport = sport
        self._explainer: Optional[shap.TreeExplainer] = None

    def _get_explainer(self, model) -> Optional[shap.TreeExplainer]:
        if model is None:
            return None
        if self._explainer is None:
            self._explainer = shap.TreeExplainer(model)
        return self._explainer

    def explain(
        self,
        feature_vec: np.ndarray,
        model,
        scaler,
        is_trained: bool,
        xgb_cols:  Optional[list[int]] = None,
        xgb_names: Optional[list[str]] = None,
    ) -> dict:
        """
        Compute SHAP values for one feature vector.

        xgb_cols / xgb_names: when the XGB model was trained on a SUBSET of
        the scaler's full feature vector (MLB's market-free confidence
        configuration removes home_implied_prob / run_line / line_movement),
        pass the column indices and matching feature names so SHAP receives
        exactly the columns the model expects. None preserves legacy
        all-columns behaviour for sports that don't slice.

        Returns:
          base_value   — expected model output (probability)
          shap_values  — list of {feature, shap_value, feature_value}, sorted by |shap_value|
          source       — "shap" | "heuristic"
        """
        if not is_trained or model is None:
            return self._heuristic_explain(feature_vec, self.sport.feature_names)

        ex = self._get_explainer(model)
        if ex is None:
            return self._heuristic_explain(feature_vec, self.sport.feature_names)

        X_scaled = scaler.transform(feature_vec.reshape(1, -1))
        if xgb_cols is not None:
            X_for_shap      = X_scaled[:, xgb_cols]
            vec_for_display = feature_vec[xgb_cols]
            feature_names   = list(xgb_names) if xgb_names is not None \
                              else [self.sport.feature_names[i] for i in xgb_cols]
        else:
            X_for_shap      = X_scaled
            vec_for_display = feature_vec
            feature_names   = list(self.sport.feature_names)

        raw = ex.shap_values(X_for_shap)

        # XGBoost binary: may return list[array] or plain array
        sv = raw[1][0] if isinstance(raw, list) else raw[0]

        base_logodds = ex.expected_value
        if isinstance(base_logodds, (list, np.ndarray)):
            base_logodds = base_logodds[1]
        base_prob = float(1.0 / (1.0 + np.exp(-float(base_logodds))))

        entries = [
            {
                "feature": feature_names[i],
                "shap_value": float(sv[i]),
                "feature_value": float(vec_for_display[i]),
            }
            for i in range(len(feature_names))
        ]
        entries.sort(key=lambda x: abs(x["shap_value"]), reverse=True)
        return {"base_value": base_prob, "shap_values": entries, "source": "shap"}

    def _heuristic_explain(self, vec: np.ndarray, feature_names: list[str]) -> dict:
        """Approximate attributions from the heuristic model's weights."""
        norm = vec / (self.sport.heuristic_stds + 1e-6)
        contributions = norm * self.sport.heuristic_weights
        entries = [
            {
                "feature": feature_names[i],
                "shap_value": float(contributions[i]),
                "feature_value": float(vec[i]),
            }
            for i in range(len(feature_names))
        ]
        entries.sort(key=lambda x: abs(x["shap_value"]), reverse=True)
        return {"base_value": 0.5, "shap_values": entries, "source": "heuristic"}
