"""
System configuration loaded from environment variables and optional YAML files.

Configuration hierarchy (later overrides earlier):
  1. Default values defined on each field
  2. YAML config file for the active environment (configs/{env}.yaml)
  3. Environment variables (highest precedence)

Security rules:
  - Secrets (API keys, passwords) MUST come from environment variables only.
  - YAML config files must NEVER contain secrets.
  - SecretStr fields are never logged or serialised as plaintext.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.domain.leverage import LeverageConfig

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.constants import (
    BINANCE_FUTURES_MAINNET_REST,
    BINANCE_FUTURES_MAINNET_WS,
    BINANCE_FUTURES_TESTNET_REST,
    BINANCE_FUTURES_TESTNET_WS,
    BYBIT_MAINNET_PRIVATE_WS,
    BYBIT_MAINNET_PUBLIC_WS,
    BYBIT_TESTNET_PRIVATE_WS,
    BYBIT_TESTNET_PUBLIC_WS,
    DB_MAX_OVERFLOW,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT_SECONDS,
    DEFAULT_CIRCUIT_BREAKER_COOLDOWN_MINUTES,
    DEFAULT_MAX_DAILY_LOSS_PCT,
    DEFAULT_MAX_DRAWDOWN_PCT,
    DEFAULT_MAX_OPEN_POSITIONS,
    DEFAULT_MIN_RISK_REWARD,
    DEFAULT_RISK_PCT,
    HTTP_MAX_RETRIES,
    HTTP_TIMEOUT_SECONDS,
    MAX_LEVERAGE,
    MAX_POSITION_PCT_OF_EQUITY,
)
from src.core.enums import TradingEnvironment

logger = logging.getLogger(__name__)


class DatabaseConfig(BaseSettings):
    """PostgreSQL connection settings."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    url: str = Field(
        default="postgresql+asyncpg://trader:secret@localhost:5432/trading",
        description="SQLAlchemy async database URL",
    )
    pool_size: int = Field(default=DB_POOL_SIZE, ge=1, le=50)
    max_overflow: int = Field(default=DB_MAX_OVERFLOW, ge=0, le=100)
    pool_timeout: float = Field(default=float(DB_POOL_TIMEOUT_SECONDS), gt=0)
    command_timeout: float = Field(default=10.0, gt=0)
    echo: bool = Field(default=False, description="Echo SQL statements — dev only")

    @field_validator("url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql", "sqlite")):
            raise ValueError(f"Unsupported database URL scheme: {v!r}")
        return v


class RedisConfig(BaseSettings):
    """Redis connection settings."""

    model_config = SettingsConfigDict(env_prefix="REDIS_")

    url: str = Field(default="redis://localhost:6379/0")
    password: SecretStr | None = Field(default=None)
    max_connections: int = Field(default=20, ge=1, le=500)
    socket_timeout: float = Field(default=5.0, gt=0)
    decode_responses: bool = Field(default=True)


class BybitConfig(BaseSettings):
    """
    Bybit V5 USDT-Perpetual ("linear") exchange credentials and connectivity
    settings, mirroring ``BinanceConfig``. Only used when
    ``EXCHANGE_PROVIDER=bybit`` (see ``bootstrap/exchange_factory.py``).
    """

    model_config = SettingsConfigDict(env_prefix="BYBIT_")

    api_key: SecretStr = Field(default=SecretStr(""), description="Bybit API key")
    api_secret: SecretStr = Field(default=SecretStr(""), description="Bybit API secret")
    testnet: bool = Field(
        default=True,
        description="Use Bybit testnet endpoints. Must be False for live trading.",
    )
    recv_window: int = Field(default=5_000, ge=100, le=60_000)
    http_timeout: float = Field(default=HTTP_TIMEOUT_SECONDS, gt=0)
    max_retries: int = Field(default=HTTP_MAX_RETRIES, ge=0, le=10)
    rest_mainnet: str = Field(
        default="https://api.bybit.com",
        description="Bybit mainnet REST base URL. Overridable via configs/exchanges.yaml.",
    )
    rest_testnet: str = Field(
        default="https://api-testnet.bybit.com",
        description="Bybit testnet REST base URL. Overridable via configs/exchanges.yaml.",
    )
    max_orders_per_second: int = Field(
        default=10, gt=0, description="RateLimiter ceiling passed to BybitFuturesClient."
    )
    max_orders_per_minute: int = Field(
        default=600, gt=0, description="RateLimiter ceiling passed to BybitFuturesClient."
    )

    @property
    def rest_base_url(self) -> str:
        return self.rest_testnet if self.testnet else self.rest_mainnet

    @property
    def ws_public_url(self) -> str:
        """Active public (market data) WebSocket base URL."""
        return BYBIT_TESTNET_PUBLIC_WS if self.testnet else BYBIT_MAINNET_PUBLIC_WS

    @property
    def ws_private_url(self) -> str:
        """Active private (account data) WebSocket base URL."""
        return BYBIT_TESTNET_PRIVATE_WS if self.testnet else BYBIT_MAINNET_PRIVATE_WS

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def validate_not_placeholder_bybit(cls, v: Any) -> Any:
        if isinstance(v, str) and v.lower() in {
            "your_api_key_here",
            "your_api_secret_here",
            "changeme",
        }:
            raise ValueError(
                "API credentials must not be placeholder values. "
                "Set BYBIT_API_KEY and BYBIT_API_SECRET in your .env file."
            )
        return v


