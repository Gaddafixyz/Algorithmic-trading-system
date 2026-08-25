"""
Abstract base exchange client.

Provides common infrastructure (retry, rate limiting, auth hooks)
that all exchange implementations share. Concrete implementations
extend this and implement the abstract methods.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from decimal import Decimal
from typing import Any

import aiohttp
from tenacity import (
    AsyncRetrying,
    RetryError,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.core.constants import (
    DEFAULT_HTTP_TIMEOUT,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_MAX_DELAY,
)
from src.core.enums import ExchangeStatus, OrderSide, OrderType, Timeframe
from src.core.exceptions import (
    ExchangeConnectionError,
    ExchangeRateLimitError,
    ExchangeTimeoutError,
    MaxRetriesExceededError,
)
from src.core.models import OHLCV, OrderBook, Ticker
from src.domain.order import Order
from src.domain.position import Position
from src.monitoring.metrics import api_latency, api_requests

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token-bucket rate limiter for exchange API calls.

    Tracks requests-per-second and requests-per-minute independently.
    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        max_per_second: int,
        max_per_minute: int,
        max_per_10_seconds: int = 0,
    ) -> None:
        self.max_per_second = max_per_second
        self.max_per_minute = max_per_minute
        self.max_per_10_seconds = max_per_10_seconds
        self._requests_1s: deque[float] = deque()
        self._requests_10s: deque[float] = deque()
        self._requests_1m: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a request slot is available."""
        async with self._lock:
            now = time.monotonic()
            self._purge(now)
            await self._wait_if_limited(now)
            self._record(now)

    def _purge(self, now: float) -> None:
        while self._requests_1s and now - self._requests_1s[0] > 1.0:
            self._requests_1s.popleft()
        while self._requests_10s and now - self._requests_10s[0] > 10.0:
            self._requests_10s.popleft()
        while self._requests_1m and now - self._requests_1m[0] > 60.0:
            self._requests_1m.popleft()

    async def _wait_if_limited(self, now: float) -> None:
        if len(self._requests_1s) >= self.max_per_second:
            wait = 1.0 - (now - self._requests_1s[0])
            if wait > 0:
                await asyncio.sleep(wait)
        if self.max_per_10_seconds and len(self._requests_10s) >= self.max_per_10_seconds:
            wait = 10.0 - (now - self._requests_10s[0])
            if wait > 0:
                await asyncio.sleep(wait)
        if len(self._requests_1m) >= self.max_per_minute:
            wait = 60.0 - (now - self._requests_1m[0])
            if wait > 0:
                await asyncio.sleep(wait)

    def _record(self, now: float) -> None:
        self._requests_1s.append(now)
        self._requests_10s.append(now)
        self._requests_1m.append(now)


