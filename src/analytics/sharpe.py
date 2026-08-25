"""Risk-adjusted return metrics."""

from __future__ import annotations

import math

import numpy as np

from src.core.constants import ANNUAL_TRADING_DAYS, RISK_FREE_RATE


def sharpe_ratio(
    returns: list[float] | np.ndarray,
    risk_free_rate: float = RISK_FREE_RATE,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
) -> float:
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2 or np.std(arr) == 0:
        return 0.0
    daily_rf = risk_free_rate / periods_per_year
    excess = arr - daily_rf
    return float(np.mean(excess) / np.std(excess, ddof=1) * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: list[float] | np.ndarray,
    risk_free_rate: float = RISK_FREE_RATE,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
) -> float:
    arr = np.asarray(returns, dtype=float)
    downside = arr[arr < 0]
    if len(downside) < 2 or np.std(downside) == 0:
        return 0.0
    daily_rf = risk_free_rate / periods_per_year
    return float((np.mean(arr) - daily_rf) / np.std(downside, ddof=1) * math.sqrt(periods_per_year))


def calmar_ratio(total_return_pct: float, max_drawdown_pct: float) -> float:
    if max_drawdown_pct <= 0:
        return 0.0
    return total_return_pct / max_drawdown_pct


def information_ratio(
    strategy_returns: np.ndarray,
    benchmark_returns: np.ndarray,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
) -> float:
    active = strategy_returns - benchmark_returns
    if np.std(active) == 0:
        return 0.0
    return float(np.mean(active) / np.std(active, ddof=1) * math.sqrt(periods_per_year))
