"""Unit tests for src.monitoring.watchdog.EventLoopWatchdog.

Regression coverage: `on_timeout` fires from a background thread. A
coroutine callback must be bridged back onto the loop that was running
when `start()` was called (via `run_coroutine_threadsafe`), not invoked
directly — a bare call to a coroutine function on that thread just
creates an unawaited coroutine object and never runs it, so the alert
silently never fires. The watchdog must also edge-trigger (fire once
per not-stalled -> stalled transition) rather than re-firing every
check_interval for as long as the stall persists.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from src.monitoring.watchdog import EventLoopWatchdog


class TestEventLoopWatchdogBasics:
    async def test_is_alive_true_before_timeout(self) -> None:
        wd = EventLoopWatchdog(timeout_seconds=1000.0, check_interval=1000.0)
        assert wd.is_alive() is True

    async def test_is_alive_false_once_age_exceeds_timeout(self) -> None:
        wd = EventLoopWatchdog(timeout_seconds=0.05, check_interval=1000.0)
        wd._last_ping = time.monotonic() - 1.0  # noqa: SLF001
        assert wd.is_alive() is False

    async def test_ping_resets_age(self) -> None:
        wd = EventLoopWatchdog(timeout_seconds=1000.0, check_interval=1000.0)
        wd.ping()
        assert wd.age_seconds < 1.0

    async def test_timeout_seconds_property(self) -> None:
        wd = EventLoopWatchdog(timeout_seconds=42.0, check_interval=1.0)
        assert wd.timeout_seconds == 42.0

    async def test_start_stop_lifecycle(self) -> None:
        wd = EventLoopWatchdog(timeout_seconds=1000.0, check_interval=0.01)
        wd.start()
        assert wd._running is True  # noqa: SLF001
        wd.stop()
        assert wd._running is False  # noqa: SLF001

    async def test_start_spawns_a_daemon_thread(self) -> None:
        # Must be a daemon thread: a non-daemon watchdog thread would
        # keep the process alive after the main loop/app has otherwise
        # finished shutting down.
        wd = EventLoopWatchdog(timeout_seconds=1000.0, check_interval=0.01)
        wd.start()
        try:
            assert wd._thread is not None  # noqa: SLF001
            assert wd._thread.daemon is True  # noqa: SLF001
        finally:
            wd.stop()


class TestEventLoopWatchdogCoroutineCallbackBridging:
    """
    Regression coverage: coroutine on_timeout callbacks must actually run
    (on the main loop), not be silently dropped or created-but-never-
    awaited.
    """

    async def test_coroutine_on_timeout_actually_executes(self) -> None:
        fired = asyncio.Event()
        calls: list[float] = []

        async def on_timeout() -> None:
            calls.append(1.0)
            fired.set()

        wd = EventLoopWatchdog(
            timeout_seconds=0.02, check_interval=0.01, on_timeout=on_timeout
        )
        wd.start()  # captures this test's running loop
        try:
            await asyncio.wait_for(fired.wait(), timeout=2.0)
        finally:
            wd.stop()

        assert calls == [1.0]

    async def test_coroutine_on_timeout_fires_once_per_stall_transition(self) -> None:
        """Must not re-fire every check_interval while still stalled."""
        call_count = 0
        got_two = asyncio.Event()

        async def on_timeout() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                got_two.set()

        wd = EventLoopWatchdog(
            timeout_seconds=0.02, check_interval=0.01, on_timeout=on_timeout
        )
        wd.start()
        try:
            # Never ping again — stays stalled for many check cycles.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(got_two.wait(), timeout=0.3)
        finally:
            wd.stop()

        assert call_count == 1

    async def test_recovery_resets_edge_trigger_for_next_stall(self) -> None:
        call_count = 0

        async def on_timeout() -> None:
            nonlocal call_count
            call_count += 1

        wd = EventLoopWatchdog(
            timeout_seconds=0.02, check_interval=0.01, on_timeout=on_timeout
        )
        wd.start()
        try:
            await asyncio.sleep(0.1)  # first stall fires (call_count -> 1)
            wd.ping()  # recover
            await asyncio.sleep(0.1)  # second stall fires (call_count -> 2)
        finally:
            wd.stop()

        assert call_count == 2

    async def test_no_running_loop_at_start_drops_coroutine_callback_safely(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        If start() is somehow called with no running loop, a later stall
        must log-and-drop the coroutine callback rather than raising out
        of the watchdog thread or silently constructing a dangling
        coroutine object.
        """
        async def on_timeout() -> None:
            pytest.fail("should never run: no loop was available to schedule on")

        wd = EventLoopWatchdog(
            timeout_seconds=0.02, check_interval=0.01, on_timeout=on_timeout
        )
        wd._loop = None  # simulate start() outside a running loop  # noqa: SLF001

        with caplog.at_level(logging.ERROR, logger="src.monitoring.watchdog"):
            wd._running = True  # noqa: SLF001
            wd._fire_timeout(1.0)  # noqa: SLF001

        assert any("dropping alert" in rec.message for rec in caplog.records)


class TestEventLoopWatchdogSyncCallback:
    async def test_sync_on_timeout_still_supported(self) -> None:
        calls: list[float] = []
        wd = EventLoopWatchdog(
            timeout_seconds=1.0, check_interval=1.0, on_timeout=lambda: calls.append(1.0)
        )
        wd._loop = None  # noqa: SLF001
        wd._fire_timeout(5.0)  # noqa: SLF001
        assert calls == [1.0]

    async def test_sync_on_timeout_exception_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken() -> None:
            raise ValueError("boom")

        wd = EventLoopWatchdog(timeout_seconds=1.0, check_interval=1.0, on_timeout=broken)
        with caplog.at_level(logging.ERROR, logger="src.monitoring.watchdog"):
            wd._fire_timeout(5.0)  # noqa: SLF001  # must not raise

        assert any("Watchdog callback failed" in rec.message for rec in caplog.records)
