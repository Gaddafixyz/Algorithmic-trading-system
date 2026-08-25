"""Supplementary unit tests for src.risk.margin.

Covers maintenance_margin and distance_to_liquidation_pct, which are
not exercised by tests/unit/test_risk.py's TestMargin class.
"""

from __future__ import annotations

from decimal import Decimal

from src.risk.margin import distance_to_liquidation_pct, maintenance_margin


class TestMaintenanceMargin:
    def test_calculates_notional_times_the_maintenance_rate(self) -> None:
        result = maintenance_margin(
            quantity=Decimal("1"),
            entry_price=Decimal("50000"),
            maint_margin_rate=Decimal("0.004"),
        )
        assert result == Decimal("50000") * Decimal("0.004")

    def test_uses_the_default_rate_of_0_004_when_not_specified(self) -> None:
        result = maintenance_margin(
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
        )
        assert result == Decimal("2") * Decimal("100") * Decimal("0.004")

    def test_scales_linearly_with_quantity(self) -> None:
        single = maintenance_margin(quantity=Decimal("1"), entry_price=Decimal("100"))
        double = maintenance_margin(quantity=Decimal("2"), entry_price=Decimal("100"))
        assert double == single * 2

    def test_zero_quantity_yields_zero_margin(self) -> None:
        result = maintenance_margin(quantity=Decimal("0"), entry_price=Decimal("50000"))
        assert result == Decimal("0")


class TestDistanceToLiquidationPct:
    def test_calculates_absolute_percentage_distance(self) -> None:
        result = distance_to_liquidation_pct(
            entry_price=Decimal("50000"), liq_price=Decimal("45000")
        )
        assert result == Decimal("10")

    def test_is_direction_agnostic_via_absolute_value(self) -> None:
        # A short's liquidation price sits above entry; the function
        # must still report a positive distance, not a negative one.
        below = distance_to_liquidation_pct(
            entry_price=Decimal("50000"), liq_price=Decimal("45000")
        )
        above = distance_to_liquidation_pct(
            entry_price=Decimal("50000"), liq_price=Decimal("55000")
        )
        assert below == above == Decimal("10")

    def test_zero_distance_when_liquidation_equals_entry(self) -> None:
        result = distance_to_liquidation_pct(
            entry_price=Decimal("50000"), liq_price=Decimal("50000")
        )
        assert result == Decimal("0")

    def test_high_leverage_scenario_yields_a_small_percentage(self) -> None:
        # A realistic 20x-leverage-style liquidation distance should be
        # a small single-digit percentage, not something implausible.
        result = distance_to_liquidation_pct(
            entry_price=Decimal("50000"), liq_price=Decimal("47600")
        )
        assert Decimal("4") < result < Decimal("5")
