"""
Break of Structure (BOS) detection.

A BOS occurs when price closes beyond a significant swing high (bullish BOS)
or swing low (bearish BOS), confirming continuation of the prevailing trend.

BOS ≠ CHoCH:
- BOS = continuation: price breaks the same-direction previous swing
- CHoCH = reversal: price breaks the opposite-direction swing

References: ICT methodology, Smart Money Concepts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.core.models import OHLCV
from src.smart_money.market_structure import SwingPoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BOSEvent:
    """A confirmed Break of Structure."""

    direction: str              # "bullish" or "bearish"
    broken_price: Decimal       # The swing level that was broken
    break_candle_index: int     # Index of the candle that broke the level
    break_price: Decimal        # Close price of the breaking candle
    swing_point: SwingPoint     # The swing that was breached
    timestamp: datetime

    @property
    def is_bullish(self) -> bool:
        return self.direction == "bullish"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "bearish"

    def __str__(self) -> str:
        return (
            f"BOS({self.direction.upper()} broke {self.broken_price} "
            f"@ {self.timestamp.strftime('%Y-%m-%d %H:%M')})"
        )


def detect_bos(
    candles: list[OHLCV],
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
    *,
    require_close: bool = True,
) -> list[BOSEvent]:
    """
    Scan candles for Break of Structure events.

    A bullish BOS: close above last confirmed swing high.
    A bearish BOS: close below last confirmed swing low.

    Args:
        candles: OHLCV series (must be in chronological order).
        swing_highs: Pre-computed swing highs.
        swing_lows: Pre-computed swing lows.
        require_close: If True, require a candle close beyond the level
                       (not just a wick), which is more reliable.

    Returns:
        List of BOS events ordered by bar index.
    """
    events: list[BOSEvent] = []
    if not swing_highs or not swing_lows:
        return events

    # Track which swing levels have already been broken to avoid duplicates
    broken_high_indices: set[int] = set()
    broken_low_indices: set[int] = set()

    for i, candle in enumerate(candles):
        price = candle.close if require_close else candle.high

        # Check for bullish BOS: close above a previous swing high
        for sh in swing_highs:
            if sh.bar_index in broken_high_indices:
                continue
            if sh.bar_index >= i:
                continue  # Swing must precede the break candle
            if price > sh.price:
                events.append(BOSEvent(
                    direction="bullish",
                    broken_price=sh.price,
                    break_candle_index=i,
                    break_price=price,
                    swing_point=sh,
                    timestamp=candle.timestamp,
                ))
                broken_high_indices.add(sh.bar_index)
                logger.debug("Bullish BOS: %s broken at bar %d", sh.price, i)

        # Check for bearish BOS: close below a previous swing low
        low_price = candle.close if require_close else candle.low
        for sl in swing_lows:
            if sl.bar_index in broken_low_indices:
                continue
            if sl.bar_index >= i:
                continue
            if low_price < sl.price:
                events.append(BOSEvent(
                    direction="bearish",
                    broken_price=sl.price,
                    break_candle_index=i,
                    break_price=low_price,
                    swing_point=sl,
                    timestamp=candle.timestamp,
                ))
                broken_low_indices.add(sl.bar_index)
                logger.debug("Bearish BOS: %s broken at bar %d", sl.price, i)

    return sorted(events, key=lambda e: e.break_candle_index)


def latest_bos(
    candles: list[OHLCV],
    swing_highs: list[SwingPoint],
    swing_lows: list[SwingPoint],
) -> BOSEvent | None:
    """Return the most recent BOS event, or None."""
    events = detect_bos(candles, swing_highs, swing_lows)
    return events[-1] if events else None
