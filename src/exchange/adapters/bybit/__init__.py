"""
Bybit V5 USDT-Perpetual exchange adapter.

Implements ``src.core.interfaces.IExchangeClient`` so the rest of the
system (execution, risk, backtesting, persistence) can target Bybit
exactly the way it targets Binance — by constructing a client and handing
it to ``ExecutionEngine`` / ``CandleService`` / etc. See
``bootstrap/exchange_factory.py`` for the config-driven selection point.
"""

from __future__ import annotations

from src.exchange.adapters.bybit.rest import BybitFuturesClient
from src.exchange.adapters.bybit.websocket import BybitWebSocket

__all__ = ["BybitFuturesClient", "BybitWebSocket"]
