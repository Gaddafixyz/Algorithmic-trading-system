"""Tests for src.persistence.orders.OrderRepository."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.enums import OrderSide, OrderStatus, OrderType
from src.domain.order import Fill, Order
from src.persistence.orders import OrderRepository


def make_domain_order(symbol: str = "BTCUSDT", quantity: str = "0.1") -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
    )


class TestSaveOrder:
    async def test_persists_all_mapped_fields(self, db_session: AsyncSession) -> None:
        repo = OrderRepository(db_session)
        order = make_domain_order()
        result = await repo.save_order(order)

        assert result.order_id == order.order_id
        assert result.client_order_id == order.client_order_id
        assert result.symbol == "BTCUSDT"
        assert result.side == "buy"
        assert result.order_type == "market"
        assert result.status == "pending"
        assert result.quantity == Decimal("0.1")

    async def test_repeated_saves_across_status_transitions_update_the_same_row(
        self, db_session: AsyncSession
    ) -> None:
        repo = OrderRepository(db_session)
        order = make_domain_order()
        await repo.save_order(order)
        await db_session.commit()

        order.transition_to(OrderStatus.SUBMITTED)
        await repo.save_order(order)
        order.transition_to(OrderStatus.OPEN)
        await repo.save_order(order)

        assert await repo.count() == 1
        found = await repo.find_by_id(order.order_id)
        assert found.status == "open"

    async def test_fill_data_is_persisted_after_a_fill(self, db_session: AsyncSession) -> None:
        repo = OrderRepository(db_session)
        order = make_domain_order()
        await repo.save_order(order)
        await db_session.commit()

        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.add_fill(
            Fill(
                fill_id="f1",
                price=Decimal("50000"),
                quantity=Decimal("0.1"),
                fee=Decimal("2"),
                fee_asset="USDT",
            )
        )
        await repo.save_order(order)

        found = await repo.find_by_id(order.order_id)
        assert found.status == "filled"
        assert found.filled_quantity == Decimal("0.1")
        assert found.average_fill_price == Decimal("50000")
        assert found.total_fee == Decimal("2")

    async def test_nullable_fields_default_to_none(self, db_session: AsyncSession) -> None:
        repo = OrderRepository(db_session)
        order = make_domain_order()
        result = await repo.save_order(order)
        assert result.exchange_order_id is None
        assert result.price is None
        assert result.signal_id is None
