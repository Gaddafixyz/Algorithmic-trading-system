"""
Exception hierarchy for the trading system.

Design principles:
- Specific > generic: catch the most specific exception possible.
- Every exception carries enough context to diagnose the problem.
- Financial exceptions always include symbol and amount where applicable.
- Never swallow exceptions silently.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class TradingSystemError(Exception):
    """Base exception for all trading system errors."""

    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, context={self.context})"


# ─── Configuration ──────────────────────────────────────────────────────────

class ConfigurationError(TradingSystemError):
    """Invalid or missing configuration."""


class MissingConfigurationError(ConfigurationError):
    """Required configuration key is absent."""

    def __init__(self, key: str, *, detail: str | None = None) -> None:
        # detail lets a call site supply a more actionable message (e.g.
        # which env vars to set) than the generic default below, while
        # still keeping the structured .key attribute and staying a
        # ConfigurationError for any existing broad `except
        # ConfigurationError` handler. Keyword-only, defaults to the
        # original behavior — backward compatible with any caller that
        # only ever passed `key`.
        message = detail or f"Missing required configuration: {key}"
        super().__init__(message, {"key": key})
        self.key = key


class InvalidConfigurationError(ConfigurationError):
    """Configuration value is invalid."""

    def __init__(self, key: str, value: Any, reason: str) -> None:
        super().__init__(
            f"Invalid configuration '{key}': {reason}",
            {"key": key, "value": value, "reason": reason},
        )


# ─── Validation ─────────────────────────────────────────────────────────────

class ValidationError(TradingSystemError):
    """Input validation failure."""


class InvalidSymbolError(ValidationError):
    """Symbol does not meet format requirements."""

    def __init__(self, symbol: str) -> None:
        super().__init__(f"Invalid symbol: {symbol!r}", {"symbol": symbol})


class InvalidPriceError(ValidationError):
    """Price is zero, negative, or otherwise invalid."""

    def __init__(self, price: Any, symbol: str = "") -> None:
        super().__init__(
            f"Invalid price {price} for {symbol or 'unknown symbol'}",
            {"price": price, "symbol": symbol},
        )


class InvalidQuantityError(ValidationError):
    """Quantity is zero, negative, or exceeds limits."""

    def __init__(self, quantity: Any, symbol: str = "") -> None:
        super().__init__(
            f"Invalid quantity {quantity} for {symbol or 'unknown symbol'}",
            {"quantity": quantity, "symbol": symbol},
        )


class InvalidOrderError(ValidationError):
    """Order parameters are invalid."""


class InvalidTimeframeError(ValidationError):
    """Unsupported or invalid timeframe."""

    def __init__(self, timeframe: str) -> None:
        super().__init__(f"Invalid timeframe: {timeframe!r}", {"timeframe": timeframe})


# ─── Exchange ────────────────────────────────────────────────────────────────

class ExchangeError(TradingSystemError):
    """Base class for exchange-related errors."""


class ExchangeConnectionError(ExchangeError):
    """Cannot connect to exchange."""


class ExchangeAuthenticationError(ExchangeError):
    """API key/secret invalid or permissions insufficient."""


class ExchangeRateLimitError(ExchangeError):
    """Rate limit exceeded."""

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            f"Rate limit exceeded. Retry after {retry_after_seconds}s.",
            {"retry_after_seconds": retry_after_seconds},
        )
        self.retry_after_seconds = retry_after_seconds


class ExchangeTimeoutError(ExchangeError):
    """Exchange request timed out."""

    def __init__(self, operation: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Exchange operation '{operation}' timed out after {timeout_seconds}s",
            {"operation": operation, "timeout_seconds": timeout_seconds},
        )


class OrderNotFoundError(ExchangeError):
    """Order ID does not exist on exchange."""

    def __init__(self, order_id: str, symbol: str) -> None:
        super().__init__(
            f"Order {order_id} not found for {symbol}",
            {"order_id": order_id, "symbol": symbol},
        )


class InsufficientBalanceError(ExchangeError):
    """Insufficient funds to place order."""

    def __init__(self, required: Decimal, available: Decimal, asset: str) -> None:
        super().__init__(
            f"Insufficient {asset}: required={required}, available={available}",
            {"required": str(required), "available": str(available), "asset": asset},
        )
        self.required = required
        self.available = available
        self.asset = asset


class ExchangeAPIError(ExchangeError):
    """Exchange returned an error response."""

    def __init__(self, code: int | str, message: str, raw: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"Exchange API error [{code}]: {message}",
            {"code": code, "message": message, "raw": raw},
        )
        self.code = code
        self.raw = raw


# ─── Market Data ─────────────────────────────────────────────────────────────

class MarketDataError(TradingSystemError):
    """Base class for market data errors."""


class StaleDataError(MarketDataError):
    """Market data is too old to be reliable."""

    def __init__(self, symbol: str, age_seconds: float, max_age_seconds: float) -> None:
        super().__init__(
            f"Stale data for {symbol}: age={age_seconds:.1f}s, max={max_age_seconds}s",
            {"symbol": symbol, "age_seconds": age_seconds, "max_age_seconds": max_age_seconds},
        )


class InsufficientDataError(MarketDataError):
    """Not enough data points for calculation."""

    def __init__(self, required: int, available: int, context: str = "") -> None:
        super().__init__(
            f"Insufficient data{' for ' + context if context else ''}: "
            f"required={required}, available={available}",
            {"required": required, "available": available, "context": context},
        )


class DataQualityError(MarketDataError):
    """Market data failed quality checks."""


# ─── Strategy ────────────────────────────────────────────────────────────────

class StrategyError(TradingSystemError):
    """Base class for strategy errors."""


class SignalGenerationError(StrategyError):
    """Failed to generate trading signal."""


class NoSignalError(StrategyError):
    """No signal was generated (not an error, but a control flow sentinel)."""

    def __init__(self, symbol: str, reason: str = "") -> None:
        super().__init__(
            f"No signal for {symbol}" + (f": {reason}" if reason else ""),
            {"symbol": symbol, "reason": reason},
        )


# ─── Risk ────────────────────────────────────────────────────────────────────

class RiskError(TradingSystemError):
    """Base class for risk management errors."""


class RiskLimitExceededError(RiskError):
    """A risk limit was breached."""

    def __init__(self, limit_name: str, current: float, limit: float) -> None:
        super().__init__(
            f"Risk limit '{limit_name}' exceeded: current={current:.4f}, limit={limit:.4f}",
            {"limit_name": limit_name, "current": current, "limit": limit},
        )
        self.limit_name = limit_name
        self.current = current
        self.limit = limit


class CircuitBreakerOpenError(RiskError):
    """Circuit breaker is open; trading is halted."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Circuit breaker open: {reason}", {"reason": reason})


