"""
Bybit V5 (USDT perpetual, category="linear") API response parser.

Converts raw exchange JSON into the same domain objects used by the
Binance adapter, so the rest of the system (execution, risk, persistence)
is completely exchange-agnostic. All Decimal conversions go through
``str()`` to avoid float imprecision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.enums import OrderSide, OrderStatus, OrderType, PositionSide, TimeInForce
from src.core.models import OHLCV, BookLevel, OrderBook, Ticker
from src.domain.order import Order
from src.domain.position import Position

# ─── Timeframe mapping (Bybit uses bare interval codes) ───────────────────────

TIMEFRAME_TO_BYBIT: dict[str, str] = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "Created": OrderStatus.SUBMITTED,
    "New": OrderStatus.OPEN,
    "Rejected": OrderStatus.REJECTED,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "PartiallyFilledCanceled": OrderStatus.CANCELLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Deactivated": OrderStatus.CANCELLED,
    "Triggered": OrderStatus.OPEN,
}

_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "Market": OrderType.MARKET,
    "Limit": OrderType.LIMIT,
}

_ORDER_SIDE_MAP: dict[str, OrderSide] = {
    "Buy": OrderSide.BUY,
    "Sell": OrderSide.SELL,
}

_TIF_MAP: dict[str, TimeInForce] = {
    "GTC": TimeInForce.GTC,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
    "PostOnly": TimeInForce.GTX,
}


def _d(value: Any) -> Decimal:
    """Safely convert any value to Decimal via str; blank strings -> 0."""
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _ts(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)


_LIMIT_ORDER_TYPES: frozenset[OrderType] = frozenset(
    {
        OrderType.LIMIT,
        OrderType.STOP_LIMIT,
        OrderType.TAKE_PROFIT_LIMIT,
    }
)


def to_bybit_order_type(order_type: OrderType) -> str:
    """
    Map internal OrderType to Bybit's order type + trigger semantics.

    Bybit V5 only has Market/Limit at the ``orderType`` level; stop and
    take-profit behaviour is expressed via ``triggerPrice`` +
    ``triggerDirection`` on top of a Market or Limit order, which the
    REST client sets separately. This function returns just the base type.

    Any order type carrying a limit price (``LIMIT``, ``STOP_LIMIT``,
    ``TAKE_PROFIT_LIMIT``) maps to Bybit's ``"Limit"``; everything else
    (``MARKET``, ``STOP_MARKET``, ``TAKE_PROFIT_MARKET``,
    ``TRAILING_STOP_MARKET``) maps to ``"Market"``.
    """
    if order_type in _LIMIT_ORDER_TYPES:
        return "Limit"
    return "Market"


def parse_order(data: dict[str, Any]) -> Order:
    """Parse a Bybit V5 order object (from create/get/list-order endpoints)."""
    raw_status = data.get("orderStatus", "New")
    status = _ORDER_STATUS_MAP.get(raw_status, OrderStatus.OPEN)

    raw_type = data.get("orderType", "Market")
    order_type = _ORDER_TYPE_MAP.get(raw_type, OrderType.MARKET)

    raw_side = data.get("side", "Buy")
    side = _ORDER_SIDE_MAP.get(raw_side, OrderSide.BUY)

    raw_tif = data.get("timeInForce", "GTC")
    tif = _TIF_MAP.get(raw_tif, TimeInForce.GTC)

    price = _d(data.get("price")) or None
    if price is not None and price <= Decimal("0"):
        price = None

    avg_price = _d(data.get("avgPrice")) or None
    if avg_price is not None and avg_price <= Decimal("0"):
        avg_price = None

    stop_price = _d(data.get("triggerPrice")) or None
    if stop_price is not None and stop_price <= Decimal("0"):
        stop_price = None

    return Order(
        order_id=str(data.get("orderId", "")),
        client_order_id=str(data.get("orderLinkId", "")),
        symbol=str(data.get("symbol", "")),
        side=side,
        order_type=order_type,
        quantity=_d(data.get("qty", "0")),
        filled_quantity=_d(data.get("cumExecQty", "0")),
        price=price,
        stop_price=stop_price,
        average_fill_price=avg_price,
        time_in_force=tif,
        reduce_only=bool(data.get("reduceOnly", False)),
        status=status,
        exchange_order_id=str(data.get("orderId", "")),
    )


def parse_position(data: dict[str, Any]) -> Position | None:
    """Parse a Bybit V5 position object. Returns None for a flat (size=0) entry."""
    size = _d(data.get("size", "0"))
    if size == Decimal("0"):
        return None

    entry_price = _d(data.get("avgPrice", "0"))
    if entry_price <= Decimal("0"):
        return None

    side_raw = data.get("side", "Buy")
    side = PositionSide.LONG if side_raw == "Buy" else PositionSide.SHORT

    leverage_raw = data.get("leverage", "1")
    leverage = int(float(leverage_raw)) if leverage_raw else 1

    liq_price = _d(data.get("liqPrice"))

    return Position(
        symbol=str(data.get("symbol", "")),
        side=side,
        quantity=size,
        entry_quantity=size,
        entry_price=entry_price,
        mark_price=_d(data.get("markPrice", str(entry_price))),
        liquidation_price=liq_price if liq_price > Decimal("0") else None,
        leverage=leverage,
        initial_margin=_d(data.get("positionIM", "0")),
        maintenance_margin=_d(data.get("positionMM", "0")),
        realized_pnl=_d(data.get("cumRealisedPnl", "0")),
    )


def parse_candle(raw: list[Any]) -> OHLCV:
    """
    Parse a single Bybit kline row.

    Bybit returns rows newest-first as
    ``[start, open, high, low, close, volume, turnover]`` (all strings).
    """
    return OHLCV(
        timestamp=_ts(raw[0]),
        open=_d(raw[1]),
        high=_d(raw[2]),
        low=_d(raw[3]),
        close=_d(raw[4]),
        volume=_d(raw[5]),
    )


def parse_candles(raw_list: list[list[Any]]) -> list[OHLCV]:
    """Parse Bybit kline rows into ascending-chronological OHLCV list."""
    candles = [parse_candle(c) for c in raw_list]
    return sorted(candles, key=lambda c: c.timestamp)


def parse_ticker(data: dict[str, Any]) -> Ticker:
    """Parse a Bybit V5 linear-perpetual ticker object."""
    last = _d(data.get("lastPrice", "0"))
    change_pct = _d(data.get("price24hPcnt", "0")) * Decimal("100")
    prev_price = _d(data.get("prevPrice24h", "0"))
    change_abs = last - prev_price if prev_price > 0 else Decimal("0")

    return Ticker(
        symbol=str(data["symbol"]),
        price=last,
        price_change_24h=change_abs,
        price_change_pct_24h=change_pct,
        high_24h=_d(data.get("highPrice24h", "0")),
        low_24h=_d(data.get("lowPrice24h", "0")),
        volume_24h=_d(data.get("volume24h", "0")),
        quote_volume_24h=_d(data.get("turnover24h", "0")),
        timestamp=datetime.now(tz=UTC),
        open_interest=_d(data["openInterest"]) if "openInterest" in data else None,
        funding_rate=_d(data["fundingRate"]) if "fundingRate" in data else None,
        next_funding_time=(
            _ts(data["nextFundingTime"]) if data.get("nextFundingTime") else None
        ),
    )


def normalize_instruments_info(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize a Bybit V5 ``/v5/market/instruments-info`` response into a
    Binance-``exchangeInfo``-shaped dict.

    This lets ``src.exchange.binance.exchange_info.parse_instruments`` be
    reused unmodified for both providers — normalization happens once, here,
    at the adapter boundary, rather than teaching the downstream instrument
    parser about a second wire format.
    """
    symbols: list[dict[str, Any]] = []
    for row in data.get("result", {}).get("list", []):
        try:
            status = "TRADING" if row.get("status") == "Trading" else row.get("status", "")
            leverage_filter = row.get("leverageFilter", {})
            price_filter = row.get("priceFilter", {})
            lot_size_filter = row.get("lotSizeFilter", {})

            max_leverage = int(float(leverage_filter.get("maxLeverage", "20")))

            symbols.append({
                "symbol": row["symbol"],
                "contractType": "PERPETUAL",
                "status": status,
                "baseAsset": row.get("baseCoin", ""),
                "quoteAsset": row.get("quoteCoin", ""),
                "leverageBracket": [{"initialLeverage": max_leverage}],
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": price_filter.get("tickSize", "0.01"),
                        "minPrice": price_filter.get("minPrice", "0"),
                        "maxPrice": price_filter.get("maxPrice", "1000000"),
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": lot_size_filter.get("qtyStep", "0.001"),
                        "minQty": lot_size_filter.get("minOrderQty", "0.001"),
                        "maxQty": lot_size_filter.get("maxOrderQty", "1000000"),
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "notional": lot_size_filter.get("minNotionalValue", "5"),
                    },
                ],
            })
        except (KeyError, ValueError, TypeError):
            # Malformed row — skip it here; parse_instruments logs+skips
            # per-symbol failures anyway, this just guards the normalizer
            # itself from raising on an unexpected/missing field.
            continue

    return {"symbols": symbols}


def parse_order_book(data: dict[str, Any], symbol: str) -> OrderBook:
    """Parse a Bybit V5 order book snapshot (``/v5/market/orderbook``)."""
    bids = [BookLevel(price=_d(b[0]), quantity=_d(b[1])) for b in data.get("b", [])]
    asks = [BookLevel(price=_d(a[0]), quantity=_d(a[1])) for a in data.get("a", [])]
    return OrderBook(
        symbol=symbol,
        bids=sorted(bids, key=lambda b: b.price, reverse=True),
        asks=sorted(asks, key=lambda a: a.price),
        timestamp=datetime.now(tz=UTC),
        last_update_id=int(data.get("u", 0)),
    )
