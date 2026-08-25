"""Unit tests for src.execution.execution_engine.ExecutionEngine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.enums import (
    AlertSeverity,
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalDirection,
    Timeframe,
)
from src.core.events import (
    AlertFiredEvent,
    EventBus,
    FundingRateEvent,
    MarkPriceUpdatedEvent,
    OrderFilledEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    TickReceivedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.domain.instrument import Instrument
from src.domain.order import Fill, Order
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.execution.execution_engine import ExecutionEngine
from src.execution.order_manager import OrderManager
from src.execution.position_manager import PositionManager


@pytest.fixture(autouse=True)
def isolated_event_bus():
    """Give every test its own EventBus so handlers/subscriptions don't leak."""
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def make_instrument(symbol: str = "BTCUSDT") -> Instrument:
    return Instrument(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        instrument_type=InstrumentType.PERPETUAL,
        tick_size=Decimal("0.01"),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
        min_notional=Decimal("5"),
        max_leverage=125,
        default_leverage=10,
    )


def make_signal_event(
    symbol: str = "BTCUSDT",
    direction: SignalDirection = SignalDirection.LONG,
    entry: str = "50000",
    sl: str = "49000",
    tp: str = "52000",
    score: float = 70.0,
    signal_id: str = "",
    timestamp: Any = None,
    open_fvgs: list[dict[str, str]] | None = None,
) -> SignalGeneratedEvent:
    kwargs: dict[str, Any] = {}
    if timestamp is not None:
        kwargs["timestamp"] = timestamp
    return SignalGeneratedEvent(
        source="test",
        symbol=symbol,
        timeframe=Timeframe.H1.value,
        direction=direction,
        score=score,
        confidence=0.7,
        entry_price=Decimal(entry),
        stop_loss=Decimal(sl),
        take_profit=Decimal(tp),
        strategy_id="test-strategy",
        signal_id=signal_id,
        open_fvgs=open_fvgs or [],
        **kwargs,
    )


@pytest.fixture
def order_manager() -> AsyncMock:
    om = AsyncMock(spec=OrderManager)
    om.get_open_orders.return_value = []
    return om


@pytest.fixture
def risk_manager() -> AsyncMock:
    rm = AsyncMock()
    rm.validate_signal.return_value = True
    rm.calculate_position_size.return_value = Decimal("0.1")
    return rm


@pytest.fixture
def position_manager() -> AsyncMock:
    return AsyncMock(spec=PositionManager)


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio()


@pytest.fixture
def instruments() -> dict[str, Instrument]:
    return {"BTCUSDT": make_instrument("BTCUSDT")}


@pytest.fixture
def engine(
    order_manager: AsyncMock,
    position_manager: AsyncMock,
    risk_manager: AsyncMock,
    portfolio: Portfolio,
    instruments: dict[str, Instrument],
) -> ExecutionEngine:
    return ExecutionEngine(
        order_manager=order_manager,
        position_manager=position_manager,
        risk_manager=risk_manager,
        portfolio=portfolio,
        instruments=instruments,
        use_limit_entries=False,  # simpler: forces MARKET orders in tests
        use_oco_exits=True,
    )


class TestStartStop:
    async def test_start_subscribes_to_signal_and_fill_events(
        self, engine: ExecutionEngine, isolated_event_bus: EventBus
    ) -> None:
        await engine.start()
        assert engine.stats["running"] is True

    async def test_start_subscribes_to_mark_price_events_regardless_of_trailing_flag(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
        isolated_event_bus: EventBus,
    ) -> None:
        # Mark price feeds RiskManager's margin/exposure checks, not just
        # trailing-stop management, so it must be wired even when trailing
        # exits are disabled.
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            manage_trailing_exits=False,
        )
        await engine.start()
        assert len(isolated_event_bus._handlers["MarkPriceUpdatedEvent"]) == 1  # type: ignore[attr-defined]

    async def test_stop_sets_running_false(self, engine: ExecutionEngine) -> None:
        await engine.start()
        await engine.stop()
        assert engine.stats["running"] is False


