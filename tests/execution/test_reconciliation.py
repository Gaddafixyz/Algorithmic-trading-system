"""Unit tests for src.execution.reconciliation.ReconciliationService."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.enums import AlertSeverity, OrderSide, OrderStatus, OrderType, PositionSide
from src.core.events import (
    AlertFiredEvent,
    EventBus,
    OrderFilledEvent,
    reset_event_bus,
    set_event_bus,
)
from src.domain.order import Order
from src.domain.position import Position
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.execution.reconciliation import ReconciliationService


@dataclass
class FakeExchangePosition:
    """Minimal stand-in for whatever BinanceFuturesRESTClient.get_positions() returns."""

    symbol: str
    quantity: Decimal
    mark_price: Decimal


def make_position(
    symbol: str = "BTCUSDT",
    quantity: str = "0.1",
    mark_price: str = "50000",
) -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal(quantity),
        entry_quantity=Decimal(quantity),
        entry_price=Decimal(mark_price),
        mark_price=Decimal(mark_price),
    )


def make_order(
    symbol: str = "BTCUSDT",
    quantity: str = "0.1",
    status: OrderStatus = OrderStatus.OPEN,
    client_order_id: str | None = None,
) -> Order:
    kwargs: dict = dict(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
    )
    if client_order_id is not None:
        kwargs["client_order_id"] = client_order_id
    order = Order(**kwargs)
    if status != OrderStatus.PENDING:
        order.transition_to(OrderStatus.SUBMITTED)
        if status != OrderStatus.SUBMITTED:
            order.transition_to(status)
    return order


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


@pytest.fixture
def exchange() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def positions() -> AsyncMock:
    pm = AsyncMock(spec=PositionManager)
    pm.open_positions = []
    return pm


@pytest.fixture
def orders() -> AsyncMock:
    om = AsyncMock(spec=OrderManager)
    om.get_open_orders.return_value = []
    return om


@pytest.fixture
def service(exchange: AsyncMock, positions: AsyncMock, orders: AsyncMock) -> ReconciliationService:
    return ReconciliationService(
        exchange=exchange, position_manager=positions, order_manager=orders
    )


class TestReconcileFetchFailure:
    async def test_aborts_the_cycle_without_raising_when_fetch_fails(
        self, service: ReconciliationService, exchange: AsyncMock
    ) -> None:
        exchange.get_positions.side_effect = ConnectionError("network blip")

        report = await service.reconcile()  # must not raise

        assert report["position_discrepancies"] == []
        assert report["orphan_positions"] == []
        assert service.stats["reconcile_failures"] == 1

    async def test_does_not_increment_discrepancies_on_fetch_failure(
        self, service: ReconciliationService, exchange: AsyncMock
    ) -> None:
        exchange.get_positions.side_effect = ConnectionError("network blip")
        await service.reconcile()
        assert service.stats["discrepancies_total"] == 0

    async def test_subsequent_cycle_can_still_succeed_after_a_failure(
        self, service: ReconciliationService, exchange: AsyncMock
    ) -> None:
        exchange.get_positions.side_effect = [ConnectionError("blip"), []]
        await service.reconcile()
        report = await service.reconcile()
        assert service.stats["reconcile_cycles"] == 2
        assert report["position_discrepancies"] == []


class TestReconcilePositionVanishedFromExchange:
    async def test_force_closes_the_local_position_at_mark_price(
        self,
        service: ReconciliationService,
        exchange: AsyncMock,
        positions: AsyncMock,
    ) -> None:
        local_pos = make_position(mark_price="51000")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = []  # exchange has nothing

        report = await service.reconcile()

        positions.close_position.assert_awaited_once_with(
            local_pos.position_id, exit_price=Decimal("51000")
        )
        assert len(report["position_discrepancies"]) == 1
        assert report["position_discrepancies"][0]["issue"] == "not_on_exchange"

    async def test_fires_a_critical_alert_for_the_vanished_position(
        self,
        service: ReconciliationService,
        exchange: AsyncMock,
        positions: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        alerts: list[AlertFiredEvent] = []

        async def _capture(event: AlertFiredEvent) -> None:
            alerts.append(event)

        isolated_event_bus.subscribe(AlertFiredEvent, _capture)

        local_pos = make_position()
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = []

        await service.reconcile()

        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.CRITICAL
        assert alerts[0].alert_name == "position_closed_externally"

    async def test_increments_discrepancies_found(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        positions.open_positions = [make_position()]
        exchange.get_positions.return_value = []
        await service.reconcile()
        assert service.stats["discrepancies_total"] == 1

    async def test_continues_the_cycle_even_if_force_close_itself_fails(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        positions.open_positions = [make_position()]
        positions.close_position.side_effect = RuntimeError("close failed")
        exchange.get_positions.return_value = []

        report = await service.reconcile()  # must not raise
        assert len(report["position_discrepancies"]) == 1


class TestReconcilePositionQuantityMismatch:
    async def test_detects_a_quantity_mismatch_beyond_tolerance(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        local_pos = make_position(quantity="0.1")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = [
            FakeExchangePosition(
                symbol="BTCUSDT", quantity=Decimal("0.2"), mark_price=Decimal("50000")
            )
        ]

        report = await service.reconcile()

        assert len(report["position_discrepancies"]) == 1
        assert report["position_discrepancies"][0]["issue"] == "qty_mismatch"

    async def test_updates_the_local_mark_price_from_the_exchange(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        local_pos = make_position(quantity="0.1", mark_price="50000")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = [
            FakeExchangePosition(
                symbol="BTCUSDT", quantity=Decimal("0.1"), mark_price=Decimal("52000")
            )
        ]

        await service.reconcile()
        assert local_pos.mark_price == Decimal("52000")

    async def test_tiny_quantity_difference_within_tolerance_is_not_a_discrepancy(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        # Difference of 0.00005 is below the 0.0001 tolerance threshold.
        local_pos = make_position(quantity="0.10000")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = [
            FakeExchangePosition(
                symbol="BTCUSDT", quantity=Decimal("0.10005"), mark_price=Decimal("50000")
            )
        ]

        report = await service.reconcile()
        assert report["position_discrepancies"] == []

    async def test_matching_quantities_produce_no_discrepancy(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        local_pos = make_position(quantity="0.1")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = [
            FakeExchangePosition(
                symbol="BTCUSDT", quantity=Decimal("0.1"), mark_price=Decimal("50000")
            )
        ]

        report = await service.reconcile()
        assert report["position_discrepancies"] == []
        assert service.stats["discrepancies_total"] == 0


class TestReconcileOrphanExchangePositions:
    async def test_detects_an_exchange_position_with_no_local_record(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        positions.open_positions = []  # nothing tracked locally
        exchange.get_positions.return_value = [
            FakeExchangePosition(symbol="SOLUSDT", quantity=Decimal("5"), mark_price=Decimal("100"))
        ]

        report = await service.reconcile()

        assert len(report["orphan_positions"]) == 1
        assert report["orphan_positions"][0]["symbol"] == "SOLUSDT"

    async def test_fires_a_critical_alert_for_an_orphan_position(
        self,
        service: ReconciliationService,
        exchange: AsyncMock,
        positions: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        alerts: list[AlertFiredEvent] = []

        async def _capture(event: AlertFiredEvent) -> None:
            alerts.append(event)

        isolated_event_bus.subscribe(AlertFiredEvent, _capture)

        positions.open_positions = []
        exchange.get_positions.return_value = [
            FakeExchangePosition(symbol="SOLUSDT", quantity=Decimal("5"), mark_price=Decimal("100"))
        ]

        await service.reconcile()

        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.CRITICAL
        assert alerts[0].alert_name == "orphan_exchange_position"

    async def test_a_matched_symbol_is_not_also_reported_as_an_orphan(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        local_pos = make_position(symbol="BTCUSDT")
        positions.open_positions = [local_pos]
        exchange.get_positions.return_value = [
            FakeExchangePosition(
                symbol="BTCUSDT", quantity=Decimal("0.1"), mark_price=Decimal("50000")
            )
        ]

        report = await service.reconcile()
        assert report["orphan_positions"] == []


class TestReconcileOrderSync:
    async def test_order_check_failure_is_logged_and_skipped(
        self, service: ReconciliationService, exchange: AsyncMock, orders: AsyncMock
    ) -> None:
        local_order = make_order()
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []
        exchange.get_order.side_effect = RuntimeError("exchange unreachable")

        report = await service.reconcile()  # must not raise
        assert report["order_updates"] == []

    async def test_matching_status_produces_no_update(
        self, service: ReconciliationService, exchange: AsyncMock, orders: AsyncMock
    ) -> None:
        local_order = make_order(status=OrderStatus.OPEN)
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []

        exch_order = make_order(status=OrderStatus.OPEN)
        exchange.get_order.return_value = exch_order

        report = await service.reconcile()
        assert report["order_updates"] == []

    async def test_status_divergence_updates_the_local_order(
        self, service: ReconciliationService, exchange: AsyncMock, orders: AsyncMock
    ) -> None:
        local_order = make_order(status=OrderStatus.OPEN)
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []

        exch_order = make_order(status=OrderStatus.CANCELLED)
        exchange.get_order.return_value = exch_order

        report = await service.reconcile()

        assert len(report["order_updates"]) == 1
        assert local_order.status == OrderStatus.CANCELLED

    async def test_a_missed_fill_discovered_via_reconciliation_publishes_order_filled_event(
        self,
        service: ReconciliationService,
        exchange: AsyncMock,
        orders: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        # This is the recovery path for a fill that happened while the
        # user-data WebSocket was down: reconciliation must still
        # trigger the normal fill pipeline (position creation, etc.)
        # rather than leaving a filled order with no position ever
        # created for it.
        fill_events: list[OrderFilledEvent] = []

        async def _capture(event: OrderFilledEvent) -> None:
            fill_events.append(event)

        isolated_event_bus.subscribe(OrderFilledEvent, _capture)

        local_order = make_order(status=OrderStatus.OPEN)
        assert local_order.filled_quantity == Decimal("0")
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []

        exch_order = make_order(status=OrderStatus.OPEN)
        exch_order.transition_to(OrderStatus.PARTIALLY_FILLED)
        exch_order.filled_quantity = Decimal("0.1")
        exch_order.average_fill_price = Decimal("50000")
        exch_order.transition_to(OrderStatus.FILLED)
        exchange.get_order.return_value = exch_order

        await service.reconcile()

        assert len(fill_events) == 1
        assert fill_events[0].symbol == "BTCUSDT"
        assert fill_events[0].filled_quantity == Decimal("0.1")

    async def test_does_not_republish_order_filled_for_an_order_already_partially_filled_locally(
        self,
        service: ReconciliationService,
        exchange: AsyncMock,
        orders: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        fill_events: list[OrderFilledEvent] = []

        async def _capture(event: OrderFilledEvent) -> None:
            fill_events.append(event)

        isolated_event_bus.subscribe(OrderFilledEvent, _capture)

        local_order = make_order(status=OrderStatus.OPEN)
        local_order.filled_quantity = Decimal("0.05")  # was_unfilled == False
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []

        exch_order = make_order(status=OrderStatus.OPEN)
        exch_order.filled_quantity = Decimal("0.1")
        exch_order.average_fill_price = Decimal("50000")
        exch_order.transition_to(OrderStatus.PARTIALLY_FILLED)
        exchange.get_order.return_value = exch_order

        await service.reconcile()
        assert fill_events == []

    async def test_syncs_fill_data_even_when_the_local_transition_is_invalid(
        self, service: ReconciliationService, exchange: AsyncMock, orders: AsyncMock
    ) -> None:
        # Exchange and local state machines can disagree on the exact
        # path; the fill data (qty/price/fee) must still be applied
        # even if transition_to() raises, by forcing the status field
        # directly as a fallback.
        local_order = make_order(
            status=OrderStatus.FILLED
        )  # terminal: no further transitions valid
        orders.get_open_orders.return_value = [local_order]
        exchange.get_positions.return_value = []

        exch_order = make_order(status=OrderStatus.OPEN)
        exch_order.transition_to(OrderStatus.CANCELLED)
        exch_order.filled_quantity = Decimal("0.1")
        exch_order.average_fill_price = Decimal("50000")
        exchange.get_order.return_value = exch_order

        await service.reconcile()  # must not raise despite the invalid transition

        assert local_order.status == OrderStatus.CANCELLED
        assert local_order.filled_quantity == Decimal("0.1")


class TestStats:
    async def test_stats_reflects_cycle_count_and_totals(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock
    ) -> None:
        positions.open_positions = [make_position()]
        exchange.get_positions.return_value = []  # vanished -> 1 discrepancy

        await service.reconcile()
        stats = service.stats
        assert stats["reconcile_cycles"] == 1
        assert stats["discrepancies_total"] == 1
        assert stats["reconcile_failures"] == 0
