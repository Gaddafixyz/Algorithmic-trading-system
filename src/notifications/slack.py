"""Slack webhook notification channel."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Sends messages to a Slack channel via incoming webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url
        self._sent = 0

    async def send(self, message: str, severity: str = "info") -> bool:
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🔴", "critical": "🚨"}.get(severity, "📢")
        payload = {"text": f"{emoji} {message}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        self._sent += 1
                        return True
                    return False
        except Exception as exc:
            logger.warning("Slack error: %s", exc)
            return False

    async def send_trade_notification(self, trade_data: dict[str, Any]) -> bool:
        msg = f"Trade: {trade_data.get('symbol')} PnL={trade_data.get('net_pnl')}"
        return await self.send(msg)
