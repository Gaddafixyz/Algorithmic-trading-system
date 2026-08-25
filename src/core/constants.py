"""
System-wide constants for the trading system.

Design rule: if a value appears in more than one place, it belongs here.
Named constants prevent magic numbers and make intent explicit.

All monetary constants use Decimal-compatible string literals.
"""

from __future__ import annotations

from decimal import Decimal

# ─── Exchange ────────────────────────────────────────────────────────────────

# Binance USDT-M Futures REST endpoints
BINANCE_FUTURES_MAINNET_REST = "https://fapi.binance.com"
BINANCE_FUTURES_TESTNET_REST = "https://testnet.binancefuture.com"

# Binance USDT-M Futures WebSocket base URLs
BINANCE_FUTURES_MAINNET_WS = "wss://fstream.binance.com"
BINANCE_FUTURES_TESTNET_WS = "wss://stream.binancefuture.com"

# Bybit V5 splits market data and account data across two endpoints
# (unlike Binance's single combined-stream endpoint).
BYBIT_MAINNET_PUBLIC_WS = "wss://stream.bybit.com/v5/public/linear"
BYBIT_TESTNET_PUBLIC_WS = "wss://stream-testnet.bybit.com/v5/public/linear"
BYBIT_MAINNET_PRIVATE_WS = "wss://stream.bybit.com/v5/private"
BYBIT_TESTNET_PRIVATE_WS = "wss://stream-testnet.bybit.com/v5/private"

# Binance API defaults
BINANCE_RECV_WINDOW_MS: int = 5_000       # Timestamp tolerance window (ms)
BINANCE_MAX_CANDLES: int = 1_500          # Max klines per REST request
BINANCE_RATE_LIMIT_WEIGHT: int = 2_400   # Max request weight per minute
BINANCE_LISTEN_KEY_REFRESH_MINUTES: int = 30  # User-data stream key renewal interval

# Maker/taker fee rates for Binance Futures (VIP 0, standard)
BINANCE_MAKER_FEE_RATE: Decimal = Decimal("0.0002")   # 0.02%
BINANCE_TAKER_FEE_RATE: Decimal = Decimal("0.0004")   # 0.04%

# ─── Position Sizing ─────────────────────────────────────────────────────────

# Default fixed-fractional risk per trade
DEFAULT_RISK_PCT: Decimal = Decimal("1.0")

# Maximum fraction of equity to allocate to a single position
MAX_POSITION_PCT_OF_EQUITY: Decimal = Decimal("20.0")

# Maximum leverage allowed (exchange limit is 125x, but we cap conservatively)
MAX_LEVERAGE: int = 20

# Kelly Criterion fraction (25% fractional Kelly reduces variance)
KELLY_FRACTION: Decimal = Decimal("0.25")

# Minimum fee rate used in position sizing to prevent oversizing near fee breakeven
MIN_FEE_RATE: Decimal = Decimal("0.0004")

# ─── Risk Limits ─────────────────────────────────────────────────────────────

DEFAULT_MAX_DAILY_LOSS_PCT: Decimal = Decimal("5.0")
DEFAULT_MAX_DRAWDOWN_PCT: Decimal = Decimal("15.0")
DEFAULT_MAX_OPEN_POSITIONS: int = 5
DEFAULT_MIN_RISK_REWARD: Decimal = Decimal("1.5")
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_MINUTES: int = 60

# ─── Market Data ─────────────────────────────────────────────────────────────

# Minimum candles required before any SMC analysis is attempted
SMC_MIN_CANDLES: int = 100

# Swing point lookback — must dominate this many bars on each side
SMC_SWING_LOOKBACK: int = 5

# Maximum age of an order block in bars before it is considered invalid
SMC_OB_MAX_AGE_BARS: int = 100

# FVG minimum size as multiple of ATR (smaller FVGs are noise)
SMC_FVG_MIN_ATR_MULTIPLE: Decimal = Decimal("0.3")

# Equal highs/lows proximity threshold (as % of price)
SMC_EQUAL_LEVEL_TOLERANCE_PCT: Decimal = Decimal("0.15")

