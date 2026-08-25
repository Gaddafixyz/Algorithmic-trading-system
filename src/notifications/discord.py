"""Discord webhook notification channel."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

SEVERITY_COLORS = {
    "info": 0x3498DB,
    "warning": 0xF39C12,
    "error": 0xE74C3C,
    "critical": 0x8E44AD,
}


class DiscordNotifier:
    """Sends embed messages to a Discord channel via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url
        self._sent = 0
        self._failed = 0

    async def send(self, message: str, severity: str = "info") -> bool:
        color = SEVERITY_COLORS.get(severity.lower(), 0x95A5A6)
        payload = {
            "embeds": [
                {
                    "description": message,
                    "color": color,
                }
            ]
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status in (200, 204):
                        self._sent += 1
                        return True
                    self._failed += 1
                    return False
        except Exception as exc:
            logger.warning("Discord error: %s", exc)
            self._failed += 1
            return False

    async def send_trade_notification(self, trade_data: dict[str, Any]) -> bool:
        pnl = trade_data.get("net_pnl", 0)
        is_win = float(str(pnl).replace(",", "")) > 0
        msg = (
            f"**{'✅ WIN' if is_win else '❌ LOSS'}** — {trade_data.get('symbol', '?')}\n"
            f"PnL: `{pnl}`\n"
            f"Entry: {trade_data.get('entry_price')} → Exit: {trade_data.get('exit_price')}"
        )
        return await self.send(msg, "info")
