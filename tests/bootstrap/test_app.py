"""Unit tests for src.bootstrap.app.Application / create_application.

Constructs real Application instances from a minimal, real Settings
object (dummy non-placeholder Binance/Bybit credentials, no network
access -- Application.__init__ only wires objects together, it never
connects). This exercises the actual dependency graph rather than a
mocked stand-in, which is what makes this module worth testing: the
wiring itself (notification channel selection, account-service
selection, correlation-tracker gating, dashboard provider wiring) is
the behavior under test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.bootstrap.app import Application, create_application
from src.core.config import SymbolRiskConfig
from src.core.enums import TradingEnvironment


class TestApplicationConstruction:
    def test_binance_provider_builds_binance_rest_and_ws_clients(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.exchange.binance.rest import BinanceFuturesRESTClient
        from src.exchange.binance.websocket import BinanceFuturesWebSocket

        settings = make_settings(provider="binance", with_binance=True)
        app = Application(settings)

        assert isinstance(app.rest_client, BinanceFuturesRESTClient)
        assert isinstance(app.ws_client, BinanceFuturesWebSocket)
        assert app.exchange_provider == "binance"

    def test_bybit_provider_builds_bybit_rest_and_ws_clients(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.exchange.adapters.bybit import BybitFuturesClient
        from src.exchange.adapters.bybit.websocket import BybitWebSocket

        settings = make_settings(provider="bybit", with_binance=False, with_bybit=True)
        app = Application(settings)

        assert isinstance(app.rest_client, BybitFuturesClient)
        assert isinstance(app.ws_client, BybitWebSocket)
        assert app.exchange_provider == "bybit"

    def test_listen_key_starts_unset(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        assert app._listen_key is None

    def test_paper_mode_builds_paper_exchange_client_as_trading_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.paper_trading.paper_exchange_client import PaperExchangeClient

        settings = make_settings(trading_env=TradingEnvironment.PAPER)
        app = Application(settings)

        assert isinstance(app.paper_exchange_client, PaperExchangeClient)
        assert app.trading_client is app.paper_exchange_client
        assert app.order_manager._exchange is app.paper_exchange_client  # noqa: SLF001
        assert app.reconciliation._exchange is app.paper_exchange_client  # noqa: SLF001

    def test_non_paper_mode_uses_rest_client_as_trading_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(trading_env=TradingEnvironment.TESTNET)
        app = Application(settings)

        assert app.paper_exchange_client is None
        assert app.trading_client is app.rest_client
        assert app.order_manager._exchange is app.rest_client  # noqa: SLF001

    def test_binance_provider_constructs_account_service(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.exchange.binance.account import BinanceAccountService

        settings = make_settings(provider="binance", with_binance=True)
        app = Application(settings)
        assert isinstance(app.account_service, BinanceAccountService)

    def test_bybit_provider_has_no_account_service(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="bybit", with_binance=False, with_bybit=True)
        app = Application(settings)
        assert app.account_service is None

    def test_instruments_start_empty(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        assert app.instruments == {}


class TestCorrelationTrackerGating:
    def test_no_correlation_tracker_with_zero_symbols(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(symbols={})
        app = Application(settings)
        assert app.risk_manager._correlation_tracker is None  # noqa: SLF001

    def test_no_correlation_tracker_with_a_single_enabled_symbol(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            symbols={"BTCUSDT": SymbolRiskConfig(leverage=5, enabled=True)}
        )
        app = Application(settings)
        assert app.risk_manager._correlation_tracker is None  # noqa: SLF001

    def test_correlation_tracker_created_with_two_or_more_enabled_symbols(
        self, make_settings
    ) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            symbols={
                "BTCUSDT": SymbolRiskConfig(leverage=5, enabled=True),
                "ETHUSDT": SymbolRiskConfig(leverage=5, enabled=True),
            }
        )
        app = Application(settings)
        assert app.risk_manager._correlation_tracker is not None  # noqa: SLF001

    def test_disabled_symbols_do_not_count_toward_correlation_gating(
        self, make_settings
    ) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            symbols={
                "BTCUSDT": SymbolRiskConfig(leverage=5, enabled=True),
                "ETHUSDT": SymbolRiskConfig(leverage=5, enabled=False),
            }
        )
        app = Application(settings)
        assert app.risk_manager._correlation_tracker is None  # noqa: SLF001


class TestNotificationSetup:
    def test_no_channels_configured_by_default(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        assert app.notifier._channels == []  # noqa: SLF001

    def test_telegram_channel_added_when_token_and_chat_id_set(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.notifications.telegram import TelegramNotifier

        settings = make_settings(
            notifications={
                "telegram_bot_token": "tok",
                "telegram_chat_id": "chat-1",
            }
        )
        app = Application(settings)
        assert any(isinstance(c, TelegramNotifier) for c in app.notifier._channels)  # noqa: SLF001

    def test_telegram_channel_not_added_when_only_token_set(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(notifications={"telegram_bot_token": "tok"})
        app = Application(settings)
        assert app.notifier._channels == []  # noqa: SLF001

    def test_discord_channel_added_when_webhook_set(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.notifications.discord import DiscordNotifier

        settings = make_settings(
            notifications={"discord_webhook_url": "https://discord.example/webhook"}
        )
        app = Application(settings)
        assert any(isinstance(c, DiscordNotifier) for c in app.notifier._channels)  # noqa: SLF001

    def test_slack_channel_added_when_webhook_set(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.notifications.slack import SlackNotifier

        settings = make_settings(
            notifications={"slack_webhook_url": "https://hooks.slack.example/webhook"}
        )
        app = Application(settings)
        assert any(isinstance(c, SlackNotifier) for c in app.notifier._channels)  # noqa: SLF001

    def test_email_channel_requires_all_four_fields(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.notifications.email import EmailNotifier

        settings = make_settings(
            notifications={
                "smtp_host": "smtp.example.com",
                "smtp_user": "bot@example.com",
                "smtp_password": "secret",
                "alert_email": "ops@example.com",
            }
        )
        app = Application(settings)
        assert any(isinstance(c, EmailNotifier) for c in app.notifier._channels)  # noqa: SLF001

    def test_email_channel_not_added_when_password_missing(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            notifications={
                "smtp_host": "smtp.example.com",
                "smtp_user": "bot@example.com",
                "alert_email": "ops@example.com",
            }
        )
        app = Application(settings)
        assert settings.notifications.email_enabled is False
        assert app.notifier._channels == []  # noqa: SLF001


class TestSyncEquity:
    async def test_binance_uses_account_service_equity(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        # Non-paper: sync_equity's account_service branch is only taken
        # when there's no paper_exchange_client (see TestSyncEquityPaperMode
        # for the paper-mode branch, which bypasses account_service/
        # rest_client entirely).
        settings = make_settings(
            provider="binance", with_binance=True, trading_env=TradingEnvironment.TESTNET,
        )
        app = Application(settings)

        from unittest.mock import AsyncMock

        app.account_service.get_equity = AsyncMock(return_value=Decimal("7500"))

        await app.sync_equity()

        assert app.risk_manager.equity == Decimal("7500")

    async def test_bybit_uses_usdt_wallet_balance(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            provider="bybit", with_binance=False, with_bybit=True,
            trading_env=TradingEnvironment.TESTNET,
        )
        app = Application(settings)

        from unittest.mock import AsyncMock

        app.rest_client.get_account_balance = AsyncMock(
            return_value={"USDT": Decimal("3200"), "BTC": Decimal("0.1")}
        )

        await app.sync_equity()

        assert app.risk_manager.equity == Decimal("3200")

    async def test_bybit_with_no_usdt_balance_defaults_to_zero(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            provider="bybit", with_binance=False, with_bybit=True,
            trading_env=TradingEnvironment.TESTNET,
        )
        app = Application(settings)

        from unittest.mock import AsyncMock

        app.rest_client.get_account_balance = AsyncMock(return_value={})

        await app.sync_equity()

        assert app.risk_manager.equity == Decimal("0")


class TestSyncEquityPaperMode:
    """
    In paper mode (settings.is_paper), sync_equity must read
    PaperExchangeClient.equity directly and must never touch
    account_service or the real rest_client's balance endpoint — there is
    no live account to bypass BinanceAccountService for.
    """

    async def test_reads_paper_exchange_client_equity(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            provider="binance", with_binance=True,
            trading_env=TradingEnvironment.PAPER, paper_initial_equity=12345.0,
        )
        app = Application(settings)
        assert app.paper_exchange_client is not None

        await app.sync_equity()

        assert app.risk_manager.equity == Decimal("12345.0")

    async def test_never_calls_account_service_in_paper_mode(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            provider="binance", with_binance=True, trading_env=TradingEnvironment.PAPER,
        )
        app = Application(settings)

        from unittest.mock import AsyncMock

        app.account_service.get_equity = AsyncMock(
            side_effect=AssertionError("must not be called in paper mode")
        )

        await app.sync_equity()  # must not raise

    async def test_reflects_simulated_pnl_after_a_fill(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(
            provider="binance", with_binance=True, trading_env=TradingEnvironment.PAPER,
        )
        app = Application(settings)
        starting_equity = app.paper_exchange_client.equity

        app.paper_exchange_client.equity -= Decimal("50")  # simulate a fee/loss
        await app.sync_equity()

        assert app.risk_manager.equity == starting_equity - Decimal("50")


class TestGetInstrument:
    def test_raises_key_error_before_instruments_loaded(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        with pytest.raises(KeyError, match="Run initialize_instruments"):
            app.get_instrument("BTCUSDT")

    def test_looks_up_case_insensitively(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        sentinel = object()
        app.instruments["BTCUSDT"] = sentinel
        assert app.get_instrument("btcusdt") is sentinel


class TestDashboardProviders:
    def test_health_provider_reflects_watchdog_state(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        health = app._dashboard_health()  # noqa: SLF001
        assert health["status"] == "degraded"  # watchdog never pinged yet
        assert health["trading_env"] == settings.trading_env.value

    def test_health_provider_reports_ok_after_a_ping(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        app.watchdog.ping()
        health = app._dashboard_health()  # noqa: SLF001
        assert health["status"] == "ok"

    def test_status_provider_reports_current_equity(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        app.risk_manager.update_equity(Decimal("9999"))
        status = app._dashboard_status()  # noqa: SLF001
        assert status["equity"] == str(Decimal("9999"))

    def test_metrics_provider_includes_execution_and_risk_stats(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = Application(settings)
        metrics = app._dashboard_metrics()  # noqa: SLF001
        assert "execution" in metrics
        assert "risk" in metrics


class TestCreateApplication:
    def test_returns_an_application_instance(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings()
        app = create_application(settings)
        assert isinstance(app, Application)

    def test_configures_logging_before_building(self, make_settings, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
        calls: list[str] = []
        monkeypatch.setattr(
            "src.bootstrap.app.configure_logging",
            lambda **kwargs: calls.append("logging_configured"),
        )
        settings = make_settings()
        create_application(settings)
        assert calls == ["logging_configured"]

    def test_redis_event_bus_backend_swaps_the_global_event_bus(
        self, make_settings
    ) -> None:  # type: ignore[no-untyped-def]
        from src.core.events import get_event_bus, reset_event_bus
        from src.core.events_redis import RedisEventBus

        settings = make_settings(event_bus_backend="redis")
        try:
            create_application(settings)
            assert isinstance(get_event_bus(), RedisEventBus)
        finally:
            reset_event_bus()

    def test_memory_backend_leaves_default_event_bus(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        from src.core.events import EventBus, get_event_bus, reset_event_bus

        reset_event_bus()
        settings = make_settings(event_bus_backend="memory")
        try:
            create_application(settings)
            bus = get_event_bus()
            assert type(bus) is EventBus
        finally:
            reset_event_bus()
