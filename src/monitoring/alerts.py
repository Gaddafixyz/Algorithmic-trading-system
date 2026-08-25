"""
Alert management system.

Defines alert rules and dispatches alerts to notification channels
when thresholds are breached. Supports cooldown periods to avoid
alert spam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.enums import AlertSeverity
from src.core.events import AlertFiredEvent, get_event_bus

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, AlertSeverity, str], Coroutine[Any, Any, None]]


@dataclass
class AlertRule:
    """A single alert rule with threshold and cooldown."""

    name: str
    severity: AlertSeverity
    check_fn: Callable[[], bool]  # Returns True when alert should fire
    message_fn: Callable[[], str]  # Generates alert message
    cooldown_minutes: int = 30  # Min minutes between repeated alerts
    enabled: bool = True

    # State
    last_fired: datetime | None = field(default=None, repr=False)
    fire_count: int = field(default=0, repr=False)

    def is_cooled_down(self) -> bool:
        """True if enough time has passed since the last alert."""
        if self.last_fired is None:
            return True
        elapsed = datetime.now(tz=UTC) - self.last_fired
        return elapsed >= timedelta(minutes=self.cooldown_minutes)

    def should_fire(self) -> bool:
        return self.enabled and self.check_fn() and self.is_cooled_down()

    def mark_fired(self) -> None:
        self.last_fired = datetime.now(tz=UTC)
        self.fire_count += 1


class AlertManager:
    """
    Evaluates alert rules and publishes AlertFiredEvents.

    Call evaluate() periodically (e.g. every 60 seconds).
    """

    def __init__(self) -> None:
        self._rules: list[AlertRule] = []
        self._bus = get_event_bus()
        self._callbacks: list[AlertCallback] = []
        self._fired_total: int = 0

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)
        logger.debug("Alert rule registered: %s", rule.name)

    def add_callback(self, callback: AlertCallback) -> None:
        """Register a callback to receive alert notifications."""
        self._callbacks.append(callback)

    async def evaluate(self) -> list[AlertRule]:
        """
        Evaluate all alert rules and fire any that are triggered.

        Returns list of rules that fired in this evaluation.
        """
        fired: list[AlertRule] = []

        for rule in self._rules:
            try:
                if rule.should_fire():
                    message = rule.message_fn()
                    rule.mark_fired()
                    fired.append(rule)
                    self._fired_total += 1

                    logger.warning(
                        "ALERT [%s] %s: %s",
                        rule.severity.value.upper(),
                        rule.name,
                        message,
                    )

                    # Publish to event bus
                    await self._bus.publish(
                        AlertFiredEvent(
                            source="alert_manager",
                            alert_name=rule.name,
                            severity=rule.severity,
                            message=message,
                        )
                    )

                    # Dispatch to direct callbacks
                    for cb in self._callbacks:
                        try:
                            await cb(rule.name, rule.severity, message)
                        except Exception as exc:
                            logger.error("Alert callback failed: %s", exc)

            except Exception as exc:
                logger.error("Error evaluating alert rule '%s': %s", rule.name, exc)

        return fired

    def disable_rule(self, name: str) -> bool:
        for rule in self._rules:
            if rule.name == name:
                rule.enabled = False
                return True
        return False

    def enable_rule(self, name: str) -> bool:
        for rule in self._rules:
            if rule.name == name:
                rule.enabled = True
                return True
        return False

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_rules": len(self._rules),
            "enabled_rules": sum(1 for r in self._rules if r.enabled),
            "total_fired": self._fired_total,
        }


def build_standard_alerts(
    risk_manager: Any,
    circuit_breaker: Any,
    daily_loss_warning_pct: float = 3.0,
    drawdown_warning_pct: float = 10.0,
    cooldown_minutes: int | None = None,
) -> list[AlertRule]:
    """
    Build the standard set of trading system alert rules.

    Args:
        daily_loss_warning_pct: threshold (%) for the ``daily_loss_high`` rule.
        drawdown_warning_pct: threshold (%) for the ``drawdown_warning`` rule.
        cooldown_minutes: when given, overrides all three rules' cooldown
            uniformly (this mirrors configs/monitoring.yaml's single
            ``alerts.cooldown_minutes`` key, which doesn't distinguish per
            rule). When ``None`` (the default, used when no yaml override
            is present), each rule keeps its own historically-tuned
            cooldown — 5 min for the CRITICAL circuit-breaker alert, 60/120
            min for the two WARNING-severity threshold alerts — since a
            CRITICAL page shouldn't be silenced as long as a WARNING one.
    """
    circuit_breaker_cooldown = cooldown_minutes if cooldown_minutes is not None else 5
    daily_loss_cooldown = cooldown_minutes if cooldown_minutes is not None else 60
    drawdown_cooldown = cooldown_minutes if cooldown_minutes is not None else 120
    return [
        AlertRule(
            name="circuit_breaker_open",
            severity=AlertSeverity.CRITICAL,
            check_fn=lambda: circuit_breaker.is_open,
            message_fn=lambda: f"Circuit breaker OPEN: {circuit_breaker.open_reason}",
            cooldown_minutes=circuit_breaker_cooldown,
        ),
        AlertRule(
            name="daily_loss_high",
            severity=AlertSeverity.WARNING,
            check_fn=lambda: (
                risk_manager.equity > 0
                and float(abs(risk_manager.daily_pnl) / risk_manager.equity * 100)
                > daily_loss_warning_pct
                and risk_manager.daily_pnl < 0
            ),
            message_fn=lambda: (
                f"Daily loss at {float(abs(risk_manager.daily_pnl) / risk_manager.equity * 100):.1f}%"
            ),
            cooldown_minutes=daily_loss_cooldown,
        ),
        AlertRule(
            name="drawdown_warning",
            severity=AlertSeverity.WARNING,
            check_fn=lambda: risk_manager._drawdown.current_drawdown_pct > drawdown_warning_pct,
            message_fn=lambda: f"Drawdown at {risk_manager._drawdown.current_drawdown_pct:.1f}%",
            cooldown_minutes=drawdown_cooldown,
        ),
    ]
