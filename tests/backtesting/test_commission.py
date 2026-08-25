"""Unit tests for src.backtesting.commission."""

from __future__ import annotations

from decimal import Decimal

from src.backtesting.commission import PercentageCommission, TieredCommission, ZeroCommission


class TestPercentageCommission:
    def test_calculates_notional_times_the_rate(self) -> None:
        model = PercentageCommission(rate=Decimal("0.0004"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("50000"))
        assert result == Decimal("50000") * Decimal("0.0004")

    def test_uses_the_default_rate_when_not_specified(self) -> None:
        model = PercentageCommission()
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0004")

    def test_zero_quantity_yields_zero_commission(self) -> None:
        model = PercentageCommission()
        assert model.calculate(quantity=Decimal("0"), price=Decimal("50000")) == Decimal("0")


class TestZeroCommission:
    def test_always_returns_zero(self) -> None:
        model = ZeroCommission()
        assert model.calculate(quantity=Decimal("100"), price=Decimal("50000")) == Decimal("0")


class TestTieredCommission:
    def test_starts_at_the_lowest_tier_rate(self) -> None:
        model = TieredCommission()
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0004")

    def test_set_volume_tier_selects_the_correct_middle_tier(self) -> None:
        model = TieredCommission()
        model.set_volume_tier(Decimal("300_000"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0002")

    def test_set_volume_tier_selects_the_highest_tier(self) -> None:
        model = TieredCommission()
        model.set_volume_tier(Decimal("3_000_000"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.00016")

    def test_volume_exactly_at_a_tier_boundary_selects_that_tier(self) -> None:
        model = TieredCommission()
        model.set_volume_tier(Decimal("250_000"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0002")

    def test_volume_below_the_first_paid_tier_stays_at_default(self) -> None:
        model = TieredCommission()
        model.set_volume_tier(Decimal("100_000"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0004")

    def test_re_setting_a_lower_volume_downgrades_the_tier_back(self) -> None:
        model = TieredCommission()
        model.set_volume_tier(Decimal("3_000_000"))
        model.set_volume_tier(Decimal("0"))
        result = model.calculate(quantity=Decimal("1"), price=Decimal("100"))
        assert result == Decimal("100") * Decimal("0.0004")
