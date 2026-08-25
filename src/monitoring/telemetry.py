"""
Structured logging and telemetry configuration.

Configures structlog for JSON-structured logging with:
- Timestamp, log level, module, message
- Request/correlation IDs for tracing
- Environment-appropriate rendering (JSON in prod, colored in dev)
- Optional rotating file sink and per-logger level overrides (both
  sourced from configs/logging.yaml via Settings.logging — see
  core/config.py)

Console and file both render through the same shared structlog processor
chain via structlog.stdlib.ProcessorFormatter, so record shape (level,
logger name, timestamp, correlation IDs) is identical across sinks. The
file sink always renders JSON regardless of `json_logs`, since archived
logs are read by tooling, not a terminal.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

_DEFAULT_NOISY_LOGGERS = ("asyncio", "websockets", "aiohttp", "sqlalchemy.engine")


def configure_logging(
    log_level: str = "INFO",
    json_logs: bool = False,
    *,
    file_enabled: bool = False,
    log_dir: Path | str = "./logs",
    file_name: str = "trading.log",
    max_bytes: int = 52_428_800,
    backup_count: int = 10,
    logger_levels: dict[str, str] | None = None,
) -> None:
    """
    Configure structured logging for the entire application.

    Args:
        log_level: Standard Python log level string.
        json_logs: If True, console emits JSON; if False, colored console output.
        file_enabled: If True, also attach a RotatingFileHandler to the root logger.
        log_dir: Directory the log file is written into (created if missing).
        file_name: Log file name within log_dir.
        max_bytes: Rotate after the file reaches this size.
        backup_count: Number of rotated backups to retain.
        logger_levels: Per-logger level overrides, applied after the root
            logger is configured (e.g. {"asyncio": "WARNING"}). Sourced from
            Settings.logging.logger_levels (configs/logging.yaml's
            `loggers:` section, merged over a hardcoded noisy-third-party
            default — see core/config.py:LoggingConfig). Defaults to that
            same hardcoded set if not provided, so callers that don't pass
            it (e.g. existing tests) keep prior behavior.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    console_render = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_render,
        ],
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    if file_enabled:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        file_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_path / file_name),
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers (or whatever configs/logging.yaml's
    # `loggers:` section overrides them to — see LoggingConfig.logger_levels).
    resolved_logger_levels = (
        logger_levels
        if logger_levels is not None
        else {name: "WARNING" for name in _DEFAULT_NOISY_LOGGERS}
    )
    for name, level_name in resolved_logger_levels.items():
        logging.getLogger(name).setLevel(getattr(logging, level_name, logging.WARNING))


def get_logger(name: str) -> Any:
    """Return a structlog-bound logger."""
    return structlog.get_logger(name)
