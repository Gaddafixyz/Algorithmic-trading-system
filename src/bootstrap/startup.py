"""
System startup sequence.

Executes all startup steps in dependency order:
1. Connect to database
2. Run migrations
3. Connect to exchange REST
4. Load instruments
5. Apply per-symbol leverage
6. Sync account equity
7. Start WebSocket
8. Start market data feeds
9. Start execution engine
10. Start monitoring
11. Start strategies
"""

from __future__ import annotations

import asyncio
import logging

from src.bootstrap.app import Application
from src.core.constants import (
    ALERT_EVAL_INTERVAL_SECONDS,
    EQUITY_SYNC_INTERVAL_SECONDS,
    METRIC_SNAPSHOT_INTERVAL_SECONDS,
    PORTFOLIO_RISK_CHECK_INTERVAL_SECONDS,
    REBALANCE_CHECK_INTERVAL_SECONDS,
)
from src.core.exceptions import InitializationError

logger = logging.getLogger(__name__)


async def startup(app: Application, strategy_orchestrator: object | None = None) -> None:
    """
    Execute the full startup sequence.

    Args:
        app: Fully constructed Application instance.
        strategy_orchestrator: Optional pre-configured orchestrator.
    """
    settings = app.settings
    logger.info("=== Trading System Startup [%s] ===", settings.trading_env.value)

    # 0. Environment/exchange safety cross-check — must run before any
    # network connection is opened.
    #
    # trading_env (paper/testnet/production) and the exchange config's
    # `testnet` flag (BINANCE_TESTNET / BYBIT_TESTNET) are two
    # independently-settable knobs with no prior cross-validation between
    # them. An operator could set TRADING_ENV=paper expecting no real
    # capital at risk while testnet=False was left set from a prior
    # deployment (e.g. a shared .env or systemd unit) — the system would
    # then silently place real orders with real funds under a nominally
    # safe label, with nothing anywhere to catch the mismatch.
    exchange_cfg = settings.binance if app.exchange_provider == "binance" else settings.bybit
    if exchange_cfg is not None:
        env_var = f"{app.exchange_provider.upper()}_TESTNET"
        if not settings.is_production and not exchange_cfg.testnet:
            raise InitializationError(
                component="environment/exchange safety cross-check",
                reason=(
                    f"trading_env={settings.trading_env.value!r} but "
                    f"{env_var}=false — refusing to start. Non-production "
                    f"environments must run against exchange testnet endpoints: "
                    f"set {env_var}=true, or trading_env=production if real "
                    f"trading is actually intended."
                ),
            )
        if settings.is_production and exchange_cfg.testnet:
            logger.warning(
                "trading_env=production but %s=true — this deployment is "
                "connected to the exchange TESTNET and will not place real "
                "orders. If live trading is intended, set %s=false.",
                env_var,
                env_var,
            )

    # 1. Database
    await app.db.connect()
    if app.settings.database.url.startswith("postgresql"):
        # run_migrations() existed but was never called — the docstring's
        # own "should be called during system startup before any DB
        # operations" was aspirational, not actually true. create_tables()
        # (ORM-driven CREATE TABLE IF NOT EXISTS) was doing duty for it
        # instead, which only ever gets a *fresh* database to the current
        # ORM model shape; it can't apply an incremental Alembic revision
        # to an EXISTING database (a new column/table/index added since
        # the last deploy just silently never appears), and it doesn't
        # touch alembic_version, so Alembic's own view of schema state
        # drifts from reality the first time this matters.
        #
        # run_migrations() is a sync function whose Alembic env.py calls
        # asyncio.run() internally (needed for its own asyncpg engine) —
        # calling it directly here, inside this already-running event
        # loop, would raise "asyncio.run() cannot be called from a
        # running event loop". Routed through a thread via
        # asyncio.to_thread() so Alembic gets a clean loop of its own.
        from src.persistence.migrations import run_migrations

        await asyncio.to_thread(run_migrations, app.settings.database.url)
        logger.info("✓ Database migrations applied")
    else:
        # SQLite (local dev/testing only — see DatabaseConfig.
        # validate_database_url) isn't run through Alembic here: its
        # ALTER TABLE support is limited enough that Alembic normally
        # needs batch mode for it, which this repo's env.py doesn't
        # configure. ORM-driven table creation is what migrations.py's
        # own create_all_tables()/create_tables() docstrings intend for
        # this case, so kept as the SQLite path.
        await app.db.create_tables()
        logger.info("✓ Database tables created (SQLite dev mode)")
    logger.info("✓ Database connected")

    # 2. Exchange REST
    await app.rest_client.connect()
    logger.info("✓ Exchange REST connected")

    # 3. Instruments
    await app.initialize_instruments()
    logger.info("✓ Instruments loaded")

    # 3b. Per-symbol leverage (configs/symbols.yaml) — must run before any
    # order can be placed, since RiskManager's pre-trade liquidation/margin
    # checks assume this leverage is what's actually live on the exchange.
    await app.apply_symbol_leverage()
    logger.info("✓ Symbol leverage applied")

    # 4. Account equity
    await app.sync_equity()
    logger.info("✓ Equity synced: %s USDT", app.risk_manager.equity)

    # 5. Production guard
    if settings.is_production:
        logger.warning("⚠️  LIVE TRADING MODE — real capital at risk")
        equity = app.risk_manager.equity
        if equity <= 0:
            raise InitializationError(
                component="production equity guard",
                reason="Cannot start live trading: equity is zero or negative",
            )

    # 6. User-data stream + WebSocket
    #
    # Binance authenticates the user-data stream with a listen key (a REST-
    # issued token that must be renewed every ~30 min); Bybit's private WS
    # authenticates per-connection with a signed HMAC frame using the
    # api_key/api_secret already passed to BybitWebSocket at construction,
    # so it needs none of the listen-key machinery below.
    #
    # Paper mode skips this block entirely: there is no real account to
    # authenticate against, and PaperExchangeClient delivers its own
    # simulated fills directly to FillEngine (see below) instead of via a
    # user-data WebSocket stream.
    if app.paper_exchange_client is None:
        if app.exchange_provider == "binance":
            listen_key = await app.rest_client.create_listen_key()
            app._listen_key = listen_key  # stored for clean teardown on shutdown

            # Inject key into WebSocket client and register the renewal callback
            app.ws_client._listen_key = listen_key

            async def _renew_listen_key() -> str:
                await app.rest_client.renew_listen_key(listen_key)
                return listen_key  # Binance renews in-place; key string stays the same

            app.ws_client.set_renewal_callback(_renew_listen_key)
            logger.info("✓ Listen key created (%s...)", listen_key[:8])

        # Wire the user-data stream to FillEngine — this is the only path by
        # which exchange fill notifications ever become OrderFilledEvents.
        # Without this registration entries would be submitted and marked OPEN
        # and then wait forever, and protective stop-loss/take-profit orders
        # would never be placed even after the entry actually executes on the
        # exchange. This call is provider-agnostic: both WS clients normalize
        # to the same execution-report shape (see adapter parsers).
        app.ws_client.subscribe_user_data(app.fill_engine.process_execution_report)

        await app.ws_client.start()
        logger.info("✓ WebSocket connected (%s)", app.exchange_provider)
    else:
        # Market data still flows over the real ws_client (candles, mark
        # price, etc. — PaperExchangeClient delegates reads to it), but no
        # user-data stream/listen-key is needed for a simulated account.
        # PaperExchangeClient.start() subscribes to mark-price updates for
        # every paper-traded symbol and delivers simulated fills straight
        # to FillEngine via the same normalized execution-report shape the
        # live user-data stream would have used.
        app.paper_exchange_client.subscribe_fills(app.fill_engine.process_execution_report)
        await app.ws_client.start()
        await app.paper_exchange_client.start()
        logger.info(
            "✓ Paper exchange client started (simulated fills, live %s market data)",
            app.exchange_provider,
        )

    # 7. Execution engine
    await app.execution_engine.start()
    logger.info("✓ Execution engine started")

    # 7b. Risk manager (subscribes to PositionClosedEvent for daily P&L)
    await app.risk_manager.start()
    logger.info("✓ Risk manager started")

    # 7c. Background scheduler — periodic equity resync via
    # BinanceAccountService.get_equity(). RiskManager's position-sizing
    # and drawdown checks key off risk_manager.equity, which is otherwise
    # only ever set once at startup (step 4) and would silently go stale
    # across deposits, withdrawals, or funding payments for the life of
    # the process.
    app.scheduler.schedule(
        "equity_resync",
        app.sync_equity,
        interval_seconds=EQUITY_SYNC_INTERVAL_SECONDS,
    )
    # Periodic MetricSnapshotORM persistence — see
    # Application.record_metrics_snapshot for why this exists (the
    # repository/model previously had no caller anywhere in the live
    # path). Scheduled rather than run inline anywhere else so a slow or
    # failing DB write can never block the trading path.
    app.scheduler.schedule(
        "metrics_snapshot",
        app.record_metrics_snapshot,
        interval_seconds=METRIC_SNAPSHOT_INTERVAL_SECONDS,
    )
    # Periodic portfolio-wide gross exposure check. Pre-trade checks in
    # RiskManager.calculate_position_size only run when a *new* signal is
    # being sized — a book can still drift over the gross exposure ceiling
    # purely from price moves on existing positions, with no new trade to
    # catch it. check_portfolio_risk() (previously a near-stub that only
    # checked the circuit breaker) now also enforces ExposureTracker's
    # gross exposure limit and trips the breaker on breach.
    app.scheduler.schedule(
        "portfolio_risk_check",
        app.risk_manager.check_portfolio_risk,
        interval_seconds=PORTFOLIO_RISK_CHECK_INTERVAL_SECONDS,
    )
    # Periodic allocation-drift check — see Application.check_portfolio_rebalance
    # for why this exists (PortfolioService/PortfolioAllocator/PortfolioRebalancer
    # previously had no caller anywhere in the live path). Detection-only: logs
    # drift beyond threshold, never resizes or places an order itself.
    app.scheduler.schedule(
        "portfolio_rebalance_check",
        app.check_portfolio_rebalance,
        interval_seconds=REBALANCE_CHECK_INTERVAL_SECONDS,
    )
    app.scheduler.start()
    logger.info(
        "✓ Scheduler started (equity resync every %.0fs, metrics snapshot every %.0fs)",
        EQUITY_SYNC_INTERVAL_SECONDS,
        METRIC_SNAPSHOT_INTERVAL_SECONDS,
    )

    # 8. Monitoring
    if settings.monitoring.enable_prometheus:
        from src.monitoring.metrics import start_metrics_server

        start_metrics_server(settings.monitoring.prometheus_port)
        logger.info("✓ Prometheus metrics on port %d", settings.monitoring.prometheus_port)

    if settings.monitoring.enable_dashboard:
        app.dashboard.start()
        if settings.monitoring.enable_dashboard_ws:
            await app.dashboard.start_ws()
        logger.info(
            "✓ Dashboard started (http :%d, ws :%d)",
            settings.monitoring.dashboard_port,
            settings.monitoring.dashboard_ws_port,
        )

    await app.heartbeat.start()
    app.watchdog.start()
    # MetricsCollector subscribes to the event bus regardless of whether
    # enable_prometheus is set — the subscription itself is cheap, and
    # gating it on enable_prometheus would just mean flipping that flag
    # mid-process (e.g. a hot-reloaded config) starts from a cold metrics
    # state instead of one that was tracking all along.
    await app.metrics_collector.start()
    logger.info("✓ Monitoring started")

    # 8b. Alert + notification wiring ──────────────────────────────────────
    #
    # Three previously-dead paths are wired here:
    #
    # 1. AlertFiredEvent → Notifier.send(): every CRITICAL/WARNING event
    #    published anywhere in the system (ExecutionEngine, Reconciliation,
    #    AlertManager) was routed to the event bus but had zero subscribers
    #    there — the Notifier existed and had channels configured but was
    #    never connected to the bus. On-call engineers were never paged for
    #    UNPROTECTED_POSITION, orphan_exchange_position, or any other alert.
    #
    # 2. build_standard_alerts() + AlertManager.evaluate() scheduling:
    #    AlertManager.evaluate() was never called; build_standard_alerts()
    #    was never invoked, so the threshold-based rules (circuit_breaker_open,
    #    daily_loss_high, drawdown_warning) were defined but never registered
    #    or polled. Scheduled every ALERT_EVAL_INTERVAL_SECONDS alongside
    #    other periodic tasks.
    #
    # 3. PositionClosedEvent → Notifier.send_trade(): Notifier.send_trade()
    #    and every channel's send_trade_notification() method existed but
    #    were dead code in live trading — no subscriber ever called them.

    from src.core.events import (
        AlertFiredEvent,
        PositionClosedEvent,
        SystemStartedEvent,
        get_event_bus,
    )
    from src.monitoring.alerts import build_standard_alerts

    bus = get_event_bus()

    # 1. Forward every AlertFiredEvent to the Notifier fan-out.
    async def _on_alert_fired(event: object) -> None:
        if not isinstance(event, AlertFiredEvent):
            return
        severity_label = event.severity.value.upper()
        await app.notifier.send(
            f"[{severity_label}] {event.alert_name}: {event.message}",
            event.severity,
        )

    bus.subscribe(AlertFiredEvent, _on_alert_fired)

    # 2. Register standard alert rules and schedule periodic evaluation.
    for rule in build_standard_alerts(
        risk_manager=app.risk_manager,
        circuit_breaker=app.risk_manager._circuit_breaker,
        daily_loss_warning_pct=settings.monitoring.daily_loss_warning_pct,
        drawdown_warning_pct=settings.monitoring.drawdown_warning_pct,
        cooldown_minutes=(
            settings.monitoring.alert_cooldown_minutes
            if "alert_cooldown_minutes" in settings._monitoring_yaml_overrides
            else None
        ),
    ):
        app.alert_manager.add_rule(rule)

    app.scheduler.schedule(
        "alert_eval",
        app.alert_manager.evaluate,
        interval_seconds=ALERT_EVAL_INTERVAL_SECONDS,
    )
    logger.info(
        "✓ Alert rules registered (%d rules, eval every %.0fs)",
        len(app.alert_manager._rules),
        ALERT_EVAL_INTERVAL_SECONDS,
    )

    # 3. Send trade-close notifications on every PositionClosedEvent.
    async def _on_position_closed_notify(event: object) -> None:
        if not isinstance(event, PositionClosedEvent):
            return
        await app.notifier.send_trade(
            {
                "symbol": event.symbol,
                "side": event.side.value if hasattr(event.side, "value") else str(event.side),
                "net_pnl": str(event.realized_pnl - event.fees),
                "entry_price": str(event.entry_price),
                "exit_price": str(event.exit_price),
            }
        )

    bus.subscribe(PositionClosedEvent, _on_position_closed_notify)
    logger.info("✓ Trade-close notifications wired")

    # 4. System lifecycle events → notifications + system_info metric.
    #
    # SystemStartedEvent was already imported above (for the step-10
    # publish call) but never subscribed to. SystemShutdownEvent has the
    # same gap — see shutdown.py step 0's comment ("so a subscriber e.g.
    # a notifier can still reach the network... before those connections
    # are torn down"), which describes exactly the wiring added here but
    # was never actually connected. Both route through the same
    # AlertFiredEvent → Notifier.send() pipeline as #1 above.
    #
    # SystemStartedEvent is also the natural place to finally set
    # system_info (trading_system Info metric) — declared in metrics.py
    # but never populated, unlike every other metric here which updates
    # continuously, this is a set-once-at-startup value.
    from src.core.enums import AlertSeverity
    from src.core.events import SystemShutdownEvent
    from src.monitoring.metrics import system_info

    async def _on_system_started(event: object) -> None:
        if not isinstance(event, SystemStartedEvent):
            return
        try:
            system_info.info({"version": event.version, "trading_env": event.trading_env})
        except Exception:
            logger.exception("Failed to set system_info metric")
        await bus.publish(
            AlertFiredEvent(
                source="system",
                alert_name="system_started",
                severity=AlertSeverity.INFO,
                message=f"Trading system started (env={event.trading_env}, version={event.version})",
            )
        )

    async def _on_system_shutdown(event: object) -> None:
        if not isinstance(event, SystemShutdownEvent):
            return
        await bus.publish(
            AlertFiredEvent(
                source="system",
                alert_name="system_shutdown",
                severity=AlertSeverity.WARNING,
                message=f"Trading system shutdown starting: {event.reason}",
            )
        )

    bus.subscribe(SystemStartedEvent, _on_system_started)
    bus.subscribe(SystemShutdownEvent, _on_system_shutdown)
    logger.info("✓ System lifecycle notifications wired")

    # 5. OrderStatusChangedEvent audit trail. Fires on every order-status
    # transition (see OrderManager._publish_status_change) but previously
    # had zero subscribers — every transition just evaporated on the bus.
    # A structured log line (not an alert — this fires on every single
    # order, including normal NEW→FILLED flow, so routing it through
    # Notifier would be alert spam) gives operators a searchable audit
    # trail without duplicating OrderManager's own DB persistence, which
    # already runs independently of this event.
    from src.core.events import OrderStatusChangedEvent

    async def _on_order_status_changed(event: object) -> None:
        if not isinstance(event, OrderStatusChangedEvent):
            return
        logger.info(
            "order_status_changed order_id=%s symbol=%s %s -> %s",
            event.order_id,
            event.symbol,
            event.previous_status.value,
            event.new_status.value,
        )

    bus.subscribe(OrderStatusChangedEvent, _on_order_status_changed)
    logger.info("✓ Order status audit trail wired")

    # 6. Hard-limit risk events → notifications. DailyLossLimitReachedEvent
    # and RiskLimitBreachedEvent are published at the exact moment
    # RiskManager.validate_signal() rejects a signal for breaching the
    # daily-loss or drawdown hard limit (see risk_manager.py) — both had
    # producers but zero subscribers, so the specific reason a limit
    # tripped never reached on-call beyond the generic circuit-breaker
    # state change. This is distinct from the polling-based
    # daily_loss_high/drawdown_warning AlertRules registered in step 2
    # above: those are early-warning thresholds (checked every
    # ALERT_EVAL_INTERVAL_SECONDS, cooldown-throttled) below the hard
    # limit; these two events fire once, immediately, at the instant the
    # hard limit itself is breached and the circuit breaker trips —
    # complementary, not redundant.
    from src.core.events import DailyLossLimitReachedEvent, RiskLimitBreachedEvent

    async def _on_daily_loss_limit_reached(event: object) -> None:
        if not isinstance(event, DailyLossLimitReachedEvent):
            return
        await bus.publish(
            AlertFiredEvent(
                source="risk_manager",
                alert_name="daily_loss_limit_reached",
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"Daily loss limit reached: daily_pnl={event.daily_pnl} "
                    f"(limit={event.limit_pct}%) — circuit breaker tripped"
                ),
            )
        )

    async def _on_risk_limit_breached(event: object) -> None:
        if not isinstance(event, RiskLimitBreachedEvent):
            return
        await bus.publish(
            AlertFiredEvent(
                source="risk_manager",
                alert_name=f"risk_limit_breached_{event.limit_name}",
                severity=event.severity,
                message=(
                    f"Risk limit '{event.limit_name}' breached: "
                    f"current={event.current_value} limit={event.limit_value}"
                ),
            )
        )

    bus.subscribe(DailyLossLimitReachedEvent, _on_daily_loss_limit_reached)
    bus.subscribe(RiskLimitBreachedEvent, _on_risk_limit_breached)
    logger.info("✓ Hard-limit risk event notifications wired")

    # 7. SignalExpiredEvent audit trail. Published from two sites
    # (orchestrator.py when a signal expires before generation completes,
    # execution_engine.py when a signal goes stale in transit before
    # submission) but previously had zero subscribers anywhere — in-process
    # or via the Redis notifications worker — so every expiry just
    # evaporated on the bus with no visibility. Not routed through
    # Notifier: expiries are a normal (if worth watching) occurrence, not
    # an incident on their own, so this mirrors step 5's
    # OrderStatusChangedEvent treatment — a structured log line for a
    # searchable audit trail, not alert spam. The MetricsCollector handles
    # the aggregate rate (signals_expired counter) separately.
    from src.core.events import SignalExpiredEvent

    async def _on_signal_expired(event: object) -> None:
        if not isinstance(event, SignalExpiredEvent):
            return
        logger.info(
            "signal_expired signal_id=%s symbol=%s reason=%s",
            event.signal_id,
            event.symbol,
            event.reason,
        )

    bus.subscribe(SignalExpiredEvent, _on_signal_expired)
    logger.info("✓ Signal expiry audit trail wired")

    # 8c. Health checks ──────────────────────────────────────────────────
    #
    # HealthChecker was instantiated in Application.__init__ but nothing
    # ever called .register() or scheduled .run_all() — the dashboard's
    # _dashboard_health reads the watchdog directly instead. Registering
    # the standard db/exchange checks here and polling them alongside the
    # other scheduled tasks gives ComponentHealthChangedEvent a real
    # producer and surfaces DB/exchange-connectivity regressions that the
    # watchdog (event-loop liveness only) can't see.
    import functools

    app.health_checker.register(
        "database", functools.partial(app.health_checker.check_database, app.db)
    )
    app.health_checker.register(
        "exchange", functools.partial(app.health_checker.check_exchange, app.rest_client)
    )
    app._last_health_status: dict[str, str] = {}  # type: ignore[attr-defined]

    from src.core.events import ComponentHealthChangedEvent

    async def _run_health_checks() -> None:
        results = await app.health_checker.run_all()
        for name, check in results["checks"].items():
            new_status = check["status"]
            prev_status = app._last_health_status.get(name)  # type: ignore[attr-defined]
            if prev_status != new_status:
                app._last_health_status[name] = new_status  # type: ignore[attr-defined]
                await bus.publish(
                    ComponentHealthChangedEvent(
                        component=name,
                        previous_status=prev_status or "unknown",
                        new_status=new_status,
                        detail=str(check.get("detail", check.get("error", ""))),
                    )
                )

    # ComponentHealthChangedEvent had a producer (above) but no subscriber
    # anywhere in src/ — a DB or exchange connectivity failure would flip
    # health_checker's internal status and publish the event, then the
    # event just evaporated on the bus. Route it through the same
    # AlertFiredEvent → Notifier.send() pipeline wired in step 1 above,
    # so on-call actually gets paged for infra regressions, not just
    # trading-logic ones. Edge-triggered already (see the prev/new_status
    # diff above), so no separate cooldown is needed here.
    async def _on_component_health_changed(event: object) -> None:
        from src.core.enums import AlertSeverity

        if not isinstance(event, ComponentHealthChangedEvent):
            return
        severity = {
            "healthy": AlertSeverity.INFO,
            "degraded": AlertSeverity.WARNING,
            "unhealthy": AlertSeverity.CRITICAL,
        }.get(event.new_status, AlertSeverity.WARNING)
        message = f"{event.component}: {event.previous_status} → {event.new_status}"
        if event.detail:
            message += f" ({event.detail})"
        await bus.publish(
            AlertFiredEvent(
                source="health_checker",
                alert_name=f"component_health_{event.component}",
                severity=severity,
                message=message,
            )
        )

    bus.subscribe(ComponentHealthChangedEvent, _on_component_health_changed)

    app.scheduler.schedule(
        "health_check",
        _run_health_checks,
        interval_seconds=settings.monitoring.health_check_interval_seconds,
    )
    logger.info("✓ Health checks registered (database, exchange)")

    # 9. Strategy orchestrator
    if strategy_orchestrator is not None:
        await strategy_orchestrator.start()  # type: ignore[union-attr]
        logger.info("✓ Strategy orchestrator started")

    # 10. Announce successful startup. SystemStartedEvent existed as
    # schema only — published here, last, so a subscriber that reacts to
    # it (e.g. a "system is up" notification) only fires once every prior
    # step has actually succeeded.
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        app_version = _pkg_version("trading-enhanced-3")
    except (ImportError, PackageNotFoundError):
        app_version = "unknown"

    await bus.publish(
        SystemStartedEvent(trading_env=settings.trading_env.value, version=app_version)
    )

    logger.info("=== Startup complete ===")
