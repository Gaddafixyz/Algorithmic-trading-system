"""Unit tests for src.execution.reconciliation.ReconciliationService.reconcile_pnl.

Separate file from test_reconciliation.py (which covers reconcile()'s
position/order sync) since reconcile_pnl is a distinct feature added in
this session with its own fixtures and failure modes.
"""

from __future__ import annotations

import time as time_module
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.enums import AlertSeverity, PositionSide
from src.core.events import AlertFiredEvent, EventBus, reset_event_bus, set_event_bus
from src.core.interfaces import IExchangeClient
from src.domain.trade import Trade
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.execution.reconciliation import ReconciliationService


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def make_trade(
    *,
    gross_pnl: str = "100",
    fees: str = "1",
    closed_at: datetime,
) -> Trade:
    return Trade(
        position_id="pos-1", symbol="BTCUSDT", side=PositionSide.LONG,
        quantity=Decimal("1"), entry_quantity=Decimal("1"),
        entry_price=Decimal("50000"), exit_price=Decimal("50100"),
        gross_pnl=Decimal(gross_pnl), fees=Decimal(fees),
        opened_at=closed_at - timedelta(hours=1), closed_at=closed_at,
    )


@pytest.fixture
def exchange() -> AsyncMock:
    mock = AsyncMock()
    mock.get_income_history = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def positions() -> AsyncMock:
    pm = AsyncMock(spec=PositionManager)
    pm.open_positions = []
    pm.closed_trades = []
    return pm


@pytest.fixture
def orders() -> AsyncMock:
    om = AsyncMock(spec=OrderManager)
    om.get_open_orders.return_value = []
    return om


@pytest.fixture
def service(exchange: AsyncMock, positions: AsyncMock, orders: AsyncMock) -> ReconciliationService:
    return ReconciliationService(exchange=exchange, position_manager=positions, order_manager=orders)


class TestUnsupportedExchange:
    async def test_skips_without_error_when_exchange_lacks_income_history(
        self, positions: AsyncMock, orders: AsyncMock,
    ) -> None:
        # spec=IExchangeClient excludes get_income_history (not part of
        # the interface — Binance-only extra), so hasattr() is False.
        bare_exchange = AsyncMock(spec=IExchangeClient)
        service = ReconciliationService(
            exchange=bare_exchange, position_manager=positions, order_manager=orders,
        )

        report = await service.reconcile_pnl()

        assert report["skipped"] == "unsupported_by_exchange"
        assert service.stats["pnl_reconcile_cycles"] == 0


class TestReconcilePnlWithinTolerance:
    async def test_no_alert_when_amounts_match(
        self, service: ReconciliationService, exchange: AsyncMock,
        positions: AsyncMock, isolated_event_bus: EventBus,
    ) -> None:
        now = datetime.now(tz=UTC)
        positions.closed_trades = [make_trade(gross_pnl="100", fees="1", closed_at=now)]
        exchange.get_income_history.return_value = [{"income": "99"}]  # matches 100-1

        received: list[AlertFiredEvent] = []
        isolated_event_bus.subscribe(AlertFiredEvent, lambda e: received.append(e))

        report = await service.reconcile_pnl()

        assert report["within_tolerance"] is True
        assert received == []
        assert service.stats["pnl_discrepancies_total"] == 0

    async def test_small_diff_within_default_tolerance_no_alert(
        self, service: ReconciliationService, exchange: AsyncMock,
        positions: AsyncMock, isolated_event_bus: EventBus,
    ) -> None:
        now = datetime.now(tz=UTC)
        positions.closed_trades = [make_trade(gross_pnl="100", fees="1", closed_at=now)]
        exchange.get_income_history.return_value = [{"income": "99.50"}]  # diff=0.50 < $1 floor

        received: list[AlertFiredEvent] = []
        isolated_event_bus.subscribe(AlertFiredEvent, lambda e: received.append(e))

        report = await service.reconcile_pnl()

        assert report["within_tolerance"] is True
        assert received == []


class TestReconcilePnlMismatch:
    async def test_publishes_alert_on_significant_mismatch(
        self, service: ReconciliationService, exchange: AsyncMock,
        positions: AsyncMock, isolated_event_bus: EventBus,
    ) -> None:
        now = datetime.now(tz=UTC)
        positions.closed_trades = [make_trade(gross_pnl="100", fees="1", closed_at=now)]
        exchange.get_income_history.return_value = [{"income": "50"}]  # diff=49, way over tolerance

        received: list[AlertFiredEvent] = []
        isolated_event_bus.subscribe(AlertFiredEvent, lambda e: received.append(e))

        report = await service.reconcile_pnl()

        assert report["within_tolerance"] is False
        assert len(received) == 1
        alert = received[0]
        assert alert.alert_name == "realized_pnl_mismatch"
        assert alert.severity == AlertSeverity.WARNING
        assert service.stats["pnl_discrepancies_total"] == 1

    async def test_excludes_trades_outside_the_window(
        self, service: ReconciliationService, exchange: AsyncMock, positions: AsyncMock,
    ) -> None:
        now = datetime.now(tz=UTC)
        old_trade = make_trade(gross_pnl="500", fees="0", closed_at=now - timedelta(hours=48))
        recent_trade = make_trade(gross_pnl="100", fees="1", closed_at=now)
        positions.closed_trades = [old_trade, recent_trade]
        exchange.get_income_history.return_value = [{"income": "99"}]  # matches only recent_trade

        report = await service.reconcile_pnl()

        assert report["local_realized_pnl"] == "99"
        assert report["within_tolerance"] is True


class TestReconcilePnlFetchFailure:
    async def test_does_not_raise_and_does_not_advance_watermark(
        self, service: ReconciliationService, exchange: AsyncMock,
    ) -> None:
        exchange.get_income_history.side_effect = ConnectionError("network blip")

        report = await service.reconcile_pnl()  # must not raise

        assert report["skipped"] is None
        assert "within_tolerance" not in report
        assert service.stats["pnl_reconcile_failures"] == 1


class TestReconcilePnlWatermark:
    async def test_second_call_queries_from_first_calls_end_time(
        self, service: ReconciliationService, exchange: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t0 = 1_700_000_000.0
        monkeypatch.setattr(time_module, "time", lambda: t0)
        await service.reconcile_pnl()
        first_call_end_ms = exchange.get_income_history.call_args.kwargs["end_time"]

        t1 = t0 + 120.0
        monkeypatch.setattr(time_module, "time", lambda: t1)
        await service.reconcile_pnl()
        second_call_start_ms = exchange.get_income_history.call_args.kwargs["start_time"]

        assert second_call_start_ms == first_call_end_ms

    async def test_first_call_looks_back_default_window(
        self, service: ReconciliationService, exchange: AsyncMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.core.constants import PNL_RECONCILE_LOOKBACK_SECONDS

        t0 = 1_700_000_000.0
        monkeypatch.setattr(time_module, "time", lambda: t0)

        await service.reconcile_pnl()

        call_kwargs = exchange.get_income_history.call_args.kwargs
        expected_start_ms = int(t0 * 1000) - int(PNL_RECONCILE_LOOKBACK_SECONDS * 1000)
        assert call_kwargs["start_time"] == expected_start_ms
