"""Unit tests for src.exchange.adapters.bybit.rest.BybitFuturesClient.

Uses a real local aiohttp.web test server rather than a mocking
library, for the same reason documented in test_base_client.py and
test_binance_rest.py: aioresponses (the only available
aiohttp-mocking library) has a known unreleased-fix incompatibility
with aiohttp 3.14+.

This file was added to close a coverage gap flagged during an audit:
``auth.py`` and ``parser.py`` were already covered indirectly via
``test_bybit_adapter.py``, but ``rest.py`` itself — the actual HTTP
client, including ``_handle_response_error`` — had zero dedicated
tests. Writing ``TestErrorMapping`` below surfaced a real bug in
``_handle_response_error``: the original guard
(``if status != 200 or not isinstance(body, dict): return``) returned
without raising for *every* non-200 HTTP status, silently swallowing
gateway-level errors (401/403/404) and returning the raw error body to
the caller as if it were valid data. Fixed alongside this test file.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.core.enums import OrderSide, OrderType, Timeframe
from src.core.exceptions import ExchangeAPIError, ExchangeAuthenticationError, OrderNotFoundError
from src.exchange.adapters.bybit.rest import BybitFuturesClient


class _BybitServerFixture:
    def __init__(self) -> None:
        self.request_log: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, str], list[web.Response]] = {}
        self.server: TestServer | None = None
        self.client: BybitFuturesClient | None = None

    def queue(
        self, method: str, path: str, *, status: int = 200, json_body: Any = None,
    ) -> None:
        key = (method.upper(), path)
        resp = web.Response(
            status=status, text=json.dumps(json_body), content_type="application/json",
        )
        self._routes.setdefault(key, []).append(resp)

    async def _handler(self, request: web.Request) -> web.Response:
        body = await request.text()
        self.request_log.append({
            "method": request.method, "path": request.path,
            "query": dict(request.query), "body": body,
            "headers": dict(request.headers),
        })
        key = (request.method, request.path)
        queue = self._routes.get(key, [])
        if not queue:
            return web.Response(
                status=404,
                text=json.dumps({"retCode": 10404, "retMsg": "not found"}),
            )
        return queue.pop(0)

    async def start(self, max_retries: int = 1) -> BybitFuturesClient:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        base_url = f"http://{self.server.host}:{self.server.port}"
        self.client = BybitFuturesClient(
            api_key="test-key", api_secret="test-secret", base_url=base_url,
            max_retries=max_retries,
        )
        return self.client

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        if self.server:
            await self.server.close()


def _ok(result: Any) -> dict[str, Any]:
    """Wrap a payload in Bybit's standard successful-envelope shape."""
    return {"retCode": 0, "retMsg": "OK", "result": result}


@pytest.fixture
async def server():
    fixture = _BybitServerFixture()
    yield fixture
    await fixture.stop()


class TestRateLimiterConstruction:
    """
    Mirrors test_binance_rest.py::TestRateLimiterConstruction --
    max_orders_per_second/max_orders_per_minute previously weren't
    constructor params here either; RateLimiter(max_per_second=10,
    max_per_minute=600, ...) was hardcoded inline in __init__, so
    configs/exchanges.yaml's bybit.max_orders_per_second/
    max_orders_per_minute had no path to reach it.
    """

    def test_custom_limits_reach_the_rate_limiter(self) -> None:
        client = BybitFuturesClient(
            api_key="k", api_secret="s", base_url="http://example.invalid",
            max_orders_per_second=3, max_orders_per_minute=45,
        )
        assert client._rate_limiter.max_per_second == 3
        assert client._rate_limiter.max_per_minute == 45

    def test_default_limits_unchanged(self) -> None:
        client = BybitFuturesClient(
            api_key="k", api_secret="s", base_url="http://example.invalid",
        )
        assert client._rate_limiter.max_per_second == 10
        assert client._rate_limiter.max_per_minute == 600


class TestGetAccountBalance:
    async def test_filters_zero_balances_and_parses_decimals(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/account/wallet-balance", json_body=_ok({
            "list": [{
                "coin": [
                    {"coin": "USDT", "walletBalance": "10000.5"},
                    {"coin": "BNB", "walletBalance": "0"},
                ]
            }]
        }))
        result = await client.get_account_balance()
        assert result == {"USDT": Decimal("10000.5")}


class TestGetPositions:
    async def test_returns_only_non_flat_positions(self, server: _BybitServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/v5/position/list", json_body=_ok({
            "list": [
                {"symbol": "BTCUSDT", "size": "0.5", "avgPrice": "50000",
                 "markPrice": "51000", "side": "Buy", "leverage": "10"},
                {"symbol": "ETHUSDT", "size": "0", "avgPrice": "0", "side": "Buy"},
            ]
        }))
        result = await client.get_positions()
        assert len(result) == 1
        assert result[0].symbol == "BTCUSDT"


