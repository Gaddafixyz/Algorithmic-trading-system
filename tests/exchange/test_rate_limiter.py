"""Unit tests for src.exchange.base_client.RateLimiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.exchange.base_client import RateLimiter


class TestRateLimiterBasicAcquire:
    async def test_acquire_under_the_limit_does_not_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=10, max_per_minute=100)
        await limiter.acquire()
        assert sleep_calls == []


class TestRateLimiterPerSecondLimit:
    async def test_waits_when_the_per_second_limit_is_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock["t"] += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=2, max_per_minute=1000)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(1.0, abs=0.01)

    async def test_does_not_wait_once_the_window_has_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

        async def fake_sleep(seconds: float) -> None:
            clock["t"] += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=1, max_per_minute=1000)
        await limiter.acquire()
        clock["t"] += 1.1
        sleep_calls: list[float] = []

        async def track_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", track_sleep)
        await limiter.acquire()
        assert sleep_calls == []


class TestRateLimiterPerMinuteLimit:
    async def test_waits_when_the_per_minute_limit_is_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock["t"] += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=1000, max_per_minute=2)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(60.0, abs=0.01)


class TestRateLimiterPer10SecondLimit:
    async def test_waits_when_the_10_second_limit_is_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock["t"] += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=1000, max_per_minute=1000, max_per_10_seconds=2)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(10.0, abs=0.01)

    async def test_disabled_10_second_limit_is_not_enforced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"t": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        limiter = RateLimiter(max_per_second=1000, max_per_minute=1000)
        for _ in range(20):
            await limiter.acquire()
        assert sleep_calls == []
