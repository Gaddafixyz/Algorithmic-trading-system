"""Shared fixtures for tests/bootstrap.

Provides:
- ``make_settings``: builds a minimal, valid ``Settings`` instance without
  touching the environment or any .env file, defaulting to a Binance
  provider with dummy (non-placeholder) credentials.
- ``isolated_event_bus``: fresh EventBus per test, matching the pattern
  used elsewhere in the suite (tests/risk/test_risk_manager_events.py etc).
"""

from __future__ import annotations

from typing import Callable

import pytest

from src.core.config import BinanceConfig, BybitConfig, Settings
from src.core.events import EventBus, reset_event_bus, set_event_bus


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def _make(
        *,
        provider: str = "binance",
        with_binance: bool = True,
        with_bybit: bool = False,
        **overrides: object,
    ) -> Settings:
        kwargs: dict[str, object] = {"exchange_provider": provider}
        if with_binance:
            kwargs["binance"] = BinanceConfig(
                api_key="dummy_api_key", api_secret="dummy_api_secret", testnet=True,
            )
        if with_bybit:
            kwargs["bybit"] = BybitConfig(
                api_key="dummy_api_key", api_secret="dummy_api_secret", testnet=True,
            )
        kwargs.update(overrides)
        return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def isolated_event_bus() -> EventBus:
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()
