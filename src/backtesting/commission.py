"""Commission models for backtesting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal


class CommissionModel(ABC):
    """
    Base commission model interface.

    An ABC so a subclass that forgets to implement ``calculate`` fails at
    instantiation rather than silently returning ``NotImplementedError``
    only when a backtest first tries to cost a fill.
    """

    @abstractmethod
    def calculate(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Return the commission owed for a fill of ``quantity`` at ``price``."""
        raise NotImplementedError


class PercentageCommission(CommissionModel):
    """Fixed percentage commission per side (default: Binance taker 0.04%)."""

    def __init__(self, rate: Decimal = Decimal("0.0004")) -> None:
        self._rate = rate

    def calculate(self, quantity: Decimal, price: Decimal) -> Decimal:
        return quantity * price * self._rate


class ZeroCommission(CommissionModel):
    """No commission (for benchmarking gross performance)."""

    def calculate(self, quantity: Decimal, price: Decimal) -> Decimal:
        return Decimal("0")


class TieredCommission(CommissionModel):
    """
    Volume-tiered commission (simplified Binance structure).

    Lower rate applied to larger notional.
    """

    def __init__(self) -> None:
        # (min_monthly_volume_usd, rate)
        self._tiers = [
            (Decimal("0"), Decimal("0.0004")),
            (Decimal("250_000"), Decimal("0.0002")),
            (Decimal("2_500_000"), Decimal("0.00016")),
        ]
        self._current_rate = self._tiers[0][1]

    def set_volume_tier(self, monthly_volume: Decimal) -> None:
        for min_vol, rate in reversed(self._tiers):
            if monthly_volume >= min_vol:
                self._current_rate = rate
                break

    def calculate(self, quantity: Decimal, price: Decimal) -> Decimal:
        return quantity * price * self._current_rate
