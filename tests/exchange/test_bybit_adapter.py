"""Unit tests for the Bybit adapter (parser + auth signing, no network calls)."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import OrderSide, OrderStatus, OrderType, PositionSide
from src.exchange.adapters.bybit.auth import query_string_for_signing, sign_request
from src.exchange.adapters.bybit.parser import (
    parse_candles,
    parse_order,
    parse_order_book,
    parse_position,
    parse_ticker,
    to_bybit_order_type,
)


class TestAuth:
    def test_sign_request_produces_expected_headers(self):
        headers = sign_request("key", "secret", 5000, "payload=1")
        assert headers["X-BAPI-API-KEY"] == "key"
        assert headers["X-BAPI-RECV-WINDOW"] == "5000"
        assert len(headers["X-BAPI-SIGN"]) == 64  # hex sha256

    def test_signature_changes_with_payload(self):
        h1 = sign_request("key", "secret", 5000, "a=1", clock_offset_ms=0)
        h2 = sign_request("key", "secret", 5000, "a=2", clock_offset_ms=0)
        assert h1["X-BAPI-SIGN"] != h2["X-BAPI-SIGN"]

    def test_query_string_sorted_and_skips_none(self):
        qs = query_string_for_signing({"b": 2, "a": 1, "c": None})
        assert qs == "a=1&b=2"


class TestOrderTypeMapping:
    def test_market_and_limit(self):
        assert to_bybit_order_type(OrderType.MARKET) == "Market"
        assert to_bybit_order_type(OrderType.LIMIT) == "Limit"

    def test_stop_limit_maps_to_limit(self):
        assert to_bybit_order_type(OrderType.STOP_LIMIT) == "Limit"


class TestParseOrder:
    def test_filled_market_order(self):
        raw = {
            "orderId": "123",
            "orderLinkId": "ts_abc",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "orderType": "Market",
            "qty": "0.01",
            "cumExecQty": "0.01",
            "price": "0",
            "avgPrice": "65000.5",
            "timeInForce": "GTC",
            "reduceOnly": False,
            "orderStatus": "Filled",
        }
        order = parse_order(raw)
        assert order.order_id == "123"
        assert order.symbol == "BTCUSDT"
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.status == OrderStatus.FILLED
        assert order.average_fill_price == Decimal("65000.5")
        assert order.price is None

    def test_new_limit_order_has_price(self):
        raw = {
            "orderId": "456",
            "orderLinkId": "",
            "symbol": "ETHUSDT",
            "side": "Sell",
            "orderType": "Limit",
            "qty": "1",
            "cumExecQty": "0",
            "price": "3500",
            "avgPrice": "",
            "timeInForce": "PostOnly",
            "reduceOnly": True,
            "orderStatus": "New",
        }
        order = parse_order(raw)
        assert order.status == OrderStatus.OPEN
        assert order.side == OrderSide.SELL
        assert order.price == Decimal("3500")
        assert order.reduce_only is True


class TestParsePosition:
    def test_flat_position_returns_none(self):
        assert parse_position({"symbol": "BTCUSDT", "size": "0", "avgPrice": "0"}) is None

    def test_open_long_position(self):
        raw = {
            "symbol": "BTCUSDT",
            "size": "0.5",
            "side": "Buy",
            "avgPrice": "60000",
            "markPrice": "61000",
            "liqPrice": "40000",
            "unrealisedPnl": "500",
            "leverage": "10",
            "positionIM": "3000",
            "positionMM": "300",
            "cumRealisedPnl": "0",
        }
        pos = parse_position(raw)
        assert pos is not None
        assert pos.side == PositionSide.LONG
        assert pos.quantity == Decimal("0.5")
        assert pos.leverage == 10
        assert pos.liquidation_price == Decimal("40000")


class TestParseCandles:
    def test_sorts_ascending_and_converts_types(self):
        raw = [
            ["1000", "10", "12", "9", "11", "100", "1100"],
            ["2000", "11", "13", "10", "12", "150", "1800"],
        ]
        candles = parse_candles(raw)
        assert len(candles) == 2
        assert candles[0].timestamp < candles[1].timestamp
        assert candles[0].close == Decimal("11")


class TestParseTicker:
    def test_basic_fields(self):
        raw = {
            "symbol": "BTCUSDT",
            "lastPrice": "65000",
            "prevPrice24h": "64000",
            "price24hPcnt": "0.0156",
            "highPrice24h": "66000",
            "lowPrice24h": "63000",
            "volume24h": "1000",
            "turnover24h": "65000000",
            "openInterest": "5000",
            "fundingRate": "0.0001",
        }
        ticker = parse_ticker(raw)
        assert ticker.symbol == "BTCUSDT"
        assert ticker.price == Decimal("65000")
        assert ticker.open_interest == Decimal("5000")


class TestParseOrderBook:
    def test_bids_desc_asks_asc(self):
        raw = {
            "b": [["100", "1"], ["101", "2"]],
            "a": [["102", "1"], ["103", "2"]],
            "u": 42,
        }
        book = parse_order_book(raw, "BTCUSDT")
        assert book.bids[0].price == Decimal("101")
        assert book.asks[0].price == Decimal("102")
        assert book.last_update_id == 42
