"""
Async task scheduler for periodic and one-shot tasks.

Features:
- Periodic tasks with configurable intervals
- One-shot delayed tasks
- Graceful cancellation
- Error isolation (one task failure doesn't kill others)
- Structured logging for all task events
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

TaskFn = Callable[[], Coroutine[Any, Any, None]]


class ScheduledTask:
    """A single scheduled task with metadata."""

    def __init__(
        self,
        name: str,
        fn: TaskFn,
        interval_seconds: float,
        run_immediately: bool = False,
    ) -> None:
        self.name = name
        self.fn = fn
        self.interval_seconds = interval_seconds
        self.run_immediately = run_immediately
        self._task: asyncio.Task[None] | None = None
        self._run_count: int = 0
        self._error_count: int = 0
        self._last_run: datetime | None = None
        self._last_error: Exception | None = None

    async def _run_loop(self) -> None:
        """Task execution loop."""
        if not self.run_immediately:
            await asyncio.sleep(self.interval_seconds)

        while True:
            try:
                await self.fn()
                self._run_count += 1
                self._last_run = datetime.now(tz=UTC)
                logger.debug("Task '%s' completed (run #%d)", self.name, self._run_count)
            except asyncio.CancelledError:
                logger.info("Task '%s' cancelled", self.name)
                return
            except Exception as exc:
                self._error_count += 1
                self._last_error = exc
                logger.exception("Task '%s' failed (error #%d): %s", self.name, self._error_count, exc)

            await asyncio.sleep(self.interval_seconds)

    def start(self) -> None:
        """Start the task loop."""
        self._task = asyncio.create_task(self._run_loop(), name=f"scheduler.{self.name}")
        logger.info("Scheduled task started: %s (interval=%.1fs)", self.name, self.interval_seconds)

    def cancel(self) -> None:
        """Cancel the task."""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "run_count": self._run_count,
            "error_count": self._error_count,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "is_running": self.is_running,
        }


class Scheduler:
    """Registry and manager for periodic async tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._running = False

    def schedule(
        self,
        name: str,
        fn: TaskFn,
        interval_seconds: float,
        *,
        run_immediately: bool = False,
    ) -> None:
        """Register a periodic task. Starts immediately if scheduler is running."""
        if name in self._tasks:
            raise ValueError(f"Task '{name}' already registered")
        task = ScheduledTask(name, fn, interval_seconds, run_immediately)
        self._tasks[name] = task
        if self._running:
            task.start()

    def start(self) -> None:
        """Start all registered tasks."""
        self._running = True
        for task in self._tasks.values():
            if not task.is_running:
                task.start()
        logger.info("Scheduler started with %d tasks", len(self._tasks))

    async def stop(self, timeout_seconds: float = 10.0) -> None:
        """Cancel all tasks and wait for them to finish."""
        self._running = False
        for task in self._tasks.values():
            task.cancel()

        running_tasks = [
            t._task for t in self._tasks.values()
            if t._task and not t._task.done()
        ]
        if running_tasks:
            await asyncio.wait(running_tasks, timeout=timeout_seconds)
        logger.info("Scheduler stopped")

    def cancel(self, name: str) -> bool:
        """Cancel a specific task."""
        task = self._tasks.get(name)
        if task:
            task.cancel()
            del self._tasks[name]
            return True
        return False

    @property
    def stats(self) -> list[dict[str, Any]]:
        return [t.stats for t in self._tasks.values()]
