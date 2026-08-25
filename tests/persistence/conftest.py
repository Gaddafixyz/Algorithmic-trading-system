"""Shared fixtures for src.persistence tests.

Uses a real in-memory SQLite database (via aiosqlite) rather than
mocking SQLAlchemy, so tests exercise genuine SQL execution --
constraint violations, upsert semantics, and filtering all run against
an actual database engine, not a mock that could hide real bugs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.persistence.database import Base

# Import every ORM model module so its table is registered on
# Base.metadata before create_all() runs below.
import src.persistence.orders  # noqa: F401
import src.persistence.trades  # noqa: F401
import src.persistence.signals  # noqa: F401
import src.persistence.metrics  # noqa: F401


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        # Mirror src.persistence.database.Database.session()'s
        # commit-on-success unit-of-work boundary. BaseRepository.save()
        # / upsert() only flush() (visible within the current
        # transaction) rather than commit() -- committing is
        # deliberately left to a higher-level transaction boundary. A
        # bare session without this final commit would make every
        # write silently vanish when the fixture tears down, which
        # would make tests validate only within-transaction visibility
        # rather than genuine durable persistence.
        await session.commit()

    await engine.dispose()
