"""Unit tests for src.risk.take_profit."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import SignalDirection
from src.risk.take_profit import (
    fixed_rr_take_profit,
    liquidity_target,
    partial_take_profit_levels,
)


class TestFixedRrTakeProfit:
    def test_long_target_is_above_entry_by_risk_times_rr(self) -> None:
        result = fixed_rr_take_profit(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            direction=SignalDirection.LONG,
            risk_reward=Decimal("2.0"),
        )
        # risk = 1000; target = 50000 + 1000*2 = 52000
        assert result == Decimal("52000")

    def test_short_target_is_below_entry_by_risk_times_rr(self) -> None:
        result = fixed_rr_take_profit(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("51000"),
            direction=SignalDirection.SHORT,
            risk_reward=Decimal("2.0"),
        )
        # risk = 1000; target = 50000 - 1000*2 = 48000
        assert result == Decimal("48000")

    def test_uses_default_risk_reward_of_2_when_not_specified(self) -> None:
        result = fixed_rr_take_profit(
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            direction=SignalDirection.LONG,
        )
        assert result == Decimal("120")

    def test_risk_is_computed_from_absolute_distance_regardless_of_stop_side(
        self,
    ) -> None:
        # A short's stop_loss sits above entry; abs() ensures 'risk' is
        # still a positive magnitude rather than going negative.
        result = fixed_rr_take_profit(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("51000"),  # stop above entry, as expected for a short
            direction=SignalDirection.SHORT,
            risk_reward=Decimal("1.0"),
        )
        assert result == Decimal("49000")


class TestLiquidityTarget:
    def test_long_target_sits_just_below_the_liquidity_price(self) -> None:
        result = liquidity_target(
            direction=SignalDirection.LONG,
            liquidity_price=Decimal("52000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("52000") * (Decimal("1") - Decimal("0.001"))
        assert result < Decimal("52000")

    def test_short_target_sits_just_above_the_liquidity_price(self) -> None:
        result = liquidity_target(
            direction=SignalDirection.SHORT,
            liquidity_price=Decimal("48000"),
            buffer_pct=Decimal("0.001"),
        )
        assert result == Decimal("48000") * (Decimal("1") + Decimal("0.001"))
        assert result > Decimal("48000")

    def test_zero_buffer_targets_the_liquidity_price_exactly(self) -> None:
        result = liquidity_target(
            direction=SignalDirection.LONG,
            liquidity_price=Decimal("52000"),
            buffer_pct=Decimal("0"),
        )
        assert result == Decimal("52000")


class TestPartialTakeProfitLevels:
    def test_default_levels_are_50_percent_at_1r_and_50_percent_at_2r(self) -> None:
        result = partial_take_profit_levels(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            direction=SignalDirection.LONG,
        )
        assert result == [
            (Decimal("51000"), Decimal("0.5")),
            (Decimal("52000"), Decimal("0.5")),
        ]

    def test_short_direction_targets_are_below_entry(self) -> None:
        result = partial_take_profit_levels(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("51000"),
            direction=SignalDirection.SHORT,
        )
        assert result == [
            (Decimal("49000"), Decimal("0.5")),
            (Decimal("48000"), Decimal("0.5")),
        ]

    def test_custom_levels_are_used_when_provided(self) -> None:
        result = partial_take_profit_levels(
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            direction=SignalDirection.LONG,
            levels=[(0.5, 0.3), (1.5, 0.7)],
        )
        assert result == [
            (Decimal("105"), Decimal("0.3")),
            (Decimal("115"), Decimal("0.7")),
        ]

    def test_returns_one_tuple_per_input_level(self) -> None:
        result = partial_take_profit_levels(
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            direction=SignalDirection.LONG,
            levels=[(1.0, 0.25), (2.0, 0.25), (3.0, 0.5)],
        )
        assert len(result) == 3

    def test_empty_levels_list_returns_empty_result(self) -> None:
        result = partial_take_profit_levels(
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            direction=SignalDirection.LONG,
            levels=[],
        )
        assert result == []
