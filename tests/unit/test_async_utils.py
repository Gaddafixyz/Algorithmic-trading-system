"""Unit tests for src.utils.async_utils."""

from __future__ import annotations

import asyncio

import pytest

from src.utils.async_utils import (
    cancel_and_wait,
    create_task_with_log,
    gather_with_errors,
    run_with_timeout,
)


class TestGatherWithErrors:
    async def test_returns_all_results_including_exceptions(self) -> None:
        async def ok() -> int:
            return 1

        async def fail() -> int:
            raise ValueError("boom")

        results = await gather_with_errors(ok(), fail())
        assert results[0] == 1
        assert isinstance(results[1], ValueError)


class TestRunWithTimeout:
    async def test_returns_result_when_within_timeout(self) -> None:
        async def quick() -> str:
            return "done"

        result = await run_with_timeout(quick(), timeout=1.0)
        assert result == "done"

    async def test_returns_default_on_timeout(self) -> None:
        async def slow() -> str:
            await asyncio.sleep(10)
            return "never"

        result = await run_with_timeout(slow(), timeout=0.01, default="fallback")
        assert result == "fallback"


class TestCreateTaskWithLog:
    async def test_task_result_is_returned(self) -> None:
        async def coro() -> int:
            return 7

        task = create_task_with_log(coro(), name="t1")
        assert await task == 7

    async def test_task_exception_is_reraised(self) -> None:
        async def coro() -> None:
            raise ValueError("task failed")

        task = create_task_with_log(coro(), name="t2")
        with pytest.raises(ValueError, match="task failed"):
            await task


class TestCancelAndWait:
    """
    Regression tests for cancel_and_wait.

    Previously ``except (asyncio.CancelledError, Exception): pass``
    silently swallowed *any* exception raised while the cancelled task
    unwound -- not just the expected CancelledError. That could hide a
    real bug in cleanup code at the worst possible time (shutdown). The
    fix suppresses only CancelledError and re-raises everything else.
    """

    async def test_cancels_a_running_task_cleanly(self) -> None:
        async def forever() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(forever())
        await asyncio.sleep(0)  # let it start
        await cancel_and_wait(task)

        assert task.cancelled()

    async def test_already_done_task_is_a_noop(self) -> None:
        async def quick() -> int:
            return 1

        task = asyncio.create_task(quick())
        await task
        # Should not raise even though the task is already done.
        await cancel_and_wait(task)

    async def test_non_cancelled_exception_during_unwind_is_reraised(self) -> None:
        started = asyncio.Event()

        async def bad_cleanup() -> None:
            started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                # Simulates a real bug in a finally/except block during
                # shutdown -- this must NOT be silently swallowed.
                raise RuntimeError("cleanup exploded") from None

        task = asyncio.create_task(bad_cleanup())
        await started.wait()

        with pytest.raises(RuntimeError, match="cleanup exploded"):
            await cancel_and_wait(task)
