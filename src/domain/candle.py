"""
Candlestick (OHLCV) domain model.

Wraps the core OHLCV value object with trading-specific metadata.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from src.core.enums import Timeframe
from src.core.models import OHLCV, StrictModel
from src.core.validation import validate_ohlcv_consistency


class Candle(StrictModel):
    """
    OHLCV candle with symbol and timeframe context.

    Immutable after creation. All computed properties are deterministic.
    """

    symbol: str
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal = Decimal("0")
    open_time: datetime
    close_time: datetime
    is_closed: bool = True
    trade_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_prices(self) -> Candle:
        validate_ohlcv_consistency(self.open, self.high, self.low, self.close, self.volume)
        if self.close_time < self.open_time:
            raise ValueError("close_time must be >= open_time")
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

    def is_doji(self, threshold_pct: Decimal = Decimal("0.1")) -> bool:
        """True if body is < threshold_pct of range."""
        if self.range_size == Decimal("0"):
            return True
        return (self.body_size / self.range_size) < threshold_pct

    @property
    def upper_wick(self) -> Decimal:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> Decimal:
        return min(self.open, self.close) - self.low

    @property
    def body_top(self) -> Decimal:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> Decimal:
        return min(self.open, self.close)

    @property
    def mid_price(self) -> Decimal:
        return (self.high + self.low) / Decimal("2")

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")

    @property
    def body_pct(self) -> Decimal:
        """Body as % of total range."""
        if self.range_size == Decimal("0"):
            return Decimal("0")
        return (self.body_size / self.range_size) * Decimal("100")

    def to_ohlcv(self) -> OHLCV:
        """Convert to core OHLCV value object."""
        return OHLCV(
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            timestamp=self.open_time,
        )

    @classmethod
    def from_ohlcv(
        cls,
        ohlcv: OHLCV,
        symbol: str,
        timeframe: Timeframe,
        close_time: datetime,
    ) -> Candle:
        """Construct from a core OHLCV value object."""
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open=ohlcv.open,
            high=ohlcv.high,
            low=ohlcv.low,
            close=ohlcv.close,
            volume=ohlcv.volume,
            open_time=ohlcv.timestamp,
            close_time=close_time,
        )

    def __str__(self) -> str:
        direction = "🟢" if self.is_bullish else "🔴"
        return (
            f"{direction} {self.symbol} {self.timeframe} "
            f"O:{self.open} H:{self.high} L:{self.low} C:{self.close}"
        )
