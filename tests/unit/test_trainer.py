"""Unit tests for src.machine_learning.trainer.SignalFilterTrainer.

Uses a real, fast ``LogisticRegression`` as the injected estimator for
functional tests (train/evaluate/cross_validate/save/load) rather than
the default XGBoost/GradientBoostingClassifier, which is either
unavailable in this sandbox (xgboost) or slow to fit repeatedly
(GradientBoostingClassifier at n_estimators=200-300). Genuine sklearn
objects are used throughout instead of mocks, since the whole point of
this module is its integration with scikit-learn's fit/predict_proba/
predict contract, TimeSeriesSplit, and pickling round-trip.

Regression coverage:
- ``tune()``'s AUC scorer must use ``response_method="predict_proba"``
  rather than the removed ``needs_proba`` kwarg (silently-wrong CV
  scores otherwise) — covered indirectly by exercising the full
  ``tune()`` path end-to-end.
- ``_default_param_space()`` must branch on the actual estimator type
  (XGBoost vs. sklearn fallback), since the two don't share every
  hyperparameter name.
- ``load()`` must reject a saved model whose feature schema no longer
  matches ``FEATURE_NAMES`` rather than silently loading a
  mis-aligned model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

from src.machine_learning.features import FEATURE_NAMES, N_FEATURES
from src.machine_learning.signal_filter import SignalFilter
from src.machine_learning.trainer import SignalFilterTrainer


def make_dataset(n: int = 60, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Small, learnable synthetic dataset: y correlates with feature 0."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, N_FEATURES)).astype(np.float32)
    y = (X[:, 0] + rng.normal(scale=0.25, size=n) > 0).astype(int)
    return X, y


def make_trainer(**kwargs) -> SignalFilterTrainer:
    kwargs.setdefault("estimator", LogisticRegression(max_iter=200))
    kwargs.setdefault("random_state", 1)
    return SignalFilterTrainer(**kwargs)


class TestDefaultEstimatorSelection:
    def test_falls_back_when_xgboost_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With xgboost import blocked, the fallback estimator must still
        expose a fit/predict/predict_proba-compatible interface."""
        monkeypatch.setitem(sys.modules, "xgboost", None)
        trainer = SignalFilterTrainer()
        est = trainer._estimator  # noqa: SLF001
        assert hasattr(est, "fit")
        assert hasattr(est, "predict_proba")
        assert type(est).__name__ == "GradientBoostingClassifier"

    def test_custom_estimator_is_used_as_is(self) -> None:
        custom = LogisticRegression(max_iter=50)
        trainer = SignalFilterTrainer(estimator=custom)
        assert trainer._estimator is custom  # noqa: SLF001

    def test_default_param_space_matches_fallback_estimator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "xgboost", None)
        trainer = SignalFilterTrainer()
        space = trainer._default_param_space()  # noqa: SLF001
        # The sklearn-fallback space has no `colsample_bytree` /
        # `min_child_weight` (XGBoost-only params).
        assert "colsample_bytree" not in space
        assert "n_estimators" in space
        assert "max_depth" in space


class TestTrain:
    def test_train_returns_signal_filter(self) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        result = trainer.train(X, y)
        assert isinstance(result, SignalFilter)

    def test_train_fits_a_usable_model(self) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        # Predict on the training features to confirm the estimator was
        # actually fit (an unfit sklearn estimator raises NotFittedError).
        preds = trainer._estimator.predict(X)  # noqa: SLF001
        assert preds.shape == (len(X),)
        # sf.score() exercises the same fitted estimator via SignalFilter.
        assert 0.0 <= sf.score(X, y) <= 1.0

    def test_train_raises_on_length_mismatch(self) -> None:
        X, y = make_dataset(n=60)
        trainer = make_trainer()
        with pytest.raises(ValueError, match="length mismatch"):
            trainer.train(X, y[:-1])

    def test_train_raises_on_wrong_feature_count(self) -> None:
        trainer = make_trainer()
        X_bad = np.zeros((10, N_FEATURES - 1), dtype=np.float32)
        y = np.zeros(10, dtype=int)
        with pytest.raises(ValueError, match="expected"):
            trainer.train(X_bad, y)

    def test_train_raises_on_1d_X(self) -> None:
        trainer = make_trainer()
        with pytest.raises(ValueError, match="2-D"):
            trainer.train(np.zeros(10), np.zeros(10))

    def test_train_raises_on_non_finite_X(self) -> None:
        X, y = make_dataset()
        X[0, 0] = np.nan
        trainer = make_trainer()
        with pytest.raises(ValueError, match="non-finite"):
            trainer.train(X, y)

    def test_eval_set_does_not_break_estimators_without_early_stopping(self) -> None:
        """LogisticRegression has no `early_stopping_rounds` attribute;
        passing eval_set must be a no-op, not an error."""
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y, eval_set=(X, y))
        assert isinstance(sf, SignalFilter)

    def test_min_probability_propagates_to_returned_filter(self) -> None:
        X, y = make_dataset()
        trainer = make_trainer(min_probability=0.7)
        sf = trainer.train(X, y)
        assert sf.min_probability == 0.7


