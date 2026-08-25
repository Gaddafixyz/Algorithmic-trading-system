"""Unit tests for domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalDirection,
    SignalStrength,
    Timeframe,
)
from src.core.exceptions import OrderStateError
from src.domain.balance import Balance
from src.domain.candle import Candle
from src.domain.order import Fill, Order
from src.domain.position import Position
from src.domain.signal import Signal


class TestOrder:
    def _make_order(self, **kwargs) -> Order:
        defaults = dict(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        defaults.update(kwargs)
        return Order(**defaults)

    def test_initial_state_is_pending(self):
        order = self._make_order()
        assert order.status == OrderStatus.PENDING

    def test_valid_state_transition(self):
        order = self._make_order()
        order.transition_to(OrderStatus.SUBMITTED)
        assert order.status == OrderStatus.SUBMITTED
        order.transition_to(OrderStatus.OPEN)
        assert order.status == OrderStatus.OPEN

    def test_invalid_state_transition_raises(self):
        order = self._make_order()
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.FILLED)
        with pytest.raises(OrderStateError):
            order.transition_to(OrderStatus.OPEN)

    def test_add_fill_updates_quantities(self):
        order = self._make_order(quantity=Decimal("1.0"))
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        fill = Fill(
            price=Decimal("50000"),
            quantity=Decimal("0.5"),
            fee=Decimal("1.0"),
            fee_asset="USDT",
        )
        order.add_fill(fill)
        assert order.filled_quantity == Decimal("0.5")
        assert order.remaining_quantity == Decimal("0.5")
        assert order.status == OrderStatus.PARTIALLY_FILLED

    def test_full_fill_marks_filled(self):
        order = self._make_order(quantity=Decimal("1.0"))
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        fill = Fill(
            price=Decimal("50000"),
            quantity=Decimal("1.0"),
            fee=Decimal("2.0"),
            fee_asset="USDT",
        )
        order.add_fill(fill)
        assert order.status == OrderStatus.FILLED
        assert order.remaining_quantity == Decimal("0")

    def test_average_fill_price_calculated(self):
        order = self._make_order(quantity=Decimal("2.0"))
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.add_fill(
            Fill(price=Decimal("100"), quantity=Decimal("1.0"), fee=Decimal("0"), fee_asset="USDT")
        )
        order.add_fill(
            Fill(price=Decimal("200"), quantity=Decimal("1.0"), fee=Decimal("0"), fee_asset="USDT")
        )
        # Average: (100*1 + 200*1) / 2 = 150
        assert order.average_fill_price == Decimal("150")

    def test_limit_order_requires_price(self):
        with pytest.raises(Exception):
            Order(
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.1"),
                price=None,
            )

    def test_is_buy_is_sell(self):
        buy = self._make_order(side=OrderSide.BUY)
        sell = self._make_order(side=OrderSide.SELL)
        assert buy.is_buy and not buy.is_sell
        assert sell.is_sell and not sell.is_buy

    def test_terminal_states_reject_transitions(self):
        order = self._make_order()
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.transition_to(OrderStatus.CANCELLED)
        assert order.is_terminal
        with pytest.raises(OrderStateError):
            order.transition_to(OrderStatus.FILLED)


class TestPosition:
    def _make_position(self, **kwargs) -> Position:
        defaults = dict(
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            quantity=Decimal("1.0"),
            entry_quantity=Decimal("1.0"),
            entry_price=Decimal("50000"),
            mark_price=Decimal("50000"),
        )
        defaults.update(kwargs)
        return Position(**defaults)

    def test_long_unrealized_pnl(self):
        pos = self._make_position(
            mark_price=Decimal("51000"),
        )
        assert pos.unrealized_pnl == Decimal("1000")

    def test_short_unrealized_pnl(self):
        pos = self._make_position(
            side=PositionSide.SHORT,
            mark_price=Decimal("49000"),
        )
        assert pos.unrealized_pnl == Decimal("1000")

    def test_partial_close(self):
        pos = self._make_position(quantity=Decimal("2.0"), entry_quantity=Decimal("2.0"))
        pnl = pos.add_partial_close(Decimal("1.0"), Decimal("51000"), Decimal("0"))
        assert pnl == Decimal("1000")
        assert pos.quantity == Decimal("1.0")
        assert pos.is_open

    def test_full_close(self):
        pos = self._make_position()
        pos.add_partial_close(Decimal("1.0"), Decimal("52000"), Decimal("10"))
        assert pos.is_closed
        assert pos.exit_price == Decimal("52000")
        assert pos.realized_pnl == Decimal("2000")
        assert pos.total_fees == Decimal("10")

    def test_update_mark_price(self):
        pos = self._make_position()
        pos.update_mark_price(Decimal("51000"))
        assert pos.mark_price == Decimal("51000")
        assert pos.unrealized_pnl == Decimal("1000")

    def test_breakeven_calculation(self):
        pos = self._make_position(total_fees=Decimal("50"))
        be = pos.set_breakeven()
        assert be is not None
        assert be > pos.entry_price  # Long: BE above entry after fees

    def test_invalid_entry_price_raises(self):
        with pytest.raises(Exception):
            self._make_position(entry_price=Decimal("0"))


class TestSignal:
    def _make_signal(self, **kwargs) -> Signal:
        defaults = dict(
            strategy_id="test_strategy",
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            direction=SignalDirection.LONG,
            score=70.0,
            strength=SignalStrength.STRONG,
            confidence=0.7,
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
        )
        defaults.update(kwargs)
        return Signal(**defaults)

    def test_risk_reward_ratio(self):
        sig = self._make_signal()
        # Risk: 50000-49000=1000, Reward: 52000-50000=2000 → RR=2.0
        assert sig.risk_reward_ratio == Decimal("2")

    def test_stop_distance(self):
        sig = self._make_signal()
        assert sig.stop_distance == Decimal("1000")

    def test_is_long_is_short(self):
        long_sig = self._make_signal(direction=SignalDirection.LONG)
        short_sig = self._make_signal(
            direction=SignalDirection.SHORT,
            stop_loss=Decimal("51000"),
            take_profit=Decimal("48000"),
        )
        assert long_sig.is_long and not long_sig.is_short
        assert short_sig.is_short and not short_sig.is_long

    def test_invalid_stop_for_long_raises(self):
        with pytest.raises(Exception):
            self._make_signal(stop_loss=Decimal("51000"))  # SL above entry for long

    def test_invalid_tp_for_long_raises(self):
        with pytest.raises(Exception):
            self._make_signal(take_profit=Decimal("49000"))  # TP below entry for long

    def test_expiry(self):
        sig = self._make_signal(max_age_seconds=0.001)
        import time

        time.sleep(0.01)
        assert sig.is_expired()

    def test_not_expired_fresh(self):
        sig = self._make_signal(max_age_seconds=3600)
        assert not sig.is_expired()


class TestBalance:
    def test_initial_balance(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        assert b.available == Decimal("10000")
        assert b.total_pnl == Decimal("0")

    def test_reserve_reduces_available(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        b.reserve(Decimal("1000"))
        assert b.available == Decimal("9000")
        assert b.current == Decimal("10000")

    def test_cannot_reserve_more_than_available(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        with pytest.raises(ValueError):
            b.reserve(Decimal("15000"))

    def test_credit_increases_balance(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        b.credit(Decimal("500"))
        assert b.current == Decimal("10500")

    def test_debit_decreases_balance(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        b.debit(Decimal("2000"))
        assert b.current == Decimal("8000")

    def test_cannot_debit_more_than_available(self):
        b = Balance(initial=Decimal("10000"), current=Decimal("10000"))
        with pytest.raises(ValueError):
            b.debit(Decimal("15000"))


class TestCandle:
    def _make_candle(self, **kwargs) -> Candle:
        defaults = dict(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("98"),
            close=Decimal("103"),
            volume=Decimal("1000"),
            open_time=datetime(2024, 1, 1, tzinfo=UTC),
            close_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        )
        defaults.update(kwargs)
        return Candle(**defaults)

    def test_is_bullish(self):
        candle = self._make_candle(open=Decimal("100"), close=Decimal("103"))
        assert candle.is_bullish
        assert not candle.is_bearish

    def test_is_bearish(self):
        candle = self._make_candle(open=Decimal("103"), close=Decimal("100"))
        assert candle.is_bearish
        assert not candle.is_bullish

    def test_upper_wick(self):
        candle = self._make_candle(
            open=Decimal("100"), high=Decimal("108"), low=Decimal("98"), close=Decimal("103")
        )
        # Upper wick = high - max(open, close) = 108 - 103 = 5
        assert candle.upper_wick == Decimal("5")

    def test_lower_wick(self):
        candle = self._make_candle(
            open=Decimal("100"), high=Decimal("108"), low=Decimal("95"), close=Decimal("103")
        )
        # Lower wick = min(open, close) - low = 100 - 95 = 5
        assert candle.lower_wick == Decimal("5")

    def test_invalid_high_low_raises(self):
        with pytest.raises(Exception):
            self._make_candle(high=Decimal("95"), low=Decimal("100"))
