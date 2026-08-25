"""Unit tests for src.risk.stop_loss."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import SignalDirection
from src.risk.stop_loss import (
    atr_stop_loss,
    ob_stop_loss,
    structure_stop_loss,
    validate_stop_risk,
)


class TestAtrStopLoss:
    def test_long_places_stop_below_entry_by_atr_times_multiplier(self) -> None:
        result = atr_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.LONG,
            atr=Decimal("100"),
            multiplier=Decimal("1.5"),
        )
        assert result == Decimal("49850")

    def test_short_places_stop_above_entry_by_atr_times_multiplier(self) -> None:
        result = atr_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.SHORT,
            atr=Decimal("100"),
            multiplier=Decimal("1.5"),
        )
        assert result == Decimal("50150")

    def test_uses_the_default_multiplier_of_1_5_when_not_specified(self) -> None:
        result = atr_stop_loss(
            entry_price=Decimal("100"),
            direction=SignalDirection.LONG,
            atr=Decimal("10"),
        )
        assert result == Decimal("85")

    def test_zero_atr_places_stop_exactly_at_entry(self) -> None:
        result = atr_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.LONG,
            atr=Decimal("0"),
        )
        assert result == Decimal("50000")


class TestStructureStopLoss:
    def test_long_stop_is_below_the_swing_level_by_buffer(self) -> None:
        result = structure_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.LONG,
            swing_level=Decimal("49000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("49000") - Decimal("49000") * Decimal("0.001")
        assert result < Decimal("49000")

    def test_short_stop_is_above_the_swing_level_by_buffer(self) -> None:
        result = structure_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.SHORT,
            swing_level=Decimal("51000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("51000") + Decimal("51000") * Decimal("0.001")
        assert result > Decimal("51000")

    def test_entry_price_argument_does_not_affect_the_result(self) -> None:
        # The stop is anchored entirely to swing_level; entry_price is
        # accepted for API symmetry/signature consistency with the other
        # stop strategies but is not used in the calculation.
        result_a = structure_stop_loss(
            entry_price=Decimal("1"),
            direction=SignalDirection.LONG,
            swing_level=Decimal("49000"),
        )
        result_b = structure_stop_loss(
            entry_price=Decimal("999999"),
            direction=SignalDirection.LONG,
            swing_level=Decimal("49000"),
        )
        assert result_a == result_b


class TestObStopLoss:
    def test_long_stop_is_below_ob_bottom_by_buffer_pct(self) -> None:
        result = ob_stop_loss(
            direction=SignalDirection.LONG,
            ob_top=Decimal("50000"),
            ob_bottom=Decimal("49000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("49000") * (Decimal("1") - Decimal("0.001"))
        assert result < Decimal("49000")

    def test_short_stop_is_above_ob_top_by_buffer_pct(self) -> None:
        result = ob_stop_loss(
            direction=SignalDirection.SHORT,
            ob_top=Decimal("50000"),
            ob_bottom=Decimal("49000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("50000") * (Decimal("1") + Decimal("0.001"))
        assert result > Decimal("50000")

    def test_zero_buffer_returns_the_boundary_price_exactly(self) -> None:
        result = ob_stop_loss(
            direction=SignalDirection.LONG,
            ob_top=Decimal("50000"),
            ob_bottom=Decimal("49000"),
            buffer_pct=Decimal("0"),
        )
        assert result == Decimal("49000")


class TestValidateStopRisk:
    def test_true_for_a_reasonable_stop_distance(self) -> None:
        # distance = |50000-49000|/50000*100 = 2.0%, within [0.05, 5.0].
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("49000"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
            )
            is True
        )

    def test_false_when_stop_distance_exceeds_max_stop_pct(self) -> None:
        # distance = |50000-40000|/50000*100 = 20.0% > default max of 5.0%.
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("40000"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
            )
            is False
        )

    def test_false_when_stop_distance_is_below_the_minimum_0_05_pct(self) -> None:
        # distance = |50000-49999|/50000*100 = 0.002% < 0.05% minimum.
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("49999"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
            )
            is False
        )

    def test_respects_a_custom_max_stop_pct(self) -> None:
        # distance = 2.0%; with max_stop_pct=1.0 this should now be rejected.
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("49000"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
                max_stop_pct=Decimal("1.0"),
            )
            is False
        )

    def test_works_symmetrically_for_a_stop_above_entry_short_side(self) -> None:
        # distance = |50000-51000|/50000*100 = 2.0%, still within bounds
        # regardless of whether the stop sits above or below entry.
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("51000"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
            )
            is True
        )

    def test_boundary_at_exactly_max_stop_pct_is_accepted(self) -> None:
        # distance = |50000-47500|/50000*100 = 5.0%, exactly at the default
        # max_stop_pct boundary; the check is a strict '>' so this passes.
        assert (
            validate_stop_risk(
                entry_price=Decimal("50000"),
                stop_loss=Decimal("47500"),
                equity=Decimal("10000"),
                risk_pct=Decimal("1.0"),
            )
            is True
        )
