"""Unit tests for src.notifications.telegram.TelegramNotifier.

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

from src.notifications.telegram import TelegramNotifier


class _BotServer:
    def __init__(self, *, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.server: TestServer | None = None

    async def _handler(self, request: web.Request) -> web.Response:
        self.paths.append(request.path)
        self.requests.append(await request.json())
        return web.Response(status=self.status, text="{}")

    async def start(self) -> TelegramNotifier:
        app = web.Application()
        app.router.add_post("/bot{token}/sendMessage", self._handler)
        self.server = TestServer(app)
        await self.server.start_server()
        # Patch the module-level template so the client hits our test server
        # instead of api.telegram.org.
        import src.notifications.telegram as telegram_module

        telegram_module.TELEGRAM_API = (
            f"http://{self.server.host}:{self.server.port}/bot{{token}}/sendMessage"
        )
        return TelegramNotifier(bot_token="test-token", chat_id="12345")

    async def stop(self) -> None:
        if self.server:
            await self.server.close()


@pytest.fixture
async def bot():
    fixture = _BotServer()
    yield fixture
    await fixture.stop()


class TestSend:
    async def test_success_posts_chat_id_and_text(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        ok = await notifier.send("hello there")

        assert ok is True
        assert bot.requests[0]["chat_id"] == "12345"
        assert bot.requests[0]["text"] == "hello there"

    async def test_info_severity_disables_notification(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        await notifier.send("hello", "info")
        assert bot.requests[0]["disable_notification"] is True

    async def test_non_info_severity_enables_notification(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        await notifier.send("hello", "critical")
        assert bot.requests[0]["disable_notification"] is False

    async def test_non_200_status_returns_false(self) -> None:
        fixture = _BotServer(status=400)
        notifier = await fixture.start()
        try:
            ok = await notifier.send("hello")
            assert ok is False
            assert notifier._failed == 1
        finally:
            await fixture.stop()

    async def test_connection_failure_returns_false_not_raise(self) -> None:
        notifier = TelegramNotifier(bot_token="t", chat_id="1")
        import src.notifications.telegram as telegram_module

        telegram_module.TELEGRAM_API = "http://127.0.0.1:1/bot{token}/sendMessage"
        ok = await notifier.send("hello")
        assert ok is False


class TestSendTradeNotification:
    """Regression coverage for the win/loss detection fix.

    The original implementation determined WIN/LOSS by checking whether
    the PnL *string* started with "-", which mislabeled breakeven trades
    (net_pnl == "0") and any non-numeric formatting as a WIN, and was
    inconsistent with DiscordNotifier's numeric comparison. It now parses
    the value numerically, matching Discord's behavior.
    """

    async def test_positive_pnl_is_win(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "15.00"})
        assert "WIN" in bot.requests[0]["text"]

    async def test_negative_pnl_is_loss(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "-15.00"})
        assert "LOSS" in bot.requests[0]["text"]

    async def test_zero_pnl_is_not_a_win(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "0"})
        assert "LOSS" in bot.requests[0]["text"]

    async def test_missing_pnl_does_not_raise(self, bot: _BotServer) -> None:
        notifier = await bot.start()
        # Should not crash on the "?" placeholder from a partial payload.
        await notifier.send_trade_notification({"symbol": "BTCUSDT"})
        assert "LOSS" in bot.requests[0]["text"]
