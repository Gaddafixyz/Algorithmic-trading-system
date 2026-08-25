"""
Metrics Collector.

Bridges the event bus to the Prometheus metrics defined in
src/monitoring/metrics.py. All 18 metrics were previously declared but
never incremented/set/observed anywhere in the codebase — this component
closes the event-driven subset of that gap (signals, orders, fills,
cancels, position open/close, position PnL, circuit breaker state).

The remaining metrics (risk_validations, ws_messages_received,
ws_reconnects, api_requests, api_latency, candle_processing_latency,
account_equity/daily_pnl/drawdown_pct, system_info) are instrumented at
their natural call sites instead, since they either fire far more often
than the event bus should carry (per-HTTP-call latency) or aren't tied
to a discrete domain event (periodic account gauges) — see CHANGELOG.

Follows the same __init__ / async start() / event-bus-subscribe pattern
as ExecutionEngine and RiskManager.
"""

from __future__ import annotations

import logging

from src.core.enums import CircuitBreakerState
from src.core.events import (
    CircuitBreakerStateChangedEvent,
    OrderBookUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrderSubmittedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    get_event_bus,
)
from src.monitoring.metrics import (
    circuit_breaker_state,
    open_positions_count,
    orderbook_spread_pct,
    orders_cancelled,
    orders_filled,
    orders_submitted,
    position_pnl,
    signals_expired,
    signals_generated,
    trades_closed,
)

logger = logging.getLogger(__name__)


