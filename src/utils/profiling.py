"""Performance profiling utilities."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Generator

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def timer(label: str, warn_threshold_ms: float = 100.0) -> Generator[None, None, None]:
    """Context manager that logs elapsed time."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > warn_threshold_ms:
            logger.warning("SLOW: %s took %.1fms", label, elapsed_ms)
        else:
            logger.debug("%s: %.1fms", label, elapsed_ms)


class RollingTimer:
    """Tracks rolling average latency for an operation."""

    def __init__(self, window: int = 100) -> None:
        self._window = window
        self._samples: list[float] = []

    def record(self, elapsed_ms: float) -> None:
        self._samples.append(elapsed_ms)
        if len(self._samples) > self._window:
            self._samples.pop(0)

    @property
    def avg_ms(self) -> float:
        return sum(self._samples) / len(self._samples) if self._samples else 0.0

    @property
    def p99_ms(self) -> float:
        if not self._samples:
            return 0.0
        sorted_s = sorted(self._samples)
        idx = int(len(sorted_s) * 0.99)
        return sorted_s[min(idx, len(sorted_s) - 1)]
