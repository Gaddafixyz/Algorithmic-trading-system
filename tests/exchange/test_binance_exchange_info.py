"""Unit tests for src.exchange.binance.exchange_info.parse_instruments."""

from __future__ import annotations

from decimal import Decimal

from src.exchange.binance.exchange_info import parse_instruments


def make_symbol_entry(
    symbol: str = "BTCUSDT",
    contract_type: str = "PERPETUAL",
    status: str = "TRADING",
    base: str = "BTC",
    quote: str = "USDT",
    tick_size: str = "0.01",
    min_price: str = "0.01",
    max_price: str = "1000000",
    step_size: str = "0.001",
    min_qty: str = "0.001",
    max_qty: str = "1000",
    min_notional: str = "5",
    leverage: int = 75,
) -> dict:
    return {
        "symbol": symbol,
        "contractType": contract_type,
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "leverageBracket": [{"initialLeverage": leverage}],
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "tickSize": tick_size,
                "minPrice": min_price,
                "maxPrice": max_price,
            },
            {"filterType": "LOT_SIZE", "stepSize": step_size, "minQty": min_qty, "maxQty": max_qty},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
        ],
    }


class TestFiltering:
    def test_includes_active_perpetual_trading_contracts(self) -> None:
        info = {"symbols": [make_symbol_entry()]}
        result = parse_instruments(info)
        assert "BTCUSDT" in result

    def test_excludes_non_perpetual_contract_types(self) -> None:
        info = {"symbols": [make_symbol_entry(contract_type="CURRENT_QUARTER")]}
        result = parse_instruments(info)
        assert result == {}

    def test_excludes_non_trading_status(self) -> None:
        info = {"symbols": [make_symbol_entry(status="BREAK")]}
        result = parse_instruments(info)
        assert result == {}

    def test_processes_multiple_symbols(self) -> None:
        info = {
            "symbols": [
                make_symbol_entry(symbol="BTCUSDT"),
                make_symbol_entry(symbol="ETHUSDT", base="ETH"),
            ]
        }
        result = parse_instruments(info)
        assert set(result.keys()) == {"BTCUSDT", "ETHUSDT"}

    def test_empty_symbols_list_returns_empty_dict(self) -> None:
        assert parse_instruments({"symbols": []}) == {}

    def test_missing_symbols_key_returns_empty_dict(self) -> None:
        assert parse_instruments({}) == {}


class TestFilterExtraction:
    def test_extracts_price_filter_values(self) -> None:
        info = {"symbols": [make_symbol_entry(tick_size="0.5", min_price="1", max_price="99999")]}
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.tick_size == Decimal("0.5")
        assert instrument.min_price == Decimal("1")
        assert instrument.max_price == Decimal("99999")

    def test_extracts_lot_size_values(self) -> None:
        info = {"symbols": [make_symbol_entry(step_size="0.01", min_qty="0.01", max_qty="500")]}
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.step_size == Decimal("0.01")
        assert instrument.min_qty == Decimal("0.01")
        assert instrument.max_qty == Decimal("500")

    def test_extracts_min_notional(self) -> None:
        info = {"symbols": [make_symbol_entry(min_notional="10")]}
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.min_notional == Decimal("10")

    def test_uses_lot_size_as_market_lot_size_fallback_when_absent(self) -> None:
        # MARKET_LOT_SIZE isn't included in make_symbol_entry's filters
        # at all; the source falls back to LOT_SIZE for it, and this
        # must not raise despite that filter being entirely absent.
        info = {"symbols": [make_symbol_entry()]}
        result = parse_instruments(info)
        assert "BTCUSDT" in result  # parsed successfully without MARKET_LOT_SIZE


class TestDefaults:
    def test_missing_filters_entirely_uses_documented_defaults(self) -> None:
        info = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [],
                }
            ]
        }
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.tick_size == Decimal("0.01")
        assert instrument.min_price == Decimal("0")
        assert instrument.max_price == Decimal("1000000")
        assert instrument.step_size == Decimal("0.001")
        assert instrument.min_qty == Decimal("0.001")
        assert instrument.max_qty == Decimal("1000000")
        assert instrument.min_notional == Decimal("5")

    def test_missing_leverage_bracket_defaults_to_20(self) -> None:
        info = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [],
                }
            ]
        }
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.max_leverage == 20


class TestDefaultLeverageCap:
    def test_default_leverage_is_capped_at_20_when_max_is_higher(self) -> None:
        info = {"symbols": [make_symbol_entry(leverage=125)]}
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.max_leverage == 125
        assert instrument.default_leverage == 20

    def test_default_leverage_uses_max_when_max_is_lower_than_20(self) -> None:
        info = {"symbols": [make_symbol_entry(leverage=10)]}
        instrument = parse_instruments(info)["BTCUSDT"]
        assert instrument.max_leverage == 10
        assert instrument.default_leverage == 10


class TestPerSymbolErrorIsolation:
    def test_a_malformed_symbol_does_not_prevent_parsing_the_rest(self) -> None:
        malformed = {
            "symbol": "BROKEN",
            "contractType": "PERPETUAL",
            "status": "TRADING",
            # Missing baseAsset/quoteAsset entirely -> KeyError inside the try block.
            "filters": [],
        }
        good = make_symbol_entry(symbol="ETHUSDT", base="ETH")
        info = {"symbols": [malformed, good]}

        result = parse_instruments(info)  # must not raise
        assert "ETHUSDT" in result
        assert "BROKEN" not in result

    def test_all_symbols_malformed_returns_an_empty_dict_without_raising(self) -> None:
        info = {"symbols": [{"symbol": "BROKEN", "contractType": "PERPETUAL", "status": "TRADING"}]}
        assert parse_instruments(info) == {}
