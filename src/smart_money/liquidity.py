"""
Smart Money Concepts: Liquidity Pool Identification.

Liquidity pools are areas where stop-loss orders and pending orders
accumulate. Smart money targets these areas to fill large orders.

Types:
- Equal Highs (EQH): Multiple swing highs at similar levels → buy-side liquidity
- Equal Lows (EQL): Multiple swing lows at similar levels → sell-side liquidity
- Previous day/week highs and lows
- Round numbers and psychological levels
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.core.enums import LiquidityType
from src.core.models import OHLCV
from src.smart_money.market_structure import SwingPoint

logger = logging.getLogger(__name__)


@dataclass
class LiquidityPool:
    """A zone of accumulated liquidity."""

    liq_type: LiquidityType
    price: Decimal  # Level where liquidity sits
    zone_top: Decimal  # Upper boundary of the zone
    zone_bottom: Decimal  # Lower boundary
    bar_index: int
    timestamp: datetime
    is_swept: bool = False
    sweep_bar_index: int | None = None
    sweep_price: Decimal | None = None
    confluence_count: int = 1  # How many touches created this pool
    tags: list[str] = field(default_factory=list)

    @property
    def is_buy_side(self) -> bool:
        """Buy-side liquidity sits above price (above equal highs)."""
        return self.liq_type in (
            LiquidityType.EQUAL_HIGHS,
            LiquidityType.BSL,
            LiquidityType.SWING_HIGH,
        )

    @property
    def is_sell_side(self) -> bool:
        """Sell-side liquidity sits below price (below equal lows)."""
        return self.liq_type in (
            LiquidityType.EQUAL_LOWS,
            LiquidityType.SSL,
            LiquidityType.SWING_LOW,
        )

    def check_sweep(self, candle: OHLCV, bar_index: int) -> bool:
        """
        Check if a candle sweeps this liquidity pool.

        A sweep = wick beyond the level followed by rejection (close back inside).
        """
        if self.is_swept:
            return True

        if self.is_buy_side:
            # Buy-side swept when price wicks above the level but closes below
            if candle.high > self.price and candle.close < self.price:
                self.is_swept = True
                self.sweep_bar_index = bar_index
                self.sweep_price = candle.high
                logger.debug("Buy-side liquidity swept at %s (bar %d)", self.price, bar_index)
                return True
        elif candle.low < self.price and candle.close > self.price:
            self.is_swept = True
            self.sweep_bar_index = bar_index
            self.sweep_price = candle.low
            logger.debug("Sell-side liquidity swept at %s (bar %d)", self.price, bar_index)
            return True

        return False

    def __str__(self) -> str:
        status = "SWEPT" if self.is_swept else "ACTIVE"
        return f"Liquidity({self.liq_type} @ {self.price} [{status}])"


def identify_equal_highs(
    swing_highs: list[SwingPoint],
    tolerance_pct: float = 0.1,
) -> list[LiquidityPool]:
    """
    Identify equal highs (buy-side liquidity clusters).

    Two swing highs are "equal" if their prices are within `tolerance_pct`%
    of each other.
    """
    pools: list[LiquidityPool] = []
    used: set[int] = set()

    for i, sh1 in enumerate(swing_highs):
        if i in used:
            continue
        cluster = [sh1]
        for j, sh2 in enumerate(swing_highs[i + 1 :], start=i + 1):
            if j in used:
                continue
            diff_pct = abs(float(sh1.price - sh2.price)) / float(sh1.price) * 100
            if diff_pct <= tolerance_pct:
                cluster.append(sh2)
                used.add(j)

        if len(cluster) >= 2:
            avg_price = sum(s.price for s in cluster) / Decimal(str(len(cluster)))
            tolerance = avg_price * Decimal(str(tolerance_pct / 100))
            pool = LiquidityPool(
                liq_type=LiquidityType.EQUAL_HIGHS,
                price=avg_price,
                zone_top=avg_price + tolerance,
                zone_bottom=avg_price - tolerance,
                bar_index=cluster[-1].bar_index,
                timestamp=cluster[-1].timestamp,
                confluence_count=len(cluster),
            )
            pools.append(pool)
            logger.debug("Equal highs cluster at %s (%d touches)", avg_price, len(cluster))

    return pools


def identify_equal_lows(
    swing_lows: list[SwingPoint],
    tolerance_pct: float = 0.1,
) -> list[LiquidityPool]:
    """Identify equal lows (sell-side liquidity clusters)."""
    pools: list[LiquidityPool] = []
    used: set[int] = set()

    for i, sl1 in enumerate(swing_lows):
        if i in used:
            continue
        cluster = [sl1]
        for j, sl2 in enumerate(swing_lows[i + 1 :], start=i + 1):
            if j in used:
                continue
            diff_pct = abs(float(sl1.price - sl2.price)) / float(sl1.price) * 100
            if diff_pct <= tolerance_pct:
                cluster.append(sl2)
                used.add(j)

        if len(cluster) >= 2:
            avg_price = sum(s.price for s in cluster) / Decimal(str(len(cluster)))
            tolerance = avg_price * Decimal(str(tolerance_pct / 100))
            pool = LiquidityPool(
                liq_type=LiquidityType.EQUAL_LOWS,
                price=avg_price,
                zone_top=avg_price + tolerance,
                zone_bottom=avg_price - tolerance,
                bar_index=cluster[-1].bar_index,
                timestamp=cluster[-1].timestamp,
                confluence_count=len(cluster),
            )
            pools.append(pool)

    return pools


def identify_swing_liquidity(
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
) -> list[LiquidityPool]:
    """Create liquidity pools at every significant swing point."""
    pools: list[LiquidityPool] = []

    for sh in swing_highs:
        pools.append(
            LiquidityPool(
                liq_type=LiquidityType.SWING_HIGH,
                price=sh.price,
                zone_top=sh.price * Decimal("1.001"),
                zone_bottom=sh.price * Decimal("0.999"),
                bar_index=sh.bar_index,
                timestamp=sh.timestamp,
            )
        )

    for sl in swing_lows:
        pools.append(
            LiquidityPool(
                liq_type=LiquidityType.SWING_LOW,
                price=sl.price,
                zone_top=sl.price * Decimal("1.001"),
                zone_bottom=sl.price * Decimal("0.999"),
                bar_index=sl.bar_index,
                timestamp=sl.timestamp,
            )
        )

    return pools


def update_liquidity_sweeps(
    pools: list[LiquidityPool],
    candles: list[OHLCV],
    start_bar: int = 0,
) -> None:
    """Update sweep status for all liquidity pools."""
    for pool in pools:
        if pool.is_swept:
            continue
        for i in range(max(start_bar, pool.bar_index + 1), len(candles)):
            if pool.check_sweep(candles[i], i):
                break


def get_active_liquidity(pools: list[LiquidityPool]) -> list[LiquidityPool]:
    return [p for p in pools if not p.is_swept]
