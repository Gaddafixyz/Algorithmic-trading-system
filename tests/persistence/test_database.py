"""Tests for src.persistence.database.Database."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

import src.persistence.database as database_module
from src.core.config import DatabaseConfig
from src.persistence.database import Database, get_database


def make_config() -> DatabaseConfig:
    # Explicit construction avoids picking up real DATABASE_* env vars
    # that might be set in the environment (BaseSettings reads them by
    # default), and points at an in-memory SQLite DB for fast, isolated
    # tests rather than requiring a real Postgres instance.
    return DatabaseConfig(url="sqlite+aiosqlite:///:memory:")


class TestConnectDisconnect:
    async def test_is_connected_false_before_connect(self) -> None:
        db = Database(make_config())
        assert db.is_connected is False

    async def test_is_connected_true_after_connect(self) -> None:
        db = Database(make_config())
        await db.connect()
        assert db.is_connected is True
        await db.disconnect()

    async def test_is_connected_false_after_disconnect(self) -> None:
        db = Database(make_config())
        await db.connect()
        await db.disconnect()
        assert db.is_connected is False

    async def test_disconnect_before_connect_does_not_raise(self) -> None:
        db = Database(make_config())
        await db.disconnect()  # must not raise
        assert db.is_connected is False


class TestCreateTables:
    async def test_raises_runtime_error_if_called_before_connect(self) -> None:
        db = Database(make_config())
        with pytest.raises(RuntimeError, match="not connected"):
            await db.create_tables()

    async def test_creates_tables_after_connect(self) -> None:
        db = Database(make_config())
        await db.connect()
        await db.create_tables()

        # Verify against real SQL that at least one known table exists.
        async with db.session() as session:
            result = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
            )
            assert result.scalar() == "orders"

        await db.disconnect()


class TestSessionContextManager:
    async def test_raises_runtime_error_if_called_before_connect(self) -> None:
        db = Database(make_config())
        with pytest.raises(RuntimeError, match="not connected"):
            async with db.session():
                pass

    async def test_commits_on_success(self) -> None:
        db = Database(make_config())
        await db.connect()
        await db.create_tables()

        from src.persistence.orders import OrderORM
        from datetime import UTC, datetime

        async with db.session() as session:
            session.add(OrderORM(
                order_id="o1", client_order_id="c1", symbol="BTCUSDT", side="BUY",
                order_type="MARKET", status="OPEN", quantity=Decimal("0.1"),
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            ))

        # A fresh session should see the committed row.
        async with db.session() as verify_session:
            result = await verify_session.get(OrderORM, "o1")
            assert result is not None

        await db.disconnect()

    async def test_rolls_back_on_exception_and_propagates_it(self) -> None:
        db = Database(make_config())
        await db.connect()
        await db.create_tables()

        from src.persistence.orders import OrderORM
        from datetime import UTC, datetime

        with pytest.raises(ValueError, match="boom"):
            async with db.session() as session:
                session.add(OrderORM(
                    order_id="o2", client_order_id="c2", symbol="BTCUSDT", side="BUY",
                    order_type="MARKET", status="OPEN", quantity=Decimal("0.1"),
                    created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                ))
                raise ValueError("boom")

        # The row must not have been committed.
        async with db.session() as verify_session:
            result = await verify_session.get(OrderORM, "o2")
            assert result is None

        await db.disconnect()


class TestGetDatabaseSingleton:
    def test_returns_the_same_instance_on_repeated_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(database_module, "_db", None)
        first = get_database()
        second = get_database()
        assert first is second
        monkeypatch.setattr(database_module, "_db", None)  # reset for other tests
