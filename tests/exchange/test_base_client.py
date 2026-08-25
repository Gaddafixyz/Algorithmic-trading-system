"""Unit tests for src.exchange.base_client.BaseExchangeClient.

Uses a real local aiohttp.web test server (via aiohttp.test_utils)
rather than a mocking library. This is a deliberate choice: the only
available aiohttp-request-mocking library, aioresponses (0.7.9 at
time of writing), has a known incompatibility with aiohttp >= 3.14
(ClientResponse.__init__() gained a required `stream_writer` keyword
argument that aioresponses does not yet pass; the fix is merged
upstream but not published as a release). Rather than monkeypatch
around a third-party library's internals -- fragile, and liable to
silently double-patch once the real fix ships -- these tests spin up
a genuine local HTTP server and exercise the real aiohttp client/
server wire protocol, arguably a more faithful test of the retry/
backoff/error-handling logic anyway.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.core.enums import ExchangeStatus
from src.core.exceptions import ExchangeConnectionError, MaxRetriesExceededError
from src.exchange.base_client import BaseExchangeClient


class _MinimalClient(BaseExchangeClient):
    """Minimal concrete BaseExchangeClient for testing the base's HTTP logic."""

    def __init__(self, *args: Any, drift_error_codes: tuple[int, ...] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._drift_error_codes = drift_error_codes
        self.sync_clock_calls = 0

    async def get_server_time(self) -> int:
        return 1_700_000_000_000

    async def sync_clock(self) -> int:
        self.sync_clock_calls += 1
        return await super().sync_clock()

    async def _sign_request(self, params, data):
        return params, data

    def _handle_response_error(self, status: int, body: Any) -> None:
        if status >= 400:
            raise ExchangeConnectionError(f"error status {status}: {body}")

    def _is_clock_drift_error(self, body: Any) -> bool:
        if isinstance(body, dict) and body.get("code") in self._drift_error_codes:
            return True
        return False

    async def get_account_balance(self) -> dict[str, Decimal]:
        raise NotImplementedError

    async def get_positions(self):
        raise NotImplementedError

    async def place_order(self, *args, **kwargs):
        raise NotImplementedError

    async def cancel_order(self, symbol, order_id):
        raise NotImplementedError

    async def get_order(self, symbol, order_id):
        raise NotImplementedError

    async def get_open_orders(self, symbol=None):
        raise NotImplementedError

    async def get_candles(self, symbol, timeframe, limit, start_time=None):
        raise NotImplementedError

    async def get_ticker(self, symbol):
        raise NotImplementedError

    async def get_order_book(self, symbol, depth=10):
        raise NotImplementedError

    async def set_leverage(self, symbol, leverage):
        raise NotImplementedError

    async def set_margin_type(self, symbol, margin_type):
        raise NotImplementedError


class _TestServerFixture:
    """Wraps a local aiohttp.web server plus a BaseExchangeClient pointed at it."""

    def __init__(self) -> None:
        self.request_log: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, str], list[web.Response]] = {}
        self.server: TestServer | None = None
        self.client: _MinimalClient | None = None

    def queue_response(
        self, method: str, path: str, *, status: int = 200,
        json_body: Any = None, text_body: str | None = None,
        content_type: str = "application/json", headers: dict[str, str] | None = None,
    ) -> None:
        key = (method.upper(), path)
        body = json.dumps(json_body) if json_body is not None else (text_body or "")
        resp = web.Response(status=status, text=body, content_type=content_type, headers=headers or {})
        self._routes.setdefault(key, []).append(resp)

    async def _handler(self, request: web.Request) -> web.Response:
        self.request_log.append({"method": request.method, "path": request.path})
        key = (request.method, request.path)
        queue = self._routes.get(key, [])
        if not queue:
            return web.Response(status=404, text="not found")
        return queue.pop(0)

    async def start(self, max_retries: int = 3) -> _MinimalClient:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        base_url = f"http://{self.server.host}:{self.server.port}"
        self.client = _MinimalClient(base_url, max_retries=max_retries)
        return self.client

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
        if self.server:
            await self.server.close()


@pytest.fixture
async def server():
    fixture = _TestServerFixture()
    yield fixture
    await fixture.stop()


