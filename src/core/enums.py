"""
Core enumerations for the trading system.

All enums use StrEnum for JSON serialization compatibility and
human-readable string values. Numeric values are avoided to prevent
fragile integer-to-meaning mappings across serialization boundaries.
"""

from __future__ import annotations

from enum import StrEnum, auto


class TradingEnvironment(StrEnum):
    """Deployment environment — controls exchange endpoints and safety guards."""

    PAPER = auto()
    TESTNET = auto()
    PRODUCTION = auto()


class OrderSide(StrEnum):
    """Order direction relative to the market."""

    BUY = auto()
    SELL = auto()


class OrderType(StrEnum):
    """Order execution type as supported by Binance USDT-M Futures."""

    MARKET = auto()
    LIMIT = auto()
    STOP_MARKET = auto()
    STOP_LIMIT = auto()
    TAKE_PROFIT_MARKET = auto()
    TAKE_PROFIT_LIMIT = auto()
    TRAILING_STOP_MARKET = auto()


class OrderStatus(StrEnum):
    """
    Lifecycle state of an order.

    State machine:
        PENDING → SUBMITTED → OPEN → PARTIALLY_FILLED → FILLED
                            → CANCELLED
                            → REJECTED
                            → EXPIRED
        Any non-terminal → FAILED (unrecoverable local error)
    """

    PENDING = auto()  # Created locally, not yet sent to exchange
    SUBMITTED = auto()  # Sent to exchange, awaiting acknowledgement
    OPEN = auto()  # Acknowledged by exchange, resting in order book
    PARTIALLY_FILLED = auto()  # Some quantity has been filled
    FILLED = auto()  # Fully filled — terminal
    CANCELLED = auto()  # Cancelled by user or exchange — terminal
    REJECTED = auto()  # Rejected by exchange — terminal
    EXPIRED = auto()  # Time-in-force expired — terminal
    FAILED = auto()  # Unrecoverable local error — terminal


class TimeInForce(StrEnum):
    """Order time-in-force policy."""

    GTC = auto()  # Good Till Cancel
    IOC = auto()  # Immediate Or Cancel
    FOK = auto()  # Fill Or Kill
    GTX = auto()  # Good Till Crossing (maker-only / post-only)


class PositionSide(StrEnum):
    """Position direction, used in hedge mode and for P&L calculation."""

    LONG = auto()
    SHORT = auto()
    BOTH = auto()  # One-way mode; Binance uses "BOTH" in one-way mode


class PositionStatus(StrEnum):
    """Position lifecycle status."""

    OPEN = auto()
    CLOSING = auto()  # Partial close in progress
    CLOSED = auto()


class SignalDirection(StrEnum):
    """Trading signal direction produced by the strategy engine."""

    LONG = auto()
    SHORT = auto()
    NEUTRAL = auto()


class SignalStrength(StrEnum):
    """Qualitative strength classification of a trading signal."""

    WEAK = auto()
    MODERATE = auto()
    STRONG = auto()
    VERY_STRONG = auto()


class Timeframe(StrEnum):
    """
    OHLCV candlestick timeframes supported by Binance Futures.

    Values match Binance API interval strings exactly.
    """

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MN1 = "1M"

    @property
    def seconds(self) -> int:
        """Duration of one candle in seconds."""
        _map: dict[str, int] = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1_800,
            "1h": 3_600,
            "2h": 7_200,
            "4h": 14_400,
            "6h": 21_600,
            "8h": 28_800,
            "12h": 43_200,
            "1d": 86_400,
            "3d": 259_200,
            "1w": 604_800,
            "1M": 2_592_000,
        }
        return _map[self.value]

    @property
    def milliseconds(self) -> int:
        """Duration of one candle in milliseconds."""
        return self.seconds * 1_000

    @property
    def label(self) -> str:
        """Human-readable label, e.g. '1 hour', '15 minutes'."""
        _labels: dict[str, str] = {
            "1m": "1 minute",
            "3m": "3 minutes",
            "5m": "5 minutes",
            "15m": "15 minutes",
            "30m": "30 minutes",
            "1h": "1 hour",
            "2h": "2 hours",
            "4h": "4 hours",
            "6h": "6 hours",
            "8h": "8 hours",
            "12h": "12 hours",
            "1d": "1 day",
            "3d": "3 days",
            "1w": "1 week",
            "1M": "1 month",
        }
        return _labels[self.value]


