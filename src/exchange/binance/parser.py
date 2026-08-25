"""
Binance API response parser.

Converts raw exchange JSON into domain objects.
All Decimal conversions go through str() to avoid float imprecision.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.core.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from src.core.models import OHLCV, BookLevel, FundingRate, OrderBook, Ticker
from src.domain.order import Order
from src.domain.position import Position

logger = logging.getLogger(__name__)


def _ts(ms: int | str) -> datetime:
    """Convert millisecond timestamp to UTC datetime."""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)


def _d(value: Any) -> Decimal:
    """Safely convert any value to Decimal via str."""
    return Decimal(str(value))


# ─── Order status mapping ─────────────────────────────────────────────────────

_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.OPEN,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "PENDING_CANCEL": OrderStatus.OPEN,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}

_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "STOP": OrderType.STOP_LIMIT,
    "STOP_MARKET": OrderType.STOP_MARKET,
    "TAKE_PROFIT": OrderType.TAKE_PROFIT_LIMIT,
    "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
    "TRAILING_STOP_MARKET": OrderType.TRAILING_STOP_MARKET,
}

_ORDER_SIDE_MAP: dict[str, OrderSide] = {
    "BUY": OrderSide.BUY,
    "SELL": OrderSide.SELL,
}

_TIF_MAP: dict[str, TimeInForce] = {
    "GTC": TimeInForce.GTC,
    "IOC": TimeInForce.IOC,
    "FOK": TimeInForce.FOK,
    "GTX": TimeInForce.GTX,
}

_POSITION_SIDE_MAP: dict[str, PositionSide] = {
    "LONG": PositionSide.LONG,
    "SHORT": PositionSide.SHORT,
    "BOTH": PositionSide.BOTH,
}


def parse_order(data: dict[str, Any]) -> Order:
    """Parse Binance futures order response into domain Order."""
    raw_status = data.get("status", "NEW")
    status = _ORDER_STATUS_MAP.get(raw_status, OrderStatus.OPEN)

    raw_type = data.get("type", "MARKET")
    order_type = _ORDER_TYPE_MAP.get(raw_type, OrderType.MARKET)

    raw_side = data.get("side", "BUY")
    side = _ORDER_SIDE_MAP.get(raw_side, OrderSide.BUY)

    raw_tif = data.get("timeInForce", "GTC")
    tif = _TIF_MAP.get(raw_tif, TimeInForce.GTC)

    raw_pos_side = data.get("positionSide", "BOTH")
    pos_side = _POSITION_SIDE_MAP.get(raw_pos_side, PositionSide.BOTH)

    price_raw = data.get("price", "0")
    price = _d(price_raw) if price_raw and _d(price_raw) > Decimal("0") else None

    stop_price_raw = data.get("stopPrice", "0")
    stop_price = (
        _d(stop_price_raw) if stop_price_raw and _d(stop_price_raw) > Decimal("0") else None
    )

    avg_price_raw = data.get("avgPrice", "0")
    avg_price = _d(avg_price_raw) if avg_price_raw and _d(avg_price_raw) > Decimal("0") else None

    order = Order(
        order_id=str(data.get("orderId", "")),
        client_order_id=str(data.get("clientOrderId", "")),
        symbol=str(data.get("symbol", "")),
        side=side,
        order_type=order_type,
        position_side=pos_side,
        quantity=_d(data.get("origQty", "0")),
        filled_quantity=_d(data.get("executedQty", "0")),
        price=price,
        stop_price=stop_price,
        average_fill_price=avg_price,
        time_in_force=tif,
        reduce_only=bool(data.get("reduceOnly", False)),
        close_position=bool(data.get("closePosition", False)),
        status=status,
        exchange_order_id=str(data.get("orderId", "")),
    )

    # Apply submitted state if we got a valid response
    if status != OrderStatus.OPEN:
        order.status = status
    elif order.status == OrderStatus.PENDING:
        order.status = OrderStatus.OPEN

    # Binance (2026-03-26): expired orders may carry an expiryReason field
    # explaining why the order expired (e.g. price range execution rule
    # violations).  Log it so it shows up in audit trails without requiring
    # a schema change to the Order domain object.
    expiry_reason = data.get("expiryReason")
    if expiry_reason:
        logger.info(
            "Order %s expired with reason: %s",
            order.exchange_order_id,
            expiry_reason,
        )

    return order


def parse_position(data: dict[str, Any]) -> Position | None:
    """Parse Binance futures position risk response. Returns None for zero positions."""
    qty = _d(data.get("positionAmt", "0"))
    if qty == Decimal("0"):
        return None

    entry_price = _d(data.get("entryPrice", "0"))
    if entry_price <= Decimal("0"):
        return None

    symbol = str(data.get("symbol", ""))
    mark_price = _d(data.get("markPrice", str(entry_price)))
    notional = abs(_d(data.get("notional", "0")))
    liq_price_raw = _d(data.get("liquidationPrice", "0"))

    abs_qty = abs(qty)
    side = PositionSide.LONG if qty > Decimal("0") else PositionSide.SHORT

    leverage = int(data.get("leverage", 1))
    initial_margin = notional / Decimal(str(leverage)) if leverage > 0 else Decimal("0")

    # Note: unrealizedProfit from the exchange is intentionally not passed
    # here — Position.unrealized_pnl is a computed @property derived from
    # (mark_price - entry_price) * quantity, which is more reliable than the
    # exchange's snapshot value (which may lag by one funding period).
    # The exchange value is used only to sanity-log large discrepancies.
    exchange_upnl = _d(data.get("unrealizedProfit", "0"))
    pos = Position(
        symbol=symbol,
        side=side,
        quantity=abs_qty,
        entry_quantity=abs_qty,
        entry_price=entry_price,
        mark_price=mark_price,
        liquidation_price=liq_price_raw if liq_price_raw > Decimal("0") else None,
        leverage=leverage,
        initial_margin=initial_margin,
    )
    # Log a warning if local computation diverges significantly from exchange value
    # (>0.5% relative) — this can surface stale mark price or fee discrepancies.
    computed_upnl = pos.unrealized_pnl
    if exchange_upnl != Decimal("0") and computed_upnl != Decimal("0"):
        divergence = abs(computed_upnl - exchange_upnl) / abs(exchange_upnl)
        if divergence > Decimal("0.005"):
            logger.warning(
                "parse_position: unrealized PnL divergence for %s: computed=%s exchange=%s (%.2f%%)",
                symbol,
                computed_upnl,
                exchange_upnl,
                float(divergence * 100),
            )
    return pos


def parse_candle(raw: list[Any]) -> OHLCV:
    """Parse a single Binance kline array into OHLCV."""
    return OHLCV(
        timestamp=_ts(raw[0]),
        open=_d(raw[1]),
        high=_d(raw[2]),
        low=_d(raw[3]),
        close=_d(raw[4]),
        volume=_d(raw[5]),
    )


def parse_candles(raw_list: list[list[Any]]) -> list[OHLCV]:
    """Parse list of Binance klines into list of OHLCV."""
    return [parse_candle(c) for c in raw_list]


def parse_ticker(data: dict[str, Any]) -> Ticker:
    """Parse Binance 24h ticker response."""
    return Ticker(
        symbol=str(data["symbol"]),
        price=_d(data.get("lastPrice", "0")),
        price_change_24h=_d(data.get("priceChange", "0")),
        price_change_pct_24h=_d(data.get("priceChangePercent", "0")),
        high_24h=_d(data.get("highPrice", "0")),
        low_24h=_d(data.get("lowPrice", "0")),
        volume_24h=_d(data.get("volume", "0")),
        quote_volume_24h=_d(data.get("quoteVolume", "0")),
        timestamp=datetime.now(tz=UTC),
        open_interest=_d(data["openInterest"]) if "openInterest" in data else None,
    )


def parse_order_book(data: dict[str, Any], symbol: str) -> OrderBook:
    """Parse Binance order book depth response."""
    bids = [BookLevel(price=_d(b[0]), quantity=_d(b[1])) for b in data.get("bids", [])]
    asks = [BookLevel(price=_d(a[0]), quantity=_d(a[1])) for a in data.get("asks", [])]
    return OrderBook(
        symbol=symbol,
        bids=sorted(bids, key=lambda b: b.price, reverse=True),
        asks=sorted(asks, key=lambda a: a.price),
        timestamp=datetime.now(tz=UTC),
        last_update_id=int(data.get("lastUpdateId", 0)),
    )


def parse_funding_rate(data: dict[str, Any]) -> FundingRate:
    """Parse Binance mark-price-stream funding rate response (current/live rate)."""
    return FundingRate(
        symbol=str(data["symbol"]),
        rate=_d(data.get("lastFundingRate", "0")),
        next_funding_time=_ts(data["nextFundingTime"]),
        timestamp=datetime.now(tz=UTC),
    )


def parse_funding_rate_history_record(data: dict[str, Any]) -> FundingRate:
    """
    Parse a single record from ``get_funding_rate_history()`` (a *settled*
    historical funding event, not the live/upcoming rate).

    Response shape differs from the mark-price stream: keys are
    ``fundingRate``/``fundingTime`` rather than ``lastFundingRate``/
    ``nextFundingTime``. ``timestamp`` is set to the settlement time itself
    (what ``_apply_funding_rates`` keys off of); ``next_funding_time`` is
    derived as settlement + 8h since Binance funding is fixed-interval and
    the history endpoint doesn't report it directly.
    """
    settled_at = _ts(data["fundingTime"])
    return FundingRate(
        symbol=str(data["symbol"]),
        rate=_d(data.get("fundingRate", "0")),
        next_funding_time=settled_at + timedelta(hours=8),
        timestamp=settled_at,
    )
