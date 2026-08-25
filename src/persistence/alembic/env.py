"""
Alembic environment configuration.

Supports both offline (SQL script generation) and online (direct DB)
migration modes.  Loads the database URL from the application's settings
so it never has to be hard-coded here.
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all ORM models so Alembic can detect schema changes.
# The explicit imports populate Base.metadata before autogenerate runs.
from src.persistence.database import Base
import src.persistence.orders    # noqa: F401
import src.persistence.trades    # noqa: F401
import src.persistence.signals   # noqa: F401
import src.persistence.metrics   # noqa: F401

logger = logging.getLogger("alembic.env")

# Alembic Config object — gives access to values in alembic.ini
config = context.config

# Setup Python logging from alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Provide SQLAlchemy metadata so autogenerate can diff against the live DB
target_metadata = Base.metadata


def _get_url() -> str:
    """
    Return the database URL.

    Priority:
    1. ``sqlalchemy.url`` set via alembic CLI (e.g. ``-x sqlalchemy.url=…``)
    2. The application's ``DatabaseConfig.url`` from environment / YAML.
    """
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    # Fall back to application settings
    from src.core.config import get_settings
    return get_settings().database.url


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Generates SQL without connecting to the database.  Useful for producing
    migration scripts to review or run manually in production.
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode against a live database connection.

    Uses an async engine so it is compatible with asyncpg.
    """
    config_section = config.get_section(config.config_ini_section) or {}
    config_section["sqlalchemy.url"] = _get_url()

    connectable = async_engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
