"""Unit tests for src.monitoring.telemetry (structlog configuration)."""

from __future__ import annotations

import logging
import logging.handlers

import structlog

from src.monitoring.telemetry import configure_logging, get_logger


def _console_handler() -> logging.StreamHandler:
    """The console handler is always handlers[0] after configure_logging()."""
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    return handler


class TestConfigureLogging:
    def teardown_method(self) -> None:
        # structlog.configure() is process-global; reset so other test
        # modules don't inherit whatever renderer/level this test left
        # behind. Also strip handlers off the root logger for the same
        # reason (configure_logging mutates it directly).
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()

    def test_json_logs_true_uses_json_renderer_on_console(self) -> None:
        configure_logging(log_level="INFO", json_logs=True)
        formatter = _console_handler().formatter
        assert isinstance(formatter, structlog.stdlib.ProcessorFormatter)
        assert isinstance(formatter.processors[-1], structlog.processors.JSONRenderer)

    def test_json_logs_false_uses_console_renderer(self) -> None:
        configure_logging(log_level="INFO", json_logs=False)
        formatter = _console_handler().formatter
        assert isinstance(formatter.processors[-1], structlog.dev.ConsoleRenderer)

    def test_default_is_console_renderer(self) -> None:
        configure_logging()
        formatter = _console_handler().formatter
        assert isinstance(formatter.processors[-1], structlog.dev.ConsoleRenderer)

    def test_common_processors_are_always_present(self) -> None:
        configure_logging()
        # Shared processors run as the foreign_pre_chain on the console
        # handler's formatter (and are also the head of structlog's own
        # configure()d processor chain).
        pre_chain = _console_handler().formatter.foreign_pre_chain
        assert structlog.contextvars.merge_contextvars in pre_chain
        assert structlog.stdlib.add_log_level in pre_chain
        assert structlog.stdlib.add_logger_name in pre_chain

        processors = structlog.get_config()["processors"]
        assert structlog.contextvars.merge_contextvars in processors
        assert structlog.stdlib.add_log_level in processors
        assert structlog.stdlib.add_logger_name in processors

    def test_invalid_log_level_falls_back_to_info(self) -> None:
        # getattr(logging, "NOT_A_LEVEL".upper(), logging.INFO) silently
        # falls back to INFO rather than raising -- this must not throw.
        configure_logging(log_level="not-a-real-level")
        root_level = logging.getLogger().level
        assert root_level == logging.INFO

    def test_log_level_is_applied_to_root_logger(self) -> None:
        configure_logging(log_level="WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_noisy_third_party_loggers_are_silenced_to_warning(self) -> None:
        configure_logging(log_level="DEBUG")
        for noisy in ("asyncio", "websockets", "aiohttp", "sqlalchemy.engine"):
            assert logging.getLogger(noisy).level == logging.WARNING

    def test_logger_levels_none_falls_back_to_hardcoded_noisy_defaults(self) -> None:
        # Callers that don't pass logger_levels (e.g. this test, or any
        # code not yet updated to read Settings.logging.logger_levels)
        # keep the pre-existing hardcoded behavior.
        configure_logging(log_level="DEBUG", logger_levels=None)
        for noisy in ("asyncio", "websockets", "aiohttp", "sqlalchemy.engine"):
            assert logging.getLogger(noisy).level == logging.WARNING

    def test_logger_levels_overrides_are_applied(self) -> None:
        configure_logging(
            log_level="DEBUG",
            logger_levels={"websockets": "DEBUG", "asyncio": "WARNING"},
        )
        assert logging.getLogger("websockets").level == logging.DEBUG
        assert logging.getLogger("asyncio").level == logging.WARNING

    def test_logger_levels_only_applies_what_it_is_given(self) -> None:
        # aiohttp/sqlalchemy.engine aren't in this dict, so configure_logging
        # doesn't touch them -- unlike the None-default path, an explicit
        # (possibly partial) dict is used as-is, not merged with the
        # hardcoded set. Settings.logging.logger_levels is what performs
        # the merge (see core/config.py:_load_logging_yaml), before the
        # dict ever reaches this function.
        configure_logging(log_level="DEBUG", logger_levels={"websockets": "ERROR"})
        assert logging.getLogger("websockets").level == logging.ERROR

    def test_file_disabled_by_default_only_console_handler(self) -> None:
        configure_logging()
        assert len(logging.getLogger().handlers) == 1

    def test_file_enabled_adds_rotating_file_handler(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        configure_logging(
            file_enabled=True,
            log_dir=tmp_path,
            file_name="trading.log",
            max_bytes=1024,
            backup_count=3,
        )
        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        file_handler = handlers[1]
        assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
        assert file_handler.maxBytes == 1024
        assert file_handler.backupCount == 3
        assert (tmp_path / "trading.log").exists()

    def test_file_sink_always_renders_json_regardless_of_console_mode(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        configure_logging(json_logs=False, file_enabled=True, log_dir=tmp_path)
        file_handler = logging.getLogger().handlers[1]
        assert isinstance(file_handler.formatter.processors[-1], structlog.processors.JSONRenderer)

    def test_file_dir_created_if_missing(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        nested = tmp_path / "nested" / "logs"
        assert not nested.exists()
        configure_logging(file_enabled=True, log_dir=nested)
        assert nested.exists()


class TestGetLogger:
    def teardown_method(self) -> None:
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()

    def test_returns_a_bound_logger(self) -> None:
        configure_logging()
        logger = get_logger("my.module")
        # structlog's bound loggers expose the standard logging verbs.
        assert hasattr(logger, "info")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "error")

    def test_does_not_raise_when_logging(self) -> None:
        configure_logging()
        logger = get_logger("my.module")
        # Should not raise regardless of renderer configuration.
        logger.info("test message", extra_field=1)

    def test_does_not_raise_when_logging_to_file(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        configure_logging(file_enabled=True, log_dir=tmp_path)
        logger = get_logger("my.module")
        logger.info("test message", extra_field=1)
        assert (tmp_path / "trading.log").read_text().strip() != ""
