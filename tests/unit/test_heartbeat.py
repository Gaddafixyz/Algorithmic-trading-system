"""Unit tests for src.monitoring.heartbeat.HeartbeatService."""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.monitoring.heartbeat import HeartbeatService


class TestHeartbeatService:
    async def test_start_and_stop_lifecycle(self) -> None:
        svc = HeartbeatService(interval_seconds=1000.0)
        await svc.start()
        assert svc._running is True  # noqa: SLF001
        await svc.stop()
        assert svc._running is False  # noqa: SLF001

    async def test_beat_count_increments_over_time(self) -> None:
        svc = HeartbeatService(interval_seconds=0.01)
        await svc.start()
        await asyncio.sleep(0.05)
        await svc.stop()
        assert svc.beat_count >= 2

    async def test_context_fn_contributes_to_heartbeat(self) -> None:
        svc = HeartbeatService(interval_seconds=0.01)
        svc.add_context(lambda: {"custom": 42})
        await svc.start()
        await asyncio.sleep(0.03)
        await svc.stop()
        assert svc.beat_count >= 1


class TestHeartbeatContextFnErrorHandling:
    """
    Regression coverage: a raising context function must not silently
    disappear (previously ``except Exception: pass``). It should be
    logged, and other (working) context functions must still run.
    """

    async def test_failing_context_fn_is_logged_not_silently_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        svc = HeartbeatService(interval_seconds=0.01)

        def broken() -> dict:
            raise ValueError("context boom")

        svc.add_context(broken)

        with caplog.at_level(logging.WARNING, logger="src.monitoring.heartbeat"):
            await svc.start()
            await asyncio.sleep(0.03)
            await svc.stop()

        assert any("raised" in rec.message for rec in caplog.records)

    async def test_failing_context_fn_does_not_block_other_context_fns(self) -> None:
        svc = HeartbeatService(interval_seconds=0.01)
        good_calls: list[int] = []

        def broken() -> dict:
            raise ValueError("boom")

        def good() -> dict:
            good_calls.append(1)
            return {"ok": True}

        svc.add_context(broken)
        svc.add_context(good)

        await svc.start()
        await asyncio.sleep(0.03)
        await svc.stop()

        assert len(good_calls) >= 1

    async def test_failing_context_fn_does_not_stop_heartbeat_loop(self) -> None:
        svc = HeartbeatService(interval_seconds=0.01)
        svc.add_context(lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        await svc.start()
        await asyncio.sleep(0.05)
        await svc.stop()

        # The loop must keep beating despite the always-failing context fn.
        assert svc.beat_count >= 2
