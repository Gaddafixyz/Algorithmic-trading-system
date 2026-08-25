"""
Tests for src.monitoring.alerts.

This module previously had zero test coverage (see CHANGELOG's recurring
"known gap" note). Focus here is build_standard_alerts(), including the
daily_loss_warning_pct / drawdown_warning_pct / cooldown_minutes params
added when configs/monitoring.yaml was wired into Settings -- those were
hardcoded (3.0 / 10.0 / 5-60-120) before that change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.core.enums import AlertSeverity
from src.monitoring.alerts import AlertRule, build_standard_alerts


@dataclass
class _FakeCircuitBreaker:
    is_open: bool = False
    open_reason: str = ""


@dataclass
class _FakeDrawdownTracker:
    current_drawdown_pct: float = 0.0


class _FakeRiskManager:
    def __init__(self, equity: float, daily_pnl: float, drawdown_pct: float = 0.0) -> None:
        self.equity = equity
        self.daily_pnl = daily_pnl
        self._drawdown = _FakeDrawdownTracker(current_drawdown_pct=drawdown_pct)


class TestAlertRuleCooldown:
    def test_never_fired_is_cooled_down(self) -> None:
        rule = AlertRule(
            name="x", severity=AlertSeverity.WARNING,
            check_fn=lambda: True, message_fn=lambda: "x",
        )
        assert rule.is_cooled_down() is True

    def test_recently_fired_is_not_cooled_down(self) -> None:
        rule = AlertRule(
            name="x", severity=AlertSeverity.WARNING,
            check_fn=lambda: True, message_fn=lambda: "x",
            cooldown_minutes=30,
        )
        rule.last_fired = datetime.now(tz=UTC)
        assert rule.is_cooled_down() is False

    def test_cooldown_elapsed_is_cooled_down_again(self) -> None:
        rule = AlertRule(
            name="x", severity=AlertSeverity.WARNING,
            check_fn=lambda: True, message_fn=lambda: "x",
            cooldown_minutes=30,
        )
        rule.last_fired = datetime.now(tz=UTC) - timedelta(minutes=31)
        assert rule.is_cooled_down() is True


class TestBuildStandardAlertsDefaults:
    """Behavior with no explicit params -- must match pre-yaml-wiring defaults."""

    def test_default_thresholds_and_cooldowns(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0, drawdown_pct=0)
        cb = _FakeCircuitBreaker()
        rules = {r.name: r for r in build_standard_alerts(rm, cb)}

        assert rules["circuit_breaker_open"].cooldown_minutes == 5
        assert rules["daily_loss_high"].cooldown_minutes == 60
        assert rules["drawdown_warning"].cooldown_minutes == 120

    def test_daily_loss_high_fires_above_default_3pct(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=-31)  # -3.1%
        rules = {r.name: r for r in build_standard_alerts(rm, _FakeCircuitBreaker())}
        assert rules["daily_loss_high"].check_fn() is True

    def test_daily_loss_high_does_not_fire_below_default_3pct(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=-29)  # -2.9%
        rules = {r.name: r for r in build_standard_alerts(rm, _FakeCircuitBreaker())}
        assert rules["daily_loss_high"].check_fn() is False

    def test_daily_loss_high_ignores_positive_pnl(self) -> None:
        # abs() in the check means a large gain must not be treated as a loss.
        rm = _FakeRiskManager(equity=1000, daily_pnl=+50)
        rules = {r.name: r for r in build_standard_alerts(rm, _FakeCircuitBreaker())}
        assert rules["daily_loss_high"].check_fn() is False

    def test_drawdown_warning_fires_above_default_10pct(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0, drawdown_pct=10.5)
        rules = {r.name: r for r in build_standard_alerts(rm, _FakeCircuitBreaker())}
        assert rules["drawdown_warning"].check_fn() is True

    def test_circuit_breaker_rule_reflects_breaker_state(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0)
        cb = _FakeCircuitBreaker(is_open=True, open_reason="daily loss limit")
        rules = {r.name: r for r in build_standard_alerts(rm, cb)}
        assert rules["circuit_breaker_open"].check_fn() is True
        assert "daily loss limit" in rules["circuit_breaker_open"].message_fn()


class TestBuildStandardAlertsConfigurable:
    """
    Params threaded through from Settings.monitoring (configs/monitoring.yaml)
    in bootstrap/startup.py.
    """

    def test_custom_daily_loss_threshold_applied(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=-15)  # -1.5%
        rules = {
            r.name: r
            for r in build_standard_alerts(rm, _FakeCircuitBreaker(), daily_loss_warning_pct=1.0)
        }
        assert rules["daily_loss_high"].check_fn() is True

    def test_custom_drawdown_threshold_applied(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0, drawdown_pct=6.0)
        rules = {
            r.name: r
            for r in build_standard_alerts(rm, _FakeCircuitBreaker(), drawdown_warning_pct=5.0)
        }
        assert rules["drawdown_warning"].check_fn() is True

    def test_explicit_cooldown_overrides_all_three_rules_uniformly(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0)
        rules = {
            r.name: r
            for r in build_standard_alerts(rm, _FakeCircuitBreaker(), cooldown_minutes=15)
        }
        assert rules["circuit_breaker_open"].cooldown_minutes == 15
        assert rules["daily_loss_high"].cooldown_minutes == 15
        assert rules["drawdown_warning"].cooldown_minutes == 15

    def test_cooldown_none_preserves_per_rule_defaults(self) -> None:
        rm = _FakeRiskManager(equity=1000, daily_pnl=0)
        rules = {
            r.name: r
            for r in build_standard_alerts(rm, _FakeCircuitBreaker(), cooldown_minutes=None)
        }
        assert rules["circuit_breaker_open"].cooldown_minutes == 5
        assert rules["daily_loss_high"].cooldown_minutes == 60
        assert rules["drawdown_warning"].cooldown_minutes == 120
