"""
Training utilities for the SMC signal filter.

Provides ``SignalFilterTrainer``, which wraps scikit-learn / XGBoost
to train, evaluate, persist, and load the signal-quality classifier.

Workflow::

    from src.machine_learning.trainer import SignalFilterTrainer
    from src.machine_learning.features import extract_features

    # Build training data from historical signal + outcome pairs
    X = np.vstack([extract_features(sig, ana) for sig, ana in historical_pairs])
    y = np.array([1 if trade.net_pnl > 0 else 0 for trade in historical_trades])

    trainer = SignalFilterTrainer()
    model   = trainer.train(X, y)
    metrics = trainer.evaluate(model, X_test, y_test)
    trainer.save(model, Path("models/signal_filter.pkl"))

    # Later, at runtime:
    model = trainer.load(Path("models/signal_filter.pkl"))
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from src.machine_learning.features import FEATURE_NAMES, N_FEATURES
from src.machine_learning.signal_filter import SignalFilter

logger = logging.getLogger(__name__)

# Default model filename
DEFAULT_MODEL_PATH = Path("models/signal_filter.pkl")


class SignalFilterTrainer:
    """
    Trains and evaluates a binary signal-quality classifier.

    The default classifier is an XGBoost gradient-boosted tree, which
    handles the mixed numeric/boolean feature space well and produces
    calibrated probabilities via ``predict_proba``.  Any scikit-learn
    compatible estimator can be injected via the ``estimator`` argument.

    Args:
        estimator: Optional pre-configured scikit-learn estimator.
            Defaults to XGBClassifier with conservative hyper-parameters.
        min_probability: Passed to the ``SignalFilter`` wrapping the trained
            model.  Signals with a predicted positive-class probability below
            this threshold are suppressed.
        random_state: RNG seed for reproducibility.
    """

    def __init__(
        self,
        estimator: Any | None = None,
        min_probability: float = 0.55,
        random_state: int = 42,
    ) -> None:
        self._min_prob = min_probability
        self._random_state = random_state
        self._estimator = estimator or self._default_estimator()

    # ── Public API ────────────────────────────────────────────────────────────

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        eval_set: tuple[np.ndarray, np.ndarray] | None = None,
    ) -> SignalFilter:
        """
        Fit the classifier and return a ready-to-use ``SignalFilter``.

        Args:
            X: Feature matrix of shape ``(n_samples, N_FEATURES)``.
            y: Binary labels, 1 = profitable setup, 0 = losing setup.
            eval_set: Optional ``(X_val, y_val)`` for early stopping when
                using tree-boosting estimators that support it.

        Returns:
            A ``SignalFilter`` wrapping the fitted model.
        """
        self._validate_X(X)
        if len(X) != len(y):
            raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")

        unique, counts = np.unique(y, return_counts=True)
        logger.info(
            "Training signal filter: %d samples, class distribution %s",
            len(X),
            dict(zip(unique.tolist(), counts.tolist(), strict=True)),
        )

        fit_kwargs: dict[str, Any] = {}
        if eval_set is not None:
            # XGBoost / LightGBM support early_stopping_rounds via eval_set
            if hasattr(self._estimator, "early_stopping_rounds"):
                fit_kwargs["eval_set"] = [eval_set]

        self._estimator.fit(X, y, **fit_kwargs)
        logger.info("Training complete. Wrapping in SignalFilter.")
        return SignalFilter(self._estimator, min_probability=self._min_prob)

    def evaluate(
        self,
        signal_filter: SignalFilter | Any,
        X_test: np.ndarray,
        y_test: np.ndarray,
    ) -> dict[str, float]:
        """
        Compute evaluation metrics on a held-out test set.

        Works with a ``SignalFilter`` instance or a raw sklearn estimator.

        Returns a dict with keys:
            - ``accuracy``: overall accuracy
            - ``precision``: precision for positive class
            - ``recall``: recall for positive class
            - ``f1``: F1 score for positive class
            - ``auc``: ROC-AUC score
        """
        from sklearn.metrics import (
            accuracy_score,
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )

        model = signal_filter._model if isinstance(signal_filter, SignalFilter) else signal_filter
        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]

        metrics = {
            "accuracy": float(accuracy_score(y_test, y_pred)),
            "precision": float(precision_score(y_test, y_pred, zero_division=0)),
            "recall": float(recall_score(y_test, y_pred, zero_division=0)),
            "f1": float(f1_score(y_test, y_pred, zero_division=0)),
            "auc": float(roc_auc_score(y_test, y_prob)),
        }
        logger.info(
            "Evaluation — acc=%.3f prec=%.3f rec=%.3f f1=%.3f auc=%.3f",
            metrics["accuracy"],
            metrics["precision"],
            metrics["recall"],
            metrics["f1"],
            metrics["auc"],
        )
        return metrics

    def cross_validate(
        self,
        X: np.ndarray,
        y: np.ndarray,
        n_splits: int = 5,
    ) -> dict[str, float]:
        """
        Time-series-aware cross-validation (``TimeSeriesSplit``).

        Unlike random k-fold, this respects the temporal ordering of samples
        so that future data never leaks into the training window.

        Returns mean ± std for accuracy, F1, and AUC.
        """
        from sklearn.metrics import f1_score, make_scorer, roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit, cross_validate

        tscv = TimeSeriesSplit(n_splits=n_splits)
        scoring = {
            "accuracy": "accuracy",
            "f1": make_scorer(f1_score, zero_division=0),
            # `needs_proba` was removed in scikit-learn 1.6 in favour of
            # `response_method`. The old kwarg doesn't raise here (make_scorer
            # still accepts **kwargs) — it just gets silently ignored, so the
            # scorer falls back to `response_method="predict"` and calling
            # roc_auc_score on hard class labels either errors per-fold
            # (caught by cross_validate, which then scores that fold NaN) or
            # produces a degenerate AUC. Either way, cross-validated AUC goes
            # silently wrong. Use `response_method` directly.
            "auc": make_scorer(roc_auc_score, response_method="predict_proba"),
        }
        cv_results = cross_validate(
            self._estimator, X, y, cv=tscv, scoring=scoring, return_train_score=False
        )
        summary: dict[str, float] = {}
        for metric in ("accuracy", "f1", "auc"):
            vals = cv_results[f"test_{metric}"]
            summary[f"{metric}_mean"] = float(np.mean(vals))
            summary[f"{metric}_std"] = float(np.std(vals))

        logger.info(
            "Cross-validation (%d splits) — acc=%.3f±%.3f f1=%.3f±%.3f auc=%.3f±%.3f",
            n_splits,
            summary["accuracy_mean"],
            summary["accuracy_std"],
            summary["f1_mean"],
            summary["f1_std"],
            summary["auc_mean"],
            summary["auc_std"],
        )
        return summary

    def tune(
        self,
        X: np.ndarray,
        y: np.ndarray,
        param_space: dict[str, dict[str, Any]] | None = None,
        n_trials: int = 50,
        n_splits: int = 5,
        metric: str = "auc",
    ) -> dict[str, Any]:
        """
        Hyperparameter search via Optuna (TPE sampler), scored with
        time-series cross-validation so tuning respects the same
        no-look-ahead constraint as the rest of the system — mirrors the
        pattern in ``src/backtesting/optimization.py``.

        On success, updates ``self._estimator`` in place to a fresh,
        *unfitted* clone configured with the best-found hyperparameters.
        Call ``train()`` afterward to fit it on the full training set —
        ``tune()`` deliberately does not fit the final model itself, so the
        caller decides what data the final fit sees (e.g. train+val
        combined vs. train only).

        Args:
            X: Feature matrix of shape ``(n_samples, N_FEATURES)``.
            y: Binary labels.
            param_space: Optuna-style search space, e.g.
                ``{"max_depth": {"type": "int", "low": 2, "high": 8}}``.
                Supported ``type``s: ``int``, ``float`` (with optional
                ``log: True``), ``categorical`` (with ``choices``).
                Defaults to ``_default_param_space()``, adapted to whichever
                estimator is active (XGBoost vs. the sklearn fallback).
            n_trials: Number of Optuna trials.
            n_splits: Number of ``TimeSeriesSplit`` folds per trial.
            metric: ``"auc"`` or ``"f1"`` — which metric to maximize.

        Returns:
            Dict with ``best_params``, ``best_score``, ``n_trials``, ``metric``.

        Raises:
            ImportError: if optuna is not installed.
        """
        try:
            import optuna

            optuna.logging.set_verbosity(optuna.logging.WARNING)
        except ImportError as e:
            raise ImportError("optuna is required for tune() — pip install optuna") from e

        self._validate_X(X)
        if len(X) != len(y):
            raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")
        if metric not in ("auc", "f1"):
            raise ValueError(f"metric must be 'auc' or 'f1', got {metric!r}")

        from sklearn.base import clone
        from sklearn.metrics import f1_score, roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit

        space = param_space or self._default_param_space()
        tscv = TimeSeriesSplit(n_splits=n_splits)

        def objective(trial: Any) -> float:
            params: dict[str, Any] = {}
            for name, spec in space.items():
                if spec["type"] == "int":
                    params[name] = trial.suggest_int(name, spec["low"], spec["high"])
                elif spec["type"] == "float":
                    params[name] = trial.suggest_float(
                        name, spec["low"], spec["high"], log=spec.get("log", False)
                    )
                elif spec["type"] == "categorical":
                    params[name] = trial.suggest_categorical(name, spec["choices"])
                else:
                    raise ValueError(f"Unknown param_space type: {spec['type']!r}")

            est = clone(self._estimator)
            est.set_params(**params)

            fold_scores: list[float] = []
            for train_idx, val_idx in tscv.split(X):
                y_tr, y_val = y[train_idx], y[val_idx]
                if len(np.unique(y_tr)) < 2 or len(np.unique(y_val)) < 2:
                    # A fold with only one class present can't be scored
                    # meaningfully (undefined AUC / degenerate F1) — skip it
                    # rather than let it silently distort the trial's mean.
                    continue
                try:
                    est.fit(X[train_idx], y_tr)
                    if metric == "auc":
                        prob = est.predict_proba(X[val_idx])[:, 1]
                        score = roc_auc_score(y_val, prob)
                    else:
                        pred = est.predict(X[val_idx])
                        score = f1_score(y_val, pred, zero_division=0)
                except Exception as exc:
                    logger.debug("Tuning trial fold failed: %s", exc)
                    continue
                fold_scores.append(score)

            if not fold_scores:
                # No fold could be scored (e.g. too little data for
                # n_splits) — penalize instead of raising so the study can
                # keep exploring other trials.
                return -1.0
            return float(np.mean(fold_scores))

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=self._random_state),
        )
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

        logger.info(
            "Hyperparameter tuning complete (%d trials, %d-fold TS-CV) — "
            "best %s=%.4f, params=%s",
            n_trials,
            n_splits,
            metric,
            study.best_value,
            study.best_params,
        )

        self._estimator = clone(self._estimator)
        self._estimator.set_params(**study.best_params)

        return {
            "best_params": study.best_params,
            "best_score": float(study.best_value),
            "n_trials": n_trials,
            "metric": metric,
        }

    def _default_param_space(self) -> dict[str, dict[str, Any]]:
        """
        Default Optuna search space, adapted to whichever estimator
        ``_default_estimator()`` built (XGBoost when available, else the
        sklearn ``GradientBoostingClassifier`` fallback — the two don't
        share every parameter name).
        """
        if type(self._estimator).__name__ == "XGBClassifier":
            return {
                "n_estimators": {"type": "int", "low": 100, "high": 600},
                "max_depth": {"type": "int", "low": 2, "high": 8},
                "learning_rate": {"type": "float", "low": 0.005, "high": 0.3, "log": True},
                "subsample": {"type": "float", "low": 0.5, "high": 1.0},
                "colsample_bytree": {"type": "float", "low": 0.5, "high": 1.0},
                "min_child_weight": {"type": "int", "low": 1, "high": 15},
            }
        return {
            "n_estimators": {"type": "int", "low": 50, "high": 400},
            "max_depth": {"type": "int", "low": 2, "high": 6},
            "learning_rate": {"type": "float", "low": 0.005, "high": 0.3, "log": True},
            "subsample": {"type": "float", "low": 0.5, "high": 1.0},
        }

    def save(self, signal_filter: SignalFilter, path: Path | str = DEFAULT_MODEL_PATH) -> None:
        """Serialise the fitted model to disk with pickle."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as fh:
            pickle.dump(
                {
                    "model": signal_filter._model,
                    "min_probability": signal_filter.min_probability,
                    "feature_names": FEATURE_NAMES,
                    "n_features": N_FEATURES,
                },
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        logger.info("Signal filter saved to %s", out)

    def load(self, path: Path | str = DEFAULT_MODEL_PATH) -> SignalFilter:
        """
        Load a previously saved signal filter.

        Validates that the feature schema matches the current
        ``FEATURE_NAMES`` so stale models are caught at startup.
        """
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"No saved signal filter at {src}")
        with src.open("rb") as fh:
            payload = pickle.load(fh)  # noqa: S301

        saved_features = payload.get("feature_names", [])
        if saved_features != FEATURE_NAMES:
            raise ValueError(
                f"Saved model feature schema ({len(saved_features)} features) "
                f"does not match current schema ({len(FEATURE_NAMES)} features). "
                "Retrain the model."
            )
        model = payload["model"]
        min_prob = payload.get("min_probability", self._min_prob)
        logger.info("Signal filter loaded from %s (min_prob=%.2f)", src, min_prob)
        return SignalFilter(model, min_probability=min_prob)

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _validate_X(X: np.ndarray) -> None:
        if X.ndim != 2:
            raise ValueError(f"X must be 2-D (n_samples, n_features), got shape {X.shape}")
        if X.shape[1] != N_FEATURES:
            raise ValueError(
                f"X has {X.shape[1]} features, expected {N_FEATURES}. "
                "Re-extract features with the current schema."
            )
        if not np.isfinite(X).all():
            n_bad = (~np.isfinite(X)).sum()
            raise ValueError(
                f"X contains {n_bad} non-finite values (NaN / inf). Impute before training."
            )

    def _default_estimator(self) -> Any:
        """
        Build the default XGBoost classifier.

        Falls back to scikit-learn's GradientBoostingClassifier when XGBoost
        is not installed, preserving the same interface.
        """
        try:
            from xgboost import XGBClassifier

            return XGBClassifier(
                n_estimators=300,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                scale_pos_weight=1,  # adjust for class imbalance if needed
                eval_metric="logloss",
                # NOTE: `use_label_encoder` intentionally omitted. It's been
                # unnecessary since xgboost 1.3 (labels are used as-is, no
                # encoding needed), `use_label_encoder=False` deprecate-warns
                # as of 1.7, and `use_label_encoder=True` raises outright
                # since 1.6. Passing it at all is a future break waiting to
                # happen for zero benefit.
                random_state=self._random_state,
                n_jobs=-1,
            )
        except ImportError:
            logger.warning(
                "xgboost not available; falling back to GradientBoostingClassifier. "
                "Install xgboost for better performance."
            )
            from sklearn.ensemble import GradientBoostingClassifier

            return GradientBoostingClassifier(
                n_estimators=200,
                max_depth=3,
                learning_rate=0.05,
                subsample=0.8,
                random_state=self._random_state,
            )
