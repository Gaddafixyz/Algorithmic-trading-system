"""
Position sizing models.

Supported methods:
1. Fixed Fractional Risk (primary): risk a fixed % of equity per trade
2. Kelly Criterion (optional): mathematically optimal sizing
3. Volatility-adjusted: scale size inversely with ATR

All methods return quantity in base asset units.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal

logger = logging.getLogger(__name__)


def calculate_position_size(
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    *,
    leverage: int = 1,
    fee_rate: Decimal = Decimal("0.0004"),
) -> Decimal:
    """
    Fixed fractional position sizing.

    Formula:
        risk_amount = equity * risk_pct / 100
        stop_distance = |entry - stop_loss|
        quantity = risk_amount / stop_distance

    The quantity accounts for entry fees so actual risk stays correct.

    Args:
        equity: Current total equity in quote currency.
        risk_pct: Percentage of equity to risk (e.g. Decimal("1.0") = 1%).
        entry_price: Intended entry price.
        stop_loss: Stop-loss price.
        leverage: Applied leverage (default 1x).
        fee_rate: Taker fee rate (default 0.04% per side).

    Returns:
        Position size in base asset units, or Decimal("0") if invalid.
    """
    if equity <= Decimal("0"):
        logger.warning("Cannot size position: equity <= 0")
        return Decimal("0")
    if entry_price <= Decimal("0") or stop_loss <= Decimal("0"):
        logger.warning("Cannot size position: invalid prices entry=%s sl=%s", entry_price, stop_loss)
        return Decimal("0")

    risk_amount = equity * (risk_pct / Decimal("100"))
    stop_distance = abs(entry_price - stop_loss)

    if stop_distance <= Decimal("0"):
        logger.warning("Cannot size position: stop distance is zero")
        return Decimal("0")

    # Account for round-trip fees reducing effective risk
    # Effective risk per unit = stop_distance + 2 * entry_price * fee_rate
    effective_risk_per_unit = stop_distance + Decimal("2") * entry_price * fee_rate

    quantity = risk_amount / effective_risk_per_unit

    # Scale by leverage (leverage increases notional but not equity risk)
    # With leverage, we can hold the same risk with smaller margin
    # but quantity stays the same — leverage affects margin, not size
    # (position size is already denominated in base units)

    logger.debug(
        "Position sizing: equity=%s risk_pct=%s%% risk_amount=%s "
        "stop_dist=%s qty=%s",
        equity, risk_pct, risk_amount, stop_distance, quantity,
    )
    return quantity


def kelly_position_size(
    equity: Decimal,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    entry_price: Decimal,
    *,
    kelly_fraction: float = 0.25,  # Use fractional Kelly for safety
    max_risk_pct: float = 5.0,
) -> Decimal:
    """
    Kelly Criterion position sizing.

    Full Kelly is theoretically optimal but extremely volatile.
    We use fractional Kelly (default 25%) for practical safety.

    Args:
        equity: Current equity.
        win_rate: Historical win rate as fraction (0-1).
        avg_win: Average win in quote currency per unit.
        avg_loss: Average loss in quote currency per unit (positive value).
        entry_price: Current entry price.
        kelly_fraction: Fraction of Kelly to use (0-1).
        max_risk_pct: Hard cap on risk % regardless of Kelly output.

    Returns:
        Position quantity in base asset units.
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
        return Decimal("0")

    loss_rate = 1.0 - win_rate
    win_loss_ratio = avg_win / avg_loss

    # Kelly formula: f = W/L_ratio - (1 - W) / 1
    # Simplified for binary outcomes:
    kelly_f = (win_rate * (win_loss_ratio + 1) - 1) / win_loss_ratio
    if kelly_f <= 0:
        return Decimal("0")

    # Apply fractional Kelly and cap
    effective_f = min(kelly_f * kelly_fraction, max_risk_pct / 100.0)
    risk_amount = equity * Decimal(str(effective_f))

    if entry_price <= Decimal("0"):
        return Decimal("0")

    quantity = risk_amount / entry_price
    logger.debug(
        "Kelly sizing: win_rate=%.2f win_loss_ratio=%.2f kelly_f=%.4f "
        "effective_f=%.4f qty=%s",
        win_rate, win_loss_ratio, kelly_f, effective_f, quantity,
    )
    return quantity


def volatility_adjusted_size(
    equity: Decimal,
    risk_pct: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    atr: float,
    target_atr_risk: float = 1.5,
) -> Decimal:
    """
    Volatility-adjusted position size.

    Scales the base size by the ratio of target_atr_risk to actual stop distance.
    When the stop is wide (high volatility), size is reduced; when tight, size increases
    up to the normal fixed-fractional amount.

    Args:
        equity, risk_pct, entry_price, stop_loss: Same as fixed fractional.
        atr: Current ATR value.
        target_atr_risk: Target stop distance as multiple of ATR.

    Returns:
        Adjusted position quantity.
    """
    base_size = calculate_position_size(equity, risk_pct, entry_price, stop_loss)
    if base_size <= Decimal("0") or atr <= 0:
        return base_size

    stop_distance = float(abs(entry_price - stop_loss))
    target_distance = atr * target_atr_risk

    if stop_distance <= 0:
        return base_size

    # Scale: if stop is at exactly target_atr_risk * ATR → no change
    # If stop is wider → reduce size; if narrower → increase (capped at base)
    scale = target_distance / stop_distance
    scale = min(scale, 1.0)  # Never size more than base
    return base_size * Decimal(str(scale))
