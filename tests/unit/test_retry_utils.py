"""Unit tests for src.utils.retry."""

from __future__ import annotations

import asyncio

import pytest

from src.utils.retry import async_retry


class TestAsyncRetry:
    async def test_succeeds_on_first_attempt_without_retrying(self) -> None:
        calls = 0

        @async_retry(max_attempts=3, base_delay=0.001)
        async def works() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = await works()
        assert result == "ok"
        assert calls == 1

    async def test_retries_after_failure_and_eventually_succeeds(self) -> None:
        calls = 0

        @async_retry(max_attempts=3, base_delay=0.001)
        async def fails_twice_then_succeeds() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = await fails_twice_then_succeeds()
        assert result == "recovered"
        assert calls == 3

    async def test_raises_the_last_exception_after_exhausting_all_attempts(
        self,
    ) -> None:
        calls = 0

        @async_retry(max_attempts=3, base_delay=0.001)
        async def always_fails() -> None:
            nonlocal calls
            calls += 1
            raise ConnectionError(f"attempt {calls}")

        with pytest.raises(ConnectionError, match="attempt 3"):
            await always_fails()
        assert calls == 3

    async def test_does_not_retry_exceptions_outside_the_specified_types(self) -> None:
        calls = 0

        @async_retry(max_attempts=3, base_delay=0.001, exceptions=(ConnectionError,))
        async def raises_value_error() -> None:
            nonlocal calls
            calls += 1
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            await raises_value_error()
        # Only attempted once: ValueError isn't in the retryable exceptions tuple.
        assert calls == 1

    async def test_preserves_function_return_value_type(self) -> None:
        @async_retry(max_attempts=2, base_delay=0.001)
        async def returns_a_dict() -> dict[str, int]:
            return {"count": 42}

        result = await returns_a_dict()
        assert result == {"count": 42}

    async def test_backoff_delay_grows_exponentially_up_to_max_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        calls = 0

        @async_retry(
            max_attempts=4, base_delay=1.0, max_delay=3.0, backoff=2.0
        )
        async def always_fails() -> None:
            nonlocal calls
            calls += 1
            raise ConnectionError("boom")

        with pytest.raises(ConnectionError):
            await always_fails()

        # 3 sleeps between 4 attempts: 1.0, 2.0, then capped at max_delay=3.0
        # (uncapped would be 4.0).
        assert sleep_calls == [1.0, 2.0, 3.0]

    async def test_default_exceptions_tuple_retries_broad_exception(self) -> None:
        calls = 0

        @async_retry(max_attempts=2, base_delay=0.001)
        async def fails_with_generic_exception() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("generic failure")
            return "ok"

        result = await fails_with_generic_exception()
        assert result == "ok"
        assert calls == 2

    async def test_preserves_function_metadata_via_functools_wraps(self) -> None:
        @async_retry(max_attempts=1)
        async def documented() -> None:
            """A docstring."""

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "A docstring."
