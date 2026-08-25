"""
Export historical candle data to CSV.

Usage:
    python scripts/export_data.py --symbol BTCUSDT --timeframe 1h --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import get_settings
from src.core.enums import Timeframe
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.monitoring.telemetry import configure_logging


async def export_candles(
    symbol: str,
    timeframe: str,
    days: int,
    output: Path,
) -> None:
    configure_logging("WARNING")
    settings = get_settings()
    if settings.binance is None:
        print("ERROR: Binance credentials required")
        sys.exit(1)

    try:
        tf = Timeframe(timeframe)
    except ValueError:
        print(f"ERROR: Invalid timeframe {timeframe}")
        sys.exit(1)

    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )

    async with client:
        start = datetime.now(tz=UTC) - timedelta(days=days)
        candles = await client.get_candles(symbol, tf, limit=1500, start_time=start)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for c in candles:
            writer.writerow(
                {
                    "timestamp": c.timestamp.isoformat(),
                    "open": str(c.open),
                    "high": str(c.high),
                    "low": str(c.low),
                    "close": str(c.close),
                    "volume": str(c.volume),
                }
            )

    print(f"Exported {len(candles)} candles to {output}")


def main() -> None:
    p = argparse.ArgumentParser(description="Export candle data to CSV")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--output", type=Path, default=Path("./data/candles.csv"))
    args = p.parse_args()

    asyncio.run(export_candles(args.symbol, args.timeframe, args.days, args.output))


if __name__ == "__main__":
    main()
