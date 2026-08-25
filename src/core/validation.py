"""
Reusable validation functions for the trading system.

All validators:
- Are pure functions with no side effects
- Raise specific exceptions from src.core.exceptions
- Accept flexible input types and coerce to Decimal where needed
- Include the failing value in the exception message for diagnosability
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from src.core.constants import BINANCE_MAX_CANDLES
from src.core.enums import Timeframe
from src.core.exceptions import (
    DataQualityError,
    InvalidPriceError,
    InvalidQuantityError,
    InvalidSymbolError,
    InvalidTimeframeError,
    ValidationError,
)

# Binance perpetual futures symbol pattern.
# Examples: BTCUSDT, ETHUSDT, 1000PEPEUSDT, SOLUSDT
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")


def validate_symbol(symbol: str) -> str:
    """
    Validate and normalise a trading symbol to uppercase.

    Args:
        symbol: Raw symbol string, e.g. "btcusdt", "ETHUSDT ".

    Returns:
        Normalised uppercase symbol.

    Raises:
        InvalidSymbolError: If the symbol is empty or contains invalid characters.
    """
    if not symbol or not symbol.strip():
        raise InvalidSymbolError(symbol)
    normalised = symbol.strip().upper()
    if not _SYMBOL_RE.match(normalised):
        raise InvalidSymbolError(normalised)
    return normalised


def validate_price(
    price: Decimal | float | str | int,
    symbol: str = "",
) -> Decimal:
    """
    Validate a price value — must be strictly positive.

    Args:
        price: Price value in any numeric form.
        symbol: Symbol context for error messages.

    Returns:
        Validated Decimal price.

    Raises:
        InvalidPriceError: If price is not a valid positive number.
    """
    try:
        d = Decimal(str(price))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidPriceError(price, symbol) from exc

    if d <= Decimal("0"):
        raise InvalidPriceError(d, symbol)
    return d


def validate_quantity(
    qty: Decimal | float | str | int,
    symbol: str = "",
) -> Decimal:
    """
    Validate a quantity value — must be strictly positive.

    Args:
        qty: Quantity value in any numeric form.
        symbol: Symbol context for error messages.

    Returns:
        Validated Decimal quantity.

    Raises:
        InvalidQuantityError: If quantity is not a valid positive number.
    """
    try:
        d = Decimal(str(qty))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidQuantityError(qty, symbol) from exc

    if d <= Decimal("0"):
        raise InvalidQuantityError(d, symbol)
    return d


def validate_timeframe(timeframe: str) -> Timeframe:
    """
    Validate and return a Timeframe enum.

    Args:
        timeframe: Timeframe string, e.g. "1h", "4h", "1d".

    Returns:
        Matching Timeframe enum value.

    Raises:
        InvalidTimeframeError: If the string does not match any supported timeframe.
    """
    try:
        return Timeframe(timeframe)
    except ValueError as exc:
        raise InvalidTimeframeError(timeframe) from exc


def validate_leverage(
    leverage: int | float | Decimal,
    max_leverage: Decimal,
    *,
    detail: str | None = None,
) -> Decimal:
    """
    Validate leverage is within permitted range [1, max_leverage].

    Args:
        detail: Optional override for the "exceeds maximum" message, for
            call sites that can give more actionable context (e.g. which
            config file/symbol set it, and why clamping isn't an option).
            No effect on the "invalid value" / "below 1" messages.

    Raises:
        ValidationError: If leverage is outside the valid range.
    """
    try:
        d = Decimal(str(leverage))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Invalid leverage value: {leverage!r}") from exc

    if d < Decimal("1"):
        raise ValidationError(f"Leverage must be >= 1, got {d}")
    if d > max_leverage:
        raise ValidationError(detail or f"Leverage {d} exceeds maximum {max_leverage}")
    return d


def validate_candle_limit(limit: int) -> int:
    """
    Validate that a candle request limit is within exchange bounds.

    Raises:
        ValidationError: If limit is outside [1, BINANCE_MAX_CANDLES].
    """
    if limit < 1:
        raise ValidationError(f"Candle limit must be >= 1, got {limit}")
    if limit > BINANCE_MAX_CANDLES:
        raise ValidationError(
            f"Candle limit {limit} exceeds maximum {BINANCE_MAX_CANDLES}"
        )
    return limit


def validate_timestamp_not_future(
    ts: datetime,
    tolerance_seconds: float = 5.0,
) -> datetime:
    """
    Raise if timestamp is more than tolerance_seconds in the future.

    Timezone-naive timestamps are assumed to be UTC.

    Raises:
        ValidationError: If timestamp is unreasonably in the future.
    """
    aware_ts = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    delta = (aware_ts - now).total_seconds()
    if delta > tolerance_seconds:
        raise ValidationError(
            f"Timestamp {aware_ts.isoformat()} is {delta:.1f}s in the future "
            f"(tolerance: {tolerance_seconds}s)"
        )
    return aware_ts


def validate_percentage(
    value: float | Decimal,
    name: str = "value",
) -> Decimal:
    """
    Validate a percentage is in the closed interval [0, 100].

    Raises:
        ValidationError: If value is outside [0, 100].
    """
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Cannot convert {value!r} to percentage") from exc

    if not (Decimal("0") <= d <= Decimal("100")):
        raise ValidationError(f"{name} must be in [0, 100], got {d}")
    return d


def validate_positive(value: Decimal, name: str) -> Decimal:
    """
    Validate a Decimal is strictly positive.

    Raises:
        ValidationError: If value is zero or negative.
    """
    if value <= Decimal("0"):
        raise ValidationError(f"{name} must be positive, got {value}")
    return value


def validate_non_negative(value: Decimal, name: str) -> Decimal:
    """
    Validate a Decimal is non-negative (>= 0).

    Raises:
        ValidationError: If value is negative.
    """
    if value < Decimal("0"):
        raise ValidationError(f"{name} must be non-negative, got {value}")
    return value


def validate_ohlcv_consistency(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
) -> None:
    """
    Validate that OHLCV values are internally consistent.

    Rules:
    - All prices must be positive
    - high >= max(open, close)
    - low <= min(open, close)
    - high >= low
    - volume >= 0

    Raises:
        DataQualityError: If any consistency check fails, with all errors listed.
    """
    errors: list[str] = []

    for label, price in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if price <= Decimal("0"):
            errors.append(f"{label} price {price} must be positive")

    if not errors:  # Only check relationships if prices are individually valid
        if high < low:
            errors.append(f"High ({high}) < Low ({low})")
        if high < open_:
            errors.append(f"High ({high}) < Open ({open_})")
        if high < close:
            errors.append(f"High ({high}) < Close ({close})")
        if low > open_:
            errors.append(f"Low ({low}) > Open ({open_})")
        if low > close:
            errors.append(f"Low ({low}) > Close ({close})")

    if volume < Decimal("0"):
        errors.append(f"Volume ({volume}) must be non-negative")

    if errors:
        raise DataQualityError("OHLCV validation failed: " + "; ".join(errors))


def validate_stop_loss(
    stop_loss: Decimal,
    entry_price: Decimal,
    is_long: bool,
) -> None:
    """
    Validate stop loss is on the correct side of the entry price.

    - Long: stop_loss must be strictly below entry_price
    - Short: stop_loss must be strictly above entry_price

    Raises:
        ValidationError: If stop loss is on the wrong side of entry.
    """
    if is_long and stop_loss >= entry_price:
        raise ValidationError(
            f"Long stop loss {stop_loss} must be strictly below entry {entry_price}"
        )
    if not is_long and stop_loss <= entry_price:
        raise ValidationError(
            f"Short stop loss {stop_loss} must be strictly above entry {entry_price}"
        )


def validate_take_profit(
    take_profit: Decimal,
    entry_price: Decimal,
    is_long: bool,
) -> None:
    """
    Validate take profit is on the correct side of the entry price.

    - Long: take_profit must be strictly above entry_price
    - Short: take_profit must be strictly below entry_price

    Raises:
        ValidationError: If take profit is on the wrong side of entry.
    """
    if is_long and take_profit <= entry_price:
        raise ValidationError(
            f"Long take profit {take_profit} must be strictly above entry {entry_price}"
        )
    if not is_long and take_profit >= entry_price:
        raise ValidationError(
            f"Short take profit {take_profit} must be strictly below entry {entry_price}"
        )


def validate_risk_reward(
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    is_long: bool,
    minimum_rr: Decimal,
) -> Decimal:
    """
    Calculate and validate the risk-reward ratio.

    Args:
        entry_price: Order entry price.
        stop_loss: Stop-loss price.
        take_profit: Take-profit price.
        is_long: True for long, False for short.
        minimum_rr: Minimum acceptable R:R ratio.

    Returns:
        Calculated risk-reward ratio.

    Raises:
        ValidationError: If R:R is below minimum or stop distance is zero.
    """
    stop_distance = abs(entry_price - stop_loss)
    if stop_distance == Decimal("0"):
        raise ValidationError("Stop distance is zero — cannot compute risk-reward")

    if is_long:
        reward = take_profit - entry_price
    else:
        reward = entry_price - take_profit

    if reward <= Decimal("0"):
        raise ValidationError(
            f"Take profit is on the wrong side: reward={reward}"
        )

    rr = reward / stop_distance
    if rr < minimum_rr:
        raise ValidationError(
            f"Risk-reward {rr:.2f} is below minimum {minimum_rr}"
        )
    return rr
