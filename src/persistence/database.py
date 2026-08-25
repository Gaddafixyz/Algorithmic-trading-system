"""
Async PostgreSQL database connection manager.

Uses SQLAlchemy 2.0 async engine with asyncpg.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import DatabaseConfig

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


class Database:
    """
    Async database connection manager.

    Provides:
    - Engine creation with pool configuration
    - Session factory with automatic cleanup
    - Schema creation / migration hooks
    """

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        """Create the async engine and session factory."""
        # pool_size/max_overflow/pool_timeout/pool_pre_ping are
        # Postgres-pool-specific kwargs that create_async_engine()
        # rejects outright for non-Postgres dialects (e.g. SQLite,
        # which validate_database_url() on DatabaseConfig explicitly
        # advertises as a supported scheme — used for local
        # development and testing without a real Postgres instance).
        # Passing them unconditionally would make connect() fail for
        # any URL matching that advertised-but-unsupported case.
        engine_kwargs: dict[str, object] = {"echo": self._config.echo}
        if self._config.url.startswith("postgresql"):
            engine_kwargs.update(
                pool_size=self._config.pool_size,
                max_overflow=self._config.max_overflow,
                pool_timeout=self._config.pool_timeout,
                pool_pre_ping=True,
            )
        self._engine = create_async_engine(self._config.url, **engine_kwargs)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        logger.info("Database connected: %s", self._config.url.split("@")[-1])

    async def disconnect(self) -> None:
        """Dispose the connection pool."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        logger.info("Database disconnected")

    async def create_tables(self) -> None:
        """Create all tables defined in ORM models."""
        if self._engine is None:
            raise RuntimeError("Database not connected")
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables created")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Provide a transactional session with automatic rollback on error."""
        if self._session_factory is None:
            raise RuntimeError("Database not connected — call connect() first")
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @property
    def is_connected(self) -> bool:
        return self._engine is not None


_db: Database | None = None


def get_database() -> Database:
    """Return the singleton Database instance."""
    global _db  # noqa: PLW0603
    if _db is None:
        from src.core.config import get_settings

        _db = Database(get_settings().database)
    return _db