class BinanceConfig(BaseSettings):
    """
    Binance USDT-M Futures exchange credentials and connectivity settings.

    API keys are loaded from environment variables only.
    Placeholder values are rejected to prevent accidental live trading
    with test credentials.
    """

    model_config = SettingsConfigDict(env_prefix="BINANCE_")

    api_key: SecretStr = Field(description="Binance API key (from environment only)")
    api_secret: SecretStr = Field(description="Binance API secret (from environment only)")
    testnet: bool = Field(
        default=True,
        description="Use Binance testnet endpoints. Must be False for live trading.",
    )
    recv_window: int = Field(
        default=5_000,
        ge=100,
        le=60_000,
        description="Timestamp tolerance window in milliseconds",
    )
    http_timeout: float = Field(
        default=HTTP_TIMEOUT_SECONDS,
        gt=0,
        description="HTTP request timeout in seconds",
    )
    max_retries: int = Field(
        default=HTTP_MAX_RETRIES,
        ge=0,
        le=10,
        description="Maximum retry attempts for failed requests",
    )
    rest_mainnet: str = Field(
        default=BINANCE_FUTURES_MAINNET_REST,
        description="Binance mainnet REST base URL. Overridable via configs/exchanges.yaml.",
    )
    rest_testnet: str = Field(
        default=BINANCE_FUTURES_TESTNET_REST,
        description="Binance testnet REST base URL. Overridable via configs/exchanges.yaml.",
    )
    ws_mainnet: str = Field(
        default=BINANCE_FUTURES_MAINNET_WS,
        description="Binance mainnet WebSocket base URL. Overridable via configs/exchanges.yaml.",
    )
    ws_testnet: str = Field(
        default=BINANCE_FUTURES_TESTNET_WS,
        description="Binance testnet WebSocket base URL. Overridable via configs/exchanges.yaml.",
    )
    max_orders_per_second: int = Field(
        default=10, gt=0, description="RateLimiter ceiling passed to BinanceFuturesRESTClient."
    )
    max_orders_per_minute: int = Field(
        default=1_200, gt=0, description="RateLimiter ceiling passed to BinanceFuturesRESTClient."
    )

    @property
    def rest_base_url(self) -> str:
        """Active REST API base URL."""
        return self.rest_testnet if self.testnet else self.rest_mainnet

    @property
    def ws_base_url(self) -> str:
        """Active WebSocket base URL."""
        return self.ws_testnet if self.testnet else self.ws_mainnet

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def validate_not_placeholder(cls, v: Any) -> Any:
        if isinstance(v, str) and v.lower() in {
            "your_api_key_here",
            "your_api_secret_here",
            "changeme",
            "",
        }:
            raise ValueError(
                "API credentials must not be placeholder values. "
                "Set BINANCE_API_KEY and BINANCE_API_SECRET in your .env file."
            )
        return v


