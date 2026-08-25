"""Unit tests for src.bootstrap.dependency_container.wire_container.

wire_container() takes a fully constructed Application and registers its
components into the global DIContainer / ComponentRegistry singletons.
Uses a duck-typed fake "Application" (a SimpleNamespace with the exact
attributes wire_container reads) rather than a real Application, since
wire_container only ever accesses attributes -- it never calls methods on
them -- keeping this test focused on the wiring logic itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bootstrap.dependency_container import wire_container
from src.core.dependency import get_container, reset_container
from src.core.registry import get_registry, reset_registry


@pytest.fixture(autouse=True)
def _isolated_di_state():  # type: ignore[no-untyped-def]
    reset_container()
    reset_registry()
    yield
    reset_container()
    reset_registry()


def _fake_app() -> SimpleNamespace:
    """A minimal stand-in with every attribute wire_container touches."""
    return SimpleNamespace(
        rest_client=object(),
        ws_client=object(),
        account_service=object(),
        cache=object(),
        candle_service=object(),
        market_feed=object(),
        risk_manager=object(),
        portfolio=object(),
        order_manager=object(),
        position_manager=object(),
        execution_engine=object(),
        fill_engine=object(),
        health_checker=object(),
        alert_manager=object(),
        notifier=object(),
        heartbeat=object(),
        db=object(),
        scheduler=object(),
    )


class TestWireContainer:
    def test_registers_all_documented_component_names(self) -> None:
        app = _fake_app()
        wire_container(app)
        registry = get_registry()

        expected_names = {
            "rest_client",
            "ws_client",
            "account_service",
            "cache",
            "candle_service",
            "market_feed",
            "risk_manager",
            "portfolio",
            "order_manager",
            "position_manager",
            "execution_engine",
            "fill_engine",
            "health_checker",
            "alert_manager",
            "notifier",
            "heartbeat",
            "database",
            "scheduler",
        }
        for name in expected_names:
            # get_optional never raises for a registered name; None would
            # mean it was never registered.
            assert registry.get_optional(name, object) is not None, name

    def test_registered_instances_are_the_exact_objects_passed_in(self) -> None:
        app = _fake_app()
        wire_container(app)
        registry = get_registry()

        assert registry.get("rest_client", object) is app.rest_client
        assert registry.get("ws_client", object) is app.ws_client
        assert registry.get("order_manager", object) is app.order_manager
        assert registry.get("database", object) is app.db

    def test_rest_and_ws_clients_are_bound_as_di_singletons(self) -> None:
        app = _fake_app()
        wire_container(app)
        container = get_container()

        assert container.resolve(type(app.rest_client)) is app.rest_client
        assert container.resolve(type(app.ws_client)) is app.ws_client

    def test_calling_twice_raises_on_duplicate_registration(self) -> None:
        """Regression/documentation test: wire_container is NOT idempotent.
        ComponentRegistry.register() rejects re-registering an existing
        name (overwrite=False by default), so calling wire_container a
        second time against the same registry (e.g. a hot reload without
        a process restart) raises rather than silently re-wiring."""
        app = _fake_app()
        wire_container(app)
        with pytest.raises(ValueError, match="already registered"):
            wire_container(app)
