"""Volatility indicators: Bollinger Bands, historical volatility."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.core.constants import DEFAULT_BB_PERIOD, DEFAULT_BB_STD
from src.core.models import OHLCV
from src.indicators.sma import calculate_sma


@dataclass
class BollingerBands:
    upper: np.ndarray
    middle: np.ndarray
    lower: np.ndarray
    bandwidth: np.ndarray
    percent_b: np.ndarray


def bollinger_bands(
    candles: list[OHLCV],
    period: int = DEFAULT_BB_PERIOD,
    std_dev: float = DEFAULT_BB_STD,
) -> BollingerBands:
    """Calculate Bollinger Bands."""
    closes = np.array([float(c.close) for c in candles])
    middle = calculate_sma(closes.tolist(), period)

    upper = np.full(len(closes), np.nan)
    lower = np.full(len(closes), np.nan)
    bw = np.full(len(closes), np.nan)
    pb = np.full(len(closes), np.nan)

    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        sd = np.std(window, ddof=0)
        mid = middle[i]
        upper[i] = mid + std_dev * sd
        lower[i] = mid - std_dev * sd
        if mid != 0.0:
            bw[i] = (upper[i] - lower[i]) / mid
        if upper[i] != lower[i]:
            pb[i] = (closes[i] - lower[i]) / (upper[i] - lower[i])

    return BollingerBands(
        upper=upper,
        middle=middle,
        lower=lower,
        bandwidth=bw,
        percent_b=pb,
    )


def historical_volatility(candles: list[OHLCV], period: int = 20) -> float:
    """
    Annualised historical volatility from log returns.

    Returns as a fraction (0.3 = 30% annualised).
    """
    closes = np.array([float(c.close) for c in candles])
    if len(closes) < period + 1:
        return 0.0
    log_returns = np.diff(np.log(closes[-period - 1 :]))
    return float(np.std(log_returns, ddof=1) * np.sqrt(365))
