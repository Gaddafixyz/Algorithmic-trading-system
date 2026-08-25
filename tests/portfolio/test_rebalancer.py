"""Unit tests for src.portfolio.rebalancer."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.enums import PositionSide
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.portfolio.rebalancer import PortfolioRebalancer, RebalanceDirection


def make_position(symbol: str, qty: str, entry: str, mark: str) -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal(qty),
        entry_quantity=Decimal(qty),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
    )


def make_portfolio(*positions: Position) -> Portfolio:
    pf = Portfolio(current_equity=Decimal("10000"), initial_equity=Decimal("10000"), peak_equity=Decimal("10000"))
    for p in positions:
        pf.add_position(p)
    return pf


class TestCheckRebalance:
    def test_no_positions_no_target_returns_empty(self):
        rebalancer = PortfolioRebalancer()
        assert rebalancer.check_rebalance(make_portfolio(), {}) == []

    def test_within_threshold_is_omitted(self):
        # 1 BTC @ mark 10000 = 10000 notional; target 10500 -> ~4.8% drift,
        # below the default 10% threshold.
        pos = make_position("BTCUSDT", "1", "10000", "10000")
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer(drift_threshold_pct=Decimal("10"))
        actions = rebalancer.check_rebalance(pf, {"BTCUSDT": Decimal("10500")})
        assert actions == []

    def test_beyond_threshold_flagged_increase(self):
        pos = make_position("BTCUSDT", "1", "10000", "10000")
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer(drift_threshold_pct=Decimal("10"))
        actions = rebalancer.check_rebalance(pf, {"BTCUSDT": Decimal("15000")})
        assert len(actions) == 1
        assert actions[0].direction == RebalanceDirection.INCREASE
        assert actions[0].delta_notional == Decimal("5000")

    def test_beyond_threshold_flagged_decrease(self):
        pos = make_position("BTCUSDT", "2", "10000", "10000")  # 20000 notional
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer(drift_threshold_pct=Decimal("10"))
        actions = rebalancer.check_rebalance(pf, {"BTCUSDT": Decimal("10000")})
        assert len(actions) == 1
        assert actions[0].direction == RebalanceDirection.DECREASE

    def test_position_with_no_target_flagged_full_drift(self):
        # Symbol was dropped from the target set entirely but we're still
        # holding it — should be flagged regardless of threshold.
        pos = make_position("SOLUSDT", "10", "100", "100")
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer()
        actions = rebalancer.check_rebalance(pf, {})
        assert len(actions) == 1
        assert actions[0].symbol == "SOLUSDT"
        assert actions[0].direction == RebalanceDirection.DECREASE
        assert actions[0].drift_pct == Decimal("100")

    def test_closed_positions_excluded_from_current_notional(self):
        from src.core.enums import PositionStatus

        pos = make_position("BTCUSDT", "1", "10000", "10000")
        pos.status = PositionStatus.CLOSED
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer()
        actions = rebalancer.full_report(pf, {"BTCUSDT": Decimal("5000")})
        btc = next(a for a in actions if a.symbol == "BTCUSDT")
        assert btc.current_notional == Decimal("0")


class TestFullReport:
    def test_includes_holds(self):
        pos = make_position("BTCUSDT", "1", "10000", "10000")
        pf = make_portfolio(pos)
        rebalancer = PortfolioRebalancer(drift_threshold_pct=Decimal("50"))
        actions = rebalancer.full_report(pf, {"BTCUSDT": Decimal("10500")})
        assert len(actions) == 1
        assert actions[0].direction == RebalanceDirection.HOLD


class TestConstructorValidation:
    def test_rejects_non_positive_threshold(self):
        with pytest.raises(ValueError):
            PortfolioRebalancer(drift_threshold_pct=Decimal("0"))
