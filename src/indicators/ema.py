"""
Exponential Moving Average (EMA) indicator.

Uses standard EMA with alpha = 2 / (period + 1).
Seeded with SMA for first value (standard practice).
"""

from __future__ import annotations

import numpy as np

from src.core.exceptions import InsufficientDataError
from src.core.models import OHLCV


def calculate_ema(
    values: list[float] | np.ndarray,
    period: int,
) -> np.ndarray:
    """
    Calculate EMA for a series of values.

    Returns array of same length; first (period - 1) values are NaN.
    """
    arr = np.asarray(values, dtype=float)
    n = len(arr)

    if n < period:
        raise InsufficientDataError(
            required=period,
            available=n,
            context=f"EMA({period})",
        )

    ema = np.full(n, np.nan)
    alpha = 2.0 / (period + 1)

    # Seed with SMA
    ema[period - 1] = np.mean(arr[:period])
    for i in range(period, n):
        ema[i] = arr[i] * alpha + ema[i - 1] * (1.0 - alpha)

    return ema


def ema_from_candles(
    candles: list[OHLCV],
    period: int,
    source: str = "close",
) -> np.ndarray:
    """Calculate EMA from OHLCV candles using the specified price source."""
    prices = _extract_prices(candles, source)
    return calculate_ema(prices, period)


def _extract_prices(candles: list[OHLCV], source: str) -> list[float]:
    mapping = {
        "open": lambda c: float(c.open),
        "high": lambda c: float(c.high),
        "low": lambda c: float(c.low),
        "close": lambda c: float(c.close),
        "hl2": lambda c: (float(c.high) + float(c.low)) / 2,
        "hlc3": lambda c: (float(c.high) + float(c.low) + float(c.close)) / 3,
        "ohlc4": lambda c: (float(c.open) + float(c.high) + float(c.low) + float(c.close)) / 4,
    }
    extractor = mapping.get(source)
    if extractor is None:
        raise ValueError(f"Unknown price source: {source!r}. Valid: {list(mapping.keys())}")
    return [extractor(c) for c in candles]