# Premium zone begins at this Fibonacci level (above equilibrium)
SMC_PREMIUM_ZONE: Decimal = Decimal("0.618")

# Discount zone ends at this Fibonacci level (below equilibrium)
SMC_DISCOUNT_ZONE: Decimal = Decimal("0.382")

# Optimal Trade Entry zone (OTE): 61.8%–78.6% retracement
SMC_OTE_LOW: Decimal = Decimal("0.618")
SMC_OTE_HIGH: Decimal = Decimal("0.786")

# ─── Indicators ──────────────────────────────────────────────────────────────

ATR_PERIOD: int = 14
RSI_PERIOD: int = 14
RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0
EMA_FAST_PERIOD: int = 12
EMA_SLOW_PERIOD: int = 26
MACD_SIGNAL_PERIOD: int = 9
BB_PERIOD: int = 20
BB_STD_DEV: float = 2.0

# ATR multiplier for initial stop-loss placement
ATR_STOP_MULTIPLIER: Decimal = Decimal("1.5")

# ─── Candle Cache ────────────────────────────────────────────────────────────

# Default candle buffer size per symbol/timeframe
CANDLE_BUFFER_SIZE: int = 500

# Maximum age before a ticker is considered stale
TICKER_TTL_SECONDS: float = 30.0

# Mark price TTL before the system stops trading
MARK_PRICE_TTL_SECONDS: float = 10.0

# ─── WebSocket ───────────────────────────────────────────────────────────────

WS_PING_INTERVAL_SECONDS: float = 20.0
WS_RECONNECT_DELAY_SECONDS: float = 5.0
WS_MAX_RECONNECT_ATTEMPTS: int = 10
WS_CONNECTION_TIMEOUT_SECONDS: float = 30.0

# ─── HTTP Client ─────────────────────────────────────────────────────────────

HTTP_TIMEOUT_SECONDS: float = 10.0
HTTP_MAX_RETRIES: int = 3
HTTP_RETRY_BACKOFF_BASE: float = 1.5
HTTP_RETRY_BACKOFF_MAX: float = 30.0

# ─── Database ────────────────────────────────────────────────────────────────

DB_POOL_SIZE: int = 5
DB_MAX_OVERFLOW: int = 10
DB_POOL_TIMEOUT_SECONDS: int = 30
DB_ECHO_SQL: bool = False

# ─── Monitoring ──────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL_SECONDS: float = 60.0
WATCHDOG_TIMEOUT_SECONDS: float = 120.0
HEALTH_CHECK_INTERVAL_SECONDS: float = 30.0
METRICS_PORT: int = 9090
DASHBOARD_PORT: int = 8080

# How often ReconciliationService.reconcile() runs against the exchange.
# This is the safety net that catches fills/closures missed by the
# user-data WS (gaps, dropped connections) — without a periodic scheduler
# calling it, the entire exchange-sync safety net is dead and a process
# restart loses all state with no way to recover it.
RECONCILE_INTERVAL_SECONDS: float = 60.0

# How often the account equity is re-synced from the exchange via
# BinanceAccountService.get_equity() after the initial startup sync.
# RiskManager's position-sizing and drawdown checks key off
# risk_manager.equity, so a stale value (e.g. after a large deposit,
# withdrawal, or funding payment) can silently mis-size new entries
# until the next resync.
EQUITY_SYNC_INTERVAL_SECONDS: float = 300.0

# How often a MetricSnapshotORM row is persisted via MetricRepository.
# MetricRepository/MetricSnapshotORM existed but were never invoked from
# anywhere in the live path — no periodic performance snapshot was ever
# written, so historical equity/drawdown/win-rate trends were unrecoverable
# after a restart (only the current in-memory RiskManager/PositionManager
# stats, which reset to zero on every process start).
METRIC_SNAPSHOT_INTERVAL_SECONDS: float = 300.0
PORTFOLIO_RISK_CHECK_INTERVAL_SECONDS: float = 60.0

