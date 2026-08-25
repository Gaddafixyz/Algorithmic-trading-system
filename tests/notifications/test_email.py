"""Unit tests for src.notifications.email.EmailNotifier.

smtplib is blocking I/O run via loop.run_in_executor (see the module
docstring in src/notifications/email.py for why), so smtplib.SMTP itself is
mocked rather than spinning up a real SMTP server.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.notifications.email import EmailNotifier


def _make_notifier(**overrides) -> EmailNotifier:
    kwargs = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username="bot@example.com",
        password="hunter2",
        recipient="alerts@example.com",
    )
    kwargs.update(overrides)
    return EmailNotifier(**kwargs)


class TestSend:
    async def test_success_logs_in_and_sends_returns_true(self) -> None:
        notifier = _make_notifier()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server

            ok = await notifier.send("something happened", "warning")

        assert ok is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("bot@example.com", "hunter2")
        assert mock_server.sendmail.call_count == 1
        sender, recipients, raw_message = mock_server.sendmail.call_args.args
        assert sender == "bot@example.com"
        assert recipients == ["alerts@example.com"]
        assert "something happened" in raw_message
        assert "WARNING" in raw_message  # subject includes upper-cased severity

    async def test_use_tls_false_skips_starttls(self) -> None:
        notifier = _make_notifier(use_tls=False)
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server

            await notifier.send("msg")

        mock_server.starttls.assert_not_called()

    async def test_sender_defaults_to_username(self) -> None:
        notifier = _make_notifier()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server
            await notifier.send("msg")

        sender = mock_server.sendmail.call_args.args[0]
        assert sender == "bot@example.com"

    async def test_explicit_sender_overrides_username(self) -> None:
        notifier = _make_notifier(sender="alerts-from@example.com")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server
            await notifier.send("msg")

        sender = mock_server.sendmail.call_args.args[0]
        assert sender == "alerts-from@example.com"

    async def test_smtp_failure_returns_false_not_raise(self) -> None:
        notifier = _make_notifier()
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            ok = await notifier.send("msg")

        assert ok is False

    async def test_login_failure_returns_false_not_raise(self) -> None:
        import smtplib

        notifier = _make_notifier()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
            mock_smtp_cls.return_value.__enter__.return_value = mock_server

            ok = await notifier.send("msg")

        assert ok is False


class TestSendTradeNotification:
    async def test_includes_all_trade_fields_in_body(self) -> None:
        notifier = _make_notifier()
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_server

            await notifier.send_trade_notification({"symbol": "BTCUSDT", "net_pnl": "10.00"})

        raw_message = mock_server.sendmail.call_args.args[2]
        assert "symbol: BTCUSDT" in raw_message
        assert "net_pnl: 10.00" in raw_message
        assert "Trade Closed" in raw_message
