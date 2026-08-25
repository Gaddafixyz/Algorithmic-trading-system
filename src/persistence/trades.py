"""Trade persistence: ORM model and repository."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, String, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.database import Base
from src.persistence.repository import BaseRepository
from src.domain.trade import Trade as DomainTrade


class TradeORM(Base):
    """SQLAlchemy ORM model for completed trades."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_symbol", "symbol"),
        Index("ix_trades_opened_at", "opened_at"),
        Index("ix_trades_strategy_id", "strategy_id"),
    )

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    strategy_id: Mapped[str | None] = mapped_column(String(64))

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    timeframe: Mapped[str | None] = mapped_column(String(10))

    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)

    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    funding_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))

    initial_risk: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    leverage: Mapped[int] = mapped_column(Integer, default=1)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Numeric(15, 2), default=0)


class TradeRepository(BaseRepository[TradeORM]):
    """Repository for Trade persistence."""

    def __init__(self, session: Any) -> None:
        super().__init__(session, TradeORM)

    async def save_trade(self, trade: DomainTrade) -> TradeORM:
        """Convert domain Trade to ORM and persist."""
        orm = TradeORM(
            trade_id=trade.trade_id,
            position_id=trade.position_id,
            signal_id=trade.signal_id,
            strategy_id=trade.strategy_id,
            symbol=trade.symbol,
            side=trade.side.value,
            timeframe=trade.timeframe.value if trade.timeframe else None,
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            gross_pnl=trade.gross_pnl,
            fees=trade.fees,
            funding_pnl=trade.funding_pnl,
            initial_risk=trade.initial_risk,
            leverage=trade.leverage,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            duration_seconds=trade.duration_seconds,
        )
        return await self.save(orm)

    async def find_by_strategy(self, strategy_id: str) -> list[TradeORM]:
        return await self.find_all(strategy_id=strategy_id)

    async def find_by_symbol(self, symbol: str) -> list[TradeORM]:
        return await self.find_all(symbol=symbol)
