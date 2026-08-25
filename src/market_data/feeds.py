"""
Market data feed orchestrator.

Central point for subscribing to and consuming market data.
Bridges exchange streams to the internal event bus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.core.enums import Timeframe
from src.core.events import (
    CandleClosedEvent,
    FundingRateEvent,
    MarkPriceUpdatedEvent,
    OrderBookUpdatedEvent,
    TickReceivedEvent,
    get_event_bus,
)
from src.core.interfaces import IWebSocketStreamClient
from src.core.models import OHLCV
from src.market_data.candles import CandleService
from src.market_data.cache import MarketDataCache

logger = logging.getLogger(__name__)


class MarketDataFeed:
    """
    Subscribes to live exchange data and publishes events to the event bus.

    Strategies consume market data via the event bus rather than
    calling this service directly.
    """

    def __init__(
        self,
        ws_client: IWebSocketStreamClient,
        candle_service: CandleService,
        cache: MarketDataCache,
    ) -> None:
        self._ws = ws_client
        self._candles = candle_service
        self._cache = cache
        self._bus = get_event_bus()
        self._subscribed_symbols: set[str] = set()
        # Labels published events with the exchange that actually produced
        # them (see IWebSocketStreamClient.provider) instead of assuming
        # Binance — this feed is provider-agnostic and runs unmodified
        # against BybitWebSocket too. isinstance-checked (not a bare
        # getattr) because BaseEvent.source is a strict Pydantic str
        # field: a test double or future client that doesn't set
        # `provider` must fall back to "unknown" rather than pass through
        # a non-str value and blow up event construction.
        provider = getattr(ws_client, "provider", None)
        self._source = provider if isinstance(provider, str) else "unknown"
        # Register the candle-gap backfill hook once, the first time this
        # feed is asked to subscribe to anything — the WS client fires this
        # after every reconnect (not the first connect), letting
        # CandleService pull whatever candles closed via REST while the
        # socket was down. Guarded by a flag since ws_client is shared
        # across all subscribe() calls but the callback only needs setting
        # once.
        self._registered_reconnect_hook = False
        # Per-symbol last-seen funding rate/next-funding-time, used to
        # dedup FundingRateEvent publication in _make_mark_price_handler
        # (see its docstring for why this can't just fire on every
        # mark-price tick).
        self._last_funding_rate: dict[str, Decimal] = {}
        self._last_next_funding_time: dict[str, datetime] = {}

    async def subscribe(
        self,
        symbol: str,
        timeframes: list[Timeframe],
        *,
        subscribe_ticks: bool = True,
        subscribe_mark_price: bool = True,
        subscribe_book_ticker: bool = True,
    ) -> None:
        """
        Subscribe to all required streams for a symbol.

        Warm-up candle history first, then attach live streams.

        subscribe_mark_price defaults to True: without it, nothing ever
        delivers the exchange's real mark price to consumers (ExecutionEngine,
        RiskManager) — paper trading and backtesting can get away with
        approximating mark price from candle closes, but in live trading
        that proxy can diverge materially from the exchange's actual
        margin/liquidation price during fast moves.

        subscribe_book_ticker defaults to True: the best-bid/best-ask
        spread it delivers is the input a future execution-quality guard
        (e.g. rejecting entries into an abnormally wide spread) needs;
        without a live subscriber nothing ever publishes
        OrderBookUpdatedEvent even though the event type has existed
        since 1.0.0.
        """
        symbol = symbol.upper()
        self._subscribed_symbols.add(symbol)

        if not self._registered_reconnect_hook:
            if hasattr(self._ws, "set_reconnect_callback"):
                self._ws.set_reconnect_callback(self._candles.resync_after_reconnect)
            self._registered_reconnect_hook = True

        # Warm up candle cache for each timeframe
        for tf in timeframes:
            await self._candles.initialize(symbol, tf)

        # Subscribe to live candle streams
        for tf in timeframes:
            await self._candles.subscribe_live(
                symbol,
                tf,
                callback=self._make_candle_callback(symbol, tf),
            )

        # Subscribe to tick stream
        if subscribe_ticks:
            async def _on_tick(msg: dict[str, Any]) -> None:
                price = Decimal(str(msg.get("p", "0")))
                qty = Decimal(str(msg.get("q", "0")))
                is_maker = bool(msg.get("m", False))
                await self._bus.publish(
                    TickReceivedEvent(
                        source=self._source,
                        symbol=symbol,
                        price=price,
                        quantity=qty,
                        is_buyer_maker=is_maker,
                    )
                )

            self._ws.subscribe_agg_trade(symbol, _on_tick)

        # Subscribe to the dedicated mark-price stream. Both providers push
        # this every ~second regardless of trade activity, and it carries
        # the exchange-computed mark price (plus index price and funding
        # rate) actually used for margin and liquidation — not the
        # last-trade price carried by TickReceivedEvent.
        if subscribe_mark_price:
            self._ws.subscribe_mark_price(symbol, self._make_mark_price_handler(symbol))

        # Subscribe to the best-bid/best-ask stream (Binance bookTicker /
        # Bybit orderbook.1, normalized to the same shape at the adapter
        # boundary — see BybitWebSocket._normalize_book_ticker).
        if subscribe_book_ticker:
            self._ws.subscribe_book_ticker(symbol, self._make_book_ticker_handler(symbol))

        logger.info(
            "Market data feed active: %s timeframes=%s ticks=%s mark_price=%s book_ticker=%s",
            symbol,
            [tf.value for tf in timeframes],
            subscribe_ticks,
            subscribe_mark_price,
            subscribe_book_ticker,
        )

    def _make_candle_callback(
        self,
        symbol: str,
        timeframe: Timeframe,
    ) -> Any:
        """Create a candle-close callback that publishes a CandleClosedEvent."""
        async def _on_close(candle: OHLCV) -> None:
            await self._bus.publish(
                CandleClosedEvent(
                    source=self._source,
                    symbol=symbol,
                    timeframe=timeframe.value,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    candle_timestamp=candle.timestamp,
                )
            )

        return _on_close

    def _make_mark_price_handler(self, symbol: str) -> Any:
        """
        Create a handler for the exchange's mark-price stream.

        Consumes the Binance-shaped markPriceUpdate dict that both
        BinanceFuturesWebSocket (natively) and BybitWebSocket
        (normalized in _normalize_ticker) deliver:
            {"e":"markPriceUpdate","E":..., "s":"BTCUSDT",
             "p":"<mark price>", "P":"<estimated settle price>",
             "i":"<index price>", "r":"<funding rate>", "T":<next funding ms>}
        """
        async def _on_mark_price(msg: dict[str, Any]) -> None:
            mark_price_raw = msg.get("p")
            if mark_price_raw is None:
                logger.warning("markPrice message missing 'p' field: %s", msg)
                return

            index_price_raw = msg.get("i")
            funding_rate_raw = msg.get("r")
            next_funding_ms = msg.get("T")
            mark_price = Decimal(str(mark_price_raw))

            # Cache alongside publishing the event: MarketDataFeed is handed
            # the cache specifically for this (put_mark_price/get_mark_price
            # already exist on MarketDataCache), but nothing was ever
            # calling it — any synchronous/polling reader (dashboard,
            # reconciliation, a future consumer that doesn't want to
            # subscribe to the event bus) would silently always get None.
            self._cache.put_mark_price(symbol, mark_price)

            funding_rate = (
                Decimal(str(funding_rate_raw)) if funding_rate_raw is not None else None
            )
            next_funding_time = (
                datetime.fromtimestamp(int(next_funding_ms) / 1000, tz=timezone.utc)
                if next_funding_ms is not None
                else None
            )

            await self._bus.publish(
                MarkPriceUpdatedEvent(
                    source=self._source,
                    symbol=symbol,
                    mark_price=mark_price,
                    index_price=(
                        Decimal(str(index_price_raw)) if index_price_raw is not None else None
                    ),
                    funding_rate=funding_rate,
                    next_funding_time=next_funding_time,
                )
            )

            # FundingRateEvent is dedicated to funding-rate changes, unlike
            # MarkPriceUpdatedEvent above (published on every ~1s tick
            # regardless of whether the rate moved). Firing it on every
            # mark-price update would spam subscribers with an event named
            # "FundingRateEvent" that's 99% identical repeats — instead,
            # publish only when the rate (or, absent a rate change, the
            # next funding time — Bybit's ticker deltas can carry an
            # updated nextFundingTime alone as a period rolls over) has
            # actually moved since the last update we saw for this symbol.
            if next_funding_time is not None:
                self._last_next_funding_time[symbol] = next_funding_time
            if funding_rate is not None:
                previous_rate = self._last_funding_rate.get(symbol)
                previous_next_funding = self._last_next_funding_time.get(symbol)
                changed = previous_rate is None or previous_rate != funding_rate
                self._last_funding_rate[symbol] = funding_rate
                effective_next_funding = next_funding_time or previous_next_funding
                if changed and effective_next_funding is not None:
                    await self._bus.publish(
                        FundingRateEvent(
                            source=self._source,
                            symbol=symbol,
                            rate=funding_rate,
                            next_funding_time=effective_next_funding,
                        )
                    )

        return _on_mark_price

    def _make_book_ticker_handler(self, symbol: str) -> Any:
        """
        Create a handler for the best-bid/best-ask stream.

        Consumes the Binance-shaped bookTicker dict that both
        BinanceFuturesWebSocket (natively) and BybitWebSocket
        (normalized in _normalize_book_ticker) deliver:
            {"s":"BTCUSDT","b":"<bid price>","B":"<bid qty>",
             "a":"<ask price>","A":"<ask qty>"}

        Either side may legitimately be absent right after subscribing
        (see _normalize_book_ticker) — the update is skipped rather than
        publishing a one-sided or zero-filled spread.
        """
        async def _on_book_ticker(msg: dict[str, Any]) -> None:
            bid_raw = msg.get("b")
            ask_raw = msg.get("a")
            if bid_raw is None or ask_raw is None:
                return

            best_bid = Decimal(str(bid_raw))
            best_ask = Decimal(str(ask_raw))
            self._cache.put_best_bid_ask(symbol, best_bid, best_ask)
            await self._bus.publish(
                OrderBookUpdatedEvent(
                    source=self._source,
                    symbol=symbol,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=best_ask - best_bid,
                )
            )

        return _on_book_ticker

    @property
    def subscribed_symbols(self) -> set[str]:
        return self._subscribed_symbols
