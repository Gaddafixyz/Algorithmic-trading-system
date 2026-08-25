"""Unit tests for src.backtesting.simulator.BacktestBroker."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.backtesting.commission import ZeroCommission
from src.backtesting.simulator import (
    BacktestBroker,
    FundingRate,
    PartialFillModel,
)
from src.backtesting.slippage import ZeroSlippage
from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.core.models import OHLCV
from src.domain.signal import Signal


def make_candle(
    o: str = "50000",
    h: str = "50100",
    low: str = "49900",
    c: str = "50000",
    volume: str = "1000",
    ts: datetime | None = None,
) -> OHLCV:
    return OHLCV(
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(volume),
        timestamp=ts or datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    entry: str = "50000",
    sl: str = "49000",
    tp: str = "52000",
    symbol: str = "BTCUSDT",
) -> Signal:
    return Signal(
        strategy_id="test",
        symbol=symbol,
        timeframe=Timeframe.H1,
        direction=direction,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal(entry),
        stop_loss=Decimal(sl),
        take_profit=Decimal(tp),
    )


def make_broker(
    equity: str = "10000",
    risk_pct: str = "1.0",
    max_positions: int = 5,
    funding_rates: list[FundingRate] | None = None,
    partial_fill: PartialFillModel | None = None,
) -> BacktestBroker:
    return BacktestBroker(
        initial_equity=Decimal(equity),
        commission=ZeroCommission(),
        slippage=ZeroSlippage(),
        risk_pct=Decimal(risk_pct),
        max_positions=max_positions,
        funding_rates=funding_rates,
        partial_fill=partial_fill or PartialFillModel(enabled=False),
    )


class TestPlaceSignalGuards:
    def test_does_not_queue_when_already_in_position(self) -> None:
        broker = make_broker()
        broker._positions["BTCUSDT"] = None  # type: ignore[assignment]
        broker.place_signal(make_signal(), make_candle())
        assert broker._pending == []

    def test_does_not_queue_when_at_max_positions(self) -> None:
        broker = make_broker(max_positions=0)
        broker.place_signal(make_signal(), make_candle())
        assert broker._pending == []

    def test_stop_distance_guard_is_unreachable_via_a_valid_signal(self) -> None:
        # BacktestBroker.place_signal() has a defensive
        # `if stop_dist <= Decimal("0"): return` guard. Tracing this:
        # Signal's own Pydantic validator already rejects
        # stop_loss == entry_price (and the wrong-side case) for any
        # direction, so no valid Signal can ever reach place_signal()
        # with a zero stop distance. This documents that finding
        # directly rather than forcing an invalid Signal fixture just
        # to hit a coverage line.
        import pytest

        with pytest.raises(ValueError, match="must be strictly below entry"):
            make_signal(entry="50000", sl="50000")

    def test_queues_a_valid_signal(self) -> None:
        broker = make_broker()
        broker.place_signal(make_signal(), make_candle())
        assert len(broker._pending) == 1


class TestNextBarOpenFillSemantics:
    def test_order_fills_at_the_next_bars_open_not_the_signal_bar(self) -> None:
        broker = make_broker()
        signal_bar = make_candle(o="50000", c="50000")
        broker.place_signal(make_signal(entry="50000"), signal_bar)

        next_bar = make_candle(
            o="50500", h="50700", low="50400", c="50600"
        )  # a different open price
        broker.process_pending_orders(next_bar)

        assert len(broker.open_positions) == 1
        assert broker.open_positions[0].entry_price == Decimal("50500")

    def test_insufficient_equity_rejects_the_fill_without_raising(self) -> None:
        # A tight stop relative to entry produces a large quantity for
        # a given risk_pct; combined with tiny equity and an extreme
        # risk_pct, cost = qty * price ends up far exceeding equity.
        broker = make_broker(equity="1", risk_pct="100.0")
        broker.place_signal(make_signal(entry="50000", sl="49999", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle())
        assert broker.open_positions == []

    def test_max_positions_reached_at_fill_time_refunds_commission_and_skips(
        self,
    ) -> None:
        broker = make_broker(max_positions=1)
        broker.place_signal(make_signal(symbol="BTCUSDT"), make_candle())
        broker.process_pending_orders(make_candle())

        broker.place_signal(make_signal(symbol="ETHUSDT"), make_candle())
        broker.process_pending_orders(make_candle())

        assert len(broker.open_positions) == 1
        assert broker.open_positions[0].symbol == "BTCUSDT"


class TestSignalIdPropagation:
    """
    Position/Trade must carry signal_id/strategy_id through from the
    originating Signal — the live path (execution_engine.py) already does
    this; the backtest simulator was silently dropping both fields, which
    made it impossible to join a closed backtest Trade back to the Signal
    that generated it (needed for ML training-label generation and any
    per-signal-component win-rate analysis).
    """

    def test_open_position_carries_signal_and_strategy_id(self) -> None:
        broker = make_broker()
        signal = make_signal()
        broker.place_signal(signal, make_candle())
        broker.process_pending_orders(make_candle())

        assert len(broker.open_positions) == 1
        pos = broker.open_positions[0]
        assert pos.signal_id == signal.signal_id
        assert pos.strategy_id == signal.strategy_id

    def test_closed_trade_carries_signal_and_strategy_id(self) -> None:
        broker = make_broker()
        signal = make_signal(entry="50000", sl="49000", tp="52000")
        broker.place_signal(signal, make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        # Force an exit via take-profit hit
        broker.process_pending_orders(make_candle(o="50000", h="52100", low="49900"))

        assert len(broker.closed_trades) == 1
        trade = broker.closed_trades[0]
        assert trade.signal_id == signal.signal_id
        assert trade.strategy_id == signal.strategy_id


class TestPartialFillCapping:
    def test_large_order_relative_to_volume_is_partially_filled(self) -> None:
        broker = make_broker(
            equity="1000000",
            risk_pct="50.0",
            partial_fill=PartialFillModel(volume_participation=Decimal("0.1"), enabled=True),
        )
        broker.place_signal(make_signal(entry="50000", sl="49000"), make_candle(volume="1"))
        broker.process_pending_orders(make_candle(volume="1"))

        # max_qty = volume(1) * 0.1 = 0.1; requested qty is much larger.
        assert len(broker.open_positions) == 1
        assert broker.open_positions[0].quantity == Decimal("0.1")
        assert len(broker._pending) == 1  # remainder requeued
        assert broker._pending[0].is_partial_remainder is True

    def test_requeued_remainder_adds_to_the_existing_position_with_vwap_entry(
        self,
    ) -> None:
        broker = make_broker(
            equity="1000000",
            risk_pct="50.0",
            partial_fill=PartialFillModel(volume_participation=Decimal("0.1"), enabled=True),
        )
        broker.place_signal(make_signal(entry="50000", sl="49000"), make_candle(volume="1"))
        broker.process_pending_orders(make_candle(o="50000", volume="1"))
        first_qty = broker.open_positions[0].quantity

        # Next bar: fills more of the remainder at a different open price.
        broker.process_pending_orders(
            make_candle(o="51000", h="51100", low="50900", c="51000", volume="1")
        )

        pos = broker.open_positions[0]
        assert pos.quantity > first_qty
        # VWAP entry must sit between the two fill prices.
        assert Decimal("50000") < pos.entry_price < Decimal("51000")

    def test_disabled_partial_fill_fills_the_full_quantity_at_once(self) -> None:
        broker = make_broker(equity="1000000", risk_pct="50.0")  # partial_fill disabled
        broker.place_signal(make_signal(entry="50000", sl="49000"), make_candle(volume="1"))
        broker.process_pending_orders(make_candle(volume="1"))
        assert broker._pending == []  # nothing requeued


class TestPositionSizingParity:
    """place_signal() used to reimplement fixed-fractional sizing inline
    (equity * risk_pct/100 / stop_distance), silently omitting the fee
    adjustment risk/position_sizing.py::calculate_position_size() applies
    live — the same "two implementations quietly drift apart" bug already
    found once in this codebase for Sharpe/Sortino's ddof convention.
    Backtests now delegate to the exact same function RiskManager uses
    live, via the broker's `sizing_fee_rate` (default matches
    calculate_position_size()'s own default of 0.0004).
    """

    def test_quantity_matches_calculate_position_size_directly(self) -> None:
        from src.risk.position_sizing import calculate_position_size

        broker = make_broker(equity="10000", risk_pct="1.0")
        broker.place_signal(make_signal(entry="50000", sl="49000"), make_candle())
        broker.process_pending_orders(make_candle())

        expected = calculate_position_size(
            equity=Decimal("10000"),
            risk_pct=Decimal("1.0"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            fee_rate=Decimal("0.0004"),
        )
        assert broker.open_positions[0].quantity == expected
        # And confirms the fee adjustment is actually applied — the old,
        # fee-naive formula would have produced exactly 0.1 here.
        assert broker.open_positions[0].quantity < Decimal("0.1")

    def test_custom_sizing_fee_rate_is_honored(self) -> None:
        broker = BacktestBroker(
            initial_equity=Decimal("10000"),
            commission=ZeroCommission(),
            slippage=ZeroSlippage(),
            risk_pct=Decimal("1.0"),
            partial_fill=PartialFillModel(enabled=False),
            sizing_fee_rate=Decimal("0"),
        )
        broker.place_signal(make_signal(entry="50000", sl="49000"), make_candle())
        broker.process_pending_orders(make_candle())

        # fee_rate=0 recovers the pre-fix formula exactly: 10000*0.01/1000
        assert broker.open_positions[0].quantity == Decimal("0.1")


class TestFundingRateAccrual:
    def test_long_position_pays_funding_on_a_positive_rate(self) -> None:
        settlement = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        broker = make_broker(
            equity="10000",
            funding_rates=[
                FundingRate(timestamp=settlement, symbol="BTCUSDT", rate=Decimal("0.0001"))
            ],
        )
        broker.place_signal(make_signal(), make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))

        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 9, 0, tzinfo=UTC)))

        pos = broker.open_positions[0]
        assert pos.funding_pnl < Decimal("0")  # long pays

    def test_short_position_receives_funding_on_a_positive_rate(self) -> None:
        settlement = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        broker = make_broker(
            equity="10000",
            funding_rates=[
                FundingRate(timestamp=settlement, symbol="BTCUSDT", rate=Decimal("0.0001"))
            ],
        )
        signal = make_signal(direction=SignalDirection.SHORT, entry="50000", sl="51000", tp="48000")
        broker.place_signal(signal, make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 9, 0, tzinfo=UTC)))

        pos = broker.open_positions[0]
        assert pos.funding_pnl > Decimal("0")  # short receives

    def test_a_settlement_is_only_applied_once(self) -> None:
        settlement = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        broker = make_broker(
            equity="10000",
            funding_rates=[
                FundingRate(timestamp=settlement, symbol="BTCUSDT", rate=Decimal("0.0001"))
            ],
        )
        broker.place_signal(make_signal(), make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 7, 0, tzinfo=UTC)))
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 9, 0, tzinfo=UTC)))
        funding_after_first_pass = broker.open_positions[0].funding_pnl

        # A later bar must not re-apply the same 08:00 settlement.
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 10, 0, tzinfo=UTC)))
        assert broker.open_positions[0].funding_pnl == funding_after_first_pass

    def test_no_open_positions_means_no_funding_applied(self) -> None:
        settlement = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        broker = make_broker(
            equity="10000",
            funding_rates=[
                FundingRate(timestamp=settlement, symbol="BTCUSDT", rate=Decimal("0.0001"))
            ],
        )
        broker.process_pending_orders(make_candle(ts=datetime(2024, 1, 1, 9, 0, tzinfo=UTC)))
        assert broker.equity == Decimal("10000")


class TestExitDetection:
    def test_long_stop_loss_hit_closes_the_position(self) -> None:
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="49500", h="49600", low="48900", c="49000"))
        assert broker.open_positions == []
        assert len(broker.closed_trades) == 1
        assert broker.closed_trades[0].gross_pnl < Decimal("0")

    def test_long_take_profit_hit_closes_the_position(self) -> None:
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="51500", h="52100", low="51400", c="52000"))
        assert broker.open_positions == []
        assert broker.closed_trades[0].gross_pnl > Decimal("0")

    def test_short_stop_loss_hit_closes_the_position(self) -> None:
        broker = make_broker()
        signal = make_signal(direction=SignalDirection.SHORT, entry="50000", sl="51000", tp="48000")
        broker.place_signal(signal, make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="50500", h="51100", low="50400", c="51000"))
        assert broker.open_positions == []
        assert broker.closed_trades[0].gross_pnl < Decimal("0")

    def test_short_take_profit_hit_closes_the_position(self) -> None:
        broker = make_broker()
        signal = make_signal(direction=SignalDirection.SHORT, entry="50000", sl="51000", tp="48000")
        broker.place_signal(signal, make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="48500", h="48600", low="47900", c="48000"))
        assert broker.open_positions == []
        assert broker.closed_trades[0].gross_pnl > Decimal("0")

    def test_no_exit_when_neither_stop_nor_tp_is_reached(self) -> None:
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="50100", h="50200", low="50000", c="50150"))
        assert len(broker.open_positions) == 1

    def test_gap_below_stop_uses_the_worse_open_price_for_a_long(self) -> None:
        # Gap opens below the stop entirely -> exit at the worse (lower)
        # of open/stop, not the stop price itself.
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        broker.process_pending_orders(make_candle(o="48000", h="48100", low="47900", c="47950"))
        assert broker.closed_trades[0].exit_price == Decimal("48000")

    def test_same_bar_entry_and_exit_is_excluded_from_exit_checks(self) -> None:
        # A position that fills on this exact bar must not also be
        # checked for exit on the same bar (look-ahead artifact guard).
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        # This bar both fills the entry AND would trigger the stop if checked.
        broker.process_pending_orders(make_candle(o="50000", h="50100", low="48500", c="49500"))
        assert len(broker.open_positions) == 1  # not exited on the fill bar


class TestFeeAccounting:
    def test_entry_commission_is_not_double_counted_on_exit(self) -> None:
        broker = BacktestBroker(
            initial_equity=Decimal("10000"),
            commission=None,  # defaults to PercentageCommission(0.04%)
            slippage=ZeroSlippage(),
            risk_pct=Decimal("1.0"),
            partial_fill=PartialFillModel(enabled=False),
        )
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))
        pos_fees_at_entry = broker.open_positions[0].total_fees
        assert pos_fees_at_entry > Decimal("0")

        broker.process_pending_orders(make_candle(o="52000", h="52100", low="51900", c="52000"))
        trade = broker.closed_trades[0]
        # Trade.fees = entry + exit commission, not double the entry alone.
        assert trade.fees > pos_fees_at_entry


class TestEquityProperty:
    def test_equity_includes_unrealized_pnl_of_open_positions(self) -> None:
        broker = make_broker()
        broker.place_signal(make_signal(entry="50000", sl="49000", tp="52000"), make_candle())
        broker.process_pending_orders(make_candle(o="50000"))

        cash_equity = broker._equity
        broker.process_pending_orders(make_candle(o="50100", h="51100", low="50050", c="51000"))
        assert broker.equity != cash_equity  # unrealized gain reflected

    def test_equity_with_no_positions_equals_cash(self) -> None:
        broker = make_broker()
        assert broker.equity == Decimal("10000")
