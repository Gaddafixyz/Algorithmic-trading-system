"""Unit tests for src.portfolio.allocator."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.portfolio.allocator import AllocationMethod, PortfolioAllocator


class TestEqualWeight:
    def test_splits_evenly(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("100"), max_open_positions=10)
        result = allocator.allocate(Decimal("10000"), ["BTCUSDT", "ETHUSDT"])
        assert result["BTCUSDT"] == Decimal("5000")
        assert result["ETHUSDT"] == Decimal("5000")

    def test_empty_symbols_returns_empty(self):
        allocator = PortfolioAllocator()
        assert allocator.allocate(Decimal("10000"), []) == {}

    def test_zero_equity_returns_empty(self):
        allocator = PortfolioAllocator()
        assert allocator.allocate(Decimal("0"), ["BTCUSDT"]) == {}

    def test_negative_equity_returns_empty(self):
        allocator = PortfolioAllocator()
        assert allocator.allocate(Decimal("-100"), ["BTCUSDT"]) == {}


class TestPositionCap:
    def test_caps_at_max_position_pct(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("20"), max_open_positions=10)
        # Equal weight across 2 symbols would give 50% each — should be
        # capped to 20% of equity per symbol.
        result = allocator.allocate(Decimal("10000"), ["BTCUSDT", "ETHUSDT"])
        assert result["BTCUSDT"] == Decimal("2000")
        assert result["ETHUSDT"] == Decimal("2000")


class TestMaxOpenPositionsCap:
    def test_truncates_to_top_weighted_and_renormalizes(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("100"), max_open_positions=2)
        weights = {"BTCUSDT": Decimal("0.6"), "ETHUSDT": Decimal("0.3"), "SOLUSDT": Decimal("0.1")}
        result = allocator.allocate(
            Decimal("10000"), list(weights.keys()),
            method=AllocationMethod.FIXED, fixed_weights=weights,
        )
        assert "SOLUSDT" not in result
        assert set(result) == {"BTCUSDT", "ETHUSDT"}
        # Renormalized: 0.6 / 0.9 and 0.3 / 0.9 of equity — BTC should get
        # roughly double ETH's allocation, and the two should sum to ~equity
        # (float weighting math means this is approximate, not exact).
        ratio = result["BTCUSDT"] / result["ETHUSDT"]
        assert abs(ratio - Decimal("2")) < Decimal("0.01")
        total = result["BTCUSDT"] + result["ETHUSDT"]
        assert abs(total - Decimal("10000")) < Decimal("0.01")


class TestFixedWeights:
    def test_requires_fixed_weights(self):
        allocator = PortfolioAllocator()
        with pytest.raises(ValueError):
            allocator.allocate(Decimal("10000"), ["BTCUSDT"], method=AllocationMethod.FIXED)

    def test_ignores_symbols_not_in_weight_map(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("100"))
        result = allocator.allocate(
            Decimal("10000"), ["BTCUSDT", "ETHUSDT"],
            method=AllocationMethod.FIXED,
            fixed_weights={"BTCUSDT": Decimal("1.0")},
        )
        assert set(result) == {"BTCUSDT"}
        assert result["BTCUSDT"] == Decimal("10000")


class TestRiskParity:
    def test_lower_volatility_gets_higher_weight(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("100"), max_open_positions=10)
        result = allocator.allocate(
            Decimal("10000"), ["BTCUSDT", "ETHUSDT"],
            method=AllocationMethod.RISK_PARITY,
            volatilities={"BTCUSDT": 0.01, "ETHUSDT": 0.04},
        )
        assert result["BTCUSDT"] > result["ETHUSDT"]
        assert result["BTCUSDT"] + result["ETHUSDT"] == Decimal("10000")

    def test_missing_volatility_falls_back_without_raising(self):
        allocator = PortfolioAllocator(max_position_pct_of_equity=Decimal("100"), max_open_positions=10)
        result = allocator.allocate(
            Decimal("10000"), ["BTCUSDT", "ETHUSDT"],
            method=AllocationMethod.RISK_PARITY,
            volatilities={"BTCUSDT": 0.01},  # ETHUSDT missing
        )
        assert set(result) == {"BTCUSDT", "ETHUSDT"}


class TestConstructorValidation:
    def test_rejects_non_positive_pct_cap(self):
        with pytest.raises(ValueError):
            PortfolioAllocator(max_position_pct_of_equity=Decimal("0"))

    def test_rejects_zero_max_open_positions(self):
        with pytest.raises(ValueError):
            PortfolioAllocator(max_open_positions=0)