# How often PortfolioService.check_rebalance() is evaluated against live
# positions. PortfolioService/PortfolioAllocator/PortfolioRebalancer existed
# but had no caller anywhere in the live path — target allocation vs. actual
# notional per symbol was never compared, so drift beyond the configured
# threshold (e.g. one symbol growing to dominate the book purely from price
# moves) would never surface anywhere short of an operator eyeballing the
# dashboard. Detection-only (see PortfolioRebalancer docstring) — this never
# places or cancels an order itself, only logs so an operator or a future
# scheduled task can act on it.
REBALANCE_CHECK_INTERVAL_SECONDS: float = 300.0

# Alert cooldown — suppress duplicate alerts for this period
ALERT_COOLDOWN_SECONDS: float = 1_800.0  # 30 minutes
ALERT_EVAL_INTERVAL_SECONDS: float = 60.0   # How often AlertManager.evaluate() runs

# ─── Backtesting ─────────────────────────────────────────────────────────────

# Walk-forward optimisation: fraction of data used for in-sample (IS)
WALK_FORWARD_IS_FRACTION: float = 0.70

# Minimum trades required for statistical significance
MIN_TRADES_FOR_SIGNIFICANCE: int = 30

# Default Sharpe ratio annualisation factor (252 trading days for crypto ~365)
SHARPE_ANNUALISATION_FACTOR: float = 365.0

# ─── Precision ───────────────────────────────────────────────────────────────

# Decimal places used when rounding prices for display
PRICE_DISPLAY_PRECISION: int = 8

# Decimal places used when rounding quantities for display
QUANTITY_DISPLAY_PRECISION: int = 8

# ─── Sentinel values ─────────────────────────────────────────────────────────

ZERO: Decimal = Decimal("0")
ONE: Decimal = Decimal("1")
ONE_HUNDRED: Decimal = Decimal("100")

# ─── Compatibility aliases ────────────────────────────────────────────────────
# These names are used by base_client.py and websocket.py; defined here so the
# canonical values live in one place.

DEFAULT_HTTP_TIMEOUT: float = HTTP_TIMEOUT_SECONDS
DEFAULT_RETRY_ATTEMPTS: int = HTTP_MAX_RETRIES
DEFAULT_RETRY_BASE_DELAY: float = HTTP_RETRY_BACKOFF_BASE
DEFAULT_RETRY_MAX_DELAY: float = HTTP_RETRY_BACKOFF_MAX

DEFAULT_WS_PING_INTERVAL: float = WS_PING_INTERVAL_SECONDS
DEFAULT_WS_RECONNECT_DELAY: float = WS_RECONNECT_DELAY_SECONDS
DEFAULT_WS_MAX_RECONNECT_DELAY: float = 60.0  # seconds; caps exponential backoff

# These names are used by indicators, smart_money, market_data.cache, and
# analytics modules; defined here as aliases so the canonical values above
# remain the single source of truth.

DEFAULT_RSI_PERIOD: int = RSI_PERIOD
DEFAULT_ATR_PERIOD: int = ATR_PERIOD
DEFAULT_BB_PERIOD: int = BB_PERIOD
DEFAULT_BB_STD: float = BB_STD_DEV
DEFAULT_EMA_FAST: int = EMA_FAST_PERIOD
DEFAULT_EMA_SLOW: int = EMA_SLOW_PERIOD
DEFAULT_MACD_SIGNAL: int = MACD_SIGNAL_PERIOD

SMC_FVG_MIN_SIZE_ATR_MULT: Decimal = SMC_FVG_MIN_ATR_MULTIPLE

# Candle cache buffer size / TTLs used by market_data.cache
CANDLE_CACHE_MAX_ITEMS: int = CANDLE_BUFFER_SIZE
CANDLE_CACHE_TTL_SECONDS: float = 60.0
TICKER_CACHE_TTL_SECONDS: float = TICKER_TTL_SECONDS

# Sharpe/analytics defaults
ANNUAL_TRADING_DAYS: float = SHARPE_ANNUALISATION_FACTOR
RISK_FREE_RATE: float = 0.0  # Assume 0% risk-free rate for crypto by default
