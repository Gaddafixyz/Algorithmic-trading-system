"""
Database migration script.

Usage:
    python scripts/migrate_db.py             # Run pending migrations
    python scripts/migrate_db.py --create    # Create all tables from ORM models
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import get_settings
from src.monitoring.telemetry import configure_logging


async def run_create(database_url: str) -> None:
    from src.persistence.migrations import create_all_tables

    await create_all_tables(database_url)
    print("✓ All tables created from ORM models")


def run_migrate(database_url: str) -> None:
    from src.persistence.migrations import run_migrations

    run_migrations(database_url)
    print("✓ Migrations applied")


def main() -> None:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="Database migration tool")
    parser.add_argument("--create", action="store_true", help="Create tables from ORM (dev only)")
    args = parser.parse_args()

    settings = get_settings()
    db_url = settings.database.url

    if args.create:
        asyncio.run(run_create(db_url))
    else:
        run_migrate(db_url)


if __name__ == "__main__":
    main()
