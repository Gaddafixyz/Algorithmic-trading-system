"""
Instrument domain object.

Represents a tradeable financial instrument (spot, perpetual, futures).
Encapsulates exchange-specific trading rules and precision.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from src.core.enums import InstrumentType, MarginType
from src.core.models import StrictModel


class Instrument(StrictModel):
    """
    A tradeable instrument on an exchange.

    Contains all exchange-enforced constraints required to build valid orders.
    """

    symbol: str
    base_asset: str
    quote_asset: str
    instrument_type: InstrumentType = InstrumentType.PERPETUAL

    # Price constraints
    tick_size: Decimal = Field(gt=Decimal("0"))
    min_price: Decimal = Field(ge=Decimal("0"))
    max_price: Decimal = Field(gt=Decimal("0"))

    # Quantity constraints
    step_size: Decimal = Field(gt=Decimal("0"))
    min_qty: Decimal = Field(gt=Decimal("0"))
    max_qty: Decimal = Field(gt=Decimal("0"))

    # Notional constraint
    min_notional: Decimal = Field(ge=Decimal("0"))

    # Leverage
    max_leverage: int = Field(ge=1, le=125)
    default_leverage: int = Field(ge=1)
    margin_type: MarginType = MarginType.ISOLATED

    # Status
    is_active: bool = True
    contract_size: Decimal = Decimal("1")  # For futures (1 = 1 unit)

    @field_validator("symbol", "base_asset", "quote_asset")
    @classmethod
    def upper_string(cls, v: str) -> str:
        return v.upper().strip()

    @model_validator(mode="after")
    def validate_constraints(self) -> Instrument:
        if self.min_qty >= self.max_qty:
            raise ValueError(f"min_qty ({self.min_qty}) >= max_qty ({self.max_qty})")
        if self.min_price >= self.max_price:
            raise ValueError(f"min_price ({self.min_price}) >= max_price ({self.max_price})")
        if self.default_leverage > self.max_leverage:
            raise ValueError(
                f"default_leverage ({self.default_leverage}) > max_leverage ({self.max_leverage})"
            )
        return self

    def round_price(self, price: Decimal) -> Decimal:
        """Round price to the instrument's tick size."""
        ticks = (price / self.tick_size).to_integral_value()
        return (ticks * self.tick_size).quantize(self.tick_size)

    def round_quantity(self, qty: Decimal) -> Decimal:
        """Round quantity to the instrument's step size."""
        steps = (qty / self.step_size).to_integral_value()
        return (steps * self.step_size).quantize(self.step_size)

    def validate_order_params(
        self,
        qty: Decimal,
        price: Decimal,
    ) -> list[str]:
        """Return list of validation errors for order parameters."""
        errors: list[str] = []

        rounded_qty = self.round_quantity(qty)
        if rounded_qty < self.min_qty:
            errors.append(f"Quantity {qty} (rounded: {rounded_qty}) < min {self.min_qty}")
        if rounded_qty > self.max_qty:
            errors.append(f"Quantity {qty} (rounded: {rounded_qty}) > max {self.max_qty}")

        rounded_price = self.round_price(price)
        if rounded_price < self.min_price:
            errors.append(f"Price {price} (rounded: {rounded_price}) < min {self.min_price}")
        if rounded_price > self.max_price:
            errors.append(f"Price {price} (rounded: {rounded_price}) > max {self.max_price}")

        notional = rounded_qty * rounded_price
        if notional < self.min_notional:
            errors.append(f"Notional {notional} < min {self.min_notional}")

        return errors

    def price_precision(self) -> int:
        """Number of decimal places for price."""
        exponent = self.tick_size.as_tuple().exponent
        # tick_size is Pydantic-validated as a finite positive Decimal
        # (Field(gt=0)), so its exponent can never be the NaN/Infinity
        # sentinel 'n'/'N'/'F' that Decimal.as_tuple() is typed to allow.
        assert isinstance(exponent, int)
        return max(0, -exponent)

    def qty_precision(self) -> int:
        """Number of decimal places for quantity."""
        exponent = self.step_size.as_tuple().exponent
        assert isinstance(exponent, int)
        return max(0, -exponent)

    def __str__(self) -> str:
        return f"Instrument({self.symbol}, {self.instrument_type})"