class TestPlaceOrder:
    async def test_market_order_omits_time_in_force_and_price(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/create", json_body=_ok({"orderId": "1"}))
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "c1", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Market", "orderStatus": "New",
            "qty": "0.1", "cumExecQty": "0",
        }]}))
        await client.place_order("BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"))
        sent_body = json.loads(server.request_log[0]["body"])
        assert "timeInForce" not in sent_body
        assert "price" not in sent_body

    async def test_limit_order_includes_time_in_force_and_price(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/create", json_body=_ok({"orderId": "1"}))
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "c1", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Limit", "orderStatus": "New",
            "qty": "0.1", "cumExecQty": "0", "price": "50000",
        }]}))
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.LIMIT, Decimal("0.1"), price=Decimal("50000")
        )
        sent_body = json.loads(server.request_log[0]["body"])
        assert sent_body["timeInForce"] == "GTC"
        assert sent_body["price"] == "50000"

    async def test_zero_price_is_not_sent(self, server: _BybitServerFixture) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/create", json_body=_ok({"orderId": "1"}))
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "c1", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Market", "orderStatus": "New",
            "qty": "0.1", "cumExecQty": "0",
        }]}))
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), price=Decimal("0")
        )
        sent_body = json.loads(server.request_log[0]["body"])
        assert "price" not in sent_body

    async def test_client_order_id_is_forwarded_as_order_link_id(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/create", json_body=_ok({"orderId": "1"}))
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "my-id", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Market", "orderStatus": "New",
            "qty": "0.1", "cumExecQty": "0",
        }]}))
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), client_order_id="my-id"
        )
        sent_body = json.loads(server.request_log[0]["body"])
        assert sent_body["orderLinkId"] == "my-id"


