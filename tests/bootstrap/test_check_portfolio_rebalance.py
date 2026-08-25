"""Unit tests for Application.check_portfolio_rebalance.

Direct coverage of the method's own behavior. Its scheduling (task
name, interval) is covered separately in test_startup.py's
test_schedules_all_documented_periodic_tasks.

Flagged in [1.3.21]'s CHANGELOG as a test gap alongside
record_metrics_snapshot (see test_record_metrics_snapshot.py) — both
previously had only indirect coverage via their scheduling.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.bootstrap.app import Application
from src.portfolio.rebalancer import RebalanceAction, RebalanceDirection


def make_action(
    symbol: str = "BTCUSDT",
    direction: RebalanceDirection = RebalanceDirection.INCREASE,
) -> RebalanceAction:
    return RebalanceAction(
        symbol=symbol,
        current_notional=Decimal("1000"),
        target_notional=Decimal("2000"),
        delta_notional=Decimal("1000"),
        drift_pct=Decimal("50"),
        direction=direction,
    )


@pytest.fixture
def app() -> Application:
    instance = Application.__new__(Application)
    instance.portfolio = MagicMock()
    instance.portfolio_service = MagicMock()
    instance._rebalance_symbols = ["BTCUSDT", "ETHUSDT"]
    instance._last_rebalance_actions = []
    return instance


class TestCheckPortfolioRebalance:
    async def test_noop_when_no_rebalance_symbols_configured(self, app: Application) -> None:
        app._rebalance_symbols = []

        await app.check_portfolio_rebalance()

        app.portfolio_service.target_allocation.assert_not_called()

    async def test_calls_target_allocation_with_configured_symbols(self, app: Application) -> None:
        app.portfolio_service.rebalancer.check_rebalance.return_value = []

        await app.check_portfolio_rebalance()

        app.portfolio_service.target_allocation.assert_called_once_with(["BTCUSDT", "ETHUSDT"])

    async def test_stores_actions_on_last_rebalance_actions(self, app: Application) -> None:
        actions = [make_action()]
        app.portfolio_service.rebalancer.check_rebalance.return_value = actions

        await app.check_portfolio_rebalance()

        assert app._last_rebalance_actions == actions

    async def test_empty_actions_clears_last_rebalance_actions(self, app: Application) -> None:
        app._last_rebalance_actions = [make_action()]
        app.portfolio_service.rebalancer.check_rebalance.return_value = []

        await app.check_portfolio_rebalance()

        assert app._last_rebalance_actions == []

    async def test_exception_is_swallowed(self, app: Application) -> None:
        app.portfolio_service.target_allocation.side_effect = RuntimeError("boom")

        await app.check_portfolio_rebalance()  # should not raise
