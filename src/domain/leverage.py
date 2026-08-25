"""Leverage configuration per symbol."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator

from src.core.enums import MarginType
from src.core.models import MutableModel


class LeverageConfig(MutableModel):
    """Per-symbol leverage and margin configuration."""

    symbol: str
    leverage: int = Field(ge=1, le=125)
    margin_type: MarginType = MarginType.ISOLATED
    max_notional: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()

    @property
    def effective_margin_rate(self) -> Decimal:
        """Initial margin rate = 1 / leverage."""
        return Decimal("1") / Decimal(str(self.leverage))
