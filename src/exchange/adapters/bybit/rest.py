"""
Bybit V5 USDT-Perpetual ("linear" category) REST client.

Implements the same ``BaseExchangeClient`` contract as the Binance
adapter (``src/exchange/binance/rest.py``), so ``ExecutionEngine``,
``RiskManager``, backtesting, and every other layer can drive either
exchange without any code changes — the concrete client is selected once,
in ``bootstrap``, from ``configs/exchanges.yaml``.

Docs: https://bybit-exchange.github.io/docs/v5/intro

Note on request signing: Bybit V5 signs
``timestamp + api_key + recv_window + payload`` where ``payload`` is the
sorted query string for GET/DELETE and the exact JSON request body for
POST. This client signs POST bodies with the same ``json.dumps`` call
aiohttp uses to serialize the body (see ``_sign_request``), so the
signed string and the transmitted bytes always match. As with any new
exchange integration, verify signed calls against Bybit's public testnet
(``api-testnet.bybit.com``) before routing real capital through it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.core.enums import MarginType, OrderSide, OrderType, Timeframe
from src.core.exceptions import (
    ExchangeAPIError,
    ExchangeAuthenticationError,
    OrderNotFoundError,
)
from src.core.models import OHLCV, OrderBook, Ticker
from src.domain.order import Order
from src.domain.position import Position
from src.exchange.adapters.bybit.auth import query_string_for_signing, sign_request
from src.exchange.adapters.bybit.parser import (
    TIMEFRAME_TO_BYBIT,
    normalize_instruments_info,
    parse_candles,
    parse_order,
    parse_order_book,
    parse_position,
    parse_ticker,
    to_bybit_order_type,
)
from src.exchange.base_client import BaseExchangeClient, RateLimiter

logger = logging.getLogger(__name__)

_CATEGORY = "linear"  # USDT-margined perpetuals; Bybit's other categories
# (spot, inverse, option) are out of scope here.


def _fmt_decimal(value: Decimal) -> str:
    """Render a Decimal as fixed-point (never scientific notation)."""
    return format(value, "f")


class BybitFuturesClient(BaseExchangeClient):
    """Bybit V5 linear-perpetual futures REST API client."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.bybit.com",
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 3,
        max_orders_per_second: int = 10,
        max_orders_per_minute: int = 600,
    ) -> None:
        rate_limiter = RateLimiter(
            max_per_second=max_orders_per_second,
            max_per_minute=max_orders_per_minute,
            max_per_10_seconds=100,
        )
        super().__init__(
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            rate_limiter=rate_limiter,
        )
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window

    # ─── Signing / error handling ─────────────────────────────────────────────

    async def _sign_request(
        self,
        params: dict[str, Any],
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = json.dumps(data) if data else query_string_for_signing(params)
        headers = sign_request(
            self._api_key,
            self._api_secret,
            self._recv_window,
            payload,
            clock_offset_ms=self.clock_offset_ms,
        )
        # BaseExchangeClient builds its aiohttp.ClientSession once, at
        # connect(), from _default_headers() — _execute_request() never
        # passes per-call headers (Binance signs via the query string, so
        # the base class was never built for header-based signing). Bybit
        # V5 signs via X-BAPI-* headers, so we update the session's
        # (persistent, merged-per-request) default headers with a fresh
        # signature immediately before each signed call. This client does
        # not issue concurrent signed requests on the same session
        # (ExecutionEngine serializes order calls per symbol), so the brief
        # window between updating these headers and the request firing is
        # safe in practice; a future concurrent-safe version should carry
        # signed headers on the request itself instead.
        if self._session is not None:
            self._session.headers.update(headers)
        return params, data

    async def get_server_time(self) -> int:
        data = await self._get("/v5/market/time")
        return int(data["result"]["timeNano"]) // 1_000_000

    def _is_clock_drift_error(self, body: Any) -> bool:
        # Bybit error code 10002: "request not authorized / timestamp error"
        return isinstance(body, dict) and body.get("retCode") == 10002

    def _handle_response_error(self, status: int, body: Any) -> None:
        # BUG (found during test-coverage audit): the original guard here was
        # `if status != 200 or not isinstance(body, dict): return`, which
        # returned WITHOUT raising for every non-200 HTTP status — the exact
        # inverse of the intended check. Bybit V5 normally reports API-level
        # errors as HTTP 200 with a non-zero `retCode`, so status==200 is the
        # common case this method exists to inspect, but a non-200 response
        # (401/403 from the gateway before the request ever reaches Bybit's
        # business logic, 404, etc. — anything below the >=500 threshold
        # already handled in base_client._execute_request) is itself an
        # error and must not be swallowed and returned to the caller as if
        # it were valid data.
        # Note ret_code has no default here (vs. body.get("retCode", 0)) so
        # "key absent" (None) stays distinguishable from "key present and
        # zero" (0, meaning Bybit itself reported success).
        ret_code = body.get("retCode") if isinstance(body, dict) else None
        msg = body.get("retMsg", str(body)) if isinstance(body, dict) else str(body)

        if status == 200:
            if ret_code in (None, 0):
                return
        elif ret_code is None:
            # Non-200 with no Bybit-shaped error body (e.g. a bare 401/403/404
            # from the gateway, before the request ever reached Bybit's own
            # business logic) — map on HTTP status alone rather than
            # swallowing it.
            if status in (401, 403):
                raise ExchangeAuthenticationError(f"Bybit HTTP {status}: {msg}")
            raise ExchangeAPIError(status, msg, body)

        if ret_code in (10003, 10004, 10005):  # invalid key / sign / permission
            raise ExchangeAuthenticationError(f"Bybit auth error [{ret_code}]: {msg}")
        if ret_code == 110001:  # order does not exist
            raise OrderNotFoundError("unknown", "unknown")
        raise ExchangeAPIError(ret_code if ret_code is not None else status, msg, body)

    # ─── Account ──────────────────────────────────────────────────────────────

    async def get_account_balance(self) -> dict[str, Decimal]:
        data = await self._get(
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            signed=True,
        )
        result: dict[str, Decimal] = {}
        for account in data["result"]["list"]:
            for coin in account.get("coin", []):
                bal = Decimal(str(coin.get("walletBalance", "0") or "0"))
                if bal > Decimal("0"):
                    result[coin["coin"]] = bal
        return result

    # ─── Positions ────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        data = await self._get(
            "/v5/position/list",
            params={"category": _CATEGORY, "settleCoin": "USDT"},
            signed=True,
        )
        positions = []
        for item in data["result"]["list"]:
            pos = parse_position(item)
            if pos is not None:
                positions.append(pos)
        logger.debug("Fetched %d open Bybit positions", len(positions))
        return positions

    # ─── Orders ───────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Decimal | None = None,
        *,
        stop_price: Decimal | None = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        body: dict[str, Any] = {
            "category": _CATEGORY,
            "symbol": symbol.upper(),
            "side": "Buy" if side == OrderSide.BUY else "Sell",
            "orderType": to_bybit_order_type(order_type),
            "qty": _fmt_decimal(quantity),
            "reduceOnly": reduce_only,
        }
        if order_type != OrderType.MARKET:
            body["timeInForce"] = (
                "PostOnly" if time_in_force.upper() == "GTX" else time_in_force.upper()
            )
        if price is not None and price > Decimal("0"):
            body["price"] = _fmt_decimal(price)
        if stop_price is not None and stop_price > Decimal("0"):
            body["triggerPrice"] = _fmt_decimal(stop_price)
            body["triggerDirection"] = 1 if side == OrderSide.SELL else 2
        if client_order_id:
            body["orderLinkId"] = client_order_id

        logger.info(
            "Placing Bybit order: %s %s %s qty=%s price=%s",
            symbol,
            side,
            order_type,
            quantity,
            price,
        )
        response = await self._post("/v5/order/create", data=body, signed=True)
        exchange_order_id = response["result"]["orderId"]

        # Bybit's create-order response only echoes back the IDs, not the
        # full order state, so fetch the authoritative order snapshot.
        return await self.get_order(symbol, exchange_order_id)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            body: dict[str, Any] = {"category": _CATEGORY, "symbol": symbol.upper()}
            if order_id.startswith("ts_"):
                body["orderLinkId"] = order_id
            else:
                body["orderId"] = order_id
            await self._post("/v5/order/cancel", data=body, signed=True)
            logger.info("Cancelled Bybit order %s for %s", order_id, symbol)
            return True
        except OrderNotFoundError:
            logger.info("Cancel no-op: Bybit order %s for %s already gone", order_id, symbol)
            return True
        except ExchangeAPIError as exc:
            logger.warning("Failed to cancel Bybit order %s: %s", order_id, exc)
            return False

    async def get_order(self, symbol: str, order_id: str) -> Order:
        params: dict[str, Any] = {"category": _CATEGORY, "symbol": symbol.upper()}
        if order_id.startswith("ts_"):
            params["orderLinkId"] = order_id
        else:
            params["orderId"] = order_id
        data = await self._get("/v5/order/realtime", params=params, signed=True)
        orders = data["result"]["list"]
        if not orders:
            # Not in open orders — check order history (filled/cancelled).
            data = await self._get("/v5/order/history", params=params, signed=True)
            orders = data["result"]["list"]
        if not orders:
            raise OrderNotFoundError(order_id, symbol)
        return parse_order(orders[0])

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"category": _CATEGORY}
        if symbol:
            params["symbol"] = symbol.upper()
        else:
            params["settleCoin"] = "USDT"
        data = await self._get("/v5/order/realtime", params=params, signed=True)
        return [parse_order(item) for item in data["result"]["list"]]

    # ─── Market data ──────────────────────────────────────────────────────────

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        start_time: datetime | None = None,
    ) -> list[OHLCV]:
        interval = TIMEFRAME_TO_BYBIT.get(timeframe.value)
        if interval is None:
            raise ValueError(f"Bybit does not support timeframe {timeframe.value}")
        params: dict[str, Any] = {
            "category": _CATEGORY,
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["start"] = int(start_time.timestamp() * 1000)
        data = await self._get("/v5/market/kline", params=params)
        return parse_candles(data["result"]["list"])

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._get(
            "/v5/market/tickers",
            params={"category": _CATEGORY, "symbol": symbol.upper()},
        )
        return parse_ticker(data["result"]["list"][0])

    async def get_order_book(self, symbol: str, depth: int = 25) -> OrderBook:
        data = await self._get(
            "/v5/market/orderbook",
            params={"category": _CATEGORY, "symbol": symbol.upper(), "limit": min(depth, 200)},
        )
        return parse_order_book(data["result"], symbol.upper())

    async def get_exchange_info(self) -> dict[str, Any]:
        """
        Fetch exchange trading rules and symbol info.

        Normalized to the Binance ``exchangeInfo`` shape at the adapter
        boundary (see ``parser.normalize_instruments_info``) so
        ``parse_instruments`` and everything downstream of it need no
        provider-specific branches.
        """
        data = await self._get(
            "/v5/market/instruments-info",
            params={"category": _CATEGORY},
        )
        return normalize_instruments_info(data)

    # ─── Account configuration ───────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._post(
            "/v5/position/set-leverage",
            data={
                "category": _CATEGORY,
                "symbol": symbol.upper(),
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
            signed=True,
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        mode = 0 if MarginType(margin_type.lower()) == MarginType.CROSS else 1
        await self._post(
            "/v5/position/switch-isolated",
            data={
                "category": _CATEGORY,
                "symbol": symbol.upper(),
                "tradeMode": mode,
                "buyLeverage": "1",
                "sellLeverage": "1",
            },
            signed=True,
        )
