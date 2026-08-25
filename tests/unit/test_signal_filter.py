"""Unit tests for src.machine_learning.signal_filter.SignalFilter.

Regression coverage: `should_trade` must fail OPEN (return True) on any
model/feature-extraction error, never block a trade because of an ML
bug — see the module docstring's own framing of this as intentional.
Also covers the `SklearnClassifier` structural-protocol validation in
`__init__`, the `min_probability` bound check, and the decision/filter
counters used for introspection.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.domain.signal import Signal
from src.machine_learning.features import FEATURE_NAMES, N_FEATURES
from src.machine_learning.signal_filter import SignalFilter, SklearnClassifier


def make_signal(**kwargs) -> Signal:
    defaults = dict(
        strategy_id="test_strategy",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        direction=SignalDirection.LONG,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
    )
    defaults.update(kwargs)
    return Signal(**defaults)


class _StubModel:
    """Minimal object satisfying the SklearnClassifier protocol."""

    def __init__(self, positive_proba: float = 0.8, predict_value: int = 1) -> None:
        self._positive_proba = positive_proba
        self._predict_value = predict_value
        self.predict_proba_calls: list[np.ndarray] = []
        self.feature_importances_: np.ndarray | None = None

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        self.predict_proba_calls.append(X)
        n = X.shape[0]
        return np.tile([1.0 - self._positive_proba, self._positive_proba], (n, 1))

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.full(X.shape[0], self._predict_value)


class _BrokenModel:
    """Model whose predict_proba always raises, to exercise fail-open."""

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        raise RuntimeError("model exploded")

    def predict(self, X: np.ndarray) -> np.ndarray:
        raise RuntimeError("model exploded")


class TestSignalFilterConstruction:
    def test_accepts_object_satisfying_protocol(self) -> None:
        sf = SignalFilter(_StubModel())
        assert sf.min_probability == 0.55

    def test_rejects_object_missing_predict_proba(self) -> None:
        class NoProba:
            def predict(self, X):  # noqa: ANN001, ANN201
                return X

        with pytest.raises(TypeError, match="predict_proba"):
            SignalFilter(NoProba())

    @pytest.mark.parametrize("bad_prob", [0.0, 1.0, -0.1, 1.1])
    def test_rejects_out_of_range_min_probability(self, bad_prob: float) -> None:
        with pytest.raises(ValueError, match="min_probability"):
            SignalFilter(_StubModel(), min_probability=bad_prob)

    def test_accepts_boundary_valid_min_probability(self) -> None:
        sf = SignalFilter(_StubModel(), min_probability=0.5)
        assert sf.min_probability == 0.5


class TestShouldTradeDecision:
    def test_allows_when_probability_above_threshold(self) -> None:
        sf = SignalFilter(_StubModel(positive_proba=0.9), min_probability=0.55)
        assert sf.should_trade(make_signal()) is True

    def test_blocks_when_probability_below_threshold(self) -> None:
        sf = SignalFilter(_StubModel(positive_proba=0.2), min_probability=0.55)
        assert sf.should_trade(make_signal()) is False

    def test_probability_exactly_at_threshold_allows(self) -> None:
        sf = SignalFilter(_StubModel(positive_proba=0.55), min_probability=0.55)
        assert sf.should_trade(make_signal()) is True

    def test_works_with_analysis_none(self) -> None:
        """Features degrade to defaults; must not raise."""
        sf = SignalFilter(_StubModel(positive_proba=0.9))
        assert sf.should_trade(make_signal(), analysis=None) is True

    def test_feature_vector_passed_has_correct_shape(self) -> None:
        model = _StubModel(positive_proba=0.9)
        sf = SignalFilter(model)
        sf.should_trade(make_signal())
        assert model.predict_proba_calls[0].shape == (1, N_FEATURES)


class TestShouldTradeFailsOpen:
    """Regression: model/feature errors must never block a trade."""

    def test_broken_model_fails_open(self) -> None:
        sf = SignalFilter(_BrokenModel())
        assert sf.should_trade(make_signal()) is True

    def test_broken_model_does_not_increment_decision_counters(self) -> None:
        sf = SignalFilter(_BrokenModel())
        sf.should_trade(make_signal())
        assert sf.decisions == 0
        assert sf.allowed == 0

    def test_broken_model_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        sf = SignalFilter(_BrokenModel())
        with caplog.at_level(logging.WARNING, logger="src.machine_learning.signal_filter"):
            sf.should_trade(make_signal())
        assert any("failing open" in rec.message for rec in caplog.records)


class TestIntrospectionCounters:
    def test_decisions_and_allowed_accumulate(self) -> None:
        sf = SignalFilter(_StubModel(positive_proba=0.9), min_probability=0.55)
        for _ in range(3):
            sf.should_trade(make_signal())
        assert sf.decisions == 3
        assert sf.allowed == 3

    def test_filter_rate_reflects_blocked_fraction(self) -> None:
        allow_model = _StubModel(positive_proba=0.9)
        block_model = _StubModel(positive_proba=0.1)
        sf_allow = SignalFilter(allow_model, min_probability=0.55)
        sf_block = SignalFilter(block_model, min_probability=0.55)

        sf_allow.should_trade(make_signal())
        sf_block.should_trade(make_signal())

        assert sf_allow.filter_rate == 0.0
        assert sf_block.filter_rate == 1.0

    def test_filter_rate_zero_before_any_decision(self) -> None:
        sf = SignalFilter(_StubModel())
        assert sf.filter_rate == 0.0

    def test_filter_rate_mixed_decisions(self) -> None:
        model = _StubModel(positive_proba=0.9)
        sf = SignalFilter(model, min_probability=0.55)
        sf.should_trade(make_signal())  # allowed
        model._positive_proba = 0.1
        sf.should_trade(make_signal())  # blocked
        assert sf.decisions == 2
        assert sf.allowed == 1
        assert sf.filter_rate == 0.5


class TestScore:
    def test_score_computes_accuracy(self) -> None:
        model = _StubModel(predict_value=1)
        sf = SignalFilter(model)
        X = np.zeros((4, N_FEATURES), dtype=np.float32)
        y = np.array([1, 1, 1, 1])
        assert sf.score(X, y) == 1.0

    def test_score_computes_partial_accuracy(self) -> None:
        model = _StubModel(predict_value=1)
        sf = SignalFilter(model)
        X = np.zeros((4, N_FEATURES), dtype=np.float32)
        y = np.array([1, 1, 0, 0])
        assert sf.score(X, y) == 0.5


class TestFeatureImportances:
    def test_returns_none_when_model_lacks_attribute(self) -> None:
        sf = SignalFilter(_StubModel())
        assert sf.feature_importances() is None

    def test_returns_named_dict_when_present(self) -> None:
        model = _StubModel()
        model.feature_importances_ = np.linspace(0, 1, N_FEATURES)
        sf = SignalFilter(model)
        importances = sf.feature_importances()
        assert importances is not None
        assert set(importances.keys()) == set(FEATURE_NAMES)
        assert importances[FEATURE_NAMES[-1]] == pytest.approx(1.0)


class TestRepr:
    def test_repr_contains_key_fields(self) -> None:
        sf = SignalFilter(_StubModel(positive_proba=0.9), min_probability=0.6)
        text = repr(sf)
        assert "_StubModel" in text
        assert "0.60" in text
