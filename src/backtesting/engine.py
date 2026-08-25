"""
Event-driven backtesting engine.

Replays historical OHLCV data through strategies without look-ahead bias.
Each candle is processed in strict chronological order.
Simulates fills, slippage, fees, and position management.

Anti-bias guarantees:
- Strategies only see candles up to and including the current bar
- No future data is accessible at any point in the loop
- Order fills happen at the NEXT bar's open (realistic assumption)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from src.analytics.performance import PerformanceReport, calculate_performance
from src.backtesting.commission import CommissionModel
from src.backtesting.simulator import BacktestBroker, FundingRate, PartialFillModel
from src.backtesting.slippage import SlippageModel
from src.core.enums import Timeframe
from src.core.models import OHLCV
from src.domain.trade import Trade

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    initial_equity: Decimal = Decimal("10000")
    commission_model: CommissionModel | None = None
    slippage_model: SlippageModel | None = None
    max_open_positions: int = 5
    risk_pct_per_trade: Decimal = Decimal("1.0")
    use_limit_entries: bool = False  # Market entries for simplicity
    # Historical funding rates to accrue on open positions
    funding_rates: list[FundingRate] | None = None
    # Partial-fill model; set ``enabled=False`` to restore legacy behaviour
    partial_fill: PartialFillModel | None = None
    # Opt-in: report information_ratio vs. a same-asset buy-and-hold
    # benchmark (built from the same `candles` series the backtest was
    # run on). Off by default — same opt-in convention as other optional
    # analytics — since "strategy beat buy-and-hold" is one interpretation
    # of the result, not the only one a caller may want.
    benchmark_buy_and_hold: bool = False
    # Fee rate assumed by the broker's fixed-fractional position sizing
    # (see simulator.py::BacktestBroker.place_signal). Defaults to the
    # same 0.0004 (Binance taker) that both PercentageCommission and
    # risk/position_sizing.py::calculate_position_size() already default
    # to independently, so leaving this untouched matches what the live
    # RiskManager would size for the same signal under default settings.
    sizing_fee_rate: Decimal = Decimal("0.0004")


@dataclass
class BacktestResult:
    """Complete backtest result."""

    config: BacktestConfig
    symbol: str
    timeframe: Timeframe
    start_date: datetime
    end_date: datetime
    candles_processed: int = 0
    signals_generated: int = 0
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[Decimal] = field(default_factory=list)
    performance: PerformanceReport | None = None

    def summarize(self) -> dict[str, Any]:
        if not self.performance:
            return {}
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "start": self.start_date.isoformat(),
            "end": self.end_date.isoformat(),
            "candles": self.candles_processed,
            "signals": self.signals_generated,
            "trades": len(self.trades),
            **self.performance.to_dict(),
        }


class BacktestEngine:
    """
    Event-driven backtesting engine.

    Usage:
        engine = BacktestEngine(strategy, config)
        result = await engine.run(candles, symbol, timeframe)
    """

    def __init__(self, strategy: Any, config: BacktestConfig | None = None) -> None:
        self._strategy = strategy
        self._config = config or BacktestConfig()
        self._broker = BacktestBroker(
            initial_equity=self._config.initial_equity,
            commission=self._config.commission_model,
            slippage=self._config.slippage_model,
            risk_pct=self._config.risk_pct_per_trade,
            max_positions=self._config.max_open_positions,
            funding_rates=self._config.funding_rates,
            partial_fill=self._config.partial_fill,
            sizing_fee_rate=self._config.sizing_fee_rate,
        )

    async def run(
        self,
        candles: list[OHLCV],
        symbol: str,
        timeframe: Timeframe,
        htf_candles: list[OHLCV] | None = None,
        htf_timeframe: Timeframe | None = None,
        ltf_candles: list[OHLCV] | None = None,
        ltf_timeframe: Timeframe | None = None,
        *,
        n_trials: int = 1,
    ) -> BacktestResult:
        """
        Run the backtest over the provided candle series.

        Args:
            candles: Full OHLCV history in chronological order.
            symbol: Trading symbol.
            timeframe: Candle timeframe.
            htf_candles: Optional higher timeframe candles for bias. This
                parameter previously existed but was never actually used —
                it was accepted and then silently dropped, so every
                backtest ran with htf_structure permanently UNKNOWN
                regardless of what was passed in, the same wiring gap that
                existed in the live path (see main.py SMCStrategy.on_candle).
                Now sliced per-bar and forwarded to the strategy (via
                set_htf_candles(), if the strategy exposes it) so backtest
                behaviour actually matches what a strategy using HTF
                confluence would see live.
            htf_timeframe: Required alongside htf_candles to know each HTF
                candle's duration, so only HTF candles that have *actually
                closed* as of the current bar are ever exposed — passing a
                still-forming HTF candle would leak future information the
                live system would not yet have at that timestamp.
            ltf_candles: Optional lower timeframe candles for the
                MTFContext LTF tier — same no-look-ahead handling as
                htf_candles (see set_ltf_candles()). Previously there was
                no candle source for this tier anywhere in the codebase,
                live or backtest; this is that source for backtests.
            ltf_timeframe: Required alongside ltf_candles, same role as
                htf_timeframe.
            n_trials: Number of independent parameter combinations tried
                before arriving at whatever strategy config produced
                `candles`'s results — forwarded to
                analytics/performance.py::calculate_performance() to
                deflate result.performance.deflated_sharpe_ratio
                accordingly. Defaults to 1 (a single, non-optimized run);
                optimization.py passes its actual Optuna n_trials when
                validating the winning params on OOS data, since that
                Sharpe *is* the result of a search and should be judged
                as one.

        Returns:
            BacktestResult with full trade history and performance metrics.
        """
        if len(candles) < 50:
            raise ValueError(f"Need at least 50 candles, got {len(candles)}")

        result = BacktestResult(
            config=self._config,
            symbol=symbol,
            timeframe=timeframe,
            start_date=candles[0].timestamp,
            end_date=candles[-1].timestamp,
        )
        result.equity_curve.append(self._config.initial_equity)

        await self._strategy.initialize()

        # HTF context: track how much of htf_candles is already "closed" as
        # of the current bar with a monotonically-advancing cursor, so the
        # whole backtest stays O(N + M) rather than O(N*M) from rescanning
        # htf_candles from the start on every bar.
        htf_duration = timedelta(seconds=htf_timeframe.seconds) if htf_timeframe else None
        htf_cursor = 0
        supports_htf = hasattr(self._strategy, "set_htf_candles")

        # LTF context: identical cursor pattern to HTF above, mirrored for
        # the lower-timeframe MTFContext tier.
        ltf_duration = timedelta(seconds=ltf_timeframe.seconds) if ltf_timeframe else None
        ltf_cursor = 0
        supports_ltf = hasattr(self._strategy, "set_ltf_candles")

        # Main backtest loop: O(N) time complexity
        for i in range(50, len(candles)):
            # STRICT: only expose candles[0..i] to strategy — no future data
            visible_candles = candles[: i + 1]

            if supports_htf and htf_candles and htf_duration is not None:
                current_time = candles[i].timestamp
                while (
                    htf_cursor < len(htf_candles)
                    and htf_candles[htf_cursor].timestamp + htf_duration <= current_time
                ):
                    htf_cursor += 1
                self._strategy.set_htf_candles(htf_candles[:htf_cursor] or None)

            if supports_ltf and ltf_candles and ltf_duration is not None:
                current_time = candles[i].timestamp
                while (
                    ltf_cursor < len(ltf_candles)
                    and ltf_candles[ltf_cursor].timestamp + ltf_duration <= current_time
                ):
                    ltf_cursor += 1
                self._strategy.set_ltf_candles(ltf_candles[:ltf_cursor] or None)

            # Let the broker process any pending orders at this bar's open
            self._broker.process_pending_orders(candles[i])

            # Run strategy analysis
            try:
                signal = await self._strategy.on_candle(symbol, timeframe, visible_candles)
            except Exception as exc:
                logger.warning("Strategy error at bar %d: %s", i, exc)
                signal = None

            if signal is not None:
                result.signals_generated += 1
                self._broker.place_signal(signal, candles[i])

            # Update equity curve
            equity = self._broker.equity
            result.equity_curve.append(equity)
            result.candles_processed += 1

        # Collect all closed trades
        result.trades = self._broker.closed_trades
        result.performance = calculate_performance(
            result.trades,
            initial_equity=float(self._config.initial_equity),
            benchmark_candles=candles if self._config.benchmark_buy_and_hold else None,
            n_trials=n_trials,
        )

        logger.info(
            "Backtest complete: %s %s | %d candles | %d trades | PnL=%s",
            symbol,
            timeframe.value,
            result.candles_processed,
            len(result.trades),
            result.performance.total_net_pnl if result.performance else "N/A",
        )
        return result
