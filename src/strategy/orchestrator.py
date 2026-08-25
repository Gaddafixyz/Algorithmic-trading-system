"""
Strategy Orchestrator.

Connects the event bus (candle close events) to registered strategies.
Routes signals to the execution engine via the event bus.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from src.core.enums import Timeframe
from src.core.events import (
    CandleClosedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    get_event_bus,
)
from src.core.exceptions import NoSignalError, SignalGenerationError
from src.core.interfaces import IStrategy
from src.domain.position import Position
from src.market_data.cache import MarketDataCache
from src.market_data.candles import CandleService
from src.monitoring.metrics import candle_processing_latency
from src.strategy.filters import (
    filter_by_kill_zone,
    filter_by_macd_trend,
    filter_by_minimum_atr,
    filter_by_rsi_exhaustion,
    filter_by_spread,
)
from src.utils.profiling import RollingTimer

logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    """
    Routes market data events to registered strategies and
    publishes generated signals to the event bus.
    """

    def __init__(
        self,
        candle_service: CandleService,
        position_manager: Any | None = None,
        db: Any | None = None,
        min_atr_pct: float = 0.0,
        rsi_exhaustion_filter: bool = False,
        rsi_overbought: float = 80.0,
        rsi_oversold: float = 20.0,
        macd_trend_filter: bool = False,
        market_data_cache: MarketDataCache | None = None,
        max_spread_pct: float = 0.0,
        kill_zone_filter: bool = False,
        require_kill_zone: bool = True,
    ) -> None:
        self._strategies: dict[str, IStrategy] = {}
        self._candle_service = candle_service
        self._positions = position_manager
        self._db = db
        self._bus = get_event_bus()
        self._running = False
        self._signals_generated = 0
        self._signals_filtered_atr = 0
        self._signals_filtered_rsi = 0
        self._signals_filtered_macd = 0
        self._signals_filtered_spread = 0
        self._signals_filtered_kill_zone = 0
        # Per-strategy rolling candle-processing latency, sampled alongside
        # the existing candle_processing_latency Prometheus histogram.
        # RollingTimer gives an in-process avg/p99 readable synchronously
        # from .stats (e.g. for the dashboard) without a Prometheus query —
        # useful for at-a-glance health during local runs or when Prometheus
        # isn't scraped yet. window=100 candles is a few hours to a day of
        # history depending on timeframe, enough to smooth out one-off GC
        # pauses or slow candles without masking a sustained regression.
        self._latency_timers: dict[str, RollingTimer] = {}
        # 0 (default) disables the check entirely — filter_by_minimum_atr
        # itself also no-ops when called with atr=0 (dead/missing ATR data),
        # so a strategy that doesn't populate signal.atr never gets
        # spuriously blocked either.
        self._min_atr_pct = min_atr_pct
        # RSI/MACD confirmation filters — both False (disabled) by default,
        # same reasoning as min_atr_pct=0.0: these are opt-in gates on top
        # of confluence.py's existing 100pt score, not part of it, so
        # existing signal flow is unchanged unless explicitly enabled.
        # rsi/macd values live in signal.metadata (see confluence.py's
        # score_confluence()), not as first-class Signal fields, since
        # they're informational/filter inputs rather than trade parameters.
        self._rsi_exhaustion_filter = rsi_exhaustion_filter
        self._rsi_overbought = rsi_overbought
        self._rsi_oversold = rsi_oversold
        self._macd_trend_filter = macd_trend_filter
        # Execution-quality guard: reject a signal if the live best-bid/
        # best-ask spread (from MarketDataFeed's book-ticker handler, via
        # MarketDataCache.get_best_bid_ask) is wider than max_spread_pct.
        # 0 (default) disables the check — same opt-in pattern as
        # min_atr_pct/rsi_exhaustion_filter/macd_trend_filter above. Also
        # a no-op if market_data_cache is None or has no cached quote yet
        # for the symbol (nothing published/subscribed to book-ticker),
        # so existing callers/tests are unaffected.
        self._market_data_cache = market_data_cache
        self._max_spread_pct = max_spread_pct
        # Session-timing gate: reject a signal generated outside an ICT
        # kill zone (see strategy/filters.py::filter_by_kill_zone and
        # smart_money/killzones.py::should_trade_now). should_trade_now()
        # was fully implemented and unit-tested (tests/strategy/
        # test_killzones.py) but had no caller anywhere in the strategy
        # pipeline before this wiring — confluence.py's "session" scoring
        # factor uses smart_money/sessions.py::session_score as a
        # continuous weight, which is a different mechanism (nudges the
        # 0-100 score) from this (hard pass/reject), so the two don't
        # duplicate each other. False (disabled) by default, same opt-in
        # pattern as rsi_exhaustion_filter/macd_trend_filter/max_spread_pct
        # above — existing signal flow is unchanged unless explicitly
        # enabled.
        self._kill_zone_filter = kill_zone_filter
        self._require_kill_zone = require_kill_zone

    async def _persist_signal(self, signal: Any) -> None:
        """Best-effort persistence of a generated signal."""
        if self._db is None:
            return
        try:
            from src.persistence.signals import SignalRepository

            async with self._db.session() as session:
                await SignalRepository(session).save_signal(signal)
        except Exception as exc:
            logger.warning("Failed to persist signal %s: %s", signal.signal_id, exc)

    def register_strategy(self, strategy: IStrategy) -> None:
        """Register a strategy to receive market data events."""
        sid = strategy.strategy_id
        if sid in self._strategies:
            raise ValueError(f"Strategy '{sid}' already registered")
        self._strategies[sid] = strategy
        logger.info("Registered strategy: %s", sid)

    async def start(self) -> None:
        """Initialize all strategies and subscribe to candle events."""
        for strategy in self._strategies.values():
            await strategy.initialize()

        self._bus.subscribe(CandleClosedEvent, self._on_candle_closed)
        self._bus.subscribe(PositionOpenedEvent, self._on_position_opened)
        self._bus.subscribe(PositionClosedEvent, self._on_position_closed)
        self._running = True
        logger.info("StrategyOrchestrator started with %d strategies", len(self._strategies))

    async def stop(self) -> None:
        self._running = False
        logger.info("StrategyOrchestrator stopped")

    async def _on_candle_closed(self, event: Any) -> None:
        """Dispatch a candle close event to all relevant strategies."""
        if not isinstance(event, CandleClosedEvent):
            return

        symbol = event.symbol
        timeframe = Timeframe(event.timeframe)

        for strategy in self._strategies.values():
            if symbol not in strategy.symbols:
                continue
            if timeframe not in strategy.timeframes:
                continue

            try:
                candles = self._candle_service.get_candles(symbol, timeframe, limit=500)
                # candle_processing_latency (trading_candle_processing_seconds)
                # was declared in metrics.py but never observed. Times just
                # the strategy call itself (not candle-fetch, which is an
                # in-memory cache read) since that's the variable-cost part
                # of "processing a candle close through strategy" the metric
                # name describes.
                _t0 = time.monotonic()
                try:
                    signal = await strategy.on_candle(symbol, timeframe, candles)
                except NoSignalError:
                    raise
                except Exception as exc:
                    raise SignalGenerationError(
                        f"Strategy '{strategy.strategy_id}' failed generating a "
                        f"signal for {symbol} {timeframe.value}: {exc}"
                    ) from exc
                _elapsed = time.monotonic() - _t0
                candle_processing_latency.labels(symbol=symbol, timeframe=timeframe.value).observe(
                    _elapsed
                )
                self._latency_timers.setdefault(
                    strategy.strategy_id, RollingTimer(window=100)
                ).record(_elapsed * 1000)

                if signal is not None:
                    if signal.is_expired():
                        logger.warning(
                            "Strategy '%s' returned an already-expired signal for %s %s "
                            "(generated_at=%s, max_age=%.0fs) — discarding",
                            strategy.strategy_id,
                            symbol,
                            timeframe,
                            signal.generated_at.isoformat(),
                            signal.max_age_seconds,
                        )
                        await self._bus.publish(
                            SignalExpiredEvent(
                                source=strategy.strategy_id,
                                symbol=symbol,
                                signal_id=signal.signal_id,
                                reason="expired_at_generation",
                            )
                        )
                        continue

                    if self._min_atr_pct > 0 and not filter_by_minimum_atr(
                        signal, float(signal.atr), self._min_atr_pct
                    ):
                        self._signals_filtered_atr += 1
                        logger.info(
                            "Strategy '%s' signal for %s %s filtered: ATR too low "
                            "(min_atr_pct=%.4f)",
                            strategy.strategy_id,
                            symbol,
                            timeframe,
                            self._min_atr_pct,
                        )
                        continue

                    if self._kill_zone_filter and not filter_by_kill_zone(self._require_kill_zone):
                        self._signals_filtered_kill_zone += 1
                        logger.info(
                            "Strategy '%s' signal for %s %s filtered: outside kill zone "
                            "(require_kill_zone=%s)",
                            strategy.strategy_id,
                            symbol,
                            timeframe,
                            self._require_kill_zone,
                        )
                        continue

                    if self._rsi_exhaustion_filter and not filter_by_rsi_exhaustion(
                        signal,
                        signal.metadata.get("rsi"),
                        self._rsi_overbought,
                        self._rsi_oversold,
                    ):
                        self._signals_filtered_rsi += 1
                        logger.info(
                            "Strategy '%s' signal for %s %s filtered: RSI exhaustion",
                            strategy.strategy_id,
                            symbol,
                            timeframe,
                        )
                        continue

                    if self._macd_trend_filter and not filter_by_macd_trend(
                        signal, signal.metadata.get("macd_histogram")
                    ):
                        self._signals_filtered_macd += 1
                        logger.info(
                            "Strategy '%s' signal for %s %s filtered: MACD trend disagreement",
                            strategy.strategy_id,
                            symbol,
                            timeframe,
                        )
                        continue

                    if self._max_spread_pct > 0 and self._market_data_cache is not None:
                        quote = self._market_data_cache.get_best_bid_ask(symbol)
                        if quote is not None:
                            best_bid, best_ask = quote
                            if not filter_by_spread(
                                signal, float(best_bid), float(best_ask), self._max_spread_pct
                            ):
                                self._signals_filtered_spread += 1
                                logger.info(
                                    "Strategy '%s' signal for %s %s filtered: spread too wide "
                                    "(max_spread_pct=%.4f)",
                                    strategy.strategy_id,
                                    symbol,
                                    timeframe,
                                    self._max_spread_pct,
                                )
                                continue

                    self._signals_generated += 1
                    await self._persist_signal(signal)
                    await self._bus.publish(
                        SignalGeneratedEvent(
                            source=strategy.strategy_id,
                            symbol=signal.symbol,
                            timeframe=signal.timeframe.value,
                            direction=signal.direction,
                            score=signal.score,
                            confidence=signal.confidence,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit,
                            strategy_id=strategy.strategy_id,
                            atr=signal.atr,
                            signal_id=signal.signal_id,
                            open_fvgs=signal.metadata.get("open_fvgs", []),
                        )
                    )
                else:
                    # NoSignalError is a control-flow sentinel, not a fault
                    # (see core/exceptions.py) — most candle closes don't
                    # produce a signal, so this is the expected common case,
                    # not something to log as an error.
                    raise NoSignalError(
                        symbol=symbol,
                        reason=(
                            f"strategy '{strategy.strategy_id}' found no valid "
                            f"setup on {timeframe.value}"
                        ),
                    )

            except NoSignalError:
                continue
            except Exception as exc:
                logger.exception(
                    "Strategy '%s' error on %s %s: %s",
                    strategy.strategy_id,
                    symbol,
                    timeframe,
                    exc,
                )

    async def _on_position_opened(self, event: Any) -> None:
        """Notify the owning strategy so it tracks the open position."""
        if not isinstance(event, PositionOpenedEvent):
            return
        strategy = self._strategies.get(event.strategy_id or "")
        if strategy is None or self._positions is None:
            return
        if not hasattr(strategy, "register_position"):
            return
        position = self._positions.get(event.position_id)
        if position is not None:
            strategy.register_position(position)  # type: ignore[attr-defined]

    async def _on_position_closed(self, event: Any) -> None:
        """
        Notify the owning strategy that a position closed.

        This releases the strategy's position tracking and, via
        BaseStrategy.on_position_closed(), clears the associated active
        signal immediately rather than waiting for it to self-heal via
        Signal.is_expired()'s max_age_seconds fallback (up to an hour).
        """
        if not isinstance(event, PositionClosedEvent):
            return
        strategy = self._strategies.get(event.strategy_id or "")
        if strategy is None or not hasattr(strategy, "on_position_closed"):
            return

        position = self._positions.get(event.position_id) if self._positions else None
        if position is None:
            # Fall back to a lightweight reconstruction from the event so
            # the strategy still hears about the close even if the position
            # is no longer resolvable (e.g. no position_manager wired in).
            position = Position(
                position_id=event.position_id,
                symbol=event.symbol,
                side=event.side,
                quantity=Decimal("0"),
                entry_quantity=event.quantity,
                entry_price=event.entry_price,
                mark_price=event.exit_price,
                strategy_id=event.strategy_id,
            )

        try:
            await strategy.on_position_closed(position)
        except Exception as exc:
            logger.exception(
                "Strategy '%s' error handling position close for %s: %s",
                strategy.strategy_id,
                event.symbol,
                exc,
            )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "strategies": len(self._strategies),
            "signals_generated": self._signals_generated,
            "signals_filtered_atr": self._signals_filtered_atr,
            "signals_filtered_kill_zone": self._signals_filtered_kill_zone,
            "signals_filtered_rsi": self._signals_filtered_rsi,
            "signals_filtered_macd": self._signals_filtered_macd,
            "signals_filtered_spread": self._signals_filtered_spread,
            "running": self._running,
            "candle_processing_latency_ms": {
                strategy_id: {"avg": t.avg_ms, "p99": t.p99_ms}
                for strategy_id, t in self._latency_timers.items()
            },
        }
