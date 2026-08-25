"""Take-profit level calculation."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import SignalDirection


def fixed_rr_take_profit(
    entry_price: Decimal,
    stop_loss: Decimal,
    direction: SignalDirection,
    risk_reward: Decimal = Decimal("2.0"),
) -> Decimal:
    """Calculate take-profit based on fixed risk/reward ratio."""
    risk = abs(entry_price - stop_loss)
    if direction == SignalDirection.LONG:
        return entry_price + (risk * risk_reward)
    return entry_price - (risk * risk_reward)


def liquidity_target(
    direction: SignalDirection,
    liquidity_price: Decimal,
    buffer_pct: Decimal = Decimal("0.001"),
) -> Decimal:
    """Target just before a liquidity level (not through it)."""
    if direction == SignalDirection.LONG:
        return liquidity_price * (Decimal("1") - buffer_pct)
    return liquidity_price * (Decimal("1") + buffer_pct)


def partial_take_profit_levels(
    entry_price: Decimal,
    stop_loss: Decimal,
    direction: SignalDirection,
    levels: list[tuple[float, float]] | None = None,
) -> list[tuple[Decimal, Decimal]]:
    """
    Build partial take-profit schedule.

    Default: take 50% at 1:1, 50% at 2:1.

    Args:
        entry_price, stop_loss, direction: Trade parameters.
        levels: List of (rr_ratio, fraction_to_close).

    Returns:
        List of (price, fraction) tuples.
    """
    if levels is None:
        levels = [(1.0, 0.5), (2.0, 0.5)]

    risk = abs(entry_price - stop_loss)
    result: list[tuple[Decimal, Decimal]] = []

    for rr, fraction in levels:
        rr_d = Decimal(str(rr))
        frac_d = Decimal(str(fraction))
        if direction == SignalDirection.LONG:
            price = entry_price + risk * rr_d
        else:
            price = entry_price - risk * rr_d
        result.append((price, frac_d))

    return result
