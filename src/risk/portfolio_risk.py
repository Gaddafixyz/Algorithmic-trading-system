"""
Portfolio-level risk aggregation and reporting.

Extends the single-position risk checks in ``risk_manager.py`` with
cross-position views: correlation-adjusted concentration limits,
portfolio-level Value-at-Risk (which is *not* just the sum of
per-position VaR once you account for correlation), and volatility-
targeted / risk-parity capital allocation across concurrently traded
symbols.

These functions consume a ``CorrelationTracker`` (``risk/exposure.py``)
rather than owning their own price history, so callers that already
maintain one (e.g. the strategy orchestrator) don't have to feed prices
twice.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal
from typing import Any

import numpy as np

from src.domain.portfolio import Portfolio
from src.risk.exposure import CorrelationTracker

logger = logging.getLogger(__name__)


def portfolio_risk_report(
    portfolio: Portfolio,
    equity: Decimal,
    correlation_tracker: CorrelationTracker | None = None,
) -> dict[str, Any]:
    """
    Generate a snapshot of current portfolio risk metrics.

    When ``correlation_tracker`` is supplied and has enough history, the
    report also includes the diversification ratio and the most
    correlated open pair — the two numbers that most directly answer
    "am I actually diversified, or just holding five correlated bets on
    the same move?"
    """
    open_positions = list(portfolio.open_positions)

    total_unrealized = sum((p.unrealized_pnl for p in open_positions), Decimal("0"))
    total_notional = sum((p.notional_value for p in open_positions), Decimal("0"))

    symbols = [p.symbol for p in open_positions]
    sides = [p.side.value for p in open_positions]

    long_notional = sum((p.notional_value for p in open_positions if p.is_long), Decimal("0"))
    short_notional = sum((p.notional_value for p in open_positions if p.is_short), Decimal("0"))

    report: dict[str, Any] = {
        "equity": str(equity),
        "open_positions": len(open_positions),
        "symbols": symbols,
        "sides": sides,
        "total_unrealized_pnl": str(total_unrealized),
        "total_notional": str(total_notional),
        "gross_exposure_pct": float(total_notional / equity * 100) if equity > 0 else 0.0,
        "net_exposure_pct": float((long_notional - short_notional) / equity * 100)
        if equity > 0
        else 0.0,
        "long_notional": str(long_notional),
        "short_notional": str(short_notional),
        "drawdown_pct": float(portfolio.drawdown),
    }

    if correlation_tracker is not None and len(open_positions) >= 2:
        held = set(symbols)
        tracked = held & set(correlation_tracker.symbols)
        if len(tracked) >= 2:
            report["diversification_ratio"] = correlation_tracker.diversification_ratio()
            pair = correlation_tracker.highest_correlated_pair()
            if pair is not None:
                report["highest_correlated_pair"] = {
                    "symbol_a": pair[0],
                    "symbol_b": pair[1],
                    "correlation": pair[2],
                }

    return report


def correlation_adjusted_size(
    base_quantity: Decimal,
    candidate_symbol: str,
    portfolio: Portfolio,
    correlation_tracker: CorrelationTracker,
    *,
    max_correlation: float = 0.7,
    min_scale: float = 0.25,
) -> Decimal:
    """
    Scale down a proposed new position's size based on how correlated it
    is with symbols already held.

    Rationale: fixed-fractional and Kelly sizing both size each position
    in isolation, so a book that's "10 different 1%-risk trades" can
    actually be one concentrated 8%-risk bet if those symbols move
    together (e.g. most altcoins vs. BTC in a risk-off event). This
    scales the incremental position down — never up — proportionally to
    the strongest correlation against an already-open symbol, floored at
    ``min_scale`` so a single highly-correlated pair doesn't zero out the
    trade entirely (that's what hard exposure limits in
    ``risk_manager.py`` are for).

    Args:
        base_quantity: Size computed by the normal position-sizing model.
        candidate_symbol: Symbol being sized.
        portfolio: Current portfolio (for the set of already-open symbols).
        correlation_tracker: Rolling correlation tracker covering both the
            candidate and the held symbols.
        max_correlation: Correlation at/above which sizing is scaled down
            to ``min_scale``. Below this, scaling is linear.
        min_scale: Floor on the size multiplier (0-1).

    Returns:
        The (possibly reduced) position quantity. Returns ``base_quantity``
        unchanged if there's nothing open yet or insufficient correlation
        data.
    """
    if base_quantity <= Decimal("0"):
        return base_quantity

    held_symbols = {p.symbol for p in portfolio.open_positions if p.symbol != candidate_symbol}
    if not held_symbols:
        return base_quantity

    matrix = correlation_tracker.matrix()
    if candidate_symbol not in matrix:
        return base_quantity

    correlations = [
        abs(matrix[candidate_symbol][sym])
        for sym in held_symbols
        if sym in matrix.get(candidate_symbol, {}) and not math.isnan(matrix[candidate_symbol][sym])
    ]
    if not correlations:
        return base_quantity

    worst = max(correlations)
    if worst <= 0:
        return base_quantity

    # Linear scale from 1.0 at correlation=0 down to min_scale at
    # correlation>=max_correlation.
    scale = 1.0 - (min(worst, max_correlation) / max_correlation) * (1.0 - min_scale)
    scale = max(min_scale, min(1.0, scale))

    if scale < 1.0:
        logger.info(
            "Correlation-adjusted sizing: %s worst_corr=%.2f vs held=%s -> scale=%.2f",
            candidate_symbol,
            worst,
            sorted(held_symbols),
            scale,
        )
    return base_quantity * Decimal(str(scale))


def portfolio_var_cvar(
    portfolio: Portfolio,
    equity: Decimal,
    correlation_tracker: CorrelationTracker,
    *,
    confidence: float = 0.95,
) -> dict[str, Any] | None:
    """
    Portfolio-level parametric VaR/CVaR using the per-symbol volatility
    (from the correlation tracker's return history) and the full
    correlation matrix — i.e. actual portfolio variance ``w^T Σ w``, not
    the sum of standalone position VaRs, which overstates risk for a
    diversified book and understates it for a correlated one.

    Returns ``None`` when there isn't enough data (fewer than two open,
    tracked symbols with return history) to form a covariance matrix.
    All percentages are relative to ``equity``.
    """
    open_positions = [p for p in portfolio.open_positions if p.notional_value > 0]
    symbols = [p.symbol for p in open_positions if p.symbol in correlation_tracker.symbols]
    if len(symbols) < 2 or equity <= Decimal("0"):
        return None

    returns_by_symbol = {s: correlation_tracker.returns(s) for s in symbols}
    min_len = min(len(r) for r in returns_by_symbol.values())
    if min_len < 2:
        return None

    # Weight vector: each symbol's signed notional as a fraction of equity.
    notional_by_symbol: dict[str, float] = {}
    for p in open_positions:
        if p.symbol not in symbols:
            continue
        signed = float(p.notional_value) * (1.0 if p.is_long else -1.0)
        notional_by_symbol[p.symbol] = notional_by_symbol.get(p.symbol, 0.0) + signed

    weights = np.array([notional_by_symbol[s] / float(equity) for s in symbols])
    return_matrix = np.array([returns_by_symbol[s][-min_len:] for s in symbols])
    cov = np.cov(return_matrix)
    if cov.ndim == 0:  # only one symbol slipped through
        return None

    portfolio_variance = float(weights @ cov @ weights)
    portfolio_std = math.sqrt(max(portfolio_variance, 0.0))

    z = _inverse_normal_cdf(confidence)
    var_pct = z * portfolio_std * 100
    # Gaussian-tail CVaR closed form: E[X | X < -VaR] for a normal is
    # mean - std * phi(z)/(1-confidence). Assumes zero-mean returns over
    # the one-period horizon, consistent with the VaR side of this calc.
    phi_z = math.exp(-(z**2) / 2) / math.sqrt(2 * math.pi)
    cvar_pct = (phi_z / (1 - confidence)) * portfolio_std * 100

    return {
        "confidence": confidence,
        "portfolio_var_pct": var_pct,
        "portfolio_cvar_pct": cvar_pct,
        "portfolio_daily_vol_pct": portfolio_std * 100,
        "symbols_used": symbols,
        "gross_weight": float(np.abs(weights).sum()),
    }


def risk_parity_weights(
    volatilities: dict[str, float],
    correlation_tracker: CorrelationTracker | None = None,
) -> dict[str, float]:
    """
    Inverse-volatility capital weights across concurrently traded symbols
    (simple risk parity: weight_i proportional to 1/vol_i, normalized to
    sum to 1).

    This is the naive/robust risk-parity variant — it ignores
    cross-correlation in the *weighting* itself (a full min-variance or
    equal-risk-contribution solve would use the covariance matrix), which
    makes it far less sensitive to the estimation noise that plagues
    full covariance-matrix optimization on a handful of crypto pairs.
    Use ``portfolio_var_cvar`` above to check the *resulting* portfolio's
    actual correlation-aware risk after applying these weights.

    Args:
        volatilities: symbol -> volatility estimate (any consistent unit,
            e.g. ATR% or realized daily-return stdev).
        correlation_tracker: unused in the weight calculation itself;
            accepted so callers can pass it through for a future
            equal-risk-contribution upgrade without changing the call site.

    Returns:
        symbol -> weight, summing to 1.0. Empty dict if no valid
        (positive) volatilities are provided.
    """
    valid = {s: v for s, v in volatilities.items() if v and v > 0}
    if not valid:
        return {}
    inv_vol = {s: 1.0 / v for s, v in valid.items()}
    total = sum(inv_vol.values())
    return {s: w / total for s, w in inv_vol.items()}


def _inverse_normal_cdf(p: float) -> float:
    """Delegate to the shared Acklam approximation in analytics."""
    from src.analytics.advanced_metrics import _inverse_normal_cdf as impl

    return impl(p)
