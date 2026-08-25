"""
Exit management logic.

Handles:
- Trailing stop adjustments
- Breakeven moves
- Partial take-profits
- Time-based exits
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from src.domain.position import Position

logger = logging.getLogger(__name__)

# ATR-as-%-of-price bands used to classify the current volatility regime.
# Below LOW: market is quiet, a tight trail locks in gains without giving
# back much. Above HIGH: market is choppy/volatile, a tight trail would get
# stopped out by noise, so we widen it.
LOW_VOL_ATR_PCT = 0.3
HIGH_VOL_ATR_PCT = 1.2

REGIME_TRAIL_MULTIPLIERS = {
    "low": Decimal("1.5"),
    "normal": Decimal("2.0"),
    "high": Decimal("2.75"),
}


def should_move_to_breakeven(
    position: Position,
    current_price: Decimal,
    activation_rr: float = 1.0,
) -> bool:
    """
    Return True if position has moved enough to justify moving SL to breakeven.

    Default: activate at 1:1 risk/reward.
    """
    if position.stop_loss is None or position.breakeven_triggered:
        return False

    initial_risk = abs(position.entry_price - (position.stop_loss or position.entry_price))
    if initial_risk == Decimal("0"):
        return False

    if position.is_long:
        profit = current_price - position.entry_price
    else:
        profit = position.entry_price - current_price

    return profit >= initial_risk * Decimal(str(activation_rr))


def calculate_trailing_stop(
    position: Position,
    current_price: Decimal,
    atr: Decimal,
    atr_multiplier: Decimal = Decimal("2.0"),
) -> Decimal:
    """Calculate the new trailing stop level based on ATR."""
    trail_distance = atr * atr_multiplier

    if position.is_long:
        new_sl = current_price - trail_distance
        # Only move stop up, never down
        if position.stop_loss is not None:
            return max(new_sl, position.stop_loss)
        return new_sl
    else:
        new_sl = current_price + trail_distance
        # Only move stop down, never up
        if position.stop_loss is not None:
            return min(new_sl, position.stop_loss)
        return new_sl


def calculate_partial_tp(
    position: Position,
    tp_levels: list[tuple[Decimal, Decimal]],
    current_price: Decimal,
) -> list[tuple[Decimal, Decimal]]:
    """
    Return list of (price, qty_fraction) for triggered partial TPs.

    Args:
        position: Open position.
        tp_levels: List of (price, fraction_of_qty) to take at each level.
        current_price: Current market price.

    Returns:
        Subset of tp_levels that have been triggered.
    """
    triggered = []
    for price, fraction in tp_levels:
        if (
            position.is_long
            and current_price >= price
            or position.is_short
            and current_price <= price
        ):
            triggered.append((price, fraction))
    return triggered


def is_stop_hit(position: Position, current_price: Decimal) -> bool:
    """True if stop-loss has been triggered."""
    if position.stop_loss is None:
        return False
    if position.is_long:
        return current_price <= position.stop_loss
    return current_price >= position.stop_loss


def is_tp_hit(position: Position, current_price: Decimal) -> bool:
    """True if take-profit has been triggered."""
    if position.take_profit is None:
        return False
    if position.is_long:
        return current_price >= position.take_profit
    return current_price <= position.take_profit


def classify_volatility_regime(atr: Decimal, price: Decimal) -> str:
    """
    Classify current volatility as "low", "normal", or "high" based on
    ATR as a percentage of price.
    """
    if price <= Decimal("0"):
        return "normal"
    atr_pct = float(atr / price) * 100
    if atr_pct < LOW_VOL_ATR_PCT:
        return "low"
    if atr_pct > HIGH_VOL_ATR_PCT:
        return "high"
    return "normal"


def regime_aware_trail_multiplier(atr: Decimal, price: Decimal) -> Decimal:
    """
    Return an ATR trail multiplier scaled to the current volatility regime.

    A fixed 2.0x ATR trail (the previous hardcoded default) is too tight in
    choppy/high-volatility markets (gets stopped out by noise) and leaves
    too much on the table in quiet markets. This scales the multiplier by
    regime so `calculate_trailing_stop` can use a distance appropriate to
    current conditions.
    """
    regime = classify_volatility_regime(atr, price)
    return REGIME_TRAIL_MULTIPLIERS[regime]


@dataclass
class ExitPlan:
    """Unified exit-management recommendation for a single evaluation pass."""

    stop_hit: bool = False
    tp_hit: bool = False
    move_to_breakeven: bool = False
    new_stop_loss: Decimal | None = None
    triggered_partial_tps: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    volatility_regime: str = "normal"
    # (price, fraction, zone_index) for open FVG "target" zones current_price
    # has now reached. zone_index refers to the caller's fvg_target_zones
    # list so the caller can persist which indices have already been taken
    # (mirrors how partial_tp_levels/triggered_partial_tps is tracked).
    fvg_target_partial_tps: list[tuple[Decimal, Decimal, int]] = field(default_factory=list)
    # True if current_price has traded into an FVG formed against the
    # position's direction (an "opposing" imbalance) — an early warning
    # that momentum may be reversing. When True, new_stop_loss may have
    # been tightened to that zone's protective edge (see below).
    fvg_reversal_risk: bool = False

    @property
    def should_close_position(self) -> bool:
        """True if the position should be fully closed this pass."""
        return self.stop_hit or self.tp_hit


def evaluate_exit_actions(
    position: Position,
    current_price: Decimal,
    atr: Decimal,
    *,
    breakeven_activation_rr: float = 1.0,
    partial_tp_levels: list[tuple[Decimal, Decimal]] | None = None,
    use_regime_aware_trail: bool = True,
    trail_atr_multiplier: Decimal = Decimal("2.0"),
    fvg_target_zones: list[tuple[Decimal, Decimal]] | None = None,
    fvg_reversal_zones: list[tuple[Decimal, Decimal]] | None = None,
    fvg_target_taken: set[int] | None = None,
    fvg_partial_tp_fraction: Decimal = Decimal("0.25"),
) -> ExitPlan:
    """
    Evaluate all exit conditions for a position in one pass and return a
    single actionable plan, so callers don't have to re-derive ordering
    (e.g. stop/TP hits take priority over trailing/breakeven adjustments
    that would otherwise be moot once the position is closing).

    Args:
        position: Open position to evaluate.
        current_price: Latest mark price.
        atr: Current ATR value (same units as price).
        breakeven_activation_rr: RR multiple at which to move SL to breakeven.
        partial_tp_levels: Optional (price, qty_fraction) ladder to check.
        use_regime_aware_trail: If True, scale the trail distance by the
            current volatility regime instead of a fixed multiplier.
        trail_atr_multiplier: Multiplier to use when
            use_regime_aware_trail is False.
        fvg_target_zones: (bottom, top) zones for open FVGs that formed in
            the *same* direction as this position (e.g. bullish FVGs for a
            long). Price reaching one of these is treated as reaching a
            resting liquidity/imbalance level worth banking partial profit
            at — the classic SMC "price is drawn back to fill imbalances"
            behaviour, used here as a target rather than an entry trigger.
            Caller is expected to have already classified same-direction
            vs. opposing zones (this function has no position-direction-
            aware FVG-type knowledge of its own — see execution_engine.py).
        fvg_reversal_zones: (bottom, top) zones for open FVGs that formed
            *against* this position's direction. Price trading back into
            one of these is a warning that the move may be reversing;
            triggers fvg_reversal_risk and may tighten new_stop_loss to
            that zone's protective edge.
        fvg_target_taken: Indices into fvg_target_zones already triggered
            on a prior pass, so a partial TP isn't re-submitted every tick
            while price sits inside the same zone.
        fvg_partial_tp_fraction: Fraction of remaining quantity to close
            when a target zone triggers.
    """
    plan = ExitPlan()

    # Hard exits take priority — if we're closing, nothing else matters.
    if is_stop_hit(position, current_price):
        plan.stop_hit = True
        return plan
    if is_tp_hit(position, current_price):
        plan.tp_hit = True
        return plan

    if partial_tp_levels:
        plan.triggered_partial_tps = calculate_partial_tp(
            position, partial_tp_levels, current_price
        )

    if should_move_to_breakeven(position, current_price, breakeven_activation_rr):
        plan.move_to_breakeven = True

    if atr > Decimal("0"):
        multiplier = (
            regime_aware_trail_multiplier(atr, current_price)
            if use_regime_aware_trail
            else trail_atr_multiplier
        )
        plan.volatility_regime = classify_volatility_regime(atr, current_price)
        plan.new_stop_loss = calculate_trailing_stop(
            position, current_price, atr, atr_multiplier=multiplier
        )

    if fvg_target_zones:
        for i, (bottom, top) in enumerate(fvg_target_zones):
            if fvg_target_taken and i in fvg_target_taken:
                continue
            if bottom <= current_price <= top:
                mid = (top + bottom) / Decimal("2")
                plan.fvg_target_partial_tps.append((mid, fvg_partial_tp_fraction, i))

    if fvg_reversal_zones:
        for bottom, top in fvg_reversal_zones:
            if not (bottom <= current_price <= top):
                continue
            plan.fvg_reversal_risk = True
            # Tighten toward the zone's edge that sits *behind* price in
            # the trade's favorable direction — i.e. the boundary that, if
            # breached, means the imbalance has been fully traded through
            # rather than just wicked into. Only ever applied if it
            # improves on whatever trailing/breakeven logic above already
            # produced, so this can tighten a stop but never loosen one.
            candidate = top if position.is_short else bottom
            if (
                plan.new_stop_loss is None
                or position.is_long
                and candidate > plan.new_stop_loss
                or position.is_short
                and candidate < plan.new_stop_loss
            ):
                plan.new_stop_loss = candidate
            break  # one confirmed reversal-risk trigger is enough signal

    return plan
