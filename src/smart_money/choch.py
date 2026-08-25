"""
Change of Character (CHoCH) detection.

A CHoCH signals a potential reversal of the prevailing trend:
- In an uptrend: price closes below the most recent higher low (bearish CHoCH)
- In a downtrend: price closes above the most recent lower high (bullish CHoCH)

CHoCH is the first sign that the market may be switching bias.
A subsequent BOS in the new direction confirms the structural shift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.core.enums import MarketStructure
from src.core.models import OHLCV
from src.smart_money.market_structure import MarketStructureState, SwingPoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CHoCHEvent:
    """A confirmed Change of Character."""

    direction: str          # "bullish" (reversal up) or "bearish" (reversal down)
    broken_price: Decimal   # The swing level that was broken
    break_candle_index: int
    break_price: Decimal
    swing_point: SwingPoint
    timestamp: datetime
    prior_structure: MarketStructure

    @property
    def is_bullish(self) -> bool:
        return self.direction == "bullish"

    @property
    def is_bearish(self) -> bool:
        return self.direction == "bearish"

    def __str__(self) -> str:
        return (
            f"CHoCH({self.direction.upper()} broke {self.broken_price} "
            f"from {self.prior_structure} @ {self.timestamp.strftime('%Y-%m-%d %H:%M')})"
        )


def detect_choch(
    candles: list[OHLCV],
    structure: MarketStructureState,
    *,
    require_close: bool = True,
) -> list[CHoCHEvent]:
    """
    Scan for CHoCH events against the current market structure.

    In a bullish structure, look for bearish CHoCH (break of a HL).
    In a bearish structure, look for bullish CHoCH (break of a LH).

    Args:
        candles: OHLCV series.
        structure: Current market structure state with swing points.
        require_close: Require candle close beyond level.

    Returns:
        List of CHoCH events.
    """
    events: list[CHoCHEvent] = []
    if structure.structure == MarketStructure.UNKNOWN:
        return events

    broken_indices: set[int] = set()

    for i, candle in enumerate(candles):
        # Bearish CHoCH in bullish structure: break of a higher low
        if structure.structure == MarketStructure.BULLISH:
            price = candle.close if require_close else candle.low
            for sl in structure.swing_lows:
                if sl.bar_index in broken_indices or sl.bar_index >= i:
                    continue
                if price < sl.price:
                    events.append(CHoCHEvent(
                        direction="bearish",
                        broken_price=sl.price,
                        break_candle_index=i,
                        break_price=price,
                        swing_point=sl,
                        timestamp=candle.timestamp,
                        prior_structure=MarketStructure.BULLISH,
                    ))
                    broken_indices.add(sl.bar_index)
                    logger.debug("Bearish CHoCH at bar %d: broke HL %s", i, sl.price)

        # Bullish CHoCH in bearish structure: break of a lower high
        elif structure.structure == MarketStructure.BEARISH:
            price = candle.close if require_close else candle.high
            for sh in structure.swing_highs:
                if sh.bar_index in broken_indices or sh.bar_index >= i:
                    continue
                if price > sh.price:
                    events.append(CHoCHEvent(
                        direction="bullish",
                        broken_price=sh.price,
                        break_candle_index=i,
                        break_price=price,
                        swing_point=sh,
                        timestamp=candle.timestamp,
                        prior_structure=MarketStructure.BEARISH,
                    ))
                    broken_indices.add(sh.bar_index)
                    logger.debug("Bullish CHoCH at bar %d: broke LH %s", i, sh.price)

    return sorted(events, key=lambda e: e.break_candle_index)


def latest_choch(
    candles: list[OHLCV],
    structure: MarketStructureState,
) -> CHoCHEvent | None:
    """Return the most recent CHoCH, or None."""
    events = detect_choch(candles, structure)
    return events[-1] if events else None
