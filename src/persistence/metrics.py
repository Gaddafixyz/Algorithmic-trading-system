"""Performance metrics persistence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Float, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.persistence.database import Base
from src.persistence.repository import BaseRepository


class MetricSnapshotORM(Base):
    """Periodic performance metric snapshot."""

    __tablename__ = "metric_snapshots"
    __table_args__ = (Index("ix_metrics_timestamp", "snapshot_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    equity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    total_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(default=0)
    total_trades: Mapped[int] = mapped_column(default=0)


class MetricRepository(BaseRepository[MetricSnapshotORM]):
    def __init__(self, session: Any) -> None:
        super().__init__(session, MetricSnapshotORM)

    async def save_snapshot(self, snapshot: MetricSnapshotORM) -> MetricSnapshotORM:
        return await self.save(snapshot)
