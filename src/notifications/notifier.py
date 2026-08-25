"""
Unified notification dispatcher.

Routes alerts and trade notifications to all configured channels.
Channels that are not configured are silently skipped.
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.enums import AlertSeverity

logger = logging.getLogger(__name__)


class Notifier:
    """
    Fan-out notification dispatcher.

    Add channel instances via add_channel(); send() dispatches to all.
    """

    def __init__(self) -> None:
        self._channels: list[Any] = []

    def add_channel(self, channel: Any) -> None:
        self._channels.append(channel)

    async def send(self, message: str, severity: AlertSeverity = AlertSeverity.INFO) -> None:
        """Send a plain text notification to all channels."""
        for channel in self._channels:
            try:
                await channel.send(message, severity.value)
            except Exception as exc:
                logger.warning("Notification channel %s failed: %s", type(channel).__name__, exc)

    async def send_trade(self, trade_data: dict[str, Any]) -> None:
        """Send a trade notification to all channels."""
        message = self._format_trade(trade_data)
        await self.send(message, AlertSeverity.INFO)

    @staticmethod
    def _format_trade(data: dict[str, Any]) -> str:
        symbol = data.get("symbol", "?")
        side = data.get("side", "?")
        pnl = data.get("net_pnl", "?")
        entry = data.get("entry_price", "?")
        exit_p = data.get("exit_price", "?")
        return (
            f"📊 Trade Closed: {symbol} {side}\n" f"Entry: {entry} → Exit: {exit_p}\n" f"PnL: {pnl}"
        )
