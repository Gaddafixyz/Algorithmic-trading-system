"""Unit tests for src.execution.position_manager.PositionManager.

Focused on apply_funding() — PositionManager itself had no dedicated
test file before this one; other coverage of register/get/close_position
lives indirectly in test_execution_engine.py via mocked PositionManager.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.enums import PositionSide
from src.core.events import EventBus, reset_event_bus, set_event_bus
from src.domain.position import Position
from src.execution.position_manager import PositionManager


@pytest.fixture(autouse=True)
def isolated_event_bus():
    """Give every test its own EventBus so handlers/subscriptions don't leak."""
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def make_position(
    *,
    side: PositionSide = PositionSide.LONG,
    symbol: str = "BTCUSDT",
    qty: str = "1",
    entry: str = "50000",
    mark: str = "50000",
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        entry_quantity=Decimal(qty),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
    )


@pytest.fixture
def manager() -> PositionManager:
    return PositionManager()


class TestApplyFunding:
    def test_long_pays_on_positive_rate(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="1", mark="50000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        # notional 50000 * rate 0.0001 = 5; long pays => funding_pnl negative
        assert pos.funding_pnl == Decimal("-5")

    def test_short_receives_on_positive_rate(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.SHORT, qty="1", mark="50000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        assert pos.funding_pnl == Decimal("5")

    def test_long_receives_on_negative_rate(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="1", mark="50000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("-0.0001"))

        assert pos.funding_pnl == Decimal("5")

    def test_uses_current_mark_price_not_entry_price(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="1", entry="50000", mark="60000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        # notional uses mark_price (60000), not entry_price (50000)
        assert pos.funding_pnl == Decimal("-6")

    def test_scales_with_quantity(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="2.5", mark="50000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        assert pos.funding_pnl == Decimal("-12.5")

    def test_accumulates_across_multiple_settlements(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="1", mark="50000")
        manager.register(pos)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))
        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        assert pos.funding_pnl == Decimal("-10")

    def test_ignores_other_symbols(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, symbol="BTCUSDT", mark="50000")
        manager.register(pos)

        manager.apply_funding("ETHUSDT", Decimal("0.0001"))

        assert pos.funding_pnl == Decimal("0")

    def test_no_open_positions_is_a_noop(self, manager: PositionManager) -> None:
        # Should not raise even with nothing registered.
        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

    def test_skips_closed_positions(self, manager: PositionManager) -> None:
        pos = make_position(side=PositionSide.LONG, qty="1", mark="50000")
        manager.register(pos)
        pos.add_partial_close(pos.quantity, Decimal("50000"), Decimal("0"))
        assert pos.is_open is False

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        assert pos.funding_pnl == Decimal("0")

    def test_multiple_positions_same_symbol_both_settled(self, manager: PositionManager) -> None:
        pos1 = make_position(side=PositionSide.LONG, qty="1", mark="50000")
        pos2 = make_position(side=PositionSide.SHORT, qty="1", mark="50000")
        manager.register(pos1)
        manager.register(pos2)

        manager.apply_funding("BTCUSDT", Decimal("0.0001"))

        assert pos1.funding_pnl == Decimal("-5")
        assert pos2.funding_pnl == Decimal("5")
