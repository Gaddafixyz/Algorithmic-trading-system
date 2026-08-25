"""Volume-based indicators."""

from __future__ import annotations

import numpy as np

from src.core.models import OHLCV
from src.indicators.sma import calculate_sma


def volume_sma(candles: list[OHLCV], period: int = 20) -> np.ndarray:
    """Simple moving average of volume."""
    vols = [float(c.volume) for c in candles]
    return calculate_sma(vols, period)


def relative_volume(candles: list[OHLCV], period: int = 20) -> float:
    """
    Current bar volume relative to its SMA.

    > 1.0 means above-average volume; > 2.0 is a high-volume bar.
    """
    # Was a duplicate inline copy of volume_sma's body (vols = [...]; sma =
    # calculate_sma(vols, period)) — volume_sma() sat completely unused
    # despite this function being its one obvious caller. Same computation,
    # now shared.
    vols = [float(c.volume) for c in candles]
    sma = volume_sma(candles, period)
    avg = sma[-1]
    if avg == 0.0 or np.isnan(avg):
        return 1.0
    return vols[-1] / avg


def obv(candles: list[OHLCV]) -> np.ndarray:
    """On-Balance Volume."""
    n = len(candles)
    result = np.zeros(n)
    result[0] = float(candles[0].volume)
    for i in range(1, n):
        if candles[i].close > candles[i - 1].close:
            result[i] = result[i - 1] + float(candles[i].volume)
        elif candles[i].close < candles[i - 1].close:
            result[i] = result[i - 1] - float(candles[i].volume)
        else:
            result[i] = result[i - 1]
    return result


def vwap(candles: list[OHLCV]) -> float:
    """
    Volume-weighted average price.

    Computes across all provided candles (session VWAP when daily candles passed).
    """
    numerator = sum(float(c.typical_price) * float(c.volume) for c in candles)
    denominator = sum(float(c.volume) for c in candles)
    if denominator == 0.0:
        return float(candles[-1].close)
    return numerator / denominator


def is_high_volume_bar(candles: list[OHLCV], period: int = 20, threshold: float = 1.5) -> bool:
    """True if the latest bar's volume is >= threshold * average volume."""
    return relative_volume(candles, period) >= threshold
