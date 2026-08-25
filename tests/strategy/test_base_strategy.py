"""Unit tests for src.strategy.base_strategy.BaseStrategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import PositionSide, SignalDirection, SignalStrength, Timeframe
from src.core.models import OHLCV
from src.domain.position import Position
from src.domain.signal import Signal
from src.strategy.base_strategy import BaseStrategy


class _ConcreteStrategy(BaseStrategy):
    """Minimal concrete subclass for exercising BaseStrategy's behavior."""

    @property
    def strategy_id(self) -> str:
        return "test-strategy"

    @property
    def symbols(self) -> list[str]:
        return ["BTCUSDT"]

    @property
    def timeframes(self) -> list[Timeframe]:
        return [Timeframe.H1]

    async def on_candle(
        self, symbol: str, timeframe: Timeframe, candles: list[OHLCV]
    ) -> Signal | None:
        return None


def make_signal(expires_at: datetime | None = None) -> Signal:
    return Signal(
        strategy_id="test-strategy", symbol="BTCUSDT", timeframe=Timeframe.H1,
        direction=SignalDirection.LONG, score=70.0, strength=SignalStrength.STRONG,
        confidence=0.7, entry_price=Decimal("50000"), stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"), expires_at=expires_at,
    )


def make_position(symbol: str = "BTCUSDT") -> Position:
    return Position(
        symbol=symbol, side=PositionSide.LONG, quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"), entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )


class TestWarmUp:
    def test_not_warmed_up_before_enough_candles(self) -> None:
        strategy = _ConcreteStrategy(min_candles=3)
        assert strategy.is_warmed_up("BTCUSDT", Timeframe.H1) is False

    def test_warmed_up_after_enough_candles_recorded(self) -> None:
        strategy = _ConcreteStrategy(min_candles=3)
        for _ in range(3):
            strategy.record_candle("BTCUSDT", Timeframe.H1)
        assert strategy.is_warmed_up("BTCUSDT", Timeframe.H1) is True

    def test_warm_up_is_tracked_independently_per_symbol_and_timeframe(self) -> None:
        strategy = _ConcreteStrategy(min_candles=2)
        strategy.record_candle("BTCUSDT", Timeframe.H1)
        strategy.record_candle("BTCUSDT", Timeframe.H1)
        assert strategy.is_warmed_up("BTCUSDT", Timeframe.H1) is True
        assert strategy.is_warmed_up("BTCUSDT", Timeframe.M15) is False
        assert strategy.is_warmed_up("ETHUSDT", Timeframe.H1) is False


class TestSignalManagement:
    def test_no_active_signal_initially(self) -> None:
        strategy = _ConcreteStrategy()
        assert strategy.has_active_signal("BTCUSDT") is False

    def test_registered_signal_becomes_active(self) -> None:
        strategy = _ConcreteStrategy()
        strategy.register_signal("BTCUSDT", make_signal())
        assert strategy.has_active_signal("BTCUSDT") is True

    def test_expired_registered_signal_is_not_considered_active(self) -> None:
        strategy = _ConcreteStrategy()
        past = datetime.now(UTC) - timedelta(hours=1)
        strategy.register_signal("BTCUSDT", make_signal(expires_at=past))
        assert strategy.has_active_signal("BTCUSDT") is False

    def test_clear_signal_removes_it(self) -> None:
        strategy = _ConcreteStrategy()
        strategy.register_signal("BTCUSDT", make_signal())
        strategy.clear_signal("BTCUSDT")
        assert strategy.has_active_signal("BTCUSDT") is False

    def test_clear_signal_for_an_unregistered_symbol_does_not_raise(self) -> None:
        strategy = _ConcreteStrategy()
        strategy.clear_signal("NEVERUSED")  # must not raise


class TestCanGenerateSignal:
    def test_false_when_a_signal_is_already_active_for_the_symbol(self) -> None:
        strategy = _ConcreteStrategy(max_open_signals=3)
        strategy.register_signal("BTCUSDT", make_signal())
        assert strategy.can_generate_signal("BTCUSDT") is False

    def test_true_when_under_the_max_open_signals_cap(self) -> None:
        strategy = _ConcreteStrategy(max_open_signals=3)
        strategy.register_signal("ETHUSDT", make_signal())
        assert strategy.can_generate_signal("BTCUSDT") is True

    def test_false_when_at_the_max_open_signals_cap(self) -> None:
        strategy = _ConcreteStrategy(max_open_signals=1)
        strategy.register_signal("ETHUSDT", make_signal())
        assert strategy.can_generate_signal("BTCUSDT") is False


class TestPositionTracking:
    def test_no_position_initially(self) -> None:
        strategy = _ConcreteStrategy()
        assert strategy.has_position("BTCUSDT") is False

    def test_registered_open_position_is_tracked(self) -> None:
        strategy = _ConcreteStrategy()
        strategy.register_position(make_position())
        assert strategy.has_position("BTCUSDT") is True


class TestLifecycle:
    async def test_start_calls_initialize_and_sets_running(self) -> None:
        strategy = _ConcreteStrategy()
        assert strategy.is_running is False
        await strategy.start()
        assert strategy.is_running is True

    async def test_stop_clears_running(self) -> None:
        strategy = _ConcreteStrategy()
        await strategy.start()
        await strategy.stop()
        assert strategy.is_running is False

    async def test_on_position_closed_removes_the_position_and_logs(self) -> None:
        strategy = _ConcreteStrategy()
        position = make_position()
        strategy.register_position(position)
        assert strategy.has_position("BTCUSDT") is True

        await strategy.on_position_closed(position)
        assert strategy.has_position("BTCUSDT") is False

    async def test_on_tick_default_implementation_does_not_raise(self) -> None:
        strategy = _ConcreteStrategy()
        await strategy.on_tick(object())  # default no-op override point
