"""
Advanced quantitative performance metrics.

These go beyond the basic Sharpe/Sortino/Calmar set in ``sharpe.py`` and
``performance.py`` to answer the questions a senior quant reviewing a
strategy will ask first: *is this Sharpe ratio actually statistically
significant, or is it noise mined out of a handful of backtests?* and
*what does the loss tail actually look like?*

All functions are pure (no I/O, no side effects) and take plain
return series so they can be used identically by the backtester, the
paper-trading report, and live analytics.
"""

from __future__ import annotations

import math

import numpy as np

from src.core.constants import ANNUAL_TRADING_DAYS, RISK_FREE_RATE


def probabilistic_sharpe_ratio(
    returns: list[float] | np.ndarray,
    benchmark_sharpe: float = 0.0,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
) -> float:
    """
    Probability that the true Sharpe ratio exceeds ``benchmark_sharpe``.

    Implements Bailey & Lopez de Prado's PSR, which corrects the observed
    Sharpe ratio for the skewness and excess kurtosis of the return
    distribution and for the finite sample size. A PSR of 0.95 means
    there's a 95% chance the strategy's *true* Sharpe ratio is above the
    benchmark, given the observed sample.

    Returns a value in [0, 1]. Returns 0.5 (uninformative) if there is
    too little data or the return series has zero variance.
    """
    arr = np.asarray(returns, dtype=float)
    n = len(arr)
    if n < 3 or np.std(arr, ddof=1) == 0:
        return 0.5

    sr = _annualized_sharpe(arr, periods_per_year)
    skew = _skewness(arr)
    kurt = _kurtosis(arr)  # excess kurtosis (normal = 0)

    # Per-period (not annualized) Sharpe for the PSR formula itself.
    sr_period = sr / math.sqrt(periods_per_year)
    bench_period = benchmark_sharpe / math.sqrt(periods_per_year)

    denom = math.sqrt(max(1e-12, 1 - skew * sr_period + (kurt / 4) * sr_period**2))
    z = (sr_period - bench_period) * math.sqrt(n - 1) / denom
    return float(_normal_cdf(z))


def deflated_sharpe_ratio(
    returns: list[float] | np.ndarray,
    n_trials: int,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
    variance_of_trial_sharpes: float = 1.0,
) -> float:
    """
    Probability the observed Sharpe ratio is genuine skill, not the best
    result of ``n_trials`` independent backtests/parameter combinations
    (the "you tried 200 confluence-weight combinations and kept the best
    one" problem).

    This deflates the benchmark Sharpe used in the PSR calculation to the
    *expected maximum* Sharpe ratio one would see across ``n_trials``
    purely by chance, per Bailey & Lopez de Prado (2014).

    Args:
        n_trials: number of independent strategy variants/parameter sets
            that were tried before selecting this one. Set to 1 for a
            single pre-registered backtest with no parameter search.
        variance_of_trial_sharpes: variance across the trials' Sharpe
            ratios; defaults to 1.0 (a conservative, commonly used
            approximation when the actual cross-trial variance is
            unknown).

    Returns a value in [0, 1] — the DSR. Below ~0.95 the result should be
    treated with real skepticism when the strategy came out of an
    optimizer sweep (see ``scripts/optimize.py``).
    """
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")

    euler_mascheroni = 0.5772156649
    if n_trials == 1:
        expected_max_sr = 0.0
    else:
        # Expected maximum of n_trials draws from a standard normal,
        # scaled by the cross-trial Sharpe standard deviation.
        z_term = (1 - euler_mascheroni) * _inverse_normal_cdf(1 - 1.0 / n_trials)
        z_term += euler_mascheroni * _inverse_normal_cdf(1 - 1.0 / (n_trials * math.e))
        expected_max_sr = math.sqrt(max(variance_of_trial_sharpes, 1e-12)) * z_term

    return probabilistic_sharpe_ratio(
        returns, benchmark_sharpe=expected_max_sr, periods_per_year=periods_per_year
    )


