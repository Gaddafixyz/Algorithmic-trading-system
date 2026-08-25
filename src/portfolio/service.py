"""
Portfolio service: facade over the domain ``Portfolio``, the allocator,
and the rebalancer.

This is the single entry point other layers (bootstrap, dashboard,
strategy orchestrator) should use for allocation/rebalance questions,
rather than constructing ``PortfolioAllocator``/``PortfolioRebalancer``
themselves — keeps the drift-threshold and allocation-constraint config
in one place.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.domain.portfolio import Portfolio
from src.portfolio.allocator import AllocationMethod, PortfolioAllocator
from src.portfolio.rebalancer import PortfolioRebalancer, RebalanceAction

logger = logging.getLogger(__name__)


class PortfolioService:
    """Facade combining live portfolio state with allocation/rebalance logic."""

    def __init__(
        self,
        portfolio: Portfolio,
        allocator: PortfolioAllocator | None = None,
        rebalancer: PortfolioRebalancer | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.allocator = allocator or PortfolioAllocator()
        self.rebalancer = rebalancer or PortfolioRebalancer()

    def target_allocation(
        self,
        symbols: list[str],
        *,
        method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT,
        volatilities: dict[str, float] | None = None,
        fixed_weights: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        """Target notional per symbol at current equity."""
        return self.allocator.allocate(
            equity=self.portfolio.current_equity,
            symbols=symbols,
            method=method,
            volatilities=volatilities,
            fixed_weights=fixed_weights,
        )

    def check_rebalance(
        self,
        symbols: list[str],
        *,
        method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT,
        volatilities: dict[str, float] | None = None,
        fixed_weights: dict[str, Decimal] | None = None,
    ) -> list[RebalanceAction]:
        """Actionable (non-HOLD) drift between current and target allocation."""
        target = self.target_allocation(
            symbols, method=method, volatilities=volatilities, fixed_weights=fixed_weights,
        )
        return self.rebalancer.check_rebalance(self.portfolio, target)

    def allocation_report(
        self,
        symbols: list[str],
        *,
        method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT,
        volatilities: dict[str, float] | None = None,
        fixed_weights: dict[str, Decimal] | None = None,
    ) -> dict[str, Any]:
        """
        Full allocation snapshot (including HOLDs) for dashboards/reporting.

        Shape mirrors ``Application._dashboard_status`` conventions —
        plain str-keyed dict of JSON-serializable values, no Decimal
        objects left un-stringified.
        """
        target = self.target_allocation(
            symbols, method=method, volatilities=volatilities, fixed_weights=fixed_weights,
        )
        actions = self.rebalancer.full_report(self.portfolio, target)
        return {
            "equity": str(self.portfolio.current_equity),
            "method": method.value,
            "allocations": [
                {
                    "symbol": a.symbol,
                    "current_notional": str(a.current_notional),
                    "target_notional": str(a.target_notional),
                    "delta_notional": str(a.delta_notional),
                    "drift_pct": str(a.drift_pct),
                    "direction": a.direction.value,
                }
                for a in actions
            ],
        }
