"""Unit tests for src.backtesting.reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.analytics.performance import calculate_performance
from src.backtesting.engine import BacktestConfig, BacktestResult
from src.backtesting.reports import print_backtest_summary, save_backtest_report
from src.core.enums import PositionSide, Timeframe
from src.domain.trade import Trade


def make_trade(gross_pnl: str = "100") -> Trade:
    return Trade(
        position_id="pos-1", symbol="BTCUSDT", side=PositionSide.LONG,
        quantity=Decimal("0.1"), entry_quantity=Decimal("0.1"),
        entry_price=Decimal("50000"), exit_price=Decimal("51000"),
        gross_pnl=Decimal(gross_pnl), fees=Decimal("2"),
        opened_at=datetime(2024, 1, 1, tzinfo=UTC),
        closed_at=datetime(2024, 1, 1, 2, tzinfo=UTC),
    )


def make_result(trades: list[Trade] | None = None) -> BacktestResult:
    trades = trades or []
    result = BacktestResult(
        config=BacktestConfig(), symbol="BTCUSDT", timeframe=Timeframe.H1,
        start_date=datetime(2024, 1, 1, tzinfo=UTC), end_date=datetime(2024, 1, 2, tzinfo=UTC),
        candles_processed=100, signals_generated=5, trades=trades,
        equity_curve=[Decimal("10000"), Decimal("10100"), Decimal("10050")],
    )
    result.performance = calculate_performance(trades, initial_equity=10000.0)
    return result


class TestSaveBacktestReport:
    def test_creates_the_output_directory_if_missing(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "nested" / "reports"
        save_backtest_report(make_result(), output_dir)
        assert output_dir.is_dir()

    def test_writes_a_summary_json_file(self, tmp_path: Path) -> None:
        save_backtest_report(make_result(), tmp_path)
        summary_file = tmp_path / "BTCUSDT_1h_summary.json"
        assert summary_file.exists()
        data = json.loads(summary_file.read_text())
        assert data["symbol"] == "BTCUSDT"
        assert data["candles"] == 100

    def test_writes_a_trades_csv_when_trades_exist(self, tmp_path: Path) -> None:
        save_backtest_report(make_result(trades=[make_trade()]), tmp_path)
        trades_file = tmp_path / "BTCUSDT_1h_trades.csv"
        assert trades_file.exists()
        content = trades_file.read_text()
        assert "trade_id" in content.splitlines()[0]

    def test_does_not_write_a_trades_csv_when_there_are_no_trades(
        self, tmp_path: Path
    ) -> None:
        save_backtest_report(make_result(trades=[]), tmp_path)
        trades_file = tmp_path / "BTCUSDT_1h_trades.csv"
        assert not trades_file.exists()

    def test_always_writes_an_equity_curve_csv(self, tmp_path: Path) -> None:
        save_backtest_report(make_result(), tmp_path)
        equity_file = tmp_path / "BTCUSDT_1h_equity.csv"
        assert equity_file.exists()
        lines = equity_file.read_text().splitlines()
        assert len(lines) == 4  # header + 3 equity points


class TestPrintBacktestSummary:
    def test_does_not_raise_when_performance_is_none(self, capsys) -> None:
        result = BacktestResult(
            config=BacktestConfig(), symbol="BTCUSDT", timeframe=Timeframe.H1,
            start_date=datetime(2024, 1, 1, tzinfo=UTC), end_date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        print_backtest_summary(result)  # must not raise

    def test_does_not_raise_with_a_real_performance_report(self) -> None:
        result = make_result(trades=[make_trade(), make_trade(gross_pnl="-50")])
        print_backtest_summary(result)  # must not raise
