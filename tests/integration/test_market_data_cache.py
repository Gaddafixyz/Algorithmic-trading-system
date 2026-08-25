"""Integration tests for market data cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import Timeframe
from src.core.models import OHLCV, Ticker
from src.market_data.cache import CandleBuffer, MarketDataCache


def make_ohlcv(i: int, price: float = 100.0) -> OHLCV:
    base = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)
    return OHLCV(
        timestamp=base,
        open=Decimal(str(price)),
        high=Decimal(str(price + 2)),
        low=Decimal(str(price - 2)),
        close=Decimal(str(price + 1)),
        volume=Decimal("500"),
    )


def make_ticker(symbol: str = "BTCUSDT", price: float = 50000.0) -> Ticker:
    return Ticker(
        symbol=symbol,
        price=Decimal(str(price)),
        price_change_24h=Decimal("100"),
        price_change_pct_24h=Decimal("0.2"),
        high_24h=Decimal(str(price + 500)),
        low_24h=Decimal(str(price - 500)),
        volume_24h=Decimal("10000"),
        quote_volume_24h=Decimal("500000000"),
        timestamp=datetime.now(tz=UTC),
    )


class TestCandleBuffer:
    def test_append_and_retrieve(self):
        buf = CandleBuffer(max_size=100)
        for i in range(10):
            buf.append(make_ohlcv(i))
        assert len(buf) == 10
        candles = buf.get_latest(5)
        assert len(candles) == 5
        # Should be the 5 most recent
        assert candles[-1].timestamp > candles[0].timestamp

    def test_max_size_respected(self):
        buf = CandleBuffer(max_size=5)
        for i in range(10):
            buf.append(make_ohlcv(i))
        assert len(buf) == 5

    def test_deduplication_by_timestamp(self):
        buf = CandleBuffer()
        c1 = make_ohlcv(0, price=100.0)
        c2 = make_ohlcv(0, price=101.0)  # Same timestamp, different price
        buf.append(c1)
        buf.append(c2)
        assert len(buf) == 1
        # Latest value at that timestamp should be c2
        assert buf.latest.close == Decimal("102.0")

    def test_latest_returns_most_recent(self):
        buf = CandleBuffer()
        for i in range(5):
            buf.append(make_ohlcv(i, price=float(100 + i)))
        assert buf.latest.open == Decimal(str(104.0))

    def test_empty_buffer_latest_is_none(self):
        buf = CandleBuffer()
        assert buf.latest is None

    def test_get_latest_more_than_available(self):
        buf = CandleBuffer()
        for i in range(3):
            buf.append(make_ohlcv(i))
        candles = buf.get_latest(10)
        assert len(candles) == 3


class TestMarketDataCache:
    def test_put_and_get_candles(self):
        cache = MarketDataCache()
        for i in range(20):
            cache.put_candle("BTCUSDT", Timeframe.H1, make_ohlcv(i))
        candles = cache.get_candles("BTCUSDT", Timeframe.H1, limit=10)
        assert len(candles) == 10

    def test_put_candles_bulk(self):
        cache = MarketDataCache()
        candles = [make_ohlcv(i) for i in range(50)]
        cache.put_candles("BTCUSDT", Timeframe.H1, candles)
        retrieved = cache.get_candles("BTCUSDT", Timeframe.H1, limit=50)
        assert len(retrieved) == 50

    def test_different_symbols_isolated(self):
        cache = MarketDataCache()
        cache.put_candle("BTCUSDT", Timeframe.H1, make_ohlcv(0, 50000.0))
        cache.put_candle("ETHUSDT", Timeframe.H1, make_ohlcv(0, 3000.0))

        btc = cache.get_candles("BTCUSDT", Timeframe.H1, limit=1)
        eth = cache.get_candles("ETHUSDT", Timeframe.H1, limit=1)

        assert btc[0].open == Decimal("50000.0")
        assert eth[0].open == Decimal("3000.0")

    def test_different_timeframes_isolated(self):
        cache = MarketDataCache()
        cache.put_candle("BTCUSDT", Timeframe.H1, make_ohlcv(0, 100.0))
        cache.put_candle("BTCUSDT", Timeframe.H4, make_ohlcv(0, 200.0))

        h1 = cache.get_candles("BTCUSDT", Timeframe.H1, limit=1)
        h4 = cache.get_candles("BTCUSDT", Timeframe.H4, limit=1)

        assert h1[0].open != h4[0].open

    def test_get_candles_returns_empty_for_unknown(self):
        cache = MarketDataCache()
        candles = cache.get_candles("UNKNOWN", Timeframe.H1, limit=10)
        assert candles == []

    def test_ticker_stored_and_retrieved(self):
        cache = MarketDataCache(ticker_ttl_seconds=60.0)
        ticker = make_ticker("BTCUSDT", 50000.0)
        cache.put_ticker(ticker)
        retrieved = cache.get_ticker("BTCUSDT")
        assert retrieved is not None
        assert retrieved.price == Decimal("50000.0")

    def test_stale_ticker_not_returned(self):
        cache = MarketDataCache(ticker_ttl_seconds=0.001)
        ticker = make_ticker()
        cache.put_ticker(ticker)
        import time

        time.sleep(0.01)
        assert cache.get_ticker("BTCUSDT") is None

    def test_mark_price_stored_and_retrieved(self):
        cache = MarketDataCache(ticker_ttl_seconds=60.0)
        cache.put_mark_price("BTCUSDT", Decimal("49999"))
        price = cache.get_mark_price("BTCUSDT")
        assert price == Decimal("49999")

    def test_best_bid_ask_stored_and_retrieved(self):
        cache = MarketDataCache(ticker_ttl_seconds=60.0)
        cache.put_best_bid_ask("BTCUSDT", Decimal("50000.10"), Decimal("50000.50"))
        quote = cache.get_best_bid_ask("BTCUSDT")
        assert quote is not None
        best_bid, best_ask = quote
        assert best_bid == Decimal("50000.10")
        assert best_ask == Decimal("50000.50")

    def test_best_bid_ask_case_insensitive_symbol(self):
        cache = MarketDataCache(ticker_ttl_seconds=60.0)
        cache.put_best_bid_ask("btcusdt", Decimal("100"), Decimal("101"))
        assert cache.get_best_bid_ask("BTCUSDT") is not None

    def test_best_bid_ask_unknown_symbol_returns_none(self):
        cache = MarketDataCache()
        assert cache.get_best_bid_ask("ETHUSDT") is None

    def test_stale_best_bid_ask_not_returned(self):
        cache = MarketDataCache(ticker_ttl_seconds=0.001)
        cache.put_best_bid_ask("BTCUSDT", Decimal("100"), Decimal("101"))
        import time

        time.sleep(0.01)
        assert cache.get_best_bid_ask("BTCUSDT") is None

    def test_candle_count(self):
        cache = MarketDataCache()
        for i in range(7):
            cache.put_candle("BTCUSDT", Timeframe.M15, make_ohlcv(i))
        assert cache.candle_count("BTCUSDT", Timeframe.M15) == 7

    def test_stats(self):
        cache = MarketDataCache()
        cache.put_candle("BTCUSDT", Timeframe.H1, make_ohlcv(0))
        cache.put_ticker(make_ticker())
        cache.put_best_bid_ask("BTCUSDT", Decimal("100"), Decimal("101"))
        stats = cache.stats
        assert stats["candle_keys"] >= 1
        assert stats["tickers"] >= 1
        assert stats["best_bid_ask"] >= 1
