"""Unit tests for src.strategy.orchestrator.StrategyOrchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

import src.strategy.filters as filters_module
from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.core.events import (
    CandleClosedEvent,
    EventBus,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.domain.signal import Signal
from src.core.exceptions import NoSignalError, SignalGenerationError
from src.core.interfaces import IStrategy
from src.market_data.cache import MarketDataCache
from src.market_data.candles import CandleService
from src.strategy.orchestrator import StrategyOrchestrator


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def make_candle_event(symbol: str = "BTCUSDT", timeframe: str = "1h") -> CandleClosedEvent:
    return CandleClosedEvent(
        source="test", symbol=symbol, timeframe=timeframe,
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100.5"),
        volume=Decimal("1000"), candle_timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_signal(
    symbol: str = "BTCUSDT",
    *,
    generated_at: datetime | None = None,
    max_age_seconds: float = 3600.0,
) -> Signal:
    kwargs: dict = {}
    if generated_at is not None:
        kwargs["generated_at"] = generated_at
    return Signal(
        strategy_id="test-strategy", symbol=symbol, timeframe=Timeframe.H1,
        direction=SignalDirection.LONG, score=70.0, strength=SignalStrength.STRONG,
        confidence=0.7, entry_price=Decimal("50000"), stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"), max_age_seconds=max_age_seconds, **kwargs,
    )


def make_strategy(
    strategy_id: str = "test-strategy",
    symbols: list[str] | None = None,
    timeframes: list[Timeframe] | None = None,
    signal_to_return: Signal | None = None,
) -> AsyncMock:
    strategy = AsyncMock(spec=IStrategy)
    strategy.strategy_id = strategy_id
    strategy.symbols = symbols or ["BTCUSDT"]
    strategy.timeframes = timeframes or [Timeframe.H1]
    strategy.on_candle.return_value = signal_to_return
    return strategy


@pytest.fixture
def candle_service() -> AsyncMock:
    service = AsyncMock(spec=CandleService)
    service.get_candles.return_value = []
    return service


@pytest.fixture
def orchestrator(candle_service: AsyncMock) -> StrategyOrchestrator:
    return StrategyOrchestrator(candle_service)


class TestRegisterStrategy:
    def test_registers_a_strategy(self, orchestrator: StrategyOrchestrator) -> None:
        orchestrator.register_strategy(make_strategy())
        assert orchestrator.stats["strategies"] == 1

    def test_raises_on_duplicate_strategy_id(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        orchestrator.register_strategy(make_strategy(strategy_id="dup"))
        with pytest.raises(ValueError, match="already registered"):
            orchestrator.register_strategy(make_strategy(strategy_id="dup"))


class TestStartStop:
    async def test_start_calls_initialize_on_all_registered_strategies(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        strategy = make_strategy()
        orchestrator.register_strategy(strategy)
        await orchestrator.start()
        strategy.initialize.assert_awaited_once()
        assert orchestrator.stats["running"] is True

    async def test_stop_sets_running_false(self, orchestrator: StrategyOrchestrator) -> None:
        await orchestrator.start()
        await orchestrator.stop()
        assert orchestrator.stats["running"] is False


class TestCandleDispatchFiltering:
    async def test_ignores_non_candle_closed_events(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        strategy = make_strategy()
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(object())
        strategy.on_candle.assert_not_awaited()

    async def test_skips_a_strategy_not_subscribed_to_the_symbol(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        strategy = make_strategy(symbols=["ETHUSDT"])
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event(symbol="BTCUSDT"))
        strategy.on_candle.assert_not_awaited()

    async def test_skips_a_strategy_not_subscribed_to_the_timeframe(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        strategy = make_strategy(timeframes=[Timeframe.M15])
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event(timeframe="1h"))
        strategy.on_candle.assert_not_awaited()

    async def test_dispatches_to_a_matching_strategy(
        self, orchestrator: StrategyOrchestrator, candle_service: AsyncMock
    ) -> None:
        strategy = make_strategy(symbols=["BTCUSDT"], timeframes=[Timeframe.H1])
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        strategy.on_candle.assert_awaited_once()


class TestSignalPublication:
    async def test_a_generated_signal_is_published_to_the_event_bus(
        self, orchestrator: StrategyOrchestrator, isolated_event_bus: EventBus
    ) -> None:
        received: list[SignalGeneratedEvent] = []

        async def capture(event: SignalGeneratedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(SignalGeneratedEvent, capture)

        signal = make_signal()
        strategy = make_strategy(signal_to_return=signal)
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())

        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"
        # Regression: the event previously didn't carry signal_id at all,
        # so ExecutionEngine reconstructed a *different* Signal.signal_id
        # (a fresh uuid4) than the one _persist_signal() already wrote to
        # the signals table — every downstream order/position/trade's
        # signal_id pointed at a row that doesn't exist.
        assert received[0].signal_id == signal.signal_id
        assert orchestrator.stats["signals_generated"] == 1

    async def test_open_fvgs_metadata_is_carried_onto_the_event(
        self, orchestrator: StrategyOrchestrator, isolated_event_bus: EventBus
    ) -> None:
        # Regression: SignalGeneratedEvent didn't carry metadata at all, so
        # ExecutionEngine's reconstructed Signal always had an empty
        # metadata dict on this path — any FVG target/reversal zone
        # classification silently resolved to nothing no matter what
        # confluence.py put in the original Signal's metadata.
        received: list[SignalGeneratedEvent] = []

        async def capture(event: SignalGeneratedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(SignalGeneratedEvent, capture)

        signal = make_signal()
        signal.metadata["open_fvgs"] = [
            {"type": "bullish", "top": "50500", "bottom": "50300"}
        ]
        strategy = make_strategy(signal_to_return=signal)
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())

        assert len(received) == 1
        assert received[0].open_fvgs == [
            {"type": "bullish", "top": "50500", "bottom": "50300"}
        ]

    async def test_no_signal_returned_publishes_nothing(
        self, orchestrator: StrategyOrchestrator, isolated_event_bus: EventBus
    ) -> None:
        received: list[SignalGeneratedEvent] = []

        async def capture(event: SignalGeneratedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(SignalGeneratedEvent, capture)

        strategy = make_strategy(signal_to_return=None)
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert received == []
        assert orchestrator.stats["signals_generated"] == 0

    async def test_no_signal_raises_and_is_caught_as_control_flow(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        """
        NoSignalError (core/exceptions.py) is a control-flow sentinel, not
        a fault: the orchestrator raises it internally when a strategy
        returns None, then catches it itself in the same dispatch loop.
        It must never propagate out of _on_candle_closed() or be logged
        as an error — see test_no_signal_returned_publishes_nothing for
        the externally-observable half of this contract.
        """
        strategy = make_strategy(signal_to_return=None)
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        # Must not raise NoSignalError (or anything else) out to the caller.
        await orchestrator._on_candle_closed(make_candle_event())

    async def test_strategy_exception_is_wrapped_as_signal_generation_error(
        self, orchestrator: StrategyOrchestrator, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        An unexpected exception from strategy.on_candle() (anything other
        than NoSignalError) is re-raised as SignalGenerationError before
        it reaches the outer handler, so the logged/observable failure
        type is a domain exception rather than whatever internal error
        (IndexError, KeyError, etc.) the strategy happened to raise.
        """
        strategy = make_strategy(strategy_id="flaky")
        strategy.on_candle.side_effect = ValueError("bad candle data")
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        import logging

        with caplog.at_level(logging.ERROR):
            await orchestrator._on_candle_closed(make_candle_event())  # must not raise

        assert "SignalGenerationError" in caplog.text
        assert "bad candle data" in caplog.text


