"""
Event-driven architecture backbone.

Events flow through the system via an async event bus.
Handlers are registered by event type; all handlers run concurrently.

Design:
- Events are immutable value objects (frozen Pydantic models).
- The bus is async and non-blocking.
- Failed handlers are logged but do not block other handlers.
- Wildcard subscription is supported via subscribe_all().
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Coroutine, TypeAlias

from pydantic import Field

from src.core.enums import (
    AlertSeverity,
    CircuitBreakerState,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalDirection,
)
from src.core.models import StrictModel, utc_now

logger = logging.getLogger(__name__)

# Type alias for async event handler
EventHandler: TypeAlias = Callable[["BaseEvent"], Coroutine[Any, Any, None]]


class BaseEvent(StrictModel):
    """Base class for all system events."""

    event_id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    timestamp: datetime = Field(default_factory=utc_now)
    source: str = "system"

    @property
    def event_type(self) -> str:
        return self.__class__.__name__


# ─── Market Data Events ───────────────────────────────────────────────────────

class CandleClosedEvent(BaseEvent):
    """Fired when a candlestick closes."""

    symbol: str
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    candle_timestamp: datetime


class TickReceivedEvent(BaseEvent):
    """Fired on each trade/tick update."""

    symbol: str
    price: Decimal
    quantity: Decimal
    is_buyer_maker: bool


class OrderBookUpdatedEvent(BaseEvent):
    """Fired when order book depth changes."""

    symbol: str
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal


class FundingRateEvent(BaseEvent):
    """Fired on funding rate update."""

    symbol: str
    rate: Decimal
    next_funding_time: datetime


class MarkPriceUpdatedEvent(BaseEvent):
    """
    Fired on each update from the exchange's dedicated mark-price stream
    (Binance `<symbol>@markPrice`).

    This is the exchange-computed price used for unrealized PnL, margin,
    and liquidation calculations — distinct from `TickReceivedEvent.price`,
    which reflects the last executed trade and can diverge from mark price
    during fast moves or thin liquidity. Consumers that maintain
    position.mark_price (e.g. ExecutionEngine, RiskManager) should source
    it from this event, not from trade ticks.
    """

    symbol: str
    mark_price: Decimal
    index_price: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None


# ─── Signal Events ────────────────────────────────────────────────────────────

class SignalGeneratedEvent(BaseEvent):
    """Fired when a trading signal is produced."""

    symbol: str
    timeframe: str
    direction: SignalDirection
    score: float
    confidence: float
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    strategy_id: str
    # ATR at signal generation time — passed through to the position's
    # entry_atr tag so tick-level trailing-stop management has a valid
    # volatility baseline. Without this field, ExecutionEngine's Signal
    # reconstruction from the event leaves atr=None, the position gets
    # no entry_atr tag, and all trailing/breakeven calculations silently
    # degrade to zero-ATR mode.
    atr: Decimal | None = None
    # The originating Signal's stable identity. Without this, every
    # consumer that reconstructs a Signal from this event (ExecutionEngine)
    # got a *fresh* Signal.signal_id (its Pydantic default_factory mints a
    # new uuid4 per construction), which silently diverged from the
    # signal_id the orchestrator already persisted via _persist_signal().
    # Every order/position/trade downstream carries signal_id for lineage
    # (see src/persistence/{orders,trades}.py) — without this field, that
    # lineage pointed at an id that exists nowhere in the signals table.
    signal_id: str = ""
    # Open FVG snapshot from confluence.py's scoring metadata (see
    # score_confluence()'s "open_fvgs" key), carried through so
    # ExecutionEngine can classify target/reversal zones for exit
    # management once a position opens (see _on_entry_filled). Same
    # rationale as atr/signal_id above: the internal Signal ExecutionEngine
    # reconstructs from this event only has what's on the event itself —
    # without this field, signal.metadata is always empty on that path and
    # every FVG exit zone silently resolves to nothing.
    open_fvgs: list[dict[str, str]] = Field(default_factory=list)


class SignalExpiredEvent(BaseEvent):
    """Fired when a signal expires without being acted on."""

    symbol: str
    signal_id: str
    reason: str


# ─── Order Events ─────────────────────────────────────────────────────────────

class OrderSubmittedEvent(BaseEvent):
    """Fired when an order is sent to the exchange."""

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal | None
    # Without this, the trading_orders_submitted_total Prometheus counter
    # (which is labeled by order_type) had no way to populate that label
    # from this event — MetricsCollector's subscriber would have had to
    # fall back to a constant/"unknown" value for every order.
    order_type: OrderType


class OrderFilledEvent(BaseEvent):
    """Fired when an order is partially or fully filled."""

    order_id: str
    symbol: str
    side: OrderSide
    filled_quantity: Decimal
    fill_price: Decimal
    fee: Decimal
    fee_asset: str
    is_partial: bool


class OrderCancelledEvent(BaseEvent):
    """Fired when an order is cancelled."""

    order_id: str
    symbol: str
    reason: str


class OrderStatusChangedEvent(BaseEvent):
    """Fired on any order status transition."""

    order_id: str
    symbol: str
    previous_status: OrderStatus
    new_status: OrderStatus


class OrderRejectedEvent(BaseEvent):
    """Fired when an order is rejected by the exchange."""

    order_id: str
    symbol: str
    reason: str
    error_code: str | None = None


# ─── Position Events ──────────────────────────────────────────────────────────

class PositionOpenedEvent(BaseEvent):
    """Fired when a new position is opened."""

    position_id: str
    symbol: str
    side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    strategy_id: str | None = None


class PositionClosedEvent(BaseEvent):
    """Fired when a position is fully closed."""

    position_id: str
    symbol: str
    side: PositionSide
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    realized_pnl: Decimal
    fees: Decimal
    strategy_id: str | None = None


class PositionUpdatedEvent(BaseEvent):
    """Fired when a position's size changes."""

    position_id: str
    symbol: str
    quantity: Decimal
    unrealized_pnl: Decimal


