"""
Rebalance detection: compares live portfolio notional against target
allocation and reports drift.

Deliberately detection-only — mirrors the split already used elsewhere in
the system between "detect a divergence" (e.g. ``ReconciliationService``)
and "act on it" (``ExecutionEngine``). ``PortfolioRebalancer`` never places
or cancels an order itself; it returns ``RebalanceAction`` objects for a
caller (strategy orchestrator, a scheduled task, or an operator reviewing
the dashboard) to act on. This keeps "should we rebalance" auditable and
testable independent of execution, and avoids a rebalancer silently
resizing positions based on a target that may itself be stale.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from src.domain.portfolio import Portfolio

logger = logging.getLogger(__name__)


class RebalanceDirection(str, Enum):
    INCREASE = "increase"
    DECREASE = "decrease"
    HOLD = "hold"


class RebalanceAction(BaseModel):
    """A single symbol's drift from its target allocation."""

    symbol: str
    current_notional: Decimal
    target_notional: Decimal
    delta_notional: Decimal  # target - current; positive means "needs more"
    drift_pct: Decimal  # abs(delta) / target * 100; Decimal("0") if target is 0
    direction: RebalanceDirection


class PortfolioRebalancer:
    """
    Flags symbols whose current notional has drifted from target beyond
    a configurable threshold.
    """

    def __init__(self, drift_threshold_pct: Decimal = Decimal("10")) -> None:
        if drift_threshold_pct <= 0:
            raise ValueError("drift_threshold_pct must be positive")
        self.drift_threshold_pct = drift_threshold_pct

    def check_rebalance(
        self,
        portfolio: Portfolio,
        target_allocation: dict[str, Decimal],
    ) -> list[RebalanceAction]:
        """
        Compare current per-symbol notional in ``portfolio`` against
        ``target_allocation`` and return actionable drifts only (symbols
        within ``drift_threshold_pct`` are omitted — use
        ``full_report`` if you need every symbol including HOLDs).
        """
        return [a for a in self.full_report(portfolio, target_allocation)
                if a.direction != RebalanceDirection.HOLD]

    def full_report(
        self,
        portfolio: Portfolio,
        target_allocation: dict[str, Decimal],
    ) -> list[RebalanceAction]:
        """
        Same comparison as ``check_rebalance`` but includes every symbol
        that has either an open position or a target (i.e. HOLDs too) —
        useful for a dashboard/report view of full allocation state.
        """
        current_notional = self._current_notional_by_symbol(portfolio)
        symbols = set(current_notional) | set(target_allocation)

        actions: list[RebalanceAction] = []
        for symbol in sorted(symbols):
            current = current_notional.get(symbol, Decimal("0"))
            target = target_allocation.get(symbol, Decimal("0"))
            delta = target - current

            if target > 0:
                drift_pct = abs(delta) / target * Decimal("100")
            elif current > 0:
                # Target dropped to zero (symbol no longer in the target
                # set) but we're still holding it — full drift, should
                # be flagged for exit regardless of threshold.
                drift_pct = Decimal("100")
            else:
                drift_pct = Decimal("0")

            if drift_pct < self.drift_threshold_pct:
                direction = RebalanceDirection.HOLD
            elif delta > 0:
                direction = RebalanceDirection.INCREASE
            elif delta < 0:
                direction = RebalanceDirection.DECREASE
            else:
                direction = RebalanceDirection.HOLD

            actions.append(RebalanceAction(
                symbol=symbol,
                current_notional=current,
                target_notional=target,
                delta_notional=delta,
                drift_pct=drift_pct,
                direction=direction,
            ))

        return actions

    @staticmethod
    def _current_notional_by_symbol(portfolio: Portfolio) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for position in portfolio.open_positions:
            totals[position.symbol] = totals.get(position.symbol, Decimal("0")) + position.notional_value
        return totals
