"""
Trade domain model.

A Trade is the closed-loop record of a complete round-trip:
position opened → position closed. It aggregates all fills,
fees, and PnL into a single auditable record.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from src.core.enums import PositionSide, Timeframe
from src.core.models import StrictModel


class Trade(StrictModel):
    """
    Immutable record of a completed round-trip trade.

    Created when a position closes. Used for analytics,
    reporting, and persistence.
    """

    trade_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    position_id: str
    signal_id: str | None = None
    strategy_id: str | None = None

    symbol: str
    side: PositionSide
    timeframe: Timeframe | None = None

    # Quantities
    quantity: Decimal
    entry_quantity: Decimal  # May differ if partially scaled in

    # Prices
    entry_price: Decimal  # Volume-weighted average entry
    exit_price: Decimal  # Volume-weighted average exit

    # PnL
    gross_pnl: Decimal
    fees: Decimal
    funding_pnl: Decimal = Decimal("0")

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees + self.funding_pnl

    @property
    def net_pnl_pct(self) -> Decimal:
        """Net PnL as % of entry notional."""
        notional = self.entry_price * self.quantity
        if notional == Decimal("0"):
            return Decimal("0")
        return (self.net_pnl / notional) * Decimal("100")

    @property
    def gross_pnl_pct(self) -> Decimal:
        notional = self.entry_price * self.quantity
        if notional == Decimal("0"):
            return Decimal("0")
        return (self.gross_pnl / notional) * Decimal("100")

    @property
    def risk_reward_achieved(self) -> Decimal | None:
        if self.initial_risk is None or self.initial_risk == Decimal("0"):
            return None
        return self.net_pnl / self.initial_risk

    # Trade metadata
    initial_risk: Decimal | None = None  # Capital at risk when opened
    max_adverse_excursion: Decimal | None = None  # MAE
    max_favorable_excursion: Decimal | None = None  # MFE

    # Timing
    opened_at: datetime
    closed_at: datetime
    duration_seconds: float = Field(default=0.0, ge=0)

    leverage: int = Field(default=1, ge=1)
    tags: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""

    @model_validator(mode="after")
    def compute_duration(self) -> Trade:
        if self.duration_seconds == 0.0:
            object.__setattr__(
                self,
                "duration_seconds",
                (self.closed_at - self.opened_at).total_seconds(),
            )
        return self

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > Decimal("0")

    @property
    def is_loser(self) -> bool:
        return self.net_pnl < Decimal("0")

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    @property
    def duration_hours(self) -> float:
        return self.duration_seconds / 3600.0

    @classmethod
    def from_position(
        cls,
        position_id: str,
        symbol: str,
        side: PositionSide,
        quantity: Decimal,
        entry_price: Decimal,
        exit_price: Decimal,
        gross_pnl: Decimal,
        fees: Decimal,
        opened_at: datetime,
        closed_at: datetime,
        *,
        signal_id: str | None = None,
        strategy_id: str | None = None,
        timeframe: Timeframe | None = None,
        initial_risk: Decimal | None = None,
        leverage: int = 1,
        funding_pnl: Decimal = Decimal("0"),
        tags: dict[str, Any] | None = None,
    ) -> Trade:
        return cls(
            position_id=position_id,
            signal_id=signal_id,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            timeframe=timeframe,
            quantity=quantity,
            entry_quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=gross_pnl,
            fees=fees,
            funding_pnl=funding_pnl,
            initial_risk=initial_risk,
            opened_at=opened_at,
            closed_at=closed_at,
            leverage=leverage,
            tags=tags or {},
        )

    def __str__(self) -> str:
        return (
            f"Trade({self.symbol} {self.side} qty={self.quantity} "
            f"entry={self.entry_price} exit={self.exit_price} "
            f"PnL={self.net_pnl:+.4f})"
        )