class TestOnSignalEntrySubmission:
    async def test_ignores_non_signal_events(
        self, engine: ExecutionEngine, order_manager: AsyncMock
    ) -> None:
        await engine.start()
        await engine._on_signal(object())
        order_manager.submit_order.assert_not_awaited()

    async def test_does_nothing_when_not_running(
        self, engine: ExecutionEngine, order_manager: AsyncMock
    ) -> None:
        # Deliberately not calling start().
        await engine._on_signal(make_signal_event())
        order_manager.submit_order.assert_not_awaited()

    async def test_skips_signal_for_an_unconfigured_symbol(
        self, engine: ExecutionEngine, order_manager: AsyncMock
    ) -> None:
        await engine.start()
        await engine._on_signal(make_signal_event(symbol="UNKNOWNCOIN"))
        order_manager.submit_order.assert_not_awaited()

    async def test_submits_a_market_entry_order_for_a_valid_signal(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        submitted_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = submitted_order

        await engine.start()
        await engine._on_signal(make_signal_event())

        order_manager.submit_order.assert_awaited_once()
        assert engine.stats["orders_submitted"] == 1

    async def test_skips_when_risk_manager_rejects_the_signal(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        risk_manager.validate_signal.side_effect = ValueError("risk rejected")

        await engine.start()
        await engine._on_signal(make_signal_event())

        order_manager.submit_order.assert_not_awaited()
        assert engine.stats["orders_failed"] == 1

    async def test_skips_when_position_size_rounds_to_zero(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        risk_manager.calculate_position_size.return_value = Decimal("0")

        await engine.start()
        await engine._on_signal(make_signal_event())

        order_manager.submit_order.assert_not_awaited()
        # This is a "skip", not a validation failure, so it should not
        # increment orders_failed.
        assert engine.stats["orders_failed"] == 0


class TestOnSignalIdLineage:
    """
    Regression coverage for the signal_id lineage bug: ExecutionEngine
    used to reconstruct a Signal without forwarding signal_id at all, so
    every order/position/trade downstream carried a signal_id that
    matched nothing in the persisted signals table (a fresh uuid4 minted
    on reconstruction, never the id _persist_signal() actually wrote).
    """

    async def test_reconstructed_signal_carries_the_events_signal_id(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        await engine.start()
        await engine._on_signal(make_signal_event(signal_id="orig-signal-abc123"))

        validated_signal = risk_manager.validate_signal.call_args.args[0]
        assert validated_signal.signal_id == "orig-signal-abc123"

    async def test_event_without_signal_id_falls_back_to_a_fresh_one(
        self,
        engine: ExecutionEngine,
        risk_manager: AsyncMock,
    ) -> None:
        # Older/test events that don't set signal_id must not end up with
        # a blank id — Signal's own default_factory should still apply.
        await engine.start()
        await engine._on_signal(make_signal_event())

        validated_signal = risk_manager.validate_signal.call_args.args[0]
        assert validated_signal.signal_id != ""


class TestOnSignalExpiredInTransit:
    """
    Regression coverage for SignalExpiredEvent wiring: the event type
    existed but nothing published it for a signal that aged out while
    sitting on the event bus. This also exercises the generated_at
    backfill fix — without it, a reconstructed Signal always got a fresh
    "now" timestamp and is_expired() could never fire.
    """

    async def test_stale_event_is_discarded_and_publishes_signal_expired_event(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        expired_received: list[SignalExpiredEvent] = []
        isolated_event_bus.subscribe(SignalExpiredEvent, expired_received.append)

        old_timestamp = datetime(2020, 1, 1, tzinfo=UTC)
        await engine.start()
        await engine._on_signal(
            make_signal_event(signal_id="stale-signal-1", timestamp=old_timestamp)
        )

        order_manager.submit_order.assert_not_awaited()
        assert len(expired_received) == 1
        assert expired_received[0].signal_id == "stale-signal-1"
        assert expired_received[0].reason == "expired_in_transit"

    async def test_fresh_event_does_not_publish_signal_expired_event(
        self,
        engine: ExecutionEngine,
        isolated_event_bus: EventBus,
    ) -> None:
        expired_received: list[SignalExpiredEvent] = []
        isolated_event_bus.subscribe(SignalExpiredEvent, expired_received.append)

        await engine.start()
        await engine._on_signal(make_signal_event())

        assert expired_received == []


class TestOnSignalDuplicatePrevention:
    async def test_skips_a_second_signal_for_a_symbol_with_a_pending_entry(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        submitted_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = submitted_order

        await engine.start()
        await engine._on_signal(make_signal_event())  # first signal: submits
        order_manager.submit_order.reset_mock()

        await engine._on_signal(make_signal_event())  # second: must be skipped
        order_manager.submit_order.assert_not_awaited()

    async def test_skips_a_signal_when_an_open_order_already_exists_for_the_symbol(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
    ) -> None:
        # Simulates an order that's open on the exchange but wasn't
        # submitted through this engine instance's _pending_signals
        # tracking (e.g. after a restart) -- the open-orders check
        # catches this independently.
        existing_open_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.get_open_orders.return_value = [existing_open_order]

        await engine.start()
        await engine._on_signal(make_signal_event())

        order_manager.submit_order.assert_not_awaited()


class TestOnFillCreatesPosition:
    async def test_fill_of_a_tracked_order_creates_a_position(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        position_manager: AsyncMock,
    ) -> None:
        order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = order

        await engine.start()
        await engine._on_signal(make_signal_event())

        # Simulate the fill: mutate the same Order object OrderManager
        # is tracking, then have get_order return it.
        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.1"),
            fee=Decimal("2"), fee_asset="USDT",
        ))  # auto-transitions to FILLED since filled_quantity >= quantity
        order_manager.get_order.return_value = order

        fill_event = OrderFilledEvent(
            source="test", order_id=order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("2"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)

        assert len(portfolio.positions) == 1
        position_manager.register.assert_called_once()

    async def test_open_fvgs_are_classified_into_target_and_reversal_tags(
        self,
        engine: ExecutionEngine,
        order_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        position_manager: AsyncMock,
    ) -> None:
        # Regression: fvg_at_price() had no caller anywhere in exit
        # management. This confirms the entry-time snapshot classifies
        # zones correctly for a LONG position: bullish FVGs (same
        # direction) -> fvg_target_zones, bearish FVGs (opposing) ->
        # fvg_reversal_zones.
        order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = order

        await engine.start()
        await engine._on_signal(make_signal_event(
            direction=SignalDirection.LONG,
            open_fvgs=[
                {"type": "bullish", "top": "50500", "bottom": "50300"},
                {"type": "bearish", "top": "49900", "bottom": "49700"},
            ],
        ))

        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.1"),
            fee=Decimal("2"), fee_asset="USDT",
        ))
        order_manager.get_order.return_value = order

        fill_event = OrderFilledEvent(
            source="test", order_id=order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("2"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)

        assert len(portfolio.positions) == 1
        position = next(iter(portfolio.positions.values()))
        assert position.tags["fvg_target_zones"] == [
            {"top": "50500", "bottom": "50300"}
        ]
        assert position.tags["fvg_reversal_zones"] == [
            {"top": "49900", "bottom": "49700"}
        ]

    async def test_ignores_fill_events_for_untracked_orders(
        self, engine: ExecutionEngine, order_manager: AsyncMock, portfolio: Portfolio
    ) -> None:
        order_manager.get_order.return_value = None
        await engine.start()

        fill_event = OrderFilledEvent(
            source="test", order_id="unknown-order", symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("0"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)
        assert len(portfolio.positions) == 0

    async def test_ignores_non_terminal_partial_fill(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        risk_manager: AsyncMock, portfolio: Portfolio,
    ) -> None:
        order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = order
        await engine.start()
        await engine._on_signal(make_signal_event())

        order.transition_to(OrderStatus.SUBMITTED)
        order.transition_to(OrderStatus.OPEN)
        order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.05"),
            fee=Decimal("1"), fee_asset="USDT",
        ))  # auto-transitions to PARTIALLY_FILLED (not terminal)
        order_manager.get_order.return_value = order

        fill_event = OrderFilledEvent(
            source="test", order_id=order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.05"),
            fill_price=Decimal("50000"), fee=Decimal("1"), fee_asset="USDT",
            is_partial=True,
        )
        await engine._on_fill(fill_event)
        assert len(portfolio.positions) == 0  # position not yet created


class TestPlaceExitOrdersUnprotectedPositionAlert:
    """The most safety-critical branch: if protective orders fail to
    place after entry, the engine must fire a CRITICAL alert rather
    than silently leave a leveraged position with no stop-loss."""

    async def test_stop_loss_placement_success_places_take_profit_too(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        risk_manager: AsyncMock, portfolio: Portfolio,
    ) -> None:
        entry_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )
        order_manager.submit_order.return_value = entry_order
        await engine.start()
        await engine._on_signal(make_signal_event())

        entry_order.transition_to(OrderStatus.SUBMITTED)
        entry_order.transition_to(OrderStatus.OPEN)
        entry_order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.1"),
            fee=Decimal("2"), fee_asset="USDT",
        ))  # auto-transitions to FILLED
        order_manager.get_order.return_value = entry_order

        # Subsequent submit_order calls (SL, then TP) succeed.
        order_manager.submit_order.side_effect = None
        order_manager.submit_order.return_value = Order(
            symbol="BTCUSDT", side=OrderSide.SELL,
            order_type=OrderType.STOP_MARKET, quantity=Decimal("0.1"),
            stop_price=Decimal("49000"),
        )

        fill_event = OrderFilledEvent(
            source="test", order_id=entry_order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("2"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)

        # Entry (1) + SL (1) + TP (1) = 3 submit_order calls total.
        assert order_manager.submit_order.await_count == 3

    async def test_stop_loss_failure_after_all_retries_fires_critical_alert(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        risk_manager: AsyncMock, portfolio: Portfolio, isolated_event_bus: EventBus,
    ) -> None:
        entry_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )

        alerts_received: list[AlertFiredEvent] = []

        async def capture_alert(event: AlertFiredEvent) -> None:
            alerts_received.append(event)

        isolated_event_bus.subscribe(AlertFiredEvent, capture_alert)

        call_count = {"n": 0}

        async def submit_side_effect(order: Order) -> Order:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return entry_order  # entry succeeds
            raise RuntimeError("exchange rejected stop-loss order")  # SL fails, every retry

        order_manager.submit_order.side_effect = submit_side_effect

        await engine.start()
        await engine._on_signal(make_signal_event())

        entry_order.transition_to(OrderStatus.SUBMITTED)
        entry_order.transition_to(OrderStatus.OPEN)
        entry_order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.1"),
            fee=Decimal("2"), fee_asset="USDT",
        ))  # auto-transitions to FILLED
        order_manager.get_order.return_value = entry_order

        fill_event = OrderFilledEvent(
            source="test", order_id=entry_order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("2"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)

        # Position must still have been created (entry succeeded)...
        assert len(portfolio.positions) == 1
        # ...but a CRITICAL unprotected_position alert must have fired.
        assert len(alerts_received) == 1
        assert alerts_received[0].severity == AlertSeverity.CRITICAL
        assert alerts_received[0].alert_name == "unprotected_position"
        assert "BTCUSDT" in alerts_received[0].message

    async def test_take_profit_only_failure_fires_a_warning_not_critical_alert(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        risk_manager: AsyncMock, portfolio: Portfolio, isolated_event_bus: EventBus,
    ) -> None:
        # Stop-loss succeeds (position IS protected); take-profit fails.
        # This is documented as a degraded-but-safe state and must fire
        # a distinct, lower-severity alert (take_profit_placement_failed
        # / WARNING) rather than the same CRITICAL unprotected_position
        # alert used for a failed stop-loss.
        entry_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=Decimal("0.1"),
        )

        alerts_received: list[AlertFiredEvent] = []

        async def capture_alert(event: AlertFiredEvent) -> None:
            alerts_received.append(event)

        isolated_event_bus.subscribe(AlertFiredEvent, capture_alert)

        call_count = {"n": 0}

        async def submit_side_effect(order: Order) -> Order:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return entry_order  # entry
            if call_count["n"] == 2:
                return Order(  # stop-loss succeeds
                    symbol="BTCUSDT", side=OrderSide.SELL,
                    order_type=OrderType.STOP_MARKET, quantity=Decimal("0.1"),
                    stop_price=Decimal("49000"),
                )
            raise RuntimeError("take-profit rejected")  # TP fails

        order_manager.submit_order.side_effect = submit_side_effect

        await engine.start()
        await engine._on_signal(make_signal_event())

        entry_order.transition_to(OrderStatus.SUBMITTED)
        entry_order.transition_to(OrderStatus.OPEN)
        entry_order.add_fill(Fill(
            fill_id="f1", price=Decimal("50000"), quantity=Decimal("0.1"),
            fee=Decimal("2"), fee_asset="USDT",
        ))  # auto-transitions to FILLED
        order_manager.get_order.return_value = entry_order

        fill_event = OrderFilledEvent(
            source="test", order_id=entry_order.order_id, symbol="BTCUSDT",
            side=OrderSide.BUY, filled_quantity=Decimal("0.1"),
            fill_price=Decimal("50000"), fee=Decimal("2"), fee_asset="USDT",
            is_partial=False,
        )
        await engine._on_fill(fill_event)

        assert len(portfolio.positions) == 1
        # Exactly one alert, and it must be the WARNING-severity
        # take-profit alert, NOT the CRITICAL unprotected_position one.
        assert len(alerts_received) == 1
        assert alerts_received[0].severity == AlertSeverity.WARNING
        assert alerts_received[0].alert_name == "take_profit_placement_failed"


