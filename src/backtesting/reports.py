"""Backtest report generation."""

from __future__ import annotations

import math
from pathlib import Path

from src.backtesting.engine import BacktestResult
from src.utils.files import write_csv, write_json


def save_backtest_report(result: BacktestResult, output_dir: Path) -> None:
    """Save backtest results to JSON and CSV files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"{result.symbol}_{result.timeframe.value}"

    # Summary JSON
    write_json(output_dir / f"{name}_summary.json", result.summarize())

    # Trade CSV
    trade_rows = [
        {
            "trade_id": t.trade_id,
            "symbol": t.symbol,
            "side": t.side.value,
            "quantity": str(t.quantity),
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "gross_pnl": str(t.gross_pnl),
            "fees": str(t.fees),
            "net_pnl": str(t.net_pnl),
            "opened_at": t.opened_at.isoformat(),
            "closed_at": t.closed_at.isoformat(),
            "duration_hours": round(t.duration_hours, 2),
        }
        for t in result.trades
    ]
    if trade_rows:
        write_csv(output_dir / f"{name}_trades.csv", trade_rows)

    # Equity curve CSV
    equity_rows = [{"bar": i, "equity": str(e)} for i, e in enumerate(result.equity_curve)]
    write_csv(output_dir / f"{name}_equity.csv", equity_rows)


def print_backtest_summary(result: BacktestResult) -> None:
    """Print a formatted backtest summary to stdout."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    p = result.performance
    if p is None:
        console.print("[yellow]No performance data available[/yellow]")
        return

    table = Table(title=f"Backtest: {result.symbol} {result.timeframe.value}", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    rows = [
        ("Period", f"{result.start_date.date()} → {result.end_date.date()}"),
        ("Candles Processed", str(result.candles_processed)),
        ("Signals Generated", str(result.signals_generated)),
        ("Total Trades", str(p.total_trades)),
        ("Win Rate", f"{p.win_rate * 100:.1f}%"),
        ("Profit Factor", f"{p.profit_factor:.2f}"),
        ("Total Net PnL", str(p.total_net_pnl)),
        ("Avg Trade PnL", str(p.avg_trade_pnl)),
        ("Largest Winner", str(p.largest_winner)),
        ("Largest Loser", str(p.largest_loser)),
        ("Max Drawdown", f"{p.max_drawdown_pct:.2f}%"),
        ("Sharpe Ratio", f"{p.sharpe_ratio:.2f}"),
        ("Sortino Ratio", f"{p.sortino_ratio:.2f}"),
        ("Calmar Ratio", f"{p.calmar_ratio:.2f}"),
    ]
    if p.information_ratio is not None:
        rows.append(("Information Ratio", f"{p.information_ratio:.2f}"))
    rows += [
        ("Ulcer Index", f"{p.ulcer_index:.2f}"),
        ("Prob. Sharpe Ratio", f"{p.probabilistic_sharpe_ratio:.2f}"),
        ("Deflated Sharpe Ratio", f"{p.deflated_sharpe_ratio:.2f}"),
        (
            "Omega Ratio",
            f"{p.omega_ratio:.2f}" if math.isfinite(p.omega_ratio) else str(p.omega_ratio),
        ),
        ("Tail Ratio", f"{p.tail_ratio:.2f}" if math.isfinite(p.tail_ratio) else str(p.tail_ratio)),
        ("Historical VaR (95%)", f"{p.historical_var_95 * 100:.2f}%"),
        ("Historical CVaR (95%)", f"{p.historical_cvar_95 * 100:.2f}%"),
        ("System Quality Number", f"{p.system_quality_number:.2f}"),
        ("Max Consec. Wins", str(p.max_consecutive_wins)),
        ("Max Consec. Losses", str(p.max_consecutive_losses)),
        ("Avg Trade Duration", f"{p.avg_trade_duration_hours:.1f}h"),
    ]

    for metric, value in rows:
        table.add_row(metric, value)

    console.print(table)
