"""
Performance analytics: comprehensive trade statistics.

Computes all standard trading performance metrics from a list of Trade records.
No look-ahead bias: metrics are computed from closed trades only.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np

from src.analytics.advanced_metrics import (
    deflated_sharpe_ratio as calc_deflated_sharpe_ratio,
)
from src.analytics.advanced_metrics import (
    historical_cvar as calc_historical_cvar,
)
from src.analytics.advanced_metrics import (
    historical_var as calc_historical_var,
)
from src.analytics.advanced_metrics import omega_ratio as calc_omega_ratio
from src.analytics.advanced_metrics import (
    probabilistic_sharpe_ratio as calc_probabilistic_sharpe_ratio,
)
from src.analytics.advanced_metrics import tail_ratio as calc_tail_ratio
from src.analytics.advanced_metrics import ulcer_index as calc_ulcer_index
from src.analytics.expectancy import expectancy as calculate_expectancy
from src.analytics.expectancy import (
    system_quality_number as calculate_system_quality_number,
)
from src.analytics.sharpe import calmar_ratio as calc_calmar_ratio
from src.analytics.sharpe import information_ratio as calc_information_ratio
from src.analytics.sharpe import sharpe_ratio as calc_sharpe_ratio
from src.analytics.sharpe import sortino_ratio as calc_sortino_ratio
from src.core.constants import ANNUAL_TRADING_DAYS
from src.core.models import OHLCV
from src.domain.trade import Trade


@dataclass
class PerformanceReport:
    """Complete performance report for a strategy or account."""

    # Overview
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0

    # P&L
    total_net_pnl: Decimal = Decimal("0")
    total_gross_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    avg_trade_pnl: Decimal = Decimal("0")
    avg_winner: Decimal = Decimal("0")
    avg_loser: Decimal = Decimal("0")
    largest_winner: Decimal = Decimal("0")
    largest_loser: Decimal = Decimal("0")

    # Ratios
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: Decimal = Decimal("0")
    risk_reward_avg: float = 0.0

    # Risk-adjusted
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    information_ratio: float | None = None

    # Advanced quantitative metrics (analytics/advanced_metrics.py +
    # expectancy.py::system_quality_number). These were implemented and
    # unit-tested (tests/unit/test_advanced_metrics.py, 19 tests) but had
    # zero callers anywhere in src/ or scripts/ — the module's own
    # docstring says it exists "to be used identically by the
    # backtester, the paper-trading report, and live analytics", but
    # never actually was. probabilistic_sharpe_ratio and
    # deflated_sharpe_ratio are in [0, 1] (probability the true Sharpe
    # beats a benchmark / beats what n_trials random parameter searches
    # would produce by chance); deflated_sharpe_ratio equals
    # probabilistic_sharpe_ratio when n_trials=1 (the default — a single
    # pre-registered backtest, no parameter search to deflate against).
    probabilistic_sharpe_ratio: float = 0.0
    deflated_sharpe_ratio: float = 0.0
    omega_ratio: float = 0.0
    tail_ratio: float = 0.0
    ulcer_index: float = 0.0
    historical_var_95: float = 0.0
    historical_cvar_95: float = 0.0
    system_quality_number: float = 0.0

    # Drawdown
    max_drawdown_pct: float = 0.0
    max_drawdown_abs: Decimal = Decimal("0")
    avg_drawdown_pct: float = 0.0
    recovery_factor: float = 0.0

    # Duration
    avg_trade_duration_hours: float = 0.0
    avg_winner_duration_hours: float = 0.0
    avg_loser_duration_hours: float = 0.0

    # Streaks
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # By symbol (filled if multi-symbol)
    by_symbol: dict[str, Any] = field(default_factory=dict)

    @property
    def total_return_pct(self) -> float:
        """Total return — requires knowing initial equity externally."""
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "total_net_pnl": str(self.total_net_pnl),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy": str(self.expectancy),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "information_ratio": (
                round(self.information_ratio, 4) if self.information_ratio is not None else None
            ),
            "probabilistic_sharpe_ratio": round(self.probabilistic_sharpe_ratio, 4),
            "deflated_sharpe_ratio": round(self.deflated_sharpe_ratio, 4),
            "omega_ratio": round(self.omega_ratio, 4) if math.isfinite(self.omega_ratio) else str(self.omega_ratio),
            "tail_ratio": round(self.tail_ratio, 4) if math.isfinite(self.tail_ratio) else str(self.tail_ratio),
            "ulcer_index": round(self.ulcer_index, 4),
            "historical_var_95": round(self.historical_var_95, 4),
            "historical_cvar_95": round(self.historical_cvar_95, 4),
            "system_quality_number": round(self.system_quality_number, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "avg_trade_duration_hours": round(self.avg_trade_duration_hours, 2),
            "max_consecutive_wins": self.max_consecutive_wins,
            "max_consecutive_losses": self.max_consecutive_losses,
        }


def benchmark_returns_for_trades(
    trades: list[Trade],
    benchmark_candles: list[OHLCV],
) -> np.ndarray:
    """
    Per-trade buy-and-hold benchmark return, aligned 1:1 (and in the same
    chronological order) with the per-trade strategy returns computed in
    calculate_performance() — the missing piece for
    analytics/sharpe.py::information_ratio(), which needs a
    benchmark_returns series the same shape as strategy_returns.

    For each trade, looks up the benchmark's close price at-or-before
    opened_at and at-or-before closed_at and returns the pct change
    between them — i.e. "what would holding the benchmark instead, over
    this exact trade's holding window, have returned". This function only
    does the alignment; sourcing benchmark_candles (the traded symbol's
    own candles for a same-asset buy-and-hold comparison, a market index
    for a market-relative one, etc.) is the caller's job — same
    division of responsibility as the empirical confluence-weight tuning
    in scripts/optimize.py, which needs real market data this sandbox
    doesn't have.

    Args:
        trades: Trade records to align against. Sorted internally by
            closed_at to match calculate_performance()'s own ordering.
        benchmark_candles: Benchmark OHLCV series. Need not be sorted or
            pre-trimmed to the trades' date range, but must cover it —
            a trade opening before the benchmark's earliest candle or
            closing after its latest one uses the nearest available
            price as a best-effort fallback rather than raising, since a
            partial benchmark series is still useful for the trades it
            does cover.

    Returns:
        Array of per-trade benchmark returns, same length and order as
        the trades passed in (after internal sorting by closed_at).

    Raises:
        ValueError: If benchmark_candles is empty.
    """
    if not benchmark_candles:
        raise ValueError("benchmark_candles must be non-empty")

    sorted_candles = sorted(benchmark_candles, key=lambda c: c.timestamp)
    timestamps = [c.timestamp for c in sorted_candles]

    def price_at_or_before(ts: datetime) -> Decimal:
        idx = bisect.bisect_right(timestamps, ts) - 1
        if idx < 0:
            idx = 0  # trade predates the benchmark series: use earliest available
        return sorted_candles[idx].close

    trades_sorted = sorted(trades, key=lambda t: t.closed_at)
    returns: list[float] = []
    for t in trades_sorted:
        entry_price = price_at_or_before(t.opened_at)
        exit_price = price_at_or_before(t.closed_at)
        if entry_price == Decimal("0"):
            returns.append(0.0)
            continue
        returns.append(float((exit_price - entry_price) / entry_price))
    return np.array(returns)


def calculate_performance(
    trades: list[Trade],
    initial_equity: float = 10_000.0,
    periods_per_year: float = ANNUAL_TRADING_DAYS,
    benchmark_candles: list[OHLCV] | None = None,
    n_trials: int = 1,
) -> PerformanceReport:
    """
    Calculate comprehensive performance metrics from a list of closed trades.

    Args:
        trades: List of completed Trade records.
        initial_equity: Starting capital for drawdown calculations.
        periods_per_year: Trading periods per year (252 for daily).
        benchmark_candles: Optional benchmark OHLCV series (e.g. the
            traded symbol's own candles for a buy-and-hold comparison).
            When provided, report.information_ratio is populated via
            benchmark_returns_for_trades() + analytics/sharpe.py's
            information_ratio(); left as None (not 0.0 — "not computed"
            is a different fact than "computed as zero") when omitted.
        n_trials: Number of independent strategy variants/parameter
            combinations that were tried before arriving at these trades
            (e.g. the size of an Optuna search space in
            scripts/optimize.py). Deflates report.deflated_sharpe_ratio
            toward the Sharpe ratio you'd expect from picking the best
            of n_trials random results by chance alone. Defaults to 1 —
            a single pre-registered backtest with no parameter search —
            in which case deflated_sharpe_ratio equals
            probabilistic_sharpe_ratio.

    Returns:
        PerformanceReport with all metrics populated.
    """
    if not trades:
        return PerformanceReport()

    trades_sorted = sorted(trades, key=lambda t: t.closed_at)
    report = PerformanceReport()
    report.total_trades = len(trades_sorted)

    # ─── P&L breakdown ────────────────────────────────────────────────────────
    pnls = [t.net_pnl for t in trades_sorted]
    winners = [p for p in pnls if p > Decimal("0")]
    losers = [p for p in pnls if p < Decimal("0")]

    report.winning_trades = len(winners)
    report.losing_trades = len(losers)
    report.breakeven_trades = report.total_trades - report.winning_trades - report.losing_trades

    report.total_net_pnl = sum(pnls, Decimal("0"))
    report.total_gross_pnl = sum(t.gross_pnl for t in trades_sorted)
    report.total_fees = sum(t.fees for t in trades_sorted)

    report.avg_trade_pnl = report.total_net_pnl / Decimal(str(report.total_trades))
    report.avg_winner = sum(winners, Decimal("0")) / Decimal(str(max(len(winners), 1)))
    report.avg_loser = sum(losers, Decimal("0")) / Decimal(str(max(len(losers), 1)))
    report.largest_winner = max(pnls)
    report.largest_loser = min(pnls)

    # ─── Ratios ───────────────────────────────────────────────────────────────
    report.win_rate = report.winning_trades / report.total_trades

    gross_wins = sum(winners, Decimal("0"))
    gross_losses = abs(sum(losers, Decimal("0")))
    report.profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    # Expectancy = (win_rate × avg_win) + (loss_rate × avg_loser). Delegated
    # to analytics/expectancy.py's expectancy() rather than reimplemented
    # here — this used to duplicate that formula inline and could drift
    # from it silently; now there's exactly one implementation.
    report.expectancy = calculate_expectancy(trades_sorted)

    # ─── Return series for risk metrics ───────────────────────────────────────
    pnls_float = [float(p) for p in pnls]
    equity_curve = [initial_equity]
    for p in pnls_float:
        equity_curve.append(equity_curve[-1] + p)

    returns = np.diff(equity_curve) / np.array(equity_curve[:-1])

    # Sharpe / Sortino — delegated to analytics/sharpe.py rather than
    # reimplemented inline. The two implementations had silently drifted:
    # this block used to compute np.std(excess) with numpy's default
    # ddof=0 (population std) while sharpe.py used ddof=1 (sample std, the
    # standard unbiased-estimator convention for Sharpe/Sortino), so the
    # same trade history could report two different ratios depending on
    # which code path was reporting it. Standardized on sharpe.py's
    # ddof=1 convention; both functions independently guard len<2 and
    # zero-std cases and return 0.0, matching this block's prior behavior.
    if len(returns) >= 2:
        report.sharpe_ratio = calc_sharpe_ratio(returns, periods_per_year=periods_per_year)
        report.sortino_ratio = calc_sortino_ratio(returns, periods_per_year=periods_per_year)

        # Advanced quantitative metrics — same `returns` series used for
        # Sharpe/Sortino above, per advanced_metrics.py's own contract
        # ("pure...take plain return series so they can be used
        # identically by the backtester, the paper-trading report, and
        # live analytics").
        report.probabilistic_sharpe_ratio = calc_probabilistic_sharpe_ratio(
            returns, periods_per_year=periods_per_year
        )
        report.deflated_sharpe_ratio = calc_deflated_sharpe_ratio(
            returns, n_trials=n_trials, periods_per_year=periods_per_year
        )
        report.omega_ratio = calc_omega_ratio(returns)
        report.tail_ratio = calc_tail_ratio(returns)
        # Historical (empirical) VaR/CVaR rather than parametric_var —
        # preferred by parametric_var's own docstring "when a return
        # series is available" (crypto returns are fat-tailed enough
        # that the Gaussian approximation understates tail risk).
        report.historical_var_95 = calc_historical_var(returns, confidence=0.95)
        report.historical_cvar_95 = calc_historical_cvar(returns, confidence=0.95)

        if benchmark_candles:
            bench_returns = benchmark_returns_for_trades(trades_sorted, benchmark_candles)
            # Guaranteed equal length by construction — both are derived
            # 1:1 from the same trades_sorted list — but guard anyway
            # rather than let a future refactor silently misalign the
            # two series and hand information_ratio() garbage.
            if len(bench_returns) == len(returns):
                report.information_ratio = calc_information_ratio(
                    returns, bench_returns, periods_per_year=periods_per_year
                )

    # ─── Drawdown ─────────────────────────────────────────────────────────────
    eq_arr = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq_arr)
    drawdowns_abs = peaks - eq_arr
    drawdowns_pct = np.where(peaks > 0, drawdowns_abs / peaks * 100, 0)

    report.max_drawdown_pct = float(np.max(drawdowns_pct))
    report.max_drawdown_abs = Decimal(str(float(np.max(drawdowns_abs))))
    report.avg_drawdown_pct = float(np.mean(drawdowns_pct[drawdowns_pct > 0])) if any(drawdowns_pct > 0) else 0.0

    # Ulcer index — RMS of drawdown-from-peak over the whole equity curve,
    # not just Sharpe/Sortino's period-to-period returns; penalizes deep,
    # prolonged drawdowns that a single max_drawdown_pct number can hide
    # (two curves with the same max drawdown can spend very different
    # amounts of time underwater).
    report.ulcer_index = calc_ulcer_index(equity_curve)

    # Calmar ratio = annualised return / max drawdown
    if report.max_drawdown_pct > 0:
        total_return_pct = (equity_curve[-1] - initial_equity) / initial_equity * 100
        report.calmar_ratio = calc_calmar_ratio(total_return_pct, report.max_drawdown_pct)

    # Recovery factor = net profit / max absolute drawdown
    if report.max_drawdown_abs > 0:
        report.recovery_factor = float(report.total_net_pnl / report.max_drawdown_abs)

    # ─── Duration ─────────────────────────────────────────────────────────────
    durations = [t.duration_hours for t in trades_sorted]
    winner_durations = [t.duration_hours for t in trades_sorted if t.is_winner]
    loser_durations = [t.duration_hours for t in trades_sorted if t.is_loser]

    report.avg_trade_duration_hours = sum(durations) / len(durations)
    if winner_durations:
        report.avg_winner_duration_hours = sum(winner_durations) / len(winner_durations)
    if loser_durations:
        report.avg_loser_duration_hours = sum(loser_durations) / len(loser_durations)

    # ─── Streaks ──────────────────────────────────────────────────────────────
    max_w = max_l = cur_w = cur_l = 0
    for t in trades_sorted:
        if t.is_winner:
            cur_w += 1
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif t.is_loser:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)
        else:
            cur_w = cur_l = 0

    report.max_consecutive_wins = max_w
    report.max_consecutive_losses = max_l

    # ─── System Quality Number (Van Tharp) ───────────────────────────────────
    # Delegated to analytics/expectancy.py's system_quality_number(), which
    # itself needs >= 3 trades with a populated Trade.initial_risk to
    # compute R-multiples; returns 0.0 (not populated) otherwise, same
    # "not enough data yet" convention the rest of this function already
    # uses for sharpe/sortino below 2 returns.
    report.system_quality_number = calculate_system_quality_number(trades_sorted)

    return report
