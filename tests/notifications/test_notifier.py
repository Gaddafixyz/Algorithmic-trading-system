"""Unit tests for src.notifications.notifier.Notifier.

No previous coverage existed for this module: the fan-out dispatcher is the
only path production code actually calls (see bootstrap/startup.py
`_on_alert_fired` and `_on_position_closed_notify`), so a silent regression
here means every alert/trade-close notification stops firing with nothing
in the test suite to catch it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from src.core.enums import AlertSeverity
from src.notifications.notifier import Notifier


class _RecordingChannel:
    """A channel double that records every call it receives."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def send(self, message: str, severity: str) -> bool:
        self.calls.append((message, severity))
        if self.fail:
            raise RuntimeError("channel unavailable")
        return True


class TestSend:
    async def test_dispatches_to_every_registered_channel(self) -> None:
        notifier = Notifier()
        a, b = _RecordingChannel(), _RecordingChannel()
        notifier.add_channel(a)
        notifier.add_channel(b)

        await notifier.send("hello", AlertSeverity.WARNING)

        assert a.calls == [("hello", "warning")]
        assert b.calls == [("hello", "warning")]

    async def test_defaults_to_info_severity(self) -> None:
        notifier = Notifier()
        channel = _RecordingChannel()
        notifier.add_channel(channel)

        await notifier.send("hello")

        assert channel.calls == [("hello", "info")]

    async def test_no_channels_is_a_noop(self) -> None:
        notifier = Notifier()
        # Should not raise even with nothing registered.
        await notifier.send("hello")

    async def test_one_failing_channel_does_not_block_others(self) -> None:
        """Error isolation: a channel raising must not stop the fan-out."""
        notifier = Notifier()
        failing = _RecordingChannel(fail=True)
        healthy = _RecordingChannel()
        notifier.add_channel(failing)
        notifier.add_channel(healthy)

        await notifier.send("hello", AlertSeverity.CRITICAL)

        assert failing.calls == [("hello", "critical")]
        assert healthy.calls == [("hello", "critical")]

    async def test_channel_exception_is_swallowed_not_raised(self) -> None:
        notifier = Notifier()
        notifier.add_channel(_RecordingChannel(fail=True))
        # Must not propagate.
        await notifier.send("hello")


class TestSendTrade:
    async def test_formats_and_sends_at_info_severity(self) -> None:
        notifier = Notifier()
        channel = AsyncMock()
        notifier.add_channel(channel)

        trade_data: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "net_pnl": "125.50",
            "entry_price": "50000",
            "exit_price": "50500",
        }
        await notifier.send_trade(trade_data)

        channel.send.assert_awaited_once()
        message, severity = channel.send.await_args.args
        assert severity == "info"
        assert "BTCUSDT" in message
        assert "LONG" in message
        assert "50000" in message and "50500" in message
        assert "125.50" in message

    async def test_missing_fields_fall_back_to_placeholder(self) -> None:
        notifier = Notifier()
        channel = AsyncMock()
        notifier.add_channel(channel)

        await notifier.send_trade({})

        message, _ = channel.send.await_args.args
        assert "?" in message
