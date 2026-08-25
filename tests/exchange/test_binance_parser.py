"""Unit tests for src.exchange.binance.parser."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import OrderSide, OrderStatus, OrderType, PositionSide, TimeInForce
from src.exchange.binance.parser import (
    parse_candle,
    parse_candles,
    parse_funding_rate,
    parse_order,
    parse_order_book,
    parse_position,
    parse_ticker,
)


class TestParseOrder:
    def test_maps_all_documented_status_values(self) -> None:
        cases = {
            "NEW": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        for raw_status, expected in cases.items():
            data = {
                "orderId": 1,
                "clientOrderId": "c1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "status": raw_status,
                "origQty": "0.1",
                "executedQty": "0",
            }
            order = parse_order(data)
            assert order.status == expected

    def test_unknown_status_defaults_to_open(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "SOME_NEW_UNKNOWN_STATUS",
            "origQty": "0.1",
            "executedQty": "0",
        }
        assert parse_order(data).status == OrderStatus.OPEN

    def test_maps_order_type_and_side(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "stopPrice": "49000",
        }
        order = parse_order(data)
        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.STOP_MARKET

    def test_maps_time_in_force(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "status": "NEW",
            "timeInForce": "IOC",
            "origQty": "0.1",
            "executedQty": "0",
            "price": "50000",
        }
        assert parse_order(data).time_in_force == TimeInForce.IOC

    def test_zero_price_is_treated_as_none(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "price": "0",
        }
        assert parse_order(data).price is None

    def test_positive_price_is_preserved(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "price": "50000.5",
        }
        assert parse_order(data).price == Decimal("50000.5")

    def test_zero_stop_price_is_treated_as_none(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "stopPrice": "0",
        }
        assert parse_order(data).stop_price is None

    def test_zero_avg_price_is_treated_as_none(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "avgPrice": "0",
        }
        assert parse_order(data).average_fill_price is None

    def test_quantities_are_parsed_as_decimal(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "status": "PARTIALLY_FILLED",
            "origQty": "1.5",
            "executedQty": "0.75",
        }
        order = parse_order(data)
        assert order.quantity == Decimal("1.5")
        assert order.filled_quantity == Decimal("0.75")

    def test_reduce_only_and_close_position_flags(self) -> None:
        data = {
            "orderId": 1,
            "clientOrderId": "c1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "status": "NEW",
            "origQty": "0.1",
            "executedQty": "0",
            "reduceOnly": True,
            "closePosition": True,
        }
        order = parse_order(data)
        assert order.reduce_only is True
        assert order.close_position is True


class TestParsePosition:
    def test_zero_quantity_returns_none(self) -> None:
        data = {"positionAmt": "0", "entryPrice": "50000", "symbol": "BTCUSDT"}
        assert parse_position(data) is None

    def test_non_positive_entry_price_returns_none(self) -> None:
        data = {"positionAmt": "0.1", "entryPrice": "0", "symbol": "BTCUSDT"}
        assert parse_position(data) is None

    def test_positive_quantity_is_a_long_position(self) -> None:
        data = {
            "positionAmt": "0.5",
            "entryPrice": "50000",
            "symbol": "BTCUSDT",
            "markPrice": "51000",
            "notional": "25500",
            "leverage": "10",
            "unrealizedProfit": "500",
        }
        pos = parse_position(data)
        assert pos is not None
        assert pos.side == PositionSide.LONG
        assert pos.quantity == Decimal("0.5")

    def test_negative_quantity_is_a_short_position_with_absolute_qty(self) -> None:
        data = {
            "positionAmt": "-0.5",
            "entryPrice": "50000",
            "symbol": "BTCUSDT",
            "markPrice": "49000",
            "notional": "24500",
            "leverage": "10",
        }
        pos = parse_position(data)
        assert pos is not None
        assert pos.side == PositionSide.SHORT
        assert pos.quantity == Decimal("0.5")  # absolute value

    def test_initial_margin_is_notional_over_leverage(self) -> None:
        data = {
            "positionAmt": "1",
            "entryPrice": "50000",
            "symbol": "BTCUSDT",
            "markPrice": "50000",
            "notional": "50000",
            "leverage": "10",
        }
        pos = parse_position(data)
        assert pos.initial_margin == Decimal("5000")

    def test_zero_leverage_yields_zero_initial_margin(self) -> None:
        data = {
            "positionAmt": "1",
            "entryPrice": "50000",
            "symbol": "BTCUSDT",
            "markPrice": "50000",
            "notional": "50000",
            "leverage": "0",
        }
        pos = parse_position(data)
        assert pos.initial_margin == Decimal("0")

    def test_zero_liquidation_price_is_treated_as_none(self) -> None:
        data = {
            "positionAmt": "1",
            "entryPrice": "50000",
            "symbol": "BTCUSDT",
            "liquidationPrice": "0",
        }
        pos = parse_position(data)
        assert pos.liquidation_price is None

    def test_mark_price_defaults_to_entry_price_when_missing(self) -> None:
        data = {"positionAmt": "1", "entryPrice": "50000", "symbol": "BTCUSDT"}
        pos = parse_position(data)
        assert pos.mark_price == Decimal("50000")


class TestParseCandle:
    def test_parses_a_single_kline_array(self) -> None:
        raw = [1609459200000, "50000", "50500", "49500", "50200", "1000"]
        candle = parse_candle(raw)
        assert candle.open == Decimal("50000")
        assert candle.high == Decimal("50500")
        assert candle.low == Decimal("49500")
        assert candle.close == Decimal("50200")
        assert candle.volume == Decimal("1000")

    def test_timestamp_is_converted_from_milliseconds(self) -> None:
        raw = [1609459200000, "50000", "50500", "49500", "50200", "1000"]
        candle = parse_candle(raw)
        assert candle.timestamp.year == 2021
        assert candle.timestamp.month == 1
        assert candle.timestamp.day == 1


class TestParseCandles:
    def test_parses_a_list_of_klines(self) -> None:
        raw_list = [
            [1609459200000, "50000", "50500", "49500", "50200", "1000"],
            [1609462800000, "50200", "50700", "50000", "50600", "1200"],
        ]
        candles = parse_candles(raw_list)
        assert len(candles) == 2
        assert candles[0].close == Decimal("50200")
        assert candles[1].close == Decimal("50600")

    def test_empty_list_returns_empty_list(self) -> None:
        assert parse_candles([]) == []


class TestParseTicker:
    def test_parses_all_fields(self) -> None:
        data = {
            "symbol": "BTCUSDT",
            "lastPrice": "50000",
            "priceChange": "500",
            "priceChangePercent": "1.0",
            "highPrice": "50500",
            "lowPrice": "49500",
            "volume": "10000",
            "quoteVolume": "500000000",
        }
        ticker = parse_ticker(data)
        assert ticker.symbol == "BTCUSDT"
        assert ticker.price == Decimal("50000")
        assert ticker.price_change_pct_24h == Decimal("1.0")

    def test_open_interest_defaults_to_none_when_absent(self) -> None:
        data = {
            "symbol": "BTCUSDT",
            "lastPrice": "50000",
            "priceChange": "0",
            "priceChangePercent": "0",
            "highPrice": "50000",
            "lowPrice": "50000",
            "volume": "0",
            "quoteVolume": "0",
        }
        assert parse_ticker(data).open_interest is None

    def test_open_interest_is_parsed_when_present(self) -> None:
        data = {
            "symbol": "BTCUSDT",
            "lastPrice": "50000",
            "priceChange": "0",
            "priceChangePercent": "0",
            "highPrice": "50000",
            "lowPrice": "50000",
            "volume": "0",
            "quoteVolume": "0",
            "openInterest": "1234.5",
        }
        assert parse_ticker(data).open_interest == Decimal("1234.5")


class TestParseOrderBook:
    def test_bids_are_sorted_descending_by_price(self) -> None:
        data = {"bids": [["100", "1"], ["102", "1"], ["101", "1"]], "asks": []}
        book = parse_order_book(data, "BTCUSDT")
        assert [b.price for b in book.bids] == [Decimal("102"), Decimal("101"), Decimal("100")]

    def test_asks_are_sorted_ascending_by_price(self) -> None:
        data = {"bids": [], "asks": [["105", "1"], ["103", "1"], ["104", "1"]]}
        book = parse_order_book(data, "BTCUSDT")
        assert [a.price for a in book.asks] == [Decimal("103"), Decimal("104"), Decimal("105")]

    def test_symbol_and_last_update_id_are_set(self) -> None:
        data = {"bids": [], "asks": [], "lastUpdateId": 12345}
        book = parse_order_book(data, "ETHUSDT")
        assert book.symbol == "ETHUSDT"
        assert book.last_update_id == 12345


class TestParseFundingRate:
    def test_parses_rate_and_next_funding_time(self) -> None:
        data = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1609488000000,
        }
        rate = parse_funding_rate(data)
        assert rate.symbol == "BTCUSDT"
        assert rate.rate == Decimal("0.0001")
        assert rate.next_funding_time.year == 2021
