"""Entry execution logic: limit vs market, entry zone validation."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import OrderSide, OrderType, SignalDirection
from src.domain.signal import Signal


def get_order_side(direction: SignalDirection) -> OrderSide:
    return OrderSide.BUY if direction == SignalDirection.LONG else OrderSide.SELL


def get_entry_order_type(signal: Signal, use_limit: bool = True) -> OrderType:
    """Choose limit or market entry based on configuration and signal type."""
    if not use_limit:
        return OrderType.MARKET
    if signal.entry_zone_high and signal.entry_zone_low:
        return OrderType.LIMIT
    return OrderType.MARKET


def is_entry_valid(
    signal: Signal,
    current_price: Decimal,
    tolerance_atr: Decimal | None = None,
) -> bool:
    """
    Validate that the current price allows entry at the signal's level.

    Returns True if price is close enough to the intended entry.
    """
    if signal.is_expired():
        return False

    if signal.entry_zone_high and signal.entry_zone_low:
        return signal.entry_zone_low <= current_price <= signal.entry_zone_high

    # Without a zone, allow entry within 0.3% of entry price
    tolerance = tolerance_atr or (signal.entry_price * Decimal("0.003"))
    return abs(current_price - signal.entry_price) <= tolerance
