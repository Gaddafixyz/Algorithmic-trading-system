"""
Bybit V5 WebSocket stream client (USDT-perpetual, category="linear").

Subscribes to:
- Kline (candlestick) streams
- Public trade stream (tick data)
- Order book depth streams
- Ticker stream (mark price, funding rate)
- Private stream: order, execution (fills), position, wallet updates

Bybit V5 splits market data and account data across two separate
WebSocket endpoints (``/v5/public/linear`` and ``/v5/private``), unlike
Binance's single combined-stream endpoint. This client owns both
connections under one object so callers (``CandleService``,
``MarketDataFeed``, bootstrap wiring) can use it exactly like
``BinanceFuturesWebSocket``: same ``subscribe_*``/``start``/``stop``
surface. Outgoing messages are normalized into the same wire shape
Binance sends (``t``/``o``/``h``/``l``/``c``/``v``/``x`` for klines;
``p``/``q``/``m`` for trades; ``ORDER_TRADE_UPDATE`` with an ``o`` block
for the user-data stream) so existing downstream code
(``src/market_data/candles.py``, ``src/market_data/feeds.py``,
``src/execution/fill_engine.py``) works unmodified regardless of which
exchange is configured.

Bybit protocol notes (V5, docs verified 2026-05):
- Unlike Binance, Bybit does not ping the client — the *client* must
  send ``{"op": "ping"}`` roughly every 20s on each connection or the
  server closes the socket after ~60s of silence.
- The private connection requires an explicit ``auth`` op sent
  immediately after connecting; topic subscriptions sent before a
  successful auth response are rejected. Auth uses a short-lived
  (~10s) expiring signature, not the REST request-signing scheme.
- Public order-book depth is only available at fixed levels (1, 50,
  200, 500 for linear); the Binance-style ``speed_ms`` push-rate
  parameter has no Bybit equivalent (push rate is fixed per depth
  level) and is accepted only for signature parity with the Binance
  client — see ``subscribe_depth``.

Docs: https://bybit-exchange.github.io/docs/v5/ws/connect
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from src.core.constants import (
    DEFAULT_WS_MAX_RECONNECT_DELAY,
    DEFAULT_WS_PING_INTERVAL,
    DEFAULT_WS_RECONNECT_DELAY,
)
from src.core.enums import Timeframe
from src.exchange.adapters.bybit.parser import TIMEFRAME_TO_BYBIT
from src.monitoring.metrics import ws_messages_received, ws_reconnects

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# Bybit V5 supports these fixed order-book depth levels for linear
# perpetuals. subscribe_depth() snaps any requested depth to the
# nearest supported level (rounding up) rather than silently failing.
_SUPPORTED_DEPTH_LEVELS: tuple[int, ...] = (1, 50, 200, 500)

# How long the auth signature remains valid, in ms. Kept short per
# Bybit's guidance so a captured signature can't be replayed later.
_AUTH_EXPIRE_WINDOW_MS: int = 10_000

# Private-stream topics subscribed automatically once user-data
# handling is registered — mirrors what the Binance listen-key stream
# delivers in one combined feed.
_PRIVATE_TOPICS: tuple[str, ...] = ("order", "execution", "position", "wallet")


def _snap_depth(depth: int) -> int:
    """Round a requested depth up to the nearest level Bybit supports."""
    for level in _SUPPORTED_DEPTH_LEVELS:
        if depth <= level:
            return level
    return _SUPPORTED_DEPTH_LEVELS[-1]


def _sign_ws_auth(api_secret: str, expires_ms: int) -> str:
    """Bybit V5 WS auth signature: HMAC-SHA256 of ``GET/realtime{expires}``."""
    payload = f"GET/realtime{expires_ms}"
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BybitWebSocket:
    """
    WebSocket client for Bybit V5 linear-perpetual real-time data.

    Manages two independent connections (public market data, private
    account data) with automatic reconnection on each, and normalizes
    outgoing messages to the same shape ``BinanceFuturesWebSocket``
    produces so this is a drop-in replacement for any consumer of that
    class.
    """

    #: See BinanceFuturesWebSocket.provider — same purpose, other value.
    provider: str = "bybit"

    def __init__(
        self,
        ws_public_url: str,
        ws_private_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        ping_interval: float = DEFAULT_WS_PING_INTERVAL,
        reconnect_delay: float = DEFAULT_WS_RECONNECT_DELAY,
        max_reconnect_delay: float = DEFAULT_WS_MAX_RECONNECT_DELAY,
    ) -> None:
        self._ws_public_url = ws_public_url.rstrip("/")
        self._ws_private_url = ws_private_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._ping_interval = ping_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._subscriptions: dict[str, MessageHandler] = {}
        self._user_data_handler: MessageHandler | None = None

        self._public_ws: websockets.WebSocketClientProtocol | None = None
        self._private_ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_count = 0
        self._message_count = 0

        self._public_task: asyncio.Task[None] | None = None
        self._private_task: asyncio.Task[None] | None = None
        self._public_ping_task: asyncio.Task[None] | None = None
        self._private_ping_task: asyncio.Task[None] | None = None
        # See BinanceFuturesWebSocket.set_reconnect_callback — same purpose.
        # Only relevant to the public connection since kline/tick data
        # (the only streams a resync would need to backfill) is public-only.
        self._reconnect_callback: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._ever_connected_public = False

    # ─── Subscription management ──────────────────────────────────────────────

    def subscribe_kline(
        self,
        symbol: str,
        timeframe: Timeframe,
        handler: MessageHandler,
    ) -> str:
        """Subscribe to candlestick stream."""
        interval = TIMEFRAME_TO_BYBIT.get(timeframe.value)
        if interval is None:
            raise ValueError(f"Timeframe {timeframe.value} has no Bybit kline interval")
        topic = f"kline.{interval}.{symbol.upper()}"
        self._subscriptions[topic] = handler
        logger.debug("Subscribed to kline topic: %s", topic)
        return topic

    def subscribe_agg_trade(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to the public trade stream (tick data)."""
        topic = f"publicTrade.{symbol.upper()}"
        self._subscriptions[topic] = handler
        return topic

    def subscribe_book_ticker(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to best bid/ask updates (depth-1 order book as a proxy —
        Bybit has no dedicated bookTicker topic)."""
        topic = f"orderbook.1.{symbol.upper()}"
        self._subscriptions[topic] = handler
        return topic

    def subscribe_depth(
        self,
        symbol: str,
        depth: int,
        speed_ms: int,
        handler: MessageHandler,
    ) -> str:
        """
        Subscribe to order book depth stream.

        ``speed_ms`` is accepted for signature parity with the Binance
        client but has no effect: Bybit's push frequency is fixed per
        depth level, not independently configurable. ``depth`` is
        snapped up to the nearest level Bybit actually supports
        (1/50/200/500).
        """
        level = _snap_depth(depth)
        topic = f"orderbook.{level}.{symbol.upper()}"
        self._subscriptions[topic] = handler
        return topic

    def subscribe_mark_price(self, symbol: str, handler: MessageHandler) -> str:
        """Subscribe to mark price / funding rate updates (Bybit's tickers topic)."""
        topic = f"tickers.{symbol.upper()}"
        self._subscriptions[topic] = handler
        return topic

    def subscribe_user_data(self, handler: MessageHandler) -> None:
        """
        Register the handler for private account events (order updates,
        fills, position, wallet), delivered over the authenticated
        private connection. This is the only path by which exchange
        fill notifications reach ``FillEngine.process_execution_report``
        — messages are normalized to the Binance ``ORDER_TRADE_UPDATE``
        shape that method expects.
        """
        self._user_data_handler = handler

    def unsubscribe(self, topic: str) -> None:
        """Remove a public-stream subscription."""
        self._subscriptions.pop(topic, None)

    def unsubscribe_all(self) -> None:
        """Remove all public-stream subscriptions."""
        self._subscriptions.clear()

    # ─── Connection lifecycle ─────────────────────────────────────────────────

    def set_reconnect_callback(
        self,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """
        Register an async callable invoked after each successful *public*
        reconnect (not the client's first-ever connection). See
        BinanceFuturesWebSocket.set_reconnect_callback for the full
        rationale — same contract here. Only the public connection fires
        this since kline/tick data (what a resync backfills) is public-only;
        the private connection's account-state gap is instead covered by
        ReconciliationService's periodic REST poll.
        """
        self._reconnect_callback = callback

    async def start(self) -> None:
        """Start the public stream, and the private stream if credentials
        and a user-data handler are both present."""
        self._running = True
        self._public_task = asyncio.create_task(
            self._public_run_loop(), name="bybit_ws.public"
        )
        if self._user_data_handler is not None:
            if not self._api_key or not self._api_secret:
                logger.warning(
                    "subscribe_user_data() was called but no Bybit API "
                    "credentials are set — private stream will not start"
                )
            else:
                self._private_task = asyncio.create_task(
                    self._private_run_loop(), name="bybit_ws.private"
                )
        logger.info(
            "Bybit WebSocket client started (public_topics=%d, private=%s)",
            len(self._subscriptions),
            self._private_task is not None,
        )

    async def stop(self) -> None:
        """Stop both connections and cancel all background tasks."""
        self._running = False
        for ws in (self._public_ws, self._private_ws):
            if ws is not None:
                await ws.close()
        for task in (
            self._public_ping_task,
            self._private_ping_task,
            self._public_task,
            self._private_task,
        ):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("Bybit WebSocket client stopped")

    # ─── Public connection ─────────────────────────────────────────────────────

    async def _public_run_loop(self) -> None:
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect_public()
                delay = self._reconnect_delay
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._reconnect_count += 1
                ws_reconnects.inc()
                logger.warning(
                    "Bybit public WebSocket disconnected (reconnect #%d): %s. "
                    "Retrying in %.1fs",
                    self._reconnect_count, exc, delay,
                )
                if not self._running:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _connect_public(self) -> None:
        if not self._subscriptions:
            await asyncio.sleep(1)
            return

        async with websockets.connect(
            self._ws_public_url, max_size=10 * 1024 * 1024
        ) as ws:
            self._public_ws = ws
            logger.info("Bybit public WebSocket connected: %s", self._ws_public_url)

            if self._ever_connected_public and self._reconnect_callback is not None:
                try:
                    await self._reconnect_callback()
                except Exception as exc:
                    logger.error("Reconnect callback failed: %s", exc)
            self._ever_connected_public = True

            topics = list(self._subscriptions.keys())
            await ws.send(json.dumps({"op": "subscribe", "args": topics}))

            self._public_ping_task = asyncio.create_task(
                self._ping_loop(ws), name="bybit_ws.public_ping"
            )
            try:
                async for raw_message in ws:
                    if not self._running:
                        return
                    self._message_count += 1
                    try:
                        await self._dispatch_public(raw_message)
                    except Exception as exc:
                        logger.exception("Error dispatching Bybit public message: %s", exc)
            finally:
                self._public_ping_task.cancel()
                try:
                    await self._public_ping_task
                except (asyncio.CancelledError, ConnectionClosed):
                    pass

    async def _dispatch_public(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON Bybit WebSocket message: %r", raw[:200])
            return

        # Ack/pong control frames — nothing to route.
        if "op" in message and "topic" not in message:
            if message.get("op") == "subscribe" and not message.get("success", True):
                logger.error("Bybit subscribe rejected: %s", message.get("ret_msg"))
            return

        topic = message.get("topic", "")
        ws_messages_received.labels(stream=topic).inc()
        data = message.get("data", [])
        handler = self._subscriptions.get(topic)
        if handler is None:
            return

        if topic.startswith("kline."):
            for row in data:
                await handler(self._normalize_kline(row))
        elif topic.startswith("publicTrade."):
            for trade in data:
                await handler(self._normalize_trade(trade))
        elif topic.startswith("tickers."):
            await handler(self._normalize_ticker(data if isinstance(data, dict) else {}))
        elif topic.startswith("orderbook.1."):
            # Depth-1 only: per Bybit's docs, linear/inverse/spot level-1
            # orderbook data is snapshot-only (never delta) — every push
            # carries the full current best bid/ask, so no local book
            # needs to be maintained here the way a real depth>1 consumer
            # would need to. This is exactly what subscribe_book_ticker()
            # subscribes to; a caller using subscribe_depth() with a
            # higher level still lands in the raw pass-through branch
            # below since its topic won't match "orderbook.1.".
            await handler(self._normalize_book_ticker(
                data if isinstance(data, dict) else {}
            ))
        else:
            # orderbook.{50,200,500}.* and anything else: pass through
            # as-is, wrapped so callers can access both the payload and
            # update type (snapshot vs delta) — a genuine multi-level
            # depth consumer needs the delta semantics preserved.
            await handler({"data": data, "type": message.get("type", "")})

    @staticmethod
    def _normalize_kline(row: dict[str, Any]) -> dict[str, Any]:
        """Bybit kline row -> Binance-shaped kline dict (t/o/h/l/c/v/x)."""
        return {
            "t": int(row.get("start", 0)),
            "o": row.get("open", "0"),
            "h": row.get("high", "0"),
            "l": row.get("low", "0"),
            "c": row.get("close", "0"),
            "v": row.get("volume", "0"),
            "x": bool(row.get("confirm", False)),
        }

    @staticmethod
    def _normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
        """
        Bybit publicTrade entry -> Binance-shaped aggTrade dict (p/q/m).

        Bybit's ``S`` is the *taker's* side. Binance's ``m`` means "buyer
        was the maker" (the resting order was a buy), which is true
        exactly when the taker's side was Sell.
        """
        return {
            "p": trade.get("p", "0"),
            "q": trade.get("v", "0"),
            "m": trade.get("S") == "Sell",
        }

    @staticmethod
    def _normalize_ticker(ticker: dict[str, Any]) -> dict[str, Any]:
        """
        Bybit ticker snapshot/delta -> Binance-shaped markPriceUpdate dict.

        Bybit's public "tickers" topic sends a full snapshot on subscribe
        and delta messages afterward that only carry changed fields —
        indexPrice/fundingRate/nextFundingTime may legitimately be absent
        from a given delta (they change far less often than markPrice), so
        callers must treat missing keys as "unchanged", not "zero". We
        only include a key here when Bybit actually sent it, matching
        MarketDataFeed._make_mark_price_handler's use of msg.get(...) with
        None-safe fallback for i/r/T.
        """
        out: dict[str, Any] = {"p": ticker.get("markPrice", ticker.get("lastPrice", "0"))}
        if "indexPrice" in ticker:
            out["i"] = ticker["indexPrice"]
        if "fundingRate" in ticker:
            out["r"] = ticker["fundingRate"]
        if "nextFundingTime" in ticker:
            out["T"] = ticker["nextFundingTime"]
        return out

    @staticmethod
    def _normalize_book_ticker(data: dict[str, Any]) -> dict[str, Any]:
        """
        Bybit ``orderbook.1`` snapshot -> Binance-shaped bookTicker dict
        (``b``/``B``/``a``/``A``).

        Depth-1 pushes are always full snapshots (never deltas — see the
        routing comment in ``_dispatch_public``), so ``data["b"]``/
        ``data["a"]`` each contain exactly one ``[price, qty]`` level and
        can be read directly with no local book state to maintain. A side
        with a temporarily empty book (e.g. right after subscribing,
        before Bybit has pushed a level) is simply omitted from the
        output — mirrors ``_normalize_ticker``'s "missing key means
        unknown, not zero" convention so a caller reading a half-populated
        message doesn't mistake an absent price for a real one at 0.
        """
        out: dict[str, Any] = {"s": data.get("s", "")}
        bids = data.get("b") or []
        asks = data.get("a") or []
        if bids:
            out["b"], out["B"] = bids[0][0], bids[0][1]
        if asks:
            out["a"], out["A"] = asks[0][0], asks[0][1]
        return out

    # ─── Private connection ────────────────────────────────────────────────────

    async def _private_run_loop(self) -> None:
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect_private()
                delay = self._reconnect_delay
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._reconnect_count += 1
                ws_reconnects.inc()
                logger.warning(
                    "Bybit private WebSocket disconnected (reconnect #%d): %s. "
                    "Retrying in %.1fs",
                    self._reconnect_count, exc, delay,
                )
                if not self._running:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def _connect_private(self) -> None:
        if not self._api_key or not self._api_secret:
            # Invariant enforced by start() (private task is only ever
            # scheduled when both are set) — checked explicitly rather than
            # via assert so the invariant still holds under `python -O`.
            raise RuntimeError(
                "_connect_private called without api_key/api_secret set"
            )

        async with websockets.connect(
            self._ws_private_url, max_size=10 * 1024 * 1024
        ) as ws:
            self._private_ws = ws
            logger.info("Bybit private WebSocket connected: %s", self._ws_private_url)

            expires = int(time.time() * 1000) + _AUTH_EXPIRE_WINDOW_MS
            signature = _sign_ws_auth(self._api_secret, expires)
            await ws.send(json.dumps({
                "op": "auth",
                "args": [self._api_key, expires, signature],
            }))

            # First frame must be the auth ack. Subscribing before a
            # successful ack is rejected by Bybit, so we block on it here
            # rather than racing it against the general dispatch loop.
            ack_raw = await ws.recv()
            ack = json.loads(ack_raw)
            if ack.get("op") == "auth" and not ack.get("success", False):
                raise ConnectionError(f"Bybit WS auth failed: {ack.get('ret_msg')}")
            logger.info("Bybit private WebSocket authenticated")

            await ws.send(json.dumps({"op": "subscribe", "args": list(_PRIVATE_TOPICS)}))

            self._private_ping_task = asyncio.create_task(
                self._ping_loop(ws), name="bybit_ws.private_ping"
            )
            try:
                async for raw_message in ws:
                    if not self._running:
                        return
                    self._message_count += 1
                    try:
                        await self._dispatch_private(raw_message)
                    except Exception as exc:
                        logger.exception("Error dispatching Bybit private message: %s", exc)
            finally:
                self._private_ping_task.cancel()
                try:
                    await self._private_ping_task
                except (asyncio.CancelledError, ConnectionClosed):
                    pass

    async def _dispatch_private(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Received non-JSON Bybit private message: %r", raw[:200])
            return

        topic = message.get("topic", "")
        if not topic:
            return  # ack/pong control frame

        # Labeled "user_data" rather than the raw topic name for
        # consistency with the Binance client's user-data-stream labeling
        # (see BinanceFuturesWebSocket._dispatch) — both ultimately feed
        # FillEngine via the same normalized ORDER_TRADE_UPDATE shape, so a
        # dashboard querying this metric across exchanges gets a single
        # comparable series rather than exchange-specific topic names.
        ws_messages_received.labels(stream="user_data").inc()

        if self._user_data_handler is None:
            return

        data = message.get("data", [])
        if topic == "execution":
            for exec_report in data:
                await self._user_data_handler(self._normalize_execution(exec_report))
        elif topic == "order":
            for order_update in data:
                await self._user_data_handler(self._normalize_order_status(order_update))
        # "position" and "wallet" topics: reconciliation already covers
        # position/equity sync via REST polling, so no handler is wired
        # here yet. Nothing consumes them today; extend when needed.

    @staticmethod
    def _normalize_execution(exec_report: dict[str, Any]) -> dict[str, Any]:
        """
        Bybit ``execution`` topic entry -> Binance ORDER_TRADE_UPDATE shape.

        This is the message ``FillEngine.process_execution_report``
        consumes, so field names must match what it reads: c/l/L/n/N/m
        inside an "o" block, keyed off event type "ORDER_TRADE_UPDATE".

        Critically, ``FillEngine`` also gates on an "x" execution-type
        field, only processing entries where ``o["x"] == "TRADE"`` (this
        mirrors Binance, where the same ORDER_TRADE_UPDATE event type
        covers NEW/CANCELED/TRADE and non-TRADE variants echo stale
        l/L fields that must not be double-counted as fills). Bybit's V5
        execution topic carries the equivalent discriminator as
        ``execType`` (``"Trade"`` for a genuine fill; ``"Funding"``,
        ``"AdlTrade"``, ``"BustTrade"``, ``"Delivery"``, ``"BlockTrade"``,
        ``"MovePosition"`` for everything else). Without mapping it here,
        "x" was never set on the output, so ``order_data.get("x", "")``
        always evaluated to "" != "TRADE" and every real Bybit fill was
        silently dropped — orders would submit and sit OPEN forever with
        protective SL/TP never placed. Absent ``execType`` (shouldn't
        happen on the live stream, but keeps this safe against a partial
        payload) defaults to "Trade" since the ``execution`` topic only
        ever carries genuine execution entries.
        """
        exec_type = str(exec_report.get("execType", "Trade"))
        return {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "c": exec_report.get("orderLinkId", ""),
                "l": exec_report.get("execQty", "0"),
                "L": exec_report.get("execPrice", "0"),
                "n": exec_report.get("execFee", "0"),
                "N": exec_report.get("feeCurrency", "USDT"),
                "m": bool(exec_report.get("isMaker", False)),
                "x": "TRADE" if exec_type == "Trade" else exec_type,
            },
        }

    @staticmethod
    def _normalize_order_status(order_update: dict[str, Any]) -> dict[str, Any]:
        """
        Bybit ``order`` topic entry -> Binance ORDER_TRADE_UPDATE shape.

        Status-only updates (no fill) carry zero qty/price so
        ``FillEngine`` no-ops on them; still normalized to the same
        envelope in case future handlers want order-status transitions
        (New/Cancelled/Rejected) that ``execution`` doesn't carry.
        """
        return {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "c": order_update.get("orderLinkId", ""),
                "l": "0",
                "L": "0",
                "n": "0",
                "N": "USDT",
                "m": False,
                "X": order_update.get("orderStatus", ""),
            },
        }

    # ─── Shared ─────────────────────────────────────────────────────────────────

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Bybit does not send server-initiated pings on V5 — the client
        must send ``{"op": "ping"}`` itself roughly every 20s or the
        server closes the connection after ~60s of silence.
        """
        try:
            while self._running:
                await asyncio.sleep(self._ping_interval)
                await ws.send(json.dumps({"op": "ping"}))
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    @property
    def stats(self) -> dict[str, int]:
        return {
            "subscriptions": len(self._subscriptions),
            "messages": self._message_count,
            "reconnects": self._reconnect_count,
        }
