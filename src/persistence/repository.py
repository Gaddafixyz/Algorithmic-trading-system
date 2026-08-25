"""
Generic async repository base class.

Implements the Repository pattern over SQLAlchemy async sessions.
All concrete repositories extend BaseRepository.
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import DuplicateRecordError, RecordNotFoundError
from src.persistence.database import Base

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """
    Generic CRUD repository for a SQLAlchemy ORM model.

    Type parameter T must be a SQLAlchemy mapped class.
    """

    def __init__(self, session: AsyncSession, model: type[T]) -> None:
        self._session = session
        self._model = model

    async def save(self, entity: T) -> T:
        """
        Insert a new entity.

        Raises:
            DuplicateRecordError: if a unique constraint (including primary
                key) is violated, instead of letting a raw asyncpg/SQLAlchemy
                IntegrityError propagate. Callers that persist the same
                logical record repeatedly across its lifecycle (e.g. an order
                whose status changes many times) should use ``upsert`` below,
                not ``save``.
        """
        self._session.add(entity)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            entity_id = getattr(entity, "id", None) or self._primary_key_value(entity)
            raise DuplicateRecordError(
                f"{self._model.__name__} violates a unique constraint: {entity_id}"
            ) from exc
        logger.debug("Saved %s id=%s", self._model.__name__, getattr(entity, "id", "?"))
        return entity

    async def upsert(self, entity_id: Any, values: dict[str, Any]) -> T:
        """
        Insert a new row, or update the existing one in place, keyed on
        primary key.

        Entities such as orders are persisted repeatedly as their state
        changes (SUBMITTED -> OPEN -> PARTIALLY_FILLED -> FILLED). Naively
        constructing a fresh ORM instance and calling ``save()`` on every
        update tries to INSERT a row whose primary key already exists,
        raising a duplicate-key error instead of updating it. This loads the
        existing row (if any) and applies the new field values onto it,
        falling back to a plain insert for the first save.
        """
        existing = await self.find_by_id(entity_id)
        if existing is None:
            new_entity = self._model(**values)
            return await self.save(new_entity)

        for key, value in values.items():
            setattr(existing, key, value)
        await self._session.flush()
        logger.debug("Upserted %s id=%s", self._model.__name__, entity_id)
        return existing

    def _primary_key_value(self, entity: T) -> Any:
        try:
            state = inspect(entity)
            return state.identity
        except Exception:
            return "?"

    async def find_by_id(self, entity_id: Any) -> T | None:
        """Find entity by primary key."""
        return await self._session.get(self._model, entity_id)

    async def get_by_id(self, entity_id: Any) -> T:
        """
        Find entity by primary key, raising if it doesn't exist.

        Additive alongside find_by_id rather than a replacement: upsert()
        and delete_by_id() below both rely on find_by_id's None-means-
        absent contract as a normal, non-error outcome, so that method's
        behavior is intentionally unchanged. Use this instead wherever a
        missing record actually is a fault the caller wants surfaced
        rather than branched on.
        """
        entity = await self.find_by_id(entity_id)
        if entity is None:
            raise RecordNotFoundError(
                f"{self._model.__name__} with id={entity_id!r} not found"
            )
        return entity

    async def find_all(self, **filters: Any) -> list[T]:
        """Find all entities matching the given column filters."""
        stmt = select(self._model)
        for col_name, value in filters.items():
            col = getattr(self._model, col_name, None)
            if col is not None:
                stmt = stmt.where(col == value)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, entity: T) -> None:
        """Delete an entity."""
        await self._session.delete(entity)
        await self._session.flush()

    async def delete_by_id(self, entity_id: Any) -> bool:
        """Delete by primary key. Returns True if found and deleted."""
        entity = await self.find_by_id(entity_id)
        if entity is None:
            return False
        await self.delete(entity)
        return True

    async def count(self, **filters: Any) -> int:
        """Count entities matching filters."""
        from sqlalchemy import func
        stmt = select(func.count()).select_from(self._model)
        for col_name, value in filters.items():
            col = getattr(self._model, col_name, None)
            if col is not None:
                stmt = stmt.where(col == value)
        result = await self._session.execute(stmt)
        return result.scalar_one()
