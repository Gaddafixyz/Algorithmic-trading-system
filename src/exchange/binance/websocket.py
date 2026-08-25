"""
Binance Futures WebSocket stream client.

Subscribes to:
- Candlestick (kline) streams
- Individual symbol book tickers
- Aggregate trade streams
- User data stream (orders, positions, balance updates)

Handles reconnection with exponential backoff automatically.

Binance protocol notes (2025-01-28 / 2026-05-08):
- Server sends a ping frame every 20 s; clients must pong within 60 s
  (websockets library does this automatically).
- Server sends a ``serverShutdown`` JSON event before disconnecting for
  maintenance.  On receipt we close the connection cleanly so _run_loop
  reconnects immediately without backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from src.core.constants import (
    BINANCE_LISTEN_KEY_REFRESH_MINUTES,
    DEFAULT_WS_MAX_RECONNECT_DELAY,
    DEFAULT_WS_PING_INTERVAL,
    DEFAULT_WS_RECONNECT_DELAY,
)
from src.core.enums import Timeframe
from src.monitoring.metrics import ws_messages_received, ws_reconnects

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# Timeout for our own keepalive pings (seconds).
# Binance expects pong within 60 s for its server-initiated pings; the
# websockets library responds automatically, so 30 s for our own pings is safe.
_PING_TIMEOUT_SECONDS: float = 30.0


class BinanceFuturesWebSocket:
    """
    WebSocket client for Binance Futures real-time data.

    Manages subscription lifecycle and automatic reconnection.
    All message handling is delegated to registered callbacks.
    """

    #: Identifies the exchange to provider-agnostic consumers (e.g.
    #: MarketDataFeed/CandleService) that need to label events or branch
    #: on which exchange produced them without importing this concrete
    #: class. See BybitWebSocket.provider for the counterpart.
    provider: str = "binance"

    def __init__(
        self,
        ws_base_url: str,
        listen_key: str | None = None,
        ping_interval: float = DEFAULT_WS_PING_INTERVAL,
        reconnect_delay: float = DEFAULT_WS_RECONNECT_DELAY,
        max_reconnect_delay: float = DEFAULT_WS_MAX_RECONNECT_DELAY,
    ) -> None:
        self._ws_base_url = ws_base_url.rstrip("/")
        self._listen_key = listen_key
        self._ping_interval = ping_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._subscriptions: dict[str, MessageHandler] = {}
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_count = 0
        self._message_count = 0
        self._stream_task: asyncio.Task[None] | None = None
        self._renewal_task: asyncio.Task[None] | None = None
        # Optional callback invoked by the renewal loop to obtain a fresh key
        # from the REST layer and update self._listen_key in place.
        self._renewal_callback: Callable[[], Coroutine[Any, Any, str]] | None = None
        # Handler for the user-data stream (order/account/position updates).
        # Kept separate from self._subscriptions (rather than keyed by the
        # listen-key string) because the listen key can rotate during
        # renewal — dispatch always checks self._listen_key by identity, so
        # the handler stays wired across rotations without needing to be
        # re-registered under a new dict key.
        self._user_data_handler: MessageHandler | None = None
        # Optional async callback invoked after a successful *reconnect*
        # (i.e. not the very first connection) so consumers like
        # CandleService can backfill any candles/ticks missed while the
        # socket was down. See set_reconnect_callback().
        self._reconnect_callback: Callable[[], Coroutine[Any, Any, None]] | None = None
        # True once the first connection of this client's lifetime has
        # succeeded — distinguishes "initial connect" (candle history is
        # already warmed via REST in CandleService.initialize()) from a
        # genuine reconnect after a drop, which is when a gap can exist.
        self._ever_connected = False

    # ─── Subscription management ──────────────────────────────────────────────

    def subscribe_kline(
        self,
        symbol: str,
        timeframe: Timeframe,
        handler: MessageHandler,
    ) -> str:
        """Subscribe to candlestick stream."""
        stream = f"{symbol.lower()}@kline_{timeframe.value}"
        self._subscriptions[stream] = handler
        logger.debug("Subscribed to kline stream: %s", stream)
        return stream

    def subscribe_agg_trade(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to aggregated trade stream."""
        stream = f"{symbol.lower()}@aggTrade"
        self._subscriptions[stream] = handler
        return stream

    def subscribe_book_ticker(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to best bid/ask stream."""
        stream = f"{symbol.lower()}@bookTicker"
        self._subscriptions[stream] = handler
        return stream

    def subscribe_depth(
        self,
        symbol: str,
        depth: int,
        speed_ms: int,
        handler: MessageHandler,
    ) -> str:
        """Subscribe to partial order book depth stream."""
        stream = f"{symbol.lower()}@depth{depth}@{speed_ms}ms"
        self._subscriptions[stream] = handler
        return stream

    def subscribe_mark_price(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to mark price and funding rate updates."""
        stream = f"{symbol.lower()}@markPrice"
        self._subscriptions[stream] = handler
        return stream

    def subscribe_user_data(self, handler: MessageHandler) -> None:
        """
        Register the handler for the user-data stream (order updates,
        account updates, position updates) delivered over the listen-key
        stream. This is the only path by which exchange fill notifications
        ever reach the application — without it, nothing ever turns
        exchange fill data into an OrderFilledEvent.
        """
        self._user_data_handler = handler

    def unsubscribe(self, stream: str) -> None:
        """Remove a subscription."""
        self._subscriptions.pop(stream, None)

    def unsubscribe_all(self) -> None:
        """Remove all subscriptions."""
        self._subscriptions.clear()

    # ─── Connection lifecycle ─────────────────────────────────────────────────

    def set_renewal_callback(
        self,
        callback: Callable[[], Coroutine[Any, Any, str]],
    ) -> None:
        """
        Register an async callable that returns a fresh listen key.

        When set and a listen key is present, the WebSocket client will call
        this every ``BINANCE_LISTEN_KEY_REFRESH_MINUTES`` minutes so the
        user-data stream never expires during unattended operation.

        Typical usage::

            async def renew() -> str:
                await rest_client.renew_listen_key(current_key)
                return current_key  # key is renewed in-place on Binance

            ws.set_renewal_callback(renew)
        """
        self._renewal_callback = callback

    def set_reconnect_callback(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Register an async callable invoked after each successful reconnect.

        Fired once the socket is back up following a drop — not on the
        client's first-ever connection, since startup already warms candle
        history via REST (CandleService.initialize()). Intended for a
        consumer like CandleService.resync_after_reconnect() to backfill
        candles that closed while the socket was down, which the live
        kline stream alone can never recover (it only ever pushes candles
        going forward from when the new connection opens).

        Exceptions raised by the callback are logged and swallowed by the
        caller (_connect_and_listen) — a failed backfill must never take
        down the reconnection loop itself.
        """
        self._reconnect_callback = callback

    async def start(self) -> None:
        """Start the WebSocket connection and listening loop."""
        self._running = True
        self._stream_task = asyncio.create_task(self._run_loop(), name="ws.stream")
        # Start listen-key renewal if a key and callback are registered
        if self._listen_key and self._renewal_callback is not None:
            self._renewal_task = asyncio.create_task(
                self._renewal_loop(), name="ws.listen_key_renewal"
            )
            logger.info(
                "Listen-key renewal task started (interval=%d min)",
                BINANCE_LISTEN_KEY_REFRESH_MINUTES,
            )
        logger.info("WebSocket client started with %d subscriptions", len(self._subscriptions))

    async def stop(self) -> None:
        """Stop the client and close the WebSocket."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket client stopped")

    async def _renewal_loop(self) -> None:
        """
        Background task: renew the Binance listen key periodically.

        Binance invalidates a user-data-stream listen key after 60 minutes of
        inactivity.  This loop keeps it alive by calling the registered
        ``_renewal_callback`` every ``BINANCE_LISTEN_KEY_REFRESH_MINUTES``
        minutes (default 30).  On success the returned key replaces
        ``self._listen_key`` so reconnection after a drop uses the fresh key.
        """
        interval = BINANCE_LISTEN_KEY_REFRESH_MINUTES * 60  # seconds
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                if self._renewal_callback is None:
                    # Invariant enforced by start() (this loop is only ever
                    # scheduled when a callback is registered) — checked
                    # explicitly so the invariant still holds under
                    # `python -O`, where a bare assert would be stripped.
                    raise RuntimeError("_renewal_loop running without a renewal callback set")
                new_key = await self._renewal_callback()
                if new_key and new_key != self._listen_key:
                    logger.info(
                        "Listen key rotated (old prefix=%s... new prefix=%s...)",
                        (self._listen_key or "")[:8],
                        new_key[:8],
                    )
                    self._listen_key = new_key
                else:
                    logger.debug(
                        "Listen key renewed in-place (prefix=%s...)",
                        (self._listen_key or "")[:8],
                    )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Failed to renew listen key: %s — will retry next cycle", exc)

    async def _run_loop(self) -> None:
        """Main reconnection loop."""
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect_and_listen()
                # Normal return: either _running went False, or a serverShutdown
                # triggered a clean close.  Either way reset the backoff delay
                # so the next connection starts fresh.
                delay = self._reconnect_delay
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._reconnect_count += 1
                ws_reconnects.inc()
                logger.warning(
                    "WebSocket disconnected (reconnect #%d): %s. Retrying in %.1fs",
                    self._reconnect_count, exc, delay,
                )
                if not self._running:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _connect_and_listen(self) -> None:
        """Connect to the combined stream endpoint and dispatch messages."""
        streams = list(self._subscriptions.keys())
        if not streams and not self._listen_key:
            await asyncio.sleep(1)
            return

        # Build stream URL. When both market-data streams and a listen key
        # are present, include the listen key directly in the combined
        # stream path (like any other stream) instead of relying on a
        # post-connect SUBSCRIBE message — that message is not guaranteed
        # to succeed, and even if it is, _dispatch previously had no way to
        # route a wrapped {"stream": "<listenKey>", "data": {...}} message
        # to a handler (nothing was ever inserted into self._subscriptions
        # under the listen-key stream name), so it would be silently
        # dropped. Building the URL from all streams (including the raw,
        # case-sensitive listen key) up front avoids both problems.
        all_streams = list(streams)
        if self._listen_key and self._listen_key not in all_streams:
            all_streams.append(self._listen_key)

        if all_streams:
            combined = "/".join(all_streams)
            url = f"{self._ws_base_url}/stream?streams={combined}"
        else:
            url = f"{self._ws_base_url}/ws/{self._listen_key}"

        logger.info("Connecting to WebSocket: %s", url[:80] + "..." if len(url) > 80 else url)

        async with websockets.connect(
            url,
            ping_interval=self._ping_interval,
            # 30 s timeout for our own keepalive pings.  Binance's server-side
            # pings (every 20 s) are handled automatically by the websockets
            # library; the server allows 60 s before disconnecting.
            ping_timeout=_PING_TIMEOUT_SECONDS,
            max_size=10 * 1024 * 1024,  # 10 MB
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connected")

            if self._ever_connected and self._reconnect_callback is not None:
                # Reconnect (not the first connection) — the live kline
                # stream only pushes candles going forward from here, so
                # any candles that closed while we were down are gone
                # unless backfilled via REST now.
                try:
                    await self._reconnect_callback()
                except Exception as exc:
                    logger.error("Reconnect callback failed: %s", exc)
            self._ever_connected = True

            async for raw_message in ws:
                if not self._running:
                    return
                self._message_count += 1
                try:
                    shutdown = await self._dispatch(raw_message)
                except Exception as exc:
                    logger.exception("Error dispatching WebSocket message: %s", exc)
                    continue
                if shutdown:
                    # serverShutdown received: close cleanly so _run_loop
                    # reconnects immediately (no backoff, normal return path).
                    logger.warning(
                        "serverShutdown event received — closing and reconnecting"
                    )
                    await ws.close()
                    return

    async def _dispatch(self, raw: str | bytes) -> bool:
        """
        Route a raw WebSocket message to the appropriate handler.

        Returns True if the message is a ``serverShutdown`` event and the
        caller should close and reconnect.
        """
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON WebSocket message: %r", raw[:200])
            return False

        # ── serverShutdown (Binance 2026-05-08) ──────────────────────────────
        # Binance sends this JSON text frame when the server is about to shut
        # down for maintenance.  The caller must disconnect and open a new
        # connection immediately.
        if message.get("e") == "serverShutdown":
            return True

        # Combined stream wraps data in {"stream": "...", "data": {...}}
        if "stream" in message:
            stream_name = message["stream"]
            data = message.get("data", message)
        else:
            # Single stream message
            stream_name = self._infer_stream(message)
            data = message

        # ws_messages_received (trading_ws_messages_total) was declared in
        # metrics.py but never incremented. Labeled on the resolved stream
        # name for market-data streams; the user-data (listen-key) stream
        # is labeled "user_data" rather than the raw listen key itself,
        # since listen keys rotate/renew over the process lifetime and
        # would otherwise create unbounded label cardinality.
        is_user_data = bool(self._listen_key and stream_name == self._listen_key)
        ws_messages_received.labels(stream="user_data" if is_user_data else stream_name).inc()

        # User-data stream events arrive keyed by the (case-sensitive)
        # listen key rather than a lowercase market-data stream name.
        if is_user_data:
            if self._user_data_handler is not None:
                await self._user_data_handler(data)
            return False

        handler = self._subscriptions.get(stream_name)
        if handler:
            await handler(data)

        return False

    @staticmethod
    def _infer_stream(message: dict[str, Any]) -> str:
        """Infer stream name from message type for single-stream connections."""
        e = message.get("e", "")
        s = message.get("s", "").lower()
        if e == "kline":
            k = message.get("k", {})
            return f"{s}@kline_{k.get('i', '')}"
        if e == "aggTrade":
            return f"{s}@aggTrade"
        if e == "bookTicker":
            return f"{s}@bookTicker"
        if e == "markPriceUpdate":
            return f"{s}@markPrice"
        return ""

    @property
    def stats(self) -> dict[str, int]:
        return {
            "subscriptions": len(self._subscriptions),
            "messages": self._message_count,
            "reconnects": self._reconnect_count,
        }
