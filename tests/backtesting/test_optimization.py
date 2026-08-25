"""Unit tests for src.backtesting.optimization.optimize.

optimize() is a synchronous function that internally calls
asyncio.run() to drive each backtest trial. These tests are
deliberately plain `def` functions (not `async def`) since calling a
function that does asyncio.run() from within pytest-asyncio's already-
running event loop would raise "asyncio.run() cannot be called from a
running event loop".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtesting.optimization import optimize
from src.core.enums import Timeframe
from src.core.interfaces import IStrategy
from src.core.models import OHLCV


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


class _NoSignalStrategy(IStrategy):
    """A real, minimal strategy that never emits a signal.

    Used to exercise optimize()'s plumbing (param sampling, trial
    scoring, OOS validation control flow) without needing genuine
    trading logic -- every trial legitimately has 0 trades, so the
    < 10 trades penalty path is exercised deterministically.
    """

    def __init__(self, params: dict) -> None:
        self._params = params

    @property
    def strategy_id(self) -> str:
        return "no-signal"

    @property
    def symbols(self) -> list[str]:
        return ["BTCUSDT"]

    @property
    def timeframes(self) -> list[Timeframe]:
        return [Timeframe.H1]

    async def initialize(self) -> None:
        return None

    async def on_candle(self, symbol, timeframe, candles):
        return None

    async def on_tick(self, tick) -> None:
        return None

    async def on_position_closed(self, position) -> None:
        return None


class TestOptimizeParamSpaceTypes:
    def test_handles_int_float_and_categorical_param_types(self) -> None:
        candles = make_candles(120)
        result = optimize(
            strategy_factory=lambda params: _NoSignalStrategy(params),
            candles=candles,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            param_space={
                "lookback": {"type": "int", "low": 5, "high": 20},
                "threshold": {"type": "float", "low": 0.1, "high": 1.0},
                "mode": {"type": "categorical", "choices": ["a", "b"]},
            },
            n_trials=3,
        )
        assert "lookback" in result.best_params
        assert "threshold" in result.best_params
        assert "mode" in result.best_params
        assert result.best_params["mode"] in {"a", "b"}
        assert result.n_trials == 3


class TestOptimizeTooFewTradesPenalty:
    def test_a_strategy_with_zero_trades_gets_the_penalty_score(self) -> None:
        candles = make_candles(120)
        result = optimize(
            strategy_factory=lambda params: _NoSignalStrategy(params),
            candles=candles,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            param_space={"x": {"type": "int", "low": 1, "high": 5}},
            n_trials=2,
        )
        # Every trial has 0 trades (< 10 minimum), so best_is_score
        # must be the -999.0 penalty, and all_trials stays empty since
        # a trial is only appended to all_trials on a genuine score.
        assert result.best_is_score == -999.0
        assert result.all_trials == []


class TestOptimizeTrialExceptionIsolation:
    def test_a_factory_that_raises_does_not_crash_the_whole_optimization(self) -> None:
        def failing_factory(params: dict):
            raise RuntimeError("factory bug")

        candles = make_candles(120)
        result = optimize(
            strategy_factory=failing_factory,
            candles=candles,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            param_space={"x": {"type": "int", "low": 1, "high": 5}},
            n_trials=2,
        )
        # Every trial fails and returns the penalty score; optimize()
        # itself must still complete and return a result rather than
        # propagating the exception.
        assert result.best_is_score == -999.0


class TestOptimizeOutOfSampleValidation:
    def test_oos_result_is_populated_when_enough_oos_data_exists(self) -> None:
        candles = make_candles(200)  # 70% IS = 140, 30% OOS = 60 (>= 50)
        result = optimize(
            strategy_factory=lambda params: _NoSignalStrategy(params),
            candles=candles,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            param_space={"x": {"type": "int", "low": 1, "high": 5}},
            n_trials=2,
            is_fraction=0.7,
        )
        assert result.oos_result is not None

    def test_oos_result_is_none_when_insufficient_oos_data(self) -> None:
        candles = make_candles(120)  # 70% IS = 84, 30% OOS = 36 (< 50)
        result = optimize(
            strategy_factory=lambda params: _NoSignalStrategy(params),
            candles=candles,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            param_space={"x": {"type": "int", "low": 1, "high": 5}},
            n_trials=2,
            is_fraction=0.7,
        )
        assert result.oos_result is None
