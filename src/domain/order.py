"""
Order domain model.

Represents the full lifecycle of a trading order from local creation
through exchange acknowledgement to terminal state (filled, cancelled, etc.).

Invariants enforced:
- An order can only transition to valid next states (state machine).
- Financial quantities are Decimal for precision.
- client_order_id is set at creation and immutable (idempotency key).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator, model_validator

from src.core.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from src.core.exceptions import OrderStateError
from src.core.models import MutableModel, utc_now

# Valid state transitions
_VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.SUBMITTED, OrderStatus.FAILED, OrderStatus.CANCELLED},
    OrderStatus.SUBMITTED: {
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.FAILED,
    },
    OrderStatus.OPEN: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    # Terminal states
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.FAILED: set(),
}


class Fill(MutableModel):
    """A single order fill (partial or complete)."""

    fill_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    timestamp: datetime = Field(default_factory=utc_now)
    is_maker: bool = False


class Order(MutableModel):
    """
    Trading order with full lifecycle tracking.

    Mutable: status, filled quantities, exchange IDs, and fills
    update as the order progresses.
    """

    # Identity
    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    client_order_id: str = Field(default_factory=lambda: f"ts_{uuid.uuid4().hex[:16]}")

    # What we're trading
    symbol: str
    side: OrderSide
    order_type: OrderType
    position_side: PositionSide = PositionSide.BOTH

    # Quantities
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")

    # Prices
    price: Decimal | None = None  # Limit price
    stop_price: Decimal | None = None  # For stop orders
    average_fill_price: Decimal | None = None

    # Conditions
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    close_position: bool = False

    # State
    status: OrderStatus = OrderStatus.PENDING

    # Exchange response
    exchange_order_id: str | None = None

    # Fees
    total_fee: Decimal = Decimal("0")
    fee_asset: str = "USDT"

    # Fills
    fills: list[Fill] = Field(default_factory=list)

    # Timing
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None

    # Context
    signal_id: str | None = None
    strategy_id: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)

    # Error tracking
    error_message: str | None = None
    rejection_reason: str | None = None

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError(f"Order quantity must be positive, got {v}")
        return v

    @field_validator("price", "stop_price", mode="before")
    @classmethod
    def price_positive_if_set(cls, v: Any) -> Any:
        if v is not None:
            d = Decimal(str(v))
            if d <= Decimal("0"):
                raise ValueError(f"Price must be positive, got {d}")
            return d
        return v

    @model_validator(mode="after")
    def validate_order_type_constraints(self) -> Order:
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT orders require a price")
        if self.order_type in (OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT):
            if self.price is None or self.stop_price is None:
                raise ValueError(f"{self.order_type} requires both price and stop_price")
        if self.order_type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if self.stop_price is None:
                raise ValueError(f"{self.order_type} requires stop_price")
        return self

    # ─── State machine ────────────────────────────────────────────────────────

    def transition_to(self, new_status: OrderStatus) -> None:
        """
        Apply a status transition.

        Raises OrderStateError if the transition is invalid.
        """
        valid = _VALID_TRANSITIONS.get(self.status, set())
        if new_status not in valid:
            raise OrderStateError(self.order_id, self.status.value, new_status.value)
        self.status = new_status
        self.updated_at = utc_now()

        if new_status == OrderStatus.SUBMITTED:
            self.submitted_at = utc_now()
        elif new_status == OrderStatus.FILLED:
            self.filled_at = utc_now()

    def add_fill(self, fill: Fill) -> None:
        """Record a fill and update aggregate quantities."""
        self.fills.append(fill)
        self.filled_quantity += fill.quantity
        self.total_fee += fill.fee

        # Update average fill price
        total_cost = sum(f.price * f.quantity for f in self.fills)
        if self.filled_quantity > Decimal("0"):
            self.average_fill_price = total_cost / self.filled_quantity

        self.updated_at = utc_now()

        # Auto-transition
        if self.filled_quantity >= self.quantity:
            if self.status not in (OrderStatus.FILLED,):
                self.transition_to(OrderStatus.FILLED)
        elif self.filled_quantity > Decimal("0"):
            if self.status == OrderStatus.OPEN:
                self.transition_to(OrderStatus.PARTIALLY_FILLED)

    # ─── Computed properties ──────────────────────────────────────────────────

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def fill_percentage(self) -> Decimal:
        if self.quantity == Decimal("0"):
            return Decimal("0")
        return (self.filled_quantity / self.quantity) * Decimal("100")

    @property
    def is_open(self) -> bool:
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        )

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        return self.side == OrderSide.SELL

    @property
    def notional_value(self) -> Decimal:
        """Approximate notional value using limit price or average fill price."""
        price = self.average_fill_price or self.price
        if price is None:
            return Decimal("0")
        return self.quantity * price

    @property
    def realized_cost(self) -> Decimal:
        """Actual cost of fills (quantity * average fill price + fees)."""
        if self.average_fill_price is None:
            return Decimal("0")
        return self.filled_quantity * self.average_fill_price + self.total_fee

    def __str__(self) -> str:
        return (
            f"Order({self.client_order_id} {self.side} {self.quantity} {self.symbol} "
            f"@ {self.price or 'MARKET'} [{self.status}])"
        )

    def __repr__(self) -> str:
        return (
            f"Order(id={self.order_id!r}, symbol={self.symbol!r}, "
            f"side={self.side!r}, status={self.status!r})"
        )