class TestConnectDisconnect:
    async def test_connect_sets_status_connected(self, server: _TestServerFixture) -> None:
        client = await server.start()
        await client.connect()
        assert client.status == ExchangeStatus.CONNECTED

    async def test_disconnect_sets_status_disconnected(self, server: _TestServerFixture) -> None:
        client = await server.start()
        await client.connect()
        await client.disconnect()
        assert client.status == ExchangeStatus.DISCONNECTED

    async def test_connect_is_idempotent_when_already_connected(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        await client.connect()
        session_before = client._session
        await client.connect()
        assert client._session is session_before

    async def test_async_context_manager_connects_and_disconnects(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        async with client as ctx_client:
            assert ctx_client.status == ExchangeStatus.CONNECTED
        assert client.status == ExchangeStatus.DISCONNECTED


class TestSuccessfulRequest:
    async def test_get_returns_the_parsed_json_body(self, server: _TestServerFixture) -> None:
        client = await server.start()
        server.queue_response("GET", "/ping", json_body={"ok": True})
        result = await client._get("/ping")
        assert result == {"ok": True}

    async def test_request_count_increments_on_each_call(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        server.queue_response("GET", "/ping", json_body={"ok": True})
        server.queue_response("GET", "/ping", json_body={"ok": True})
        await client._get("/ping")
        await client._get("/ping")
        assert client.stats["requests"] == 2


class TestServerErrorRetry:
    async def test_a_500_is_retried_and_succeeds_on_a_later_attempt(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        server.queue_response("GET", "/flaky", status=500, text_body="server error")
        server.queue_response("GET", "/flaky", json_body={"ok": True})
        result = await client._get("/flaky")
        assert result == {"ok": True}

    async def test_exhausting_all_retries_raises_max_retries_exceeded(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start(max_retries=3)
        for _ in range(5):
            server.queue_response("GET", "/always-down", status=500, text_body="down")
        with pytest.raises(MaxRetriesExceededError):
            await client._get("/always-down")
        assert client.stats["errors"] >= 1


class TestRateLimitHandling:
    async def test_a_429_triggers_a_retry_and_eventually_succeeds(
        self, server: _TestServerFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = await server.start()
        server.queue_response(
            "GET", "/limited", status=429, text_body="rate limited",
            headers={"Retry-After": "2"},
        )
        server.queue_response("GET", "/limited", json_body={"ok": True})

        result = await client._get("/limited")
        assert result == {"ok": True}
        assert 2.0 in sleep_calls


class TestNonJsonResponse:
    async def test_non_json_body_is_retried_as_a_connection_error(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        server.queue_response(
            "GET", "/html-error", status=200, text_body="<html>not json</html>",
            content_type="text/html",
        )
        server.queue_response("GET", "/html-error", json_body={"ok": True})
        result = await client._get("/html-error")
        assert result == {"ok": True}


class TestClockDriftDetection:
    async def test_a_drift_error_triggers_resync_and_retries(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        client._drift_error_codes = (-1021,)
        server.queue_response(
            "GET", "/signed", status=400,
            json_body={"code": -1021, "msg": "Timestamp outside recvWindow"},
        )
        server.queue_response("GET", "/signed", json_body={"ok": True})
        result = await client._get("/signed", signed=True)
        assert result == {"ok": True}
        assert client.sync_clock_calls == 1


class TestHttpVerbMethods:
    async def test_post_sends_data_and_returns_response(
        self, server: _TestServerFixture
    ) -> None:
        client = await server.start()
        server.queue_response("POST", "/orders", json_body={"orderId": 1})
        result = await client._post("/orders", data={"symbol": "BTCUSDT"})
        assert result == {"orderId": 1}

    async def test_delete_returns_response(self, server: _TestServerFixture) -> None:
        client = await server.start()
        server.queue_response("DELETE", "/orders/1", json_body={"cancelled": True})
        result = await client._delete("/orders/1")
        assert result == {"cancelled": True}

    async def test_put_returns_response(self, server: _TestServerFixture) -> None:
        client = await server.start()
        server.queue_response("PUT", "/orders/1", json_body={"updated": True})
        result = await client._put("/orders/1", data={"quantity": "0.2"})
        assert result == {"updated": True}


class TestSyncClock:
    async def test_computes_a_reasonable_offset(self, server: _TestServerFixture) -> None:
        client = await server.start()
        offset = await client.sync_clock()
        assert isinstance(offset, int)
        assert client.clock_offset_ms == offset
