"""Unit tests for src.strategy.exit."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import PositionSide
from src.domain.position import Position
from src.strategy.exit import (
    calculate_partial_tp,
    calculate_trailing_stop,
    classify_volatility_regime,
    evaluate_exit_actions,
    is_stop_hit,
    is_tp_hit,
    regime_aware_trail_multiplier,
    should_move_to_breakeven,
)


def make_position(
    side: PositionSide = PositionSide.LONG,
    entry: str = "50000",
    stop_loss: str | None = "49000",
    take_profit: str | None = "52000",
    breakeven_triggered: bool = False,
) -> Position:
    return Position(
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"),
        entry_price=Decimal(entry),
        mark_price=Decimal(entry),
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        take_profit=Decimal(take_profit) if take_profit else None,
        breakeven_triggered=breakeven_triggered,
    )


class TestShouldMoveToBreakeven:
    def test_true_when_profit_meets_activation_rr(self) -> None:
        position = make_position(entry="50000", stop_loss="49000")
        assert should_move_to_breakeven(position, Decimal("51000")) is True

    def test_false_when_profit_is_below_activation_rr(self) -> None:
        position = make_position(entry="50000", stop_loss="49000")
        assert should_move_to_breakeven(position, Decimal("50500")) is False

    def test_false_when_no_stop_loss_is_set(self) -> None:
        position = make_position(stop_loss=None)
        assert should_move_to_breakeven(position, Decimal("60000")) is False

    def test_false_when_already_triggered(self) -> None:
        position = make_position(breakeven_triggered=True)
        assert should_move_to_breakeven(position, Decimal("60000")) is False

    def test_false_for_zero_initial_risk(self) -> None:
        position = make_position(entry="50000", stop_loss="50000")
        assert should_move_to_breakeven(position, Decimal("60000")) is False

    def test_short_direction_profit_calculated_correctly(self) -> None:
        position = make_position(side=PositionSide.SHORT, entry="50000", stop_loss="51000")
        assert should_move_to_breakeven(position, Decimal("49000")) is True


class TestCalculateTrailingStop:
    def test_long_new_stop_when_none_exists(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss=None)
        result = calculate_trailing_stop(position, Decimal("51000"), Decimal("100"))
        assert result == Decimal("51000") - Decimal("200")

    def test_long_does_not_move_the_stop_backward(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss="51000")
        result = calculate_trailing_stop(position, Decimal("51050"), Decimal("100"))
        # candidate = 51050 - 200 = 50850 < 51000 -> keep existing (max)
        assert result == Decimal("51000")

    def test_long_moves_the_stop_up_when_favorable(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss="50500")
        result = calculate_trailing_stop(position, Decimal("51500"), Decimal("100"))
        assert result == Decimal("51300")

    def test_short_new_stop_when_none_exists(self) -> None:
        position = make_position(side=PositionSide.SHORT, stop_loss=None)
        result = calculate_trailing_stop(position, Decimal("49000"), Decimal("100"))
        assert result == Decimal("49000") + Decimal("200")

    def test_short_does_not_move_the_stop_backward(self) -> None:
        position = make_position(side=PositionSide.SHORT, stop_loss="49000")
        result = calculate_trailing_stop(position, Decimal("48950"), Decimal("100"))
        # candidate = 48950 + 200 = 49150 > 49000 -> keep existing (min)
        assert result == Decimal("49000")

    def test_respects_a_custom_multiplier(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss=None)
        result = calculate_trailing_stop(
            position, Decimal("51000"), Decimal("100"), atr_multiplier=Decimal("3.0")
        )
        assert result == Decimal("50700")


class TestCalculatePartialTp:
    def test_long_returns_triggered_levels_at_or_above_price(self) -> None:
        position = make_position(side=PositionSide.LONG)
        levels = [(Decimal("51000"), Decimal("0.5")), (Decimal("53000"), Decimal("0.5"))]
        result = calculate_partial_tp(position, levels, Decimal("52000"))
        assert result == [(Decimal("51000"), Decimal("0.5"))]

    def test_long_returns_all_levels_reached(self) -> None:
        position = make_position(side=PositionSide.LONG)
        levels = [(Decimal("51000"), Decimal("0.5")), (Decimal("52000"), Decimal("0.5"))]
        result = calculate_partial_tp(position, levels, Decimal("53000"))
        assert result == levels

    def test_short_returns_triggered_levels_at_or_below_price(self) -> None:
        position = make_position(side=PositionSide.SHORT)
        levels = [(Decimal("49000"), Decimal("0.5")), (Decimal("47000"), Decimal("0.5"))]
        result = calculate_partial_tp(position, levels, Decimal("48000"))
        assert result == [(Decimal("49000"), Decimal("0.5"))]

    def test_no_levels_reached_returns_empty(self) -> None:
        position = make_position(side=PositionSide.LONG)
        levels = [(Decimal("55000"), Decimal("0.5"))]
        result = calculate_partial_tp(position, levels, Decimal("50000"))
        assert result == []


class TestIsStopHit:
    def test_long_true_when_price_at_or_below_stop(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss="49000")
        assert is_stop_hit(position, Decimal("49000")) is True
        assert is_stop_hit(position, Decimal("48900")) is True

    def test_long_false_when_price_above_stop(self) -> None:
        position = make_position(side=PositionSide.LONG, stop_loss="49000")
        assert is_stop_hit(position, Decimal("49100")) is False

    def test_short_true_when_price_at_or_above_stop(self) -> None:
        position = make_position(side=PositionSide.SHORT, stop_loss="51000")
        assert is_stop_hit(position, Decimal("51000")) is True

    def test_false_when_no_stop_loss_is_set(self) -> None:
        position = make_position(stop_loss=None)
        assert is_stop_hit(position, Decimal("1")) is False


class TestIsTpHit:
    def test_long_true_when_price_at_or_above_target(self) -> None:
        position = make_position(side=PositionSide.LONG, take_profit="52000")
        assert is_tp_hit(position, Decimal("52000")) is True

    def test_long_false_when_price_below_target(self) -> None:
        position = make_position(side=PositionSide.LONG, take_profit="52000")
        assert is_tp_hit(position, Decimal("51900")) is False

    def test_short_true_when_price_at_or_below_target(self) -> None:
        position = make_position(side=PositionSide.SHORT, take_profit="48000")
        assert is_tp_hit(position, Decimal("48000")) is True

    def test_false_when_no_take_profit_is_set(self) -> None:
        position = make_position(take_profit=None)
        assert is_tp_hit(position, Decimal("999999")) is False


class TestClassifyVolatilityRegime:
    def test_low_regime_below_threshold(self) -> None:
        # 100 / 50000 * 100 = 0.2% < 0.3% low threshold
        assert classify_volatility_regime(Decimal("100"), Decimal("50000")) == "low"

    def test_normal_regime_between_thresholds(self) -> None:
        # 400 / 50000 * 100 = 0.8%, between 0.3% and 1.2%
        assert classify_volatility_regime(Decimal("400"), Decimal("50000")) == "normal"

    def test_high_regime_above_threshold(self) -> None:
        # 700 / 50000 * 100 = 1.4% > 1.2% high threshold
        assert classify_volatility_regime(Decimal("700"), Decimal("50000")) == "high"

    def test_zero_price_defaults_to_normal(self) -> None:
        assert classify_volatility_regime(Decimal("100"), Decimal("0")) == "normal"


class TestRegimeAwareTrailMultiplier:
    def test_low_regime_uses_tighter_multiplier(self) -> None:
        assert regime_aware_trail_multiplier(Decimal("100"), Decimal("50000")) == Decimal("1.5")

    def test_normal_regime_uses_default_multiplier(self) -> None:
        assert regime_aware_trail_multiplier(Decimal("400"), Decimal("50000")) == Decimal("2.0")

    def test_high_regime_uses_wider_multiplier(self) -> None:
        assert regime_aware_trail_multiplier(Decimal("700"), Decimal("50000")) == Decimal("2.75")


class TestEvaluateExitActions:
    def test_stop_hit_short_circuits_other_checks(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(position, Decimal("48900"), atr=Decimal("100"))
        assert plan.stop_hit is True
        assert plan.tp_hit is False
        assert plan.should_close_position is True
        assert plan.new_stop_loss is None
        assert plan.move_to_breakeven is False

    def test_tp_hit_short_circuits_other_checks(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(position, Decimal("52100"), atr=Decimal("100"))
        assert plan.tp_hit is True
        assert plan.stop_hit is False
        assert plan.should_close_position is True

    def test_open_position_gets_breakeven_and_trailing_recommendation(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(position, Decimal("51000"), atr=Decimal("100"))
        assert plan.should_close_position is False
        assert plan.move_to_breakeven is True  # 1000 profit >= 1000 initial risk
        assert plan.volatility_regime == "low"  # 100/51000 ~ 0.196% < 0.3%
        # low regime -> 1.5x multiplier: 51000 - 100*1.5 = 50850
        assert plan.new_stop_loss == Decimal("50850")

    def test_fixed_multiplier_used_when_regime_aware_disabled(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            use_regime_aware_trail=False,
            trail_atr_multiplier=Decimal("3.0"),
        )
        assert plan.new_stop_loss == Decimal("51000") - Decimal("300")

    def test_zero_atr_skips_trailing_stop_calculation(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(position, Decimal("51000"), atr=Decimal("0"))
        assert plan.new_stop_loss is None

    def test_partial_tp_levels_are_evaluated(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        levels = [(Decimal("51000"), Decimal("0.5")), (Decimal("53000"), Decimal("0.5"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            partial_tp_levels=levels,
        )
        assert plan.triggered_partial_tps == [(Decimal("51000"), Decimal("0.5"))]

    def test_no_partial_tp_levels_supplied_returns_empty_list(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        plan = evaluate_exit_actions(position, Decimal("50500"), atr=Decimal("100"))
        assert plan.triggered_partial_tps == []


class TestFvgTargetPartialTp:
    def test_price_inside_target_zone_triggers_partial_tp(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50900"), Decimal("51100"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            fvg_target_zones=zones,
        )
        assert plan.fvg_target_partial_tps == [(Decimal("51000"), Decimal("0.25"), 0)]

    def test_price_outside_target_zone_no_trigger(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50900"), Decimal("51100"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("50500"),
            atr=Decimal("100"),
            fvg_target_zones=zones,
        )
        assert plan.fvg_target_partial_tps == []

    def test_already_taken_zone_index_is_skipped(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50900"), Decimal("51100"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            fvg_target_zones=zones,
            fvg_target_taken={0},
        )
        assert plan.fvg_target_partial_tps == []

    def test_custom_fraction_is_used(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50900"), Decimal("51100"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            fvg_target_zones=zones,
            fvg_partial_tp_fraction=Decimal("0.5"),
        )
        assert plan.fvg_target_partial_tps == [(Decimal("51000"), Decimal("0.5"), 0)]


class TestFvgReversalRisk:
    def test_long_entering_opposing_zone_flags_risk_and_tightens_stop(self) -> None:
        # Long position, current stop at 49000. An opposing (bearish) FVG
        # sits at 50400-50600, below current price of 51000. Price hasn't
        # traded there yet in this call, so simulate it being reached.
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50400"), Decimal("50600"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("50500"),
            atr=Decimal("0"),
            fvg_reversal_zones=zones,
        )
        assert plan.fvg_reversal_risk is True
        # Candidate stop = zone bottom (50400), which improves on 49000.
        assert plan.new_stop_loss == Decimal("50400")

    def test_short_entering_opposing_zone_flags_risk_and_tightens_stop(self) -> None:
        position = make_position(
            side=PositionSide.SHORT,
            entry="50000",
            stop_loss="51000",
            take_profit="48000",
        )
        zones = [(Decimal("49400"), Decimal("49600"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("49500"),
            atr=Decimal("0"),
            fvg_reversal_zones=zones,
        )
        assert plan.fvg_reversal_risk is True
        assert plan.new_stop_loss == Decimal("49600")

    def test_price_outside_reversal_zone_no_flag(self) -> None:
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        zones = [(Decimal("50400"), Decimal("50600"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("0"),
            fvg_reversal_zones=zones,
        )
        assert plan.fvg_reversal_risk is False
        assert plan.new_stop_loss is None

    def test_reversal_candidate_does_not_override_better_trailing_stop(self) -> None:
        # Regular ATR trailing stop is tighter/better than the FVG-zone
        # candidate would be — the FVG logic must not loosen it.
        position = make_position(entry="50000", stop_loss="49000", take_profit="52000")
        # Zone contains current_price (51000) but its bottom (50500) is a
        # worse/looser candidate than the trailing stop computed below.
        zones = [(Decimal("50500"), Decimal("51050"))]
        plan = evaluate_exit_actions(
            position,
            Decimal("51000"),
            atr=Decimal("100"),
            fvg_reversal_zones=zones,
        )
        # Regime-aware trailing stop (low regime, 1.5x): 51000 - 150 = 50850,
        # which is better (higher) than the FVG candidate of 50500.
        assert plan.fvg_reversal_risk is True
        assert plan.new_stop_loss == Decimal("50850")