class MarketStructure(StrEnum):
    """
    Smart Money Concepts market structure classification.

    Based on the sequence of swing highs and swing lows:
    - BULLISH: Higher highs and higher lows
    - BEARISH: Lower highs and lower lows
    - RANGING: No clear directional sequence
    - UNKNOWN: Insufficient data for classification
    """

    BULLISH = auto()
    BEARISH = auto()
    RANGING = auto()
    UNKNOWN = auto()


class OrderBlockType(StrEnum):
    """Smart Money order block classification."""

    BULLISH = auto()  # Demand zone — last bearish candle before bullish BOS
    BEARISH = auto()  # Supply zone — last bullish candle before bearish BOS


class FVGType(StrEnum):
    """
    Fair Value Gap (FVG) / Imbalance classification.

    BULLISH: A gap left below price during rapid upward movement (c1.high < c3.low)
    BEARISH: A gap left above price during rapid downward movement (c1.low > c3.high)
    """

    BULLISH = auto()
    BEARISH = auto()


class LiquidityType(StrEnum):
    """Liquidity pool type based on its price location and formation."""

    EQUAL_HIGHS = auto()  # Multiple swing highs at nearly the same price
    EQUAL_LOWS = auto()  # Multiple swing lows at nearly the same price
    SWING_HIGH = auto()  # Single prominent swing high (sell-side liquidity above)
    SWING_LOW = auto()  # Single prominent swing low (buy-side liquidity below)
    BSL = auto()  # Buy-side liquidity (above price — resting stops of shorts)
    SSL = auto()  # Sell-side liquidity (below price — resting stops of longs)


class SessionType(StrEnum):
    """Major Forex/Crypto trading session."""

    ASIAN = auto()
    LONDON = auto()
    NEW_YORK = auto()
    OVERLAP = auto()  # London/New York overlap — highest liquidity
    OFF_HOURS = auto()


class KillZone(StrEnum):
    """
    ICT Kill Zones — high-probability trading windows.

    These time windows consistently produce institutional order flow
    and are therefore preferred for entries.
    """

    ASIAN_KZ = auto()  # 01:00–04:00 UTC
    LONDON_OPEN_KZ = auto()  # 07:00–09:00 UTC
    NEW_YORK_OPEN_KZ = auto()  # 12:00–14:00 UTC
    NEW_YORK_LUNCH_KZ = auto()  # 17:00–19:00 UTC (reduced activity)
    LONDON_CLOSE_KZ = auto()  # 15:00–16:00 UTC


class RiskLevel(StrEnum):
    """Risk assessment level for monitoring and alerting."""

    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()


class CircuitBreakerState(StrEnum):
    """
    Circuit breaker state machine.

    CLOSED → OPEN (on limit breach)
    OPEN → HALF_OPEN (after cooldown period)
    HALF_OPEN → CLOSED (on successful recovery)
    HALF_OPEN → OPEN (on further loss)
    """

    CLOSED = auto()  # Normal operation — orders permitted
    OPEN = auto()  # Halted — no new orders
    HALF_OPEN = auto()  # Testing recovery — limited orders


class AlertSeverity(StrEnum):
    """Monitoring alert severity levels, aligned with standard SRE practices."""

    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


class TradingMode(StrEnum):
    """Active trading mode — determines fill mechanics and capital type."""

    LIVE = auto()
    PAPER = auto()
    BACKTEST = auto()


class InstrumentType(StrEnum):
    """Financial instrument classification."""

    SPOT = auto()
    PERPETUAL = auto()
    FUTURES = auto()
    OPTION = auto()


class MarginType(StrEnum):
    """
    Futures margin mode.

    ISOLATED: Loss capped at allocated margin for each position.
    CROSS: All account balance used as margin across all positions.

    Default is ISOLATED for contained risk per position.
    """

    ISOLATED = auto()
    CROSS = auto()


class HealthStatus(StrEnum):
    """Component health state used by health checks and monitoring."""

    HEALTHY = auto()
    DEGRADED = auto()
    UNHEALTHY = auto()
    UNKNOWN = auto()


class ExchangeStatus(StrEnum):
    """Exchange connectivity status."""

    CONNECTED = auto()
    DISCONNECTED = auto()
    RECONNECTING = auto()
    RATE_LIMITED = auto()


class DataQuality(StrEnum):
    """Market data quality classification."""

    GOOD = auto()
    STALE = auto()
    MISSING = auto()
    CORRUPTED = auto()
