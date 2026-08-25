"""Unit tests for src.bootstrap.startup.startup().

Uses a duck-typed fake Application (MagicMock/AsyncMock attributes, with
real AlertManager/HealthChecker instances so their internal bookkeeping
behaves correctly) driven by a real, minimal Settings object. This keeps
focus on startup()'s own orchestration -- step ordering, the production
equity guard, the binance-only listen-key path, and the alert/notifier
event-bus wiring -- without needing live exchange/DB connectivity.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bootstrap.startup import startup
from src.core.enums import AlertSeverity, PositionSide, TradingEnvironment
from src.core.events import (
    AlertFiredEvent,
    ComponentHealthChangedEvent,
    EventBus,
    PositionClosedEvent,
    SystemShutdownEvent,
    SystemStartedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.core.exceptions import InitializationError
from src.monitoring.alerts import AlertManager
from src.monitoring.health import HealthChecker


@pytest.fixture
def isolated_event_bus():  # type: ignore[no-untyped-def]
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


@pytest.fixture(autouse=True)
def _no_real_prometheus_server(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    # Settings default to enable_prometheus=True; without this, tests that
    # don't specifically exercise the Prometheus toggle would try to bind
    # a real HTTP server on the configured port as a side effect.
    monkeypatch.setattr("src.monitoring.metrics.start_metrics_server", MagicMock())


def _fake_app(
    settings,
    *,
    exchange_provider: str = "binance",
    equity: Decimal = Decimal("10000"),
    paper_exchange_client=None,
):  # type: ignore[no-untyped-def]
    app = MagicMock()
    app.settings = settings
    app.exchange_provider = exchange_provider
    # Explicit default: None means "live wiring" (listen key / user-data
    # stream path), matching Application.paper_exchange_client when
    # settings.is_paper is False. Tests exercising the actual paper-mode
    # branch pass an explicit mock via the paper_exchange_client kwarg —
    # see TestPaperExchangeClientWiring. Kept independent of trading_env
    # here so TestEnvironmentExchangeCrossCheck / TestProductionEquityGuard
    # can exercise those checks without also pulling in paper-client
    # wiring specifics.
    app.paper_exchange_client = paper_exchange_client

    app.db.connect = AsyncMock()
    app.db.create_tables = AsyncMock()
    app.db.is_connected = True

    app.rest_client.connect = AsyncMock()
    app.rest_client.create_listen_key = AsyncMock(return_value="listen-key-123")
    app.rest_client.get_server_time = AsyncMock(return_value=1234567890)

    app.initialize_instruments = AsyncMock()
    app.apply_symbol_leverage = AsyncMock()
    app.sync_equity = AsyncMock()

    app.risk_manager.equity = equity
    app.risk_manager.daily_pnl = Decimal("0")
    app.risk_manager.start = AsyncMock()
    app.risk_manager._circuit_breaker = MagicMock(is_open=False, open_reason="")
    app.risk_manager._drawdown = MagicMock(current_drawdown_pct=0.0)

    app.ws_client.start = AsyncMock()
    app.ws_client.subscribe_user_data = MagicMock()
    app.ws_client.set_renewal_callback = MagicMock()

    app.execution_engine.start = AsyncMock()

    app.scheduler.schedule = MagicMock()
    app.scheduler.start = MagicMock()

    app.dashboard.start = MagicMock()
    app.dashboard.start_ws = AsyncMock()

    app.heartbeat.start = AsyncMock()
    app.watchdog.start = MagicMock()
    app.metrics_collector.start = AsyncMock()

    app.notifier.send = AsyncMock()
    app.notifier.send_trade = AsyncMock()

    app.alert_manager = AlertManager()
    app.health_checker = HealthChecker()

    app.check_portfolio_rebalance = AsyncMock()
    return app


def _make_settings(
    make_settings, *, trading_env=TradingEnvironment.PAPER, exchange_provider="binance"
):  # type: ignore[no-untyped-def]
    with_bybit = exchange_provider == "bybit"
    settings = make_settings(
        provider=exchange_provider,
        with_binance=exchange_provider == "binance",
        with_bybit=with_bybit,
        trading_env=trading_env,
    )
    return settings


class TestHappyPath:
    async def test_full_sequence_calls_every_step(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app)

        app.db.connect.assert_awaited_once()
        app.db.create_tables.assert_awaited_once()
        app.rest_client.connect.assert_awaited_once()
        app.initialize_instruments.assert_awaited_once()
        app.apply_symbol_leverage.assert_awaited_once()
        app.sync_equity.assert_awaited_once()
        app.ws_client.start.assert_awaited_once()
        app.execution_engine.start.assert_awaited_once()
        app.risk_manager.start.assert_awaited_once()
        app.scheduler.start.assert_called_once()
        app.heartbeat.start.assert_awaited_once()
        app.watchdog.start.assert_called_once()

    async def test_schedules_all_documented_periodic_tasks(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app)

        scheduled_names = {c.args[0] for c in app.scheduler.schedule.call_args_list}
        assert scheduled_names == {
            "equity_resync",
            "metrics_snapshot",
            "portfolio_risk_check",
            "portfolio_rebalance_check",
            "alert_eval",
            "health_check",
        }

    async def test_publishes_system_started_event_last(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        received: list[SystemStartedEvent] = []

        async def _capture(event: object) -> None:
            if isinstance(event, SystemStartedEvent):
                received.append(event)

        isolated_event_bus.subscribe_all(_capture)
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app)

        assert len(received) == 1
        assert received[0].trading_env == "paper"

    async def test_registers_database_and_exchange_health_checks(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app)

        assert "database" in app.health_checker._checks
        assert "exchange" in app.health_checker._checks

    async def test_registers_three_standard_alert_rules(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app)

        rule_names = {r.name for r in app.alert_manager._rules}
        assert rule_names == {"circuit_breaker_open", "daily_loss_high", "drawdown_warning"}


class TestStrategyOrchestrator:
    async def test_starts_orchestrator_when_provided(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        orchestrator = MagicMock()
        orchestrator.start = AsyncMock()

        await startup(app, orchestrator)

        orchestrator.start.assert_awaited_once()

    async def test_no_orchestrator_is_a_noop(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)

        await startup(app, None)  # must not raise


class TestPaperExchangeClientWiring:
    """
    When Application.paper_exchange_client is set (settings.is_paper),
    startup() must route around all live-account wiring (listen key,
    user-data-stream subscription) and instead start the paper client and
    bind its simulated fills to FillEngine directly.
    """

    def _paper_client(self):  # type: ignore[no-untyped-def]
        client = MagicMock()
        client.start = AsyncMock()
        client.subscribe_fills = MagicMock()
        return client

    async def test_skips_listen_key_creation(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, exchange_provider="binance")
        paper_client = self._paper_client()
        app = _fake_app(settings, exchange_provider="binance", paper_exchange_client=paper_client)

        await startup(app)

        app.rest_client.create_listen_key.assert_not_called()
        app.ws_client.set_renewal_callback.assert_not_called()

    async def test_skips_user_data_subscription(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        paper_client = self._paper_client()
        app = _fake_app(settings, paper_exchange_client=paper_client)

        await startup(app)

        app.ws_client.subscribe_user_data.assert_not_called()

    async def test_binds_fills_and_starts_paper_client(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        paper_client = self._paper_client()
        app = _fake_app(settings, paper_exchange_client=paper_client)

        await startup(app)

        paper_client.subscribe_fills.assert_called_once_with(
            app.fill_engine.process_execution_report
        )
        paper_client.start.assert_awaited_once()

    async def test_market_data_websocket_still_starts(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        paper_client = self._paper_client()
        app = _fake_app(settings, paper_exchange_client=paper_client)

        await startup(app)

        app.ws_client.start.assert_awaited_once()


class TestListenKeyHandling:
    async def test_binance_creates_and_wires_listen_key(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, exchange_provider="binance")
        app = _fake_app(settings, exchange_provider="binance")

        await startup(app)

        app.rest_client.create_listen_key.assert_awaited_once()
        assert app._listen_key == "listen-key-123"
        assert app.ws_client._listen_key == "listen-key-123"
        app.ws_client.set_renewal_callback.assert_called_once()

    async def test_bybit_never_creates_a_listen_key(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, exchange_provider="bybit")
        app = _fake_app(settings, exchange_provider="bybit")

        await startup(app)

        app.rest_client.create_listen_key.assert_not_called()

    async def test_user_data_stream_is_always_wired_to_fill_engine(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, exchange_provider="bybit")
        app = _fake_app(settings, exchange_provider="bybit")

        await startup(app)

        app.ws_client.subscribe_user_data.assert_called_once_with(
            app.fill_engine.process_execution_report
        )


class TestEnvironmentExchangeCrossCheck:
    """
    trading_env (paper/testnet/production) and the exchange config's
    `testnet` flag are independently settable; startup() must catch a
    dangerous mismatch (non-production env pointed at real mainnet) before
    any network connection is opened.
    """

    async def test_non_production_env_with_testnet_false_refuses_to_start(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = make_settings(
            provider="binance",
            with_binance=False,
            trading_env=TradingEnvironment.PAPER,
        )
        from src.core.config import BinanceConfig

        settings.binance = BinanceConfig(
            api_key="dummy",
            api_secret="dummy",
            testnet=False,
        )
        app = _fake_app(settings)

        with pytest.raises(InitializationError, match="BINANCE_TESTNET"):
            await startup(app)

        app.rest_client.connect.assert_not_awaited()

    async def test_production_env_with_testnet_true_warns_but_proceeds(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PRODUCTION)
        app = _fake_app(settings, equity=Decimal("500"))

        await startup(app)  # must not raise

        app.rest_client.connect.assert_awaited_once()

    async def test_paper_env_with_testnet_true_proceeds(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PAPER)
        app = _fake_app(settings)

        await startup(app)  # must not raise

        app.rest_client.connect.assert_awaited_once()


class TestProductionEquityGuard:
    async def test_live_trading_with_positive_equity_proceeds(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PRODUCTION)
        app = _fake_app(settings, equity=Decimal("500"))

        await startup(app)  # must not raise

        app.ws_client.start.assert_awaited_once()

    async def test_live_trading_with_zero_equity_raises(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PRODUCTION)
        app = _fake_app(settings, equity=Decimal("0"))

        with pytest.raises(InitializationError, match="equity is zero or negative"):
            await startup(app)

    async def test_live_trading_with_negative_equity_raises(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PRODUCTION)
        app = _fake_app(settings, equity=Decimal("-1"))

        with pytest.raises(InitializationError, match="equity is zero or negative"):
            await startup(app)

    async def test_paper_trading_never_checks_equity_sign(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings, trading_env=TradingEnvironment.PAPER)
        app = _fake_app(settings, equity=Decimal("-1"))

        await startup(app)  # must not raise even with negative equity outside prod


class TestMonitoringToggles:
    async def test_dashboard_disabled_skips_dashboard_start(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        settings.monitoring.enable_dashboard = False
        app = _fake_app(settings)

        await startup(app)

        app.dashboard.start.assert_not_called()
        app.dashboard.start_ws.assert_not_awaited()

    async def test_dashboard_enabled_but_ws_disabled_skips_ws_only(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        settings.monitoring.enable_dashboard = True
        settings.monitoring.enable_dashboard_ws = False
        app = _fake_app(settings)

        await startup(app)

        app.dashboard.start.assert_called_once()
        app.dashboard.start_ws.assert_not_awaited()

    async def test_prometheus_disabled_skips_metrics_server(
        self, isolated_event_bus: EventBus, make_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _make_settings(make_settings)
        settings.monitoring.enable_prometheus = False
        app = _fake_app(settings)
        mock_start = MagicMock()
        monkeypatch.setattr("src.monitoring.metrics.start_metrics_server", mock_start)

        await startup(app)

        mock_start.assert_not_called()

    async def test_prometheus_enabled_starts_metrics_server(
        self, isolated_event_bus: EventBus, make_settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _make_settings(make_settings)
        settings.monitoring.enable_prometheus = True
        app = _fake_app(settings)
        mock_start = MagicMock()
        monkeypatch.setattr("src.monitoring.metrics.start_metrics_server", mock_start)

        await startup(app)

        mock_start.assert_called_once_with(settings.monitoring.prometheus_port)


class TestAlertAndNotificationWiring:
    async def test_alert_fired_event_is_forwarded_to_notifier(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            AlertFiredEvent(
                source="test",
                alert_name="daily_loss_high",
                severity=AlertSeverity.WARNING,
                message="Daily loss at 5.0%",
            )
        )

        app.notifier.send.assert_awaited_once_with(
            "[WARNING] daily_loss_high: Daily loss at 5.0%", AlertSeverity.WARNING
        )

    async def test_position_closed_event_triggers_trade_notification(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            PositionClosedEvent(
                source="test",
                position_id="pos-1",
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                entry_price=Decimal("50000"),
                exit_price=Decimal("51000"),
                quantity=Decimal("0.1"),
                realized_pnl=Decimal("100"),
                fees=Decimal("2"),
            )
        )

        app.notifier.send_trade.assert_awaited_once()
        payload = app.notifier.send_trade.call_args.args[0]
        assert payload["symbol"] == "BTCUSDT"
        assert payload["net_pnl"] == str(Decimal("100") - Decimal("2"))

    async def test_non_alert_events_are_ignored_by_the_alert_forwarder(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)
        app.notifier.send.reset_mock()

        await isolated_event_bus.publish(SystemStartedEvent(trading_env="paper", version="x"))

        app.notifier.send.assert_not_awaited()


class TestComponentHealthEventWiring:
    """ComponentHealthChangedEvent had a producer (the scheduled health-check
    task) but no subscriber anywhere in src/ before this pass -- a DB or
    exchange connectivity regression would flip health_checker's status and
    publish the event, then it evaporated on the bus with nobody paged.
    These confirm it's routed through the existing AlertFiredEvent ->
    Notifier.send() pipeline, reusing the severity mapping and message
    format startup() builds.
    """

    async def test_unhealthy_transition_pages_as_critical(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            ComponentHealthChangedEvent(
                source="test",
                component="database",
                previous_status="healthy",
                new_status="unhealthy",
                detail="Database not connected",
            )
        )

        app.notifier.send.assert_awaited_once_with(
            "[CRITICAL] component_health_database: database: healthy → unhealthy "
            "(Database not connected)",
            AlertSeverity.CRITICAL,
        )

    async def test_degraded_transition_pages_as_warning(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            ComponentHealthChangedEvent(
                source="test",
                component="exchange",
                previous_status="healthy",
                new_status="degraded",
            )
        )

        app.notifier.send.assert_awaited_once_with(
            "[WARNING] component_health_exchange: exchange: healthy → degraded",
            AlertSeverity.WARNING,
        )

    async def test_recovery_transition_pages_as_info(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            ComponentHealthChangedEvent(
                source="test",
                component="database",
                previous_status="unhealthy",
                new_status="healthy",
            )
        )

        app.notifier.send.assert_awaited_once_with(
            "[INFO] component_health_database: database: unhealthy → healthy",
            AlertSeverity.INFO,
        )

    async def test_detail_omitted_from_message_when_blank(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            ComponentHealthChangedEvent(
                source="test",
                component="database",
                previous_status="healthy",
                new_status="unhealthy",
            )
        )

        message = app.notifier.send.call_args.args[0]
        assert "(" not in message


class TestSystemLifecycleEventWiring:
    """SystemStartedEvent was imported in startup.py (for its own step-10
    publish call) but never subscribed to; SystemShutdownEvent had the
    same producer-with-no-subscriber gap. Both now route through the
    same AlertFiredEvent -> Notifier.send() pipeline as
    ComponentHealthChangedEvent above, and SystemStartedEvent also
    populates the system_info Prometheus Info metric (declared in
    metrics.py but never previously set).
    """

    async def test_system_started_pages_as_info_and_sets_system_info_metric(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            SystemStartedEvent(source="test", trading_env="paper", version="1.3.3")
        )

        app.notifier.send.assert_awaited_once_with(
            "[INFO] system_started: Trading system started (env=paper, version=1.3.3)",
            AlertSeverity.INFO,
        )

        from src.monitoring.metrics import system_info

        # .collect() is the stable public API for reading a metric's
        # current value; avoids depending on prometheus_client's private
        # internal attribute layout for the Info type.
        samples = list(system_info.collect()[0].samples)
        assert samples[0].labels == {"version": "1.3.3", "trading_env": "paper"}

    async def test_system_shutdown_pages_as_warning(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            SystemShutdownEvent(source="test", reason="operator requested")
        )

        app.notifier.send.assert_awaited_once_with(
            "[WARNING] system_shutdown: Trading system shutdown starting: operator requested",
            AlertSeverity.WARNING,
        )


class TestOrderStatusChangedAuditTrail:
    """OrderStatusChangedEvent had a producer (OrderManager) but zero
    subscribers -- every order-lifecycle transition evaporated on the
    bus. This is a structured log line, not an alert: it fires on every
    single order (including the normal NEW -> FILLED path), so routing
    it through Notifier would be alert spam.
    """

    async def test_status_change_is_logged(
        self, isolated_event_bus: EventBus, make_settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        from src.core.enums import OrderStatus
        from src.core.events import OrderStatusChangedEvent

        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        with caplog.at_level("INFO"):
            await isolated_event_bus.publish(
                OrderStatusChangedEvent(
                    source="test",
                    order_id="o-1",
                    symbol="BTCUSDT",
                    previous_status=OrderStatus.SUBMITTED,
                    new_status=OrderStatus.OPEN,
                )
            )

        assert any(
            "order_status_changed" in r.message and "o-1" in r.message for r in caplog.records
        )


class TestSignalExpiredAuditTrail:
    """SignalExpiredEvent had two producers (orchestrator.py at
    generation-time expiry, execution_engine.py at in-transit expiry) but
    zero subscribers anywhere -- in-process or via the Redis notifications
    worker. Mirrors OrderStatusChangedAuditTrail: a structured log line,
    not an alert, since expiry is a normal (if worth watching) occurrence
    rather than an incident on its own.
    """

    async def test_signal_expiry_is_logged(
        self, isolated_event_bus: EventBus, make_settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        from src.core.events import SignalExpiredEvent

        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        with caplog.at_level("INFO"):
            await isolated_event_bus.publish(
                SignalExpiredEvent(
                    source="test",
                    symbol="BTCUSDT",
                    signal_id="sig-1",
                    reason="expired_at_generation",
                )
            )

        assert any("signal_expired" in r.message and "sig-1" in r.message for r in caplog.records)


class TestHardLimitRiskEventWiring:
    """DailyLossLimitReachedEvent and RiskLimitBreachedEvent had producers
    (RiskManager.validate_signal(), on the daily-loss and drawdown hard-
    limit branches) but zero subscribers -- the specific reason a hard
    limit tripped never reached on-call beyond the generic circuit-breaker
    state change. Both route through the existing AlertFiredEvent ->
    Notifier.send() pipeline.
    """

    async def test_daily_loss_limit_reached_pages_as_critical(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        from src.core.events import DailyLossLimitReachedEvent

        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            DailyLossLimitReachedEvent(
                source="test",
                daily_pnl=Decimal("-500"),
                limit_pct=Decimal("5"),
            )
        )

        app.notifier.send.assert_awaited_once()
        message, severity = app.notifier.send.call_args.args
        assert severity == AlertSeverity.CRITICAL
        assert "daily_loss_limit_reached" in message
        assert "-500" in message

    async def test_risk_limit_breached_uses_event_severity(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        from src.core.events import RiskLimitBreachedEvent

        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            RiskLimitBreachedEvent(
                source="test",
                limit_name="max_drawdown",
                current_value=12.5,
                limit_value=10.0,
                severity=AlertSeverity.CRITICAL,
            )
        )

        app.notifier.send.assert_awaited_once()
        message, severity = app.notifier.send.call_args.args
        assert severity == AlertSeverity.CRITICAL
        assert "risk_limit_breached_max_drawdown" in message
        assert "12.5" in message and "10.0" in message

    async def test_risk_limit_breached_respects_non_critical_severity(
        self, isolated_event_bus: EventBus, make_settings
    ) -> None:
        from src.core.events import RiskLimitBreachedEvent

        settings = _make_settings(make_settings)
        app = _fake_app(settings)
        await startup(app)

        await isolated_event_bus.publish(
            RiskLimitBreachedEvent(
                source="test",
                limit_name="max_leverage",
                current_value=5.0,
                limit_value=3.0,
                severity=AlertSeverity.WARNING,
            )
        )

        app.notifier.send.assert_awaited_once()
        _, severity = app.notifier.send.call_args.args
        assert severity == AlertSeverity.WARNING
