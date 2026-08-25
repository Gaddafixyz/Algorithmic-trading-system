"""Supplementary unit tests for src.risk.position_sizing.

Covers guard-clause branches and the volatility_adjusted_size function
not exercised by tests/unit/test_risk.py's TestPositionSizing class.
"""

from __future__ import annotations

from decimal import Decimal

from src.risk.position_sizing import (
    calculate_position_size,
    kelly_position_size,
    volatility_adjusted_size,
)


class TestCalculatePositionSizeGuards:
    def test_zero_entry_price_returns_zero(self) -> None:
        qty = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("0"),
            stop_loss=Decimal("49000"),
        )
        assert qty == Decimal("0")

    def test_negative_entry_price_returns_zero(self) -> None:
        qty = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("-100"),
            stop_loss=Decimal("49000"),
        )
        assert qty == Decimal("0")

    def test_zero_stop_loss_price_returns_zero(self) -> None:
        qty = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("0"),
        )
        assert qty == Decimal("0")

    def test_negative_equity_returns_zero(self) -> None:
        qty = calculate_position_size(
            equity=Decimal("-500"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        assert qty == Decimal("0")

    def test_stop_loss_equal_to_entry_price_returns_zero(self) -> None:
        # stop_distance would be exactly 0, which must not divide-by-zero.
        qty = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_fee_rate_reduces_the_resulting_quantity(self) -> None:
        no_fee = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            fee_rate=Decimal("0"),
        )
        with_fee = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            fee_rate=Decimal("0.0004"),
        )
        assert with_fee < no_fee


class TestKellyPositionSizeGuards:
    def test_zero_avg_loss_returns_zero(self) -> None:
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.6,
            avg_win=200.0,
            avg_loss=0.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_negative_avg_loss_returns_zero(self) -> None:
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.6,
            avg_win=200.0,
            avg_loss=-50.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_zero_win_rate_returns_zero(self) -> None:
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.0,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_win_rate_of_exactly_1_returns_zero(self) -> None:
        # win_rate >= 1 is treated as invalid (no losing trades ever
        # observed is not a usable statistical edge for Kelly sizing).
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=1.0,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_win_rate_above_1_returns_zero(self) -> None:
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=1.5,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")

    def test_zero_entry_price_returns_zero_even_with_a_positive_edge(self) -> None:
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.6,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("0"),
        )
        assert qty == Decimal("0")

    def test_max_risk_pct_caps_the_effective_kelly_fraction(self) -> None:
        # A very favorable edge would otherwise produce a large Kelly
        # fraction; max_risk_pct must hard-cap the risk applied.
        uncapped_ish = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.9,
            avg_win=500.0,
            avg_loss=50.0,
            entry_price=Decimal("100"),
            kelly_fraction=1.0,
            max_risk_pct=100.0,
        )
        capped = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.9,
            avg_win=500.0,
            avg_loss=50.0,
            entry_price=Decimal("100"),
            kelly_fraction=1.0,
            max_risk_pct=1.0,
        )
        assert capped < uncapped_ish

    def test_kelly_fraction_scales_the_resulting_quantity(self) -> None:
        # max_risk_pct raised well above what either fraction reaches
        # here, so the comparison isolates kelly_fraction's effect
        # rather than both values saturating the same hard cap.
        quarter = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.55,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
            kelly_fraction=0.10,
            max_risk_pct=100.0,
        )
        half = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.55,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
            kelly_fraction=0.20,
            max_risk_pct=100.0,
        )
        assert half > quarter


class TestVolatilityAdjustedSize:
    def test_returns_base_size_when_stop_matches_target_atr_risk_exactly(self) -> None:
        # atr=1000, target_atr_risk=1.0 -> target_distance=1000, which
        # exactly matches the stop distance of |50000-49000|=1000, so
        # scale == 1.0 and the result equals the unadjusted base size.
        base = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        result = volatility_adjusted_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            atr=1000.0,
            target_atr_risk=1.0,
        )
        assert result == base

    def test_reduces_size_when_stop_is_wider_than_target(self) -> None:
        # stop distance = 2000; target = 1000*1.0 = 1000 -> scale = 0.5.
        base = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("48000"),
        )
        result = volatility_adjusted_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("48000"),
            atr=1000.0,
            target_atr_risk=1.0,
        )
        assert result < base
        assert result == base * Decimal("0.5")

    def test_never_sizes_larger_than_the_base_size_when_stop_is_narrower(
        self,
    ) -> None:
        # stop distance = 500; target = 1000 -> uncapped scale would be
        # 2.0, but the function must cap scale at 1.0 (never size above
        # the fixed-fractional base amount).
        base = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49500"),
        )
        result = volatility_adjusted_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49500"),
            atr=1000.0,
            target_atr_risk=1.0,
        )
        assert result == base

    def test_returns_base_size_unmodified_when_atr_is_zero(self) -> None:
        base = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        result = volatility_adjusted_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            atr=0.0,
        )
        assert result == base

    def test_returns_zero_when_the_base_size_is_already_zero(self) -> None:
        # Invalid inputs to the underlying fixed-fractional calculation
        # (e.g. zero equity) short-circuit before ATR scaling is applied.
        result = volatility_adjusted_size(
            equity=Decimal("0"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            atr=1000.0,
        )
        assert result == Decimal("0")

    def test_negative_atr_returns_the_base_size_unmodified(self) -> None:
        base = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        result = volatility_adjusted_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            atr=-50.0,
        )
        assert result == base
