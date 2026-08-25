"""
Binance USDT-M Perpetual Futures REST client.

Implements all exchange operations needed by the trading system.
Uses HMAC-SHA256 signing for private endpoints.

Docs: https://binance-docs.github.io/apidocs/futures/en/
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.core.enums import (
    ExchangeStatus,
    MarginType,
    OrderSide,
    OrderType,
    Timeframe,
)
from src.core.exceptions import (
    ExchangeAPIError,
    ExchangeAuthenticationError,
    InsufficientBalanceError,
    OrderNotFoundError,
)
from src.core.models import OHLCV, FundingRate, OrderBook, Ticker
from src.core.validation import validate_candle_limit
from src.domain.order import Order
from src.domain.position import Position
from src.exchange.base_client import BaseExchangeClient, RateLimiter
from src.exchange.binance.auth import get_headers, sign_params
from src.exchange.binance.parser import (
    parse_candles,
    parse_funding_rate_history_record,
    parse_order,
    parse_order_book,
    parse_position,
    parse_ticker,
)

logger = logging.getLogger(__name__)


def _fmt_decimal(value: Decimal) -> str:
    """
    Render a Decimal as a plain fixed-point string for exchange APIs.

    ``str(Decimal(...))`` switches to scientific notation for very small or
    very large magnitudes (e.g. ``Decimal("1E-8")`` -> ``"1E-8"``), which
    Binance's parser rejects or misinterprets. Low-priced altcoins with
    8-decimal tick sizes hit this routinely, so every quantity/price sent to
    the exchange must go through fixed-point formatting instead of bare
    ``str()``.
    """
    return format(value, "f")


class BinanceFuturesRESTClient(BaseExchangeClient):
    """
    Binance USDT-M Perpetual Futures REST API client.

    Endpoints:
      - Private (signed): account, orders, positions
      - Public: market data, exchange info
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        recv_window: int = 5000,
        timeout: float = 10.0,
        max_retries: int = 3,
        max_orders_per_second: int = 10,
        max_orders_per_minute: int = 1200,
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

    def _default_headers(self) -> dict[str, str]:
        headers = super()._default_headers()
        headers.update(get_headers(self._api_key))
        return headers

    async def _sign_request(
        self,
        params: dict[str, Any],
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add timestamp, recvWindow, and HMAC signature."""
        signed = sign_params(
            self._api_key,
            self._api_secret,
            params,
            self._recv_window,
            clock_offset_ms=self.clock_offset_ms,
        )
        return signed, data

    def _is_clock_drift_error(self, body: Any) -> bool:
        """Binance code -1021: request timestamp outside the recvWindow."""
        return isinstance(body, dict) and body.get("code") == -1021

    def _handle_response_error(self, status: int, body: Any) -> None:
        """Raise exchange-specific exception for error responses."""
        if status == 200:
            return
        if isinstance(body, dict):
            code = body.get("code", status)
            msg = body.get("msg", str(body))
        else:
            code = status
            msg = str(body)

        if status == 401 or code == -2014:
            raise ExchangeAuthenticationError(f"Binance auth error [{code}]: {msg}")
        if code == -2011:
            raise OrderNotFoundError("unknown", "unknown")
        if code in (-1013, -2010):
            raise ExchangeAPIError(code, msg, body)
        raise ExchangeAPIError(code, msg, body)

    # ─── Account ──────────────────────────────────────────────────────────────

    async def get_account_balance(self) -> dict[str, Decimal]:
        """Fetch per-asset balances from account info."""
        data = await self._get("/fapi/v2/balance", signed=True)
        return {
            item["asset"]: Decimal(str(item["balance"]))
            for item in data
            if Decimal(str(item["balance"])) > Decimal("0")
        }

    async def get_account_info(self) -> dict[str, Any]:
        """Fetch full account detail including margin, equity, positions."""
        return await self._get("/fapi/v2/account", signed=True)  # type: ignore[return-value]

    # ─── Positions ────────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """Fetch all positions with non-zero quantity."""
        data = await self._get("/fapi/v2/positionRisk", signed=True)
        positions = []
        for item in data:
            pos = parse_position(item)
            if pos is not None:
                positions.append(pos)
        logger.debug("Fetched %d open positions", len(positions))
        return positions

    async def get_position(self, symbol: str) -> list[Position]:
        """Fetch positions for a specific symbol."""
        data = await self._get(
            "/fapi/v2/positionRisk",
            params={"symbol": symbol.upper()},
            signed=True,
        )
        return [p for item in data if (p := parse_position(item)) is not None]

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
        position_side: str = "BOTH",
    ) -> Order:
        """Submit a new order to Binance Futures."""
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.value.upper(),
            "type": self._map_order_type(order_type),
            "quantity": _fmt_decimal(quantity),
            "positionSide": position_side.upper(),
            "reduceOnly": str(reduce_only).lower(),
        }

        # timeInForce is only valid for LIMIT-family and STOP_LIMIT-family
        # orders. Sending it for MARKET, STOP_MARKET, TAKE_PROFIT_MARKET, or
        # TRAILING_STOP_MARKET triggers Binance error -1106
        # ("Parameter 'timeInForce' sent when not required").
        _NO_TIF_TYPES = {
            OrderType.MARKET,
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_MARKET,
            OrderType.TRAILING_STOP_MARKET,
        }
        if order_type not in _NO_TIF_TYPES:
            params["timeInForce"] = time_in_force.upper()

        if price is not None and price > Decimal("0"):
            params["price"] = _fmt_decimal(price)

        if stop_price is not None and stop_price > Decimal("0"):
            params["stopPrice"] = _fmt_decimal(stop_price)

        if client_order_id:
            params["newClientOrderId"] = client_order_id

        logger.info(
            "Placing order: %s %s %s qty=%s price=%s",
            symbol, side, order_type, quantity, price,
        )

        response = await self._post("/fapi/v1/order", params=params, signed=True)
        order = parse_order(response)
        logger.info("Order placed: %s -> exchange_id=%s", client_order_id, order.exchange_order_id)
        return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""
        try:
            params: dict[str, Any] = {"symbol": symbol.upper()}
            if order_id.startswith("ts_"):
                params["origClientOrderId"] = order_id
            else:
                params["orderId"] = order_id
            await self._delete("/fapi/v1/order", params=params, signed=True)
            logger.info("Cancelled order %s for %s", order_id, symbol)
            return True
        except OrderNotFoundError:
            # The order is already gone (filled, expired, or previously
            # cancelled) — from the caller's perspective the goal of
            # "no longer open on the exchange" is already satisfied. Treating
            # this as a failure caused callers (e.g. cancel-all-then-resubmit
            # flows) to retry indefinitely against an order that will never
            # exist again.
            logger.info("Cancel no-op: order %s for %s already gone", order_id, symbol)
            return True
        except ExchangeAPIError as exc:
            logger.warning("Failed to cancel order %s: %s", order_id, exc)
            return False

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol."""
        await self._delete(
            "/fapi/v1/allOpenOrders",
            params={"symbol": symbol.upper()},
            signed=True,
        )
        logger.info("Cancelled all orders for %s", symbol)
        return True

    async def get_order(self, symbol: str, order_id: str) -> Order:
        """Fetch order state from exchange."""
        params: dict[str, Any] = {"symbol": symbol.upper()}
        if order_id.startswith("ts_"):
            params["origClientOrderId"] = order_id
        else:
            params["orderId"] = order_id
        data = await self._get("/fapi/v1/order", params=params, signed=True)
        return parse_order(data)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Fetch all open orders, optionally filtered by symbol."""
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        data = await self._get("/fapi/v1/openOrders", params=params, signed=True)
        return [parse_order(item) for item in data]

    # ─── Market data ──────────────────────────────────────────────────────────

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 500,
        start_time: datetime | None = None,
    ) -> list[OHLCV]:
        """Fetch historical klines from Binance."""
        limit = validate_candle_limit(limit)
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": timeframe.value,
            "limit": str(limit),
        }
        if start_time:
            params["startTime"] = str(int(start_time.timestamp() * 1000))

        data = await self._get("/fapi/v1/klines", params=params)
        candles = parse_candles(data)
        logger.debug("Fetched %d candles for %s %s", len(candles), symbol, timeframe)
        return candles

    async def get_ticker(self, symbol: str) -> Ticker:
        """Fetch 24h price statistics."""
        data = await self._get(
            "/fapi/v1/ticker/24hr",
            params={"symbol": symbol.upper()},
        )
        return parse_ticker(data)

    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch current mark price."""
        data = await self._get(
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol.upper()},
        )
        return Decimal(str(data["markPrice"]))

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        """Fetch order book depth."""
        valid_depths = {5, 10, 20, 50, 100, 500, 1000}
        depth = min((d for d in valid_depths if d >= depth), default=1000)
        data = await self._get(
            "/fapi/v1/depth",
            params={"symbol": symbol.upper(), "limit": str(depth)},
        )
        return parse_order_book(data, symbol.upper())

    async def get_exchange_info(self) -> dict[str, Any]:
        """Fetch exchange trading rules and symbol info."""
        return await self._get("/fapi/v1/exchangeInfo")  # type: ignore[return-value]

    async def get_server_time(self) -> int:
        """Fetch exchange server time in milliseconds."""
        data = await self._get("/fapi/v1/time")
        return int(data["serverTime"])

    # ─── Account configuration ────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol (1-125x)."""
        leverage = max(1, min(125, leverage))
        await self._post(
            "/fapi/v1/leverage",
            params={"symbol": symbol.upper(), "leverage": str(leverage)},
            signed=True,
        )
        logger.info("Set leverage for %s: %dx", symbol, leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        """Set margin mode: ISOLATED or CROSSED."""
        mode = "ISOLATED" if margin_type.upper() == "ISOLATED" else "CROSSED"
        try:
            await self._post(
                "/fapi/v1/marginType",
                params={"symbol": symbol.upper(), "marginType": mode},
                signed=True,
            )
            logger.info("Set margin type for %s: %s", symbol, mode)
        except ExchangeAPIError as exc:
            # Code -4046 = "No need to change margin type" — not an error
            if exc.code == -4046:
                logger.debug("Margin type already %s for %s", mode, symbol)
            else:
                raise

    async def get_income_history(
        self,
        income_type: str = "REALIZED_PNL",
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch income history (funding, realized PnL, etc.)."""
        params: dict[str, Any] = {
            "incomeType": income_type,
            "limit": str(limit),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        return await self._get("/fapi/v1/income", params=params, signed=True)  # type: ignore[return-value]

    # ─── User data stream (listen key) ───────────────────────────────────────

    async def create_listen_key(self) -> str:
        """
        Create a new user data stream listen key.

        The key expires after 60 minutes of inactivity; call
        ``renew_listen_key`` every 30 minutes to keep it alive.
        """
        data = await self._post("/fapi/v1/listenKey", signed=False)
        listen_key: str = data["listenKey"]
        logger.info("Created user-data-stream listen key (prefix=%s...)", listen_key[:8])
        return listen_key

    async def renew_listen_key(self, listen_key: str) -> None:
        """
        Extend the validity of an existing listen key (PUT /fapi/v1/listenKey).

        Binance requires this at least every 60 minutes.  Recommended
        interval: every 30 minutes (``BINANCE_LISTEN_KEY_REFRESH_MINUTES``).
        """
        await self._put("/fapi/v1/listenKey", params={"listenKey": listen_key}, signed=False)
        logger.debug("Renewed listen key (prefix=%s...)", listen_key[:8])

    async def delete_listen_key(self, listen_key: str) -> None:
        """
        Close and invalidate a user data stream listen key.

        Should be called on clean shutdown to free server-side resources.
        """
        await self._delete("/fapi/v1/listenKey", params={"listenKey": listen_key}, signed=False)
        logger.info("Deleted listen key (prefix=%s...)", listen_key[:8])

    # ─── Funding rates ────────────────────────────────────────────────────────

    async def get_funding_rate_history(
        self,
        symbol: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Fetch historical funding rate records for a symbol.

        Each record has keys: ``symbol``, ``fundingRate``, ``fundingTime``.
        Binance publishes funding every 8 hours (00:00, 08:00, 16:00 UTC).

        Args:
            symbol: Trading pair, e.g. ``"BTCUSDT"``.
            start_time: Unix epoch milliseconds (inclusive).
            end_time: Unix epoch milliseconds (inclusive).
            limit: Max records to return (1–1000, default 100).
        """
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "limit": str(min(limit, 1000)),
        }
        if start_time is not None:
            params["startTime"] = str(start_time)
        if end_time is not None:
            params["endTime"] = str(end_time)
        return await self._get("/fapi/v1/fundingRate", params=params)  # type: ignore[return-value]

    async def get_mark_price_all(self) -> list[dict[str, Any]]:
        """Fetch mark price and current funding rate for all symbols."""
        return await self._get("/fapi/v1/premiumIndex")  # type: ignore[return-value]

    async def fetch_funding_rate_history_parsed(
        self,
        symbol: str,
        start_time: int,
        end_time: int,
    ) -> list[FundingRate]:
        """
        Fetch and parse the full historical funding-rate series for
        ``symbol`` between ``start_time`` and ``end_time`` (Unix epoch ms),
        paginating past the 1000-record-per-request cap (Binance settles
        every 8h, so 1000 records ≈ 333 days per page).

        Intended as the data source for
        ``BacktestConfig.funding_rates`` — records come back sorted
        ascending by settlement time, matching what
        ``BacktestSimulator._apply_funding_rates`` expects.
        """
        out: list[FundingRate] = []
        cursor = start_time
        while True:
            page = await self.get_funding_rate_history(
                symbol=symbol, start_time=cursor, end_time=end_time, limit=1000
            )
            if not page:
                break
            out.extend(parse_funding_rate_history_record(r) for r in page)
            if len(page) < 1000:
                break
            # Advance past the last record's timestamp to avoid re-fetching it.
            cursor = int(page[-1]["fundingTime"]) + 1
            if cursor > end_time:
                break
        return out

    # ─── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _map_order_type(order_type: OrderType) -> str:
        mapping: dict[OrderType, str] = {
            OrderType.MARKET: "MARKET",
            OrderType.LIMIT: "LIMIT",
            OrderType.STOP_MARKET: "STOP_MARKET",
            OrderType.STOP_LIMIT: "STOP",
            OrderType.TAKE_PROFIT_MARKET: "TAKE_PROFIT_MARKET",
            OrderType.TAKE_PROFIT_LIMIT: "TAKE_PROFIT",
            OrderType.TRAILING_STOP_MARKET: "TRAILING_STOP_MARKET",
        }
        result = mapping.get(order_type)
        if result is None:
            raise ValueError(f"Unsupported order type: {order_type}")
        return result
