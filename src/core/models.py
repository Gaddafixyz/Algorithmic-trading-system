"""
Shared value objects and base model infrastructure.

These are pure data containers with validation.
Domain-specific models live in src/domain/.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(tz=UTC)


def new_uuid() -> str:
    """Generate a new UUID4 string."""
    return str(uuid.uuid4())


class StrictModel(BaseModel):
    """Base model with strict validation and immutability."""

    model_config = ConfigDict(
        frozen=True,
        validate_assignment=True,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
    )

    def model_copy_update(self, **kwargs: Any) -> "StrictModel":
        """Return a copy with updated fields (works with frozen models)."""
        return self.model_copy(update=kwargs)


class MutableModel(BaseModel):
    """Base model that allows mutation (for stateful entities)."""

    model_config = ConfigDict(
        validate_assignment=True,
        populate_by_name=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
    )


class Price(StrictModel):
    """Validated non-negative price value."""

    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        try:
            d = Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError(f"Cannot convert {v!r} to Decimal") from e
        if d < Decimal("0"):
            raise ValueError(f"Price must be non-negative, got {d}")
        return d

    def __gt__(self, other: "Price") -> bool:
        return self.value > other.value

    def __ge__(self, other: "Price") -> bool:
        return self.value >= other.value

    def __lt__(self, other: "Price") -> bool:
        return self.value < other.value

    def __le__(self, other: "Price") -> bool:
        return self.value <= other.value

    def __mul__(self, other: "Decimal | float | int") -> "Price":
        return Price(value=self.value * Decimal(str(other)))

    def __truediv__(self, other: "Decimal | float | int") -> "Price":
        return Price(value=self.value / Decimal(str(other)))

    def __add__(self, other: "Price") -> "Price":
        return Price(value=self.value + other.value)

    def __sub__(self, other: "Price") -> "Price":
        return Price(value=self.value - other.value)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"Price({self.value})"


class Quantity(StrictModel):
    """Validated positive quantity."""

    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        try:
            d = Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError(f"Cannot convert {v!r} to Decimal") from e
        if d <= Decimal("0"):
            raise ValueError(f"Quantity must be positive, got {d}")
        return d

    def __mul__(self, other: "Decimal | float | int | Price") -> Decimal:
        if isinstance(other, Price):
            return self.value * other.value
        return self.value * Decimal(str(other))

    def __str__(self) -> str:
        return str(self.value)


class Percentage(StrictModel):
    """Percentage value between 0 and 100."""

    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def coerce_decimal(cls, v: Any) -> Decimal:
        try:
            d = Decimal(str(v))
        except InvalidOperation as e:
            raise ValueError(f"Cannot convert {v!r} to Decimal") from e
        if not (Decimal("0") <= d <= Decimal("100")):
            raise ValueError(f"Percentage must be 0-100, got {d}")
        return d

    @property
    def as_fraction(self) -> Decimal:
        return self.value / Decimal("100")

    def __str__(self) -> str:
        return f"{self.value}%"


class OHLCV(StrictModel):
    """OHLCV candle data point."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timestamp: datetime

    @model_validator(mode="after")
    def validate_ohlcv(self) -> "OHLCV":
        if self.high < self.low:
            raise ValueError(f"High ({self.high}) < Low ({self.low})")
        if self.high < self.open or self.high < self.close:
            raise ValueError("High must be >= open and close")
        if self.low > self.open or self.low > self.close:
            raise ValueError("Low must be <= open and close")
        if self.volume < Decimal("0"):
            raise ValueError("Volume must be non-negative")
        return self

    @property
    def body_size(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range_size(self) -> Decimal:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> Decimal:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> Decimal:
        return min(self.open, self.close) - self.low

    @property
    def mid_price(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")


class Tick(StrictModel):
    """Real-time price tick."""

    symbol: str
    price: Decimal
    quantity: Decimal
    timestamp: datetime
    is_buyer_maker: bool = False


class BookLevel(StrictModel):
    """Single level in an order book."""

    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


class OrderBook(StrictModel):
    """Snapshot of order book depth."""

    symbol: str
    bids: list[BookLevel]   # Sorted descending by price
    asks: list[BookLevel]   # Sorted ascending by price
    timestamp: datetime
    last_update_id: int

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid_price(self) -> Decimal | None:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / Decimal("2")
        return None


class Ticker(StrictModel):
    """24h ticker statistics."""

    symbol: str
    price: Decimal
    price_change_24h: Decimal
    price_change_pct_24h: Decimal
    high_24h: Decimal
    low_24h: Decimal
    volume_24h: Decimal
    quote_volume_24h: Decimal
    timestamp: datetime
    open_interest: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None


class FundingRate(StrictModel):
    """Perpetual futures funding rate."""

    symbol: str
    rate: Decimal
    next_funding_time: datetime
    timestamp: datetime


class PnL(StrictModel):
    """Profit and loss summary."""

    realized: Decimal = Decimal("0")
    unrealized: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized - self.fees

    @property
    def net_realized(self) -> Decimal:
        return self.realized - self.fees


class TimestampedValue(StrictModel):
    """A decimal value with a timestamp."""

    value: Decimal
    timestamp: datetime = Field(default_factory=utc_now)


class SymbolFilter(StrictModel):
    """Exchange trading rules for a symbol."""

    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_notional: Decimal

    def round_quantity(self, qty: Decimal) -> Decimal:
        """Round quantity to exchange step size."""
        if self.step_size == Decimal("0"):
            return qty
        steps = (qty / self.step_size).to_integral_value()
        return steps * self.step_size

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to exchange tick size."""
        if self.tick_size == Decimal("0"):
            return price
        ticks = (price / self.tick_size).to_integral_value()
        return ticks * self.tick_size

    def validate_order(self, qty: Decimal, price: Decimal) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if qty < self.min_qty:
            errors.append(f"Quantity {qty} < min {self.min_qty}")
        if qty > self.max_qty:
            errors.append(f"Quantity {qty} > max {self.max_qty}")
        notional = qty * price
        if notional < self.min_notional:
            errors.append(f"Notional {notional} < min {self.min_notional}")
        return errors
