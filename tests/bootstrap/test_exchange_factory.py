"""Unit tests for src.bootstrap.exchange_factory.

Covers provider selection (binance/bybit), the ConfigurationError guard
rails when the matching sub-config is missing, and rejection of unknown
providers -- for both build_rest_client and build_ws_client.
"""

from __future__ import annotations

import pytest

from src.bootstrap.exchange_factory import build_rest_client, build_ws_client
from src.core.exceptions import ConfigurationError
from src.exchange.adapters.bybit import BybitFuturesClient
from src.exchange.adapters.bybit.websocket import BybitWebSocket
from src.exchange.binance.rest import BinanceFuturesRESTClient
from src.exchange.binance.websocket import BinanceFuturesWebSocket


class TestBuildRestClient:
    def test_binance_provider_returns_binance_rest_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="binance", with_binance=True)
        client = build_rest_client(settings)
        assert isinstance(client, BinanceFuturesRESTClient)

    def test_bybit_provider_returns_bybit_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="bybit", with_binance=False, with_bybit=True)
        client = build_rest_client(settings)
        assert isinstance(client, BybitFuturesClient)

    def test_provider_is_case_insensitive(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="BINANCE", with_binance=True)
        client = build_rest_client(settings)
        assert isinstance(client, BinanceFuturesRESTClient)

    def test_binance_provider_without_binance_config_raises(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="binance", with_binance=False)
        with pytest.raises(ConfigurationError, match="binance"):
            build_rest_client(settings)

    def test_bybit_provider_without_bybit_config_raises(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="bybit", with_binance=False, with_bybit=False)
        with pytest.raises(ConfigurationError, match="bybit"):
            build_rest_client(settings)

    def test_unknown_provider_raises_configuration_error(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="kraken", with_binance=False)
        with pytest.raises(ConfigurationError, match="Unknown exchange_provider"):
            build_rest_client(settings)

    def test_binance_client_is_constructed_with_settings_values(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="binance", with_binance=True)
        client = build_rest_client(settings)
        assert client._api_key == "dummy_api_key"  # noqa: SLF001


class TestBuildWsClient:
    def test_binance_provider_returns_binance_ws_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="binance", with_binance=True)
        client = build_ws_client(settings)
        assert isinstance(client, BinanceFuturesWebSocket)

    def test_bybit_provider_returns_bybit_ws_client(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="bybit", with_binance=False, with_bybit=True)
        client = build_ws_client(settings)
        assert isinstance(client, BybitWebSocket)

    def test_binance_provider_without_binance_config_raises(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="binance", with_binance=False)
        with pytest.raises(ConfigurationError, match="binance"):
            build_ws_client(settings)

    def test_bybit_provider_without_bybit_config_raises(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="bybit", with_binance=False, with_bybit=False)
        with pytest.raises(ConfigurationError, match="bybit"):
            build_ws_client(settings)

    def test_unknown_provider_raises_configuration_error(self, make_settings) -> None:  # type: ignore[no-untyped-def]
        settings = make_settings(provider="kraken", with_binance=False)
        with pytest.raises(ConfigurationError, match="Unknown exchange_provider"):
            build_ws_client(settings)
