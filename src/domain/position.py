"""
Position domain model.

Tracks the lifecycle of a trading position from open to close.
Enforces financial correctness for PnL calculations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from src.core.enums import MarginType, PositionSide, PositionStatus
from src.core.models import MutableModel, utc_now


class Position(MutableModel):
    """
    An open or closed trading position.

    PnL is calculated against mark price (unrealized) or exit price (realized).
    All arithmetic uses Decimal for precision.
    """

    # Identity
    position_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str
    side: PositionSide

    # Size
    quantity: Decimal          # Current open size
    entry_quantity: Decimal    # Total qty that entered

    # Prices
    entry_price: Decimal       # Average weighted entry price
    mark_price: Decimal        # Latest mark/market price
    exit_price: Decimal | None = None
    liquidation_price: Decimal | None = None

    # Margin
    margin_type: MarginType = MarginType.ISOLATED
    leverage: int = 1
    initial_margin: Decimal = Decimal("0")
    maintenance_margin: Decimal = Decimal("0")

    # Risk controls attached to position
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    trailing_stop_pct: Decimal | None = None
    breakeven_triggered: bool = False

    # Partial take-profit ladder, resolved to absolute (price, qty_fraction)
    # pairs at position-open time from ExecutionEngine's configured RR-based
    # levels (see ExecutionEngine._on_entry_filled). Empty when the feature
    # is disabled or the position predates it. partial_tp_taken tracks
    # indices into partial_tp_plan already submitted as a close order, so
    # evaluate_exit_actions/ExecutionEngine don't resubmit the same level on
    # every subsequent tick while price remains past it.
    partial_tp_plan: list[tuple[Decimal, Decimal]] = Field(default_factory=list)
    partial_tp_taken: list[int] = Field(default_factory=list)

    # PnL
    realized_pnl: Decimal = Decimal("0")
    funding_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")

    # Status
    status: PositionStatus = PositionStatus.OPEN

    # Timing
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    # Context
    strategy_id: str | None = None
    signal_id: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_prices(self) -> "Position":
        if self.entry_price <= Decimal("0"):
            raise ValueError(f"Entry price must be positive, got {self.entry_price}")
        if self.quantity < Decimal("0"):
            raise ValueError(f"Quantity must be non-negative, got {self.quantity}")
        return self

    # ─── PnL calculations ─────────────────────────────────────────────────────

    @property
    def unrealized_pnl(self) -> Decimal:
        """Current unrealized PnL at mark price."""
        if self.quantity == Decimal("0"):
            return Decimal("0")
        if self.side == PositionSide.LONG:
            return (self.mark_price - self.entry_price) * self.quantity
        elif self.side == PositionSide.SHORT:
            return (self.entry_price - self.mark_price) * self.quantity
        return Decimal("0")

    @property
    def total_pnl(self) -> Decimal:
        """Total PnL: realized + unrealized + funding - fees."""
        return self.realized_pnl + self.unrealized_pnl + self.funding_pnl - self.total_fees

    @property
    def net_realized_pnl(self) -> Decimal:
        """Realized PnL after fees."""
        return self.realized_pnl - self.total_fees

    @property
    def roi_pct(self) -> Decimal:
        """Return on initial margin as percentage."""
        if self.initial_margin == Decimal("0"):
            return Decimal("0")
        return (self.unrealized_pnl / self.initial_margin) * Decimal("100")

    @property
    def notional_value(self) -> Decimal:
        return self.quantity * self.mark_price

    @property
    def entry_notional(self) -> Decimal:
        return self.entry_quantity * self.entry_price

    @property
    def pnl_pct(self) -> Decimal:
        """Unrealized PnL as % of entry notional."""
        if self.entry_notional == Decimal("0"):
            return Decimal("0")
        return (self.unrealized_pnl / self.entry_notional) * Decimal("100")

    # ─── State updates ────────────────────────────────────────────────────────

    def update_mark_price(self, mark_price: Decimal) -> None:
        """Update the mark price and timestamp."""
        if mark_price <= Decimal("0"):
            raise ValueError(f"Mark price must be positive, got {mark_price}")
        self.mark_price = mark_price
        self.updated_at = utc_now()

    def add_partial_close(
        self,
        closed_qty: Decimal,
        exit_price: Decimal,
        fee: Decimal,
    ) -> Decimal:
        """
        Close part of the position.

        Returns realized PnL for the closed portion.
        """
        if closed_qty > self.quantity:
            raise ValueError(
                f"Cannot close {closed_qty} of position size {self.quantity}"
            )
        if exit_price <= Decimal("0"):
            raise ValueError(f"Exit price must be positive, got {exit_price}")

        if self.side == PositionSide.LONG:
            pnl = (exit_price - self.entry_price) * closed_qty
        else:
            pnl = (self.entry_price - exit_price) * closed_qty

        self.realized_pnl += pnl
        self.total_fees += fee
        self.quantity -= closed_qty
        self.updated_at = utc_now()

        if self.quantity <= Decimal("0"):
            self._close(exit_price)

        return pnl

    def _close(self, exit_price: Decimal) -> None:
        """Mark position as fully closed."""
        self.exit_price = exit_price
        self.quantity = Decimal("0")
        self.status = PositionStatus.CLOSED
        self.closed_at = utc_now()
        self.updated_at = utc_now()

    def add_funding(self, funding_payment: Decimal) -> None:
        """Record a funding payment (positive = received, negative = paid)."""
        self.funding_pnl += funding_payment
        self.updated_at = utc_now()

    def add_fee(self, fee: Decimal) -> None:
        """Accumulate trading fees."""
        self.total_fees += fee
        self.updated_at = utc_now()

    def set_breakeven(self) -> Decimal | None:
        """
        Calculate the breakeven stop-loss price accounting for fees.

        Returns the breakeven price or None if fees are zero.
        """
        if self.entry_quantity == Decimal("0"):
            return None
        fee_per_unit = self.total_fees / self.entry_quantity
        if self.side == PositionSide.LONG:
            breakeven = self.entry_price + fee_per_unit
        else:
            breakeven = self.entry_price - fee_per_unit
        self.stop_loss = breakeven
        self.breakeven_triggered = True
        self.updated_at = utc_now()
        return breakeven

    # ─── Risk helpers ─────────────────────────────────────────────────────────

    @property
    def distance_to_stop_pct(self) -> Decimal | None:
        """% distance from mark price to stop loss."""
        if self.stop_loss is None or self.mark_price == Decimal("0"):
            return None
        return abs(self.mark_price - self.stop_loss) / self.mark_price * Decimal("100")

    @property
    def distance_to_liquidation_pct(self) -> Decimal | None:
        """% distance from mark price to liquidation price."""
        if self.liquidation_price is None or self.mark_price == Decimal("0"):
            return None
        return abs(self.mark_price - self.liquidation_price) / self.mark_price * Decimal("100")

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN

    @property
    def is_closed(self) -> bool:
        return self.status == PositionStatus.CLOSED

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT

    @property
    def duration_seconds(self) -> float | None:
        """Duration the position has been open in seconds."""
        if self.closed_at:
            return (self.closed_at - self.opened_at).total_seconds()
        return (datetime.now(tz=UTC) - self.opened_at).total_seconds()

    def __str__(self) -> str:
        pnl_str = f"PnL:{self.unrealized_pnl:+.4f}" if self.is_open else f"PnL:{self.realized_pnl:+.4f}"
        return (
            f"Position({self.symbol} {self.side} qty={self.quantity} "
            f"entry={self.entry_price} {pnl_str} [{self.status}])"
        )

    def __repr__(self) -> str:
        return (
            f"Position(id={self.position_id!r}, symbol={self.symbol!r}, "
            f"side={self.side!r}, qty={self.quantity}, status={self.status!r})"
        )
