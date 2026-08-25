"""Signal persistence ORM model and repository."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, String, Float
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.database import Base
from src.persistence.repository import BaseRepository
from src.domain.signal import Signal as DomainSignal


class SignalORM(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_symbol_generated", "symbol", "generated_at"),)

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    strength: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    atr: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    market_structure: Mapped[str] = mapped_column(String(20), default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SignalRepository(BaseRepository[SignalORM]):
    def __init__(self, session: Any) -> None:
        super().__init__(session, SignalORM)

    async def save_signal(self, signal: DomainSignal) -> SignalORM:
        orm = SignalORM(
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            timeframe=signal.timeframe.value,
            direction=signal.direction.value,
            score=signal.score,
            strength=signal.strength.value,
            confidence=signal.confidence,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            atr=signal.atr,
            market_structure=signal.market_structure,
            generated_at=signal.generated_at,
        )
        return await self.save(orm)
