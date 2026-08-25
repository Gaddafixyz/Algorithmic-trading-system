"""Unit tests for execution layer components."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.enums import OrderSide, OrderStatus, OrderType, PositionSide
from src.core.exceptions import DuplicateOrderError, OrderSubmissionError
from src.domain.order import Fill, Order
from src.domain.position import Position
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.execution.fill_engine import FillEngine


def make_order(**kwargs) -> Order:
    defaults = dict(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.1"),
    )
    defaults.update(kwargs)
    return Order(**defaults)


def make_position(**kwargs) -> Position:
    defaults = dict(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )
    defaults.update(kwargs)
    return Position(**defaults)


class TestOrderManager:
    def _make_manager(self) -> tuple[OrderManager, AsyncMock]:
        mock_exchange = AsyncMock()
        # place_order returns a filled Order
        submitted_order = make_order()
        submitted_order.transition_to(OrderStatus.SUBMITTED)
        submitted_order.transition_to(OrderStatus.OPEN)
        submitted_order.exchange_order_id = "EX123"
        mock_exchange.place_order.return_value = submitted_order
        mock_exchange.cancel_order.return_value = True
        return OrderManager(mock_exchange), mock_exchange

    @pytest.mark.asyncio
    async def test_submit_order_success(self):
        manager, mock_exchange = self._make_manager()
        order = make_order()
        result = await manager.submit_order(order)
        assert result.status in (OrderStatus.OPEN, OrderStatus.SUBMITTED)
        mock_exchange.place_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_submission_raises(self):
        manager, _ = self._make_manager()
        order = make_order()
        await manager.submit_order(order)
        with pytest.raises(DuplicateOrderError):
            await manager.submit_order(order)

    @pytest.mark.asyncio
    async def test_exchange_failure_raises_submission_error(self):
        mock_exchange = AsyncMock()
        mock_exchange.place_order.side_effect = Exception("exchange error")
        manager = OrderManager(mock_exchange)
        order = make_order()
        with pytest.raises(OrderSubmissionError):
            await manager.submit_order(order)

    @pytest.mark.asyncio
    async def test_cancel_open_order(self):
        manager, mock_exchange = self._make_manager()
        order = make_order()
        await manager.submit_order(order)
        result = await manager.cancel_order(order.order_id, order.symbol)
        assert result is True
        mock_exchange.cancel_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_terminal_order_returns_false(self):
        manager, _ = self._make_manager()
        order = make_order()
        await manager.submit_order(order)
        # Force order to terminal state
        manager._orders[order.order_id].status = OrderStatus.FILLED
        result = await manager.cancel_order(order.order_id, order.symbol)
        assert result is False

    @pytest.mark.asyncio
    async def test_get_order(self):
        manager, _ = self._make_manager()
        order = make_order()
        await manager.submit_order(order)
        retrieved = await manager.get_order(order.order_id)
        assert retrieved is not None
        assert retrieved.order_id == order.order_id

    @pytest.mark.asyncio
    async def test_get_open_orders(self):
        manager, _ = self._make_manager()
        o1 = make_order(symbol="BTCUSDT")
        o2 = make_order(symbol="ETHUSDT")
        await manager.submit_order(o1)
        await manager.submit_order(o2)
        open_orders = manager.get_open_orders()
        assert len(open_orders) == 2

    @pytest.mark.asyncio
    async def test_get_open_orders_filtered_by_symbol(self):
        manager, _ = self._make_manager()
        o1 = make_order(symbol="BTCUSDT")
        o2 = make_order(symbol="ETHUSDT")
        await manager.submit_order(o1)
        await manager.submit_order(o2)
        btc_orders = manager.get_open_orders("BTCUSDT")
        assert all(o.symbol == "BTCUSDT" for o in btc_orders)


class TestPositionManager:
    @pytest.mark.asyncio
    async def test_register_and_get(self):
        pm = PositionManager()
        pos = make_position()
        pm.register(pos)
        retrieved = pm.get(pos.position_id)
        assert retrieved is not None
        assert retrieved.position_id == pos.position_id

    @pytest.mark.asyncio
    async def test_close_position_creates_trade(self):
        pm = PositionManager()
        pos = make_position(
            entry_price=Decimal("50000"),
            quantity=Decimal("1.0"),
            entry_quantity=Decimal("1.0"),
        )
        pm.register(pos)
        trade = await pm.close_position(
            pos.position_id,
            exit_price=Decimal("52000"),
            fee=Decimal("10"),
        )
        assert trade is not None
        assert trade.exit_price == Decimal("52000")
        assert trade.symbol == "BTCUSDT"
        assert len(pm.closed_trades) == 1

    @pytest.mark.asyncio
    async def test_partial_close_does_not_create_trade(self):
        pm = PositionManager()
        pos = make_position(quantity=Decimal("2.0"), entry_quantity=Decimal("2.0"))
        pm.register(pos)
        trade = await pm.close_position(
            pos.position_id,
            exit_price=Decimal("51000"),
            qty=Decimal("1.0"),
            fee=Decimal("5"),
        )
        assert trade is None
        assert len(pm.closed_trades) == 0

    def test_update_mark_price(self):
        pm = PositionManager()
        pos = make_position(mark_price=Decimal("50000"))
        pm.register(pos)
        pm.update_mark_price("BTCUSDT", Decimal("51000"))
        updated = pm.get(pos.position_id)
        assert updated.mark_price == Decimal("51000")

    def test_get_by_symbol(self):
        pm = PositionManager()
        p1 = make_position(symbol="BTCUSDT")
        p2 = make_position(symbol="ETHUSDT")
        pm.register(p1)
        pm.register(p2)
        btc_positions = pm.get_by_symbol("BTCUSDT")
        assert len(btc_positions) == 1
        assert btc_positions[0].symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_stats(self):
        pm = PositionManager()
        pos = make_position(entry_price=Decimal("50000"), quantity=Decimal("1.0"), entry_quantity=Decimal("1.0"))
        pm.register(pos)
        await pm.close_position(pos.position_id, Decimal("51000"), fee=Decimal("0"))
        stats = pm.stats
        assert stats["open_positions"] == 0
        assert stats["closed_trades"] == 1


class TestFillEngine:
    @pytest.mark.asyncio
    async def test_process_execution_report(self):
        mock_order_manager = AsyncMock()
        order = make_order()
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        mock_order_manager.get_order_by_client_id.return_value = order

        engine = FillEngine(mock_order_manager)
        msg = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "c": order.client_order_id,
                "l": "0.1",      # last filled qty
                "L": "50000",    # last fill price
                "n": "2.0",      # fee
                "N": "USDT",
                "m": False,
            },
        }
        await engine.process_execution_report(msg)
        assert engine._fills_processed == 1

    @pytest.mark.asyncio
    async def test_ignore_non_trade_update(self):
        mock_order_manager = AsyncMock()
        engine = FillEngine(mock_order_manager)
        await engine.process_execution_report({"e": "ACCOUNT_UPDATE"})
        assert engine._fills_processed == 0
        mock_order_manager.get_order_by_client_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_order_handled_gracefully(self):
        mock_order_manager = AsyncMock()
        mock_order_manager.get_order_by_client_id.return_value = None
        engine = FillEngine(mock_order_manager)
        msg = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "c": "unknown_client_id",
                "l": "0.1",
                "L": "50000",
                "n": "2.0",
                "N": "USDT",
            },
        }
        # Should not raise
        await engine.process_execution_report(msg)
        assert engine._fills_processed == 0
