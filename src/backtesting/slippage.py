"""Slippage models for realistic backtesting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from src.core.models import OHLCV


class SlippageModel(ABC):
    """
    Base slippage model interface.

    An ABC rather than a plain base class with ``raise NotImplementedError``
    so a forgotten override fails at class-definition/instantiation time
    (``TypeError: Can't instantiate abstract class``) instead of at the
    first backtest run that happens to hit the unimplemented path —
    the same reasoning ``CommissionModel`` follows.
    """

    @abstractmethod
    def apply(self, price: Decimal, is_buy: bool, candle: OHLCV) -> Decimal:
        """Return the fill price after slippage for a trade at ``price``."""
        raise NotImplementedError


class FixedSlippage(SlippageModel):
    """Fixed percentage slippage per trade (default: 0.01%)."""

    def __init__(self, slippage_pct: Decimal = Decimal("0.0001")) -> None:
        self._pct = slippage_pct

    def apply(self, price: Decimal, is_buy: bool, candle: OHLCV) -> Decimal:
        if is_buy:
            return price * (Decimal("1") + self._pct)
        return price * (Decimal("1") - self._pct)


class ZeroSlippage(SlippageModel):
    """No slippage (optimistic benchmark)."""

    def apply(self, price: Decimal, is_buy: bool, candle: OHLCV) -> Decimal:
        return price


class SpreadSlippage(SlippageModel):
    """
    Spread-based slippage: fills at bid or ask based on direction.

    Uses high-low spread as a proxy for bid-ask spread when real
    spread data is unavailable.
    """

    def __init__(self, spread_fraction: Decimal = Decimal("0.3")) -> None:
        """
        Args:
            spread_fraction: Fraction of (high - low) used as spread proxy.
        """
        self._frac = spread_fraction

    def apply(self, price: Decimal, is_buy: bool, candle: OHLCV) -> Decimal:
        half_spread = (candle.high - candle.low) * self._frac / Decimal("2")
        if is_buy:
            return price + half_spread
        return price - half_spread
