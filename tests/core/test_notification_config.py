"""Unit tests for src.core.config.NotificationConfig's *_enabled properties.

Context: `Application._setup_notifications` previously duplicated
`telegram_enabled`/`discord_enabled`/`slack_enabled`'s exact boolean
logic inline (`n.telegram_bot_token and n.telegram_chat_id`, etc.)
instead of calling the properties, even though `email_enabled` was
already called directly. `[1.3.20]`+1 switched all four branches to the
properties for consistency; these tests cover the properties in
isolation (they previously had no direct test — only indirect coverage
via TestNotificationSetup in tests/bootstrap/test_app.py, which
exercises the inline expressions' behavior, not the properties
themselves).
"""

from __future__ import annotations

from src.core.config import NotificationConfig


class TestTelegramEnabled:
    def test_false_when_neither_set(self) -> None:
        assert NotificationConfig(_env_file=None).telegram_enabled is False

    def test_false_when_only_token_set(self) -> None:
        cfg = NotificationConfig(_env_file=None, telegram_bot_token="tok")
        assert cfg.telegram_enabled is False

    def test_false_when_only_chat_id_set(self) -> None:
        cfg = NotificationConfig(_env_file=None, telegram_chat_id="chat-1")
        assert cfg.telegram_enabled is False

    def test_true_when_both_set(self) -> None:
        cfg = NotificationConfig(
            _env_file=None, telegram_bot_token="tok", telegram_chat_id="chat-1"
        )
        assert cfg.telegram_enabled is True


class TestDiscordEnabled:
    def test_false_when_unset(self) -> None:
        assert NotificationConfig(_env_file=None).discord_enabled is False

    def test_true_when_webhook_set(self) -> None:
        cfg = NotificationConfig(
            _env_file=None, discord_webhook_url="https://discord.example/webhook"
        )
        assert cfg.discord_enabled is True


class TestSlackEnabled:
    def test_false_when_unset(self) -> None:
        assert NotificationConfig(_env_file=None).slack_enabled is False

    def test_true_when_webhook_set(self) -> None:
        cfg = NotificationConfig(
            _env_file=None, slack_webhook_url="https://hooks.slack.example/webhook"
        )
        assert cfg.slack_enabled is True
