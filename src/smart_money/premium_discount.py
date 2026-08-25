"""
Smart Money Concepts: Premium / Discount Zone Analysis.

The range between the most recent swing high and swing low is divided
into premium (above 50%) and discount (below 50%) zones.

Smart money buys in discount, sells in premium.
Entries are taken in the opposing zone relative to the bias:
- Bullish bias: enter long from discount zone
- Bearish bias: enter short from premium zone

The 0.5 (50%) level = equilibrium
The 0.618-0.786 area = optimal trade entry (OTE) zone
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core.constants import SMC_DISCOUNT_ZONE, SMC_PREMIUM_ZONE
from src.smart_money.market_structure import SwingPoint


@dataclass(frozen=True)
class PremiumDiscountZones:
    """Price zone classification for a given swing range."""

    swing_high: Decimal
    swing_low: Decimal
    current_price: Decimal
    # Configurable thresholds — default to the module's original hardcoded
    # values (SMC_PREMIUM_ZONE/SMC_DISCOUNT_ZONE) so any caller that
    # doesn't pass these explicitly sees unchanged behavior. Previously
    # these were referenced directly as module constants inside the
    # properties below with no way to override them at all — see
    # calculate_premium_discount()'s `premium_zone`/`discount_zone`
    # params, which is how configs/strategy.yaml's `smc.premium_zone` /
    # `smc.discount_zone` now actually reach this class.
    premium_zone: Decimal = SMC_PREMIUM_ZONE
    discount_zone: Decimal = SMC_DISCOUNT_ZONE

    @property
    def range_size(self) -> Decimal:
        return self.swing_high - self.swing_low

    @property
    def equilibrium(self) -> Decimal:
        """50% of the range."""
        return self.swing_low + self.range_size * Decimal("0.5")

    @property
    def premium_zone_start(self) -> Decimal:
        """Level above which we are in premium."""
        return self.swing_low + self.range_size * self.premium_zone

    @property
    def discount_zone_end(self) -> Decimal:
        """Level below which we are in discount."""
        return self.swing_low + self.range_size * self.discount_zone

    @property
    def ote_zone_top(self) -> Decimal:
        """Optimal Trade Entry: 0.786 retracement."""
        return self.swing_low + self.range_size * Decimal("0.786")

    @property
    def ote_zone_bottom(self) -> Decimal:
        """Optimal Trade Entry: 0.618 retracement."""
        return self.swing_low + self.range_size * Decimal("0.618")

    @property
    def is_premium(self) -> bool:
        return self.current_price >= self.premium_zone_start

    @property
    def is_discount(self) -> bool:
        return self.current_price <= self.discount_zone_end

    @property
    def is_equilibrium(self) -> bool:
        return not self.is_premium and not self.is_discount

    @property
    def is_in_ote(self) -> bool:
        """True if price is in the 0.618–0.786 OTE zone."""
        return self.ote_zone_bottom <= self.current_price <= self.ote_zone_top

    @property
    def position_in_range(self) -> Decimal:
        """Price position as fraction of range: 0 = low, 1 = high."""
        if self.range_size == Decimal("0"):
            return Decimal("0.5")
        return (self.current_price - self.swing_low) / self.range_size

    @property
    def zone_label(self) -> str:
        if self.is_premium:
            return "PREMIUM"
        if self.is_discount:
            return "DISCOUNT"
        return "EQUILIBRIUM"


def calculate_premium_discount(
    swing_high: SwingPoint,
    swing_low: SwingPoint,
    current_price: Decimal,
    premium_zone: Decimal | None = None,
    discount_zone: Decimal | None = None,
) -> PremiumDiscountZones:
    """
    Calculate premium/discount zones for the given swing range.

    Args:
        premium_zone: Level (as a fraction of the swing range, 0-1) above
            which price is classified as premium. Defaults to
            SMC_PREMIUM_ZONE when omitted — see
            configs/strategy.yaml's `smc.premium_zone`.
        discount_zone: Level (as a fraction of the swing range, 0-1) below
            which price is classified as discount. Defaults to
            SMC_DISCOUNT_ZONE when omitted — see
            configs/strategy.yaml's `smc.discount_zone`.
    """
    kwargs: dict[str, Decimal] = {}
    if premium_zone is not None:
        kwargs["premium_zone"] = premium_zone
    if discount_zone is not None:
        kwargs["discount_zone"] = discount_zone
    return PremiumDiscountZones(
        swing_high=swing_high.price,
        swing_low=swing_low.price,
        current_price=current_price,
        **kwargs,
    )


def is_optimal_entry(
    current_price: Decimal,
    swing_high: Decimal,
    swing_low: Decimal,
    direction: str,  # "bullish" or "bearish"
    premium_zone: Decimal | None = None,
    discount_zone: Decimal | None = None,
) -> bool:
    """
    True if price is in the optimal entry zone for the given direction.

    Bullish: price in discount (below 50%)
    Bearish: price in premium (above 50%)
    """
    range_size = swing_high - swing_low
    if range_size == Decimal("0"):
        return False

    position = (current_price - swing_low) / range_size
    discount = discount_zone if discount_zone is not None else SMC_DISCOUNT_ZONE
    premium = premium_zone if premium_zone is not None else SMC_PREMIUM_ZONE

    if direction == "bullish":
        return position <= discount
    elif direction == "bearish":
        return position >= premium
    return False