class TestCancelOrder:
    async def test_returns_true_on_successful_cancellation(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/cancel", json_body=_ok({"orderId": "1"}))
        result = await client.cancel_order("BTCUSDT", "123")
        assert result is True

    async def test_returns_true_when_the_order_is_already_gone(
        self, server: _BybitServerFixture
    ) -> None:
        # Bybit retCode 110001 = order does not exist -- treated as a
        # successful cancel outcome (goal already satisfied), matching
        # the Binance adapter's -2011 "Unknown order" behavior.
        client = await server.start()
        server.queue("POST", "/v5/order/cancel", json_body={
            "retCode": 110001, "retMsg": "order not exists or too late to cancel",
        })
        result = await client.cancel_order("BTCUSDT", "999")
        assert result is True

    async def test_returns_false_on_a_genuine_api_error(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/cancel", json_body={
            "retCode": 10001, "retMsg": "params error",
        })
        result = await client.cancel_order("BTCUSDT", "123")
        assert result is False

    async def test_client_order_id_prefix_uses_order_link_id(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/order/cancel", json_body=_ok({"orderId": "1"}))
        await client.cancel_order("BTCUSDT", "ts_abc")
        sent_body = json.loads(server.request_log[-1]["body"])
        assert sent_body["orderLinkId"] == "ts_abc"
        assert "orderId" not in sent_body


class TestGetOrder:
    async def test_fetches_from_open_orders_first(self, server: _BybitServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "c1", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Market", "orderStatus": "Filled",
            "qty": "0.1", "cumExecQty": "0.1",
        }]}))
        result = await client.get_order("BTCUSDT", "123")
        assert result.exchange_order_id == "1"

    async def test_falls_back_to_order_history_when_not_open(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": []}))
        server.queue("GET", "/v5/order/history", json_body=_ok({"list": [{
            "orderId": "1", "orderLinkId": "c1", "symbol": "BTCUSDT",
            "side": "Buy", "orderType": "Market", "orderStatus": "Filled",
            "qty": "0.1", "cumExecQty": "0.1",
        }]}))
        result = await client.get_order("BTCUSDT", "123")
        assert result.exchange_order_id == "1"

    async def test_raises_order_not_found_when_absent_from_both(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": []}))
        server.queue("GET", "/v5/order/history", json_body=_ok({"list": []}))
        with pytest.raises(OrderNotFoundError):
            await client.get_order("BTCUSDT", "123")


class TestGetOpenOrders:
    async def test_uses_settle_coin_when_symbol_not_provided(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": []}))
        await client.get_open_orders()
        query = server.request_log[-1]["query"]
        assert "symbol" not in query
        assert query["settleCoin"] == "USDT"

    async def test_includes_symbol_param_when_provided(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/order/realtime", json_body=_ok({"list": []}))
        await client.get_open_orders(symbol="btcusdt")
        assert server.request_log[-1]["query"]["symbol"] == "BTCUSDT"


class TestMarketData:
    async def test_get_candles_raises_for_unsupported_timeframe(
        self, server: _BybitServerFixture
    ) -> None:
        # Timeframe.H8 ("8h") has no entry in TIMEFRAME_TO_BYBIT.
        client = await server.start()
        with pytest.raises(ValueError):
            await client.get_candles("BTCUSDT", Timeframe.H8, limit=10)
        assert not server.request_log

    async def test_get_candles_clamps_limit_to_1000(self, server: _BybitServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/v5/market/kline", json_body=_ok({"list": []}))
        await client.get_candles("BTCUSDT", Timeframe.H1, limit=5000)
        assert server.request_log[-1]["query"]["limit"] == "1000"

    async def test_get_ticker_parses_symbol_and_price(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/market/tickers", json_body=_ok({"list": [{
            "symbol": "BTCUSDT", "lastPrice": "50123.45", "price24hPcnt": "0.01",
            "prevPrice24h": "49000", "highPrice24h": "51000", "lowPrice24h": "48000",
            "volume24h": "1000", "turnover24h": "50000000",
        }]}))
        result = await client.get_ticker("BTCUSDT")
        assert result.price == Decimal("50123.45")

    async def test_get_order_book_clamps_depth_to_200(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/market/orderbook", json_body=_ok({"b": [], "a": [], "u": 1}))
        await client.get_order_book("BTCUSDT", depth=500)
        assert server.request_log[-1]["query"]["limit"] == "200"

    async def test_get_server_time_converts_nanoseconds_to_milliseconds(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/market/time", json_body=_ok({"timeNano": "1700000000000000000"}))
        result = await client.get_server_time()
        assert result == 1700000000000


class TestAccountConfiguration:
    async def test_set_leverage_sends_both_buy_and_sell(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/position/set-leverage", json_body=_ok({}))
        await client.set_leverage("BTCUSDT", 20)
        sent_body = json.loads(server.request_log[-1]["body"])
        assert sent_body["buyLeverage"] == "20"
        assert sent_body["sellLeverage"] == "20"

    async def test_set_margin_type_isolated_maps_to_mode_1(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/position/switch-isolated", json_body=_ok({}))
        await client.set_margin_type("BTCUSDT", "ISOLATED")
        sent_body = json.loads(server.request_log[-1]["body"])
        assert sent_body["tradeMode"] == 1

    async def test_set_margin_type_cross_maps_to_mode_0(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/v5/position/switch-isolated", json_body=_ok({}))
        await client.set_margin_type("BTCUSDT", "CROSS")
        sent_body = json.loads(server.request_log[-1]["body"])
        assert sent_body["tradeMode"] == 0


class TestErrorMapping:
    """
    Regression coverage for the `_handle_response_error` bug found while
    writing this file: the original guard returned early (no exception)
    for every non-200 HTTP status, silently treating gateway-level
    errors as successful responses.
    """

    async def test_ret_code_zero_on_200_does_not_raise(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/account/wallet-balance", json_body=_ok({"list": []}))
        await client.get_account_balance()  # must not raise

    async def test_auth_ret_code_on_200_raises_authentication_error(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/account/wallet-balance", json_body={
            "retCode": 10003, "retMsg": "API key is invalid",
        })
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_generic_ret_code_on_200_raises_exchange_api_error(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/v5/account/wallet-balance", json_body={
            "retCode": 10001, "retMsg": "params error",
        })
        with pytest.raises(ExchangeAPIError):
            await client.get_account_balance()

    async def test_http_401_with_no_bybit_error_body_raises_authentication_error(
        self, server: _BybitServerFixture
    ) -> None:
        # Regression case: previously swallowed entirely (returned the
        # 401 body to the caller as if it were valid account data).
        client = await server.start()
        server.queue(
            "GET", "/v5/account/wallet-balance", status=401,
            json_body={"message": "Unauthorized"},
        )
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_http_403_with_no_bybit_error_body_raises_authentication_error(
        self, server: _BybitServerFixture
    ) -> None:
        client = await server.start()
        server.queue(
            "GET", "/v5/account/wallet-balance", status=403,
            json_body={"message": "Forbidden"},
        )
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_http_404_with_bybit_shaped_error_body_uses_ret_code_classification(
        self, server: _BybitServerFixture
    ) -> None:
        # A non-200 status that DOES carry a Bybit-shaped retCode body
        # (e.g. their edge proxy passing the retCode through) must still
        # raise, using the retCode-based classification rather than the
        # generic HTTP-status fallback.
        client = await server.start()
        server.queue(
            "GET", "/v5/account/wallet-balance", status=404,
            json_body={"retCode": 10005, "retMsg": "permission denied"},
        )
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_clock_drift_error_triggers_resync_and_retry(
        self, server: _BybitServerFixture
    ) -> None:
        # Needs >1 attempt: the drift is detected and resynced on the first
        # attempt, then the retry (tenacity, not a manual second call) must
        # be allowed to happen.
        client = await server.start(max_retries=2)
        server.queue("GET", "/v5/market/time", json_body=_ok({"timeNano": "1700000000000000000"}))
        server.queue("GET", "/v5/account/wallet-balance", status=200, json_body={
            "retCode": 10002, "retMsg": "invalid timestamp",
        })
        server.queue("GET", "/v5/account/wallet-balance", json_body=_ok({"list": []}))
        await client.get_account_balance()  # must not raise; retried after resync
        assert len(server.request_log) == 3  # sync_clock + failed attempt + retry
