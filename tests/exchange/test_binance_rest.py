"""Unit tests for src.exchange.binance.rest.BinanceFuturesRESTClient.

Uses a real local aiohttp.web test server rather than a mocking
library, for the same reason documented in test_base_client.py:
aioresponses (the only available aiohttp-mocking library) has a known
unreleased-fix incompatibility with aiohttp 3.14+.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.core.enums import OrderSide, OrderType
from src.core.exceptions import ExchangeAPIError, ExchangeAuthenticationError, OrderNotFoundError
from src.exchange.binance.rest import BinanceFuturesRESTClient


class _BinanceServerFixture:
    def __init__(self) -> None:
        self.request_log: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, str], list[web.Response]] = {}
        self.server: TestServer | None = None
        self.client: BinanceFuturesRESTClient | None = None

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
        })
        key = (request.method, request.path)
        queue = self._routes.get(key, [])
        if not queue:
            return web.Response(status=404, text=json.dumps({"code": -1, "msg": "not found"}))
        return queue.pop(0)

    async def start(self) -> BinanceFuturesRESTClient:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        base_url = f"http://{self.server.host}:{self.server.port}"
        self.client = BinanceFuturesRESTClient(
            api_key="test-key", api_secret="test-secret", base_url=base_url, max_retries=1,
        )
        return self.client

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        if self.server:
            await self.server.close()


@pytest.fixture
async def server():
    fixture = _BinanceServerFixture()
    yield fixture
    await fixture.stop()


class TestRateLimiterConstruction:
    """
    max_orders_per_second/max_orders_per_minute previously weren't
    constructor params at all -- RateLimiter(max_per_second=10,
    max_per_minute=1200, ...) was hardcoded inline in __init__, so
    configs/exchanges.yaml's binance.max_orders_per_second/
    max_orders_per_minute had no path to reach it. These cover the new
    params and confirm the pre-existing defaults are unchanged for
    callers (e.g. scripts/manual_order.py) that don't pass them.
    """

    def test_custom_limits_reach_the_rate_limiter(self) -> None:
        client = BinanceFuturesRESTClient(
            api_key="k", api_secret="s", base_url="http://example.invalid",
            max_orders_per_second=3, max_orders_per_minute=90,
        )
        assert client._rate_limiter.max_per_second == 3
        assert client._rate_limiter.max_per_minute == 90

    def test_default_limits_unchanged(self) -> None:
        client = BinanceFuturesRESTClient(
            api_key="k", api_secret="s", base_url="http://example.invalid",
        )
        assert client._rate_limiter.max_per_second == 10
        assert client._rate_limiter.max_per_minute == 1200


class TestGetAccountBalance:
    async def test_filters_zero_balances_and_parses_decimals(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/balance", json_body=[
            {"asset": "USDT", "balance": "10000.5"},
            {"asset": "BNB", "balance": "0"},
        ])
        result = await client.get_account_balance()
        assert result == {"USDT": Decimal("10000.5")}


class TestGetPositions:
    async def test_returns_only_non_zero_positions(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/positionRisk", json_body=[
            {"positionAmt": "0.5", "entryPrice": "50000", "symbol": "BTCUSDT",
             "markPrice": "51000", "notional": "25500", "leverage": "10"},
            {"positionAmt": "0", "entryPrice": "0", "symbol": "ETHUSDT"},
        ])
        result = await client.get_positions()
        assert len(result) == 1
        assert result[0].symbol == "BTCUSDT"

    async def test_get_position_filters_by_symbol_query_param(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/positionRisk", json_body=[
            {"positionAmt": "0.5", "entryPrice": "50000", "symbol": "BTCUSDT",
             "markPrice": "51000", "notional": "25500", "leverage": "10"},
        ])
        await client.get_position("btcusdt")
        assert server.request_log[-1]["query"]["symbol"] == "BTCUSDT"


class TestPlaceOrder:
    async def test_market_order_omits_time_in_force_and_price(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "NEW",
            "origQty": "0.1", "executedQty": "0",
        })
        await client.place_order("BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"))
        sent_query = server.request_log[-1]["query"]
        assert "timeInForce" not in sent_query
        assert "price" not in sent_query

    async def test_limit_order_includes_time_in_force_and_price(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT",
            "side": "BUY", "type": "LIMIT", "status": "NEW",
            "origQty": "0.1", "executedQty": "0", "price": "50000",
        })
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.LIMIT, Decimal("0.1"), price=Decimal("50000")
        )
        sent_query = server.request_log[-1]["query"]
        assert sent_query["timeInForce"] == "GTC"
        assert sent_query["price"] == "50000"

    async def test_zero_price_is_not_sent(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "NEW",
            "origQty": "0.1", "executedQty": "0",
        })
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), price=Decimal("0")
        )
        assert "price" not in server.request_log[-1]["query"]

    async def test_client_order_id_is_forwarded_as_new_client_order_id(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "my-id", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "NEW",
            "origQty": "0.1", "executedQty": "0",
        })
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), client_order_id="my-id"
        )
        assert server.request_log[-1]["query"]["newClientOrderId"] == "my-id"


class TestCancelOrder:
    async def test_returns_true_on_successful_cancellation(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("DELETE", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "CANCELED",
            "origQty": "0.1", "executedQty": "0",
        })
        result = await client.cancel_order("BTCUSDT", "123")
        assert result is True

    async def test_returns_true_when_the_order_is_already_gone(
        self, server: _BinanceServerFixture
    ) -> None:
        # This is a real, deliberate behavior documented in the source:
        # an already-gone order is treated as a successful cancel
        # outcome (goal already satisfied), not a failure.
        client = await server.start()
        server.queue("DELETE", "/fapi/v1/order", status=400, json_body={
            "code": -2011, "msg": "Unknown order sent.",
        })
        result = await client.cancel_order("BTCUSDT", "999")
        assert result is True

    async def test_returns_false_on_a_genuine_api_error(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("DELETE", "/fapi/v1/order", status=400, json_body={
            "code": -1013, "msg": "Filter failure",
        })
        result = await client.cancel_order("BTCUSDT", "123")
        assert result is False

    async def test_client_order_id_prefix_uses_orig_client_order_id_param(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("DELETE", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "ts_abc", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "CANCELED",
            "origQty": "0.1", "executedQty": "0",
        })
        await client.cancel_order("BTCUSDT", "ts_abc")
        assert "origClientOrderId" in server.request_log[-1]["query"]


class TestGetOrder:
    async def test_fetches_by_order_id(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/order", json_body={
            "orderId": 1, "clientOrderId": "c1", "symbol": "BTCUSDT",
            "side": "BUY", "type": "MARKET", "status": "FILLED",
            "origQty": "0.1", "executedQty": "0.1",
        })
        result = await client.get_order("BTCUSDT", "123")
        assert result.exchange_order_id == "1"


class TestGetOpenOrders:
    async def test_omits_symbol_param_when_not_provided(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/openOrders", json_body=[])
        await client.get_open_orders()
        assert "symbol" not in server.request_log[-1]["query"]

    async def test_includes_symbol_param_when_provided(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/openOrders", json_body=[])
        await client.get_open_orders(symbol="btcusdt")
        assert server.request_log[-1]["query"]["symbol"] == "BTCUSDT"


class TestMarketData:
    async def test_get_candles_raises_when_limit_exceeds_exchange_maximum(
        self, server: _BinanceServerFixture
    ) -> None:
        from src.core.enums import Timeframe
        from src.core.exceptions import ValidationError

        client = await server.start()
        with pytest.raises(ValidationError):
            await client.get_candles("BTCUSDT", Timeframe.H1, limit=5000)
        # Fails fast before any request is made — no candle limit worth
        # silently truncating and returning incomplete history for.
        assert not server.request_log

    async def test_get_candles_raises_for_non_positive_limit(
        self, server: _BinanceServerFixture
    ) -> None:
        from src.core.enums import Timeframe
        from src.core.exceptions import ValidationError

        client = await server.start()
        with pytest.raises(ValidationError):
            await client.get_candles("BTCUSDT", Timeframe.H1, limit=0)

    async def test_get_candles_passes_through_a_valid_limit(
        self, server: _BinanceServerFixture
    ) -> None:
        from src.core.enums import Timeframe
        client = await server.start()
        server.queue("GET", "/fapi/v1/klines", json_body=[])
        await client.get_candles("BTCUSDT", Timeframe.H1, limit=1500)
        assert server.request_log[-1]["query"]["limit"] == "1500"

    async def test_get_mark_price_returns_a_decimal(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/premiumIndex", json_body={"markPrice": "50123.45"})
        result = await client.get_mark_price("BTCUSDT")
        assert result == Decimal("50123.45")

    async def test_get_order_book_snaps_depth_up_to_the_nearest_valid_value(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/depth", json_body={"bids": [], "asks": [], "lastUpdateId": 1})
        await client.get_order_book("BTCUSDT", depth=7)
        assert server.request_log[-1]["query"]["limit"] == "10"  # nearest valid >= 7

    async def test_get_server_time_returns_an_int(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/time", json_body={"serverTime": 1700000000000})
        result = await client.get_server_time()
        assert result == 1700000000000


class TestAccountConfiguration:
    async def test_set_leverage_clamps_to_the_1_125_range(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/leverage", json_body={"leverage": 125})
        await client.set_leverage("BTCUSDT", 200)  # over the max
        sent_query = server.request_log[-1]["query"]
        assert sent_query["symbol"] == "BTCUSDT"
        assert sent_query["leverage"] == "125"

    async def test_set_margin_type_no_op_when_already_set(
        self, server: _BinanceServerFixture
    ) -> None:
        # Code -4046 = "No need to change margin type" -- must not raise.
        client = await server.start()
        server.queue("POST", "/fapi/v1/marginType", status=400, json_body={
            "code": -4046, "msg": "No need to change margin type.",
        })
        await client.set_margin_type("BTCUSDT", "ISOLATED")  # must not raise

    async def test_set_margin_type_reraises_a_genuine_error(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/marginType", status=400, json_body={
            "code": -1013, "msg": "some other failure",
        })
        with pytest.raises(ExchangeAPIError):
            await client.set_margin_type("BTCUSDT", "ISOLATED")


class TestListenKeyLifecycle:
    async def test_create_listen_key_returns_the_key(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("POST", "/fapi/v1/listenKey", json_body={"listenKey": "abc123xyz"})
        result = await client.create_listen_key()
        assert result == "abc123xyz"

    async def test_renew_listen_key_does_not_raise(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("PUT", "/fapi/v1/listenKey", json_body={})
        await client.renew_listen_key("abc123")  # must not raise

    async def test_delete_listen_key_does_not_raise(self, server: _BinanceServerFixture) -> None:
        client = await server.start()
        server.queue("DELETE", "/fapi/v1/listenKey", json_body={})
        await client.delete_listen_key("abc123")  # must not raise


class TestErrorMapping:
    async def test_401_status_raises_authentication_error(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/balance", status=401, json_body={"msg": "unauthorized"})
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_code_2014_raises_authentication_error(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/balance", status=400, json_body={
            "code": -2014, "msg": "API-key format invalid.",
        })
        with pytest.raises(ExchangeAuthenticationError):
            await client.get_account_balance()

    async def test_generic_error_code_raises_exchange_api_error(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v2/balance", status=400, json_body={
            "code": -1000, "msg": "unknown error",
        })
        with pytest.raises(ExchangeAPIError):
            await client.get_account_balance()


class TestMapOrderType:
    def test_maps_every_documented_order_type(self) -> None:
        cases = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP_MARKET: "STOP_MARKET",
            OrderType.STOP_LIMIT: "STOP",
            OrderType.TAKE_PROFIT_MARKET: "TAKE_PROFIT_MARKET",
            OrderType.TAKE_PROFIT_LIMIT: "TAKE_PROFIT",
            OrderType.TRAILING_STOP_MARKET: "TRAILING_STOP_MARKET",
        }
        for order_type, expected in cases.items():
            assert BinanceFuturesRESTClient._map_order_type(order_type) == expected


class TestFetchFundingRateHistoryParsed:
    async def test_parses_records_and_sorts_ascending(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/fundingRate", json_body=[
            {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1000 * 3600},
            {"symbol": "BTCUSDT", "fundingRate": "-0.0002", "fundingTime": 1008 * 3600},
        ])
        result = await client.fetch_funding_rate_history_parsed(
            "BTCUSDT", start_time=0, end_time=2_000_000_000_000
        )
        assert len(result) == 2
        assert result[0].symbol == "BTCUSDT"
        assert result[0].rate == Decimal("0.0001")
        assert result[0].timestamp < result[1].timestamp
        # next_funding_time is derived as settlement + 8h
        assert (result[0].next_funding_time - result[0].timestamp).total_seconds() == 8 * 3600

    async def test_paginates_past_the_1000_record_cap(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        page1 = [
            {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": i * 8 * 3600 * 1000}
            for i in range(1000)
        ]
        page2 = [
            {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": (1000 + i) * 8 * 3600 * 1000}
            for i in range(5)
        ]
        server.queue("GET", "/fapi/v1/fundingRate", json_body=page1)
        server.queue("GET", "/fapi/v1/fundingRate", json_body=page2)
        result = await client.fetch_funding_rate_history_parsed(
            "BTCUSDT", start_time=0, end_time=2_000_000_000_000
        )
        assert len(result) == 1005
        # second page's cursor must have advanced past the last record of page1
        second_request_query = server.request_log[1]["query"]
        assert int(second_request_query["startTime"]) == page1[-1]["fundingTime"] + 1

    async def test_returns_empty_list_when_no_records(
        self, server: _BinanceServerFixture
    ) -> None:
        client = await server.start()
        server.queue("GET", "/fapi/v1/fundingRate", json_body=[])
        result = await client.fetch_funding_rate_history_parsed(
            "BTCUSDT", start_time=0, end_time=2_000_000_000_000
        )
        assert result == []
