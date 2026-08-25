"""
ML-based signal filter.

Wraps a trained scikit-learn-compatible binary classifier and exposes a
``should_trade`` predicate that the strategy layer can call before forwarding
a signal to the execution engine.

The classifier is trained offline (see ``trainer.py``) on historical SMC
setups labelled with their eventual outcome (profitable / not).  At inference
time, features are extracted from the live ``SMCAnalysis`` + ``Signal`` pair
and the model's positive-class probability is compared against a configurable
threshold.

The filter is intentionally conservative by default (``min_probability=0.55``)
— a 55% positive-class probability is required to allow a trade through.
Raise this threshold to filter more aggressively; lower it to pass more.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

from src.machine_learning.features import extract_features, N_FEATURES, FEATURE_NAMES

if TYPE_CHECKING:
    from src.domain.signal import Signal
    from src.smart_money.confluence import SMCAnalysis

logger = logging.getLogger(__name__)


@runtime_checkable
class SklearnClassifier(Protocol):
    """Structural protocol matching scikit-learn / XGBoost classifiers."""

    def predict_proba(self, X: np.ndarray) -> np.ndarray: ...
    def predict(self, X: np.ndarray) -> np.ndarray: ...


class SignalFilter:
    """
    ML signal filter that wraps a trained binary classifier.

    Args:
        model: A fitted classifier exposing ``predict_proba``.
        min_probability: Minimum predicted positive-class probability to
            allow a signal through.  Defaults to 0.55.
        max_ob_age_bars: Passed to ``extract_features``; cap for OB age
            normalisation.  Must match the value used during training.

    Example::

        from src.machine_learning import SignalFilter, SignalFilterTrainer

        trainer = SignalFilterTrainer()
        model   = trainer.train(feature_matrix, labels)
        sf      = SignalFilter(model)

        if sf.should_trade(signal, analysis):
            await execution_engine.submit(signal)
    """

    def __init__(
        self,
        model: SklearnClassifier,
        min_probability: float = 0.55,
        max_ob_age_bars: int = 100,
    ) -> None:
        if not isinstance(model, SklearnClassifier):
            raise TypeError(
                f"model must expose predict_proba and predict, got {type(model)}"
            )
        if not (0.0 < min_probability < 1.0):
            raise ValueError(f"min_probability must be in (0, 1), got {min_probability}")
        self._model = model
        self._min_prob = min_probability
        self._max_ob_age_bars = max_ob_age_bars
        self._decisions: int = 0
        self._allowed: int = 0

    def should_trade(
        self,
        signal: "Signal",
        analysis: "SMCAnalysis | None" = None,
    ) -> bool:
        """
        Return True if the model believes this setup has positive expectancy.

        Args:
            signal: The candidate signal to evaluate.
            analysis: The SMC analysis snapshot (optional; features degrade
                gracefully to zeros when ``None``).

        Returns:
            True → pass the signal to execution; False → suppress it.
        """
        try:
            vec = extract_features(signal, analysis, self._max_ob_age_bars)
            prob = self._predict_proba(vec)
            decision = prob >= self._min_prob
        except Exception as exc:
            # Never block a trade due to a model error — fail open
            logger.warning("SignalFilter error, failing open: %s", exc)
            return True

        self._decisions += 1
        if decision:
            self._allowed += 1

        logger.debug(
            "SignalFilter: %s %s prob=%.3f min=%.2f → %s",
            signal.symbol,
            signal.direction,
            prob,
            self._min_prob,
            "ALLOW" if decision else "BLOCK",
        )
        return decision

    def score(self, X: np.ndarray, y: np.ndarray) -> float:
        """Return accuracy on a held-out evaluation set."""
        preds = self._model.predict(X)
        return float(np.mean(preds == y))

    def _predict_proba(self, vec: np.ndarray) -> float:
        """Return positive-class probability for a single feature vector."""
        proba = self._model.predict_proba(vec.reshape(1, -1))
        # scikit-learn returns shape (1, n_classes); column 1 = positive class
        return float(proba[0, 1])

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def filter_rate(self) -> float:
        """Fraction of signals blocked since instantiation (0–1)."""
        if self._decisions == 0:
            return 0.0
        return 1.0 - self._allowed / self._decisions

    @property
    def decisions(self) -> int:
        return self._decisions

    @property
    def allowed(self) -> int:
        return self._allowed

    @property
    def min_probability(self) -> float:
        return self._min_prob

    def feature_importances(self) -> dict[str, float] | None:
        """
        Return per-feature importances if the model supports them.

        Works for tree-based models (XGBoost, LightGBM, RandomForest).
        Returns ``None`` for models without ``feature_importances_``.
        """
        importances = getattr(self._model, "feature_importances_", None)
        if importances is None:
            return None
        return dict(zip(FEATURE_NAMES, map(float, importances), strict=True))

    def __repr__(self) -> str:
        return (
            f"SignalFilter(model={type(self._model).__name__}, "
            f"min_prob={self._min_prob:.2f}, "
            f"filter_rate={self.filter_rate:.1%})"
        )
