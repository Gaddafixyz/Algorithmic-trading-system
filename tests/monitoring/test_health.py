"""Unit tests for src.monitoring.health.HealthChecker.

Covers: aggregation across sync and async check functions, the
DEGRADED-vs-UNHEALTHY overall-status logic (any single failing check
degrades the result, and unhealthy checks force the overall status to
UNHEALTHY even alongside healthy ones), exception isolation (one
check's failure must not prevent the others from running), and the
two standard checks (``check_database`` / ``check_exchange``).
"""

from __future__ import annotations

import pytest

from src.core.enums import HealthStatus
from src.monitoring.health import HealthChecker


class TestRunAllBasics:
    async def test_no_checks_registered_is_healthy(self) -> None:
        checker = HealthChecker()
        result = await checker.run_all()
        assert result["status"] == HealthStatus.HEALTHY.value
        assert result["checks"] == {}
        assert "timestamp" in result

    async def test_single_sync_check_healthy(self) -> None:
        checker = HealthChecker()
        checker.register("disk", lambda: {"free_gb": 100})
        result = await checker.run_all()
        assert result["status"] == HealthStatus.HEALTHY.value
        assert result["checks"]["disk"]["status"] == HealthStatus.HEALTHY.value
        assert result["checks"]["disk"]["detail"] == {"free_gb": 100}

    async def test_single_async_check_healthy(self) -> None:
        async def check() -> dict[str, bool]:
            return {"ok": True}

        checker = HealthChecker()
        checker.register("async_check", check)
        result = await checker.run_all()
        assert result["checks"]["async_check"]["status"] == HealthStatus.HEALTHY.value
        assert result["checks"]["async_check"]["detail"] == {"ok": True}

    async def test_mixed_sync_and_async_checks_both_run(self) -> None:
        async def async_ok() -> str:
            return "async-ok"

        checker = HealthChecker()
        checker.register("sync_one", lambda: "sync-ok")
        checker.register("async_one", async_ok)
        result = await checker.run_all()
        assert result["checks"]["sync_one"]["detail"] == "sync-ok"
        assert result["checks"]["async_one"]["detail"] == "async-ok"


class TestFailureAggregation:
    async def test_failing_check_marks_itself_unhealthy(self) -> None:
        checker = HealthChecker()

        def broken() -> None:
            raise RuntimeError("db down")

        checker.register("db", broken)
        result = await checker.run_all()
        assert result["checks"]["db"]["status"] == HealthStatus.UNHEALTHY.value
        assert "db down" in result["checks"]["db"]["error"]

    async def test_one_failing_check_makes_overall_unhealthy(self) -> None:
        checker = HealthChecker()
        checker.register("healthy_one", lambda: "ok")

        def broken() -> None:
            raise RuntimeError("boom")

        checker.register("broken_one", broken)
        result = await checker.run_all()
        assert result["status"] == HealthStatus.UNHEALTHY.value
        assert result["checks"]["healthy_one"]["status"] == HealthStatus.HEALTHY.value

    async def test_failing_async_check_does_not_block_other_checks(self) -> None:
        """Regression: one check raising must not prevent later
        registered checks from running (iteration must continue)."""
        calls: list[str] = []

        async def first_broken() -> None:
            calls.append("first")
            raise RuntimeError("first failed")

        def second_ok() -> str:
            calls.append("second")
            return "ran"

        checker = HealthChecker()
        checker.register("first", first_broken)
        checker.register("second", second_ok)
        result = await checker.run_all()

        assert calls == ["first", "second"]
        assert result["checks"]["second"]["status"] == HealthStatus.HEALTHY.value

    async def test_multiple_failures_all_captured_independently(self) -> None:
        checker = HealthChecker()

        def broken_a() -> None:
            raise ValueError("a failed")

        def broken_b() -> None:
            raise KeyError("b failed")

        checker.register("a", broken_a)
        checker.register("b", broken_b)
        result = await checker.run_all()
        assert result["status"] == HealthStatus.UNHEALTHY.value
        assert result["checks"]["a"]["status"] == HealthStatus.UNHEALTHY.value
        assert result["checks"]["b"]["status"] == HealthStatus.UNHEALTHY.value

    async def test_overall_unhealthy_takes_precedence_over_degraded_wording(self) -> None:
        """The intermediate DEGRADED status set inside the loop must be
        overridden to UNHEALTHY once any check is confirmed unhealthy,
        not left at DEGRADED."""
        checker = HealthChecker()

        def broken() -> None:
            raise RuntimeError("x")

        checker.register("only_check", broken)
        result = await checker.run_all()
        assert result["status"] == HealthStatus.UNHEALTHY.value


class TestStandardChecks:
    async def test_check_database_healthy_when_connected(self) -> None:
        class FakeDB:
            is_connected = True

        checker = HealthChecker()
        result = await checker.check_database(FakeDB())
        assert result == {"connected": True}

    async def test_check_database_raises_when_disconnected(self) -> None:
        class FakeDB:
            is_connected = False

        checker = HealthChecker()
        with pytest.raises(RuntimeError, match="not connected"):
            await checker.check_database(FakeDB())

    async def test_check_database_wired_through_run_all_reports_unhealthy(self) -> None:
        import functools

        class FakeDB:
            is_connected = False

        checker = HealthChecker()
        # `run_all` dispatches on `asyncio.iscoroutinefunction(fn)`, so the
        # registered callable itself must be recognisable as a coroutine
        # function -- a plain lambda wrapping an async call would return an
        # un-awaited coroutine object instead of running the check.
        checker.register("database", functools.partial(checker.check_database, FakeDB()))
        result = await checker.run_all()
        assert result["checks"]["database"]["status"] == HealthStatus.UNHEALTHY.value
        assert "not connected" in result["checks"]["database"]["error"]

    async def test_check_exchange_returns_server_time(self) -> None:
        class FakeClient:
            async def get_server_time(self) -> int:
                return 1_700_000_000_000

        checker = HealthChecker()
        result = await checker.check_exchange(FakeClient())
        assert result == {"server_time_ms": 1_700_000_000_000, "connected": True}

    async def test_check_exchange_propagates_client_error(self) -> None:
        class FailingClient:
            async def get_server_time(self) -> int:
                raise ConnectionError("exchange unreachable")

        checker = HealthChecker()
        with pytest.raises(ConnectionError):
            await checker.check_exchange(FailingClient())