class RiskConfig(BaseSettings):
    """Risk management parameters with validated bounds."""

    model_config = SettingsConfigDict(env_prefix="RISK_")

    max_portfolio_risk_pct: Decimal = Field(
        default=DEFAULT_RISK_PCT,
        description="Maximum risk capital per trade as % of total equity",
        gt=Decimal("0"),
        le=Decimal("10"),
    )
    max_daily_loss_pct: Decimal = Field(
        default=DEFAULT_MAX_DAILY_LOSS_PCT,
        description="Maximum cumulative daily loss before circuit breaker trips",
        gt=Decimal("0"),
        le=Decimal("50"),
    )
    max_drawdown_pct: Decimal = Field(
        default=DEFAULT_MAX_DRAWDOWN_PCT,
        description="Maximum peak-to-trough drawdown before circuit breaker trips",
        gt=Decimal("0"),
        le=Decimal("100"),
    )
    max_open_positions: int = Field(
        default=DEFAULT_MAX_OPEN_POSITIONS,
        description="Maximum simultaneous open positions",
        ge=1,
        le=100,
    )
    max_leverage: Decimal = Field(
        default=Decimal(str(MAX_LEVERAGE)),
        description="Maximum allowable leverage",
        gt=Decimal("0"),
        le=Decimal("125"),
    )
    max_position_pct_of_equity: Decimal = Field(
        default=MAX_POSITION_PCT_OF_EQUITY,
        description="Maximum single position notional as % of equity",
        gt=Decimal("0"),
        le=Decimal("100"),
    )
    max_correlated_exposure_pct: Decimal = Field(
        default=Decimal("40"),
        description="Max combined notional in correlated instruments as % of equity",
        gt=Decimal("0"),
        le=Decimal("100"),
    )
    atr_stop_multiplier: Decimal = Field(
        default=Decimal("1.5"),
        description="ATR multiplier for initial stop-loss placement",
        gt=Decimal("0"),
        le=Decimal("10"),
    )
    risk_reward_minimum: Decimal = Field(
        default=DEFAULT_MIN_RISK_REWARD,
        description="Minimum acceptable risk-reward ratio; signals below this are rejected",
        gt=Decimal("0"),
    )
    max_stop_loss_pct: Decimal = Field(
        default=Decimal("5.0"),
        description=(
            "Maximum allowed stop-loss distance from entry, as % of entry "
            "price. Guards against a bad ATR/structure/OB stop calculation "
            "(e.g. from a data glitch or misconfigured multiplier) sizing "
            "an enormous effective risk before validate_stop_risk() catches "
            "it in RiskManager.validate_signal."
        ),
        gt=Decimal("0"),
        le=Decimal("50"),
    )
    min_stop_loss_pct: Decimal = Field(
        default=Decimal("0.05"),
        description=(
            "Minimum allowed stop-loss distance from entry, as % of entry "
            "price. Guards against a near-zero stop distance that would "
            "either reject via PositionSizingError's divide-by-near-zero "
            "or size a wildly oversized position for the risk budget."
        ),
        gt=Decimal("0"),
        le=Decimal("10"),
    )
    circuit_breaker_cooldown_minutes: int = Field(
        default=DEFAULT_CIRCUIT_BREAKER_COOLDOWN_MINUTES,
        description="Minutes an OPEN circuit breaker waits before probing HALF_OPEN recovery",
        ge=1,
        le=1440,
    )
    correlation_window: int = Field(
        default=60,
        description=(
            "Number of closed candles used to compute rolling price-return "
            "correlations for correlation-adjusted position sizing"
        ),
        ge=2,
        le=1000,
    )
    max_gross_exposure_pct: Decimal = Field(
        default=Decimal("200"),
        description=(
            "Maximum total notional across all open positions (gross, "
            "longs + shorts) as % of equity — the account-wide leverage "
            "ceiling independent of any per-symbol or correlated-group cap"
        ),
        gt=Decimal("0"),
        le=Decimal("1000"),
    )
    max_net_exposure_pct: Decimal = Field(
        default=Decimal("150"),
        description=(
            "Maximum net directional notional (longs - shorts) as % of "
            "equity, so a book that individually satisfies every "
            "per-position and per-symbol cap can't still end up "
            "effectively all-long or all-short beyond intended risk"
        ),
        gt=Decimal("0"),
        le=Decimal("1000"),
    )
    position_sizing_method: str = Field(
        default="fixed_fractional",
        description=(
            "Position sizing model RiskManager.calculate_position_size() "
            "uses for the base size (before the correlation/notional/"
            "circuit-breaker adjustments that always apply on top). "
            "'fixed_fractional' (default) risks a flat % of equity per "
            "trade — see risk/position_sizing.py::calculate_position_size(), "
            "unchanged behavior from before this field existed. "
            "'volatility_adjusted' scales that base size down when the "
            "signal's stop is wider than target_atr_risk_multiple × ATR, "
            "and up (capped at the fixed-fractional amount) when tighter — "
            "see risk/position_sizing.py::volatility_adjusted_size(). "
            "'kelly' uses fractional Kelly Criterion sizing (risk/"
            "position_sizing.py::kelly_position_size()), fed by a live "
            "rolling win-rate/avg-win/avg-loss tracker (risk/"
            "trade_tracker.py::RollingTradeTracker) built from closed "
            "PositionClosedEvents — see kelly_lookback_trades/"
            "kelly_min_trades/kelly_fraction/kelly_max_risk_pct below. "
            "Falls back to 'fixed_fractional' (with a logged warning) "
            "until kelly_min_trades closed trades have accumulated in the "
            "current process, and per-signal whenever the estimated edge "
            "is non-positive."
        ),
        pattern="^(fixed_fractional|volatility_adjusted|kelly)$",
    )
    volatility_target_atr_multiple: float = Field(
        default=1.5,
        description=(
            "Only read when position_sizing_method='volatility_adjusted'. "
            "Target stop distance as a multiple of ATR that sizing treats "
            "as 'normal' — see volatility_adjusted_size()'s target_atr_risk."
        ),
        gt=0.0,
        le=10.0,
    )
    kelly_lookback_trades: int = Field(
        default=30,
        description=(
            "Only read when position_sizing_method='kelly'. Size of the "
            "rolling window of closed-trade net P&L RollingTradeTracker "
            "keeps in memory — win_rate/avg_win/avg_loss are computed "
            "fresh from whatever's currently in this window on every "
            "read, so older trades age out rather than permanently "
            "anchoring the estimate to stale market conditions."
        ),
        ge=5,
        le=500,
    )
    kelly_min_trades: int = Field(
        default=20,
        description=(
            "Only read when position_sizing_method='kelly'. Minimum "
            "closed trades required in the rolling window before Kelly "
            "sizing is trusted; below this, calculate_position_size() "
            "falls back to fixed_fractional. Must be <= "
            "kelly_lookback_trades — a floor higher than the window "
            "itself could never be satisfied."
        ),
        ge=1,
        le=500,
    )
    kelly_fraction: float = Field(
        default=0.25,
        description=(
            "Only read when position_sizing_method='kelly'. Fraction of "
            "full Kelly to actually use — full Kelly is theoretically "
            "optimal but extremely volatile in practice, so fractional "
            "Kelly (commonly 0.25–0.5) trades some growth rate for much "
            "lower variance. See kelly_position_size()'s kelly_fraction."
        ),
        gt=0.0,
        le=1.0,
    )
    kelly_max_risk_pct: float = Field(
        default=5.0,
        description=(
            "Only read when position_sizing_method='kelly'. Hard cap on "
            "the % of equity Kelly sizing can risk on one trade, "
            "regardless of what the Kelly formula itself outputs — see "
            "kelly_position_size()'s max_risk_pct. Independent of "
            "max_portfolio_risk_pct (which only feeds fixed_fractional/"
            "volatility_adjusted): Kelly's own formula can output a "
            "fraction far larger than a single flat risk_pct."
        ),
        gt=0.0,
        le=25.0,
    )

    @model_validator(mode="after")
    def _validate_kelly_thresholds(self) -> RiskConfig:
        if self.kelly_min_trades > self.kelly_lookback_trades:
            raise ValueError(
                f"kelly_min_trades ({self.kelly_min_trades}) cannot exceed "
                f"kelly_lookback_trades ({self.kelly_lookback_trades}) — a "
                "minimum-sample floor higher than the rolling window itself "
                "tracks could never be satisfied, permanently disabling "
                "Kelly sizing even after arbitrarily many trades."
            )
        return self


