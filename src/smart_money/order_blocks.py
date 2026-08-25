"""
Smart Money Concepts: Order Block Detection.

An Order Block (OB) is the last opposing candle before a significant
impulsive move that broke market structure (BOS). It represents an
area where institutional orders were placed.

Bullish OB: Last bearish (down) candle before a bullish BOS move.
Bearish OB: Last bullish (up) candle before a bearish BOS move.

OBs are considered valid until:
- Price trades through the full body (mitigated)
- They exceed a maximum age (SMC_OB_MAX_AGE_BARS)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.core.constants import SMC_OB_MAX_AGE_BARS
from src.core.enums import OrderBlockType
from src.core.models import OHLCV
from src.smart_money.bos import BOSEvent

logger = logging.getLogger(__name__)


@dataclass
class OrderBlock:
    """
    An institutional order block zone.

    The zone is defined by the candle's body (open/close).
    Wicks are noted but the body is the key area.
    """

    ob_type: OrderBlockType
    top: Decimal  # Upper boundary of the OB zone
    bottom: Decimal  # Lower boundary of the OB zone
    origin_high: Decimal  # Full candle high (for reference)
    origin_low: Decimal  # Full candle low
    bar_index: int
    timestamp: datetime
    bos_event: BOSEvent  # The BOS that validated this OB
    is_mitigated: bool = False
    mitigation_bar_index: int | None = None
    mitigation_price: Decimal | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def mid(self) -> Decimal:
        return (self.top + self.bottom) / Decimal("2")

    @property
    def size(self) -> Decimal:
        return self.top - self.bottom

    def contains_price(self, price: Decimal) -> bool:
        """True if price is within the OB zone."""
        return self.bottom <= price <= self.top

    def is_valid(self, current_bar_index: int, max_age_bars: int | None = None) -> bool:
        """
        True if OB is unmitigated and within max age.

        max_age_bars defaults to SMC_OB_MAX_AGE_BARS when omitted — see
        configs/strategy.yaml's `smc.ob_max_age_bars`, threaded through
        via get_active_order_blocks()/score_confluence().
        """
        if self.is_mitigated:
            return False
        age_limit = max_age_bars if max_age_bars is not None else SMC_OB_MAX_AGE_BARS
        return (current_bar_index - self.bar_index) <= age_limit

    def check_mitigation(self, candle: OHLCV, bar_index: int) -> bool:
        """
        Check if this candle mitigates (invalidates) the order block.

        Mitigation = price trades through 50% of the OB body.
        Some traders use full body close; we use 50% as the trigger.
        """
        if self.is_mitigated:
            return True

        fifty_pct = (self.top + self.bottom) / Decimal("2")

        if self.ob_type == OrderBlockType.BULLISH:
            # Bullish OB mitigated when price trades below 50% of body
            if candle.low <= fifty_pct:
                self.is_mitigated = True
                self.mitigation_bar_index = bar_index
                self.mitigation_price = candle.low
                logger.debug(
                    "Bullish OB mitigated at bar %d (price=%s, OB bottom=%s)",
                    bar_index,
                    candle.low,
                    self.bottom,
                )
                return True
        elif candle.high >= fifty_pct:
            self.is_mitigated = True
            self.mitigation_bar_index = bar_index
            self.mitigation_price = candle.high
            logger.debug(
                "Bearish OB mitigated at bar %d (price=%s, OB top=%s)",
                bar_index,
                candle.high,
                self.top,
            )
            return True

        return False

    def __str__(self) -> str:
        status = "MITIGATED" if self.is_mitigated else "ACTIVE"
        return f"OB({self.ob_type} {self.bottom}-{self.top} " f"bar={self.bar_index} [{status}])"


def identify_order_blocks(
    candles: list[OHLCV],
    bos_events: list[BOSEvent],
    *,
    max_lookback: int = 5,
) -> list[OrderBlock]:
    """
    Identify order blocks from BOS events.

    For each BOS, scan backward to find the last opposing candle
    before the impulsive move began.

    Args:
        candles: Full OHLCV series.
        bos_events: Pre-computed BOS events.
        max_lookback: How many bars before the BOS to search for the OB candle.

    Returns:
        List of order blocks, most recent first.
    """
    order_blocks: list[OrderBlock] = []

    for bos in bos_events:
        break_idx = bos.break_candle_index
        ob_candle_idx: int | None = None
        ob_type: OrderBlockType | None = None

        if bos.is_bullish:
            # Find last bearish candle before the bullish BOS move
            search_start = max(0, break_idx - max_lookback - 1)
            for j in range(break_idx - 1, search_start - 1, -1):
                if candles[j].is_bearish:
                    ob_candle_idx = j
                    ob_type = OrderBlockType.BULLISH
                    break
        else:
            # Find last bullish candle before the bearish BOS move
            search_start = max(0, break_idx - max_lookback - 1)
            for j in range(break_idx - 1, search_start - 1, -1):
                if candles[j].is_bullish:
                    ob_candle_idx = j
                    ob_type = OrderBlockType.BEARISH
                    break

        if ob_candle_idx is None or ob_type is None:
            continue

        candle = candles[ob_candle_idx]

        # OB zone = candle body (open to close)
        body_top = max(candle.open, candle.close)
        body_bottom = min(candle.open, candle.close)

        # Avoid duplicate OBs at the same bar
        if any(ob.bar_index == ob_candle_idx for ob in order_blocks):
            continue

        ob = OrderBlock(
            ob_type=ob_type,
            top=body_top,
            bottom=body_bottom,
            origin_high=candle.high,
            origin_low=candle.low,
            bar_index=ob_candle_idx,
            timestamp=candle.timestamp,
            bos_event=bos,
        )
        order_blocks.append(ob)
        logger.debug(
            "Identified %s OB at bar %d: %s-%s", ob_type, ob_candle_idx, body_bottom, body_top
        )

    return sorted(order_blocks, key=lambda o: o.bar_index, reverse=True)


def update_order_block_mitigation(
    order_blocks: list[OrderBlock],
    candles: list[OHLCV],
    start_bar: int = 0,
) -> None:
    """
    Update mitigation status for all order blocks against new candles.

    Mutates order blocks in-place. Call after each new candle.
    """
    for ob in order_blocks:
        if ob.is_mitigated:
            continue
        for i in range(max(start_bar, ob.bar_index + 1), len(candles)):
            if ob.check_mitigation(candles[i], i):
                break


def get_active_order_blocks(
    order_blocks: list[OrderBlock],
    current_bar_index: int,
    max_age_bars: int | None = None,
) -> list[OrderBlock]:
    """
    Return unmitigated order blocks within max age.

    max_age_bars defaults to SMC_OB_MAX_AGE_BARS (via OrderBlock.is_valid)
    when omitted.
    """
    return [ob for ob in order_blocks if ob.is_valid(current_bar_index, max_age_bars)]
