"""
Smart Money Concepts: Mitigation Block Analysis.

A mitigation block is an order block that has been partially filled
(mitigated) but is still relevant as a future reference point.

After mitigation, price often continues in the original direction
of the OB because not all orders were filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.smart_money.order_blocks import OrderBlock


@dataclass(frozen=True)
class MitigationBlock:
    """A partially-mitigated order block acting as new reference."""

    original_ob: OrderBlock
    mitigation_price: Decimal
    remaining_zone_top: Decimal
    remaining_zone_bottom: Decimal

    @property
    def zone_size(self) -> Decimal:
        return self.remaining_zone_top - self.remaining_zone_bottom

    def contains_price(self, price: Decimal) -> bool:
        return self.remaining_zone_bottom <= price <= self.remaining_zone_top


def extract_mitigation_blocks(
    order_blocks: list[OrderBlock],
) -> list[MitigationBlock]:
    """
    Convert partially-mitigated order blocks into mitigation blocks.

    These represent the unfilled portion of the OB — an area where
    remaining institutional orders may still trigger.
    """
    mitigation_blocks: list[MitigationBlock] = []

    for ob in order_blocks:
        if not ob.is_mitigated or ob.mitigation_price is None:
            continue

        from src.core.enums import OrderBlockType

        if ob.ob_type == OrderBlockType.BULLISH:
            # Bullish OB mitigated from top-down; remaining zone is above mitigation
            remaining_top = ob.top
            remaining_bottom = max(ob.bottom, ob.mitigation_price)
        else:
            # Bearish OB mitigated from bottom-up; remaining zone is below mitigation
            remaining_bottom = ob.bottom
            remaining_top = min(ob.top, ob.mitigation_price)

        if remaining_top > remaining_bottom:
            mitigation_blocks.append(MitigationBlock(
                original_ob=ob,
                mitigation_price=ob.mitigation_price,
                remaining_zone_top=remaining_top,
                remaining_zone_bottom=remaining_bottom,
            ))

    return mitigation_blocks
