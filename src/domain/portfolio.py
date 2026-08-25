"""
Portfolio domain model.

Aggregates open positions, tracks portfolio-level risk metrics,
and provides a consolidated view of the trading book.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from src.core.enums import PositionSide
from src.core.models import MutableModel, utc_now
from src.domain.position import Position


class Portfolio(MutableModel):
    """
    Live portfolio state: positions, equity, and aggregate risk metrics.

    Single source of truth for what the system currently holds.
    """

    portfolio_id: str = "default"
    positions: dict[str, Position] = Field(default_factory=dict)  # position_id -> Position

    # Equity tracking
    initial_equity: Decimal = Decimal("0")
    current_equity: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")

    updated_at: datetime = Field(default_factory=utc_now)

    # ─── Position management ──────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        self.positions[position.position_id] = position
        self.updated_at = utc_now()

    def remove_position(self, position_id: str) -> Position | None:
        pos = self.positions.pop(position_id, None)
        self.updated_at = utc_now()
        return pos

    def get_position(self, position_id: str) -> Position | None:
        return self.positions.get(position_id)

    def get_positions_for_symbol(self, symbol: str) -> list[Position]:
        return [p for p in self.positions.values() if p.symbol == symbol and p.is_open]

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    # ─── Aggregate metrics ────────────────────────────────────────────────────

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.is_open)

    @property
    def total_unrealized_pnl(self) -> Decimal:
        return sum(
            (p.unrealized_pnl for p in self.positions.values() if p.is_open),
            Decimal("0"),
        )

    @property
    def total_notional(self) -> Decimal:
        return sum(
            (p.notional_value for p in self.positions.values() if p.is_open),
            Decimal("0"),
        )

    @property
    def net_exposure(self) -> Decimal:
        """Net long - short notional."""
        long_notional = sum(
            (
                p.notional_value
                for p in self.positions.values()
                if p.is_open and p.side == PositionSide.LONG
            ),
            Decimal("0"),
        )
        short_notional = sum(
            (
                p.notional_value
                for p in self.positions.values()
                if p.is_open and p.side == PositionSide.SHORT
            ),
            Decimal("0"),
        )
        return long_notional - short_notional

    @property
    def gross_exposure(self) -> Decimal:
        return self.total_notional

    @property
    def drawdown(self) -> Decimal:
        if self.peak_equity == Decimal("0"):
            return Decimal("0")
        return (self.peak_equity - self.current_equity) / self.peak_equity * Decimal("100")

    @property
    def symbols_traded(self) -> set[str]:
        return {p.symbol for p in self.positions.values() if p.is_open}

    def update_equity(self, equity: Decimal) -> None:
        self.current_equity = equity
        if equity > self.peak_equity:
            self.peak_equity = equity
        self.updated_at = utc_now()

    def has_position_for_symbol(self, symbol: str) -> bool:
        return any(p.symbol == symbol and p.is_open for p in self.positions.values())
