"""Tests for src.persistence.metrics.MetricRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.persistence.metrics import MetricRepository, MetricSnapshotORM


def make_snapshot(strategy_id: str = "strat-1", equity: str = "10000") -> MetricSnapshotORM:
    return MetricSnapshotORM(
        snapshot_at=datetime.now(UTC),
        strategy_id=strategy_id,
        equity=Decimal(equity),
        daily_pnl=Decimal("100"),
        total_pnl=Decimal("500"),
        drawdown_pct=5.0,
        win_rate=0.6,
        open_positions=2,
        total_trades=10,
    )


class TestSaveSnapshot:
    async def test_persists_a_snapshot(self, db_session: AsyncSession) -> None:
        repo = MetricRepository(db_session)
        result = await repo.save_snapshot(make_snapshot())

        assert result.strategy_id == "strat-1"
        assert result.equity == Decimal("10000")
        assert result.win_rate == 0.6

    async def test_id_is_autoincremented(self, db_session: AsyncSession) -> None:
        repo = MetricRepository(db_session)
        first = await repo.save_snapshot(make_snapshot())
        await db_session.commit()
        second = await repo.save_snapshot(make_snapshot())

        assert first.id is not None
        assert second.id is not None
        assert first.id != second.id

    async def test_multiple_snapshots_for_the_same_strategy_are_all_kept(
        self, db_session: AsyncSession
    ) -> None:
        repo = MetricRepository(db_session)
        await repo.save_snapshot(make_snapshot(strategy_id="strat-a"))
        await repo.save_snapshot(make_snapshot(strategy_id="strat-a"))
        assert await repo.count(strategy_id="strat-a") == 2
