"""Asyncio utility functions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


async def gather_with_errors(
    *coros: Coroutine[Any, Any, Any],
    log_errors: bool = True,
) -> list[Any]:
    """Run coroutines concurrently; log but don't raise individual errors."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) and log_errors:
            logger.exception("Concurrent task error: %s", r)
    return results


async def run_with_timeout(
    coro: Coroutine[Any, Any, Any],
    timeout: float,
    default: Any = None,
) -> Any:
    """Run a coroutine with a timeout; return `default` on timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.warning("Operation timed out after %.1fs", timeout)
        return default


def create_task_with_log(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create a task that logs exceptions instead of swallowing them."""

    async def _wrapper() -> Any:
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Task %s failed: %s", name or "unnamed", exc)
            raise

    return asyncio.create_task(_wrapper(), name=name)


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """
    Cancel a task and wait for it to finish.

    Only ``asyncio.CancelledError`` (the expected outcome of cancellation)
    is suppressed. Any other exception raised by the task while it unwinds
    indicates a real bug in its cleanup path and is logged and re-raised
    rather than silently discarded — swallowing it here would hide errors
    at the worst possible time (shutdown).
    """
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Task %s raised during cancellation", task.get_name())
            raise
