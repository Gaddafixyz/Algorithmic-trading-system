"""
Application factory.

Assembles all system components in dependency order and returns
a fully configured, ready-to-start application object.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.bootstrap.exchange_factory import build_rest_client, build_ws_client
from src.core.config import Settings
from src.core.constants import EQUITY_SYNC_INTERVAL_SECONDS
from src.core.enums import AlertSeverity, TradingEnvironment
from src.core.events import AlertFiredEvent, get_event_bus
from src.core.scheduler import Scheduler
from src.domain.portfolio import Portfolio
from src.exchange.binance.exchange_info import parse_instruments
from src.execution.execution_engine import ExecutionEngine
from src.execution.fill_engine import FillEngine
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager
from src.execution.reconciliation import ReconciliationService
from src.market_data.cache import MarketDataCache
from src.market_data.candles import CandleService
from src.market_data.feeds import MarketDataFeed
from src.monitoring.alerts import AlertManager
from src.monitoring.dashboard import Dashboard
from src.monitoring.health import HealthChecker
from src.monitoring.heartbeat import HeartbeatService
from src.monitoring.metrics import (
    account_daily_pnl,
    account_drawdown_pct,
    account_equity,
    start_metrics_server,
)
from src.monitoring.metrics_collector import MetricsCollector
from src.monitoring.telemetry import configure_logging
from src.monitoring.watchdog import EventLoopWatchdog
from src.notifications.notifier import Notifier
from src.persistence.database import Database
from src.risk.exposure import CorrelationTracker
from src.risk.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class Application:
    """
    Fully assembled trading application.

    All components are wired up and ready to start.
    Call start() to begin trading; stop() for graceful shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        # Set post-construction in main.py once the StrategyOrchestrator
        # exists (it's built after create_application() since it depends
        # on app.candle_service/app.position_manager/app.db). Optional so
        # _dashboard_metrics degrades gracefully for any caller (tests,
        # alternate entrypoints) that never sets it.
        self.orchestrator: Any | None = None

        # Exchange — built via exchange_factory so the concrete provider
        # (Binance or Bybit) is selected once, here, from
        # settings.exchange_provider. Every other layer depends only on
        # the IExchangeClient / IWebSocketStreamClient protocols.
        self.exchange_provider = settings.exchange_provider.lower()
        self.rest_client = build_rest_client(settings)
        # listen_key is populated during startup (create_listen_key) and kept
        # alive by the WebSocket renewal loop; stored here so shutdown can
        # call delete_listen_key for clean server-side teardown. Bybit's
        # private WS auths via signed HMAC frames instead of a listen key,
        # so this stays None for that provider.
        self._listen_key: str | None = None
        self.ws_client = build_ws_client(settings)

        # Trading client — the exchange surface OrderManager/ReconciliationService
        # actually submit orders against. In paper mode this is a
        # PaperExchangeClient that delegates market-data reads to
        # self.rest_client (so paper trading sees genuine live prices) but
        # simulates order placement/fills entirely in-memory, routed through
        # the same RiskManager -> ExecutionEngine -> OrderManager -> FillEngine
        # pipeline live trading uses. self.rest_client itself always remains
        # the real, credentialed client — it's still needed for market data
        # (candles, instruments) even in paper mode.
        self.paper_exchange_client: Any = None
        if settings.is_paper:
            from decimal import Decimal as _Decimal

            from src.paper_trading.paper_exchange_client import PaperExchangeClient

            paper_symbols = [
                symbol for symbol, cfg in settings.symbols.items() if cfg.enabled
            ]
            self.paper_exchange_client = PaperExchangeClient(
                delegate=self.rest_client,
                ws_client=self.ws_client,
                symbols=paper_symbols,
                initial_equity=_Decimal(str(settings.paper_initial_equity)),
            )
            self.trading_client: Any = self.paper_exchange_client
            logger.info(
                "Paper trading mode: orders simulated locally, market data from %s",
                self.exchange_provider,
            )
        else:
            self.trading_client = self.rest_client

        # Market data
        self.cache = MarketDataCache()
        self.candle_service = CandleService(
            rest_client=self.rest_client,
            ws_client=self.ws_client,
            cache=self.cache,
        )
        self.market_feed = MarketDataFeed(
            ws_client=self.ws_client,
            candle_service=self.candle_service,
            cache=self.cache,
        )

        # Domain
        self.portfolio = Portfolio()

        # Account (high-level account data access — balances, positions,
        # equity — aggregated from raw REST responses into domain objects).
        # Binance-only: it exposes a richer /fapi/v2/account endpoint that
        # this service wraps for total-margin-balance equity. Bybit has no
        # equivalent aggregate endpoint, so sync_equity() falls back to
        # get_account_balance() (from IExchangeClient, both providers) for
        # that branch instead — see sync_equity() below.
        self.account_service: Any = None
        if self.exchange_provider == "binance":
            from src.exchange.binance.account import BinanceAccountService

            self.account_service = BinanceAccountService(self.rest_client)

        # Periodic background tasks (equity resync; reconciliation is
        # currently scheduled separately in LifecycleManager)
        self.scheduler = Scheduler()

        # Persistence
        self.db = Database(settings.database)

        # Execution
        self.order_manager = OrderManager(self.trading_client, db=self.db)
        self.position_manager = PositionManager(db=self.db)
        self.fill_engine = FillEngine(self.order_manager)

        # Risk
        symbol_leverage = {
            symbol: cfg.leverage
            for symbol, cfg in settings.symbols.items()
            if cfg.enabled
        }
        # Validated per-symbol leverage/margin-type/max-notional config
        # (src/domain/leverage.py::LeverageConfig), derived from the same
        # settings.symbols source as symbol_leverage above. Previously
        # built but never instantiated anywhere; now feeds both
        # apply_symbol_leverage()'s margin-type push (below) and
        # RiskManager's per-symbol max_notional pre-trade cap.
        leverage_configs = {
            symbol: cfg.to_leverage_config(symbol)
            for symbol, cfg in settings.symbols.items()
            if cfg.enabled
        }
        # Correlation tracker for correlation-adjusted position sizing.
        # Only constructed when 2+ symbols are enabled — with a single
        # symbol there's nothing to correlate against, and CorrelationTracker
        # requires window >= 2 but a >=2-symbol universe to be meaningful.
        correlation_tracker: CorrelationTracker | None = None
        if len(symbol_leverage) >= 2:
            correlation_tracker = CorrelationTracker(
                symbols=list(symbol_leverage.keys()),
                window=settings.risk.correlation_window,
            )

        self.risk_manager = RiskManager(
            config=settings.risk,
            portfolio=self.portfolio,
            order_manager=self.order_manager,
            symbol_leverage=symbol_leverage,
            correlation_tracker=correlation_tracker,
            leverage_configs=leverage_configs,
        )

        # Portfolio allocation/rebalance service. PortfolioService (and the
        # PortfolioAllocator/PortfolioRebalancer it wraps) existed but had
        # no caller anywhere in the live path — see
        # check_portfolio_rebalance() below. Constraint parameters mirror
        # the equivalent RiskConfig fields so allocation targets can never
        # imply a position size RiskManager's own pre-trade checks would
        # reject (see PortfolioAllocator docstring).
        from src.portfolio.allocator import PortfolioAllocator
        from src.portfolio.rebalancer import PortfolioRebalancer
        from src.portfolio.service import PortfolioService

        self.portfolio_service = PortfolioService(
            portfolio=self.portfolio,
            allocator=PortfolioAllocator(
                max_position_pct_of_equity=settings.risk.max_position_pct_of_equity,
                max_open_positions=settings.risk.max_open_positions,
            ),
            rebalancer=PortfolioRebalancer(),
        )
        self._rebalance_symbols = list(symbol_leverage.keys())
        self._last_rebalance_actions: list[Any] = []

        # Instruments (populated at startup)
        self.instruments: dict[str, Any] = {}

        self.execution_engine = ExecutionEngine(
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            instruments=self.instruments,
            partial_tp_levels=self._load_partial_tp_levels(),
            track_funding_pnl=self._load_track_funding_pnl(),
        )

        self.reconciliation = ReconciliationService(
            exchange=self.trading_client,
            position_manager=self.position_manager,
            order_manager=self.order_manager,
        )

        # Monitoring
        # MetricsCollector: subscribes to the event bus and wires the
        # event-driven subset of the 19 Prometheus metrics declared in
        # metrics.py (signals/orders/fills/cancels/position-lifecycle/
        # circuit-breaker/orderbook-spread) — see
        # src/monitoring/metrics_collector.py.
        self.metrics_collector = MetricsCollector()
        self.health_checker = HealthChecker()
        self.heartbeat = HeartbeatService(
            interval_seconds=settings.monitoring.heartbeat_interval_seconds,
        )
        self.alert_manager = AlertManager()
        self.watchdog = EventLoopWatchdog(
            timeout_seconds=settings.monitoring.watchdog_timeout_seconds,
            check_interval=settings.monitoring.watchdog_check_interval_seconds,
            on_timeout=self._on_watchdog_timeout,
        )

        self.dashboard = Dashboard(
            port=settings.monitoring.dashboard_port,
            ws_port=settings.monitoring.dashboard_ws_port,
        )
        self.dashboard.set_providers(
            health_fn=self._dashboard_health,
            status_fn=self._dashboard_status,
            metrics_fn=self._dashboard_metrics,
        )

        # Notifications
        self.notifier = Notifier()
        self._setup_notifications(settings)

    def _load_partial_tp_levels(self) -> list[tuple[Decimal, Decimal]] | None:
        """
        Load the partial take-profit ladder from configs/strategy.yaml.

        Mirrors main.py's build_strategy()/_build_signal_filter() pattern
        of reading strategy.yaml directly (it isn't merged into Settings
        the way risk.yaml/monitoring.yaml are — see _load_risk_yaml).
        Returns None (disabling the feature, ExecutionEngine's existing
        default behavior) if the file is missing, the section is absent,
        or partial_take_profit.enabled is not true, so no yaml change is
        required for this to be a no-op.
        """
        from pathlib import Path

        import yaml

        config_path = Path("configs/strategy.yaml")
        if not config_path.exists():
            return None
        try:
            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to read configs/strategy.yaml for partial TP config: %s", exc)
            return None

        ptp_cfg = cfg.get("partial_take_profit", {}) or {}
        if not ptp_cfg.get("enabled", False):
            return None

        levels_raw = ptp_cfg.get("levels", []) or []
        levels: list[tuple[Decimal, Decimal]] = []
        for entry in levels_raw:
            try:
                rr, fraction = entry
                levels.append((Decimal(str(rr)), Decimal(str(fraction))))
            except (TypeError, ValueError) as exc:
                logger.warning("Skipping malformed partial_take_profit level %r: %s", entry, exc)

        if not levels:
            logger.warning(
                "partial_take_profit.enabled=true but no valid levels configured — "
                "running WITHOUT partial take-profit."
            )
            return None

        logger.info("Partial take-profit enabled: %s", levels)
        return levels

    def _load_track_funding_pnl(self) -> bool:
        """
        Load execution.track_funding_pnl from configs/strategy.yaml.

        Same direct-yaml-read pattern as _load_partial_tp_levels (this
        section isn't merged into Settings). Returns False (unchanged
        behavior — no FundingRateEvent subscription, funding_pnl stays 0)
        if the file, section, or key is missing, so no yaml change is
        required for this to be a no-op.
        """
        from pathlib import Path

        import yaml

        config_path = Path("configs/strategy.yaml")
        if not config_path.exists():
            return False
        try:
            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Failed to read configs/strategy.yaml for execution config: %s", exc)
            return False

        exec_cfg = cfg.get("execution", {}) or {}
        enabled = exec_cfg.get("track_funding_pnl", False)
        if not isinstance(enabled, bool):
            logger.warning(
                "Invalid execution.track_funding_pnl in strategy.yaml: %r — using False.",
                enabled,
            )
            return False

        if enabled:
            logger.info("Funding PnL tracking enabled (execution.track_funding_pnl)")
        return enabled

    def _setup_notifications(self, settings: Settings) -> None:
        """Wire up configured notification channels."""
        n = settings.notifications

        if n.telegram_bot_token and n.telegram_chat_id:
            from src.notifications.telegram import TelegramNotifier
            self.notifier.add_channel(TelegramNotifier(
                bot_token=n.telegram_bot_token.get_secret_value(),
                chat_id=n.telegram_chat_id,
            ))
            logger.info("Telegram notifications enabled")

        if n.discord_webhook_url:
            from src.notifications.discord import DiscordNotifier
            self.notifier.add_channel(DiscordNotifier(n.discord_webhook_url.get_secret_value()))
            logger.info("Discord notifications enabled")

        if n.slack_webhook_url:
            from src.notifications.slack import SlackNotifier
            self.notifier.add_channel(SlackNotifier(n.slack_webhook_url.get_secret_value()))
            logger.info("Slack notifications enabled")

        if n.email_enabled:
            from src.notifications.email import EmailNotifier
            self.notifier.add_channel(EmailNotifier(
                smtp_host=n.smtp_host,  # type: ignore[arg-type]
                smtp_port=n.smtp_port,
                username=n.smtp_user,  # type: ignore[arg-type]
                password=n.smtp_password.get_secret_value(),  # type: ignore[union-attr]
                recipient=n.alert_email,  # type: ignore[arg-type]
            ))
            logger.info("Email notifications enabled (%s)", n.alert_email)

    async def initialize_instruments(self) -> None:
        """Load instrument/symbol rules from exchange."""
        info = await self.rest_client.get_exchange_info()
        self.instruments.update(parse_instruments(info))
        logger.info("Loaded %d instruments from exchange", len(self.instruments))

    async def apply_symbol_leverage(self) -> None:
        """
        Push configs/symbols.yaml's per-symbol leverage AND margin type to
        the exchange.

        Without the leverage call, symbols.yaml's leverage values are
        decorative — the exchange keeps running whatever leverage was last
        set manually (or its account default), which can silently diverge
        from what RiskManager's pre-trade checks assume for that symbol.

        set_margin_type() had exactly the same problem one level further:
        it was fully implemented on every IExchangeClient (Binance, Bybit,
        paper trading) and individually unit-tested, but never called from
        here or anywhere else in the live path — so symbols.yaml's
        margin_type was equally decorative. A symbol configured for
        ISOLATED margin but left on the account's CROSS default (or
        vice versa) changes real liquidation behavior without anything in
        the system knowing. Routed through LeverageConfig so both calls
        use the same validated, normalized (uppercased symbol, typed
        MarginType) view of symbols.yaml that RiskManager's max_notional
        check now also uses.
        """
        for symbol, cfg in self.settings.symbols.items():
            if not cfg.enabled:
                continue
            leverage_cfg = cfg.to_leverage_config(symbol)
            await self.trading_client.set_leverage(symbol, leverage_cfg.leverage)
            logger.info("Set %s leverage to %dx on exchange", symbol, leverage_cfg.leverage)
            await self.trading_client.set_margin_type(symbol, leverage_cfg.margin_type.value)
            logger.info(
                "Set %s margin type to %s on exchange",
                symbol, leverage_cfg.margin_type.value,
            )

    async def sync_equity(self) -> None:
        """
        Fetch and sync current account equity.

        Deliberately branches by provider rather than hiding this behind a
        shared abstraction:
          - Binance: total margin balance (wallet balance + unrealized P&L)
            via BinanceAccountService, matching how Binance itself computes
            available margin for new positions.
          - Bybit: wallet balance only (get_account_balance, summed across
            USDT-denominated assets), which is the more conservative figure
            since Bybit has no equivalent single "margin balance incl.
            unrealized P&L" field exposed the same way. This means Bybit
            equity may lag slightly behind open unrealized P&L versus
            Binance — acceptable for risk-sizing purposes, since it errs
            toward under- rather than over-stating available equity.
        """
        if self.paper_exchange_client is not None:
            # Paper mode: equity lives entirely on the simulated exchange —
            # BinanceAccountService/get_account_balance() would either hit a
            # real (and irrelevant) account or fail without live
            # credentials. Read the paper ledger directly instead.
            equity = self.paper_exchange_client.equity
        elif self.exchange_provider == "binance":
            equity = await self.account_service.get_equity()
        else:
            balances = await self.rest_client.get_account_balance()
            equity = balances.get("USDT", Decimal("0"))
        self.risk_manager.update_equity(equity)

    async def record_metrics_snapshot(self) -> None:
        """
        Update Prometheus account gauges and persist a periodic
        MetricSnapshotORM row via MetricRepository.

        MetricRepository/MetricSnapshotORM existed but were never called
        from anywhere in the live path — this is the only thing that
        writes a row. Scheduled alongside equity resync (see startup.py)
        so historical equity/drawdown/win-rate trends survive a process
        restart, unlike the in-memory RiskManager/PositionManager stats
        this reads from, which reset to zero on every start.

        account_equity/account_daily_pnl/account_drawdown_pct
        (trading_account_*) were declared in metrics.py but never set —
        unlike the other 16 Prometheus metrics (see MetricsCollector),
        there's no discrete domain event to hang these off; equity/PnL/
        drawdown drift continuously with every mark-price tick rather than
        changing at well-defined moments. This periodic snapshot already
        gathers exactly the data they need, so it's the natural call site.
        Deliberately computed and set *before* the DB-connectivity guard
        below — Prometheus reporting must keep working even when
        persistence is down, the same way OrderManager/PositionManager's
        in-memory state isn't gated on DB connectivity either.

        Best-effort: persistence failures are logged and swallowed, same
        pattern as OrderManager._persist / PositionManager._persist_trade
        / StrategyOrchestrator._persist_signal — a metrics-snapshot outage
        must never affect the trading path.
        """
        risk_stats = self.risk_manager.stats
        closed_trades = self.position_manager.closed_trades
        total_trades = len(closed_trades)
        winning_trades = sum(1 for t in closed_trades if t.is_winner)
        win_rate = (winning_trades / total_trades) if total_trades else 0.0
        total_pnl = sum((t.net_pnl for t in closed_trades), Decimal("0"))

        try:
            account_equity.set(float(self.risk_manager.equity))
            account_daily_pnl.set(float(self.risk_manager.daily_pnl))
            account_drawdown_pct.set(float(risk_stats["drawdown_pct"]))
        except Exception:
            logger.exception("Failed to record account Prometheus gauges")

        if not self.db.is_connected:
            return
        try:
            from src.persistence.metrics import MetricRepository, MetricSnapshotORM

            snapshot = MetricSnapshotORM(
                snapshot_at=datetime.now(tz=UTC),
                # Account-level rollup rather than per-strategy: strategy
                # attribution for closed trades is available (Trade.
                # strategy_id) but this snapshot mirrors what the
                # dashboard already reports at the account level
                # (_dashboard_metrics/_dashboard_status below).
                strategy_id="__account__",
                equity=self.risk_manager.equity,
                daily_pnl=self.risk_manager.daily_pnl,
                total_pnl=total_pnl,
                drawdown_pct=risk_stats["drawdown_pct"],
                win_rate=win_rate,
                open_positions=len(self.position_manager.open_positions),
                total_trades=total_trades,
            )
            async with self.db.session() as session:
                await MetricRepository(session).save_snapshot(snapshot)
            logger.debug("Metrics snapshot persisted: equity=%s", snapshot.equity)
        except Exception as exc:
            logger.warning("Failed to persist metrics snapshot: %s", exc)

    async def check_portfolio_rebalance(self) -> None:
        """
        Compare live per-symbol notional against target allocation and log
        any drift beyond PortfolioRebalancer's threshold.

        PortfolioService/PortfolioAllocator/PortfolioRebalancer existed but
        had no caller anywhere in the live path, so a book could drift
        arbitrarily far from its target weights (e.g. one symbol growing to
        dominate the book purely from price moves, or shrinking to
        near-zero) with nothing surfacing it beyond an operator manually
        computing it from the dashboard.

        Deliberately detection-only, matching PortfolioRebalancer's own
        design (see its docstring): this only logs a warning per drifted
        symbol. It never resizes or places an order itself — that stays a
        human/strategy decision, since target notional here is a simple
        equal-weight split across the currently enabled symbols, not a
        signal-aware sizing decision.
        """
        if not self._rebalance_symbols:
            return
        try:
            target = self.portfolio_service.target_allocation(self._rebalance_symbols)
            actions = self.portfolio_service.rebalancer.check_rebalance(
                self.portfolio, target
            )
            self._last_rebalance_actions = actions
            for action in actions:
                logger.warning(
                    "Portfolio drift: %s is %s target by %.2f%% (current=%s target=%s)",
                    action.symbol, action.direction.value, action.drift_pct,
                    action.current_notional, action.target_notional,
                )
        except Exception as exc:
            logger.warning("Failed to evaluate portfolio rebalance: %s", exc)

    def get_instrument(self, symbol: str) -> Any:
        inst = self.instruments.get(symbol.upper())
        if inst is None:
            raise KeyError(f"Instrument not found: {symbol}. Run initialize_instruments() first.")
        return inst

    async def _on_watchdog_timeout(self) -> None:
        """
        Fired by EventLoopWatchdog on the not-stalled -> stalled transition.

        Publishes a CRITICAL AlertFiredEvent so a stalled event loop flows
        through the same AlertFiredEvent -> Notifier.send() wiring as every
        other system alert (see bootstrap/startup.py's alert wiring). Before
        this, a stall was only ever visible as a CRITICAL log line: nobody
        got paged, and — worse — the dashboard's HTTP health endpoint that
        would otherwise surface it runs on the same (stalled) event loop, so
        polling-based detection fails at exactly the moment it's needed.
        This callback runs on the main loop (scheduled cross-thread by the
        watchdog via run_coroutine_threadsafe), so it's safe to use the
        loop-bound event bus and notifier here.
        """
        await get_event_bus().publish(AlertFiredEvent(
            alert_name="event_loop_stalled",
            severity=AlertSeverity.CRITICAL,
            message=(
                f"Event loop unresponsive for {self.watchdog.age_seconds:.1f}s "
                f"(timeout={self.watchdog.timeout_seconds:.0f}s)"
            ),
            context={"age_seconds": self.watchdog.age_seconds},
        ))

    # ─── Dashboard data providers ──────────────────────────────────────────────
    # Deliberately synchronous and cheap: these run on the dashboard's HTTP
    # thread and the WS broadcast loop, so they read cached/in-memory stats
    # rather than awaiting exchange/DB calls (that's what HealthChecker.run_all()
    # is for, on the async health-check path).

    def _dashboard_health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.watchdog.is_alive() else "degraded",
            "event_loop_age_seconds": self.watchdog.age_seconds,
            "heartbeat_count": self.heartbeat.beat_count,
            "trading_env": self.settings.trading_env.value,
        }

    def _dashboard_status(self) -> dict[str, Any]:
        from src.risk.portfolio_risk import portfolio_risk_report

        return {
            "equity": str(self.risk_manager.equity),
            "portfolio": portfolio_risk_report(self.portfolio, self.risk_manager.equity),
            "open_orders": self.order_manager.stats if hasattr(self.order_manager, "stats") else {},
        }

    def _dashboard_metrics(self) -> dict[str, Any]:
        return {
            "execution": self.execution_engine.stats,
            "risk": self.risk_manager.stats,
            "strategy": self.orchestrator.stats if self.orchestrator is not None else {},
        }


def create_application(settings: Settings) -> Application:
    """Factory function: configure logging then build the application."""
    configure_logging(
        log_level=settings.log_level,
        json_logs=settings.is_production,
        file_enabled=settings.logging.file_enabled,
        log_dir=settings.paths.log_dir,
        file_name=settings.logging.file_name,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
        logger_levels=settings.logging.logger_levels,
    )
    logger.info(
        "Building application: env=%s debug=%s",
        settings.trading_env.value,
        settings.debug,
    )

    if settings.event_bus_backend == "redis":
        from src.core.events import set_event_bus
        from src.core.events_redis import RedisEventBus

        set_event_bus(RedisEventBus(redis_url=settings.redis.url))
        logger.info("Event bus backend: redis (mirroring to %s)", settings.redis.url)

    return Application(settings)
