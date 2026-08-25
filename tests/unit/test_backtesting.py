"""Unit tests for the backtesting engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtesting.commission import PercentageCommission, ZeroCommission
from src.backtesting.slippage import FixedSlippage, ZeroSlippage
from src.backtesting.simulator import BacktestBroker
from src.core.enums import SignalDirection, Timeframe
from src.core.models import OHLCV
from src.domain.signal import Signal, SignalStrength


def ts(i: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)


def make_candle(i: int, price: float) -> OHLCV:
    return OHLCV(
        timestamp=ts(i),
        open=Decimal(str(price)),
        high=Decimal(str(price + 2)),
        low=Decimal(str(price - 2)),
        close=Decimal(str(price + 0.5)),
        volume=Decimal("1000"),
    )


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    entry: float = 100.0,
    sl: float = 98.0,
    tp: float = 104.0,
) -> Signal:
    return Signal(
        strategy_id="test",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        direction=direction,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal(str(entry)),
        stop_loss=Decimal(str(sl)),
        take_profit=Decimal(str(tp)),
    )


class TestCommissionModels:
    def test_percentage_commission(self):
        model = PercentageCommission(rate=Decimal("0.001"))
        fee = model.calculate(Decimal("1.0"), Decimal("50000"))
        assert fee == Decimal("50")

    def test_zero_commission(self):
        model = ZeroCommission()
        fee = model.calculate(Decimal("1.0"), Decimal("50000"))
        assert fee == Decimal("0")

    def test_default_rate(self):
        model = PercentageCommission()
        fee = model.calculate(Decimal("1.0"), Decimal("10000"))
        assert fee == Decimal("4")  # 0.04% * 10000 = 4


class TestSlippageModels:
    def test_zero_slippage(self):
        model = ZeroSlippage()
        candle = make_candle(0, 100.0)
        assert model.apply(Decimal("100"), True, candle) == Decimal("100")
        assert model.apply(Decimal("100"), False, candle) == Decimal("100")

    def test_fixed_slippage_buy_higher(self):
        model = FixedSlippage(slippage_pct=Decimal("0.001"))
        candle = make_candle(0, 100.0)
        fill = model.apply(Decimal("100"), True, candle)
        assert fill > Decimal("100")

    def test_fixed_slippage_sell_lower(self):
        model = FixedSlippage(slippage_pct=Decimal("0.001"))
        candle = make_candle(0, 100.0)
        fill = model.apply(Decimal("100"), False, candle)
        assert fill < Decimal("100")


class TestBacktestBroker:
    def _make_broker(
        self,
        initial_equity: float = 10000.0,
        commission=None,
        slippage=None,
    ) -> BacktestBroker:
        return BacktestBroker(
            initial_equity=Decimal(str(initial_equity)),
            commission=commission or ZeroCommission(),
            slippage=slippage or ZeroSlippage(),
        )

    def test_initial_equity(self):
        broker = self._make_broker(10000.0)
        assert broker.equity == Decimal("10000")

    def test_no_fills_before_pending_orders(self):
        broker = self._make_broker()
        signal = make_signal(entry=100.0, sl=98.0, tp=104.0)
        broker.place_signal(signal, make_candle(0, 100.0))
        # Process orders at next bar
        candle = make_candle(1, 101.0)
        broker.process_pending_orders(candle)
        assert len(broker.open_positions) == 1

    def test_long_tp_hit_closes_position(self):
        broker = self._make_broker()
        signal = make_signal(direction=SignalDirection.LONG, entry=100.0, sl=98.0, tp=104.0)
        broker.place_signal(signal, make_candle(0, 100.0))
        # Fill at next bar open (100)
        broker.process_pending_orders(make_candle(1, 100.0))
        assert len(broker.open_positions) == 1

        # TP hit on a subsequent bar: candle high >= 104
        broker.process_pending_orders(make_candle(2, 103.0))  # high=105 >= 104
        assert len(broker.open_positions) == 0
        assert len(broker.closed_trades) == 1
        trade = broker.closed_trades[0]
        assert trade.net_pnl > Decimal("0")

    def test_long_sl_hit_closes_position_with_loss(self):
        broker = self._make_broker()
        signal = make_signal(direction=SignalDirection.LONG, entry=100.0, sl=98.0, tp=104.0)
        broker.place_signal(signal, make_candle(0, 100.0))
        broker.process_pending_orders(make_candle(1, 100.0))

        # SL hit: candle low <= 98
        sl_candle = OHLCV(
            timestamp=ts(2),
            open=Decimal("99"),
            high=Decimal("100"),
            low=Decimal("97"),  # Goes through SL
            close=Decimal("98"),
            volume=Decimal("1000"),
        )
        broker.process_pending_orders(sl_candle)
        assert len(broker.closed_trades) == 1
        trade = broker.closed_trades[0]
        assert trade.net_pnl < Decimal("0")

    def test_short_tp_hit(self):
        broker = self._make_broker()
        signal = make_signal(
            direction=SignalDirection.SHORT,
            entry=100.0, sl=102.0, tp=96.0
        )
        broker.place_signal(signal, make_candle(0, 100.0))
        broker.process_pending_orders(make_candle(1, 100.0))

        # TP hit for short: candle low <= 96
        tp_candle = OHLCV(
            timestamp=ts(2),
            open=Decimal("99"),
            high=Decimal("100"),
            low=Decimal("95"),  # low <= 96
            close=Decimal("97"),
            volume=Decimal("1000"),
        )
        broker.process_pending_orders(tp_candle)
        assert len(broker.closed_trades) == 1
        assert broker.closed_trades[0].net_pnl > Decimal("0")

    def test_max_positions_respected(self):
        broker = self._make_broker()
        broker._max_positions = 2

        for i in range(5):
            signal = make_signal()
            # Each signal for different "symbol" would need different entry,
            # but broker uses symbol key so multiple signals for same symbol are skipped
            broker.place_signal(signal, make_candle(0, 100.0))

        broker.process_pending_orders(make_candle(1, 100.0))
        # Max 2 positions regardless of signals queued
        assert len(broker.open_positions) <= 2

    def test_equity_decreases_after_loss(self):
        broker = self._make_broker(initial_equity=10000.0, commission=PercentageCommission())
        signal = make_signal(entry=100.0, sl=98.0, tp=106.0)
        broker.place_signal(signal, make_candle(0, 100.0))
        broker.process_pending_orders(make_candle(1, 100.0))

        # SL hit
        sl_candle = OHLCV(
            timestamp=ts(2), open=Decimal("99"),
            high=Decimal("100"), low=Decimal("96"), close=Decimal("98"),
            volume=Decimal("1000"),
        )
        broker.process_pending_orders(sl_candle)
        assert broker.equity < Decimal("10000")

    def test_no_duplicate_positions(self):
        broker = self._make_broker()
        s1 = make_signal(entry=100.0, sl=90.0, tp=120.0)
        s2 = make_signal(entry=101.0, sl=91.0, tp=121.0)
        broker.place_signal(s1, make_candle(0, 100.0))
        broker.process_pending_orders(make_candle(1, 100.0))  # Fills s1

        broker.place_signal(s2, make_candle(1, 100.0))
        broker.process_pending_orders(make_candle(2, 100.0))  # s2 should be skipped (same symbol)
        assert len(broker.open_positions) == 1