class SymbolRiskConfig(BaseSettings):
    """Per-symbol trading configuration (leverage, margin mode, enablement).

    Loaded from configs/symbols.yaml. Per-symbol leverage here is what's
    actually set on the exchange for that symbol (via set_leverage on
    startup) and what RiskManager uses for symbol-specific liquidation/
    margin checks. RiskConfig.max_leverage remains a hard account-wide
    ceiling: any symbol leverage here that exceeds it is a config error,
    not silently clamped, since silently lowering it would make the
    configured value a lie about what's actually deployed.
    """

    model_config = SettingsConfigDict(env_prefix="")

    leverage: int = Field(default=1, ge=1, le=125)
    margin_type: str = Field(default="ISOLATED")
    enabled: bool = Field(default=True)
    max_notional: Decimal | None = Field(
        default=None,
        description=(
            "Optional hard notional cap for this symbol, enforced by "
            "RiskManager.calculate_position_size() in addition to the "
            "account-wide max_position_pct_of_equity cap. None means no "
            "symbol-specific cap beyond the account-wide one."
        ),
    )

    def to_leverage_config(self, symbol: str) -> LeverageConfig:
        """Build the validated domain model consumed by bootstrap/app.py

        (to push leverage + margin type to the exchange) and RiskManager
        (for the per-symbol max_notional pre-trade cap). Kept as a
        conversion rather than replacing SymbolRiskConfig outright so
        env-var overrides (BaseSettings) and the domain layer (plain
        pydantic model) stay decoupled.
        """
        from src.domain.leverage import LeverageConfig

        return LeverageConfig(
            symbol=symbol,
            leverage=self.leverage,
            margin_type=self.margin_type.lower(),
            max_notional=self.max_notional,
        )


class NotificationConfig(BaseSettings):
    """Notification channel configuration. All webhook URLs are treated as secrets."""

    model_config = SettingsConfigDict(env_prefix="")

    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")
    discord_webhook_url: SecretStr | None = Field(default=None, alias="DISCORD_WEBHOOK_URL")
    slack_webhook_url: SecretStr | None = Field(default=None, alias="SLACK_WEBHOOK_URL")
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT", ge=1, le=65535)
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_password: SecretStr | None = Field(default=None, alias="SMTP_PASSWORD")
    alert_email: str | None = Field(default=None, alias="ALERT_EMAIL")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_webhook_url)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password and self.alert_email)


class MonitoringConfig(BaseSettings):
    """Monitoring and observability configuration."""

    model_config = SettingsConfigDict(env_prefix="")

    prometheus_port: int = Field(
        default=9090,
        alias="PROMETHEUS_PORT",
        ge=1024,
        le=65535,
    )
    dashboard_port: int = Field(
        default=8080,
        alias="DASHBOARD_PORT",
        ge=1024,
        le=65535,
    )
    dashboard_ws_port: int = Field(
        default=8081,
        alias="DASHBOARD_WS_PORT",
        ge=1024,
        le=65535,
        description="Port for the real-time WebSocket push feed (see monitoring/dashboard.py).",
    )
    enable_prometheus: bool = Field(default=True, alias="ENABLE_PROMETHEUS")
    enable_dashboard: bool = Field(default=True, alias="ENABLE_DASHBOARD")
    enable_dashboard_ws: bool = Field(default=True, alias="ENABLE_DASHBOARD_WS")

    # The following are not env-backed (no alias) — they're only ever set via
    # configs/monitoring.yaml, merged in Settings._load_monitoring_yaml().
    # Defaults here match what was previously hardcoded at each call site
    # (AlertRule thresholds/cooldowns in monitoring/alerts.py,
    # HeartbeatService/EventLoopWatchdog/health-check-scheduler defaults in
    # bootstrap/app.py + startup.py) so behavior is unchanged when no yaml
    # file is present.
    daily_loss_warning_pct: float = Field(default=3.0, gt=0)
    drawdown_warning_pct: float = Field(default=10.0, gt=0)
    alert_cooldown_minutes: int = Field(
        default=30,
        ge=0,
        description="Applied uniformly to all build_standard_alerts() rules when set via yaml.",
    )
    health_check_interval_seconds: float = Field(default=60.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=60.0, gt=0)
    watchdog_timeout_seconds: float = Field(default=30.0, gt=0)
    watchdog_check_interval_seconds: float = Field(default=5.0, gt=0)


