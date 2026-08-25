"""Supplementary unit tests for src.risk.circuit_breaker.CircuitBreaker.

Covers branches not exercised by tests/unit/test_risk.py's
TestCircuitBreaker class: check_cooldown's both outcomes,
half_open()'s no-op guard, reset() without a prior trip, and the
stats property.

Note: CircuitBreaker previously had its own update_equity()/_peak_equity
tracking peak equity independently of src.risk.drawdown.DrawdownTracker,
which already owns this responsibility for RiskManager (fed from the
same update_equity() call in RiskManager.update_equity). That duplicate,
write-only copy was removed as dead state; peak-equity tracking now has
a single owner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import src.risk.circuit_breaker as circuit_breaker_module
from src.core.enums import CircuitBreakerState
from src.core.events import CircuitBreakerStateChangedEvent, EventBus, reset_event_bus, set_event_bus
from src.risk.circuit_breaker import CircuitBreaker


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


class TestHalfOpenGuard:
    def test_is_a_no_op_when_the_breaker_is_closed(self) -> None:
        cb = CircuitBreaker()
        cb.half_open()
        assert cb.state == CircuitBreakerState.CLOSED

    def test_is_a_no_op_when_already_half_open(self) -> None:
        cb = CircuitBreaker()
        cb.trip("test")
        cb.half_open()
        assert cb.state == CircuitBreakerState.HALF_OPEN
        cb.half_open()  # calling again while already HALF_OPEN
        assert cb.state == CircuitBreakerState.HALF_OPEN


class TestResetWithoutPriorTrip:
    def test_reset_on_a_never_tripped_breaker_does_not_raise(self) -> None:
        # Exercises the `if self._tripped_at else "unknown"` branch in
        # reset()'s log message when the breaker was never tripped.
        cb = CircuitBreaker()
        cb.reset()  # must not raise despite _tripped_at being None
        assert cb.is_closed


class TestCheckCooldown:
    def test_does_not_transition_before_the_cooldown_period_elapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixed_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return fixed_time

        monkeypatch.setattr(circuit_breaker_module, "datetime", _FrozenDatetime)

        cb = CircuitBreaker(cooldown_minutes=60)
        cb.trip("test")  # tripped_at = fixed_time
        cb.check_cooldown()
        assert cb.state == CircuitBreakerState.OPEN  # unchanged, no time has passed

    def test_transitions_to_half_open_once_cooldown_elapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trip_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        clock = {"now": trip_time}

        class _ControllableDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return clock["now"]

        monkeypatch.setattr(circuit_breaker_module, "datetime", _ControllableDatetime)

        cb = CircuitBreaker(cooldown_minutes=60)
        cb.trip("test")  # tripped_at = trip_time (via the patched now())

        clock["now"] = trip_time + timedelta(minutes=61)
        cb.check_cooldown()
        assert cb.state == CircuitBreakerState.HALF_OPEN

    def test_is_a_no_op_when_the_breaker_is_not_open(self) -> None:
        cb = CircuitBreaker()
        cb.check_cooldown()  # CLOSED state: must not raise or change state
        assert cb.state == CircuitBreakerState.CLOSED


class TestProbationTimeout:
    """
    Covers the case where begin_probation_trade() is called but the trade
    never results in a position (submission fails/raises before the
    exchange ever accepts it), so record_probation_result() is never
    invoked. Without a timeout, allow_probation_trade would stay False
    forever, permanently wedging the breaker in HALF_OPEN.
    """

    def test_abandoned_probation_trade_is_released_after_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = {"now": start}

        class _ControllableDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return clock["now"]

        monkeypatch.setattr(circuit_breaker_module, "datetime", _ControllableDatetime)

        cb = CircuitBreaker(probation_timeout_minutes=10)
        cb.trip("test")
        cb.half_open()
        assert cb.allow_probation_trade is True

        cb.begin_probation_trade()
        assert cb.allow_probation_trade is False  # slot in use, no result yet

        clock["now"] = start + timedelta(minutes=5)
        assert cb.allow_probation_trade is False  # still within timeout

        clock["now"] = start + timedelta(minutes=11)
        assert cb.allow_probation_trade is True  # abandoned slot released

    def test_probation_result_arriving_before_timeout_clears_state_normally(
        self,
    ) -> None:
        cb = CircuitBreaker()
        cb.trip("test")
        cb.half_open()
        cb.begin_probation_trade()
        cb.record_probation_result(pnl=1.0)
        assert cb.state == CircuitBreakerState.CLOSED


class TestStats:
    def test_stats_reflects_a_closed_breaker(self) -> None:
        # daily-loss/drawdown thresholds are owned and enforced by
        # RiskManager (config.max_daily_loss_pct / max_drawdown_pct) and
        # surfaced in RiskManager.stats — CircuitBreaker itself no longer
        # stores redundant copies of those limits, since it never enforced
        # them and they could drift from the values actually used.
        cb = CircuitBreaker()
        stats = cb.stats
        assert stats["state"] == CircuitBreakerState.CLOSED.value
        assert stats["trip_count"] == 0
        assert stats["reason"] is None
        assert stats["tripped_at"] is None
        assert "daily_loss_limit_pct" not in stats
        assert "drawdown_limit_pct" not in stats

    def test_stats_reflects_a_tripped_breaker(self) -> None:
        cb = CircuitBreaker()
        cb.trip("daily loss limit breached")
        stats = cb.stats
        assert stats["state"] == CircuitBreakerState.OPEN.value
        assert stats["trip_count"] == 1
        assert stats["reason"] == "daily loss limit breached"
        assert stats["tripped_at"] is not None


def _recorder(bus: EventBus) -> list:
    received: list = []

    async def _handler(event: object) -> None:
        received.append(event)

    bus.subscribe(CircuitBreakerStateChangedEvent, _handler)
    return received


class TestCircuitBreakerStateChangedEventIsPublished:
    """CircuitBreakerStateChangedEvent previously existed as schema only
    with no producer — trip()/reset()/half_open() now publish it via
    the synchronous publish_sync path."""

    def test_trip_publishes_closed_to_open_transition(
        self, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus)
        cb = CircuitBreaker()

        cb.trip("daily loss limit breached")

        assert len(received) == 1
        assert received[0].previous_state == CircuitBreakerState.CLOSED
        assert received[0].new_state == CircuitBreakerState.OPEN
        assert received[0].reason == "daily loss limit breached"

    def test_trip_when_already_open_publishes_nothing(
        self, isolated_event_bus: EventBus
    ) -> None:
        cb = CircuitBreaker()
        cb.trip("first trip")
        received = _recorder(isolated_event_bus)

        cb.trip("second trip attempt")  # no-op: already OPEN

        assert received == []

    def test_reset_publishes_open_to_closed_transition(
        self, isolated_event_bus: EventBus
    ) -> None:
        cb = CircuitBreaker()
        cb.trip("test")
        received = _recorder(isolated_event_bus)

        cb.reset()

        assert len(received) == 1
        assert received[0].previous_state == CircuitBreakerState.OPEN
        assert received[0].new_state == CircuitBreakerState.CLOSED

    def test_reset_on_never_tripped_breaker_publishes_nothing(
        self, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus)
        cb = CircuitBreaker()

        cb.reset()  # already CLOSED — no real transition

        assert received == []

    def test_half_open_publishes_open_to_half_open_transition(
        self, isolated_event_bus: EventBus
    ) -> None:
        cb = CircuitBreaker()
        cb.trip("test")
        received = _recorder(isolated_event_bus)

        cb.half_open()

        assert len(received) == 1
        assert received[0].previous_state == CircuitBreakerState.OPEN
        assert received[0].new_state == CircuitBreakerState.HALF_OPEN

    def test_half_open_when_closed_publishes_nothing(
        self, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus)
        cb = CircuitBreaker()

        cb.half_open()  # no-op: still CLOSED

        assert received == []
