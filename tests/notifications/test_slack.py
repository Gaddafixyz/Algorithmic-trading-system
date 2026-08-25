"""Unit tests for src.notifications.slack.SlackNotifier.

Uses a real local aiohttp.web test server rather than a mocking library,
for the same reason documented in tests/exchange/test_binance_rest.py:
aioresponses (the only available aiohttp-mocking library) has a known
unreleased-fix incompatibility with aiohttp 3.14+.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.notifications.slack import SlackNotifier


class _WebhookServer:
    def __init__(self, *, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.server: TestServer | None = None

    async def _handler(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        return web.Response(status=self.status)

    async def start(self) -> SlackNotifier:
        app = web.Application()
        app.router.add_post("/webhook", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        url = f"http://{self.server.host}:{self.server.port}/webhook"
        return SlackNotifier(url)

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


@pytest.fixture
async def webhook():
    fixture = _WebhookServer()
    yield fixture
    await fixture.stop()


class TestSend:
    @pytest.mark.parametrize(
        "severity,emoji",
        [("info", "ℹ️"), ("warning", "⚠️"), ("error", "🔴"), ("critical", "🚨")],
    )
    async def test_prefixes_message_with_severity_emoji(
        self, webhook: _WebhookServer, severity: str, emoji: str
    ) -> None:
        notifier = await webhook.start()
        await notifier.send("something happened", severity)
        assert webhook.requests[0]["text"] == f"{emoji} something happened"

    async def test_unknown_severity_uses_default_emoji(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send("msg", "not-a-real-severity")
        assert webhook.requests[0]["text"] == "📢 msg"

    async def test_success_returns_true(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        assert await notifier.send("msg") is True

    async def test_non_200_status_returns_false(self) -> None:
        fixture = _WebhookServer(status=500)
        notifier = await fixture.start()
        try:
            assert await notifier.send("msg") is False
        finally:
            await fixture.stop()

    async def test_connection_failure_returns_false_not_raise(self) -> None:
        notifier = SlackNotifier("http://127.0.0.1:1/webhook")
        assert await notifier.send("msg") is False


class TestSendTradeNotification:
    async def test_includes_symbol_and_pnl(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "10.00"})
        text = webhook.requests[0]["text"]
        assert "BTCUSDT" in text
        assert "10.00" in text