class TestStats:
    async def test_stats_reflects_engine_state(self, engine: ExecutionEngine) -> None:
        stats = engine.stats
        assert stats["orders_submitted"] == 0
        assert stats["orders_failed"] == 0
        assert stats["pending_signals"] == 0
        assert stats["running"] is False


def make_position(
    *,
    side: PositionSide = PositionSide.LONG,
    entry: str = "50000",
    mark: str = "50000",
    sl: str = "49000",
    tp: str = "52000",
    entry_atr: str | None = "100",
) -> Position:
    return Position(
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"),
        entry_price=Decimal(entry),
        mark_price=Decimal(mark),
        stop_loss=Decimal(sl),
        take_profit=Decimal(tp),
        tags={"entry_atr": entry_atr} if entry_atr is not None else {},
    )


class TestTickDrivenExitManagement:
    async def test_start_subscribes_to_tick_events_by_default(
        self, engine: ExecutionEngine, isolated_event_bus: EventBus,
    ) -> None:
        await engine.start()
        assert len(isolated_event_bus._handlers["TickReceivedEvent"]) == 1  # type: ignore[attr-defined]

    async def test_tick_ignored_when_no_open_position(
        self, engine: ExecutionEngine, position_manager: AsyncMock, order_manager: AsyncMock,
    ) -> None:
        position_manager.get_by_symbol.return_value = []
        await engine.start()
        await engine._on_tick(TickReceivedEvent(
            source="test", symbol="BTCUSDT", price=Decimal("51000"),
            quantity=Decimal("1"), is_buyer_maker=False,
        ))
        order_manager.submit_order.assert_not_awaited()
        order_manager.cancel_order.assert_not_awaited()

    async def test_tick_does_not_update_mark_price(
        self, engine: ExecutionEngine, position_manager: AsyncMock,
    ) -> None:
        # position.mark_price is exchange margin/PnL data and must be
        # sourced only from MarkPriceUpdatedEvent (see TestMarkPriceUpdates
        # below) — trade ticks only drive *when* trailing-stop logic
        # re-evaluates, using the trade price as that decision's input.
        position = make_position()
        position_manager.get_by_symbol.return_value = [position]
        await engine.start()
        await engine._on_tick(TickReceivedEvent(
            source="test", symbol="BTCUSDT", price=Decimal("50100"),
            quantity=Decimal("1"), is_buyer_maker=False,
        ))
        position_manager.update_mark_price.assert_not_called()

    async def test_breakeven_move_cancels_old_stop_and_submits_a_new_one(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
    ) -> None:
        position = make_position(entry="50000", sl="49000")  # 1000 risk
        engine._active_stop_orders[position.position_id] = "old-stop-order-id"
        new_stop_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, order_type=OrderType.STOP_MARKET,
            quantity=Decimal("0.1"), stop_price=Decimal("50000"), reduce_only=True,
        )
        order_manager.submit_order.return_value = new_stop_order

        await engine.start()
        # 1000 profit >= 1000 risk (1R) -> breakeven should trigger
        await engine._manage_position_exit(position, Decimal("51000"))

        order_manager.cancel_order.assert_awaited_once_with("old-stop-order-id", "BTCUSDT")
        order_manager.submit_order.assert_awaited_once()
        assert position.breakeven_triggered is True
        assert position.stop_loss == new_stop_order.stop_price
        assert engine._active_stop_orders[position.position_id] == new_stop_order.order_id

    async def test_trailing_stop_update_only_fires_when_it_improves(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
    ) -> None:
        # Already at breakeven, in a low-vol regime (entry_atr=50 vs price
        # ~50000 -> 0.1% -> "low" -> 1.5x multiplier): new trail =
        # 51500 - 50*1.5 = 51425, comfortably above the current 50000 stop.
        position = make_position(entry="50000", sl="50000", entry_atr="50")
        position.breakeven_triggered = True
        engine._active_stop_orders[position.position_id] = "old-stop-order-id"
        new_stop_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, order_type=OrderType.STOP_MARKET,
            quantity=Decimal("0.1"), stop_price=Decimal("51425"), reduce_only=True,
        )
        order_manager.submit_order.return_value = new_stop_order

        await engine.start()
        await engine._manage_position_exit(position, Decimal("51500"))

        order_manager.submit_order.assert_awaited_once()
        assert position.stop_loss == Decimal("51425")

    async def test_trailing_stop_update_logs_volatility_regime(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Regression: ExitPlan.volatility_regime was computed on every
        trailing-stop pass but never surfaced anywhere, making it
        impossible to see from logs which regime drove a given trail
        distance. It must now appear on the trailing-stop path (not the
        breakeven path, which isn't regime-derived)."""
        import logging

        position = make_position(entry="50000", sl="50000", entry_atr="50")
        position.breakeven_triggered = True  # skip the breakeven branch
        new_stop_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, order_type=OrderType.STOP_MARKET,
            quantity=Decimal("0.1"), stop_price=Decimal("51425"), reduce_only=True,
        )
        order_manager.submit_order.return_value = new_stop_order

        await engine.start()
        with caplog.at_level(logging.INFO, logger="src.execution.execution_engine"):
            await engine._manage_position_exit(position, Decimal("51500"))

        assert any("regime=low" in rec.message for rec in caplog.records)

    async def test_breakeven_move_does_not_log_a_regime(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The breakeven target isn't regime-derived, so its log line
        must not claim one."""
        import logging

        position = make_position(entry="50000", sl="49000")
        new_stop_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, order_type=OrderType.STOP_MARKET,
            quantity=Decimal("0.1"), stop_price=Decimal("50000"), reduce_only=True,
        )
        order_manager.submit_order.return_value = new_stop_order

        await engine.start()
        with caplog.at_level(logging.INFO, logger="src.execution.execution_engine"):
            await engine._manage_position_exit(position, Decimal("51000"))

        stop_logs = [r for r in caplog.records if "Stop-loss updated" in r.message]
        assert stop_logs
        assert "regime=" not in stop_logs[0].message

    async def test_no_stop_update_when_trail_would_not_improve(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
    ) -> None:
        # Price barely above entry with a wide ATR -> trail candidate is
        # below the existing stop, so nothing should be submitted.
        position = make_position(entry="50000", sl="49900", entry_atr="500")
        position.breakeven_triggered = True

        await engine.start()
        await engine._manage_position_exit(position, Decimal("50050"))

        order_manager.cancel_order.assert_not_awaited()
        order_manager.submit_order.assert_not_awaited()

    async def test_stop_or_tp_already_hit_is_left_to_the_exchange_order(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
    ) -> None:
        position = make_position(entry="50000", sl="49000", tp="52000")

        await engine.start()
        await engine._manage_position_exit(position, Decimal("48900"))  # past SL

        order_manager.cancel_order.assert_not_awaited()
        order_manager.submit_order.assert_not_awaited()

    async def test_cancel_failure_skips_the_update_instead_of_double_submitting(
        self, engine: ExecutionEngine, order_manager: AsyncMock,
    ) -> None:
        position = make_position(entry="50000", sl="49000")
        engine._active_stop_orders[position.position_id] = "old-stop-order-id"
        order_manager.cancel_order.side_effect = RuntimeError("already filled")

        await engine.start()
        await engine._manage_position_exit(position, Decimal("51000"))

        order_manager.submit_order.assert_not_awaited()
        assert position.breakeven_triggered is False  # local state left untouched

    async def test_manage_trailing_exits_false_disables_tick_subscription(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
        isolated_event_bus: EventBus,
    ) -> None:
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            manage_trailing_exits=False,
        )
        await engine.start()
        assert len(isolated_event_bus._handlers["TickReceivedEvent"]) == 0  # type: ignore[attr-defined]


class TestMarkPriceUpdates:
    """position.mark_price is exchange margin/PnL data — see docstring on
    ExecutionEngine._on_mark_price for why it's sourced only from
    MarkPriceUpdatedEvent rather than trade ticks or candle closes."""

    async def test_ignores_non_mark_price_events(
        self, engine: ExecutionEngine, position_manager: AsyncMock,
    ) -> None:
        await engine.start()
        await engine._on_mark_price(object())
        position_manager.update_mark_price.assert_not_called()

    async def test_does_nothing_when_not_running(
        self, engine: ExecutionEngine, position_manager: AsyncMock,
    ) -> None:
        # Deliberately not calling start().
        await engine._on_mark_price(MarkPriceUpdatedEvent(
            source="test", symbol="BTCUSDT", mark_price=Decimal("50100"),
        ))
        position_manager.update_mark_price.assert_not_called()

    async def test_ignored_when_no_open_position(
        self, engine: ExecutionEngine, position_manager: AsyncMock,
    ) -> None:
        position_manager.get_by_symbol.return_value = []
        await engine.start()
        await engine._on_mark_price(MarkPriceUpdatedEvent(
            source="test", symbol="BTCUSDT", mark_price=Decimal("50100"),
        ))
        position_manager.update_mark_price.assert_not_called()

    async def test_updates_mark_price_for_open_positions(
        self, engine: ExecutionEngine, position_manager: AsyncMock,
    ) -> None:
        position = make_position()
        position_manager.get_by_symbol.return_value = [position]
        await engine.start()
        await engine._on_mark_price(MarkPriceUpdatedEvent(
            source="test", symbol="BTCUSDT", mark_price=Decimal("50250.5"),
        ))
        position_manager.update_mark_price.assert_called_once_with(
            "BTCUSDT", Decimal("50250.5"),
        )

    async def test_updates_mark_price_even_when_trailing_exits_disabled(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> None:
        position = make_position()
        position_manager.get_by_symbol.return_value = [position]
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            manage_trailing_exits=False,
        )
        await engine.start()
        await engine._on_mark_price(MarkPriceUpdatedEvent(
            source="test", symbol="BTCUSDT", mark_price=Decimal("50250.5"),
        ))
        position_manager.update_mark_price.assert_called_once_with(
            "BTCUSDT", Decimal("50250.5"),
        )


class TestFundingRateUpdates:
    """FundingRateEvent settlement — see ExecutionEngine._on_funding_rate.

    Gated by track_funding_pnl (default False): the default `engine`
    fixture doesn't pass it, so it's off in every test here unless a
    test builds its own engine with track_funding_pnl=True.
    """

    async def test_not_subscribed_by_default(
        self, engine: ExecutionEngine, isolated_event_bus: EventBus,
    ) -> None:
        await engine.start()
        assert len(isolated_event_bus._handlers.get("FundingRateEvent", []))  == 0  # type: ignore[attr-defined]

    async def test_subscribed_when_enabled(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
        isolated_event_bus: EventBus,
    ) -> None:
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            track_funding_pnl=True,
        )
        await engine.start()
        assert len(isolated_event_bus._handlers["FundingRateEvent"]) == 1  # type: ignore[attr-defined]

    async def test_ignores_non_funding_events(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> None:
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            track_funding_pnl=True,
        )
        await engine.start()
        await engine._on_funding_rate(object())
        position_manager.apply_funding.assert_not_called()

    async def test_does_nothing_when_not_running(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> None:
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            track_funding_pnl=True,
        )
        # Deliberately not calling start().
        await engine._on_funding_rate(FundingRateEvent(
            source="test", symbol="BTCUSDT", rate=Decimal("0.0001"),
            next_funding_time=datetime.now(tz=UTC),
        ))
        position_manager.apply_funding.assert_not_called()

    async def test_ignored_when_no_open_position(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> None:
        position_manager.get_by_symbol.return_value = []
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            track_funding_pnl=True,
        )
        await engine.start()
        await engine._on_funding_rate(FundingRateEvent(
            source="test", symbol="BTCUSDT", rate=Decimal("0.0001"),
            next_funding_time=datetime.now(tz=UTC),
        ))
        position_manager.apply_funding.assert_not_called()

    async def test_settles_funding_for_open_positions(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> None:
        position = make_position()
        position_manager.get_by_symbol.return_value = [position]
        engine = ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
            track_funding_pnl=True,
        )
        await engine.start()
        await engine._on_funding_rate(FundingRateEvent(
            source="test", symbol="BTCUSDT", rate=Decimal("0.0001"),
            next_funding_time=datetime.now(tz=UTC),
        ))
        position_manager.apply_funding.assert_called_once_with(
            "BTCUSDT", Decimal("0.0001"),
        )


class TestPositionUpdatedEventIsPublished:
    """PositionUpdatedEvent was schema-only with no producer.

    ExecutionEngine._on_mark_price() now publishes it for every open
    position after updating mark prices, giving dashboards and subscribers
    live unrealized-PnL visibility without polling PositionManager.
    """

    @pytest.fixture
    def engine(
        self,
        order_manager: AsyncMock,
        position_manager: AsyncMock,
        risk_manager: AsyncMock,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
    ) -> ExecutionEngine:
        return ExecutionEngine(
            order_manager=order_manager,
            position_manager=position_manager,
            risk_manager=risk_manager,
            portfolio=portfolio,
            instruments=instruments,
        )

    async def test_publishes_position_updated_event_on_mark_price(
        self,
        engine: ExecutionEngine,
        position_manager: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[PositionUpdatedEvent] = []

        async def _handler(event: object) -> None:
            if isinstance(event, PositionUpdatedEvent):
                received.append(event)

        isolated_event_bus.subscribe(PositionUpdatedEvent, _handler)

        position = make_position()
        position_manager.get_by_symbol.return_value = [position]
        await engine.start()

        await engine._on_mark_price(
            MarkPriceUpdatedEvent(source="test", symbol="BTCUSDT", mark_price=Decimal("51000"))
        )

        assert len(received) == 1
        evt = received[0]
        assert evt.position_id == position.position_id
        assert evt.symbol == "BTCUSDT"
        assert evt.quantity == position.quantity

    async def test_publishes_one_event_per_open_position(
        self,
        engine: ExecutionEngine,
        position_manager: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[PositionUpdatedEvent] = []

        async def _handler(event: object) -> None:
            if isinstance(event, PositionUpdatedEvent):
                received.append(event)

        isolated_event_bus.subscribe(PositionUpdatedEvent, _handler)

        pos_a = make_position()
        pos_b = make_position()
        position_manager.get_by_symbol.return_value = [pos_a, pos_b]
        await engine.start()

        await engine._on_mark_price(
            MarkPriceUpdatedEvent(source="test", symbol="BTCUSDT", mark_price=Decimal("51000"))
        )

        assert len(received) == 2
        position_ids = {e.position_id for e in received}
        assert pos_a.position_id in position_ids
        assert pos_b.position_id in position_ids

    async def test_no_event_when_no_open_positions(
        self,
        engine: ExecutionEngine,
        position_manager: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[PositionUpdatedEvent] = []

        async def _handler(event: object) -> None:
            if isinstance(event, PositionUpdatedEvent):
                received.append(event)

        isolated_event_bus.subscribe(PositionUpdatedEvent, _handler)

        position_manager.get_by_symbol.return_value = []
        await engine.start()

        await engine._on_mark_price(
            MarkPriceUpdatedEvent(source="test", symbol="BTCUSDT", mark_price=Decimal("51000"))
        )

        assert len(received) == 0

    async def test_no_event_when_not_running(
        self,
        engine: ExecutionEngine,
        position_manager: AsyncMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[PositionUpdatedEvent] = []

        async def _handler(event: object) -> None:
            if isinstance(event, PositionUpdatedEvent):
                received.append(event)

        isolated_event_bus.subscribe(PositionUpdatedEvent, _handler)

        # Deliberately skip engine.start() so _running stays False
        await engine._on_mark_price(
            MarkPriceUpdatedEvent(source="test", symbol="BTCUSDT", mark_price=Decimal("51000"))
        )

        assert len(received) == 0
