"""Utility decorators."""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def log_call(fn: F) -> F:
    """Log entry and exit of a function with timing."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        logger.debug("→ %s()", fn.__qualname__)
        t0 = time.perf_counter()
        result = fn(*args, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.debug("← %s() %.1fms", fn.__qualname__, elapsed)
        return result
    return wrapper  # type: ignore[return-value]


def singleton(cls: type) -> type:
    """Class decorator that enforces singleton pattern."""
    instances: dict[type, Any] = {}

    @functools.wraps(cls)
    def get_instance(*args: Any, **kwargs: Any) -> Any:
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance  # type: ignore[return-value]


def validate_positive(param_name: str) -> Callable[[F], F]:
    """Decorator that validates a named parameter is positive."""
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            value = kwargs.get(param_name)
            if value is not None and value <= 0:
                raise ValueError(f"{param_name} must be positive, got {value}")
            return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator
