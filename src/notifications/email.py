"""SMTP email notification channel."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Sends alert emails via SMTP."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        recipient: str,
        sender: str | None = None,
        use_tls: bool = True,
    ) -> None:
        self._host = smtp_host
        self._port = smtp_port
        self._user = username
        self._password = password
        self._recipient = recipient
        self._sender = sender or username
        self._use_tls = use_tls

    async def send(self, message: str, severity: str = "info") -> bool:
        """
        Send an email.

        ``smtplib`` is blocking I/O with no async variant, so the actual
        send runs in the default executor via ``run_in_executor`` — calling
        it directly here would stall the entire asyncio event loop (WS
        message processing, order placement, everything) for the duration
        of the SMTP handshake plus up to the 10s socket timeout, on every
        alert sent. The other notification channels (Telegram, Discord,
        Slack) avoid this naturally since they use aiohttp.
        """
        subject = f"[Trading System] {severity.upper()} Alert"

        def _send_sync() -> None:
            msg = MIMEMultipart()
            msg["From"] = self._sender
            msg["To"] = self._recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(message, "plain"))

            with smtplib.SMTP(self._host, self._port, timeout=10) as server:
                if self._use_tls:
                    server.starttls()
                server.login(self._user, self._password)
                server.sendmail(self._sender, [self._recipient], msg.as_string())

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _send_sync)
            logger.debug("Email sent to %s", self._recipient)
            return True
        except Exception as exc:
            logger.warning("Email send failed: %s", exc)
            return False

    async def send_trade_notification(self, trade_data: dict[str, Any]) -> bool:
        msg = "\n".join(f"{k}: {v}" for k, v in trade_data.items())
        return await self.send(f"Trade Closed\n\n{msg}", "info")
