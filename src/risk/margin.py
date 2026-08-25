"""Margin and liquidation price calculations for futures."""

from __future__ import annotations

from decimal import Decimal


def initial_margin(
    quantity: Decimal,
    entry_price: Decimal,
    leverage: int,
) -> Decimal:
    """Required initial margin for an isolated position."""
    notional = quantity * entry_price
    return notional / Decimal(str(leverage))


def maintenance_margin(
    quantity: Decimal,
    entry_price: Decimal,
    maint_margin_rate: Decimal = Decimal("0.004"),
) -> Decimal:
    """Required maintenance margin (Binance default ~0.4% for major pairs)."""
    return quantity * entry_price * maint_margin_rate


def liquidation_price_long(
    entry_price: Decimal,
    leverage: int,
    maint_margin_rate: Decimal = Decimal("0.004"),
) -> Decimal:
    """
    Estimated liquidation price for an isolated long position.

    Formula (simplified): liq = entry × (1 - 1/leverage + maint_margin_rate)
    """
    return entry_price * (Decimal("1") - Decimal("1") / Decimal(str(leverage)) + maint_margin_rate)


def liquidation_price_short(
    entry_price: Decimal,
    leverage: int,
    maint_margin_rate: Decimal = Decimal("0.004"),
) -> Decimal:
    """Estimated liquidation price for an isolated short position."""
    return entry_price * (Decimal("1") + Decimal("1") / Decimal(str(leverage)) - maint_margin_rate)


def distance_to_liquidation_pct(
    entry_price: Decimal,
    liq_price: Decimal,
) -> Decimal:
    """Distance from entry to liquidation as percentage."""
    return abs(entry_price - liq_price) / entry_price * Decimal("100")
