"""
System health checks.

Exposes a /health endpoint and periodic internal health monitoring.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.core.enums import HealthStatus

logger = logging.getLogger(__name__)


class HealthChecker:
    """Aggregates health status from multiple system components."""

    def __init__(self) -> None:
        self._checks: dict[str, Any] = {}

    def register(self, name: str, check_fn: Any) -> None:
        """Register a health check function (sync or async)."""
        self._checks[name] = check_fn

    async def run_all(self) -> dict[str, Any]:
        """Run all registered health checks and aggregate results."""
        import asyncio
        results: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "status": HealthStatus.HEALTHY.value,
            "checks": {},
        }

        for name, fn in self._checks.items():
            try:
                if asyncio.iscoroutinefunction(fn):
                    result = await fn()
                else:
                    result = fn()
                results["checks"][name] = {"status": HealthStatus.HEALTHY.value, "detail": result}
            except Exception as exc:
                results["checks"][name] = {
                    "status": HealthStatus.UNHEALTHY.value,
                    "error": str(exc),
                }
                results["status"] = HealthStatus.DEGRADED.value
                logger.warning("Health check '%s' failed: %s", name, exc)

        # If any check is UNHEALTHY, overall is UNHEALTHY
        unhealthy = [
            k for k, v in results["checks"].items()
            if v.get("status") == HealthStatus.UNHEALTHY.value
        ]
        if unhealthy:
            results["status"] = HealthStatus.UNHEALTHY.value

        return results

    async def check_database(self, db: Any) -> dict[str, Any]:
        """Standard database health check."""
        if not db.is_connected:
            raise RuntimeError("Database not connected")
        return {"connected": True}

    async def check_exchange(self, client: Any) -> dict[str, Any]:
        """Standard exchange connectivity check."""
        server_time = await client.get_server_time()
        return {"server_time_ms": server_time, "connected": True}


# NOTE: A second, unwired `Watchdog` class (synchronous, ping()/is_alive())
# used to live here as an apparent duplicate of
# `src.monitoring.watchdog.EventLoopWatchdog` (the one actually constructed
# in `bootstrap/app.py` as `Application.watchdog`). It was imported in
# `app.py` but never instantiated anywhere in src/ or tests/ — confirmed via
# grep before removal. Removed as dead code; `EventLoopWatchdog` is the
# real liveness watchdog.
