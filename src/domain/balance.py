"""
Balance snapshot domain object.

Lightweight balance record used for paper trading and backtesting
where full account detail is not needed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from src.core.models import MutableModel, utc_now
from src.core.validation import validate_non_negative


class Balance(MutableModel):
    """Simple balance tracker for a single currency."""

    currency: str = "USDT"
    initial: Decimal
    current: Decimal
    reserved: Decimal = Decimal("0")  # Held for open orders

    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def available(self) -> Decimal:
        return self.current - self.reserved

    @property
    def total_pnl(self) -> Decimal:
        return self.current - self.initial

    @property
    def total_pnl_pct(self) -> Decimal:
        if self.initial == Decimal("0"):
            return Decimal("0")
        return (self.total_pnl / self.initial) * Decimal("100")

    def reserve(self, amount: Decimal) -> None:
        if amount > self.available:
            raise ValueError(f"Cannot reserve {amount}: only {self.available} available")
        self.reserved += amount
        self.updated_at = utc_now()

    def release(self, amount: Decimal) -> None:
        release_amt = min(amount, self.reserved)
        self.reserved -= release_amt
        self.updated_at = utc_now()

    def credit(self, amount: Decimal) -> None:
        # validate_non_negative sat unused in core/validation.py despite
        # being an exact match for this check — was a hand-rolled
        # `if amount < 0: raise ValueError(...)` before this.
        validate_non_negative(amount, "Credit amount")
        self.current += amount
        self.updated_at = utc_now()

    def debit(self, amount: Decimal) -> None:
        if amount > self.available:
            raise ValueError(f"Insufficient balance: need {amount}, available {self.available}")
        self.current -= amount
        self.updated_at = utc_now()
