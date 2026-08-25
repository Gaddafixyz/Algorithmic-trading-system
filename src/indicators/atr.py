"""
Average True Range (ATR) indicator.

Uses Wilder's smoothing (RMA) — the canonical version used in
TradingView, most charting platforms, and SMC methodology.
"""

from __future__ import annotations

import numpy as np

from src.core.constants import DEFAULT_ATR_PERIOD
from src.core.exceptions import InsufficientDataError
from src.core.models import OHLCV


def true_range(high: float, low: float, prev_close: float) -> float:
    """Single-bar True Range."""
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def calculate_atr(
    candles: list[OHLCV],
    period: int = DEFAULT_ATR_PERIOD,
) -> np.ndarray:
    """
    Calculate ATR using Wilder's RMA smoothing.

    Returns array aligned with input candles (first `period` values are NaN).
    Minimum input: period + 1 candles.
    """
    n = len(candles)
    if n < period + 1:
        raise InsufficientDataError(
            required=period + 1,
            available=n,
            context=f"ATR({period})",
        )

    highs = np.array([float(c.high) for c in candles])
    lows = np.array([float(c.low) for c in candles])
    closes = np.array([float(c.close) for c in candles])

    # True Range (delegates to true_range() so there is a single
    # implementation of the TR formula instead of two that can drift apart)
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = true_range(highs[i], lows[i], closes[i - 1])

    # Wilder's RMA (exponential with alpha = 1/period)
    atr = np.full(n, np.nan)
    1.0 / period

    # Seed with simple average of first `period` TRs
    atr[period - 1] = np.mean(tr[:period])

    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    return atr


def current_atr(candles: list[OHLCV], period: int = DEFAULT_ATR_PERIOD) -> float:
    """Return the single latest ATR value."""
    atr_series = calculate_atr(candles, period)
    return float(atr_series[-1])
