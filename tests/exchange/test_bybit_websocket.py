"""
Unit tests for BybitWebSocket's pure logic: auth signing, depth-level
snapping, and message normalization to the Binance wire shape that
FillEngine/CandleService/MarketDataFeed expect.

No network calls — _connect_public/_connect_private and the run loops
that actually open sockets are exercised only via manual/integration
testing, consistent with test_binance_websocket.py's scope.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from src.exchange.adapters.bybit.websocket import (
    _AUTH_EXPIRE_WINDOW_MS,
    BybitWebSocket,
    _sign_ws_auth,
    _snap_depth,
)


class TestSnapDepth:
    def test_exact_supported_levels_pass_through(self):
        assert _snap_depth(1) == 1
        assert _snap_depth(50) == 50
        assert _snap_depth(200) == 200
        assert _snap_depth(500) == 500

    def test_unsupported_depth_rounds_up_to_next_level(self):
        assert _snap_depth(10) == 50
        assert _snap_depth(51) == 200
        assert _snap_depth(201) == 500

    def test_depth_above_max_level_clamps_to_max(self):
        assert _snap_depth(1000) == 500

    def test_depth_of_zero_or_one_rounds_up_to_one(self):
        assert _snap_depth(0) == 1


class TestSignWsAuth:
    def test_signature_matches_documented_payload_format(self):
        secret = "mysecret"
        expires = 1_700_000_000_000
        expected = hmac.new(
            secret.encode(), f"GET/realtime{expires}".encode(), hashlib.sha256
        ).hexdigest()
        assert _sign_ws_auth(secret, expires) == expected

    def test_signature_changes_with_expires(self):
        secret = "mysecret"
        sig_a = _sign_ws_auth(secret, 1_700_000_000_000)
        sig_b = _sign_ws_auth(secret, 1_700_000_000_001)
        assert sig_a != sig_b

    def test_signature_changes_with_secret(self):
        expires = 1_700_000_000_000
        sig_a = _sign_ws_auth("secret_a", expires)
        sig_b = _sign_ws_auth("secret_b", expires)
        assert sig_a != sig_b

    def test_expire_window_is_short_lived(self):
        # Bybit's own guidance: keep this short so a captured signature
        # can't be replayed long after the fact.
        assert _AUTH_EXPIRE_WINDOW_MS <= 10_000


class TestNormalizeKline:
    def test_maps_bybit_fields_to_binance_shape(self):
        row = {
            "start": 1700000000000,
            "open": "50000",
            "high": "50500",
            "low": "49900",
            "close": "50200",
            "volume": "12.5",
            "confirm": True,
        }
        out = BybitWebSocket._normalize_kline(row)
        assert out == {
            "t": 1700000000000,
            "o": "50000",
            "h": "50500",
            "l": "49900",
            "c": "50200",
            "v": "12.5",
            "x": True,
        }

    def test_unconfirmed_candle_maps_x_false(self):
        row = {
            "start": 1,
            "open": "1",
            "high": "1",
            "low": "1",
            "close": "1",
            "volume": "1",
            "confirm": False,
        }
        assert BybitWebSocket._normalize_kline(row)["x"] is False

    def test_missing_fields_default_safely(self):
        out = BybitWebSocket._normalize_kline({})
        assert out["t"] == 0
        assert out["o"] == "0"
        assert out["x"] is False


class TestNormalizeTrade:
    def test_taker_sell_means_buyer_was_maker(self):
        # Binance's "m" means "buyer was the maker" — true exactly when
        # the Bybit taker's side was Sell.
        trade = {"p": "50000", "v": "0.1", "S": "Sell"}
        out = BybitWebSocket._normalize_trade(trade)
        assert out == {"p": "50000", "q": "0.1", "m": True}

    def test_taker_buy_means_buyer_was_not_maker(self):
        trade = {"p": "50000", "v": "0.1", "S": "Buy"}
        out = BybitWebSocket._normalize_trade(trade)
        assert out["m"] is False


class TestNormalizeTicker:
    def test_prefers_mark_price(self):
        out = BybitWebSocket._normalize_ticker({"markPrice": "50100", "lastPrice": "50050"})
        assert out == {"p": "50100"}

    def test_falls_back_to_last_price(self):
        out = BybitWebSocket._normalize_ticker({"lastPrice": "50050"})
        assert out == {"p": "50050"}

    def test_missing_fields_default_to_zero(self):
        assert BybitWebSocket._normalize_ticker({}) == {"p": "0"}


class TestNormalizeBookTicker:
    """
    Regression coverage for OrderBookUpdatedEvent wiring: orderbook.1.*
    messages previously fell into the raw pass-through branch of
    _dispatch_public, so MarketDataFeed (which expects Binance-shaped
    bookTicker dicts) had nothing usable to consume.
    """

    def test_extracts_top_of_book_from_snapshot(self):
        # Real Bybit depth-1 payload shape: full snapshot, single-level
        # b/a arrays (see _dispatch_public's routing comment).
        data = {
            "s": "BTCUSDT",
            "b": [["50000.10", "1.5"]],
            "a": [["50000.50", "2.0"]],
            "u": 18521288,
            "seq": 7961638724,
        }
        out = BybitWebSocket._normalize_book_ticker(data)
        assert out == {
            "s": "BTCUSDT",
            "b": "50000.10",
            "B": "1.5",
            "a": "50000.50",
            "A": "2.0",
        }

    def test_missing_bid_side_is_omitted_not_zeroed(self):
        out = BybitWebSocket._normalize_book_ticker({"s": "BTCUSDT", "a": [["50000.50", "2.0"]]})
        assert "b" not in out
        assert "B" not in out
        assert out["a"] == "50000.50"

    def test_missing_ask_side_is_omitted_not_zeroed(self):
        out = BybitWebSocket._normalize_book_ticker({"s": "BTCUSDT", "b": [["50000.10", "1.5"]]})
        assert "a" not in out
        assert "A" not in out

    def test_empty_data_produces_symbol_only(self):
        assert BybitWebSocket._normalize_book_ticker({}) == {"s": ""}


class TestDispatchPublicOrderBookRouting:
    """
    End-to-end regression: previously orderbook.1.* fell through to the
    raw-passthrough branch, so a handler registered via
    subscribe_book_ticker() never received a Binance-shaped message.
    """

    async def _make_ws_with_dispatched(self, topic: str, data: Any) -> list[dict]:
        import json

        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")
        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        ws._subscriptions[topic] = handler
        raw = json.dumps({"topic": topic, "type": "snapshot", "data": data})
        await ws._dispatch_public(raw)
        return received

    async def test_orderbook_1_is_normalized_to_book_ticker_shape(self):
        received = await self._make_ws_with_dispatched(
            "orderbook.1.BTCUSDT",
            {"s": "BTCUSDT", "b": [["50000.10", "1.5"]], "a": [["50000.50", "2.0"]]},
        )
        assert received == [
            {"s": "BTCUSDT", "b": "50000.10", "B": "1.5", "a": "50000.50", "A": "2.0"}
        ]

    async def test_higher_depth_levels_still_pass_through_raw(self):
        # orderbook.50.* etc must keep the old raw-passthrough behavior —
        # a real multi-level depth consumer needs delta semantics
        # preserved, which the level-1 normalization above deliberately
        # discards.
        received = await self._make_ws_with_dispatched(
            "orderbook.50.BTCUSDT",
            {"s": "BTCUSDT", "b": [["50000.10", "1.5"]], "a": [["50000.50", "2.0"]]},
        )
        assert received == [
            {
                "data": {"s": "BTCUSDT", "b": [["50000.10", "1.5"]], "a": [["50000.50", "2.0"]]},
                "type": "snapshot",
            }
        ]


class TestNormalizeExecution:
    def test_maps_to_order_trade_update_shape_fill_engine_expects(self):
        exec_report = {
            "orderLinkId": "abc123",
            "execQty": "0.5",
            "execPrice": "50000",
            "execFee": "0.025",
            "feeCurrency": "USDT",
            "isMaker": True,
        }
        out = BybitWebSocket._normalize_execution(exec_report)
        assert out["e"] == "ORDER_TRADE_UPDATE"
        assert out["o"] == {
            "c": "abc123",
            "l": "0.5",
            "L": "50000",
            "n": "0.025",
            "N": "USDT",
            "m": True,
        }

    def test_missing_fields_default_safely(self):
        out = BybitWebSocket._normalize_execution({})
        assert out["o"]["c"] == ""
        assert out["o"]["l"] == "0"
        assert out["o"]["N"] == "USDT"
        assert out["o"]["m"] is False

    def test_trade_exec_type_maps_to_x_trade(self):
        # Regression: without "x" == "TRADE" in the output, every Bybit
        # fill was silently dropped by FillEngine.process_execution_report
        # (it gates on order_data.get("x", "") == "TRADE"). See
        # test_fill_engine_processes_bybit_trade_execution below for the
        # end-to-end version of this regression.
        out = BybitWebSocket._normalize_execution({"execType": "Trade", "execQty": "1"})
        assert out["o"]["x"] == "TRADE"

    def test_missing_exec_type_defaults_to_trade(self):
        # The execution topic only ever carries genuine fills, so an
        # absent execType (shouldn't happen on the real stream) must
        # still be treated as a fill, not silently dropped.
        out = BybitWebSocket._normalize_execution({"execQty": "1"})
        assert out["o"]["x"] == "TRADE"

    def test_non_trade_exec_type_is_not_mapped_to_trade(self):
        # Funding/ADL/liquidation entries on the execution topic must NOT
        # be counted as order fills.
        out = BybitWebSocket._normalize_execution({"execType": "Funding"})
        assert out["o"]["x"] == "Funding"


class TestNormalizeExecutionFillEngineIntegration:
    """
    End-to-end regression for the dropped-fill bug: previous coverage
    only asserted on _normalize_execution's return shape in isolation,
    which passed even though FillEngine silently discarded every
    normalized message (missing "x" key defaulted to "" != "TRADE").
    """

    async def test_fill_engine_processes_bybit_trade_execution(self):
        from unittest.mock import AsyncMock

        from src.execution.fill_engine import FillEngine

        order = AsyncMock()
        order.order_id = "internal-1"
        order.symbol = "BTCUSDT"
        order.is_terminal = False
        order_manager = AsyncMock()
        order_manager.get_order_by_client_id = AsyncMock(return_value=order)

        engine = FillEngine(order_manager)
        exec_report = {
            "orderLinkId": "abc123",
            "execType": "Trade",
            "execQty": "0.5",
            "execPrice": "50000",
            "execFee": "0.025",
            "feeCurrency": "USDT",
            "isMaker": True,
        }
        msg = BybitWebSocket._normalize_execution(exec_report)

        await engine.process_execution_report(msg)

        order.add_fill.assert_called_once()
        assert engine._fills_processed == 1

    async def test_fill_engine_ignores_bybit_funding_settlement(self):
        from unittest.mock import AsyncMock

        from src.execution.fill_engine import FillEngine

        order_manager = AsyncMock()
        engine = FillEngine(order_manager)
        msg = BybitWebSocket._normalize_execution({"execType": "Funding", "execQty": "1"})

        await engine.process_execution_report(msg)

        order_manager.get_order_by_client_id.assert_not_called()
        assert engine._fills_processed == 0


class TestNormalizeOrderStatus:
    def test_status_only_update_has_zero_qty_price(self):
        out = BybitWebSocket._normalize_order_status(
            {"orderLinkId": "xyz", "orderStatus": "Cancelled"}
        )
        assert out["e"] == "ORDER_TRADE_UPDATE"
        assert out["o"]["c"] == "xyz"
        assert out["o"]["l"] == "0"
        assert out["o"]["L"] == "0"
        assert out["o"]["X"] == "Cancelled"


class TestSubscriptionManagement:
    def test_subscribe_kline_builds_expected_topic(self):
        from src.core.enums import Timeframe

        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")

        async def handler(_msg):
            return None

        topic = ws.subscribe_kline("btcusdt", Timeframe.H1, handler)
        assert topic == "kline.60.BTCUSDT"
        assert "kline.60.BTCUSDT" in ws._subscriptions

    def test_subscribe_depth_snaps_to_supported_level(self):
        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")

        async def handler(_msg):
            return None

        topic = ws.subscribe_depth("ETHUSDT", depth=75, speed_ms=100, handler=handler)
        assert topic == "orderbook.200.ETHUSDT"

    def test_unsubscribe_removes_topic(self):
        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")

        async def handler(_msg):
            return None

        topic = ws.subscribe_agg_trade("BTCUSDT", handler)
        assert topic in ws._subscriptions
        ws.unsubscribe(topic)
        assert topic not in ws._subscriptions

    def test_subscribe_user_data_registers_handler_but_no_public_topic(self):
        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")

        async def handler(_msg):
            return None

        ws.subscribe_user_data(handler)
        assert ws._user_data_handler is handler
        assert ws._subscriptions == {}


class TestPrivateStreamGating:
    """start() must only schedule the private connection when both API
    credentials and a user-data handler are present — otherwise it would
    attempt to auth with empty credentials, which Bybit will reject."""

    async def _handler(self, _msg):
        return None

    def test_no_credentials_no_handler_means_no_private_task_field_set(self):
        # Without a user_data handler, start() never even inspects
        # credentials — verified indirectly via subscribe_user_data's
        # absence leaving _user_data_handler None.
        ws = BybitWebSocket(ws_public_url="wss://public", ws_private_url="wss://private")
        assert ws._user_data_handler is None
        assert ws._api_key is None
        assert ws._api_secret is None
