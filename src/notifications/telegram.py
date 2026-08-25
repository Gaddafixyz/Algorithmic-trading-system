"""Telegram bot notification channel."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends messages to a Telegram chat via the Bot API."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._sent = 0
        self._failed = 0

    async def send(self, message: str, severity: str = "info") -> bool:
        """Send a message. Returns True on success."""
        url = TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_notification": severity == "info",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        self._sent += 1
                        return True
                    body = await resp.text()
                    logger.warning("Telegram send failed %d: %s", resp.status, body[:200])
                    self._failed += 1
                    return False
        except Exception as exc:
            logger.warning("Telegram error: %s", exc)
            self._failed += 1
            return False

    async def send_trade_notification(self, trade_data: dict[str, Any]) -> bool:
        symbol = trade_data.get("symbol", "?")
        pnl = trade_data.get("net_pnl", "?")
        try:
            is_win = float(str(pnl).replace(",", "")) > 0
        except ValueError:
            is_win = False
        outcome = "✅ WIN" if is_win else "❌ LOSS"
        msg = f"*{outcome}* {symbol}\nPnL: `{pnl}`"
        return await self.send(msg, "info")
