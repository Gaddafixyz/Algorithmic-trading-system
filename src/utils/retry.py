"""Generic async retry decorator and helper."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator: retry an async function with exponential backoff."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = base_delay
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    logger.warning(
                        "Retry %d/%d for %s: %s. Waiting %.1fs",
                        attempt, max_attempts, fn.__qualname__, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * backoff, max_delay)
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
