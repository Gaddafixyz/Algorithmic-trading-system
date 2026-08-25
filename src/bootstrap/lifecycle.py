"""Application lifecycle manager with signal handling."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from src.bootstrap.app import Application
from src.bootstrap.startup import startup
from src.bootstrap.shutdown import shutdown
from src.core.constants import RECONCILE_INTERVAL_SECONDS
from src.core.exceptions import ShutdownError

logger = logging.getLogger(__name__)


class LifecycleManager:
    """
    Manages application startup, runtime loop, and shutdown.

    Installs OS signal handlers (SIGINT, SIGTERM) for graceful shutdown.
    """

    def __init__(
        self,
        app: Application,
        orchestrator: Any | None = None,
    ) -> None:
        self._app = app
        self._orchestrator = orchestrator
        self._shutdown_event = asyncio.Event()
        self._reconcile_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Run the full application lifecycle."""
        loop = asyncio.get_running_loop()

        # Install signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        try:
            await startup(self._app, self._orchestrator)
            self._reconcile_task = asyncio.create_task(
                self._reconcile_loop(), name="reconciliation.scheduler"
            )
            logger.info("System running — press Ctrl+C to stop")
            await self._main_loop()
        except Exception as exc:
            logger.critical("Fatal startup error: %s", exc, exc_info=True)
        finally:
            if self._reconcile_task is not None:
                self._reconcile_task.cancel()
                try:
                    await self._reconcile_task
                except asyncio.CancelledError:
                    pass
            try:
                await shutdown(self._app, self._orchestrator)
            except ShutdownError as exc:
                # shutdown() itself is fully best-effort per-step (every
                # teardown step already runs regardless of prior failures)
                # -- this ShutdownError only means one or more of those
                # steps failed, raised *after* teardown finished, purely
                # so it's visible here. run() is in `finally`, so this
                # must not propagate and mask whatever the try block above
                # was already handling.
                logger.error("Shutdown completed with errors: %s", exc)

    async def _main_loop(self) -> None:
        """Main event loop: watchdog ping + periodic tasks."""
        while not self._shutdown_event.is_set():
            self._app.watchdog.ping()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                pass  # Normal: check again

    async def _reconcile_loop(self) -> None:
        """
        Periodically run ReconciliationService.reconcile() against the
        exchange. This is the exchange-sync safety net: it was previously
        instantiated but never invoked anywhere, so a fill missed by the
        user-data WS (or a process restart) had no way to be detected or
        corrected.
        """
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._shutdown_event.wait()),
                    timeout=RECONCILE_INTERVAL_SECONDS,
                )
                return  # shutdown requested during the wait
            except asyncio.TimeoutError:
                pass  # Normal: time to reconcile

            try:
                await self._app.reconciliation.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # reconcile() already catches and logs its own internal
                # failures; this is a final backstop so a truly unexpected
                # error can't kill the scheduler loop itself.
                logger.exception("Reconciliation scheduler tick failed: %s", exc)

    def _request_shutdown(self) -> None:
        logger.info("Shutdown signal received")
        self._shutdown_event.set()
