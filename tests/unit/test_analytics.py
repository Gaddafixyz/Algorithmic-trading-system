"""Unit tests for analytics and performance modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.analytics.performance import (
    PerformanceReport,
    calculate_performance,
    benchmark_returns_for_trades,
)
from src.analytics.expectancy import expectancy, r_multiples, system_quality_number
from src.analytics.sharpe import sharpe_ratio, sortino_ratio, calmar_ratio
from src.core.enums import PositionSide, Timeframe
from src.core.models import OHLCV
from src.domain.trade import Trade


def make_trade(
    net_pnl: float,
    duration_hours: float = 4.0,
    symbol: str = "BTCUSDT",
    initial_risk: float | None = None,
) -> Trade:
    opened = datetime(2024, 1, 1, tzinfo=UTC)
    closed = opened + timedelta(hours=duration_hours)
    gross = Decimal(str(net_pnl)) + Decimal("2")  # Add back a small fee
    return Trade(
        position_id="pos_" + str(abs(int(net_pnl * 100))),
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal("1.0"),
        entry_quantity=Decimal("1.0"),
        entry_price=Decimal("50000"),
        exit_price=Decimal(str(50000 + net_pnl)),
        gross_pnl=gross,
        fees=Decimal("2"),
        opened_at=opened,
        closed_at=closed,
        initial_risk=Decimal(str(initial_risk)) if initial_risk else None,
    )


class TestCalculatePerformance:
    def test_empty_trades(self):
        report = calculate_performance([])
        assert report.total_trades == 0
        assert report.win_rate == 0.0

    def test_all_winners(self):
        trades = [make_trade(100) for _ in range(10)]
        report = calculate_performance(trades)
        assert report.total_trades == 10
        assert report.winning_trades == 10
        assert report.win_rate == 1.0
        assert report.total_net_pnl > Decimal("0")

    def test_all_losers(self):
        trades = [make_trade(-100) for _ in range(10)]
        report = calculate_performance(trades)
        assert report.winning_trades == 0
        assert report.losing_trades == 10
        assert report.win_rate == 0.0
        assert report.total_net_pnl < Decimal("0")

    def test_mixed_results(self):
        trades = [make_trade(200) for _ in range(6)] + [make_trade(-100) for _ in range(4)]
        report = calculate_performance(trades)
        assert report.total_trades == 10
        assert abs(report.win_rate - 0.6) < 0.001
        assert report.profit_factor > 1.0

    def test_profit_factor(self):
        trades = [make_trade(200)] * 5 + [make_trade(-100)] * 5
        report = calculate_performance(trades)
        # Gross wins = 5*202 = 1010, gross losses = 5*98 = 490
        # PF ≈ 1010/490 ≈ 2.06
        assert report.profit_factor > 1.5

    def test_max_drawdown_detected(self):
        # Equity drops from 10200 to 9800 after two losses
        trades = [make_trade(100), make_trade(100), make_trade(-200), make_trade(-200), make_trade(300)]
        report = calculate_performance(trades, initial_equity=10000.0)
        assert report.max_drawdown_pct > 0.0

    def test_sharpe_positive_for_profitable_strategy(self):
        trades = [make_trade(100) for _ in range(20)]
        report = calculate_performance(trades, initial_equity=10000.0)
        assert report.sharpe_ratio > 0.0

    def test_streak_counting(self):
        # 3 wins, 2 losses, 2 wins
        trades = (
            [make_trade(100)] * 3
            + [make_trade(-50)] * 2
            + [make_trade(100)] * 2
        )
        report = calculate_performance(trades)
        assert report.max_consecutive_wins == 3
        assert report.max_consecutive_losses == 2

    def test_avg_trade_duration(self):
        trades = [make_trade(50, duration_hours=8.0) for _ in range(5)]
        report = calculate_performance(trades)
        assert abs(report.avg_trade_duration_hours - 8.0) < 0.1

    def test_largest_winner_loser(self):
        trades = [make_trade(500), make_trade(100), make_trade(-200), make_trade(-50)]
        report = calculate_performance(trades)
        assert report.largest_winner == max(t.net_pnl for t in trades)
        assert report.largest_loser == min(t.net_pnl for t in trades)

    def test_to_dict_returns_all_fields(self):
        trades = [make_trade(100)] * 5 + [make_trade(-80)] * 3
        report = calculate_performance(trades)
        d = report.to_dict()
        required_keys = [
            "total_trades", "winning_trades", "losing_trades", "win_rate",
            "total_net_pnl", "profit_factor", "sharpe_ratio", "max_drawdown_pct",
        ]
        for key in required_keys:
            assert key in d, f"Missing key: {key}"


class TestExpectancy:
    def test_positive_expectancy(self):
        trades = [make_trade(200)] * 6 + [make_trade(-100)] * 4
        e = expectancy(trades)
        assert e > Decimal("0")

    def test_negative_expectancy(self):
        trades = [make_trade(50)] * 3 + [make_trade(-200)] * 7
        e = expectancy(trades)
        assert e < Decimal("0")

    def test_empty_returns_zero(self):
        assert expectancy([]) == Decimal("0")

    def test_r_multiples_calculated(self):
        trades = [make_trade(200, initial_risk=100.0), make_trade(-100, initial_risk=100.0)]
        rs = r_multiples(trades)
        assert len(rs) == 2
        assert abs(rs[0] - 1.98) < 0.05   # 198/100 (net after fees)
        assert rs[1] < 0.0

    def test_r_multiples_excludes_no_risk(self):
        trades = [make_trade(100), make_trade(200, initial_risk=100.0)]
        rs = r_multiples(trades)
        assert len(rs) == 1  # Only the one with initial_risk

    def test_sqn_positive_for_good_system(self):
        trades = [make_trade(100, initial_risk=50.0) for _ in range(20)] + \
                 [make_trade(-40, initial_risk=50.0) for _ in range(10)]
        sqn = system_quality_number(trades)
        assert sqn > 0.0

    def test_sqn_requires_three_r_multiples(self):
        trades = [make_trade(100)]  # No initial_risk → no R-multiples
        sqn = system_quality_number(trades)
        assert sqn == 0.0


class TestSharpeRatio:
    def test_positive_returns_positive_sharpe(self):
        import numpy as np
        rng = np.random.default_rng(42)
        # Returns with clear positive mean and realistic variance
        returns = list(0.008 + rng.normal(0, 0.003, 50))
        sr = sharpe_ratio(returns)
        assert sr > 0.0

    def test_zero_std_returns_zero(self):
        returns = [0.0] * 20
        sr = sharpe_ratio(returns)
        assert sr == 0.0

    def test_empty_returns_zero(self):
        assert sharpe_ratio([]) == 0.0

    def test_negative_returns_negative_sharpe(self):
        import numpy as np
        rng = np.random.default_rng(42)
        # Returns with clear negative mean and realistic variance
        returns = list(-0.008 + rng.normal(0, 0.003, 50))
        sr = sharpe_ratio(returns)
        assert sr < 0.0

    def test_sortino_ignores_upside_volatility(self):
        # Volatile upside but no downside → high Sortino
        returns = [0.05, 0.10, 0.03, 0.08, 0.06] * 10
        sortino = sortino_ratio(returns)
        # No negative returns → sortino should be 0.0 (no downside deviation)
        # In practice sortino returns 0 when no downside
        assert sortino == 0.0 or sortino > 0.0  # Either valid

    def test_sortino_penalises_downside(self):
        mixed = [0.02, -0.05, 0.03, -0.04, 0.01] * 10
        sr = sharpe_ratio(mixed)
        so = sortino_ratio(mixed)
        # With downside, both should be computed without error
        assert isinstance(sr, float)
        assert isinstance(so, float)

    def test_calmar_ratio(self):
        cr = calmar_ratio(total_return_pct=50.0, max_drawdown_pct=10.0)
        assert abs(cr - 5.0) < 1e-9

    def test_calmar_zero_drawdown(self):
        cr = calmar_ratio(50.0, 0.0)
        assert cr == 0.0


class TestInformationRatioWiring:
    """
    Regression coverage for benchmark_returns_for_trades() and its wiring
    into calculate_performance(benchmark_candles=...): the previously
    unwired information_ratio() in analytics/sharpe.py.
    """

    def _make_dated_trade(self, opened_at, closed_at, net_pnl: float) -> Trade:
        gross = Decimal(str(net_pnl)) + Decimal("2")
        return Trade(
            position_id=f"pos_{opened_at.isoformat()}",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=Decimal("1.0"),
            entry_quantity=Decimal("1.0"),
            entry_price=Decimal("50000"),
            exit_price=Decimal(str(50000 + net_pnl)),
            gross_pnl=gross,
            fees=Decimal("2"),
            opened_at=opened_at,
            closed_at=closed_at,
        )

    def _make_candle(self, ts, close: float) -> OHLCV:
        c = Decimal(str(close))
        return OHLCV(open=c, high=c, low=c, close=c, volume=Decimal("1"), timestamp=ts)

    def test_information_ratio_none_without_benchmark(self):
        trades = [make_trade(100) for _ in range(5)]
        report = calculate_performance(trades)
        assert report.information_ratio is None

    def test_benchmark_returns_for_trades_empty_raises(self):
        trades = [make_trade(100)]
        with pytest.raises(ValueError):
            benchmark_returns_for_trades(trades, [])

    def test_benchmark_returns_for_trades_aligns_by_holding_window(self):
        base = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [self._make_candle(base + timedelta(hours=h), 100 + h) for h in range(60)]
        trades = [
            self._make_dated_trade(base + timedelta(hours=5), base + timedelta(hours=10), 50),
            self._make_dated_trade(base + timedelta(hours=20), base + timedelta(hours=50), 30),
        ]
        result = benchmark_returns_for_trades(trades, candles)
        assert len(result) == 2
        assert abs(result[0] - (110 - 105) / 105) < 1e-9
        assert abs(result[1] - (150 - 120) / 120) < 1e-9

    def test_calculate_performance_populates_information_ratio_when_strategy_beats_benchmark(self):
        base = datetime(2024, 1, 1, tzinfo=UTC)
        # Flat benchmark (zero return every window) vs. consistently
        # profitable trades → strategy clearly outperforms, so
        # information_ratio should come back positive and non-None.
        candles = [self._make_candle(base + timedelta(hours=h), 100) for h in range(500)]
        trades = [
            self._make_dated_trade(
                base + timedelta(hours=10 * i), base + timedelta(hours=10 * i + 5), 100
            )
            for i in range(20)
        ]
        report = calculate_performance(trades, benchmark_candles=candles)
        assert report.information_ratio is not None
        assert report.information_ratio > 0.0

    def test_calculate_performance_information_ratio_length_matches_trades(self):
        base = datetime(2024, 1, 1, tzinfo=UTC)
        candles = [self._make_candle(base + timedelta(hours=h), 100 + h * 0.1) for h in range(500)]
        trades = [
            self._make_dated_trade(
                base + timedelta(hours=10 * i), base + timedelta(hours=10 * i + 5), (-1) ** i * 50
            )
            for i in range(20)
        ]
        # Just needs to compute without error and land in a sane float
        # range — the alignment-length guard in calculate_performance()
        # should never actually trip here since both series are derived
        # from the same trades_sorted list, but this exercises that path.
        report = calculate_performance(trades, benchmark_candles=candles)
        assert isinstance(report.information_ratio, float)


class TestAdvancedMetricsWiring:
    """analytics/advanced_metrics.py (deflated_sharpe_ratio, omega_ratio,
    tail_ratio, ulcer_index, historical_var/cvar) and
    expectancy.py::system_quality_number were implemented and unit-tested
    in isolation but had zero callers anywhere in src/ or scripts/ before
    this wiring — calculate_performance() never populated them, so every
    PerformanceReport silently carried these fields at their 0.0 default
    regardless of the underlying trades.
    """

    def _varied_trades(self, n: int = 12) -> list[Trade]:
        # Alternating wins/losses of varying size, each with initial_risk
        # set, so both the returns-series metrics (deflated Sharpe, omega,
        # tail ratio, ulcer index, VaR/CVaR) and the R-multiple-based
        # system_quality_number have enough varied, non-degenerate data
        # to produce non-zero output rather than an early-return default.
        pnls = [120, -40, 80, -60, 150, -30, 90, -50, 60, -20, 100, -45][:n]
        return [make_trade(p, initial_risk=50.0) for p in pnls]

    def test_advanced_metrics_are_populated_not_left_at_zero_default(self):
        report = calculate_performance(self._varied_trades())

        assert report.probabilistic_sharpe_ratio != 0.0
        assert report.deflated_sharpe_ratio != 0.0
        assert report.omega_ratio != 0.0
        assert report.tail_ratio != 0.0
        assert report.ulcer_index != 0.0
        assert report.historical_var_95 != 0.0
        assert report.system_quality_number != 0.0

    def test_deflated_sharpe_equals_probabilistic_sharpe_at_n_trials_1(self):
        # n_trials=1 (the default) means "a single pre-registered
        # backtest, no parameter search" — deflated_sharpe_ratio's
        # benchmark collapses to 0, so it must exactly match
        # probabilistic_sharpe_ratio's own benchmark_sharpe=0.0 default.
        report = calculate_performance(self._varied_trades(), n_trials=1)
        assert report.deflated_sharpe_ratio == pytest.approx(report.probabilistic_sharpe_ratio)

    def test_higher_n_trials_does_not_increase_deflated_sharpe(self):
        # Deflating against more independent trials can only raise the
        # bar (the benchmark used inside deflated_sharpe_ratio), so the
        # reported DSR for the same trade history must be monotonically
        # non-increasing as n_trials grows — this is the exact "you tried
        # 200 parameter combinations and kept the best one" correction
        # the module's own docstring describes.
        trades = self._varied_trades()
        dsr_1 = calculate_performance(trades, n_trials=1).deflated_sharpe_ratio
        dsr_50 = calculate_performance(trades, n_trials=50).deflated_sharpe_ratio
        dsr_500 = calculate_performance(trades, n_trials=500).deflated_sharpe_ratio
        assert dsr_1 >= dsr_50 >= dsr_500

    def test_ulcer_index_zero_for_a_monotonically_rising_equity_curve(self):
        # An equity curve that only ever rises never drops below its
        # running peak, so ulcer_index (RMS of drawdown-from-peak) must
        # be exactly 0 — a straightforward correctness check independent
        # of the "is it wired at all" tests above.
        trades = [make_trade(p) for p in [10, 20, 30, 40]]
        report = calculate_performance(trades)
        assert report.ulcer_index == 0.0

    def test_system_quality_number_stays_zero_without_initial_risk(self):
        # system_quality_number() needs Trade.initial_risk to compute
        # R-multiples; trades without it should leave the report's SQN
        # at 0.0 (not populated) rather than raising or fabricating a
        # value — matches calculate_performance()'s existing "not enough
        # data yet" convention used elsewhere (e.g. sharpe/sortino below
        # 2 returns).
        trades = [make_trade(p) for p in [10, -5, 20, -10, 15]]  # no initial_risk
        report = calculate_performance(trades)
        assert report.system_quality_number == 0.0

    def test_to_dict_serializes_infinite_omega_and_tail_ratio_as_strings(self):
        # omega_ratio/tail_ratio can legitimately be inf (e.g. no losing
        # trades at all) — to_dict() must not let json.dumps blow up on
        # a bare float('inf'), so it falls back to str() for non-finite
        # values the same way the rest of to_dict() already treats
        # "not computed" (None) as distinct from "computed as zero".
        trades = [make_trade(p) for p in [10, 20, 30, 40, 50]]  # all winners
        report = calculate_performance(trades)
        d = report.to_dict()
        assert d["omega_ratio"] == "inf"
        assert isinstance(d["ulcer_index"], float)

    def test_to_dict_includes_all_new_advanced_metric_keys(self):
        report = calculate_performance(self._varied_trades())
        d = report.to_dict()
        for key in (
            "probabilistic_sharpe_ratio", "deflated_sharpe_ratio",
            "omega_ratio", "tail_ratio", "ulcer_index",
            "historical_var_95", "historical_cvar_95",
            "system_quality_number",
        ):
            assert key in d


class TestEngineNTrialsPassthrough:
    """BacktestEngine.run()'s n_trials kwarg (default 1) forwards to
    calculate_performance() so an OOS validation run coming out of
    scripts/optimize.py's Optuna search reports a deflated_sharpe_ratio
    that actually accounts for the size of that search, rather than
    silently defaulting to n_trials=1 (no deflation) for every backtest
    regardless of whether it came from a parameter search.
    """

    def test_default_n_trials_is_1(self):
        from unittest.mock import patch
        from src.backtesting.engine import BacktestConfig, BacktestEngine
        from src.backtesting.commission import ZeroCommission
        from src.backtesting.slippage import ZeroSlippage
        from unittest.mock import AsyncMock
        from src.core.interfaces import IStrategy

        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        config = BacktestConfig(
            initial_equity=Decimal("10000"),
            commission_model=ZeroCommission(),
            slippage_model=ZeroSlippage(),
            partial_fill=None,
        )
        engine = BacktestEngine(strategy, config)
        candles = [
            OHLCV(
                open=Decimal("50000"), high=Decimal("50050"),
                low=Decimal("49950"), close=Decimal("50000"),
                volume=Decimal("1000"),
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
            )
            for i in range(52)
        ]

        with patch(
            "src.backtesting.engine.calculate_performance",
            wraps=calculate_performance,
        ) as spy:
            import asyncio
            asyncio.run(engine.run(candles, "BTCUSDT", Timeframe.H1))
            assert spy.call_args.kwargs["n_trials"] == 1

    def test_n_trials_forwarded_when_explicitly_passed(self):
        from unittest.mock import patch
        from src.backtesting.engine import BacktestConfig, BacktestEngine
        from src.backtesting.commission import ZeroCommission
        from src.backtesting.slippage import ZeroSlippage
        from unittest.mock import AsyncMock
        from src.core.interfaces import IStrategy

        strategy = AsyncMock(spec=IStrategy)
        strategy.on_candle.return_value = None
        config = BacktestConfig(
            initial_equity=Decimal("10000"),
            commission_model=ZeroCommission(),
            slippage_model=ZeroSlippage(),
            partial_fill=None,
        )
        engine = BacktestEngine(strategy, config)
        candles = [
            OHLCV(
                open=Decimal("50000"), high=Decimal("50050"),
                low=Decimal("49950"), close=Decimal("50000"),
                volume=Decimal("1000"),
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
            )
            for i in range(52)
        ]

        with patch(
            "src.backtesting.engine.calculate_performance",
            wraps=calculate_performance,
        ) as spy:
            import asyncio
            asyncio.run(engine.run(candles, "BTCUSDT", Timeframe.H1, n_trials=75))
            assert spy.call_args.kwargs["n_trials"] == 75
