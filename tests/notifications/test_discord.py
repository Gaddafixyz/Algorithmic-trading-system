"""Unit tests for src.notifications.discord.DiscordNotifier.

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

from src.notifications.discord import SEVERITY_COLORS, DiscordNotifier


class _WebhookServer:
    def __init__(self, *, status: int = 204) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.server: TestServer | None = None

    async def _handler(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        return web.Response(status=self.status)

    async def start(self) -> DiscordNotifier:
        app = web.Application()
        app.router.add_post("/webhook", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        url = f"http://{self.server.host}:{self.server.port}/webhook"
        return DiscordNotifier(url)

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


@pytest.fixture
async def webhook():
    fixture = _WebhookServer()
    yield fixture
    await fixture.stop()


class TestSend:
    async def test_success_returns_true_and_posts_embed(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        ok = await notifier.send("something happened", "warning")

        assert ok is True
        assert len(webhook.requests) == 1
        embed = webhook.requests[0]["embeds"][0]
        assert embed["description"] == "something happened"
        assert embed["color"] == SEVERITY_COLORS["warning"]

    async def test_204_status_counts_as_success(self) -> None:
        fixture = _WebhookServer(status=204)
        notifier = await fixture.start()
        try:
            assert await notifier.send("ok") is True
        finally:
            await fixture.stop()

    async def test_unknown_severity_uses_default_color(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send("msg", "not-a-real-severity")

        assert webhook.requests[0]["embeds"][0]["color"] == 0x95A5A6

    async def test_non_success_status_returns_false_and_counts_failure(self) -> None:
        fixture = _WebhookServer(status=500)
        notifier = await fixture.start()
        try:
            ok = await notifier.send("msg")
            assert ok is False
            assert notifier._failed == 1
            assert notifier._sent == 0
        finally:
            await fixture.stop()

    async def test_connection_failure_returns_false_not_raise(self) -> None:
        # Nothing listening on this port.
        notifier = DiscordNotifier("http://127.0.0.1:1/webhook")
        ok = await notifier.send("msg")
        assert ok is False
        assert notifier._failed == 1


class TestSendTradeNotification:
    async def test_positive_pnl_is_labeled_win(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send_trade_notification(
            {"symbol": "ETHUSDT", "net_pnl": "42.10", "entry_price": "3000", "exit_price": "3050"}
        )
        message = webhook.requests[0]["embeds"][0]["description"]
        assert "WIN" in message
        assert "ETHUSDT" in message

    async def test_negative_pnl_is_labeled_loss(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send_trade_notification({"symbol": "ETHUSDT", "net_pnl": "-12.00"})
        message = webhook.requests[0]["embeds"][0]["description"]
        assert "LOSS" in message

    async def test_zero_pnl_is_not_a_win(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send_trade_notification({"symbol": "ETHUSDT", "net_pnl": "0"})
        message = webhook.requests[0]["embeds"][0]["description"]
        assert "LOSS" in message

    async def test_comma_formatted_pnl_is_parsed(self, webhook: _WebhookServer) -> None:
        notifier = await webhook.start()
        await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "1,250.00"})
        message = webhook.requests[0]["embeds"][0]["description"]
        assert "WIN" in message
