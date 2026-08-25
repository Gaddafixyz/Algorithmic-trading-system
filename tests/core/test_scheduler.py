"""Unit tests for src.core.scheduler.

No prior test file referenced this module at all. It backs every
periodic background job in the live system (alert evaluation, health
checks, cache pruning, etc. — see bootstrap/startup.py), so a scheduling
or error-isolation regression here would silently stop those jobs
without any test catching it.
"""

from __future__ import annotations

import asyncio

import pytest

from src.core.scheduler import ScheduledTask, Scheduler

# Small enough to keep the suite fast, large enough to be stable under
# normal CI scheduling jitter.
INTERVAL = 0.02


async def _wait_ticks(n: int, interval: float = INTERVAL) -> None:
    await asyncio.sleep(interval * n + interval / 2)


class TestScheduledTask:
    async def test_run_immediately_true_executes_before_first_sleep(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        task = ScheduledTask("t", fn, INTERVAL, run_immediately=True)
        task.start()
        try:
            await asyncio.sleep(INTERVAL / 4)
            assert len(calls) == 1
        finally:
            task.cancel()

    async def test_run_immediately_false_waits_one_interval_first(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        task = ScheduledTask("t", fn, INTERVAL, run_immediately=False)
        task.start()
        try:
            await asyncio.sleep(INTERVAL / 4)
            assert len(calls) == 0
            await _wait_ticks(1)
            assert len(calls) >= 1
        finally:
            task.cancel()

    async def test_repeats_at_interval(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        task = ScheduledTask("t", fn, INTERVAL, run_immediately=True)
        task.start()
        try:
            await _wait_ticks(3)
            assert len(calls) >= 3
        finally:
            task.cancel()

    async def test_one_failure_does_not_stop_the_loop(self) -> None:
        """Error isolation: an exception in fn() must not kill the task."""
        calls = []

        async def fn() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        task = ScheduledTask("t", fn, INTERVAL, run_immediately=True)
        task.start()
        try:
            await _wait_ticks(3)
            assert len(calls) >= 3
            assert task._error_count >= 1
            assert task.is_running is True
        finally:
            task.cancel()

    async def test_stats_reflect_run_and_error_counts(self) -> None:
        async def failing() -> None:
            raise ValueError("nope")

        task = ScheduledTask("failer", failing, INTERVAL, run_immediately=True)
        task.start()
        try:
            await _wait_ticks(2)
            stats = task.stats
            assert stats["name"] == "failer"
            assert stats["error_count"] >= 1
            assert stats["run_count"] == 0  # every run raised, so it never completes
            assert stats["is_running"] is True
        finally:
            task.cancel()

    async def test_cancel_stops_further_execution(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        task = ScheduledTask("t", fn, INTERVAL, run_immediately=True)
        task.start()
        await _wait_ticks(1)
        task.cancel()
        await asyncio.sleep(INTERVAL / 4)  # let the CancelledError propagate
        count_at_cancel = len(calls)
        assert task.is_running is False

        await _wait_ticks(3)
        assert len(calls) == count_at_cancel  # no further runs after cancel

    async def test_is_running_false_before_start(self) -> None:
        async def fn() -> None:
            pass

        task = ScheduledTask("t", fn, INTERVAL)
        assert task.is_running is False


class TestScheduler:
    async def test_schedule_rejects_duplicate_name(self) -> None:
        scheduler = Scheduler()

        async def fn() -> None:
            pass

        scheduler.schedule("job", fn, INTERVAL)
        with pytest.raises(ValueError):
            scheduler.schedule("job", fn, INTERVAL)

    async def test_start_starts_all_registered_tasks(self) -> None:
        calls: dict[str, int] = {"a": 0, "b": 0}

        def make_fn(key: str):
            async def fn() -> None:
                calls[key] += 1

            return fn

        scheduler = Scheduler()
        scheduler.schedule("a", make_fn("a"), INTERVAL, run_immediately=True)
        scheduler.schedule("b", make_fn("b"), INTERVAL, run_immediately=True)
        scheduler.start()
        try:
            await asyncio.sleep(INTERVAL / 4)
            assert calls["a"] == 1
            assert calls["b"] == 1
        finally:
            await scheduler.stop()

    async def test_schedule_after_start_starts_task_immediately(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        scheduler = Scheduler()
        scheduler.start()
        scheduler.schedule("late", fn, INTERVAL, run_immediately=True)
        try:
            await asyncio.sleep(INTERVAL / 4)
            assert len(calls) == 1
        finally:
            await scheduler.stop()

    async def test_stop_cancels_all_tasks(self) -> None:
        calls = []

        async def fn() -> None:
            calls.append(1)

        scheduler = Scheduler()
        scheduler.schedule("a", fn, INTERVAL, run_immediately=True)
        scheduler.start()
        await asyncio.sleep(INTERVAL / 4)
        await scheduler.stop()

        count_at_stop = len(calls)
        await _wait_ticks(3)
        assert len(calls) == count_at_stop
        assert all(not t.is_running for t in scheduler._tasks.values())

    async def test_cancel_removes_task_and_returns_true(self) -> None:
        async def fn() -> None:
            pass

        scheduler = Scheduler()
        scheduler.schedule("a", fn, INTERVAL)
        scheduler.start()
        try:
            assert scheduler.cancel("a") is True
            assert "a" not in scheduler._tasks
        finally:
            await scheduler.stop()

    async def test_cancel_unknown_task_returns_false(self) -> None:
        scheduler = Scheduler()
        assert scheduler.cancel("does-not-exist") is False

    async def test_stats_lists_every_task(self) -> None:
        async def fn() -> None:
            pass

        scheduler = Scheduler()
        scheduler.schedule("a", fn, INTERVAL)
        scheduler.schedule("b", fn, INTERVAL)
        names = {s["name"] for s in scheduler.stats}
        assert names == {"a", "b"}
