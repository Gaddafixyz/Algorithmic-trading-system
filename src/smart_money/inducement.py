"""
Smart Money Concepts: Inducement Detection.

Inducement is a false breakout / liquidity grab designed to trigger
retail stop-losses before the actual move. It appears as a brief
spike beyond a key level that quickly reverses.

Patterns:
1. Stop hunt above equal highs followed by rejection (bearish)
2. Stop hunt below equal lows followed by rejection (bullish)
3. False BOS that reverses within N bars

Identifying inducement helps avoid being on the wrong side of the move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.core.models import OHLCV
from src.smart_money.liquidity import LiquidityPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InducementEvent:
    """A detected inducement / manipulation event."""

    direction: str  # "bullish_inducement" or "bearish_inducement"
    swept_price: Decimal  # Level that was swept
    sweep_high: Decimal  # Wick high of the sweep candle
    sweep_low: Decimal  # Wick low of the sweep candle
    rejection_price: Decimal  # Close price showing rejection
    bar_index: int
    timestamp: datetime
    liquidity_pool: LiquidityPool | None = None

    @property
    def is_bullish(self) -> bool:
        """Bullish inducement = sweep of lows, expect up move."""
        return self.direction == "bullish_inducement"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "bearish_inducement"

    def __str__(self) -> str:
        return (
            f"Inducement({self.direction} swept={self.swept_price} "
            f"rejection={self.rejection_price} bar={self.bar_index})"
        )


def detect_inducement(
    candles: list[OHLCV],
    liquidity_pools: list[LiquidityPool],
    min_wick_body_ratio: float = 2.0,
) -> list[InducementEvent]:
    """
    Detect inducement events from swept liquidity pools.

    An inducement is confirmed when:
    1. Price sweeps a liquidity level (wick beyond)
    2. The candle closes back inside the range (rejection)
    3. The wick is at least `min_wick_body_ratio` × the candle body

    Args:
        candles: OHLCV series.
        liquidity_pools: Pre-identified liquidity zones.
        min_wick_body_ratio: Minimum ratio of wick to body for confirmation.

    Returns:
        List of inducement events.
    """
    events: list[InducementEvent] = []

    for i, candle in enumerate(candles):
        body = abs(candle.close - candle.open)
        if body == Decimal("0"):
            body = candle.range_size * Decimal("0.01")  # Doji: use 1% of range

        upper_wick = candle.upper_wick
        lower_wick = candle.lower_wick

        # Check each active liquidity pool
        for pool in liquidity_pools:
            if pool.is_swept or pool.bar_index >= i:
                continue

            if pool.is_buy_side:
                # Bearish inducement: wick above buy-side liquidity, close below
                if (
                    candle.high > pool.price
                    and candle.close < pool.price
                    and upper_wick > body * Decimal(str(min_wick_body_ratio))
                ):
                    events.append(
                        InducementEvent(
                            direction="bearish_inducement",
                            swept_price=pool.price,
                            sweep_high=candle.high,
                            sweep_low=candle.low,
                            rejection_price=candle.close,
                            bar_index=i,
                            timestamp=candle.timestamp,
                            liquidity_pool=pool,
                        )
                    )
                    pool.is_swept = True
                    pool.sweep_bar_index = i
                    pool.sweep_price = candle.high
                    logger.debug("Bearish inducement at bar %d: swept %s", i, pool.price)

            elif pool.is_sell_side:
                # Bullish inducement: wick below sell-side liquidity, close above
                if (
                    candle.low < pool.price
                    and candle.close > pool.price
                    and lower_wick > body * Decimal(str(min_wick_body_ratio))
                ):
                    events.append(
                        InducementEvent(
                            direction="bullish_inducement",
                            swept_price=pool.price,
                            sweep_high=candle.high,
                            sweep_low=candle.low,
                            rejection_price=candle.close,
                            bar_index=i,
                            timestamp=candle.timestamp,
                            liquidity_pool=pool,
                        )
                    )
                    pool.is_swept = True
                    pool.sweep_bar_index = i
                    pool.sweep_price = candle.low
                    logger.debug("Bullish inducement at bar %d: swept %s", i, pool.price)

    return events
