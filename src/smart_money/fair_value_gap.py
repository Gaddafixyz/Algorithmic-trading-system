"""
Smart Money Concepts: Fair Value Gap (FVG) / Imbalance Detection.

A Fair Value Gap (also called an imbalance or inefficiency) is a
three-candle pattern where the ranges of candle 1 and candle 3
do not overlap, leaving a "gap" that price tends to return to fill.

Bullish FVG: Candle 3 low > Candle 1 high (gap above candle 1)
Bearish FVG: Candle 3 high < Candle 1 low (gap below candle 1)

FVGs are POIs (Points of Interest) where price may retrace before
continuing in the prevailing direction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.core.constants import SMC_FVG_MIN_SIZE_ATR_MULT
from src.core.enums import FVGType
from src.core.models import OHLCV

logger = logging.getLogger(__name__)


@dataclass
class FairValueGap:
    """A detected fair value gap / imbalance zone."""

    fvg_type: FVGType
    top: Decimal  # Upper boundary of the gap
    bottom: Decimal  # Lower boundary of the gap
    bar_index: int  # Middle candle (candle 2) index
    timestamp: datetime
    is_filled: bool = False
    fill_bar_index: int | None = None
    is_partial_fill: bool = False
    tags: list[str] = field(default_factory=list)

    @property
    def size(self) -> Decimal:
        return self.top - self.bottom

    @property
    def mid(self) -> Decimal:
        return (self.top + self.bottom) / Decimal("2")

    def contains_price(self, price: Decimal) -> bool:
        return self.bottom <= price <= self.top

    def check_fill(self, candle: OHLCV, bar_index: int) -> bool:
        """
        Check if a candle fills (closes inside) this FVG.

        A FVG is considered filled when price trades to the 50% level.
        Full fill = price closes through the entire gap.
        """
        if self.is_filled:
            return True

        mid = self.mid

        if self.fvg_type == FVGType.BULLISH:
            # Bullish FVG filled when price trades down to midpoint
            if candle.low <= mid:
                self.is_partial_fill = True
            if candle.low <= self.bottom:
                self.is_filled = True
                self.fill_bar_index = bar_index
                return True
        else:
            # Bearish FVG filled when price trades up to midpoint
            if candle.high >= mid:
                self.is_partial_fill = True
            if candle.high >= self.top:
                self.is_filled = True
                self.fill_bar_index = bar_index
                return True

        return False

    def __str__(self) -> str:
        status = "FILLED" if self.is_filled else "OPEN"
        return f"FVG({self.fvg_type} {self.bottom}-{self.top} [{status}])"


def detect_fvgs(
    candles: list[OHLCV],
    min_size_atr_mult: float = SMC_FVG_MIN_SIZE_ATR_MULT,
    atr_values: list[float] | None = None,
) -> list[FairValueGap]:
    """
    Scan candle series for Fair Value Gaps.

    Requires at least 3 candles. FVGs are identified on the middle
    candle (index 1 of the 3-candle pattern).

    Args:
        candles: OHLCV series in chronological order.
        min_size_atr_mult: Minimum FVG size as a multiple of ATR.
                           Filters out micro-gaps that are noise.
        atr_values: ATR at each bar (same length as candles). If None,
                    no minimum-size filter is applied.

    Returns:
        List of FVGs ordered by bar index.
    """
    fvgs: list[FairValueGap] = []
    n = len(candles)

    for i in range(1, n - 1):
        c1, c2, c3 = candles[i - 1], candles[i], candles[i + 1]

        # Bullish FVG: gap between c1 high and c3 low
        if c3.low > c1.high:
            gap_size = c3.low - c1.high

            # ATR filter
            if atr_values is not None:
                atr = atr_values[i]
                if atr > 0 and float(gap_size) < atr * float(min_size_atr_mult):
                    continue

            fvgs.append(
                FairValueGap(
                    fvg_type=FVGType.BULLISH,
                    top=c3.low,
                    bottom=c1.high,
                    bar_index=i,
                    timestamp=c2.timestamp,
                )
            )

        # Bearish FVG: gap between c3 high and c1 low
        elif c3.high < c1.low:
            gap_size = c1.low - c3.high

            if atr_values is not None:
                atr = atr_values[i]
                if atr > 0 and float(gap_size) < atr * float(min_size_atr_mult):
                    continue

            fvgs.append(
                FairValueGap(
                    fvg_type=FVGType.BEARISH,
                    top=c1.low,
                    bottom=c3.high,
                    bar_index=i,
                    timestamp=c2.timestamp,
                )
            )

    logger.debug("Detected %d FVGs in %d candles", len(fvgs), n)
    return fvgs


def update_fvg_fills(
    fvgs: list[FairValueGap],
    candles: list[OHLCV],
    start_bar: int = 0,
) -> None:
    """Update fill status for all FVGs against recent candles."""
    for fvg in fvgs:
        if fvg.is_filled:
            continue
        for i in range(max(start_bar, fvg.bar_index + 2), len(candles)):
            if fvg.check_fill(candles[i], i):
                break


def get_open_fvgs(fvgs: list[FairValueGap]) -> list[FairValueGap]:
    return [f for f in fvgs if not f.is_filled]


def fvg_at_price(
    fvgs: list[FairValueGap],
    price: Decimal,
) -> list[FairValueGap]:
    """Return all open FVGs that contain the given price."""
    return [f for f in fvgs if not f.is_filled and f.contains_price(price)]
