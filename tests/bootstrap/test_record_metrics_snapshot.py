"""Unit tests for Application.record_metrics_snapshot.

Direct coverage of the method's own behavior (Prometheus gauge sets +
best-effort MetricRepository persistence). Its scheduling (task name,
interval) is covered separately in test_startup.py's
test_schedules_all_documented_periodic_tasks.

Flagged in [1.3.21]'s CHANGELOG as a test gap alongside
check_portfolio_rebalance (see test_check_portfolio_rebalance.py) —
both previously had only indirect coverage via their scheduling.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bootstrap.app import Application
from src.core.enums import PositionSide
from src.domain.trade import Trade
from src.monitoring.metrics import account_daily_pnl, account_drawdown_pct, account_equity


def make_trade(*, net_pnl_positive: bool = True) -> Trade:
    gross_pnl = Decimal("100") if net_pnl_positive else Decimal("-50")
    return Trade(
        position_id="pos-1",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        entry_quantity=Decimal("1"),
        entry_price=Decimal("50000"),
        exit_price=Decimal("50100"),
        gross_pnl=gross_pnl,
        fees=Decimal("0"),
        opened_at=datetime.now(tz=UTC),
        closed_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def app() -> Application:
    instance = Application.__new__(Application)
    instance.risk_manager = MagicMock()
    instance.risk_manager.equity = Decimal("10000")
    instance.risk_manager.daily_pnl = Decimal("150")
    instance.risk_manager.stats = {"drawdown_pct": Decimal("0.05")}
    instance.position_manager = MagicMock()
    instance.position_manager.closed_trades = []
    instance.position_manager.open_positions = []
    instance.db = MagicMock()
    instance.db.is_connected = False
    return instance


class TestRecordMetricsSnapshot:
    async def test_sets_prometheus_gauges(self, app: Application) -> None:
        await app.record_metrics_snapshot()
        assert account_equity._value.get() == 10000.0  # noqa: SLF001
        assert account_daily_pnl._value.get() == 150.0  # noqa: SLF001
        assert account_drawdown_pct._value.get() == 0.05  # noqa: SLF001

    async def test_skips_persistence_when_db_not_connected(self, app: Application) -> None:
        app.db.is_connected = False
        app.db.session = MagicMock()  # would raise/record a call if touched

        await app.record_metrics_snapshot()

        app.db.session.assert_not_called()

    async def test_persists_snapshot_when_db_connected(self, app: Application, monkeypatch: pytest.MonkeyPatch) -> None:
        app.db.is_connected = True
        fake_session = MagicMock()

        @asynccontextmanager
        async def fake_session_cm():
            yield fake_session

        app.db.session = fake_session_cm

        saved: list[object] = []

        class FakeRepo:
            def __init__(self, session: object) -> None:
                assert session is fake_session

            async def save_snapshot(self, snapshot: object) -> object:
                saved.append(snapshot)
                return snapshot

        monkeypatch.setattr("src.persistence.metrics.MetricRepository", FakeRepo)

        app.position_manager.closed_trades = [
            make_trade(net_pnl_positive=True),
            make_trade(net_pnl_positive=False),
        ]
        app.position_manager.open_positions = [MagicMock()]

        await app.record_metrics_snapshot()

        assert len(saved) == 1
        snapshot = saved[0]
        assert snapshot.strategy_id == "__account__"
        assert snapshot.equity == Decimal("10000")
        assert snapshot.daily_pnl == Decimal("150")
        assert snapshot.total_pnl == Decimal("50")  # 100 - 50 across the two trades
        assert snapshot.win_rate == 0.5
        assert snapshot.total_trades == 2
        assert snapshot.open_positions == 1

    async def test_gauge_exception_does_not_prevent_persistence_attempt(
        self, app: Application, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # risk_manager.stats missing the expected key -> gauge block raises,
        # is caught, and persistence should still be attempted afterward.
        app.risk_manager.stats = {}
        app.db.is_connected = True
        fake_session = MagicMock()

        @asynccontextmanager
        async def fake_session_cm():
            yield fake_session

        app.db.session = fake_session_cm

        called = {"save": False}

        class FakeRepo:
            def __init__(self, session: object) -> None:
                pass

            async def save_snapshot(self, snapshot: object) -> object:
                called["save"] = True
                return snapshot

        monkeypatch.setattr("src.persistence.metrics.MetricRepository", FakeRepo)

        await app.record_metrics_snapshot()  # should not raise

        assert called["save"] is True

    async def test_persistence_exception_is_swallowed(
        self, app: Application, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        app.db.is_connected = True

        @asynccontextmanager
        async def raising_session_cm():
            raise RuntimeError("db down")
            yield  # pragma: no cover - unreachable, satisfies generator shape

        app.db.session = raising_session_cm

        await app.record_metrics_snapshot()  # should not raise
