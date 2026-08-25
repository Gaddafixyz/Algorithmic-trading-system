"""
Trading Circuit Breaker.

Automatically halts trading when loss limits or anomaly conditions
are breached, preventing catastrophic drawdowns.

States:
  CLOSED  → Normal operation
  OPEN    → Trading halted; no orders accepted
  HALF_OPEN → Testing recovery after cooldown period
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.enums import CircuitBreakerState
from src.core.events import CircuitBreakerStateChangedEvent, get_event_bus

logger = logging.getLogger(__name__)

# Fraction of normal position size allowed for the single probation trade
# used to re-verify recovery while HALF_OPEN.
HALF_OPEN_SIZE_MULTIPLIER: float = 0.25

# How long a probation trade may sit "in-flight" (begin_probation_trade()
# called but record_probation_result() never arrives) before it's treated
# as abandoned and the slot is released. Guards against the case where
# order submission fails/raises after begin_probation_trade() but before a
# position is ever opened — no PositionClosedEvent will ever follow, so
# without this timeout _probation_trade_active would stay True forever,
# permanently wedging the breaker in HALF_OPEN with no recovery path short
# of a manual reset().
PROBATION_TIMEOUT_MINUTES: int = 10


class CircuitBreaker:
    """
    Automatic trading halt mechanism.

    Trips on:
    - Daily loss exceeding threshold
    - Drawdown exceeding threshold
    - Manual trip (administrator or kill switch)

    Resets:
    - After cooldown_minutes if in HALF_OPEN
    - Manually via reset()

    HALF_OPEN behaviour:
    - Only a single reduced-size "probation" trade is allowed through while
      HALF_OPEN (see ``allow_probation_trade`` / ``begin_probation_trade``).
    - Its size is scaled by ``size_multiplier`` (HALF_OPEN_SIZE_MULTIPLIER).
    - Its outcome is fed back via ``record_probation_result``: a profitable
      close fully resets the breaker to CLOSED; a loss trips it back OPEN.
    """

    def __init__(
        self,
        cooldown_minutes: int = 60,
        probation_timeout_minutes: int = PROBATION_TIMEOUT_MINUTES,
    ) -> None:
        # Note: daily-loss and drawdown *thresholds* deliberately don't live
        # here. RiskManager owns those (config.max_daily_loss_pct /
        # config.max_drawdown_pct) and is the sole place that compares
        # actual daily P&L / drawdown against them — this class previously
        # also accepted and stored its own copies of those limits, but
        # never read them for anything; they were purely decorative and
        # could drift from the values RiskManager actually enforces. This
        # class only reacts to trip()/reset() calls from whoever does own
        # the comparison.
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._probation_timeout = timedelta(minutes=probation_timeout_minutes)
        self._state = CircuitBreakerState.CLOSED
        self._tripped_at: datetime | None = None
        self._open_reason: str | None = None
        self._trip_count: int = 0
        # True once a single probation trade has been let through during
        # the current HALF_OPEN window; blocks further trades at full size
        # until that probation trade's outcome is known.
        self._probation_trade_active: bool = False
        # When the currently in-flight probation trade began; used to
        # detect and release an abandoned probation slot (see
        # PROBATION_TIMEOUT_MINUTES above). None whenever no probation
        # trade is in flight.
        self._probation_started_at: datetime | None = None
        # CircuitBreakerStateChangedEvent existed as schema only — no
        # producer anywhere published it, so nothing downstream (alerts,
        # notifications, dashboards) ever learned about a trip/reset/
        # half-open transition except by polling circuit_breaker_state
        # via AlertManager's periodic rule evaluation. publish_sync is
        # used because trip()/reset()/half_open() are synchronous.
        self._bus = get_event_bus()

    def _publish_state_change(
        self, previous: CircuitBreakerState, reason: str
    ) -> None:
        if previous == self._state:
            return
        self._bus.publish_sync(
            CircuitBreakerStateChangedEvent(
                previous_state=previous,
                new_state=self._state,
                reason=reason,
            )
        )

    def trip(self, reason: str) -> None:
        """Open the circuit breaker and halt trading."""
        if self._state == CircuitBreakerState.OPEN:
            return  # Already open
        prev = self._state
        self._state = CircuitBreakerState.OPEN
        self._tripped_at = datetime.now(tz=UTC)
        self._open_reason = reason
        self._trip_count += 1
        logger.critical(
            "CIRCUIT BREAKER TRIPPED: %s (trip #%d)", reason, self._trip_count
        )
        self._publish_state_change(prev, reason)

    def reset(self) -> None:
        """Close the circuit breaker and resume trading."""
        prev = self._state
        self._state = CircuitBreakerState.CLOSED
        self._open_reason = None
        self._probation_trade_active = False
        self._probation_started_at = None
        logger.warning(
            "Circuit breaker reset from %s (was open for %s)",
            prev,
            datetime.now(tz=UTC) - self._tripped_at if self._tripped_at else "unknown",
        )
        self._tripped_at = None
        self._publish_state_change(prev, "manual or probation-success reset")

    def half_open(self) -> None:
        """Move to half-open state to test if recovery is possible."""
        if self._state == CircuitBreakerState.OPEN:
            prev = self._state
            self._state = CircuitBreakerState.HALF_OPEN
            self._probation_trade_active = False
            self._probation_started_at = None
            logger.info("Circuit breaker in HALF_OPEN state")
            self._publish_state_change(prev, "cooldown elapsed — testing recovery")

    # ─── HALF_OPEN probation trade gating ─────────────────────────────────────

    def _release_stale_probation(self) -> None:
        """
        Release an abandoned probation slot.

        If begin_probation_trade() was called but the trade never resulted
        in a position (submission failed/raised, or was rejected upstream
        before ever reaching the exchange), record_probation_result() is
        never invoked and the slot would otherwise stay locked forever.
        """
        if not self._probation_trade_active or self._probation_started_at is None:
            return
        elapsed = datetime.now(tz=UTC) - self._probation_started_at
        if elapsed >= self._probation_timeout:
            logger.warning(
                "HALF_OPEN probation trade timed out after %s with no result "
                "(likely failed before a position was opened) — releasing slot",
                elapsed,
            )
            self._probation_trade_active = False
            self._probation_started_at = None

    @property
    def allow_probation_trade(self) -> bool:
        """True if a new reduced-size probation trade may be let through."""
        if self._state != CircuitBreakerState.HALF_OPEN:
            return False
        self._release_stale_probation()
        return not self._probation_trade_active

    def begin_probation_trade(self) -> None:
        """Mark the single HALF_OPEN probation trade as in-flight."""
        self._probation_trade_active = True
        self._probation_started_at = datetime.now(tz=UTC)

    def record_probation_result(self, pnl: float) -> None:
        """
        Feed back the outcome of the HALF_OPEN probation trade.

        A profitable close proves the strategy has recovered: fully reset
        to CLOSED. A loss means it hasn't: trip back to OPEN for another
        full cooldown rather than letting further trades through at size.
        """
        if self._state != CircuitBreakerState.HALF_OPEN:
            return
        self._probation_trade_active = False
        self._probation_started_at = None
        if pnl >= 0:
            logger.info("HALF_OPEN probation trade profitable (pnl=%.4f) — resetting to CLOSED", pnl)
            self.reset()
        else:
            logger.warning(
                "HALF_OPEN probation trade lost (pnl=%.4f) — re-tripping to OPEN", pnl
            )
            self.trip(f"Probation trade loss during HALF_OPEN recovery test ({pnl:.4f})")

    @property
    def size_multiplier(self) -> float:
        """Position-size scaling factor for the current state."""
        if self._state == CircuitBreakerState.OPEN:
            return 0.0
        if self._state == CircuitBreakerState.HALF_OPEN:
            return HALF_OPEN_SIZE_MULTIPLIER
        return 1.0

    def check_cooldown(self) -> None:
        """Auto-transition from OPEN to HALF_OPEN after cooldown period."""
        if self._state == CircuitBreakerState.OPEN and self._tripped_at:
            elapsed = datetime.now(tz=UTC) - self._tripped_at
            if elapsed >= self._cooldown:
                self.half_open()

    @property
    def is_open(self) -> bool:
        return self._state == CircuitBreakerState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitBreakerState.CLOSED

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def open_reason(self) -> str | None:
        return self._open_reason

    @property
    def trip_count(self) -> int:
        return self._trip_count

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "trip_count": self._trip_count,
            "reason": self._open_reason,
            "tripped_at": self._tripped_at.isoformat() if self._tripped_at else None,
        }
