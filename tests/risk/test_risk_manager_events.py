"""Tests for RiskManager's event-bus publishing.

DailyLossLimitReachedEvent and RiskLimitBreachedEvent (drawdown +
gross-exposure variants) previously existed in src.core.events as
schema only — the corresponding limits were enforced via exceptions
and CircuitBreaker.trip(), but nothing ever told subscribers (alerts,
notifications, dashboards) *why* trading halted through the event
bus. These tests confirm RiskManager now publishes them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import RiskConfig
from src.core.enums import SignalDirection, Timeframe
from src.core.events import (
    DailyLossLimitReachedEvent,
    EventBus,
    RiskLimitBreachedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.core.exceptions import DailyLossLimitError, PositionSizingError, RiskLimitExceededError
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.domain.signal import Signal, SignalStrength
from src.core.enums import PositionSide
from src.risk.risk_manager import RiskManager


@pytest.fixture(autouse=True)
def isolated_event_bus():
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


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


def _recorder(bus: EventBus, event_type: type) -> list:
    received: list = []

    async def _handler(event: object) -> None:
        received.append(event)

    bus.subscribe(event_type, _handler)
    return received


class TestDailyLossLimitReachedEventIsPublished:
    async def test_daily_loss_breach_publishes_event(
        self, risk_manager: RiskManager, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, DailyLossLimitReachedEvent)
        risk_manager.record_pnl(Decimal("-600"))  # 6% loss > 5% limit

        with pytest.raises(DailyLossLimitError):
            await risk_manager.validate_signal(make_signal())

        assert len(received) == 1
        assert received[0].daily_pnl == Decimal("-600")
        assert received[0].limit_pct == Decimal("5.0")

    async def test_within_limit_does_not_publish_event(
        self, risk_manager: RiskManager, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, DailyLossLimitReachedEvent)
        risk_manager.record_pnl(Decimal("-100"))  # 1% loss, well under 5% limit

        result = await risk_manager.validate_signal(make_signal())

        assert result is True
        assert received == []


class TestDrawdownRiskLimitBreachedEventIsPublished:
    async def test_drawdown_breach_publishes_event(
        self, risk_manager: RiskManager, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, RiskLimitBreachedEvent)
        # Push equity up then down to build a >15% drawdown.
        risk_manager.update_equity(Decimal("10000"))
        risk_manager.update_equity(Decimal("8000"))  # 20% drawdown from peak

        with pytest.raises(RiskLimitExceededError):
            await risk_manager.validate_signal(make_signal())

        assert len(received) == 1
        assert received[0].limit_name == "max_drawdown"
        assert received[0].limit_value == pytest.approx(15.0)


class TestGrossExposureRiskLimitBreachedEventIsPublished:
    def _exposed_config(self, **overrides: object) -> RiskConfig:
        base = dict(
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=5,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.0"),
            max_correlated_exposure_pct=Decimal("100"),
            max_gross_exposure_pct=Decimal("100"),
            max_net_exposure_pct=Decimal("500"),
        )
        base.update(overrides)
        return RiskConfig(**base)

    async def test_check_portfolio_risk_breach_publishes_event(
        self, portfolio: Portfolio, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, RiskLimitBreachedEvent)
        portfolio.add_position(
            Position(
                symbol="BTCUSDT", side=PositionSide.LONG,
                quantity=Decimal("0.5"), entry_quantity=Decimal("0.5"),
                entry_price=Decimal("50000"), mark_price=Decimal("50000"),
            )
        )
        config = self._exposed_config(max_gross_exposure_pct=Decimal("100"))
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))  # position notional 25000 = 250% of equity

        result = await rm.check_portfolio_risk()

        assert result is False
        assert len(received) == 1
        assert received[0].limit_name == "max_gross_exposure"

    async def test_within_limits_does_not_publish_event(
        self, portfolio: Portfolio, isolated_event_bus: EventBus
    ) -> None:
        received = _recorder(isolated_event_bus, RiskLimitBreachedEvent)
        config = self._exposed_config()
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))

        result = await rm.check_portfolio_risk()

        assert result is True
        assert received == []