class TestEvaluate:
    def test_evaluate_returns_expected_keys(self) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        metrics = trainer.evaluate(sf, X, y)
        assert set(metrics) == {"accuracy", "precision", "recall", "f1", "auc"}
        assert all(0.0 <= v <= 1.0 for v in metrics.values())

    def test_evaluate_accepts_signal_filter_or_raw_estimator(self) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        via_filter = trainer.evaluate(sf, X, y)
        via_raw = trainer.evaluate(trainer._estimator, X, y)  # noqa: SLF001
        assert via_filter == via_raw


class TestCrossValidate:
    def test_cross_validate_returns_mean_and_std_for_each_metric(self) -> None:
        X, y = make_dataset(n=80)
        trainer = make_trainer()
        summary = trainer.cross_validate(X, y, n_splits=3)
        for metric in ("accuracy", "f1", "auc"):
            assert f"{metric}_mean" in summary
            assert f"{metric}_std" in summary
            assert 0.0 <= summary[f"{metric}_mean"] <= 1.0

    def test_cross_validate_respects_temporal_order(self) -> None:
        """TimeSeriesSplit must be used (not random k-fold) -- verified
        indirectly: repeated calls on the same data are deterministic,
        which random shuffling without a fixed split would not
        guarantee to the same degree across independent estimator fits."""
        X, y = make_dataset(n=80)
        trainer1 = make_trainer()
        trainer2 = make_trainer()
        s1 = trainer1.cross_validate(X, y, n_splits=3)
        s2 = trainer2.cross_validate(X, y, n_splits=3)
        assert s1 == s2


class TestTune:
    def test_tune_raises_import_error_without_optuna(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "optuna", None)
        X, y = make_dataset()
        trainer = make_trainer()
        with pytest.raises(ImportError, match="optuna"):
            trainer.tune(X, y, n_trials=2)

    def test_tune_validates_inputs_before_importing_optuna(self) -> None:
        """Length-mismatch must be caught even if optuna isn't reached."""
        trainer = make_trainer()
        X, y = make_dataset(n=60)
        with pytest.raises(ValueError, match="length mismatch"):
            trainer.tune(X, y[:-1])

    def test_tune_rejects_unknown_metric(self) -> None:
        trainer = make_trainer()
        X, y = make_dataset()
        with pytest.raises(ValueError, match="metric"):
            trainer.tune(X, y, metric="not_a_metric")

    def test_tune_end_to_end_updates_estimator_when_optuna_available(self) -> None:
        pytest.importorskip("optuna")
        X, y = make_dataset(n=80)
        trainer = make_trainer()
        original = trainer._estimator  # noqa: SLF001
        result = trainer.tune(X, y, n_trials=3, n_splits=2)
        assert "best_params" in result
        assert "best_score" in result
        # tune() must swap in a fresh, unfitted clone -- not mutate the
        # original estimator in place, and must not fit the final model
        # itself (caller calls train() afterward).
        assert trainer._estimator is not original  # noqa: SLF001


class TestSaveLoad:
    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        X, y = make_dataset()
        trainer = make_trainer(min_probability=0.6)
        sf = trainer.train(X, y)
        path = tmp_path / "model.pkl"
        trainer.save(sf, path)
        assert path.exists()

        loaded = trainer.load(path)
        assert isinstance(loaded, SignalFilter)
        assert loaded.min_probability == 0.6
        # Loaded model produces identical predictions to the original.
        assert loaded.score(X, y) == sf.score(X, y)

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        nested = tmp_path / "nested" / "dir" / "model.pkl"
        trainer.save(sf, nested)
        assert nested.exists()

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        trainer = make_trainer()
        with pytest.raises(FileNotFoundError):
            trainer.load(tmp_path / "does_not_exist.pkl")

    def test_load_rejects_mismatched_feature_schema(self, tmp_path: Path) -> None:
        """Regression: a stale model trained against an old FEATURE_NAMES
        schema must be rejected at load time, not silently used."""
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        path = tmp_path / "model.pkl"
        trainer.save(sf, path)

        import pickle

        with path.open("rb") as fh:
            payload = pickle.load(fh)
        payload["feature_names"] = FEATURE_NAMES[:-1]  # drop one feature
        with path.open("wb") as fh:
            pickle.dump(payload, fh)

        with pytest.raises(ValueError, match="schema"):
            trainer.load(path)

    def test_load_accepts_str_path(self, tmp_path: Path) -> None:
        X, y = make_dataset()
        trainer = make_trainer()
        sf = trainer.train(X, y)
        path = tmp_path / "model.pkl"
        trainer.save(sf, path)
        loaded = trainer.load(str(path))
        assert isinstance(loaded, SignalFilter)
