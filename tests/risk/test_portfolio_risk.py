"""Unit tests for src.risk.portfolio_risk."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from src.core.enums import PositionSide
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.risk.exposure import CorrelationTracker
from src.risk.portfolio_risk import (
    correlation_adjusted_size,
    portfolio_risk_report,
    portfolio_var_cvar,
    risk_parity_weights,
)


def make_position(symbol: str, side: PositionSide, qty: str, entry: str, mark: str) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        entry_quantity=Decimal(qty),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
    )


def make_portfolio(*positions: Position) -> Portfolio:
    pf = Portfolio(
        current_equity=Decimal("10000"),
        initial_equity=Decimal("10000"),
        peak_equity=Decimal("10000"),
    )
    for p in positions:
        pf.add_position(p)
    return pf


def make_tracker(prices: dict[str, list[float]], window: int = 100) -> CorrelationTracker:
    symbols = list(prices.keys())
    tracker = CorrelationTracker(symbols=symbols, window=window)
    length = len(next(iter(prices.values())))
    for i in range(length):
        for sym in symbols:
            tracker.update(sym, prices[sym][i])
    return tracker


class TestPortfolioRiskReport:
    def test_empty_portfolio(self):
        pf = make_portfolio()
        report = portfolio_risk_report(pf, equity=Decimal("10000"))
        assert report["open_positions"] == 0
        assert report["gross_exposure_pct"] == 0.0

    def test_single_long_position_exposure(self):
        pos = make_position("BTCUSDT", PositionSide.LONG, "1", "10000", "10500")
        pf = make_portfolio(pos)
        report = portfolio_risk_report(pf, equity=Decimal("10000"))
        assert report["open_positions"] == 1
        assert report["gross_exposure_pct"] == pytest.approx(105.0, rel=0.01)
        assert report["net_exposure_pct"] > 0

    def test_long_short_nets_to_zero_bias(self):
        long_pos = make_position("BTCUSDT", PositionSide.LONG, "1", "100", "100")
        short_pos = make_position("ETHUSDT", PositionSide.SHORT, "1", "100", "100")
        pf = make_portfolio(long_pos, short_pos)
        report = portfolio_risk_report(pf, equity=Decimal("10000"))
        assert report["net_exposure_pct"] == pytest.approx(0.0, abs=1e-6)
        assert report["gross_exposure_pct"] == pytest.approx(2.0, rel=0.01)


class TestCorrelationAdjustedSize:
    def test_no_open_positions_returns_unchanged(self):
        pf = make_portfolio()
        tracker = make_tracker({"BTCUSDT": [100, 101, 99, 102], "ETHUSDT": [10, 10.1, 9.9, 10.2]})
        size = correlation_adjusted_size(Decimal("1.0"), "BTCUSDT", pf, tracker)
        assert size == Decimal("1.0")

    def test_highly_correlated_symbol_scales_down(self):
        rng = np.random.default_rng(0)
        base = 100 + np.cumsum(rng.normal(0, 1, 200))
        # ETH tracks BTC almost exactly -> high correlation
        eth = base * 0.1

        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=200)
        for b, e in zip(base, eth, strict=True):
            tracker.update("BTCUSDT", float(b))
            tracker.update("ETHUSDT", float(e))

        held = make_position("BTCUSDT", PositionSide.LONG, "1", "100", "100")
        pf = make_portfolio(held)

        size = correlation_adjusted_size(
            Decimal("1.0"), "ETHUSDT", pf, tracker, max_correlation=0.7, min_scale=0.25
        )
        assert size < Decimal("1.0")
        assert size >= Decimal("0.25")

    def test_uncorrelated_symbol_unchanged(self):
        rng = np.random.default_rng(1)
        btc = 100 + np.cumsum(rng.normal(0, 1, 200))
        eth = 10 + np.cumsum(rng.normal(0, 1, 200))  # independent random walk

        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=200)
        for b, e in zip(btc, eth, strict=True):
            tracker.update("BTCUSDT", float(b))
            tracker.update("ETHUSDT", float(e))

        held = make_position("BTCUSDT", PositionSide.LONG, "1", "100", "100")
        pf = make_portfolio(held)
        size = correlation_adjusted_size(Decimal("1.0"), "ETHUSDT", pf, tracker)
        # Not guaranteed exactly 1.0 with finite random data, but should be close.
        assert size >= Decimal("0.5")


class TestPortfolioVarCvar:
    def test_insufficient_symbols_returns_none(self):
        pos = make_position("BTCUSDT", PositionSide.LONG, "1", "100", "100")
        pf = make_portfolio(pos)
        tracker = make_tracker({"BTCUSDT": [100, 101, 99, 102]})
        result = portfolio_var_cvar(pf, Decimal("10000"), tracker)
        assert result is None

    def test_two_symbol_portfolio_produces_var(self):
        rng = np.random.default_rng(2)
        btc = list(100 + np.cumsum(rng.normal(0, 1, 100)))
        eth = list(10 + np.cumsum(rng.normal(0, 0.5, 100)))
        tracker = make_tracker({"BTCUSDT": btc, "ETHUSDT": eth}, window=100)

        pos_a = make_position("BTCUSDT", PositionSide.LONG, "1", "100", str(btc[-1]))
        pos_b = make_position("ETHUSDT", PositionSide.LONG, "10", "10", str(eth[-1]))
        pf = make_portfolio(pos_a, pos_b)

        result = portfolio_var_cvar(pf, Decimal("10000"), tracker)
        assert result is not None
        assert result["portfolio_var_pct"] >= 0
        assert result["portfolio_cvar_pct"] >= result["portfolio_var_pct"]


class TestRiskParityWeights:
    def test_inverse_volatility_weighting(self):
        weights = risk_parity_weights({"BTCUSDT": 0.02, "ETHUSDT": 0.04})
        assert weights["BTCUSDT"] > weights["ETHUSDT"]
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_empty_input_returns_empty(self):
        assert risk_parity_weights({}) == {}

    def test_ignores_non_positive_volatilities(self):
        weights = risk_parity_weights({"BTCUSDT": 0.02, "ETHUSDT": 0.0, "SOLUSDT": -1.0})
        assert set(weights.keys()) == {"BTCUSDT"}
        assert weights["BTCUSDT"] == pytest.approx(1.0)
