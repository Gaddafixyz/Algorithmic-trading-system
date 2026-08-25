"""Unit tests for Application.evict_stale_market_data.

Direct coverage of the method's own behavior. Its scheduling (task name,
interval) is covered separately in test_startup.py's
test_schedules_all_documented_periodic_tasks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.bootstrap.app import Application


@pytest.fixture
def app() -> Application:
    instance = Application.__new__(Application)
    instance.cache = MagicMock()
    return instance


class TestEvictStaleMarketData:
    async def test_calls_cache_eviction(self, app: Application) -> None:
        app.cache.evict_stale_tickers.return_value = 3
        await app.evict_stale_market_data()
        app.cache.evict_stale_tickers.assert_called_once_with()

    async def test_zero_evicted_does_not_raise(self, app: Application) -> None:
        app.cache.evict_stale_tickers.return_value = 0
        await app.evict_stale_market_data()  # should not raise

    async def test_cache_exception_is_swallowed(self, app: Application) -> None:
        app.cache.evict_stale_tickers.side_effect = RuntimeError("boom")
        await app.evict_stale_market_data()  # should not propagate
