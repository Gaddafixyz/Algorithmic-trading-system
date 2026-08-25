"""Tests confirming RiskManager.validate_signal() records risk_validations.

risk_validations (trading_risk_validations_total) was declared in
metrics.py but never incremented anywhere — RiskManager already tracked
pass/fail counts in-memory (self._validations_passed/_failed), but that
state was invisible to Prometheus/Grafana. These tests confirm the metric
now moves in lockstep with a pass and with a rejection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import RiskConfig
from src.core.enums import SignalDirection, Timeframe
from src.core.exceptions import RiskLimitExceededError
from src.domain.portfolio import Portfolio
from src.domain.signal import Signal, SignalStrength
from src.monitoring.metrics import risk_validations
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


def _counter_value(result: str) -> float:
    return risk_validations.labels(result=result)._value.get()


class TestRiskValidationsMetric:
    async def test_passing_signal_increments_passed_label(
        self, risk_manager: RiskManager
    ) -> None:
        before = _counter_value("passed")

        result = await risk_manager.validate_signal(make_signal())

        assert result is True
        assert _counter_value("passed") == before + 1

    async def test_rejected_signal_increments_failed_label(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.update_equity(Decimal("10000"))  # peak
        risk_manager.update_equity(Decimal("8500"))  # -15% drawdown trips the breaker

        before = _counter_value("failed")

        with pytest.raises(RiskLimitExceededError):
            await risk_manager.validate_signal(make_signal())

        assert _counter_value("failed") == before + 1

    async def test_metric_deltas_track_in_memory_counters(
        self, risk_manager: RiskManager
    ) -> None:
        # Cross-check against RiskManager's own pre-existing in-memory
        # stats (self._validations_passed/_failed) rather than just the
        # Prometheus side, to catch the case where the metric call was
        # added at the wrong line (e.g. inside a branch that doesn't
        # always execute).
        passed_before = _counter_value("passed")
        stats_passed_before = risk_manager.stats["validations_passed"]

        await risk_manager.validate_signal(make_signal())

        assert _counter_value("passed") - passed_before == (
            risk_manager.stats["validations_passed"] - stats_passed_before
        )
