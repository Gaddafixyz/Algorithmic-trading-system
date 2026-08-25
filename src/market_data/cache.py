"""
In-memory market data cache backed optionally by Redis.

Provides fast O(1) access to recent candles and tickers.
Cache entries expire automatically to prevent stale data.

Design:
- Primary store: in-process dict for lowest latency
- Candles stored as deque with configurable max length
- TTL enforced via timestamps, not background tasks
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.constants import (
    CANDLE_CACHE_MAX_ITEMS,
    CANDLE_CACHE_TTL_SECONDS,
    TICKER_CACHE_TTL_SECONDS,
)
from src.core.enums import Timeframe
from src.core.models import OHLCV, Ticker, TimestampedValue
from src.domain.candle import Candle

logger = logging.getLogger(__name__)


class CandleBuffer:
    """
    Fixed-capacity circular buffer of OHLCV candles for one symbol/timeframe.

    Guarantees chronological ordering and deduplicates by timestamp.
    """

    def __init__(self, max_size: int = 500) -> None:
        self._data: deque[OHLCV] = deque(maxlen=max_size)

    def append(self, candle: OHLCV) -> None:
        """
        Append a candle, replacing an existing candle at the same timestamp.

        In practice only the trailing (most recently forming) candle is ever
        updated — live kline updates for the still-forming candle can fire
        multiple times per second. The previous implementation scanned the
        entire buffer (up to max_size, e.g. 500) and rebuilt a brand-new
        deque on every single tick, which adds up to real latency on the
        shared asyncio event loop under load. Check the trailing candle
        directly and replace in place (O(1), no rebuild); only fall back to
        a scan for the rare case of an out-of-order/backfilled update to a
        non-trailing candle.
        """
        if self._data and self._data[-1].timestamp == candle.timestamp:
            self._data[-1] = candle
            return

        if self._data and candle.timestamp < self._data[-1].timestamp:
            for i, c in enumerate(self._data):
                if c.timestamp == candle.timestamp:
                    self._data[i] = candle
                    return

        self._data.append(candle)

    def get_latest(self, n: int) -> list[OHLCV]:
        """Return the n most recent candles, oldest first."""
        data = list(self._data)
        return data[-n:] if n < len(data) else data

    def __len__(self) -> int:
        return len(self._data)

    @property
    def latest(self) -> OHLCV | None:
        return self._data[-1] if self._data else None

    @property
    def oldest(self) -> OHLCV | None:
        return self._data[0] if self._data else None


class MarketDataCache:
    """
    In-memory cache for market data.

    Keys: (symbol, timeframe) for candles; symbol for tickers.
    Thread-safe for asyncio single-threaded use.
    """

    def __init__(
        self,
        max_candles_per_key: int = CANDLE_CACHE_MAX_ITEMS,
        ticker_ttl_seconds: float = TICKER_CACHE_TTL_SECONDS,
        candle_ttl_seconds: float = CANDLE_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_candles = max_candles_per_key
        self._ticker_ttl = ticker_ttl_seconds
        self._candle_ttl = candle_ttl_seconds

        self._candle_buffers: dict[tuple[str, str], CandleBuffer] = {}
        self._tickers: dict[str, tuple[Ticker, datetime]] = {}
        # TimestampedValue replaces a raw (Decimal, datetime) tuple here —
        # it's exactly that shape and was sitting unused in core/models.py.
        # Internal-representation-only change: put_mark_price/get_mark_price
        # keep the same public signatures below.
        self._mark_prices: dict[str, TimestampedValue] = {}
        self._best_bid_ask: dict[str, tuple[Decimal, Decimal, datetime]] = {}

    # ─── Candles ──────────────────────────────────────────────────────────────

    def _candle_key(self, symbol: str, timeframe: Timeframe) -> tuple[str, str]:
        return (symbol.upper(), timeframe.value)

    def put_candle(self, symbol: str, timeframe: Timeframe, candle: OHLCV) -> None:
        """Store a candle in the cache."""
        key = self._candle_key(symbol, timeframe)
        if key not in self._candle_buffers:
            self._candle_buffers[key] = CandleBuffer(max_size=self._max_candles)
        self._candle_buffers[key].append(candle)

    def put_candles(self, symbol: str, timeframe: Timeframe, candles: list[OHLCV]) -> None:
        """Bulk-load candles (e.g. after REST fetch)."""
        for candle in candles:
            self.put_candle(symbol, timeframe, candle)

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
    ) -> list[OHLCV]:
        """Return up to `limit` most recent candles, oldest first."""
        key = self._candle_key(symbol, timeframe)
        buf = self._candle_buffers.get(key)
        if buf is None:
            return []
        return buf.get_latest(limit)

    def get_latest_candle(self, symbol: str, timeframe: Timeframe) -> OHLCV | None:
        key = self._candle_key(symbol, timeframe)
        buf = self._candle_buffers.get(key)
        return buf.latest if buf else None

    def candle_count(self, symbol: str, timeframe: Timeframe) -> int:
        key = self._candle_key(symbol, timeframe)
        buf = self._candle_buffers.get(key)
        return len(buf) if buf else 0

    # ─── Tickers ──────────────────────────────────────────────────────────────

    def put_ticker(self, ticker: Ticker) -> None:
        """Store a ticker snapshot."""
        self._tickers[ticker.symbol.upper()] = (ticker, datetime.now(tz=UTC))

    def get_ticker(self, symbol: str) -> Ticker | None:
        """Return cached ticker if not stale."""
        entry = self._tickers.get(symbol.upper())
        if entry is None:
            return None
        ticker, cached_at = entry
        age = (datetime.now(tz=UTC) - cached_at).total_seconds()
        if age > self._ticker_ttl:
            return None
        return ticker

    # ─── Mark prices ──────────────────────────────────────────────────────────

    def put_mark_price(self, symbol: str, price: Decimal) -> None:
        self._mark_prices[symbol.upper()] = TimestampedValue(
            value=price, timestamp=datetime.now(tz=UTC)
        )

    def get_mark_price(self, symbol: str) -> Decimal | None:
        entry = self._mark_prices.get(symbol.upper())
        if entry is None:
            return None
        age = (datetime.now(tz=UTC) - entry.timestamp).total_seconds()
        if age > self._ticker_ttl:
            return None
        return entry.value

    # ─── Best bid/ask ─────────────────────────────────────────────────────────

    def put_best_bid_ask(self, symbol: str, best_bid: Decimal, best_ask: Decimal) -> None:
        """Store the latest top-of-book snapshot for a symbol.

        Fed by MarketDataFeed's book-ticker handler (OrderBookUpdatedEvent
        producer) so that strategy-side spread checks (filters.py::
        filter_by_spread) have a live source of best_bid/best_ask instead
        of needing to re-derive it from the event bus themselves.
        """
        self._best_bid_ask[symbol.upper()] = (best_bid, best_ask, datetime.now(tz=UTC))

    def get_best_bid_ask(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """Return (best_bid, best_ask) if cached and not stale, else None."""
        entry = self._best_bid_ask.get(symbol.upper())
        if entry is None:
            return None
        best_bid, best_ask, cached_at = entry
        age = (datetime.now(tz=UTC) - cached_at).total_seconds()
        if age > self._ticker_ttl:
            return None
        return best_bid, best_ask

    # ─── Housekeeping ─────────────────────────────────────────────────────────

    def evict_stale_tickers(self) -> int:
        """Remove expired ticker entries. Returns count removed."""
        now = datetime.now(tz=UTC)
        stale = [
            s for s, (_, t) in self._tickers.items()
            if (now - t).total_seconds() > self._ticker_ttl
        ]
        for s in stale:
            del self._tickers[s]
        return len(stale)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "candle_keys": len(self._candle_buffers),
            "total_candles": sum(len(b) for b in self._candle_buffers.values()),
            "tickers": len(self._tickers),
            "mark_prices": len(self._mark_prices),
            "best_bid_ask": len(self._best_bid_ask),
        }
