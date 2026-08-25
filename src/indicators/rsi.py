"""
Relative Strength Index (RSI) indicator.

Uses Wilder's smoothing (same as ATR), matching TradingView's output.
"""

from __future__ import annotations

import numpy as np

from src.core.constants import DEFAULT_RSI_PERIOD
from src.core.exceptions import InsufficientDataError
from src.core.models import OHLCV


def calculate_rsi(
    closes: list[float] | np.ndarray,
    period: int = DEFAULT_RSI_PERIOD,
) -> np.ndarray:
    """
    Calculate RSI using Wilder's RMA smoothing.

    Returns array of same length; first `period` values are NaN.
    """
    arr = np.asarray(closes, dtype=float)
    n = len(arr)

    if n < period + 1:
        raise InsufficientDataError(
            required=period + 1,
            available=n,
            context=f"RSI({period})",
        )

    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    rsi = np.full(n, np.nan)

    # Seed with simple average
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    def _rsi_from_avg(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = _rsi_from_avg(avg_gain, avg_loss)

    for i in range(1, len(deltas) - period + 1):
        idx = period + i
        avg_gain = (avg_gain * (period - 1) + gains[period + i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[period + i - 1]) / period
        rsi[idx] = _rsi_from_avg(avg_gain, avg_loss)

    return rsi


def rsi_from_candles(
    candles: list[OHLCV],
    period: int = DEFAULT_RSI_PERIOD,
) -> np.ndarray:
    """Calculate RSI from OHLCV candles using close prices."""
    closes = [float(c.close) for c in candles]
    return calculate_rsi(closes, period)


def current_rsi(candles: list[OHLCV], period: int = DEFAULT_RSI_PERIOD) -> float:
    """Return the single latest RSI value."""
    return float(rsi_from_candles(candles, period)[-1])


def is_overbought(rsi_value: float, threshold: float = 70.0) -> bool:
    return rsi_value >= threshold


def is_oversold(rsi_value: float, threshold: float = 30.0) -> bool:
    return rsi_value <= threshold
