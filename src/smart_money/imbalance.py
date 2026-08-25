"""
Smart Money Concepts: Imbalance Zones.

Imbalances are similar to FVGs but track all gap-like price areas
including single-candle imbalances (large body candles with no overlap
with adjacent candles) and volume imbalances.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime

from src.core.models import OHLCV


@dataclass
class Imbalance:
    """A price imbalance / inefficiency zone."""

    direction: str  # "bullish" or "bearish"
    top: Decimal
    bottom: Decimal
    bar_index: int
    timestamp: datetime
    is_filled: bool = False

    @property
    def size(self) -> Decimal:
        return self.top - self.bottom

    @property
    def mid(self) -> Decimal:
        return (self.top + self.bottom) / Decimal("2")

    def contains_price(self, price: Decimal) -> bool:
        return self.bottom <= price <= self.top


def detect_imbalances(candles: list[OHLCV]) -> list[Imbalance]:
    """
    Detect all price imbalances in the candle series.

    An imbalance exists when adjacent candle bodies do not overlap.
    This is identical to FVG detection but uses body levels (open/close)
    rather than high/low, capturing a slightly different set of areas.
    """
    imbalances: list[Imbalance] = []
    n = len(candles)

    for i in range(1, n - 1):
        c1, c2, c3 = candles[i - 1], candles[i], candles[i + 1]

        # Bullish imbalance: c3 body bottom > c1 body top
        c1_body_top = max(c1.open, c1.close)
        c3_body_bottom = min(c3.open, c3.close)

        if c3_body_bottom > c1_body_top:
            imbalances.append(Imbalance(
                direction="bullish",
                top=c3_body_bottom,
                bottom=c1_body_top,
                bar_index=i,
                timestamp=c2.timestamp,
            ))

        # Bearish imbalance: c3 body top < c1 body bottom
        c1_body_bottom = min(c1.open, c1.close)
        c3_body_top = max(c3.open, c3.close)

        if c3_body_top < c1_body_bottom:
            imbalances.append(Imbalance(
                direction="bearish",
                top=c1_body_bottom,
                bottom=c3_body_top,
                bar_index=i,
                timestamp=c2.timestamp,
            ))

    return imbalances
