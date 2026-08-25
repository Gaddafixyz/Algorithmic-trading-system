"""Signal scoring utilities."""

from __future__ import annotations

from src.core.enums import SignalStrength
from src.domain.signal import Signal


def classify_strength(score: float) -> SignalStrength:
    """Map a 0-100 score to a strength label."""
    if score >= 80:
        return SignalStrength.VERY_STRONG
    if score >= 65:
        return SignalStrength.STRONG
    if score >= 50:
        return SignalStrength.MODERATE
    return SignalStrength.WEAK


def position_size_multiplier(signal: Signal) -> float:
    """
    Return a position size multiplier based on signal strength.

    Very strong signals get full size; weak signals get reduced size.
    """
    multipliers = {
        SignalStrength.VERY_STRONG: 1.0,
        SignalStrength.STRONG: 0.75,
        SignalStrength.MODERATE: 0.5,
        SignalStrength.WEAK: 0.25,
    }
    return multipliers.get(signal.strength, 0.5)
