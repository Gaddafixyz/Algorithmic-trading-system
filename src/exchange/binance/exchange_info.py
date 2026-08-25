"""
Binance exchange info: symbol rules, precision, and instrument metadata.

Parses the /fapi/v1/exchangeInfo endpoint into Instrument domain objects.
Instruments are cached in-memory and refreshed periodically.

Binance (2026-07-07): a new symbolStatus value ``CANCEL_ONLY`` may appear.
Symbols in this state accept only cancellations, not new orders.  They are
correctly excluded from the active instrument set, but logged distinctly to
aid debugging (previously they would fall into the generic "skipped" path
alongside broken data).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.core.enums import InstrumentType, MarginType
from src.domain.instrument import Instrument

logger = logging.getLogger(__name__)

# Statuses that are not errors but mean "not tradeable right now".
_NON_TRADING_STATUSES = frozenset({"HALT", "BREAK", "PRE_DELIVERING", "END_OF_DAY", "CANCEL_ONLY"})


def parse_instruments(exchange_info: dict[str, Any]) -> dict[str, Instrument]:
    """
    Parse raw Binance exchangeInfo into a dict of symbol -> Instrument.

    Only active PERPETUAL contracts are included.
    """
    instruments: dict[str, Instrument] = {}

    for sym in exchange_info.get("symbols", []):
        try:
            contract_type = sym.get("contractType", "")
            status = sym.get("status", "")

            if contract_type != "PERPETUAL":
                continue

            if status == "CANCEL_ONLY":
                # Symbol is in order-cancellation-only mode (Binance 2026-07-07).
                # No new orders can be placed; exclude from active set and log
                # so operators are aware.
                logger.info(
                    "Skipping %s: status=CANCEL_ONLY (cancellations only, no new orders)",
                    sym.get("symbol", "UNKNOWN"),
                )
                continue

            if status != "TRADING":
                logger.debug(
                    "Skipping %s: status=%s (not TRADING)",
                    sym.get("symbol", "UNKNOWN"),
                    status,
                )
                continue

            symbol = sym["symbol"]
            base = sym["baseAsset"]
            quote = sym["quoteAsset"]
            max_leverage = int(
                sym.get("leverageBracket", [{"initialLeverage": 20}])[0].get("initialLeverage", 20)
            )

            # Parse filters
            filters = {f["filterType"]: f for f in sym.get("filters", [])}

            price_filter = filters.get("PRICE_FILTER", {})
            lot_size = filters.get("LOT_SIZE", {})
            min_notional = filters.get("MIN_NOTIONAL", {})
            filters.get("MARKET_LOT_SIZE", lot_size)

            tick_size = Decimal(str(price_filter.get("tickSize", "0.01")))
            min_price = Decimal(str(price_filter.get("minPrice", "0")))
            max_price = Decimal(str(price_filter.get("maxPrice", "1000000")))

            step_size = Decimal(str(lot_size.get("stepSize", "0.001")))
            min_qty = Decimal(str(lot_size.get("minQty", "0.001")))
            max_qty = Decimal(str(lot_size.get("maxQty", "1000000")))

            notional_min = Decimal(str(min_notional.get("notional", "5")))

            instrument = Instrument(
                symbol=symbol,
                base_asset=base,
                quote_asset=quote,
                instrument_type=InstrumentType.PERPETUAL,
                tick_size=tick_size,
                min_price=min_price,
                max_price=max_price,
                step_size=step_size,
                min_qty=min_qty,
                max_qty=max_qty,
                min_notional=notional_min,
                max_leverage=max_leverage,
                default_leverage=min(20, max_leverage),
                margin_type=MarginType.ISOLATED,
                is_active=True,
            )
            instruments[symbol] = instrument

        except Exception as exc:
            sym_name = sym.get("symbol", "UNKNOWN")
            logger.warning("Failed to parse instrument %s: %s", sym_name, exc)
            continue

    logger.info("Parsed %d active perpetual instruments", len(instruments))
    return instruments
