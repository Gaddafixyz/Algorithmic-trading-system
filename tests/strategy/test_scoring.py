"""Unit tests for src.strategy.scoring."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.domain.signal import Signal
from src.strategy.scoring import classify_strength, position_size_multiplier


def make_signal(strength: SignalStrength, score: float = 70.0) -> Signal:
    return Signal(
        strategy_id="test", symbol="BTCUSDT", timeframe=Timeframe.H1,
        direction=SignalDirection.LONG, score=score, strength=strength,
        confidence=0.7, entry_price=Decimal("50000"), stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
    )


class TestClassifyStrength:
    def test_very_strong_at_or_above_80(self) -> None:
        assert classify_strength(80.0) == SignalStrength.VERY_STRONG
        assert classify_strength(95.0) == SignalStrength.VERY_STRONG

    def test_strong_between_65_and_80(self) -> None:
        assert classify_strength(65.0) == SignalStrength.STRONG
        assert classify_strength(79.9) == SignalStrength.STRONG

    def test_moderate_between_50_and_65(self) -> None:
        assert classify_strength(50.0) == SignalStrength.MODERATE
        assert classify_strength(64.9) == SignalStrength.MODERATE

    def test_weak_below_50(self) -> None:
        assert classify_strength(0.0) == SignalStrength.WEAK
        assert classify_strength(49.9) == SignalStrength.WEAK

    def test_boundary_at_exactly_80_is_very_strong_not_strong(self) -> None:
        assert classify_strength(80.0) == SignalStrength.VERY_STRONG

    def test_boundary_at_exactly_65_is_strong_not_moderate(self) -> None:
        assert classify_strength(65.0) == SignalStrength.STRONG

    def test_boundary_at_exactly_50_is_moderate_not_weak(self) -> None:
        assert classify_strength(50.0) == SignalStrength.MODERATE


class TestPositionSizeMultiplier:
    def test_very_strong_gets_full_size(self) -> None:
        signal = make_signal(SignalStrength.VERY_STRONG)
        assert position_size_multiplier(signal) == 1.0

    def test_strong_gets_0_75(self) -> None:
        signal = make_signal(SignalStrength.STRONG)
        assert position_size_multiplier(signal) == 0.75

    def test_moderate_gets_0_5(self) -> None:
        signal = make_signal(SignalStrength.MODERATE)
        assert position_size_multiplier(signal) == 0.5

    def test_weak_gets_reduced_0_25(self) -> None:
        signal = make_signal(SignalStrength.WEAK)
        assert position_size_multiplier(signal) == 0.25