class MaxPositionsExceededError(RiskError):
    """Maximum number of open positions reached."""

    def __init__(self, current: int, maximum: int) -> None:
        super().__init__(
            f"Max positions exceeded: current={current}, max={maximum}",
            {"current": current, "maximum": maximum},
        )


class DailyLossLimitError(RiskError):
    """Daily loss limit has been hit."""

    def __init__(self, loss_pct: float, limit_pct: float) -> None:
        super().__init__(
            f"Daily loss limit hit: loss={loss_pct:.2f}%, limit={limit_pct:.2f}%",
            {"loss_pct": loss_pct, "limit_pct": limit_pct},
        )


class PositionSizingError(RiskError):
    """Could not calculate valid position size."""


# ─── Execution ───────────────────────────────────────────────────────────────

class ExecutionError(TradingSystemError):
    """Base class for execution errors."""


class OrderSubmissionError(ExecutionError):
    """Failed to submit order to exchange."""


class DuplicateOrderError(ExecutionError):
    """Attempted to submit a duplicate order."""

    def __init__(self, client_order_id: str) -> None:
        super().__init__(
            f"Duplicate order detected: {client_order_id}",
            {"client_order_id": client_order_id},
        )


class TakeProfitPlacementError(ExecutionError):
    """
    Take-profit order placement failed after a successful stop-loss.

    Distinct from a general OrderSubmissionError/unprotected-position
    failure: the position IS protected (stop-loss is live) when this is
    raised. Callers should treat this as a degraded-but-safe condition
    (gains won't auto-lock-in) rather than the same emergency as a
    failed stop-loss, which leaves a leveraged position with unbounded
    downside.
    """

    def __init__(self, symbol: str, cause: Exception) -> None:
        super().__init__(
            f"Take-profit placement failed for {symbol} (stop-loss is in place): {cause}",
            {"symbol": symbol},
        )


class OrderStateError(ExecutionError):
    """Invalid order state transition."""

    def __init__(self, order_id: str, current_state: str, target_state: str) -> None:
        super().__init__(
            f"Invalid order state transition for {order_id}: "
            f"{current_state} -> {target_state}",
            {"order_id": order_id, "current_state": current_state, "target_state": target_state},
        )


class MaxRetriesExceededError(ExecutionError):
    """Maximum retry attempts exhausted."""

    def __init__(self, operation: str, attempts: int) -> None:
        super().__init__(
            f"Max retries ({attempts}) exceeded for: {operation}",
            {"operation": operation, "attempts": attempts},
        )


# ─── Persistence ─────────────────────────────────────────────────────────────

class PersistenceError(TradingSystemError):
    """Base class for database/storage errors."""


class RecordNotFoundError(PersistenceError):
    """Entity not found in storage."""

    def __init__(self, entity_type: str, identifier: str) -> None:
        super().__init__(
            f"{entity_type} not found: {identifier}",
            {"entity_type": entity_type, "identifier": identifier},
        )


class DuplicateRecordError(PersistenceError):
    """Unique constraint violated."""


# ─── Backtesting ─────────────────────────────────────────────────────────────

class BacktestError(TradingSystemError):
    """Base class for backtesting errors."""


class LookAheadBiasError(BacktestError):
    """Detected potential look-ahead bias in strategy."""


# ─── System ──────────────────────────────────────────────────────────────────

class SystemError(TradingSystemError):  # noqa: A001
    """General system-level error."""


class ShutdownError(SystemError):
    """Error during graceful shutdown."""


class InitializationError(SystemError):
    """Component failed to initialize."""

    def __init__(self, component: str, reason: str) -> None:
        super().__init__(
            f"Failed to initialize {component}: {reason}",
            {"component": component, "reason": reason},
        )