class LoggingConfig(BaseSettings):
    """
    File-rotation parameters for the RotatingFileHandler wired into
    structlog by monitoring/telemetry.py:configure_logging().

    Not env-backed (no alias) — only ever set via configs/logging.yaml,
    merged in Settings._load_logging_yaml(). Defaults match the values
    previously hardcoded in the (unwired) dictConfig tree that used to
    live in that file, so behavior is unchanged when no yaml is present.
    """

    model_config = SettingsConfigDict(env_prefix="")

    file_enabled: bool = Field(default=True)
    file_name: str = Field(default="trading.log")
    max_bytes: int = Field(default=52_428_800, gt=0)
    backup_count: int = Field(default=10, ge=0)
    logger_levels: dict[str, str] = Field(
        default_factory=lambda: {
            "asyncio": "WARNING",
            "websockets": "WARNING",
            "aiohttp": "WARNING",
            "sqlalchemy.engine": "WARNING",
        },
        description=(
            "Per-logger level overrides applied after structlog setup. "
            "Defaults match the noisy-third-party suppression previously "
            "hardcoded in monitoring/telemetry.py:configure_logging(); "
            "merged (not replaced) with configs/logging.yaml's `loggers:` "
            "section in Settings._load_logging_yaml()."
        ),
    )


class PathConfig(BaseSettings):
    """File-system path configuration."""

    model_config = SettingsConfigDict(env_prefix="", protected_namespaces=())

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    log_dir: Path = Field(default=Path("./logs"), alias="LOG_DIR")
    backup_dir: Path = Field(default=Path("./backups"), alias="BACKUP_DIR")
    # Named `models_dir` (not `model_dir`) to avoid colliding with
    # Pydantic's reserved "model_" protected-namespace prefix; the
    # MODEL_DIR environment variable alias is unchanged for backward
    # compatibility with existing deployments.
    models_dir: Path = Field(default=Path("./models"), alias="MODEL_DIR")
    config_dir: Path = Field(default=Path("./configs"), alias="CONFIG_DIR")

    @model_validator(mode="after")
    def create_directories(self) -> PathConfig:
        """Ensure runtime directories exist on startup."""
        for path in (self.data_dir, self.log_dir, self.backup_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self


class Settings(BaseSettings):
    """
    Root settings object — the single source of truth for all configuration.

    Loading order (later overrides earlier):
      1. Field defaults
      2. configs/{environment}.yaml
      3. .env file
      4. Environment variables

    Usage:
        settings = get_settings()
        settings.binance.api_key.get_secret_value()
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # Core
    trading_env: TradingEnvironment = Field(
        default=TradingEnvironment.PAPER,
        alias="TRADING_ENV",
        description="Active trading environment",
    )
    log_level: str = Field(
        default="INFO",
        alias="LOG_LEVEL",
    )
    debug: bool = Field(
        default=False,
        alias="DEBUG",
        description="Enable debug mode — verbose logging, SQL echo, etc.",
    )

    exchange_provider: str = Field(
        default="binance",
        alias="EXCHANGE_PROVIDER",
        description=(
            "Which exchange adapter to construct: 'binance' or 'bybit'. "
            "See bootstrap/exchange_factory.py. Only the REST/data-layer "
            "contract (IExchangeClient) is provider-agnostic today; the "
            "live user-data-stream wiring in bootstrap/startup.py is still "
            "Binance-specific (see ROADMAP.md)."
        ),
    )

    paper_initial_equity: float = Field(
        default=10000.0,
        alias="PAPER_INITIAL_EQUITY",
        description=(
            "Starting USDT equity for PaperExchangeClient when "
            "trading_env=paper. Ignored outside paper mode."
        ),
    )

    event_bus_backend: str = Field(
        default="memory",
        alias="EVENT_BUS_BACKEND",
        description=(
            "'memory' (default, in-process only) or 'redis' (also mirrors "
            "every event onto a Redis Stream — see core/events_redis.py — "
            "so other processes can observe the event flow). In-process "
            "dispatch behavior is identical either way."
        ),
    )

    # Sub-configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    binance: BinanceConfig | None = Field(default=None)
    bybit: BybitConfig | None = Field(default=None)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    paths: PathConfig = Field(default_factory=PathConfig)

    # Per-symbol overrides loaded from configs/symbols.yaml (not env-backed —
    # there's no sane single-value env var for a per-symbol map).
    symbols: dict[str, SymbolRiskConfig] = Field(default_factory=dict)

    # YAML data loaded separately (not a pydantic field)
    _yaml_data: dict[str, Any] = {}
    # Tracks which MonitoringConfig fields were explicitly overridden by
    # configs/monitoring.yaml (see _load_monitoring_yaml). Needed because
    # after that merge self.monitoring is rebuilt via
    # `MonitoringConfig(**{**model_dump(), **overrides})`, which passes
    # every field explicitly — so MonitoringConfig.model_fields_set can no
    # longer distinguish "came from yaml" from "took its default" the way
    # it could before the merge.
    _monitoring_yaml_overrides: set[str] = set()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(f"Invalid log level {v!r}. Must be one of {valid_levels}")
        return upper

    @model_validator(mode="after")
    def _populate_exchange_config(self) -> Settings:
        """
        Auto-construct the active exchange's credential/connectivity config
        from its BINANCE_*/BYBIT_*-prefixed environment variables.

        BinanceConfig/BybitConfig are declared as optional fields defaulting
        to None, but nothing previously ever instantiated them in the real
        get_settings() path — only tests did, by passing pre-built config
        objects directly into Settings(...). That meant settings.binance and
        settings.bybit stayed None unconditionally in production, regardless
        of which BINANCE_*/BYBIT_* env vars were actually set (see
        .env.example), so exchange_factory.build_rest_client/build_ws_client
        would always raise ConfigurationError — the system could never
        actually start against a real or testnet exchange.

        Only the configured provider (settings.exchange_provider) is
        constructed, so the *other* exchange's credentials never need to be
        present. If construction fails (e.g. the relevant API key/secret
        genuinely isn't set), this leaves the field as None rather than
        raising here — exchange_factory already raises a clear
        ConfigurationError for that case, and some callers (tests, config-
        only tooling) construct Settings without ever touching the
        exchange and shouldn't be forced to supply credentials to do so.
        """
        provider = self.exchange_provider.lower()
        if provider == "binance" and self.binance is None:
            try:
                self.binance = BinanceConfig()
            except Exception:
                pass
        elif provider == "bybit" and self.bybit is None:
            try:
                self.bybit = BybitConfig()
            except Exception:
                pass
        return self

    @property
    def is_production(self) -> bool:
        """True if running in live production mode."""
        return self.trading_env == TradingEnvironment.PRODUCTION

    @property
    def is_paper(self) -> bool:
        """True if running in paper trading mode."""
        return self.trading_env == TradingEnvironment.PAPER

    @property
    def is_testnet(self) -> bool:
        """True if running on Binance testnet."""
        return self.trading_env == TradingEnvironment.TESTNET

    def load_yaml_overrides(self, config_dir: Path | None = None) -> None:
        """
        Load the YAML configuration file for the active environment.

        The YAML file must not contain secrets.
        Silently skips if no file is found (defaults apply).
        """
        base_dir = config_dir or self.paths.config_dir
        env_filenames: dict[TradingEnvironment, str] = {
            TradingEnvironment.PAPER: "paper.yaml",
            TradingEnvironment.TESTNET: "testnet.yaml",
            TradingEnvironment.PRODUCTION: "production.yaml",
        }
        config_file = base_dir / env_filenames[self.trading_env]
        if config_file.exists():
            with config_file.open("r", encoding="utf-8") as f:
                self._yaml_data = yaml.safe_load(f) or {}

        self._load_risk_yaml(base_dir)
        self._load_symbols_yaml(base_dir)
        self._load_monitoring_yaml(base_dir)
        self._load_exchanges_yaml(base_dir)
        self._load_logging_yaml(base_dir)

    def _load_risk_yaml(self, base_dir: Path) -> None:
        """
        Merge configs/risk.yaml into self.risk.

        This file previously wasn't read by any code path — RiskConfig only
        ever picked up RISK_*-prefixed env vars and field defaults, so every
        value in risk.yaml was silently ignored. Precedence is preserved per
        the module docstring (env vars > yaml > defaults): a field is only
        overridden from yaml if it was NOT already explicitly set via env/
        dotenv, which `model_fields_set` tells us prior to this merge.
        """
        risk_file = base_dir / "risk.yaml"
        if not risk_file.exists():
            return
        with risk_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        risk_section: dict[str, Any] = dict(data.get("risk", {}) or {})
        cb_section: dict[str, Any] = data.get("circuit_breaker", {}) or {}
        if "cooldown_minutes" in cb_section:
            risk_section.setdefault(
                "circuit_breaker_cooldown_minutes", cb_section["cooldown_minutes"]
            )

        env_provided = self.risk.model_fields_set
        overrides = {
            key: str(value)
            for key, value in risk_section.items()
            if key in RiskConfig.model_fields and key not in env_provided
        }
        if overrides:
            merged = {**self.risk.model_dump(), **overrides}
            self.risk = RiskConfig(**merged)
            logger.info("Applied risk.yaml overrides: %s", sorted(overrides.keys()))

    def _load_symbols_yaml(self, base_dir: Path) -> None:
        """Load per-symbol leverage/margin-type/enablement from configs/symbols.yaml."""
        symbols_file = base_dir / "symbols.yaml"
        if not symbols_file.exists():
            return
        with symbols_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        raw_symbols: dict[str, Any] = data.get("symbols", {}) or {}
        parsed: dict[str, SymbolRiskConfig] = {}
        # Deferred import: src.core.validation pulls in src.core.exceptions,
        # and importing it at module level here has caused a circular-import
        # failure before (config.py is imported very early in bootstrap).
        from src.core.validation import validate_leverage

        for symbol, cfg in raw_symbols.items():
            cfg_data = cfg or {}
            symbol_cfg = SymbolRiskConfig(**cfg_data)
            validate_leverage(
                symbol_cfg.leverage,
                self.risk.max_leverage,
                detail=(
                    f"symbols.yaml sets {symbol} leverage={symbol_cfg.leverage}x, which "
                    f"exceeds risk.max_leverage={self.risk.max_leverage}x. Fix the config "
                    f"instead of silently clamping it — a clamp would make the value "
                    f"actually deployed on the exchange different from what's declared."
                ),
            )
            parsed[symbol.upper()] = symbol_cfg
        self.symbols = parsed

    def _load_monitoring_yaml(self, base_dir: Path) -> None:
        """
        Merge configs/monitoring.yaml into self.monitoring.

        Like risk.yaml before it, this file previously wasn't read by any
        code path: alert thresholds/cooldowns were hardcoded in
        monitoring/alerts.py's build_standard_alerts(), and health-check/
        heartbeat/watchdog intervals were hardcoded constructor defaults in
        bootstrap/app.py — editing monitoring.yaml had zero effect on the
        running system. Precedence matches _load_risk_yaml: env vars >
        yaml > field defaults, using model_fields_set to detect explicit
        env/dotenv values prior to this merge.

        Only `daily_loss_warning_pct`, `drawdown_warning_pct`,
        `alert_cooldown_minutes`, `health_check_interval_seconds`,
        `heartbeat_interval_seconds`, `watchdog_timeout_seconds`, and
        `watchdog_check_interval_seconds` are sourced from this file —
        `enable_prometheus`/`prometheus_port`/`enable_dashboard`/
        `dashboard_port`/`enable_dashboard_ws` remain env-only (aliased
        fields) since they were already reachable via PROMETHEUS_PORT etc.
        and duplicating that path here would just create a second,
        possibly-conflicting source of truth for the same values.
        """
        monitoring_file = base_dir / "monitoring.yaml"
        if not monitoring_file.exists():
            return
        with monitoring_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        flat: dict[str, Any] = {}
        alerts_section: dict[str, Any] = data.get("alerts", {}) or {}
        if "daily_loss_warning_pct" in alerts_section:
            flat["daily_loss_warning_pct"] = alerts_section["daily_loss_warning_pct"]
        if "drawdown_warning_pct" in alerts_section:
            flat["drawdown_warning_pct"] = alerts_section["drawdown_warning_pct"]
        if "cooldown_minutes" in alerts_section:
            flat["alert_cooldown_minutes"] = alerts_section["cooldown_minutes"]

        health_section: dict[str, Any] = data.get("health_check", {}) or {}
        if "interval_seconds" in health_section:
            flat["health_check_interval_seconds"] = health_section["interval_seconds"]

        heartbeat_section: dict[str, Any] = data.get("heartbeat", {}) or {}
        if "interval_seconds" in heartbeat_section:
            flat["heartbeat_interval_seconds"] = heartbeat_section["interval_seconds"]

        watchdog_section: dict[str, Any] = data.get("watchdog", {}) or {}
        if "timeout_seconds" in watchdog_section:
            flat["watchdog_timeout_seconds"] = watchdog_section["timeout_seconds"]
        if "check_interval_seconds" in watchdog_section:
            flat["watchdog_check_interval_seconds"] = watchdog_section["check_interval_seconds"]

        env_provided = self.monitoring.model_fields_set
        overrides = {
            key: value
            for key, value in flat.items()
            if key in MonitoringConfig.model_fields and key not in env_provided
        }
        if overrides:
            merged = {**self.monitoring.model_dump(), **overrides}
            self.monitoring = MonitoringConfig(**merged)
            self._monitoring_yaml_overrides = set(overrides.keys())
            logger.info("Applied monitoring.yaml overrides: %s", sorted(overrides.keys()))

    def _load_logging_yaml(self, base_dir: Path) -> None:
        """
        Merge configs/logging.yaml's `rotation` section into self.logging.

        configs/logging.yaml previously held a full logging.config
        dictConfig() tree, but no code path ever called dictConfig() on
        it — the live logging setup is entirely structlog-based (see
        monitoring/telemetry.py:configure_logging(), wired from
        bootstrap/app.py:create_application()), so every value in the
        file was silently ignored. Repurposed here as rotation-only
        parameters for the RotatingFileHandler that configure_logging()
        attaches to the root logger. Precedence matches the other
        yaml loaders: env vars > yaml > field defaults.
        """
        logging_file = base_dir / "logging.yaml"
        if not logging_file.exists():
            return
        with logging_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        rotation_section: dict[str, Any] = data.get("rotation", {}) or {}
        flat: dict[str, Any] = {}
        if "enabled" in rotation_section:
            flat["file_enabled"] = rotation_section["enabled"]
        if "filename" in rotation_section:
            flat["file_name"] = rotation_section["filename"]
        if "max_bytes" in rotation_section:
            flat["max_bytes"] = rotation_section["max_bytes"]
        if "backup_count" in rotation_section:
            flat["backup_count"] = rotation_section["backup_count"]

        # loggers: is merged on top of the LoggingConfig.logger_levels
        # default (asyncio/websockets/aiohttp/sqlalchemy.engine -> WARNING)
        # rather than replacing it outright, so a yaml file that only wants
        # to add or bump one logger doesn't silently un-suppress the other
        # three. Invalid level names are skipped with a warning rather than
        # raising, matching the fail-open posture of the rest of this
        # loader.
        loggers_section: dict[str, Any] = data.get("loggers", {}) or {}
        logger_levels = dict(self.logging.logger_levels)
        for name, entry in loggers_section.items():
            level = (entry or {}).get("level") if isinstance(entry, dict) else None
            if level and hasattr(logging, str(level).upper()):
                logger_levels[str(name)] = str(level).upper()
            elif level is not None:
                logger.warning(
                    "Ignoring invalid log level %r for logger %r in logging.yaml",
                    level,
                    name,
                )
        if logger_levels != self.logging.logger_levels:
            flat["logger_levels"] = logger_levels

        env_provided = self.logging.model_fields_set
        overrides = {
            key: value
            for key, value in flat.items()
            if key in LoggingConfig.model_fields
            and (key == "logger_levels" or key not in env_provided)
        }
        if overrides:
            merged = {**self.logging.model_dump(), **overrides}
            self.logging = LoggingConfig(**merged)
            logger.info("Applied logging.yaml overrides: %s", sorted(overrides.keys()))

    def _load_exchanges_yaml(self, base_dir: Path) -> None:
        """
        Merge configs/exchanges.yaml into self.binance / self.bybit.

        Like risk.yaml/monitoring.yaml before it, this file was never read
        by any code path. ``BinanceConfig.rest_base_url``/``ws_base_url``
        and ``BybitConfig.rest_base_url`` picked between two hardcoded
        literals keyed only on ``testnet`` (module-level constants for
        Binance, inline string literals for Bybit — both happened to match
        this file's values, which is exactly why the gap went unnoticed),
        and ``BinanceFuturesRESTClient``/``BybitFuturesClient`` each
        constructed a ``RateLimiter`` with ``max_per_second``/
        ``max_per_minute`` hardcoded in ``__init__`` rather than reading
        ``max_orders_per_second``/``max_orders_per_minute`` from anywhere.
        Editing exchanges.yaml had zero effect either way. Same env > yaml
        > field-default precedence as ``_load_risk_yaml``, via
        ``model_fields_set`` — and, like binance/bybit themselves, this is
        a no-op if the relevant config was never constructed (e.g.
        credentials absent, or the other provider is active).

        Bybit's WebSocket URLs are deliberately excluded here: this file
        has no ``bybit.ws_*`` keys (see the file's own comment — Bybit's
        live WS wiring isn't reachable from ``Application`` yet per
        ROADMAP.md), so there is nothing to merge for those two fields.
        """
        exchanges_file = base_dir / "exchanges.yaml"
        if not exchanges_file.exists():
            return
        with exchanges_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if self.binance is not None:
            binance_section: dict[str, Any] = data.get("binance", {}) or {}
            env_provided = self.binance.model_fields_set
            overrides = {
                key: value
                for key, value in binance_section.items()
                if key in BinanceConfig.model_fields and key not in env_provided
            }
            if overrides:
                merged = {**self.binance.model_dump(), **overrides}
                self.binance = BinanceConfig(**merged)
                logger.info(
                    "Applied exchanges.yaml binance overrides: %s", sorted(overrides.keys())
                )

        if self.bybit is not None:
            bybit_section: dict[str, Any] = data.get("bybit", {}) or {}
            env_provided = self.bybit.model_fields_set
            overrides = {
                key: value
                for key, value in bybit_section.items()
                if key in BybitConfig.model_fields and key not in env_provided
            }
            if overrides:
                merged = {**self.bybit.model_dump(), **overrides}
                self.bybit = BybitConfig(**merged)
                logger.info("Applied exchanges.yaml bybit overrides: %s", sorted(overrides.keys()))

    def get_yaml(self, key: str, default: Any = None) -> Any:
        """
        Retrieve a value from the YAML configuration using dot notation.

        Example:
            settings.get_yaml("risk.max_drawdown_pct", default=15.0)
        """
        parts = key.split(".")
        node: Any = self._yaml_data
        for part in parts:
            if not isinstance(node, dict):
                return default
            node = node.get(part, default)
        return node


_settings: Settings | None = None


def get_settings() -> Settings:
    """
    Return the singleton Settings instance, initialising on first call.

    Thread-safety note: this is not thread-safe for the initialisation path.
    In production, call this once during startup before spawning threads.
    """
    global _settings  # noqa: PLW0603
    if _settings is None:
        _settings = Settings()
        _settings.load_yaml_overrides()
    return _settings


def override_settings(settings: Settings) -> None:
    """
    Replace the singleton settings instance.

    For use in tests only. Never call this in production code.
    """
    global _settings  # noqa: PLW0603
    _settings = settings


def reset_settings() -> None:
    """
    Reset the singleton to None, forcing re-initialisation on next get_settings() call.

    For use in tests only.
    """
    global _settings  # noqa: PLW0603
    _settings = None
