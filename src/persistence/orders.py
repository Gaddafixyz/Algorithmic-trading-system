"""Order persistence ORM model and repository."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, String, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.database import Base
from src.persistence.repository import BaseRepository
from src.domain.order import Order as DomainOrder


class OrderORM(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_symbol", "symbol"),
        Index("ix_orders_created_at", "created_at"),
    )

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    total_fee: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderRepository(BaseRepository[OrderORM]):
    def __init__(self, session: Any) -> None:
        super().__init__(session, OrderORM)

    async def save_order(self, order: DomainOrder) -> OrderORM:
        """
        Persist an order's current state.

        Orders are saved repeatedly across their lifecycle as status
        transitions occur (SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED),
        all sharing the same order_id primary key. Using ``upsert`` here
        (rather than always constructing a fresh row and inserting) avoids a
        primary-key violation on every update after the first.
        """
        values = {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "status": order.status.value,
            "quantity": order.quantity,
            "filled_quantity": order.filled_quantity,
            "price": order.price,
            "stop_price": order.stop_price,
            "average_fill_price": order.average_fill_price,
            "total_fee": order.total_fee,
            "reduce_only": order.reduce_only,
            "signal_id": order.signal_id,
            "strategy_id": order.strategy_id,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
        }
        return await self.upsert(order.order_id, values)