def omega_ratio(returns: list[float] | np.ndarray, threshold: float = 0.0) -> float:
    """
    Ratio of the probability-weighted gains to probability-weighted
    losses above/below a threshold return (default 0). Unlike Sharpe,
    Omega uses the full return distribution rather than just mean/variance,
    so it captures fat tails and skew.

    Returns ``inf`` if there are no returns below the threshold.
    """
    arr = np.asarray(returns, dtype=float) - threshold
    gains = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def tail_ratio(returns: list[float] | np.ndarray, percentile: float = 95.0) -> float:
    """
    Ratio of the right-tail magnitude to the left-tail magnitude, e.g. the
    95th-percentile gain divided by the absolute 5th-percentile loss.
    Values > 1 mean winning outliers are bigger than losing ones.
    """
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    right = np.percentile(arr, percentile)
    left = np.percentile(arr, 100 - percentile)
    if left == 0:
        return float("inf") if right > 0 else 0.0
    return float(abs(right / left))


def ulcer_index(equity_curve: list[float] | np.ndarray) -> float:
    """
    RMS of drawdown-from-peak over the equity curve — penalizes deep and
    prolonged drawdowns more than a single max-drawdown number does.
    Lower is better; 0 means the equity curve never fell below its peak.
    """
    arr = np.asarray(equity_curve, dtype=float)
    if len(arr) < 2:
        return 0.0
    running_max = np.maximum.accumulate(arr)
    drawdown_pct = np.where(running_max > 0, (arr - running_max) / running_max * 100, 0.0)
    return float(np.sqrt(np.mean(drawdown_pct**2)))


def historical_var(returns: list[float] | np.ndarray, confidence: float = 0.95) -> float:
    """
    Historical (empirical) Value-at-Risk: the loss threshold not expected
    to be exceeded with the given confidence, over one period. Returned
    as a positive number representing a loss magnitude (e.g. 0.03 = 3%).
    """
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    return float(-np.percentile(arr, (1 - confidence) * 100))


def historical_cvar(returns: list[float] | np.ndarray, confidence: float = 0.95) -> float:
    """
    Historical Conditional VaR (expected shortfall): the average loss
    *given that* the loss exceeds the VaR threshold. Always >= VaR;
    captures how bad the tail actually is, not just where it starts.
    """
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    var = -historical_var(arr, confidence)  # threshold as a raw return (negative)
    tail = arr[arr <= var]
    if len(tail) == 0:
        return float(-var)
    return float(-np.mean(tail))


def parametric_var(mean_return: float, std_return: float, confidence: float = 0.95) -> float:
    """Gaussian (delta-normal) VaR — fast approximation when you only have
    mean/std (e.g. from a covariance matrix) rather than a full return
    series. Less accurate than historical VaR for fat-tailed crypto
    returns; prefer ``historical_var``/``historical_cvar`` when a return
    series is available."""
    z = _inverse_normal_cdf(confidence)
    return float(max(0.0, z * std_return - mean_return))


# ─── Small self-contained stats helpers (avoid a scipy dependency here) ──────


def _skewness(arr: np.ndarray) -> float:
    n = len(arr)
    mean, std = np.mean(arr), np.std(arr, ddof=1)
    if std == 0 or n < 3:
        return 0.0
    return float(np.sum((arr - mean) ** 3) / n / std**3)


def _kurtosis(arr: np.ndarray) -> float:
    """Excess kurtosis (normal distribution = 0)."""
    n = len(arr)
    mean, std = np.mean(arr), np.std(arr, ddof=1)
    if std == 0 or n < 4:
        return 0.0
    return float(np.sum((arr - mean) ** 4) / n / std**4 - 3.0)


def _annualized_sharpe(arr: np.ndarray, periods_per_year: float) -> float:
    daily_rf = RISK_FREE_RATE / periods_per_year
    excess = arr - daily_rf
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def _normal_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _inverse_normal_cdf(p: float) -> float:
    """
    Acklam's rational approximation of the inverse standard normal CDF.
    Accurate to ~1e-9, avoids adding a scipy import for a single function.
    """
    if not (0.0 < p < 1.0):
        raise ValueError("p must be in (0, 1)")

    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]

    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )
