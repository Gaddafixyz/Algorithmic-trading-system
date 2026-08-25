"""Statistical helpers for indicators and analytics."""

from __future__ import annotations

import numpy as np
from scipy import stats as scipy_stats


def rolling_mean(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = np.mean(values[i - period + 1 : i + 1])
    return result


def rolling_std(values: np.ndarray, period: int, ddof: int = 1) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = np.std(values[i - period + 1 : i + 1], ddof=ddof)
    return result


def rolling_max(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = np.max(values[i - period + 1 : i + 1])
    return result


def rolling_min(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = np.min(values[i - period + 1 : i + 1])
    return result


def zscore(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling z-score."""
    mean = rolling_mean(values, period)
    std = rolling_std(values, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(std > 0, (values - mean) / std, np.nan)


def pearson_correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation coefficient between two equal-length arrays."""
    if len(x) < 2:
        return 0.0
    r, _ = scipy_stats.pearsonr(x, y)
    return float(r)


def linear_regression_slope(values: np.ndarray, period: int) -> np.ndarray:
    """Rolling linear regression slope."""
    n = len(values)
    slope = np.full(n, np.nan)
    x = np.arange(period, dtype=float)
    for i in range(period - 1, n):
        y = values[i - period + 1 : i + 1]
        if not np.any(np.isnan(y)):
            result = scipy_stats.linregress(x, y)
            slope[i] = result.slope
    return slope
