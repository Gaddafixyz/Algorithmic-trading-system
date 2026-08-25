"""
Trading signal domain model.

A Signal is the output of a strategy's analysis: a qualified trade opportunity
with all necessary parameters to execute and manage the trade.

Signals are immutable after creation. They expire after max_age_seconds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, model_validator

from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.core.models import StrictModel, utc_now
from src.core.validation import validate_stop_loss, validate_take_profit


class SignalComponent(StrictModel):
    """A contributing factor to the signal score."""

    name: str
    score: float
    weight: float
    description: str = ""


class Signal(StrictModel):
    """
    A qualified trade setup with entry, stop, and target parameters.

    The signal includes a composite confidence score (0-100) that reflects
    the strength of the confluence of confirming factors.
    """

    # Identity
    signal_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    strategy_id: str
    symbol: str
    timeframe: Timeframe

    # Direction
    direction: SignalDirection

    # Scoring
    score: float = Field(ge=0.0, le=100.0, description="Composite confluence score 0-100")
    strength: SignalStrength
    confidence: float = Field(ge=0.0, le=1.0, description="Model confidence 0-1")

    # Trade parameters
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    entry_zone_high: Decimal | None = None   # Acceptable entry range
    entry_zone_low: Decimal | None = None

    # Context
    timeframe_context: str = ""   # Higher TF narrative
    components: list[SignalComponent] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Timing
    generated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    max_age_seconds: float = 3600.0   # Default: valid for 1 hour

    # Market context at generation time
    atr: Decimal | None = None
    market_structure: str = ""

    @model_validator(mode="after")
    def validate_trade_params(self) -> "Signal":
        is_long = self.direction == SignalDirection.LONG
        try:
            validate_stop_loss(self.stop_loss, self.entry_price, is_long)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        try:
            validate_take_profit(self.take_profit, self.entry_price, is_long)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        return self

    # ─── Computed properties ──────────────────────────────────────────────────

    @property
    def risk_reward_ratio(self) -> Decimal:
        """Potential reward / potential risk."""
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        if risk == Decimal("0"):
            return Decimal("0")
        return reward / risk

    @property
    def stop_distance(self) -> Decimal:
        """Absolute distance from entry to stop loss."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def stop_distance_pct(self) -> Decimal:
        """Stop distance as % of entry price."""
        if self.entry_price == Decimal("0"):
            return Decimal("0")
        return (self.stop_distance / self.entry_price) * Decimal("100")

    @property
    def target_distance(self) -> Decimal:
        return abs(self.take_profit - self.entry_price)

    @property
    def is_long(self) -> bool:
        return self.direction == SignalDirection.LONG

    @property
    def is_short(self) -> bool:
        return self.direction == SignalDirection.SHORT

    def is_expired(self, at: datetime | None = None) -> bool:
        """True if signal has passed its expiry."""
        now = at or datetime.now(tz=UTC)
        if self.expires_at is not None:
            return now > self.expires_at
        age = (now - self.generated_at).total_seconds()
        return age > self.max_age_seconds

    def is_price_in_entry_zone(self, current_price: Decimal) -> bool:
        """True if current price is within the acceptable entry zone."""
        if self.entry_zone_high and self.entry_zone_low:
            return self.entry_zone_low <= current_price <= self.entry_zone_high
        # Fall back to within 0.5% of entry price
        tolerance = self.entry_price * Decimal("0.005")
        return abs(current_price - self.entry_price) <= tolerance

    def __str__(self) -> str:
        return (
            f"Signal({self.symbol} {self.direction} score={self.score:.1f} "
            f"entry={self.entry_price} SL={self.stop_loss} TP={self.take_profit} "
            f"RR={self.risk_reward_ratio:.2f})"
        )
