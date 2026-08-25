"""Unit tests for risk management modules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.risk.circuit_breaker import CircuitBreaker
from src.risk.drawdown import DrawdownTracker
from src.risk.position_sizing import calculate_position_size, kelly_position_size
from src.risk.stop_loss import atr_stop_loss, validate_stop_risk
from src.risk.take_profit import fixed_rr_take_profit, partial_take_profit_levels
from src.strategy.exit import calculate_trailing_stop, should_move_to_breakeven
from src.risk.margin import (
    initial_margin,
    liquidation_price_long,
    liquidation_price_short,
)
from src.core.enums import CircuitBreakerState, PositionSide, SignalDirection
from src.domain.position import Position


def make_position(
    side: PositionSide = PositionSide.LONG,
    entry: float = 50000.0,
    mark: float = 50000.0,
    qty: float = 1.0,
    sl: float | None = None,
    tp: float | None = None,
) -> Position:
    return Position(
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal(str(qty)),
        entry_quantity=Decimal(str(qty)),
        entry_price=Decimal(str(entry)),
        mark_price=Decimal(str(mark)),
        stop_loss=Decimal(str(sl)) if sl else None,
        take_profit=Decimal(str(tp)) if tp else None,
    )


class TestPositionSizing:
    def test_basic_sizing(self):
        qty = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        # Risk = 10000 * 1% = 100
        # Stop dist = 1000 (+ fees ~4)
        # Qty ≈ 100 / 1000.4 ≈ 0.0999
        assert Decimal("0.09") < qty < Decimal("0.12")

    def test_zero_equity_returns_zero(self):
        qty = calculate_position_size(
            equity=Decimal("0"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
        )
        assert qty == Decimal("0")

    def test_higher_risk_pct_larger_size(self):
        small = calculate_position_size(Decimal("10000"), Decimal("1"), Decimal("50000"), Decimal("49000"))
        large = calculate_position_size(Decimal("10000"), Decimal("2"), Decimal("50000"), Decimal("49000"))
        assert large > small

    def test_wider_stop_smaller_size(self):
        tight = calculate_position_size(Decimal("10000"), Decimal("1"), Decimal("50000"), Decimal("49500"))
        wide = calculate_position_size(Decimal("10000"), Decimal("1"), Decimal("50000"), Decimal("48000"))
        assert tight > wide

    def test_kelly_basic(self):
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.55,
            avg_win=200.0,
            avg_loss=100.0,
            entry_price=Decimal("50000"),
        )
        assert qty >= Decimal("0")

    def test_kelly_negative_edge_returns_zero(self):
        qty = kelly_position_size(
            equity=Decimal("10000"),
            win_rate=0.3,
            avg_win=50.0,
            avg_loss=200.0,
            entry_price=Decimal("50000"),
        )
        assert qty == Decimal("0")


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreakerState.CLOSED
        assert not cb.is_open

    def test_trip_opens_breaker(self):
        cb = CircuitBreaker()
        cb.trip("test reason")
        assert cb.is_open
        assert cb.open_reason == "test reason"
        assert cb.trip_count == 1

    def test_reset_closes_breaker(self):
        cb = CircuitBreaker()
        cb.trip("test")
        cb.reset()
        assert cb.is_closed
        assert cb.open_reason is None

    def test_double_trip_does_not_increment(self):
        cb = CircuitBreaker()
        cb.trip("first")
        cb.trip("second")  # Already open — no-op
        assert cb.trip_count == 1

    def test_half_open_transition(self):
        cb = CircuitBreaker()
        cb.trip("test")
        cb.half_open()
        assert cb.state == CircuitBreakerState.HALF_OPEN


class TestDrawdownTracker:
    def test_no_drawdown_at_start(self):
        dt = DrawdownTracker()
        dt.update(10000.0)
        assert dt.current_drawdown_pct == 0.0

    def test_drawdown_calculated(self):
        dt = DrawdownTracker()
        dt.update(10000.0)
        dt.update(9000.0)
        assert abs(dt.current_drawdown_pct - 10.0) < 0.01

    def test_max_drawdown_tracked(self):
        dt = DrawdownTracker()
        dt.update(10000.0)
        dt.update(8000.0)  # 20% DD
        dt.update(9500.0)  # Recovery, but max stays at 20%
        assert abs(dt.max_drawdown_pct - 20.0) < 0.01

    def test_new_peak_resets_drawdown(self):
        dt = DrawdownTracker()
        dt.update(10000.0)
        dt.update(8000.0)
        dt.update(11000.0)  # New peak
        assert dt.current_drawdown_pct == 0.0
        assert dt.peak_equity == 11000.0


class TestStopLoss:
    def test_atr_stop_long(self):
        sl = atr_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.LONG,
            atr=Decimal("500"),
            multiplier=Decimal("1.5"),
        )
        assert sl == Decimal("49250")
        assert sl < Decimal("50000")

    def test_atr_stop_short(self):
        sl = atr_stop_loss(
            entry_price=Decimal("50000"),
            direction=SignalDirection.SHORT,
            atr=Decimal("500"),
            multiplier=Decimal("1.5"),
        )
        assert sl == Decimal("50750")
        assert sl > Decimal("50000")

    def test_stop_risk_valid(self):
        assert validate_stop_risk(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            equity=Decimal("10000"),
            risk_pct=Decimal("1"),
        )

    def test_stop_too_far_invalid(self):
        assert not validate_stop_risk(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("40000"),  # 20% stop — too wide
            equity=Decimal("10000"),
            risk_pct=Decimal("1"),
        )


class TestTakeProfit:
    def test_fixed_rr_long(self):
        tp = fixed_rr_take_profit(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            direction=SignalDirection.LONG,
            risk_reward=Decimal("2"),
        )
        assert tp == Decimal("52000")

    def test_fixed_rr_short(self):
        tp = fixed_rr_take_profit(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("51000"),
            direction=SignalDirection.SHORT,
            risk_reward=Decimal("2"),
        )
        assert tp == Decimal("48000")

    def test_partial_tp_levels(self):
        levels = partial_take_profit_levels(
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            direction=SignalDirection.LONG,
            levels=[(1.0, 0.5), (2.0, 0.5)],
        )
        assert len(levels) == 2
        price1, frac1 = levels[0]
        price2, frac2 = levels[1]
        assert price1 == Decimal("51000")  # 1:1
        assert price2 == Decimal("52000")  # 2:1
        assert frac1 + frac2 == Decimal("1.0")


class TestTrailingStop:
    def test_trailing_stop_moves_up_for_long(self):
        pos = make_position(side=PositionSide.LONG, entry=50000, mark=51000, sl=49000)
        new_sl = calculate_trailing_stop(pos, Decimal("52000"), Decimal("500"), Decimal("2.0"))
        # New SL = 52000 - 2*500 = 51000 > existing 49000
        assert new_sl > Decimal("49000")

    def test_trailing_stop_does_not_move_against_position(self):
        pos = make_position(side=PositionSide.LONG, entry=50000, mark=51000, sl=50500)
        # Price moves down — trailing stop should not move down
        new_sl = calculate_trailing_stop(pos, Decimal("50200"), Decimal("500"), Decimal("2.0"))
        # 50200 - 2*500 = 49200 < current 50500 → existing stop is kept
        assert new_sl == Decimal("50500")


class TestBreakeven:
    def test_should_move_at_1r(self):
        pos = make_position(entry=50000, mark=51000, sl=49000)
        # Risk = 50000-49000=1000, profit = 51000-50000=1000 → exactly 1R
        assert should_move_to_breakeven(pos, Decimal("51000"), 1.0)

    def test_should_not_move_below_1r(self):
        pos = make_position(entry=50000, mark=50500, sl=49000)
        assert not should_move_to_breakeven(pos, Decimal("50500"), 1.0)


class TestMargin:
    def test_initial_margin(self):
        margin = initial_margin(Decimal("1.0"), Decimal("50000"), leverage=10)
        assert margin == Decimal("5000")

    def test_liquidation_price_long(self):
        liq = liquidation_price_long(Decimal("50000"), leverage=10)
        # Approx: 50000 * (1 - 0.1 + 0.004) = 50000 * 0.904 = 45200
        assert liq < Decimal("50000")
        assert liq > Decimal("40000")

    def test_liquidation_price_short(self):
        liq = liquidation_price_short(Decimal("50000"), leverage=10)
        assert liq > Decimal("50000")
