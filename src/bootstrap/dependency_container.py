"""
Dependency injection container wiring.

Registers all system components into the global DI container
so they can be resolved by name anywhere in the system.
"""

from __future__ import annotations

from src.bootstrap.app import Application
from src.core.dependency import get_container
from src.core.registry import get_registry


def wire_container(app: Application) -> None:
    """Register all application components into the DI container and registry."""
    container = get_container()
    registry = get_registry()

    # Exchange
    container.bind_singleton(type(app.rest_client), instance=app.rest_client)
    container.bind_singleton(type(app.ws_client), instance=app.ws_client)
    registry.register("rest_client", app.rest_client)
    registry.register("ws_client", app.ws_client)
    registry.register("account_service", app.account_service)

    # Market data
    registry.register("cache", app.cache)
    registry.register("candle_service", app.candle_service)
    registry.register("market_feed", app.market_feed)

    # Risk
    registry.register("risk_manager", app.risk_manager)
    registry.register("portfolio", app.portfolio)

    # Execution
    registry.register("order_manager", app.order_manager)
    registry.register("position_manager", app.position_manager)
    registry.register("execution_engine", app.execution_engine)
    registry.register("fill_engine", app.fill_engine)

    # Monitoring
    registry.register("health_checker", app.health_checker)
    registry.register("alert_manager", app.alert_manager)
    registry.register("notifier", app.notifier)
    registry.register("heartbeat", app.heartbeat)

    # Database
    registry.register("database", app.db)

    # Scheduler (periodic background tasks — e.g. equity resync)
    registry.register("scheduler", app.scheduler)
