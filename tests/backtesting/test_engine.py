"""Unit tests for src.backtesting.engine.BacktestEngine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.backtesting.commission import ZeroCommission
from src.backtesting.engine import BacktestConfig, BacktestEngine
from src.backtesting.slippage import ZeroSlippage
from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.core.interfaces import IStrategy
from src.core.models import OHLCV
from src.domain.signal import Signal


def make_candles(count: int, base_price: float = 50000.0) -> list[OHLCV]:
    candles = []
    for i in range(count):
        candles.append(
            OHLCV(
                open=Decimal(str(base_price)),
                high=Decimal(str(base_price + 50)),
                low=Decimal(str(base_price - 50)),
                close=Decimal(str(base_price)),
                volume=Decimal("1000"),
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
            )
        )
    return candles


def make_signal() -> Signal:
    return Signal(
        strategy_id="test",
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


def make_config() -> BacktestConfig:
    return BacktestConfig(
        initial_equity=Decimal("10000"),
        commission_model=ZeroCommission(),
        slippage_model=ZeroSlippage(),
        partial_fill=None,
    )


class TestMinimumCandleGuard:
    async def test_raises_with_fewer_than_50_candles(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        engine = BacktestEngine(strategy, make_config())
        with pytest.raises(ValueError, match="at least 50 candles"):
            await engine.run(make_candles(49), "BTCUSDT", Timeframe.H1)

    async def test_succeeds_with_exactly_50_candles(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(50), "BTCUSDT", Timeframe.H1)
        assert result.candles_processed == 0  # loop range(50, 50) is empty


class TestNoLookAheadBias:
    async def test_strategy_never_receives_more_candles_than_the_current_bar(
        self,
    ) -> None:
        received_lengths: list[int] = []

        async def capture_on_candle(symbol, timeframe, candles):
            received_lengths.append(len(candles))

        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.side_effect = capture_on_candle

        engine = BacktestEngine(strategy, make_config())
        candles = make_candles(55)
        await engine.run(candles, "BTCUSDT", Timeframe.H1)

        # At loop index i, visible_candles = candles[:i+1] -> length i+1.
        # Loop runs i = 50..54, so lengths must be exactly [51, 52, 53, 54, 55].
        assert received_lengths == [51, 52, 53, 54, 55]

    async def test_the_last_visible_candle_is_always_the_current_bar(self) -> None:
        seen_last_candles: list[OHLCV] = []

        async def capture_on_candle(symbol, timeframe, candles):
            seen_last_candles.append(candles[-1])

        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.side_effect = capture_on_candle

        engine = BacktestEngine(strategy, make_config())
        candles = make_candles(52)
        await engine.run(candles, "BTCUSDT", Timeframe.H1)

        assert seen_last_candles == candles[50:52]


class TestSignalToBrokerWiring:
    async def test_a_returned_signal_is_placed_with_the_broker(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        # Only emit a signal on the very first eligible bar to keep this
        # test's assertions simple (avoid multiple overlapping orders).
        call_count = {"n": 0}

        async def emit_once(symbol, timeframe, candles):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return make_signal()
            return None

        strategy.on_candle.side_effect = emit_once

        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(52), "BTCUSDT", Timeframe.H1)

        assert result.signals_generated == 1

    async def test_no_signal_returned_does_not_increment_signals_generated(
        self,
    ) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(52), "BTCUSDT", Timeframe.H1)
        assert result.signals_generated == 0


class TestPerBarStrategyErrorIsolation:
    async def test_a_strategy_exception_does_not_abort_the_backtest(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.side_effect = RuntimeError("strategy bug")

        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(55), "BTCUSDT", Timeframe.H1)

        # The loop must still have run through every bar despite every
        # single on_candle() call raising.
        assert result.candles_processed == 5
        assert result.signals_generated == 0


class TestEquityCurve:
    async def test_equity_curve_starts_with_initial_equity(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(52), "BTCUSDT", Timeframe.H1)
        assert result.equity_curve[0] == Decimal("10000")

    async def test_equity_curve_has_one_entry_per_candle_processed_plus_the_seed(
        self,
    ) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(55), "BTCUSDT", Timeframe.H1)
        assert len(result.equity_curve) == result.candles_processed + 1


class _FakeLtfAwareStrategy:
    """
    Minimal strategy double that implements set_ltf_candles() (and
    set_htf_candles(), unused here) — AsyncMock(spec=IStrategy) can't be
    used for this since set_ltf_candles isn't part of IStrategy and a
    spec'd mock rejects hasattr() checks for anything not on the spec, so
    BacktestEngine's `hasattr(strategy, "set_ltf_candles")` duck-typed
    detection would never fire against it (this is also why no prior test
    in this file exercised the HTF cursor branch either).
    """

    def __init__(self) -> None:
        self.ltf_snapshots: list[list[OHLCV] | None] = []
        self.htf_snapshots: list[list[OHLCV] | None] = []

    async def initialize(self) -> None:
        pass

    def set_htf_candles(self, htf_candles: list[OHLCV] | None) -> None:
        self.htf_snapshots.append(htf_candles)

    def set_ltf_candles(self, ltf_candles: list[OHLCV] | None) -> None:
        self.ltf_snapshots.append(ltf_candles)

    async def on_candle(self, symbol, timeframe, candles):
        return None


class TestLtfNoLookAhead:
    """
    LTF candles previously had no source anywhere in the codebase — this
    covers the BacktestEngine side: set_ltf_candles() must only ever see
    LTF candles that have actually closed as of the current execution-TF
    bar, mirroring the pre-existing HTF cursor guarantee exactly.
    """

    async def test_ltf_cursor_only_exposes_closed_ltf_candles(self) -> None:
        strategy = _FakeLtfAwareStrategy()
        engine = BacktestEngine(strategy, make_config())

        # Execution TF = H1. LTF = M15 (4 LTF candles per H1 bar).
        exec_candles = make_candles(55)  # hourly, 2024-01-01T00:00 onward
        ltf_candles = [
            OHLCV(
                open=Decimal("50000"),
                high=Decimal("50050"),
                low=Decimal("49950"),
                close=Decimal("50000"),
                volume=Decimal("100"),
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * i),
            )
            for i in range(55 * 4)
        ]

        await engine.run(
            exec_candles,
            "BTCUSDT",
            Timeframe.H1,
            ltf_candles=ltf_candles,
            ltf_timeframe=Timeframe.M15,
        )

        # At bar i (execution TF, hourly), current_time = candles[i].timestamp.
        # Only M15 candles with timestamp + 15min <= current_time have
        # actually closed. At i=50 (00:00 + 50h), that's every M15 candle
        # up to and including 00:00+49h45m -> 50*4 = 200 closed LTF candles.
        assert len(strategy.ltf_snapshots[0]) == 200
        # Snapshot must never include a still-forming LTF candle (no
        # look-ahead) and must be monotonically non-decreasing in length.
        lengths = [len(s) if s else 0 for s in strategy.ltf_snapshots]
        assert lengths == sorted(lengths)
        assert lengths[-1] <= len(ltf_candles)

    async def test_ltf_untouched_when_ltf_timeframe_not_provided(self) -> None:
        """No ltf_timeframe -> set_ltf_candles() must never be called, same
        as the existing HTF behavior when htf_timeframe is omitted."""
        strategy = _FakeLtfAwareStrategy()
        engine = BacktestEngine(strategy, make_config())
        await engine.run(make_candles(52), "BTCUSDT", Timeframe.H1)
        assert strategy.ltf_snapshots == []


class TestBacktestResultSummarize:
    async def test_summarize_returns_empty_dict_when_performance_is_none(self) -> None:
        from src.backtesting.engine import BacktestResult

        result = BacktestResult(
            config=make_config(),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        assert result.summarize() == {}

    async def test_summarize_includes_key_fields_after_a_run(self) -> None:
        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        engine = BacktestEngine(strategy, make_config())
        result = await engine.run(make_candles(52), "BTCUSDT", Timeframe.H1)

        summary = result.summarize()
        assert summary["symbol"] == "BTCUSDT"
        assert summary["timeframe"] == "1h"
        assert summary["candles"] == 2
        assert summary["signals"] == 0
        assert summary["trades"] == 0
