"""Stop-loss calculation and management."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import SignalDirection
from src.domain.signal import Signal


def atr_stop_loss(
    entry_price: Decimal,
    direction: SignalDirection,
    atr: Decimal,
    multiplier: Decimal = Decimal("1.5"),
) -> Decimal:
    """Calculate ATR-based stop loss."""
    if direction == SignalDirection.LONG:
        return entry_price - (atr * multiplier)
    return entry_price + (atr * multiplier)


def structure_stop_loss(
    entry_price: Decimal,
    direction: SignalDirection,
    swing_level: Decimal,
    buffer_pct: Decimal = Decimal("0.001"),
) -> Decimal:
    """
    Place stop below/above the nearest swing structure level.

    Buffer moves the stop slightly beyond the structure to avoid
    premature stops from noise.
    """
    buffer = swing_level * buffer_pct
    if direction == SignalDirection.LONG:
        return swing_level - buffer
    return swing_level + buffer


def ob_stop_loss(
    direction: SignalDirection,
    ob_top: Decimal,
    ob_bottom: Decimal,
    buffer_pct: Decimal = Decimal("0.001"),
) -> Decimal:
    """Place stop beyond the order block boundary."""
    if direction == SignalDirection.LONG:
        return ob_bottom * (Decimal("1") - buffer_pct)
    return ob_top * (Decimal("1") + buffer_pct)


def validate_stop_risk(
    entry_price: Decimal,
    stop_loss: Decimal,
    equity: Decimal,
    risk_pct: Decimal,
    max_stop_pct: Decimal = Decimal("5.0"),
    min_stop_pct: Decimal = Decimal("0.05"),
) -> bool:
    """
    Validate stop-loss distance is within acceptable bounds.

    Returns False if the stop is too close or too far. ``equity`` and
    ``risk_pct`` are accepted for call-site symmetry with position sizing
    (and future extension to a notional/equity-relative check) but the
    current bound is purely distance-based as a % of entry price.
    """
    if entry_price <= Decimal("0"):
        return False
    stop_dist_pct = abs(entry_price - stop_loss) / entry_price * Decimal("100")
    if stop_dist_pct > max_stop_pct:
        return False
    if stop_dist_pct < min_stop_pct:
        return False
    return True
