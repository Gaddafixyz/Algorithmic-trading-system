"""System heartbeat: periodic liveness signal."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class HeartbeatService:
    """
    Emits periodic heartbeat log entries and metrics.

    Useful for detecting when the event loop stalls or the system
    stops processing candles.
    """

    def __init__(self, interval_seconds: float = 60.0) -> None:
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._beat_count: int = 0
        self._started_at: datetime | None = None
        self._running = False
        self._context_fns: list[Any] = []

    def add_context(self, fn: Any) -> None:
        """Register a function that returns a dict to include in heartbeat."""
        self._context_fns.append(fn)

    async def start(self) -> None:
        self._running = True
        self._started_at = datetime.now(tz=UTC)
        self._task = asyncio.create_task(self._loop(), name="heartbeat")
        logger.info("Heartbeat service started (interval=%.0fs)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            self._beat_count += 1
            uptime = (
                (datetime.now(tz=UTC) - self._started_at).total_seconds() if self._started_at else 0
            )

            context: dict[str, Any] = {
                "beat": self._beat_count,
                "uptime_seconds": int(uptime),
            }
            for fn in self._context_fns:
                try:
                    context.update(fn())
                except Exception:
                    logger.warning(
                        "Heartbeat context function %r raised; omitting its "
                        "contribution to this beat",
                        getattr(fn, "__qualname__", fn),
                        exc_info=True,
                    )

            logger.info("Heartbeat #%d | uptime=%ds | %s", self._beat_count, int(uptime), context)

    @property
    def beat_count(self) -> int:
        return self._beat_count