class TestSpreadFiltering:
    """
    Regression coverage for filter_by_spread wiring: filters.py implemented
    the check and MarketDataCache exposes the live quote, but nothing in
    the orchestrator ever called it — every signal passed regardless of
    spread. Disabled by default (max_spread_pct=0.0), same opt-in pattern
    as min_atr_pct/rsi_exhaustion_filter/macd_trend_filter.
    """

    async def test_disabled_by_default_even_with_wide_spread(
        self, candle_service: AsyncMock
    ) -> None:
        cache = MarketDataCache()
        cache.put_best_bid_ask("BTCUSDT", Decimal("100"), Decimal("110"))
        orchestrator = StrategyOrchestrator(candle_service, market_data_cache=cache)

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_spread"] == 0

    async def test_rejects_signal_when_spread_too_wide(
        self, candle_service: AsyncMock
    ) -> None:
        cache = MarketDataCache()
        # (110 - 100) / 100 * 100 = 10% spread
        cache.put_best_bid_ask("BTCUSDT", Decimal("100"), Decimal("110"))
        orchestrator = StrategyOrchestrator(
            candle_service, market_data_cache=cache, max_spread_pct=0.05,
        )

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 0
        assert orchestrator.stats["signals_filtered_spread"] == 1

    async def test_allows_signal_when_spread_within_tolerance(
        self, candle_service: AsyncMock
    ) -> None:
        cache = MarketDataCache()
        cache.put_best_bid_ask("BTCUSDT", Decimal("100"), Decimal("100.02"))
        orchestrator = StrategyOrchestrator(
            candle_service, market_data_cache=cache, max_spread_pct=0.05,
        )

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_spread"] == 0

    async def test_no_op_when_no_cached_quote_for_symbol(
        self, candle_service: AsyncMock
    ) -> None:
        cache = MarketDataCache()  # nothing cached for BTCUSDT
        orchestrator = StrategyOrchestrator(
            candle_service, market_data_cache=cache, max_spread_pct=0.05,
        )

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_spread"] == 0


