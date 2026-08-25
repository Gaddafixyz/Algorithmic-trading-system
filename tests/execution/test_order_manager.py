"""Unit tests for src.execution.order_manager.OrderManager."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.enums import OrderSide, OrderStatus, OrderType
from src.core.events import (
    EventBus,
    OrderCancelledEvent,
    OrderRejectedEvent,
    OrderStatusChangedEvent,
    OrderSubmittedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.core.exceptions import (
    DuplicateOrderError,
    ExchangeConnectionError,
    ExchangeTimeoutError,
    OrderNotFoundError,
    OrderSubmissionError,
)
from src.domain.order import Order
from src.execution.order_manager import OrderManager


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def make_order(
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: str = "0.1",
    client_order_id: str | None = None,
) -> Order:
    kwargs: dict = dict(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
    )
    if client_order_id is not None:
        kwargs["client_order_id"] = client_order_id
    return Order(**kwargs)


def make_exchanged_order(source: Order, exchange_order_id: str = "EXCH-1") -> Order:
    """Return a copy of `source` as the exchange would echo it back."""
    return source.model_copy(update={"exchange_order_id": exchange_order_id})


@pytest.fixture
def exchange() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def manager(exchange: AsyncMock) -> OrderManager:
    return OrderManager(exchange=exchange)


class TestSubmitOrderSuccess:
    async def test_transitions_pending_order_to_open_on_success(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order, "EXCH-100")

        result = await manager.submit_order(order)

        assert result.status == OrderStatus.OPEN
        assert result.exchange_order_id == "EXCH-100"

    async def test_registers_the_order_for_lookup_by_order_id(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)

        assert await manager.get_order(order.order_id) is order

    async def test_registers_the_order_for_lookup_by_client_order_id(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)

        assert await manager.get_order_by_client_id(order.client_order_id) is order

    async def test_increments_submitted_count(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)

        assert manager.stats["submitted"] == 1

    async def test_calls_exchange_place_order_with_the_orders_fields(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order(quantity="0.5")
        exchange.place_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)

        exchange.place_order.assert_awaited_once()
        _, kwargs = exchange.place_order.call_args
        assert kwargs["symbol"] == "BTCUSDT"
        assert kwargs["quantity"] == Decimal("0.5")
        assert kwargs["client_order_id"] == order.client_order_id


class TestSubmitOrderDuplicateDetection:
    async def test_raises_duplicate_order_error_for_a_repeated_client_order_id(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order_a = make_order(client_order_id="dup-id-1")
        exchange.place_order.return_value = make_exchanged_order(order_a)
        await manager.submit_order(order_a)

        order_b = make_order(client_order_id="dup-id-1")
        with pytest.raises(DuplicateOrderError):
            await manager.submit_order(order_b)

    async def test_does_not_call_the_exchange_for_a_detected_duplicate(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order_a = make_order(client_order_id="dup-id-2")
        exchange.place_order.return_value = make_exchanged_order(order_a)
        await manager.submit_order(order_a)
        exchange.place_order.reset_mock()

        order_b = make_order(client_order_id="dup-id-2")
        with pytest.raises(DuplicateOrderError):
            await manager.submit_order(order_b)
        exchange.place_order.assert_not_awaited()


class TestSubmitOrderDefiniteFailure:
    async def test_generic_exchange_exception_marks_order_failed_and_raises(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.side_effect = RuntimeError("exchange rejected: insufficient margin")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)

        assert order.status == OrderStatus.FAILED
        assert "insufficient margin" in order.error_message

    async def test_definite_failure_frees_the_client_order_id_for_reuse(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        # A genuinely rejected order (not ambiguous) must free its
        # client_order_id so the caller can safely resubmit under the
        # same ID without hitting a false DuplicateOrderError.
        order = make_order(client_order_id="reusable-id")
        exchange.place_order.side_effect = RuntimeError("rejected")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)

        retry_order = make_order(client_order_id="reusable-id")
        exchange.place_order.side_effect = None
        exchange.place_order.return_value = make_exchanged_order(retry_order)
        result = await manager.submit_order(retry_order)
        assert result.status == OrderStatus.OPEN


class TestSubmitOrderAmbiguousFailureRecovery:
    """The core double-order-prevention safety property.

    A timeout/connection error during submission is ambiguous: the
    order may never have reached the exchange, or it may have landed
    and only the acknowledgment was lost. OrderManager must check with
    the exchange directly before deciding whether it's safe to free the
    client_order_id for a retry.
    """

    async def test_order_that_actually_landed_is_recovered_as_open(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.side_effect = ExchangeTimeoutError("place_order", 10.0)
        exchange.get_order.return_value = make_exchanged_order(order, "EXCH-RECOVERED")

        result = await manager.submit_order(order)

        assert result.status == OrderStatus.OPEN
        assert result.exchange_order_id == "EXCH-RECOVERED"
        assert manager.stats["submitted"] == 1

    async def test_order_that_actually_landed_keeps_its_client_order_id_reserved(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        # This is the critical safety property: once recovery confirms
        # the order landed, the client_order_id must remain reserved
        # (NOT freed), otherwise a caller could resubmit and create a
        # second live order on the exchange for what is actually a
        # single already-open position.
        order = make_order(client_order_id="ambiguous-but-landed")
        exchange.place_order.side_effect = ExchangeConnectionError("connection reset")
        exchange.get_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)

        duplicate_attempt = make_order(client_order_id="ambiguous-but-landed")
        with pytest.raises(DuplicateOrderError):
            await manager.submit_order(duplicate_attempt)

    async def test_order_that_did_not_land_is_marked_failed_and_id_freed(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order(client_order_id="ambiguous-never-landed")
        exchange.place_order.side_effect = ExchangeTimeoutError("place_order", 10.0)
        exchange.get_order.side_effect = OrderNotFoundError("ambiguous-never-landed", "BTCUSDT")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)

        assert order.status == OrderStatus.FAILED

        # Since it genuinely never landed, the client_order_id must be
        # freed for a safe retry.
        retry_order = make_order(client_order_id="ambiguous-never-landed")
        exchange.place_order.side_effect = None
        exchange.place_order.return_value = make_exchanged_order(retry_order)
        result = await manager.submit_order(retry_order)
        assert result.status == OrderStatus.OPEN

    async def test_check_landed_itself_erroring_is_treated_as_did_not_land(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        # If confirming with the exchange also fails, the safer
        # (documented) assumption is "did not land" rather than looping
        # or assuming success.
        order = make_order()
        exchange.place_order.side_effect = ExchangeTimeoutError("place_order", 10.0)
        exchange.get_order.side_effect = RuntimeError("exchange API is down")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)
        assert order.status == OrderStatus.FAILED


class TestCancelOrder:
    async def test_returns_false_for_an_unknown_order_id(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        result = await manager.cancel_order("nonexistent-id", "BTCUSDT")
        assert result is False
        exchange.cancel_order.assert_not_awaited()

    async def test_returns_false_for_an_already_terminal_order(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        order.transition_to(OrderStatus.FILLED)  # terminal state

        result = await manager.cancel_order(order.order_id, order.symbol)
        assert result is False
        exchange.cancel_order.assert_not_awaited()

    async def test_successfully_cancels_an_open_order(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        exchange.cancel_order.return_value = True

        result = await manager.cancel_order(order.order_id, order.symbol)

        assert result is True
        assert order.status == OrderStatus.CANCELLED
        assert manager.stats["cancelled"] == 1

    async def test_failed_exchange_cancellation_does_not_change_local_status(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        exchange.cancel_order.return_value = False

        result = await manager.cancel_order(order.order_id, order.symbol)

        assert result is False
        assert order.status == OrderStatus.OPEN
        assert manager.stats["cancelled"] == 0

    async def test_uses_exchange_order_id_when_available(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order, "EXCH-777")
        await manager.submit_order(order)
        exchange.cancel_order.return_value = True

        await manager.cancel_order(order.order_id, order.symbol)

        exchange.cancel_order.assert_awaited_once_with(order.symbol, "EXCH-777")


class TestCancelAllOrders:
    async def test_cancels_only_open_orders_matching_the_symbol_filter(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        btc_order = make_order(symbol="BTCUSDT")
        eth_order = make_order(symbol="ETHUSDT")
        exchange.place_order.side_effect = [
            make_exchanged_order(btc_order),
            make_exchanged_order(eth_order),
        ]
        await manager.submit_order(btc_order)
        await manager.submit_order(eth_order)
        exchange.cancel_order.return_value = True

        count = await manager.cancel_all_orders(symbol="BTCUSDT")

        assert count == 1
        assert btc_order.status == OrderStatus.CANCELLED
        assert eth_order.status == OrderStatus.OPEN

    async def test_cancels_all_open_orders_when_no_symbol_filter_given(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        btc_order = make_order(symbol="BTCUSDT")
        eth_order = make_order(symbol="ETHUSDT")
        exchange.place_order.side_effect = [
            make_exchanged_order(btc_order),
            make_exchanged_order(eth_order),
        ]
        await manager.submit_order(btc_order)
        await manager.submit_order(eth_order)
        exchange.cancel_order.return_value = True

        count = await manager.cancel_all_orders()
        assert count == 2

    async def test_skips_orders_that_are_already_terminal(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        order.transition_to(OrderStatus.FILLED)

        count = await manager.cancel_all_orders()
        assert count == 0
        exchange.cancel_order.assert_not_awaited()


class TestGetOpenOrders:
    async def test_returns_only_open_orders(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        open_order = make_order()
        exchange.place_order.return_value = make_exchanged_order(open_order)
        await manager.submit_order(open_order)

        failed_order = make_order()
        exchange.place_order.side_effect = RuntimeError("rejected")
        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(failed_order)

        result = manager.get_open_orders()
        assert result == [open_order]

    async def test_filters_by_symbol(self, manager: OrderManager, exchange: AsyncMock) -> None:
        btc_order = make_order(symbol="BTCUSDT")
        eth_order = make_order(symbol="ETHUSDT")
        exchange.place_order.side_effect = [
            make_exchanged_order(btc_order),
            make_exchanged_order(eth_order),
        ]
        await manager.submit_order(btc_order)
        await manager.submit_order(eth_order)

        result = manager.get_open_orders(symbol="ETHUSDT")
        assert result == [eth_order]


class TestGetOrderByClientId:
    async def test_returns_none_for_an_unknown_client_order_id(self, manager: OrderManager) -> None:
        assert await manager.get_order_by_client_id("never-submitted") is None


class TestStats:
    async def test_stats_reflects_total_submitted_cancelled_and_open_counts(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        exchange.cancel_order.return_value = True
        await manager.cancel_order(order.order_id, order.symbol)

        stats = manager.stats
        assert stats["total_orders"] == 1
        assert stats["submitted"] == 1
        assert stats["cancelled"] == 1
        assert stats["open"] == 0


class TestSubmitLatencyTracking:
    async def test_stats_reports_zero_latency_before_any_submission(
        self, manager: OrderManager
    ) -> None:
        stats = manager.stats
        assert stats["submit_latency_ms"] == {"avg": 0.0, "p99": 0.0}

    async def test_successful_submission_records_a_latency_sample(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)

        stats = manager.stats
        assert stats["submit_latency_ms"]["avg"] >= 0.0
        assert stats["submit_latency_ms"]["p99"] >= 0.0
        # A real sample was recorded, not just the zero-sample default.
        assert manager._submit_latency._samples  # noqa: SLF001

    async def test_definite_failure_does_not_record_a_latency_sample(
        self, manager: OrderManager, exchange: AsyncMock
    ) -> None:
        order = make_order()
        exchange.place_order.side_effect = OrderSubmissionError("rejected")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)

        assert manager._submit_latency._samples == []  # noqa: SLF001


def _recorder(bus: EventBus, event_type: type) -> list:
    """Subscribe a collector for event_type and return the list it appends to."""
    received: list = []

    async def _handler(event: object) -> None:
        received.append(event)

    bus.subscribe(event_type, _handler)
    return received


class TestOrderLifecycleEventsArePublished:
    """OrderSubmittedEvent/OrderStatusChangedEvent/OrderRejectedEvent/
    OrderCancelledEvent previously existed as schema only with no
    producer anywhere — these confirm OrderManager now actually
    publishes them via the fire-and-forget publish_sync path."""

    async def test_successful_submission_publishes_order_submitted_event(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, OrderSubmittedEvent)
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order, "EXCH-1")

        await manager.submit_order(order)
        await asyncio.sleep(0)  # let the fire-and-forget publish task run

        assert len(received) == 1
        assert received[0].client_order_id == order.client_order_id
        assert received[0].symbol == "BTCUSDT"
        # order_type was missing from OrderSubmittedEvent's schema entirely
        # (trading_orders_submitted_total is labeled by order_type, but had
        # no way to populate that label from this event) — confirms it's
        # now threaded through from the domain Order.
        assert received[0].order_type == OrderType.MARKET

    async def test_successful_submission_publishes_status_changed_to_submitted_and_open(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, OrderStatusChangedEvent)
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order, "EXCH-1")

        await manager.submit_order(order)
        await asyncio.sleep(0)

        transitions = [(e.previous_status, e.new_status) for e in received]
        assert (OrderStatus.PENDING, OrderStatus.SUBMITTED) in transitions
        assert (OrderStatus.SUBMITTED, OrderStatus.OPEN) in transitions

    async def test_definite_failure_publishes_order_rejected_event(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, OrderRejectedEvent)
        order = make_order()
        exchange.place_order.side_effect = RuntimeError("insufficient margin")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)
        await asyncio.sleep(0)

        assert len(received) == 1
        assert "insufficient margin" in received[0].reason
        assert received[0].order_id == order.order_id

    async def test_ambiguous_failure_that_did_not_land_publishes_order_rejected_event(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, OrderRejectedEvent)
        order = make_order()
        exchange.place_order.side_effect = ExchangeTimeoutError("place_order", 10.0)
        exchange.get_order.side_effect = OrderNotFoundError(order.client_order_id, "BTCUSDT")

        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)
        await asyncio.sleep(0)

        assert len(received) == 1

    async def test_ambiguous_failure_that_landed_does_not_publish_order_rejected_event(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, OrderRejectedEvent)
        order = make_order()
        exchange.place_order.side_effect = ExchangeConnectionError("connection reset")
        exchange.get_order.return_value = make_exchanged_order(order)

        await manager.submit_order(order)
        await asyncio.sleep(0)

        assert received == []

    async def test_successful_cancel_publishes_order_cancelled_and_status_changed_events(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        cancelled_events = _recorder(isolated_event_bus, OrderCancelledEvent)
        status_events = _recorder(isolated_event_bus, OrderStatusChangedEvent)
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        await asyncio.sleep(0)
        status_events.clear()
        exchange.cancel_order.return_value = True

        await manager.cancel_order(order.order_id, order.symbol)
        await asyncio.sleep(0)

        assert len(cancelled_events) == 1
        assert cancelled_events[0].order_id == order.order_id
        assert any(
            e.previous_status == OrderStatus.OPEN and e.new_status == OrderStatus.CANCELLED
            for e in status_events
        )

    async def test_failed_exchange_cancellation_does_not_publish_cancelled_event(
        self, manager: OrderManager, exchange: AsyncMock, isolated_event_bus: EventBus
    ) -> None:
        cancelled_events = _recorder(isolated_event_bus, OrderCancelledEvent)
        order = make_order()
        exchange.place_order.return_value = make_exchanged_order(order)
        await manager.submit_order(order)
        exchange.cancel_order.return_value = False

        await manager.cancel_order(order.order_id, order.symbol)
        await asyncio.sleep(0)

        assert cancelled_events == []
