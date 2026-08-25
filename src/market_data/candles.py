"""
Candle service: historical fetch + live stream + cache.

Provides a unified interface for strategies to get OHLCV data.
Handles cache warm-up, deduplication, and live candle updates.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.enums import Timeframe
from src.core.exceptions import InsufficientDataError
from src.core.interfaces import IExchangeClient, IWebSocketStreamClient
from src.core.models import OHLCV
from src.market_data.cache import MarketDataCache

logger = logging.getLogger(__name__)


class CandleService:
    """
    Unified OHLCV data service.

    - Fetches historical candles via REST on startup
    - Subscribes to live updates via WebSocket
    - Serves data from in-memory cache
    - Raises InsufficientDataError if insufficient history for indicators
    """

    def __init__(
        self,
        rest_client: IExchangeClient,
        ws_client: IWebSocketStreamClient,
        cache: MarketDataCache,
        warmup_candles: int = 500,
        resync_candles: int = 20,
    ) -> None:
        self._rest = rest_client
        self._ws = ws_client
        self._cache = cache
        self._warmup_candles = warmup_candles
        self._resync_candles = resync_candles
        self._live_callbacks: dict[tuple[str, str], list[Any]] = {}
        self._initialized: set[tuple[str, str]] = set()

    async def initialize(self, symbol: str, timeframe: Timeframe) -> None:
        """
        Warm up cache with historical data for a symbol/timeframe pair.

        Should be called during system startup before live trading begins.
        """
        key = (symbol.upper(), timeframe.value)
        if key in self._initialized:
            return

        logger.info(
            "Warming up candle cache: %s %s (%d candles)", symbol, timeframe, self._warmup_candles
        )
        candles = await self._rest.get_candles(symbol, timeframe, limit=self._warmup_candles)
        self._cache.put_candles(symbol, timeframe, candles)
        self._initialized.add(key)
        logger.info(
            "Cache warm-up complete: %s %s (%d candles loaded)",
            symbol,
            timeframe,
            len(candles),
        )

    async def subscribe_live(
        self,
        symbol: str,
        timeframe: Timeframe,
        callback: Any | None = None,
    ) -> None:
        """
        Subscribe to live kline stream and update cache on each close.

        Optionally calls `callback(candle: OHLCV)` when a candle closes.
        """
        (symbol.upper(), timeframe.value)

        async def _on_kline(msg: dict[str, Any]) -> None:
            k = msg if "t" in msg else msg.get("k", msg)
            is_closed = bool(k.get("x", False))

            candle = OHLCV(
                timestamp=datetime.fromtimestamp(int(k["t"]) / 1000, tz=UTC),
                open=Decimal(str(k["o"])),
                high=Decimal(str(k["h"])),
                low=Decimal(str(k["l"])),
                close=Decimal(str(k["c"])),
                volume=Decimal(str(k["v"])),
            )

            # Always update the current (possibly unclosed) candle
            self._cache.put_candle(symbol, timeframe, candle)

            if is_closed and callback:
                await callback(candle)

        self._ws.subscribe_kline(symbol, timeframe, _on_kline)
        logger.info("Live candle subscription started: %s %s", symbol, timeframe)

    async def resync_after_reconnect(self) -> None:
        """
        Re-fetch recent candles via REST for every symbol/timeframe pair
        this service has initialized, to backfill whatever closed while
        the live kline WebSocket was disconnected.

        The live stream only ever pushes candles forward from when a new
        connection opens — any candle that closed during the outage is
        gone unless pulled back via REST. Fetches a small recent window
        rather than a full re-warm-up (self._warmup_candles, e.g. 500)
        since the gap this covers is bounded by reconnect_delay/backoff,
        not the full history window; CandleBuffer.append dedupes by
        timestamp so an overlapping fetch is safe to merge either way.
        Called via MarketDataFeed, which registers this as the WebSocket
        client's reconnect callback (see IWebSocketStreamClient.
        set_reconnect_callback).
        """
        for symbol, tf_value in list(self._initialized):
            timeframe = Timeframe(tf_value)
            try:
                candles = await self._rest.get_candles(
                    symbol, timeframe, limit=self._resync_candles
                )
                self._cache.put_candles(symbol, timeframe, candles)
                logger.info(
                    "Resynced %s %s after reconnect (%d candles fetched)",
                    symbol,
                    timeframe,
                    len(candles),
                )
            except Exception as exc:
                logger.error(
                    "Candle resync failed for %s %s after reconnect: %s",
                    symbol,
                    timeframe,
                    exc,
                )

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        require_closed: bool = True,
    ) -> list[OHLCV]:
        """
        Get candles from cache.

        Args:
            symbol: Trading symbol.
            timeframe: Candlestick timeframe.
            limit: Number of candles requested.
            require_closed: If True, excludes the current open candle.

        Raises:
            InsufficientDataError: If fewer than `limit` candles are available.
        """
        candles = self._cache.get_candles(symbol, timeframe, limit + 1)
        if require_closed:
            # The last candle may be the current, unclosed one — drop it
            candles = candles[:-1]

        if len(candles) < limit:
            raise InsufficientDataError(
                required=limit,
                available=len(candles),
                context=f"{symbol} {timeframe}",
            )
        return candles[-limit:]

    def get_latest_candle(self, symbol: str, timeframe: Timeframe) -> OHLCV | None:
        return self._cache.get_latest_candle(symbol, timeframe)

    def candle_count(self, symbol: str, timeframe: Timeframe) -> int:
        return self._cache.candle_count(symbol, timeframe)

    def is_warmed_up(self, symbol: str, timeframe: Timeframe, min_candles: int) -> bool:
        return self.candle_count(symbol, timeframe) >= min_candles
