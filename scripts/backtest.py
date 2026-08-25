"""
Backtest runner script.

Usage:
    python scripts/backtest.py --symbol BTCUSDT --timeframe 1h --days 90
    python scripts/backtest.py --symbol ETHUSDT --timeframe 4h --days 365 --output ./results
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtesting.commission import PercentageCommission
from src.backtesting.engine import BacktestConfig, BacktestEngine
from src.backtesting.reports import print_backtest_summary, save_backtest_report
from src.backtesting.slippage import FixedSlippage
from src.core.config import get_settings
from src.core.enums import Timeframe
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.main import SMCStrategy
from src.monitoring.telemetry import configure_logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SMC Strategy Backtest Runner")
    p.add_argument("--symbol", default="BTCUSDT", help="Trading symbol")
    p.add_argument("--timeframe", default="1h", help="Candle timeframe")
    p.add_argument("--htf", default="4h", help="Higher timeframe for bias")
    p.add_argument("--ltf", default="15m", help="Lower timeframe for MTFContext LTF tier")
    p.add_argument("--days", type=int, default=90, help="History days")
    p.add_argument("--equity", type=float, default=10_000.0, help="Starting equity")
    p.add_argument("--risk-pct", type=float, default=1.0, help="Risk per trade %%")
    p.add_argument("--min-score", type=float, default=60.0, help="Min confluence score")
    p.add_argument("--output", type=Path, default=Path("./backtest_results"), help="Output dir")
    p.add_argument("--save", action="store_true", help="Save results to files")
    p.add_argument(
        "--no-funding",
        dest="funding",
        action="store_false",
        help="Skip fetching/applying historical funding-rate accrual",
    )
    p.set_defaults(funding=True)
    return p.parse_args()


async def run_backtest(args: argparse.Namespace) -> None:
    configure_logging("INFO")
    settings = get_settings()

    if settings.binance is None:
        logger.error("Binance credentials required in .env")
        sys.exit(1)

    # Fetch historical data
    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )

    try:
        tf = Timeframe(args.timeframe)
    except ValueError:
        logger.error("Invalid timeframe: %s", args.timeframe)
        sys.exit(1)

    logger.info(
        "Fetching %d days of %s %s history...",
        args.days,
        args.symbol,
        args.timeframe,
    )

    async with client:
        # Fetch candles in chunks (max 1500 per request)
        all_candles = []
        start = datetime.now(tz=UTC) - timedelta(days=args.days)
        candle_batch = await client.get_candles(args.symbol, tf, limit=1500, start_time=start)
        all_candles.extend(candle_batch)

        # Fetch HTF candles for bias
        try:
            htf = Timeframe(args.htf)
            htf_candles = await client.get_candles(args.symbol, htf, limit=500)
        except ValueError:
            htf_candles = None

        # Fetch LTF candles for the MTFContext LTF tier — same pattern as
        # HTF above. Previously there was no candle source for this tier
        # anywhere in the codebase (see CHANGELOG [1.3.2] scope note).
        try:
            ltf = Timeframe(args.ltf)
            ltf_candles = await client.get_candles(args.symbol, ltf, limit=500)
        except ValueError:
            ltf_candles = None

        # Fetch historical funding-rate settlements over the same window so
        # the backtest accrues real funding cost/credit instead of ignoring
        # it (BacktestSimulator._apply_funding_rates has always supported
        # this; nothing previously called the REST endpoint to feed it).
        funding_rates = None
        if args.funding:
            try:
                start_ms = int(start.timestamp() * 1000)
                end_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
                funding_rates = await client.fetch_funding_rate_history_parsed(
                    args.symbol, start_time=start_ms, end_time=end_ms
                )
                logger.info(
                    "Fetched %d funding-rate settlements for %s",
                    len(funding_rates),
                    args.symbol,
                )
            except Exception:
                logger.warning(
                    "Failed to fetch funding-rate history; backtest will "
                    "run without funding accrual",
                    exc_info=True,
                )
                funding_rates = None

    if len(all_candles) < 50:
        logger.error("Insufficient data: only %d candles fetched", len(all_candles))
        sys.exit(1)

    logger.info(
        "Fetched %d candles (%s → %s)",
        len(all_candles),
        all_candles[0].timestamp.date(),
        all_candles[-1].timestamp.date(),
    )

    # Build strategy
    strategy = SMCStrategy(
        symbols=[args.symbol],
        timeframes=[tf],
        htf_timeframe=Timeframe(args.htf) if htf_candles else None,
        ltf_timeframe=Timeframe(args.ltf) if ltf_candles else None,
        min_score=args.min_score,
    )

    # Build engine
    config = BacktestConfig(
        initial_equity=Decimal(str(args.equity)),
        commission_model=PercentageCommission(),
        slippage_model=FixedSlippage(),
        risk_pct_per_trade=Decimal(str(args.risk_pct)),
        funding_rates=funding_rates,
    )
    engine = BacktestEngine(strategy, config)

    # Run
    result = await engine.run(
        all_candles,
        args.symbol,
        tf,
        htf_candles=htf_candles,
        htf_timeframe=Timeframe(args.htf) if htf_candles else None,
        ltf_candles=ltf_candles,
        ltf_timeframe=Timeframe(args.ltf) if ltf_candles else None,
    )

    # Report
    print_backtest_summary(result)

    if args.save:
        save_backtest_report(result, args.output)
        logger.info("Results saved to %s", args.output)


def main() -> None:
    args = parse_args()
    asyncio.run(run_backtest(args))


if __name__ == "__main__":
    main()