class TestKillZoneFiltering:
    """
    Regression coverage for filter_by_kill_zone wiring: killzones.py's
    should_trade_now() was fully implemented and unit-tested but nothing
    in the orchestrator ever called it — every signal passed regardless
    of time of day. Disabled by default (kill_zone_filter=False), same
    opt-in pattern as min_atr_pct/rsi_exhaustion_filter/macd_trend_filter/
    max_spread_pct.
    """

    def _freeze(self, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int = 0) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 2, hour, minute, tzinfo=UTC)

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)

    async def test_disabled_by_default_outside_any_kill_zone(
        self, candle_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch, 5, 30)  # off-hours
        orchestrator = StrategyOrchestrator(candle_service)

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_kill_zone"] == 0

    async def test_rejects_signal_outside_kill_zone_when_enabled(
        self, candle_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch, 5, 30)  # off-hours
        orchestrator = StrategyOrchestrator(candle_service, kill_zone_filter=True)

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 0
        assert orchestrator.stats["signals_filtered_kill_zone"] == 1

    async def test_allows_signal_during_active_kill_zone_when_enabled(
        self, candle_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch, 14, 0)  # NY Open KZ
        orchestrator = StrategyOrchestrator(candle_service, kill_zone_filter=True)

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_kill_zone"] == 0

    async def test_require_kill_zone_false_relaxes_to_session_score(
        self, candle_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._freeze(monkeypatch, 18, 0)  # NY Lunch KZ (session_score 0.4)
        orchestrator = StrategyOrchestrator(
            candle_service, kill_zone_filter=True, require_kill_zone=False,
        )

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert orchestrator.stats["signals_generated"] == 1
        assert orchestrator.stats["signals_filtered_kill_zone"] == 0


class TestSignalExpiry:
    """
    Regression coverage for SignalExpiredEvent wiring: the event type
    existed but nothing published it — a strategy returning an
    already-expired signal was silently dropped with only a log line.
    """

    async def test_expired_signal_publishes_signal_expired_event(
        self, orchestrator: StrategyOrchestrator, isolated_event_bus: EventBus
    ) -> None:
        expired_received: list[SignalExpiredEvent] = []
        generated_received: list[SignalGeneratedEvent] = []
        isolated_event_bus.subscribe(SignalExpiredEvent, expired_received.append)
        isolated_event_bus.subscribe(SignalGeneratedEvent, generated_received.append)

        stale_signal = make_signal(
            generated_at=datetime(2020, 1, 1, tzinfo=UTC), max_age_seconds=60.0,
        )
        strategy = make_strategy(signal_to_return=stale_signal)
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())

        assert len(expired_received) == 1
        assert expired_received[0].symbol == "BTCUSDT"
        assert expired_received[0].signal_id == stale_signal.signal_id
        assert expired_received[0].reason == "expired_at_generation"
        # An expired signal must never also be published as generated —
        # the two are mutually exclusive outcomes for a given signal.
        assert generated_received == []
        # Nor should it count toward signals_generated.
        assert orchestrator.stats["signals_generated"] == 0

    async def test_fresh_signal_does_not_publish_signal_expired_event(
        self, orchestrator: StrategyOrchestrator, isolated_event_bus: EventBus
    ) -> None:
        expired_received: list[SignalExpiredEvent] = []
        isolated_event_bus.subscribe(SignalExpiredEvent, expired_received.append)

        strategy = make_strategy(signal_to_return=make_signal())
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())
        assert expired_received == []


class TestPerStrategyErrorIsolation:
    async def test_one_strategy_raising_does_not_prevent_dispatch_to_others(
        self, orchestrator: StrategyOrchestrator
    ) -> None:
        failing_strategy = make_strategy(strategy_id="failing")
        failing_strategy.on_candle.side_effect = RuntimeError("boom")
        healthy_strategy = make_strategy(strategy_id="healthy")

        orchestrator.register_strategy(failing_strategy)
        orchestrator.register_strategy(healthy_strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())  # must not raise

        healthy_strategy.on_candle.assert_awaited_once()

    async def test_candle_service_error_is_isolated_per_strategy(
        self, orchestrator: StrategyOrchestrator, candle_service: AsyncMock
    ) -> None:
        candle_service.get_candles.side_effect = RuntimeError("db down")
        strategy = make_strategy()
        orchestrator.register_strategy(strategy)
        await orchestrator.start()

        await orchestrator._on_candle_closed(make_candle_event())  # must not raise
        strategy.on_candle.assert_not_awaited()


class TestStats:
    def test_stats_reflects_initial_state(self, orchestrator: StrategyOrchestrator) -> None:
        stats = orchestrator.stats
        assert stats["strategies"] == 0
        assert stats["signals_generated"] == 0
        assert stats["running"] is False