class BaseExchangeClient(ABC):
    """
    Abstract base for exchange REST clients.

    Subclasses must implement all abstract methods.
    Provides:
    - Session management
    - Rate limiting
    - Automatic retry with exponential backoff
    - Structured request logging
    - Status tracking
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        max_retries: int = DEFAULT_RETRY_ATTEMPTS,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._rate_limiter = rate_limiter
        self._session: aiohttp.ClientSession | None = None
        self._status = ExchangeStatus.DISCONNECTED
        self._request_count: int = 0
        self._error_count: int = 0
        # Clock drift correction: offset (ms) added to local timestamps so
        # signed requests stay inside the exchange's recvWindow even if the
        # host clock has drifted. Populated by sync_clock().
        self._clock_offset_ms: int = 0

    # ─── Session lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open HTTP session."""
        if self._session and not self._session.closed:
            return
        connector = aiohttp.TCPConnector(limit=50, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=self._default_headers(),
        )
        self._status = ExchangeStatus.CONNECTED
        logger.info("Exchange client connected: %s", self.base_url)

    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._status = ExchangeStatus.DISCONNECTED
        logger.info("Exchange client disconnected")

    @property
    def clock_offset_ms(self) -> int:
        """Milliseconds to add to local time to approximate exchange time."""
        return self._clock_offset_ms

    async def sync_clock(self) -> int:
        """
        Measure and store the offset between local clock and exchange clock.

        Long-running processes can drift enough (VM clock skew, NTP issues)
        to push signed-request timestamps outside the exchange's recvWindow,
        causing every signed call to fail with a timestamp error. Callers
        should invoke this on startup and periodically (e.g. hourly), and
        immediately after catching a timestamp-related auth error.
        """
        before = time.time()
        server_time_ms = await self.get_server_time()
        after = time.time()
        local_ms = int((before + after) / 2 * 1000)
        self._clock_offset_ms = server_time_ms - local_ms
        logger.info("Clock synced with exchange: offset=%dms", self._clock_offset_ms)
        return self._clock_offset_ms

    @abstractmethod
    async def get_server_time(self) -> int:
        """Fetch exchange server time in milliseconds."""

    async def __aenter__(self) -> "BaseExchangeClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    # ─── HTTP helpers ─────────────────────────────────────────────────────────

    def _default_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        """
        Execute an HTTP request with retry and rate limiting.

        Raises exchange-specific exceptions on failure.
        """
        if self._session is None or self._session.closed:
            await self.connect()

        url = f"{self.base_url}{path}"
        if params:
            params = {k: str(v) for k, v in params.items() if v is not None}

        if signed:
            params, data = await self._sign_request(params or {}, data or {})

        # Transport-level failures (dropped connections, timeouts) as well as
        # transient server-side conditions (rate limits, IP bans) are all
        # worth retrying with backoff instead of aborting the whole call —
        # a single blip shouldn't fail an order submission outright.
        retryable = (
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
            asyncio.TimeoutError,
            ExchangeRateLimitError,
            ExchangeConnectionError,
        )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(
                    multiplier=DEFAULT_RETRY_BASE_DELAY,
                    max=DEFAULT_RETRY_MAX_DELAY,
                ),
                retry=retry_if_exception_type(retryable),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=False,
            ):
                with attempt:
                    if self._rate_limiter:
                        await self._rate_limiter.acquire()
                    try:
                        return await self._execute_request(method, url, params=params, data=data)
                    except ExchangeRateLimitError as exc:
                        # Honor the exchange's requested backoff (429 Retry-After,
                        # or a longer fixed wait for 418 IP-ban responses) before
                        # tenacity's own exponential wait kicks in.
                        if exc.retry_after_seconds:
                            logger.warning(
                                "Rate limited on %s %s — sleeping %.1fs before retry",
                                method, path, exc.retry_after_seconds,
                            )
                            await asyncio.sleep(exc.retry_after_seconds)
                        raise
        except RetryError as exc:
            raise MaxRetriesExceededError(f"{method} {path}", self.max_retries) from exc

    async def _execute_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
    ) -> Any:
        """Execute a single HTTP request and parse the response."""
        if self._session is None:
            # Guards against a caller invoking a REST method before
            # connect() (or after disconnect()) — an assert here would be
            # silently stripped under `python -O`, turning a clear "you
            # forgot to connect" error into an opaque AttributeError deep
            # in the aiohttp call below.
            raise ExchangeConnectionError(
                "HTTP session not initialized — call connect() before making requests."
            )
        self._request_count += 1

        # For signed requests, sign_params() computes the HMAC-SHA256
        # signature over a query string built with
        # urllib.parse.urlencode(..., quote_via=urllib.parse.quote).
        # Passing the same `params` dict straight to
        # aiohttp.ClientSession.request(params=params, ...) lets aiohttp/yarl
        # build the actual wire query string independently — if yarl's
        # default percent-encoding ever differs from urllib.parse.quote for
        # any parameter value (e.g. a client_order_id or symbol containing a
        # character the two encoders treat differently), the string Binance
        # receives won't match the string that was signed and the request
        # fails with -1022 INVALID_SIGNATURE. Building the exact query
        # string ourselves and appending it to the URL (rather than handing
        # aiohttp a dict to re-encode) guarantees byte-for-byte parity with
        # what was signed.
        request_url = url
        if params:
            query = urllib.parse.urlencode(
                {k: str(v) for k, v in params.items()},
                quote_via=urllib.parse.quote,
            )
            request_url = f"{url}?{query}"

        logger.debug("HTTP %s %s params=%s", method, request_url, params)

        # api_requests/api_latency (trading_api_requests_total /
        # trading_api_latency_seconds) were declared in metrics.py but never
        # instrumented anywhere. Endpoint label uses the un-queried path
        # (base_url stripped off `url`) rather than `request_url`, which
        # would blow up cardinality with per-call query-string variations
        # (timestamps, signatures, order IDs). status_label defaults to
        # "error" and is only overwritten once a response is actually
        # received — connection-level failures (ServerTimeoutError,
        # ClientConnectionError below) never reach that point, so they
        # correctly report as "error" rather than a fabricated status code.
        endpoint = url[len(self.base_url):] if url.startswith(self.base_url) else url
        status_label = "error"
        request_start = time.monotonic()
        try:
            async with self._session.request(
                method,
                request_url,
                json=data,
            ) as response:
                status_label = str(response.status)
                if response.status in (429, 418):
                    # 429 = rate limit warning, 418 = IP auto-banned for
                    # continuing to violate limits. Both carry Retry-After;
                    # 418 bans are typically much longer than 429 backoffs.
                    default_wait = "1" if response.status == 429 else "120"
                    retry_after = float(response.headers.get("Retry-After", default_wait))
                    if response.status == 418:
                        logger.critical(
                            "Binance IP ban (418) on %s %s — backing off %.0fs",
                            method, url, retry_after,
                        )
                    raise ExchangeRateLimitError(retry_after)

                if response.status >= 500:
                    # Server-side failure — transient, safe to retry.
                    self._error_count += 1
                    text = await response.text()
                    raise ExchangeConnectionError(
                        f"Exchange server error {response.status} on {method} {url}: {text[:200]}"
                    )

                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    # Non-JSON body (HTML maintenance page, proxy error page,
                    # truncated response, etc). Treat as a connection-layer
                    # failure so it's retried rather than crashing the caller
                    # with a raw JSON decode error.
                    self._error_count += 1
                    raw = await response.text()
                    raise ExchangeConnectionError(
                        f"Non-JSON response ({response.status}) from {method} {url}: {raw[:200]}"
                    ) from exc

                # No `response.status != 200` gate here: Binance signals its
                # -1021 timestamp error with a non-200 status, but Bybit
                # signals its equivalent (retCode 10002) with HTTP 200 and
                # the error carried only in the body. Gating on status would
                # make this resync-and-retry path permanently unreachable
                # for Bybit; _is_clock_drift_error() already inspects the
                # body for the exact drift-indicating code, so it alone is
                # a sufficiently narrow condition for both adapters.
                if self._is_clock_drift_error(body):
                    old_offset = self._clock_offset_ms
                    await self.sync_clock()
                    logger.warning(
                        "Timestamp/recvWindow error on %s %s — resynced clock "
                        "(offset %dms -> %dms), retrying",
                        method, url, old_offset, self._clock_offset_ms,
                    )
                    raise ExchangeConnectionError(
                        f"Clock drift detected on {method} {url}; resynced and retrying"
                    )

                self._handle_response_error(response.status, body)
                return body
        except aiohttp.ServerTimeoutError as exc:
            self._error_count += 1
            raise ExchangeTimeoutError(url, self.timeout) from exc
        except aiohttp.ClientConnectionError as exc:
            self._error_count += 1
            raise ExchangeConnectionError(f"Connection error: {exc}") from exc
        finally:
            try:
                api_requests.labels(
                    method=method, endpoint=endpoint, status=status_label
                ).inc()
                api_latency.labels(endpoint=endpoint).observe(
                    time.monotonic() - request_start
                )
            except Exception:
                logger.exception("Failed to record API metrics for %s %s", method, endpoint)

    async def _get(self, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        return await self._request("GET", path, params=params, signed=signed)

    async def _post(self, path: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        return await self._request("POST", path, params=params, data=data, signed=signed)

    async def _delete(self, path: str, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        return await self._request("DELETE", path, params=params, signed=signed)

    async def _put(self, path: str, data: dict[str, Any] | None = None, params: dict[str, Any] | None = None, signed: bool = False) -> Any:
        return await self._request("PUT", path, params=params, data=data, signed=signed)

    # ─── Abstract interface ───────────────────────────────────────────────────

    @abstractmethod
    async def _sign_request(
        self,
        params: dict[str, Any],
        data: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Sign request parameters (HMAC, RSA, etc.)."""

    @abstractmethod
    def _handle_response_error(self, status: int, body: Any) -> None:
        """Raise exchange-specific exception for error responses."""

    def _is_clock_drift_error(self, body: Any) -> bool:
        """
        Return True if an error body indicates the local clock has drifted
        outside the exchange's accepted signing window.

        Default implementation recognizes none; exchange-specific subclasses
        should override this to check their own error codes (e.g. Binance's
        -1021 "Timestamp for this request is outside of the recvWindow").
        """
        return False

    @abstractmethod
    async def get_account_balance(self) -> dict[str, Decimal]:
        """Fetch current account balances."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Fetch all open positions."""

    @abstractmethod
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
        """Place an order on the exchange."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an order. Returns True if cancelled successfully."""

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> Order:
        """Fetch order state from exchange."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Fetch all open orders."""

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        start_time: datetime | None = None,
    ) -> list[OHLCV]:
        """Fetch historical OHLCV data."""

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """Fetch 24h ticker."""

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        """Fetch order book snapshot."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol."""

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        """Set margin mode for a symbol."""

    # ─── Status ───────────────────────────────────────────────────────────────

    @property
    def status(self) -> ExchangeStatus:
        return self._status

    @property
    def stats(self) -> dict[str, int]:
        return {
            "requests": self._request_count,
            "errors": self._error_count,
        }
