"""Simple Moving Average (SMA)."""

from __future__ import annotations

import numpy as np

from src.core.exceptions import InsufficientDataError
from src.core.models import OHLCV


def calculate_sma(values: list[float] | np.ndarray, period: int) -> np.ndarray:
    """Calculate SMA. Returns NaN for first (period-1) values."""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < period:
        raise InsufficientDataError(required=period, available=n, context=f"SMA({period})")

    sma = np.full(n, np.nan)
    for i in range(period - 1, n):
        sma[i] = np.mean(arr[i - period + 1 : i + 1])
    return sma


def sma_from_candles(candles: list[OHLCV], period: int) -> np.ndarray:
    return calculate_sma([float(c.close) for c in candles], period)


def current_sma(candles: list[OHLCV], period: int) -> float:
    return float(sma_from_candles(candles, period)[-1])
