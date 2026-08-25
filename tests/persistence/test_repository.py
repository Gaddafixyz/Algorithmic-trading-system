"""Tests for src.persistence.repository.BaseRepository.

Uses OrderORM as a concrete model to exercise the generic repository
against a real (in-memory) database, since BaseRepository is only ever
used through a concrete subclass in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import DuplicateRecordError, RecordNotFoundError
from src.persistence.orders import OrderORM
from src.persistence.repository import BaseRepository


def make_order_orm(
    order_id: str = "order-1",
    client_order_id: str = "client-1",
    symbol: str = "BTCUSDT",
    status: str = "OPEN",
) -> OrderORM:
    now = datetime.now(UTC)
    return OrderORM(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side="BUY",
        order_type="MARKET",
        status=status,
        quantity=Decimal("0.1"),
        created_at=now,
        updated_at=now,
    )


class TestSave:
    async def test_save_persists_a_new_entity(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        order = make_order_orm()
        result = await repo.save(order)
        assert result is order

        found = await db_session.get(OrderORM, "order-1")
        assert found is not None
        assert found.symbol == "BTCUSDT"

    async def test_save_raises_duplicate_record_error_on_primary_key_conflict(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="dup-pk"))

        with pytest.raises(DuplicateRecordError):
            await repo.save(make_order_orm(order_id="dup-pk", client_order_id="different"))

    async def test_save_raises_duplicate_record_error_on_unique_constraint_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # client_order_id has its own unique constraint independent of
        # the primary key.
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="pk-1", client_order_id="shared-client-id"))

        with pytest.raises(DuplicateRecordError):
            await repo.save(make_order_orm(order_id="pk-2", client_order_id="shared-client-id"))

    async def test_save_rolls_back_the_session_after_a_duplicate(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="dup-2"))
        await db_session.commit()  # durably persist before the conflicting save

        with pytest.raises(DuplicateRecordError):
            await repo.save(make_order_orm(order_id="dup-2", client_order_id="other"))

        # Session must still be usable afterward (rollback happened),
        # and the original committed row must survive the rollback of
        # the failed duplicate-insert transaction.
        result = await repo.find_by_id("dup-2")
        assert result is not None


class TestUpsert:
    async def test_upsert_inserts_when_no_row_exists(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        now = datetime.now(UTC)
        result = await repo.upsert(
            "new-id",
            {
                "order_id": "new-id", "client_order_id": "c-new", "symbol": "ETHUSDT",
                "side": "SELL", "order_type": "LIMIT", "status": "PENDING",
                "quantity": Decimal("1"), "created_at": now, "updated_at": now,
            },
        )
        assert result.symbol == "ETHUSDT"
        assert result.status == "PENDING"

    async def test_upsert_updates_the_existing_row_in_place(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        order = make_order_orm(order_id="upsert-1", status="SUBMITTED")
        await repo.save(order)

        result = await repo.upsert("upsert-1", {"status": "FILLED"})

        assert result.status == "FILLED"
        # Confirm it's genuinely the same row, not a second insert.
        assert await repo.count() == 1

    async def test_upsert_does_not_create_a_second_row_for_the_same_key(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="lifecycle-1", status="SUBMITTED"))
        await repo.upsert("lifecycle-1", {"status": "OPEN"})
        await repo.upsert("lifecycle-1", {"status": "FILLED"})

        assert await repo.count() == 1
        found = await repo.find_by_id("lifecycle-1")
        assert found.status == "FILLED"


class TestFindById:
    async def test_returns_none_for_a_missing_id(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        assert await repo.find_by_id("does-not-exist") is None

    async def test_returns_the_matching_entity(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="findme"))
        result = await repo.find_by_id("findme")
        assert result is not None
        assert result.order_id == "findme"


class TestGetById:
    async def test_returns_the_matching_entity(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="findme"))
        result = await repo.get_by_id("findme")
        assert result.order_id == "findme"

    async def test_raises_record_not_found_for_a_missing_id(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        with pytest.raises(RecordNotFoundError, match="does-not-exist"):
            await repo.get_by_id("does-not-exist")

    async def test_does_not_change_find_by_ids_none_returning_contract(
        self, db_session: AsyncSession
    ) -> None:
        # upsert() and delete_by_id() both depend on find_by_id() returning
        # None (not raising) for a missing id -- get_by_id is additive,
        # not a replacement.
        repo = BaseRepository(db_session, OrderORM)
        assert await repo.find_by_id("still-not-here") is None


class TestFindAll:
    async def test_returns_all_entities_with_no_filters(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="a", client_order_id="ca"))
        await repo.save(make_order_orm(order_id="b", client_order_id="cb"))
        result = await repo.find_all()
        assert len(result) == 2

    async def test_filters_by_a_single_column(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="btc-1", client_order_id="c1", symbol="BTCUSDT"))
        await repo.save(make_order_orm(order_id="eth-1", client_order_id="c2", symbol="ETHUSDT"))

        result = await repo.find_all(symbol="ETHUSDT")
        assert len(result) == 1
        assert result[0].symbol == "ETHUSDT"

    async def test_filters_by_multiple_columns_combined_with_and(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(
            order_id="1", client_order_id="c1", symbol="BTCUSDT", status="OPEN"
        ))
        await repo.save(make_order_orm(
            order_id="2", client_order_id="c2", symbol="BTCUSDT", status="FILLED"
        ))

        result = await repo.find_all(symbol="BTCUSDT", status="OPEN")
        assert len(result) == 1
        assert result[0].order_id == "1"

    async def test_unknown_filter_column_is_silently_ignored(
        self, db_session: AsyncSession
    ) -> None:
        # getattr(self._model, col_name, None) returns None for an
        # unrecognized column name, and the `if col is not None` guard
        # means that filter is simply skipped rather than raising.
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm())
        result = await repo.find_all(nonexistent_column="whatever")
        assert len(result) == 1

    async def test_returns_empty_list_when_no_rows_match(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm())
        result = await repo.find_all(symbol="NONEXISTENT")
        assert result == []


class TestDelete:
    async def test_delete_removes_the_entity(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        order = make_order_orm(order_id="to-delete")
        await repo.save(order)

        await repo.delete(order)

        assert await repo.find_by_id("to-delete") is None

    async def test_delete_by_id_returns_true_and_removes_an_existing_entity(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="del-by-id"))

        result = await repo.delete_by_id("del-by-id")

        assert result is True
        assert await repo.find_by_id("del-by-id") is None

    async def test_delete_by_id_returns_false_for_a_missing_id(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        result = await repo.delete_by_id("never-existed")
        assert result is False


class TestCount:
    async def test_count_with_no_filters_returns_total_rows(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="1", client_order_id="c1"))
        await repo.save(make_order_orm(order_id="2", client_order_id="c2"))
        assert await repo.count() == 2

    async def test_count_with_filters_returns_matching_rows_only(
        self, db_session: AsyncSession
    ) -> None:
        repo = BaseRepository(db_session, OrderORM)
        await repo.save(make_order_orm(order_id="1", client_order_id="c1", symbol="BTCUSDT"))
        await repo.save(make_order_orm(order_id="2", client_order_id="c2", symbol="ETHUSDT"))
        assert await repo.count(symbol="BTCUSDT") == 1

    async def test_count_of_an_empty_table_is_zero(self, db_session: AsyncSession) -> None:
        repo = BaseRepository(db_session, OrderORM)
        assert await repo.count() == 0