class MetricsCollector:
    """
    Subscribes to the event bus and updates Prometheus metrics.

    Purely observational — never raises out of a handler and never
    influences the trading path. A metrics-recording failure must not be
    allowed to break signal/order/position processing, so every handler
    body is wrapped defensively.
    """

    def __init__(self) -> None:
        self._bus = get_event_bus()

    async def start(self) -> None:
        self._bus.subscribe(SignalGeneratedEvent, self._on_signal_generated)
        self._bus.subscribe(SignalExpiredEvent, self._on_signal_expired)
        self._bus.subscribe(OrderSubmittedEvent, self._on_order_submitted)
        self._bus.subscribe(OrderFilledEvent, self._on_order_filled)
        self._bus.subscribe(OrderCancelledEvent, self._on_order_cancelled)
        self._bus.subscribe(PositionOpenedEvent, self._on_position_opened)
        self._bus.subscribe(PositionClosedEvent, self._on_position_closed)
        self._bus.subscribe(PositionUpdatedEvent, self._on_position_updated)
        self._bus.subscribe(CircuitBreakerStateChangedEvent, self._on_circuit_breaker_state_changed)
        self._bus.subscribe(OrderBookUpdatedEvent, self._on_order_book_updated)
        logger.info("MetricsCollector subscribed to event bus")

    async def _on_signal_generated(self, event: SignalGeneratedEvent) -> None:
        if not isinstance(event, SignalGeneratedEvent):
            return
        try:
            signals_generated.labels(
                strategy=event.strategy_id,
                symbol=event.symbol,
                direction=event.direction.value,
            ).inc()
        except Exception:
            logger.exception("Failed to record signals_generated metric")

    async def _on_signal_expired(self, event: SignalExpiredEvent) -> None:
        if not isinstance(event, SignalExpiredEvent):
            return
        try:
            # `reason` has exactly two fixed producers today
            # (orchestrator.py's "expired_at_generation",
            # execution_engine.py's "expired_in_transit" — see
            # SignalExpiredEvent's two publish call sites), so it's safe
            # as a label the same way orders_cancelled's `reason` label
            # is safe: a free-text field with, in practice, low and fixed
            # cardinality. Flag for review if a future producer starts
            # putting dynamic content into it.
            signals_expired.labels(symbol=event.symbol, reason=event.reason).inc()
        except Exception:
            logger.exception("Failed to record signals_expired metric")

    async def _on_order_submitted(self, event: OrderSubmittedEvent) -> None:
        if not isinstance(event, OrderSubmittedEvent):
            return
        try:
            orders_submitted.labels(
                symbol=event.symbol,
                side=event.side.value,
                order_type=event.order_type.value,
            ).inc()
        except Exception:
            logger.exception("Failed to record orders_submitted metric")

    async def _on_order_filled(self, event: OrderFilledEvent) -> None:
        if not isinstance(event, OrderFilledEvent):
            return
        try:
            orders_filled.labels(symbol=event.symbol, side=event.side.value).inc()
        except Exception:
            logger.exception("Failed to record orders_filled metric")

    async def _on_order_cancelled(self, event: OrderCancelledEvent) -> None:
        if not isinstance(event, OrderCancelledEvent):
            return
        try:
            # `reason` is free text on the event (see OrderCancelledEvent in
            # core/events.py) rather than a small fixed enum. This mirrors
            # the metric's pre-existing label schema in metrics.py — not a
            # new design decision — but it is a real cardinality risk if a
            # future caller starts putting dynamic content (order IDs,
            # prices) into `reason`. The one producer today (OrderManager.
            # cancel_order) passes a fixed literal string, so it's safe as
            # of this change; flagged in CHANGELOG as a known limitation to
            # watch if more cancellation call sites are added.
            orders_cancelled.labels(symbol=event.symbol, reason=event.reason).inc()
        except Exception:
            logger.exception("Failed to record orders_cancelled metric")

    async def _on_position_opened(self, event: PositionOpenedEvent) -> None:
        if not isinstance(event, PositionOpenedEvent):
            return
        try:
            open_positions_count.inc()
        except Exception:
            logger.exception("Failed to record open_positions_count metric")

    async def _on_position_closed(self, event: PositionClosedEvent) -> None:
        if not isinstance(event, PositionClosedEvent):
            return
        try:
            open_positions_count.dec()
            if event.realized_pnl > 0:
                outcome = "win"
            elif event.realized_pnl < 0:
                outcome = "loss"
            else:
                outcome = "breakeven"
            trades_closed.labels(
                symbol=event.symbol,
                strategy=event.strategy_id or "unknown",
                outcome=outcome,
            ).inc()
            # Position is fully closed — its unrealized-PnL gauge no longer
            # has a meaningful live value. Reset to 0 rather than leaving
            # the last pre-close reading stale on the dashboard/Grafana.
            position_pnl.labels(symbol=event.symbol).set(0.0)
        except Exception:
            logger.exception("Failed to record position-closed metrics")

    async def _on_position_updated(self, event: PositionUpdatedEvent) -> None:
        if not isinstance(event, PositionUpdatedEvent):
            return
        try:
            position_pnl.labels(symbol=event.symbol).set(float(event.unrealized_pnl))
        except Exception:
            logger.exception("Failed to record position_pnl metric")

    async def _on_circuit_breaker_state_changed(
        self, event: CircuitBreakerStateChangedEvent
    ) -> None:
        if not isinstance(event, CircuitBreakerStateChangedEvent):
            return
        try:
            # Gauge doc: "1 if circuit breaker is open, 0 if closed". Treat
            # HALF_OPEN as not-yet-recovered-but-not-blocking-everything —
            # it still permits a single probation trade through (see
            # RiskManager.validate_signal), which is a materially different
            # state from a hard block, so it maps to 0 rather than 1.
            circuit_breaker_state.set(1 if event.new_state == CircuitBreakerState.OPEN else 0)
        except Exception:
            logger.exception("Failed to record circuit_breaker_state metric")

    async def _on_order_book_updated(self, event: OrderBookUpdatedEvent) -> None:
        if not isinstance(event, OrderBookUpdatedEvent):
            return
        try:
            # Percentage rather than the event's raw absolute `spread`
            # field, to match the domain convention already used for
            # spread thresholds (see strategy/filters.py::filter_by_spread,
            # which computes the same (ask - bid) / bid * 100) and to keep
            # the metric comparable across symbols at very different price
            # scales. best_bid == 0 shouldn't happen in practice (feeds.py
            # skips publishing when either side of the book ticker is
            # absent) but is guarded rather than trusted.
            if event.best_bid == 0:
                return
            spread_pct = float((event.best_ask - event.best_bid) / event.best_bid * 100)
            orderbook_spread_pct.labels(symbol=event.symbol).set(spread_pct)
        except Exception:
            logger.exception("Failed to record orderbook_spread_pct metric")
