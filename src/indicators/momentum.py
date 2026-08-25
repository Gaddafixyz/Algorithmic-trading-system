"""Momentum indicators: Rate of Change, Stochastic, Williams %R."""

from __future__ import annotations

import numpy as np

from src.core.models import OHLCV


def rate_of_change(candles: list[OHLCV], period: int = 14) -> np.ndarray:
    """Price Rate of Change as a percentage."""
    closes = np.array([float(c.close) for c in candles])
    n = len(closes)
    roc = np.full(n, np.nan)
    for i in range(period, n):
        prev = closes[i - period]
        if prev != 0.0:
            roc[i] = (closes[i] - prev) / prev * 100.0
    return roc


def stochastic(
    candles: list[OHLCV],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Stochastic oscillator (%K and %D).

    Returns (k, d) arrays.
    """
    n = len(candles)
    highs = np.array([float(c.high) for c in candles])
    lows = np.array([float(c.low) for c in candles])
    closes = np.array([float(c.close) for c in candles])

    k = np.full(n, np.nan)
    for i in range(k_period - 1, n):
        h = np.max(highs[i - k_period + 1 : i + 1])
        l = np.min(lows[i - k_period + 1 : i + 1])
        if h != l:
            k[i] = (closes[i] - l) / (h - l) * 100.0
        else:
            k[i] = 50.0

    # %D is SMA of %K
    from src.indicators.sma import calculate_sma
    valid_mask = ~np.isnan(k)
    d = np.full(n, np.nan)
    if valid_mask.sum() >= d_period:
        first_valid = np.where(valid_mask)[0][0]
        k_valid = k[first_valid:]
        d_partial = calculate_sma(k_valid.tolist(), d_period)
        d[first_valid:] = d_partial

    return k, d


def williams_r(candles: list[OHLCV], period: int = 14) -> np.ndarray:
    """Williams %R oscillator. Range: -100 to 0."""
    n = len(candles)
    highs = np.array([float(c.high) for c in candles])
    lows = np.array([float(c.low) for c in candles])
    closes = np.array([float(c.close) for c in candles])

    wr = np.full(n, np.nan)
    for i in range(period - 1, n):
        h = np.max(highs[i - period + 1 : i + 1])
        l = np.min(lows[i - period + 1 : i + 1])
        if h != l:
            wr[i] = (h - closes[i]) / (h - l) * -100.0
    return wr
