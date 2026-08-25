"""Process watchdog: detects stalled event loops and triggers alerts."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

logger = logging.getLogger(__name__)


class EventLoopWatchdog:
    """
    Monitors event loop responsiveness from a background thread.

    If the event loop doesn't respond within `timeout_seconds`, the
    watchdog triggers a callback (e.g. shutdown or alert).

    `on_timeout` may be a plain sync callable or a coroutine function.
    Coroutine callbacks are scheduled back onto the event loop that was
    running when `start()` was called, via `run_coroutine_threadsafe` —
    calling them directly from this background thread would either
    silently no-op (a bare call to a coroutine function just creates an
    unawaited coroutine object) or, if awaited via a throwaway
    `asyncio.run()` loop on this thread, break any loop-bound resource
    the callback touches (aiohttp sessions, DB connections, etc. all
    belong to the *main* loop). The callback fires once on the
    not-stalled -> stalled transition (and logs once on recovery)
    rather than every check_interval for as long as the stall persists,
    matching the "publish on real transitions only" convention used by
    CircuitBreaker elsewhere in this codebase.
    """

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        check_interval: float = 5.0,
        on_timeout: Any = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._interval = check_interval
        self._on_timeout = on_timeout
        self._last_ping = time.monotonic()
        self._thread: threading.Thread | None = None
        self._running = False
        self._was_stalled = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def ping(self) -> None:
        """Called from within the event loop to signal liveness."""
        self._last_ping = time.monotonic()

    @property
    def age_seconds(self) -> float:
        """Seconds since the last ping."""
        return time.monotonic() - self._last_ping

    @property
    def timeout_seconds(self) -> float:
        """The configured stall threshold."""
        return self._timeout

    def is_alive(self) -> bool:
        """True if the event loop has pinged within the timeout window."""
        return self.age_seconds <= self._timeout

    def start(self) -> None:
        self._running = True
        self._was_stalled = False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
            if self._on_timeout is not None and inspect.iscoroutinefunction(self._on_timeout):
                logger.warning(
                    "Watchdog.start() called with no running event loop; "
                    "the coroutine on_timeout callback will be dropped "
                    "(logged, not raised) if a stall is ever detected."
                )
        self._thread = threading.Thread(target=self._watch, daemon=True, name="watchdog")
        self._thread.start()
        logger.info("Watchdog started (timeout=%.0fs)", self._timeout)

    def stop(self) -> None:
        self._running = False

    def _watch(self) -> None:
        while self._running:
            time.sleep(self._interval)
            age = time.monotonic() - self._last_ping
            stalled = age > self._timeout

            if stalled and not self._was_stalled:
                logger.critical(
                    "WATCHDOG: Event loop appears stalled (last ping %.1fs ago)",
                    age,
                )
                self._fire_timeout(age)
            elif not stalled and self._was_stalled:
                logger.info("WATCHDOG: Event loop recovered (was stalled for ~%.1fs)", age)

            self._was_stalled = stalled

    def _fire_timeout(self, age: float) -> None:
        """Invoke on_timeout, bridging to the main loop if it's a coroutine."""
        if self._on_timeout is None:
            return

        if not inspect.iscoroutinefunction(self._on_timeout):
            try:
                self._on_timeout()
            except Exception as exc:
                logger.error("Watchdog callback failed: %s", exc)
            return

        if self._loop is None or not self._loop.is_running():
            logger.error(
                "Watchdog on_timeout is a coroutine callback but no running "
                "event loop is available to schedule it on; dropping alert "
                "for this stall (age=%.1fs).",
                age,
            )
            return

        future = asyncio.run_coroutine_threadsafe(self._on_timeout(), self._loop)
        # Bound the wait so a genuinely wedged loop can't block this
        # watchdog thread's own check cycle forever. If the loop is just
        # slow, the callback still runs to completion asynchronously —
        # we simply stop waiting on its result here.
        wait_seconds = min(self._interval, 5.0)
        try:
            future.result(timeout=wait_seconds)
        except FutureTimeoutError:
            logger.warning(
                "Watchdog on_timeout callback still pending after %.1fs "
                "(event loop may be slow rather than fully stalled)",
                wait_seconds,
            )
        except Exception as exc:
            logger.error("Watchdog on_timeout callback raised: %s", exc)