# ─── Risk Events ─────────────────────────────────────────────────────────────

class RiskLimitBreachedEvent(BaseEvent):
    """Fired when a risk limit is exceeded."""

    limit_name: str
    current_value: float
    limit_value: float
    severity: AlertSeverity


class CircuitBreakerStateChangedEvent(BaseEvent):
    """Fired on circuit breaker state transitions."""

    previous_state: CircuitBreakerState
    new_state: CircuitBreakerState
    reason: str


class DailyLossLimitReachedEvent(BaseEvent):
    """Fired when daily loss limit is hit."""

    daily_pnl: Decimal
    limit_pct: Decimal


# ─── System Events ────────────────────────────────────────────────────────────

class SystemStartedEvent(BaseEvent):
    """Fired after successful system startup."""

    trading_env: str
    version: str


class SystemShutdownEvent(BaseEvent):
    """Fired when graceful shutdown begins."""

    reason: str


class ComponentHealthChangedEvent(BaseEvent):
    """Fired when a component's health changes."""

    component: str
    previous_status: str
    new_status: str
    detail: str = ""


class AlertFiredEvent(BaseEvent):
    """Fired when a monitoring alert triggers."""

    alert_name: str
    severity: AlertSeverity
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


# ─── Event Bus ────────────────────────────────────────────────────────────────

class EventBus:
    """
    Async event bus with per-type subscriptions.

    Thread-safety: designed for single-threaded asyncio; use
    asyncio.run_coroutine_threadsafe for cross-thread publishing.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._published_count: int = 0
        self._error_count: int = 0
        # Tracks fire-and-forget tasks scheduled by publish_sync() so they
        # are not garbage-collected mid-execution and so exceptions raised
        # inside them are surfaced via logging rather than silently
        # swallowed at task destruction.
        self._background_tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type[BaseEvent], handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        key = event_type.__name__
        self._handlers[key].append(handler)
        logger.debug("Subscribed %s to %s", handler.__qualname__, key)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Register a handler that receives every event."""
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: type[BaseEvent], handler: EventHandler) -> None:
        """Remove a handler registration."""
        key = event_type.__name__
        try:
            self._handlers[key].remove(handler)
        except ValueError:
            logger.warning("Handler %s not registered for %s", handler.__qualname__, key)

    async def publish(self, event: BaseEvent) -> None:
        """Publish an event to all registered handlers concurrently."""
        self._published_count += 1
        handlers = (
            self._handlers.get(event.event_type, []) + self._global_handlers
        )
        if not handlers:
            return

        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )
        for result, handler in zip(results, handlers, strict=False):
            if isinstance(result, Exception):
                self._error_count += 1
                logger.exception(
                    "Event handler %s failed for %s: %s",
                    handler.__qualname__,
                    event.event_type,
                    result,
                )

    def publish_sync(self, event: BaseEvent) -> None:
        """
        Fire-and-forget publish from synchronous code.
        Schedules the event on the running event loop.

        The scheduled task is tracked internally (and reaped on
        completion) so it cannot be garbage-collected mid-flight and so
        any exception it raises is logged rather than silently dropped.
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.publish(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
        except RuntimeError:
            # No running loop — create one for this call
            asyncio.run(self.publish(event))

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        """Reap a completed publish_sync task and log any exception."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._error_count += 1
            logger.error(
                "Unhandled exception in publish_sync background task: %s",
                exc,
                exc_info=exc,
            )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "published": self._published_count,
            "errors": self._error_count,
            "handler_registrations": sum(len(v) for v in self._handlers.values()),
        }


# Singleton event bus
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the singleton event bus."""
    global _event_bus  # noqa: PLW0603
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def set_event_bus(bus: EventBus) -> None:
    """
    Install a specific event bus instance as the process-wide singleton.

    Call this once, before any component calls ``get_event_bus()`` (i.e.
    at the very start of bootstrap), to swap in a ``RedisEventBus`` or
    any other ``EventBus`` subclass without touching every call site that
    already does ``self._bus = get_event_bus()``.
    """
    global _event_bus  # noqa: PLW0603
    if _event_bus is not None:
        logger.warning(
            "set_event_bus() called after the event bus singleton was "
            "already created — existing subscribers keep the old bus."
        )
    _event_bus = bus


def reset_event_bus() -> None:
    """Reset the singleton — for testing only."""
    global _event_bus  # noqa: PLW0603
    _event_bus = None
