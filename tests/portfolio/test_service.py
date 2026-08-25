"""Unit tests for src.portfolio.service."""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import PositionSide
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.portfolio.allocator import PortfolioAllocator
from src.portfolio.rebalancer import PortfolioRebalancer, RebalanceDirection
from src.portfolio.service import PortfolioService


def make_position(symbol: str, qty: str, entry: str, mark: str) -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal(qty),
        entry_quantity=Decimal(qty),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
    )


class TestPortfolioService:
    def test_target_allocation_uses_portfolio_equity(self):
        pf = Portfolio(
            current_equity=Decimal("10000"),
            initial_equity=Decimal("10000"),
            peak_equity=Decimal("10000"),
        )
        allocator = PortfolioAllocator(
            max_position_pct_of_equity=Decimal("100"), max_open_positions=10
        )
        service = PortfolioService(pf, allocator=allocator)
        target = service.target_allocation(["BTCUSDT", "ETHUSDT"])
        assert target["BTCUSDT"] == Decimal("5000")
        assert target["ETHUSDT"] == Decimal("5000")

    def test_check_rebalance_flags_drift(self):
        pf = Portfolio(
            current_equity=Decimal("10000"),
            initial_equity=Decimal("10000"),
            peak_equity=Decimal("10000"),
        )
        pf.add_position(make_position("BTCUSDT", "1", "5000", "5000"))
        allocator = PortfolioAllocator(
            max_position_pct_of_equity=Decimal("100"), max_open_positions=10
        )
        rebalancer = PortfolioRebalancer(drift_threshold_pct=Decimal("1"))
        service = PortfolioService(pf, allocator=allocator, rebalancer=rebalancer)
        actions = service.check_rebalance(["BTCUSDT"])
        # Target with only 1 symbol at 10000 equity capped at 100% = 10000,
        # current is 5000 -> should flag INCREASE.
        assert len(actions) == 1
        assert actions[0].direction == RebalanceDirection.INCREASE

    def test_allocation_report_is_json_serializable_shape(self):
        pf = Portfolio(
            current_equity=Decimal("10000"),
            initial_equity=Decimal("10000"),
            peak_equity=Decimal("10000"),
        )
        service = PortfolioService(pf)
        report = service.allocation_report(["BTCUSDT"])
        assert report["equity"] == "10000"
        assert isinstance(report["allocations"], list)
        for entry in report["allocations"]:
            for key in ("current_notional", "target_notional", "delta_notional", "drift_pct"):
                assert isinstance(entry[key], str)

    def test_defaults_construct_allocator_and_rebalancer(self):
        pf = Portfolio(
            current_equity=Decimal("1000"),
            initial_equity=Decimal("1000"),
            peak_equity=Decimal("1000"),
        )
        service = PortfolioService(pf)
        assert isinstance(service.allocator, PortfolioAllocator)
        assert isinstance(service.rebalancer, PortfolioRebalancer)
