"""Unit tests for src.monitoring.metrics_collector.MetricsCollector.

All 18 Prometheus metrics in src.monitoring.metrics were previously
declared but never incremented/set/observed anywhere in the codebase
(test_metrics.py only verified their *definitions*). These tests confirm
MetricsCollector actually updates the 8 event-driven metrics when the
corresponding domain event is published on the bus.

Each test reads the metric's current sample value before publishing, then
asserts it moved by the expected delta — reading absolute values would
make these tests order-dependent on any other suite that touches the same
module-level (singleton) metric objects.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.enums import (
    CircuitBreakerState,
    OrderSide,
    OrderType,
    PositionSide,
    SignalDirection,
)
from src.core.events import (
    CircuitBreakerStateChangedEvent,
    EventBus,
    OrderBookUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrderSubmittedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.monitoring import metrics
from src.monitoring.metrics_collector import MetricsCollector


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


@pytest.fixture()
async def collector(isolated_event_bus: EventBus) -> MetricsCollector:
    mc = MetricsCollector()
    await mc.start()
    return mc


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


def _gauge_value(gauge, **labels) -> float:
    target = gauge.labels(**labels) if labels else gauge
    return target._value.get()


class TestSignalGenerated:
    async def test_increments_signals_generated_with_labels(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.signals_generated, strategy="smc_v1", symbol="BTCUSDT", direction="long"
        )

        await isolated_event_bus.publish(
            SignalGeneratedEvent(
                source="test",
                symbol="BTCUSDT",
                timeframe="1h",
                direction=SignalDirection.LONG,
                score=70.0,
                confidence=0.7,
                entry_price=Decimal("50000"),
                stop_loss=Decimal("49000"),
                take_profit=Decimal("52000"),
                strategy_id="smc_v1",
            )
        )

        after = _counter_value(
            metrics.signals_generated, strategy="smc_v1", symbol="BTCUSDT", direction="long"
        )
        assert after == before + 1


class TestSignalExpired:
    async def test_increments_signals_expired_with_reason_label(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.signals_expired, symbol="BTCUSDT", reason="expired_at_generation"
        )

        await isolated_event_bus.publish(
            SignalExpiredEvent(
                source="test",
                symbol="BTCUSDT",
                signal_id="sig-1",
                reason="expired_at_generation",
            )
        )

        after = _counter_value(
            metrics.signals_expired, symbol="BTCUSDT", reason="expired_at_generation"
        )
        assert after == before + 1

    async def test_distinct_reasons_use_distinct_label_values(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.signals_expired, symbol="ETHUSDT", reason="expired_in_transit"
        )

        await isolated_event_bus.publish(
            SignalExpiredEvent(
                source="test",
                symbol="ETHUSDT",
                signal_id="sig-2",
                reason="expired_in_transit",
            )
        )

        after = _counter_value(
            metrics.signals_expired, symbol="ETHUSDT", reason="expired_in_transit"
        )
        assert after == before + 1


class TestOrderSubmitted:
    async def test_increments_orders_submitted_with_order_type_label(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.orders_submitted, symbol="ETHUSDT", side="buy", order_type="market"
        )

        await isolated_event_bus.publish(
            OrderSubmittedEvent(
                source="test",
                order_id="o-1",
                client_order_id="c-1",
                symbol="ETHUSDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                price=None,
                order_type=OrderType.MARKET,
            )
        )

        after = _counter_value(
            metrics.orders_submitted, symbol="ETHUSDT", side="buy", order_type="market"
        )
        assert after == before + 1


class TestOrderFilled:
    async def test_increments_orders_filled(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(metrics.orders_filled, symbol="BTCUSDT", side="sell")

        await isolated_event_bus.publish(
            OrderFilledEvent(
                source="test",
                order_id="o-2",
                symbol="BTCUSDT",
                side=OrderSide.SELL,
                filled_quantity=Decimal("1"),
                fill_price=Decimal("50000"),
                fee=Decimal("0.5"),
                fee_asset="USDT",
                is_partial=False,
            )
        )

        after = _counter_value(metrics.orders_filled, symbol="BTCUSDT", side="sell")
        assert after == before + 1


class TestOrderCancelled:
    async def test_increments_orders_cancelled_with_reason(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.orders_cancelled, symbol="BTCUSDT", reason="cancel_order requested"
        )

        await isolated_event_bus.publish(
            OrderCancelledEvent(
                source="test",
                order_id="o-3",
                symbol="BTCUSDT",
                reason="cancel_order requested",
            )
        )

        after = _counter_value(
            metrics.orders_cancelled, symbol="BTCUSDT", reason="cancel_order requested"
        )
        assert after == before + 1


class TestPositionLifecycle:
    async def test_position_opened_increments_open_positions_count(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _gauge_value(metrics.open_positions_count)

        await isolated_event_bus.publish(
            PositionOpenedEvent(
                source="test",
                position_id="p-1",
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("1"),
                entry_price=Decimal("50000"),
                strategy_id="smc_v1",
            )
        )

        assert _gauge_value(metrics.open_positions_count) == before + 1

    async def test_position_closed_decrements_open_positions_and_records_outcome(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        # Bring the gauge up by one first so the decrement is observable
        # without depending on ambient state from other tests.
        await isolated_event_bus.publish(
            PositionOpenedEvent(
                source="test",
                position_id="p-2",
                symbol="ETHUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("1"),
                entry_price=Decimal("3000"),
                strategy_id="smc_v1",
            )
        )
        after_open = _gauge_value(metrics.open_positions_count)

        trades_before = _counter_value(
            metrics.trades_closed, symbol="ETHUSDT", strategy="smc_v1", outcome="win"
        )

        await isolated_event_bus.publish(
            PositionClosedEvent(
                source="test",
                position_id="p-2",
                symbol="ETHUSDT",
                side=PositionSide.LONG,
                entry_price=Decimal("3000"),
                exit_price=Decimal("3100"),
                quantity=Decimal("1"),
                realized_pnl=Decimal("100"),
                fees=Decimal("1"),
                strategy_id="smc_v1",
            )
        )

        assert _gauge_value(metrics.open_positions_count) == after_open - 1
        assert (
            _counter_value(
                metrics.trades_closed, symbol="ETHUSDT", strategy="smc_v1", outcome="win"
            )
            == trades_before + 1
        )
        # Closed position's live PnL gauge resets to 0 rather than showing
        # a stale pre-close reading.
        assert _gauge_value(metrics.position_pnl, symbol="ETHUSDT") == 0.0

    async def test_position_closed_with_negative_pnl_records_loss_outcome(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.trades_closed, symbol="SOLUSDT", strategy="smc_v1", outcome="loss"
        )

        await isolated_event_bus.publish(
            PositionClosedEvent(
                source="test",
                position_id="p-3",
                symbol="SOLUSDT",
                side=PositionSide.SHORT,
                entry_price=Decimal("100"),
                exit_price=Decimal("110"),
                quantity=Decimal("1"),
                realized_pnl=Decimal("-10"),
                fees=Decimal("1"),
                strategy_id="smc_v1",
            )
        )

        after = _counter_value(
            metrics.trades_closed, symbol="SOLUSDT", strategy="smc_v1", outcome="loss"
        )
        assert after == before + 1

    async def test_position_closed_without_strategy_id_uses_unknown_label(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        before = _counter_value(
            metrics.trades_closed, symbol="ADAUSDT", strategy="unknown", outcome="breakeven"
        )

        await isolated_event_bus.publish(
            PositionClosedEvent(
                source="test",
                position_id="p-4",
                symbol="ADAUSDT",
                side=PositionSide.LONG,
                entry_price=Decimal("1"),
                exit_price=Decimal("1"),
                quantity=Decimal("1"),
                realized_pnl=Decimal("0"),
                fees=Decimal("0"),
                strategy_id=None,
            )
        )

        after = _counter_value(
            metrics.trades_closed, symbol="ADAUSDT", strategy="unknown", outcome="breakeven"
        )
        assert after == before + 1

    async def test_position_updated_sets_position_pnl_gauge(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            PositionUpdatedEvent(
                source="test",
                position_id="p-5",
                symbol="BNBUSDT",
                quantity=Decimal("1"),
                unrealized_pnl=Decimal("42.5"),
            )
        )

        assert _gauge_value(metrics.position_pnl, symbol="BNBUSDT") == 42.5


class TestCircuitBreakerState:
    async def test_open_transition_sets_gauge_to_one(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            CircuitBreakerStateChangedEvent(
                source="test",
                previous_state=CircuitBreakerState.CLOSED,
                new_state=CircuitBreakerState.OPEN,
                reason="daily loss limit",
            )
        )
        assert _gauge_value(metrics.circuit_breaker_state) == 1

    async def test_closed_transition_sets_gauge_to_zero(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            CircuitBreakerStateChangedEvent(
                source="test",
                previous_state=CircuitBreakerState.HALF_OPEN,
                new_state=CircuitBreakerState.CLOSED,
                reason="recovered",
            )
        )
        assert _gauge_value(metrics.circuit_breaker_state) == 0

    async def test_half_open_transition_sets_gauge_to_zero(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        # HALF_OPEN still permits a single probation trade through (see
        # RiskManager.validate_signal) — materially different from a hard
        # block, so it must not read the same as OPEN.
        await isolated_event_bus.publish(
            CircuitBreakerStateChangedEvent(
                source="test",
                previous_state=CircuitBreakerState.OPEN,
                new_state=CircuitBreakerState.HALF_OPEN,
                reason="cooldown elapsed",
            )
        )
        assert _gauge_value(metrics.circuit_breaker_state) == 0


class TestOrderBookUpdated:
    async def test_sets_spread_pct_gauge(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTA",
                best_bid=Decimal("50000"),
                best_ask=Decimal("50025"),
                spread=Decimal("25"),
            )
        )
        # (50025 - 50000) / 50000 * 100 = 0.05
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTA") == 0.05

    async def test_ignores_non_order_book_events(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await collector._on_order_book_updated(object())
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTB") == 0.0

    async def test_zero_best_bid_does_not_raise_or_set_gauge(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        # Guard only — feeds.py never publishes with either side at zero
        # (see market_data/feeds.py's book-ticker handler), but the
        # handler must not raise (ZeroDivisionError) if it ever did.
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTC",
                best_bid=Decimal("0"),
                best_ask=Decimal("3000"),
                spread=Decimal("3000"),
            )
        )
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTC") == 0.0

    async def test_latest_update_overwrites_previous_value(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTD",
                best_bid=Decimal("50000"),
                best_ask=Decimal("50025"),
                spread=Decimal("25"),
            )
        )
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTD",
                best_bid=Decimal("50000"),
                best_ask=Decimal("50100"),
                spread=Decimal("100"),
            )
        )
        # (50100 - 50000) / 50000 * 100 = 0.2 — a gauge, not a counter, so
        # it reflects the latest tick rather than accumulating.
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTD") == 0.2

    async def test_separate_symbols_tracked_independently(
        self, isolated_event_bus: EventBus, collector: MetricsCollector
    ) -> None:
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTE1",
                best_bid=Decimal("50000"),
                best_ask=Decimal("50025"),
                spread=Decimal("25"),
            )
        )
        await isolated_event_bus.publish(
            OrderBookUpdatedEvent(
                source="test",
                symbol="OBTESTE2",
                best_bid=Decimal("3000"),
                best_ask=Decimal("3009"),
                spread=Decimal("9"),
            )
        )
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTE1") == 0.05
        assert _gauge_value(metrics.orderbook_spread_pct, symbol="OBTESTE2") == 0.3
