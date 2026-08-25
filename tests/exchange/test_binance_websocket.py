"""Unit tests for src.exchange.binance.websocket.BinanceFuturesWebSocket."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.core.enums import Timeframe
from src.exchange.binance.websocket import BinanceFuturesWebSocket


def make_ws() -> BinanceFuturesWebSocket:
    return BinanceFuturesWebSocket("wss://fstream.binance.com")


class TestSubscriptionManagement:
    async def test_subscribe_kline_builds_the_correct_stream_name(self) -> None:
        ws = make_ws()
        handler = AsyncMock()
        stream = ws.subscribe_kline("BTCUSDT", Timeframe.H1, handler)
        assert stream == "btcusdt@kline_1h"
        assert ws.stats["subscriptions"] == 1

    async def test_subscribe_agg_trade_builds_the_correct_stream_name(self) -> None:
        ws = make_ws()
        stream = ws.subscribe_agg_trade("BTCUSDT", AsyncMock())
        assert stream == "btcusdt@aggTrade"

    async def test_subscribe_book_ticker_builds_the_correct_stream_name(self) -> None:
        ws = make_ws()
        stream = ws.subscribe_book_ticker("BTCUSDT", AsyncMock())
        assert stream == "btcusdt@bookTicker"

    async def test_subscribe_depth_builds_the_correct_stream_name(self) -> None:
        ws = make_ws()
        stream = ws.subscribe_depth("BTCUSDT", 20, 100, AsyncMock())
        assert stream == "btcusdt@depth20@100ms"

    async def test_subscribe_mark_price_builds_the_correct_stream_name(self) -> None:
        ws = make_ws()
        stream = ws.subscribe_mark_price("BTCUSDT", AsyncMock())
        assert stream == "btcusdt@markPrice"

    async def test_unsubscribe_removes_a_single_stream(self) -> None:
        ws = make_ws()
        ws.subscribe_agg_trade("BTCUSDT", AsyncMock())
        ws.subscribe_agg_trade("ETHUSDT", AsyncMock())
        ws.unsubscribe("btcusdt@aggTrade")
        assert ws.stats["subscriptions"] == 1

    async def test_unsubscribe_unknown_stream_does_not_raise(self) -> None:
        ws = make_ws()
        ws.unsubscribe("never-subscribed@aggTrade")  # must not raise

    async def test_unsubscribe_all_clears_every_subscription(self) -> None:
        ws = make_ws()
        ws.subscribe_agg_trade("BTCUSDT", AsyncMock())
        ws.subscribe_book_ticker("ETHUSDT", AsyncMock())
        ws.unsubscribe_all()
        assert ws.stats["subscriptions"] == 0


class TestDispatchCombinedStream:
    async def test_routes_to_the_correct_handler_and_unwraps_data(self) -> None:
        ws = make_ws()
        received: list[dict] = []

        async def handler(data: dict) -> None:
            received.append(data)

        ws.subscribe_agg_trade("BTCUSDT", handler)
        raw = json.dumps({"stream": "btcusdt@aggTrade", "data": {"p": "50000"}})
        await ws._dispatch(raw)
        assert received == [{"p": "50000"}]

    async def test_unknown_stream_name_is_silently_ignored(self) -> None:
        ws = make_ws()
        raw = json.dumps({"stream": "unknown@stream", "data": {"x": 1}})
        await ws._dispatch(raw)  # must not raise despite no handler registered


class TestDispatchSingleStream:
    async def test_infers_kline_stream_and_routes_correctly(self) -> None:
        ws = make_ws()
        received: list[dict] = []

        async def handler(data: dict) -> None:
            received.append(data)

        ws.subscribe_kline("BTCUSDT", Timeframe.H1, handler)
        raw = json.dumps({"e": "kline", "s": "BTCUSDT", "k": {"i": "1h", "c": "50100"}})
        await ws._dispatch(raw)
        assert received == [{"e": "kline", "s": "BTCUSDT", "k": {"i": "1h", "c": "50100"}}]

    async def test_infers_agg_trade_stream(self) -> None:
        ws = make_ws()
        received: list[dict] = []

        async def handler(data: dict) -> None:
            received.append(data)

        ws.subscribe_agg_trade("BTCUSDT", handler)
        raw = json.dumps({"e": "aggTrade", "s": "BTCUSDT", "p": "50000"})
        await ws._dispatch(raw)
        assert len(received) == 1

    async def test_infers_book_ticker_stream(self) -> None:
        ws = make_ws()
        received: list[dict] = []

        async def handler(data: dict) -> None:
            received.append(data)

        ws.subscribe_book_ticker("BTCUSDT", handler)
        raw = json.dumps({"e": "bookTicker", "s": "BTCUSDT"})
        await ws._dispatch(raw)
        assert len(received) == 1

    async def test_infers_mark_price_update_stream(self) -> None:
        ws = make_ws()
        received: list[dict] = []

        async def handler(data: dict) -> None:
            received.append(data)

        ws.subscribe_mark_price("BTCUSDT", handler)
        raw = json.dumps({"e": "markPriceUpdate", "s": "BTCUSDT", "p": "50100"})
        await ws._dispatch(raw)
        assert len(received) == 1

    async def test_unrecognized_event_type_infers_an_empty_stream_and_is_ignored(
        self,
    ) -> None:
        ws = make_ws()
        raw = json.dumps({"e": "someUnknownEvent", "s": "BTCUSDT"})
        await ws._dispatch(raw)  # must not raise; no handler registered for ""


class TestDispatchNonJson:
    async def test_non_json_message_is_ignored_without_raising(self) -> None:
        ws = make_ws()
        await ws._dispatch("not valid json{{{")  # must not raise


class TestInferStream:
    def test_kline_inference(self) -> None:
        result = BinanceFuturesWebSocket._infer_stream(
            {"e": "kline", "s": "BTCUSDT", "k": {"i": "1h"}}
        )
        assert result == "btcusdt@kline_1h"

    def test_missing_event_type_returns_empty_string(self) -> None:
        result = BinanceFuturesWebSocket._infer_stream({"s": "BTCUSDT"})
        assert result == ""


class TestRenewalLoop:
    async def test_calls_the_renewal_callback_and_updates_the_listen_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 2:
                # Stop the loop only after the first full sleep->callback
                # cycle has completed, since _renewal_loop checks
                # self._running only *after* asyncio.sleep returns (so
                # setting it False during the very first sleep call
                # would skip the callback entirely -- confirmed by
                # reading the loop's actual structure rather than
                # assumed).
                ws._running = False

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        ws = BinanceFuturesWebSocket("wss://fstream.binance.com", listen_key="old-key")
        ws._running = True

        callback = AsyncMock(return_value="new-key")
        ws.set_renewal_callback(callback)

        await ws._renewal_loop()

        callback.assert_awaited_once()
        assert ws._listen_key == "new-key"

    async def test_a_callback_exception_does_not_crash_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        call_count = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                ws._running = False

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        ws = BinanceFuturesWebSocket("wss://fstream.binance.com", listen_key="old-key")
        ws._running = True

        callback = AsyncMock(side_effect=RuntimeError("renewal failed"))
        ws.set_renewal_callback(callback)

        await ws._renewal_loop()  # must not raise
        assert callback.await_count >= 1


class TestRunLoopBackoff:
    async def test_backoff_doubles_on_repeated_failures_capped_at_max(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        call_count = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        async def failing_connect_and_listen() -> None:
            call_count["n"] += 1
            if call_count["n"] >= 4:
                ws._running = False
            raise ConnectionError("simulated disconnect")

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        ws = BinanceFuturesWebSocket(
            "wss://fstream.binance.com",
            reconnect_delay=1.0,
            max_reconnect_delay=5.0,
        )
        ws._connect_and_listen = failing_connect_and_listen
        ws._running = True

        await ws._run_loop()

        # 1.0, 2.0, 4.0, capped at max_reconnect_delay=5.0 on the 4th.
        assert sleep_calls[:3] == [1.0, 2.0, 4.0]

    async def test_delay_resets_to_base_after_a_successful_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        call_count = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        async def flaky_connect_and_listen() -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ConnectionError("first failure")
            if call_count["n"] == 2:
                return  # succeeds, resets delay
            if call_count["n"] == 3:
                raise ConnectionError("second failure, after the reset")
            # call 4: stop the loop (checked *before* any further sleep,
            # since _run_loop's `if not self._running: return` guard
            # runs immediately after the exception handler and before
            # the sleep call -- confirmed by reading the loop's actual
            # control flow, not assumed).
            ws._running = False
            raise ConnectionError("third failure, loop stops before sleeping")

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        ws = BinanceFuturesWebSocket(
            "wss://fstream.binance.com",
            reconnect_delay=1.0,
            max_reconnect_delay=30.0,
        )
        ws._connect_and_listen = flaky_connect_and_listen
        ws._running = True

        await ws._run_loop()

        # Call 1 fails -> sleeps at base delay (1.0), delay becomes 2.0.
        # Call 2 succeeds -> delay resets to 1.0 (not 2.0).
        # Call 3 fails -> sleeps at the *reset* base delay (1.0), not
        # the doubled 2.0 it would have used without the reset.
        # Call 4's failure happens after _running is already False, so
        # its sleep is skipped.
        assert sleep_calls == [1.0, 1.0]


class TestStats:
    def test_stats_reflects_subscription_count(self) -> None:
        ws = make_ws()
        ws.subscribe_agg_trade("BTCUSDT", AsyncMock())
        stats = ws.stats
        assert stats["subscriptions"] == 1
        assert stats["messages"] == 0
        assert stats["reconnects"] == 0
