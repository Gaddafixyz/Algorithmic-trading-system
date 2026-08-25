"""Database migration management via Alembic."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_migrations(database_url: str, migrations_dir: Path | None = None) -> None:
    """
    Run pending Alembic migrations.

    Should be called during system startup before any DB operations.
    """
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config()
        alembic_cfg.set_main_option("sqlalchemy.url", database_url)

        migrations_path = str(migrations_dir or Path(__file__).parent / "alembic")
        alembic_cfg.set_main_option("script_location", migrations_path)

        command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations applied successfully")

    except Exception as exc:
        logger.error("Migration failed: %s", exc)
        raise


async def create_all_tables(database_url: str) -> None:
    """
    Create all tables from ORM models.

    Use this for development/testing instead of Alembic migrations.
    """
    from src.persistence.database import get_database
    db = get_database()
    await db.connect()
    await db.create_tables()
    logger.info("All tables created from ORM models")
