"""Tests for src.persistence.migrations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.persistence.migrations import create_all_tables, run_migrations


class TestRunMigrations:
    def test_configures_alembic_with_the_given_database_url(self) -> None:
        with (
            patch("alembic.config.Config") as MockConfig,
            patch("alembic.command.upgrade") as mock_upgrade,
        ):
            mock_cfg_instance = MagicMock()
            MockConfig.return_value = mock_cfg_instance

            run_migrations("postgresql+asyncpg://user:pass@host/db")

            mock_cfg_instance.set_main_option.assert_any_call(
                "sqlalchemy.url", "postgresql+asyncpg://user:pass@host/db"
            )
            mock_upgrade.assert_called_once_with(mock_cfg_instance, "head")

    def test_uses_the_default_migrations_directory_when_not_specified(self) -> None:
        with patch("alembic.config.Config") as MockConfig, patch("alembic.command.upgrade"):
            mock_cfg_instance = MagicMock()
            MockConfig.return_value = mock_cfg_instance

            run_migrations("sqlite:///test.db")

            # script_location call should point somewhere under
            # src/persistence/alembic, not an arbitrary/empty path.
            calls = mock_cfg_instance.set_main_option.call_args_list
            script_location_calls = [c for c in calls if c.args[0] == "script_location"]
            assert len(script_location_calls) == 1
            assert "alembic" in script_location_calls[0].args[1]

    def test_uses_a_custom_migrations_directory_when_provided(self, tmp_path: Path) -> None:
        with patch("alembic.config.Config") as MockConfig, patch("alembic.command.upgrade"):
            mock_cfg_instance = MagicMock()
            MockConfig.return_value = mock_cfg_instance

            run_migrations("sqlite:///test.db", migrations_dir=tmp_path)

            calls = mock_cfg_instance.set_main_option.call_args_list
            script_location_calls = [c for c in calls if c.args[0] == "script_location"]
            assert script_location_calls[0].args[1] == str(tmp_path)

    def test_reraises_and_logs_on_migration_failure(self) -> None:
        with (
            patch("alembic.config.Config") as MockConfig,
            patch("alembic.command.upgrade", side_effect=RuntimeError("migration failed")),
        ):
            mock_cfg_instance = MagicMock()
            MockConfig.return_value = mock_cfg_instance

            with pytest.raises(RuntimeError, match="migration failed"):
                run_migrations("sqlite:///test.db")


class TestCreateAllTables:
    async def test_connects_and_creates_tables_via_the_database_singleton(self) -> None:
        with patch("src.persistence.database.get_database") as mock_get_db:
            mock_db = MagicMock()
            mock_db.connect = MagicMock(return_value=_async_none())
            mock_db.create_tables = MagicMock(return_value=_async_none())
            mock_get_db.return_value = mock_db

            await create_all_tables("sqlite+aiosqlite:///:memory:")

            mock_db.connect.assert_called_once()
            mock_db.create_tables.assert_called_once()


async def _async_none() -> None:
    return None
