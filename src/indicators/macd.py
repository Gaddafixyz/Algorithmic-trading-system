"""
MACD (Moving Average Convergence Divergence).

Standard MACD: EMA(fast) - EMA(slow), signal = EMA(macd, signal_period).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.core.constants import DEFAULT_EMA_FAST, DEFAULT_EMA_SLOW, DEFAULT_MACD_SIGNAL
from src.core.models import OHLCV
from src.indicators.ema import calculate_ema


@dataclass
class MACDResult:
    macd_line: np.ndarray
    signal_line: np.ndarray
    histogram: np.ndarray


def calculate_macd(
    closes: list[float] | np.ndarray,
    fast: int = DEFAULT_EMA_FAST,
    slow: int = DEFAULT_EMA_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> MACDResult:
    """Calculate MACD, signal, and histogram."""
    arr = np.asarray(closes, dtype=float)
    ema_fast = calculate_ema(arr, fast)
    ema_slow = calculate_ema(arr, slow)

    macd_line = ema_fast - ema_slow
    # Signal is EMA of macd_line; only compute where macd_line is valid
    valid_mask = ~np.isnan(macd_line)
    signal_line = np.full(len(arr), np.nan)
    if valid_mask.sum() >= signal:
        first_valid = np.where(valid_mask)[0][0]
        valid_macd = macd_line[first_valid:]
        sig_partial = calculate_ema(valid_macd.tolist(), signal)
        signal_line[first_valid:] = sig_partial

    histogram = macd_line - signal_line
    return MACDResult(macd_line=macd_line, signal_line=signal_line, histogram=histogram)


def macd_from_candles(
    candles: list[OHLCV],
    fast: int = DEFAULT_EMA_FAST,
    slow: int = DEFAULT_EMA_SLOW,
    signal: int = DEFAULT_MACD_SIGNAL,
) -> MACDResult:
    closes = [float(c.close) for c in candles]
    return calculate_macd(closes, fast, slow, signal)
