"""Unit tests for src.bootstrap.lifecycle.LifecycleManager.

Uses a Mock/AsyncMock "Application" and patches the module-level
`startup`/`shutdown` coroutine functions so these tests exercise only
LifecycleManager's own orchestration logic (signal handling, the main
loop, the reconciliation scheduler loop, and shutdown-on-exception),
without depending on a real Application graph.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bootstrap.lifecycle import LifecycleManager
from src.core.exceptions import ShutdownError


def _fake_app() -> MagicMock:
    app = MagicMock()
    app.watchdog = MagicMock()
    app.reconciliation = MagicMock()
    app.reconciliation.reconcile = AsyncMock()
    return app


class TestRun:
    async def test_calls_startup_then_shutdown(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)
        mgr._shutdown_event.set()  # main loop exits immediately

        with patch("src.bootstrap.lifecycle.startup", new=AsyncMock()) as mock_startup, \
             patch("src.bootstrap.lifecycle.shutdown", new=AsyncMock()) as mock_shutdown:
            await mgr.run()

        mock_startup.assert_awaited_once_with(app, None)
        mock_shutdown.assert_awaited_once_with(app, None)

    async def test_shutdown_error_from_shutdown_does_not_propagate(self) -> None:
        # shutdown() raises ShutdownError when one or more teardown steps
        # failed (see bootstrap/shutdown.py) -- by that point every step
        # has already run, so LifecycleManager.run() just logs it rather
        # than letting it escape run() (which is called from `finally`
        # and must not raise over whatever the try block was handling).
        app = _fake_app()
        mgr = LifecycleManager(app)
        mgr._shutdown_event.set()

        with patch("src.bootstrap.lifecycle.startup", new=AsyncMock()), \
             patch(
                 "src.bootstrap.lifecycle.shutdown",
                 new=AsyncMock(side_effect=ShutdownError("2 error(s): boom; boom2")),
             ):
            await mgr.run()  # must not raise

    async def test_shutdown_still_runs_if_startup_raises(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)

        with patch(
            "src.bootstrap.lifecycle.startup", new=AsyncMock(side_effect=RuntimeError("boom"))
        ), patch("src.bootstrap.lifecycle.shutdown", new=AsyncMock()) as mock_shutdown:
            await mgr.run()  # must not propagate the startup exception

        mock_shutdown.assert_awaited_once_with(app, None)

    async def test_passes_orchestrator_through_to_startup_and_shutdown(self) -> None:
        app = _fake_app()
        orchestrator = MagicMock()
        mgr = LifecycleManager(app, orchestrator=orchestrator)
        mgr._shutdown_event.set()

        with patch("src.bootstrap.lifecycle.startup", new=AsyncMock()) as mock_startup, \
             patch("src.bootstrap.lifecycle.shutdown", new=AsyncMock()) as mock_shutdown:
            await mgr.run()

        mock_startup.assert_awaited_once_with(app, orchestrator)
        mock_shutdown.assert_awaited_once_with(app, orchestrator)

    async def test_reconcile_task_is_cancelled_before_shutdown(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)
        mgr._shutdown_event.set()

        with patch("src.bootstrap.lifecycle.startup", new=AsyncMock()), \
             patch("src.bootstrap.lifecycle.shutdown", new=AsyncMock()):
            await mgr.run()

        assert mgr._reconcile_task is not None
        assert mgr._reconcile_task.cancelled() or mgr._reconcile_task.done()

    async def test_signal_handlers_are_installed(self) -> None:
        import signal

        app = _fake_app()
        mgr = LifecycleManager(app)
        mgr._shutdown_event.set()
        loop = asyncio.get_running_loop()

        with patch.object(loop, "add_signal_handler") as mock_add, \
             patch("src.bootstrap.lifecycle.startup", new=AsyncMock()), \
             patch("src.bootstrap.lifecycle.shutdown", new=AsyncMock()):
            await mgr.run()

        registered = {call.args[0] for call in mock_add.call_args_list}
        assert registered == {signal.SIGINT, signal.SIGTERM}


class TestMainLoop:
    async def test_pings_watchdog_and_exits_once_shutdown_is_set(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)

        async def _set_shutdown_soon() -> None:
            await asyncio.sleep(0.01)
            mgr._shutdown_event.set()

        asyncio.create_task(_set_shutdown_soon())
        await asyncio.wait_for(mgr._main_loop(), timeout=2.0)

        assert app.watchdog.ping.called


class TestReconcileLoop:
    async def test_calls_reconcile_after_interval_elapses(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)

        with patch("src.bootstrap.lifecycle.RECONCILE_INTERVAL_SECONDS", 0.01):
            async def _stop_after_one_tick() -> None:
                await asyncio.sleep(0.03)
                mgr._shutdown_event.set()

            asyncio.create_task(_stop_after_one_tick())
            await asyncio.wait_for(mgr._reconcile_loop(), timeout=2.0)

        assert app.reconciliation.reconcile.await_count >= 1

    async def test_returns_immediately_if_shutdown_already_set(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)
        mgr._shutdown_event.set()

        await asyncio.wait_for(mgr._reconcile_loop(), timeout=1.0)

        app.reconciliation.reconcile.assert_not_awaited()

    async def test_reconcile_exception_does_not_kill_the_loop(self) -> None:
        app = _fake_app()
        app.reconciliation.reconcile = AsyncMock(
            side_effect=[RuntimeError("exchange unreachable"), None]
        )
        mgr = LifecycleManager(app)

        with patch("src.bootstrap.lifecycle.RECONCILE_INTERVAL_SECONDS", 0.01):
            async def _stop_after_two_ticks() -> None:
                await asyncio.sleep(0.05)
                mgr._shutdown_event.set()

            asyncio.create_task(_stop_after_two_ticks())
            await asyncio.wait_for(mgr._reconcile_loop(), timeout=2.0)

        assert app.reconciliation.reconcile.await_count >= 2

    async def test_cancelled_error_propagates(self) -> None:
        app = _fake_app()
        app.reconciliation.reconcile = AsyncMock(side_effect=asyncio.CancelledError())
        mgr = LifecycleManager(app)

        with patch("src.bootstrap.lifecycle.RECONCILE_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(mgr._reconcile_loop())
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)


class TestRequestShutdown:
    def test_sets_shutdown_event(self) -> None:
        app = _fake_app()
        mgr = LifecycleManager(app)
        assert not mgr._shutdown_event.is_set()
        mgr._request_shutdown()
        assert mgr._shutdown_event.is_set()
