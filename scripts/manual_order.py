"""
Manual bracket order placement — operator tool, NOT part of the automated
trading path.

This is the disposition for the previously-orphaned BinanceOrderManager:
wired in here, as a deliberate, explicitly-confirmed manual action, rather
than into ExecutionEngine's automated signal-to-order pipeline.

Why not the automated path: ExecutionEngine -> OrderManager (the
exchange-agnostic one in src/execution/order_manager.py) is what every
live signal actually goes through, and it does several things
BinanceOrderManager does not:
  - RiskManager pre-trade validation (position sizing, exposure limits,
    circuit breaker state) happens upstream of OrderManager.submit_order()
  - idempotent submission via client_order_id dedup
  - OrderSubmitted/StatusChanged/Rejected/Cancelled events published to
    the bus (dashboards, notifier, audit trail all depend on these)
  - ambiguous-failure recovery (checks whether an order that appeared to
    fail actually landed, before allowing a retry)
  - order persistence for crash recovery
  - works for both Binance and Bybit (BinanceOrderManager is Binance-only)

Routing automated signals through BinanceOrderManager instead would mean
every live trade silently skips all of the above. That's a regression,
not a wiring fix, so it isn't done here or anywhere in src/.

What BinanceOrderManager *is* genuinely useful for — and what it's wired
into here — is a human placing a one-off bracket trade (entry + stop +
target) outside the strategy engine: manual intervention, closing a gap
the strategy won't take, testing exchange behavior against a specific
setup, etc. That's an operator action, so this script requires an
explicit --confirm flag and prints the full order plan before doing
anything.

Usage:
    python scripts/manual_order.py --symbol BTCUSDT --side buy \
        --quantity 0.01 --stop 60000 --target 65000 --confirm

    python scripts/manual_order.py --symbol BTCUSDT --side buy \
        --quantity 0.01 --entry 62000 --stop 60000 --target 65000 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import get_settings
from src.core.enums import OrderSide
from src.exchange.binance.exchange_info import parse_instruments
from src.exchange.binance.orders import BinanceOrderManager
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.monitoring.telemetry import configure_logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Manually place a bracket order (entry + stop + target) on Binance. "
        "Operator tool — bypasses the strategy engine and its risk checks entirely."
    )
    p.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--quantity", required=True, type=Decimal)
    p.add_argument("--stop", required=True, type=Decimal, help="Stop-loss price")
    p.add_argument("--target", required=True, type=Decimal, help="Take-profit price")
    p.add_argument(
        "--entry", type=Decimal, default=None,
        help="Limit entry price. Omit for a market entry.",
    )
    p.add_argument(
        "--confirm", action="store_true",
        help="Required. Without this flag the order plan is printed but nothing is sent.",
    )
    return p.parse_args()


async def main() -> None:
    configure_logging("INFO")
    args = parse_args()
    settings = get_settings()

    if settings.binance is None:
        logger.error("Binance credentials required in .env")
        sys.exit(1)

    side = OrderSide.BUY if args.side == "buy" else OrderSide.SELL

    print("=" * 60)
    print("MANUAL BRACKET ORDER — operator action, not a strategy signal")
    print("=" * 60)
    print(f"  Symbol:   {args.symbol}")
    print(f"  Side:     {side.value}")
    print(f"  Quantity: {args.quantity}")
    print(f"  Entry:    {'MARKET' if args.entry is None else args.entry}")
    print(f"  Stop:     {args.stop}")
    print(f"  Target:   {args.target}")
    print("=" * 60)

    if not args.confirm:
        print("Dry run only — pass --confirm to actually place these orders.")
        return

    client = BinanceFuturesRESTClient(
        api_key=settings.binance.api_key.get_secret_value(),
        api_secret=settings.binance.api_secret.get_secret_value(),
        base_url=settings.binance.rest_base_url,
    )

    async with client:
        exchange_info = await client.get_exchange_info()
        instruments = parse_instruments(exchange_info)
        instrument = instruments.get(args.symbol.upper())
        if instrument is None:
            logger.error("Unknown symbol: %s", args.symbol)
            sys.exit(1)

        manager = BinanceOrderManager(client)
        result = await manager.place_bracket_order(
            args.symbol, side, args.quantity, instrument,
            stop_price=args.stop,
            take_profit_price=args.target,
            entry_price=args.entry,
        )

    legs = ", ".join(
        f"{leg}={order.exchange_order_id}" for leg, order in result.items()
    )
    print(f"Placed: {legs}")
    missing = {"entry", "stop_loss", "take_profit"} - result.keys()
    if missing:
        print(f"WARNING — legs that did NOT land: {sorted(missing)}. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
