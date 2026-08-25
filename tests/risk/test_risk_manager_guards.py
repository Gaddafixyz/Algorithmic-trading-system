"""Supplementary unit tests for src.risk.risk_manager.RiskManager.

Covers branches not exercised by tests/risk/test_risk_manager.py:
drawdown-limit circuit trip, position-sizing invalid-stop-distance and
zero-size-after-cap errors, and update_equity's day-rollover reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import src.risk.risk_manager as risk_manager_module
from src.core.config import RiskConfig
from src.core.enums import SignalDirection, Timeframe
from src.core.exceptions import RiskLimitExceededError
from src.domain.portfolio import Portfolio
from src.domain.signal import Signal, SignalStrength
from src.risk.risk_manager import RiskManager


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    entry: float = 50000.0,
    sl: float = 49000.0,
    tp: float = 52000.0,
    symbol: str = "BTCUSDT",
    score: float = 70.0,
) -> Signal:
    return Signal(
        strategy_id="test",
        symbol=symbol,
        timeframe=Timeframe.H1,
        direction=direction,
        score=score,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal(str(entry)),
        stop_loss=Decimal(str(sl)),
        take_profit=Decimal(str(tp)),
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_portfolio_risk_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal("5.0"),
        max_drawdown_pct=Decimal("15.0"),
        max_open_positions=3,
        max_leverage=Decimal("10.0"),
        risk_reward_minimum=Decimal("1.5"),
    )


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio()


@pytest.fixture
def risk_manager(risk_config: RiskConfig, portfolio: Portfolio) -> RiskManager:
    rm = RiskManager(config=risk_config, portfolio=portfolio)
    rm.update_equity(Decimal("10000"))
    return rm


class TestDrawdownLimitTrip:
    async def test_drawdown_at_or_above_the_limit_trips_the_breaker_and_rejects(
        self, risk_manager: RiskManager
    ) -> None:
        # Establish a peak, then drop equity by exactly 15% (the
        # configured max_drawdown_pct), which should trip the breaker
        # on the next validate_signal() call.
        risk_manager.update_equity(Decimal("10000"))  # peak
        risk_manager.update_equity(Decimal("8500"))  # -15% drawdown

        with pytest.raises(RiskLimitExceededError, match="max_drawdown"):
            await risk_manager.validate_signal(make_signal())

        assert risk_manager.circuit_breaker_state.value == "open"

    async def test_drawdown_below_the_limit_does_not_trip(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.update_equity(Decimal("10000"))
        risk_manager.update_equity(Decimal("9500"))  # -5% drawdown, under 15% limit

        result = await risk_manager.validate_signal(make_signal())
        assert result is True


class TestPositionSizingErrors:
    def test_signal_model_itself_forbids_stop_loss_equal_to_entry_price(self) -> None:
        # RiskManager.calculate_position_size() has an explicit
        # `if stop_distance <= Decimal("0"): raise PositionSizingError(...)`
        # guard for entry_price == stop_loss. However, tracing this
        # shows the guard is unreachable via the public API: Signal's
        # own Pydantic validator already rejects a long signal whose
        # stop_loss is not strictly below its entry_price, so no such
        # Signal can ever be constructed to pass into
        # calculate_position_size in the first place. This test
        # documents that finding directly (verifying the Signal-level
        # rejection) rather than asserting on the RiskManager guard,
        # which was found to be currently unreachable defensive code
        # given upstream validation -- not something worth faking a
        # signal via internal state manipulation just to hit a coverage
        # number.
        from src.domain.signal import Signal, SignalStrength

        with pytest.raises(ValueError, match="must be strictly below entry"):
            Signal(
                strategy_id="test",
                symbol="BTCUSDT",
                timeframe=Timeframe.H1,
                direction=SignalDirection.LONG,
                score=70.0,
                strength=SignalStrength.STRONG,
                confidence=0.7,
                entry_price=Decimal("50000"),
                stop_loss=Decimal("50000"),  # equal to entry -> rejected here
                take_profit=Decimal("52000"),
            )

    async def test_max_position_pct_of_equity_caps_the_resulting_size(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ) -> None:
        # A very small max_position_pct_of_equity caps the resulting
        # quantity to a correspondingly small (but, per Decimal's
        # arbitrary-precision arithmetic, not exactly zero) value. This
        # verifies the cap itself takes effect; see the note above the
        # PositionSizingError("Calculated zero position size", ...)
        # guard immediately following it in the source -- tracing the
        # arithmetic shows that guard is not reachable through this
        # path, since Decimal division doesn't underflow to exactly 0
        # the way float division can, and upstream config constraints
        # (gt=0 on both max_portfolio_risk_pct and
        # max_position_pct_of_equity) prevent driving the pre-cap size
        # to exactly 0 either.
        risk_config.max_position_pct_of_equity = Decimal("0.0000001")
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))

        signal = make_signal()
        size = await rm.calculate_position_size(signal)
        # Capped far below the uncapped fixed-fractional size.
        assert size > Decimal("0")
        assert size < Decimal("0.0001")


class TestLowConfidenceSignalSoftWarning:
    async def test_low_score_signal_still_passes_but_logs_a_warning(
        self, risk_manager: RiskManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        # score < 55.0 is a soft check: it logs a warning but does not
        # reject the signal, unlike the hard risk-reward/drawdown/loss
        # limit checks above it.
        signal = make_signal(score=40.0)
        with caplog.at_level("WARNING", logger="src.risk.risk_manager"):
            result = await risk_manager.validate_signal(signal)
        assert result is True
        messages = [r.getMessage() for r in caplog.records]
        assert any("Low confidence signal" in m for m in messages)

    async def test_score_at_or_above_55_does_not_log_a_low_confidence_warning(
        self, risk_manager: RiskManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        signal = make_signal(score=55.0)
        with caplog.at_level("WARNING", logger="src.risk.risk_manager"):
            await risk_manager.validate_signal(signal)
        messages = [r.getMessage() for r in caplog.records]
        assert not any("Low confidence signal" in m for m in messages)


class TestTrivialPassthroughs:
    async def test_check_portfolio_risk_true_when_breaker_closed(
        self, risk_manager: RiskManager
    ) -> None:
        assert await risk_manager.check_portfolio_risk() is True

    async def test_check_portfolio_risk_false_when_breaker_open(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.update_equity(Decimal("10000"))
        risk_manager.update_equity(Decimal("8000"))  # -20% > 15% limit
        with pytest.raises(RiskLimitExceededError):
            await risk_manager.validate_signal(make_signal())
        assert await risk_manager.check_portfolio_risk() is False

    async def test_on_position_update_is_a_no_op(self, risk_manager: RiskManager) -> None:
        # Documented as "Handled by event bus" -- must not raise.
        result = await risk_manager.on_position_update(object())
        assert result is None
    def test_first_update_establishes_day_start_equity_and_date(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ) -> None:
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        assert rm._day_start_equity == Decimal("10000")
        assert rm._day_start_date is not None

    def test_same_day_update_does_not_reset_day_start_equity(
        self, monkeypatch: pytest.MonkeyPatch, risk_config: RiskConfig, portfolio: Portfolio
    ) -> None:
        fixed_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return fixed_time

        monkeypatch.setattr(risk_manager_module, "datetime", _FrozenDatetime)

        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        rm.record_pnl(Decimal("-100"))  # daily_pnl becomes -100

        rm.update_equity(Decimal("9900"))  # same simulated day
        assert rm._daily_pnl == Decimal("-100")  # not reset
        assert rm._day_start_equity == Decimal("10000")  # unchanged

    def test_next_day_update_resets_daily_pnl_and_day_start_equity(
        self, monkeypatch: pytest.MonkeyPatch, risk_config: RiskConfig, portfolio: Portfolio
    ) -> None:
        day_one = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
        clock = {"now": day_one}

        class _ControllableDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return clock["now"]

        monkeypatch.setattr(risk_manager_module, "datetime", _ControllableDatetime)

        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        rm.record_pnl(Decimal("-300"))
        assert rm._daily_pnl == Decimal("-300")

        clock["now"] = day_one + timedelta(days=1)
        rm.update_equity(Decimal("9700"))

        assert rm._daily_pnl == Decimal("0")  # reset on new day
        assert rm._day_start_equity == Decimal("9700")  # new day's baseline
