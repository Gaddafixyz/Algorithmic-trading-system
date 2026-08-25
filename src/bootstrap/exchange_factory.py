"""
Exchange client factory.

Selects and constructs the concrete ``IExchangeClient`` implementation
based on ``settings.exchange_provider``. This is the single place that
knows about specific exchange adapters — every other layer (execution,
risk, backtesting, paper trading) depends only on the ``IExchangeClient``
protocol from ``src.core.interfaces``.

Usage::

    from src.bootstrap.exchange_factory import build_rest_client

    client = build_rest_client(settings)
    await client.connect()

Adding a new exchange: implement ``IExchangeClient`` under
``src/exchange/adapters/<name>/``, then add one branch here.
"""

from __future__ import annotations

import logging

from src.core.config import Settings
from src.core.exceptions import InvalidConfigurationError, MissingConfigurationError
from src.core.interfaces import IExchangeClient, IWebSocketStreamClient

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("binance", "bybit")


def build_rest_client(settings: Settings) -> IExchangeClient:
    """Construct the configured exchange's REST client from settings."""
    provider = settings.exchange_provider.lower()

    if provider == "binance":
        if settings.binance is None:
            raise MissingConfigurationError(
                "binance",
                detail=(
                    "exchange_provider='binance' but no BinanceConfig is set "
                    "(check BINANCE_API_KEY / BINANCE_API_SECRET)."
                ),
            )
        from src.exchange.binance.rest import BinanceFuturesRESTClient

        binance_cfg = settings.binance
        logger.info("Exchange provider: Binance (testnet=%s)", binance_cfg.testnet)
        return BinanceFuturesRESTClient(
            api_key=binance_cfg.api_key.get_secret_value(),
            api_secret=binance_cfg.api_secret.get_secret_value(),
            base_url=binance_cfg.rest_base_url,
            recv_window=binance_cfg.recv_window,
            timeout=binance_cfg.http_timeout,
            max_retries=binance_cfg.max_retries,
            max_orders_per_second=binance_cfg.max_orders_per_second,
            max_orders_per_minute=binance_cfg.max_orders_per_minute,
        )

    if provider == "bybit":
        if settings.bybit is None:
            raise MissingConfigurationError(
                "bybit",
                detail=(
                    "exchange_provider='bybit' but no BybitConfig is set "
                    "(check BYBIT_API_KEY / BYBIT_API_SECRET)."
                ),
            )
        from src.exchange.adapters.bybit import BybitFuturesClient

        bybit_cfg = settings.bybit
        logger.info("Exchange provider: Bybit (testnet=%s)", bybit_cfg.testnet)
        return BybitFuturesClient(
            api_key=bybit_cfg.api_key.get_secret_value(),
            api_secret=bybit_cfg.api_secret.get_secret_value(),
            base_url=bybit_cfg.rest_base_url,
            recv_window=bybit_cfg.recv_window,
            timeout=bybit_cfg.http_timeout,
            max_retries=bybit_cfg.max_retries,
            max_orders_per_second=bybit_cfg.max_orders_per_second,
            max_orders_per_minute=bybit_cfg.max_orders_per_minute,
        )

    raise InvalidConfigurationError(
        "exchange_provider",
        provider,
        f"Unknown exchange_provider '{provider}'. Supported: {SUPPORTED_PROVIDERS}",
    )


def build_ws_client(settings: Settings) -> IWebSocketStreamClient:
    """Construct the configured exchange's WebSocket streaming client."""
    provider = settings.exchange_provider.lower()

    if provider == "binance":
        if settings.binance is None:
            raise MissingConfigurationError(
                "binance",
                detail=(
                    "exchange_provider='binance' but no BinanceConfig is set "
                    "(check BINANCE_API_KEY / BINANCE_API_SECRET)."
                ),
            )
        from src.exchange.binance.websocket import BinanceFuturesWebSocket

        return BinanceFuturesWebSocket(ws_base_url=settings.binance.ws_base_url)

    if provider == "bybit":
        if settings.bybit is None:
            raise MissingConfigurationError(
                "bybit",
                detail=(
                    "exchange_provider='bybit' but no BybitConfig is set "
                    "(check BYBIT_API_KEY / BYBIT_API_SECRET)."
                ),
            )
        from src.exchange.adapters.bybit.websocket import BybitWebSocket

        bybit_cfg = settings.bybit
        return BybitWebSocket(
            ws_public_url=bybit_cfg.ws_public_url,
            ws_private_url=bybit_cfg.ws_private_url,
            api_key=bybit_cfg.api_key.get_secret_value(),
            api_secret=bybit_cfg.api_secret.get_secret_value(),
        )

    raise InvalidConfigurationError(
        "exchange_provider",
        provider,
        f"Unknown exchange_provider '{provider}'. Supported: {SUPPORTED_PROVIDERS}",
    )
