"""Unit tests for src.market_data.feeds.MarketDataFeed."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import Timeframe
from src.core.events import (
    EventBus,
    FundingRateEvent,
    MarkPriceUpdatedEvent,
    OrderBookUpdatedEvent,
    TickReceivedEvent,
    reset_event_bus,
    set_event_bus,
)
from src.exchange.binance.websocket import BinanceFuturesWebSocket
from src.market_data.cache import MarketDataCache
from src.market_data.candles import CandleService
from src.market_data.feeds import MarketDataFeed


@pytest.fixture(autouse=True)
def isolated_event_bus():
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


@pytest.fixture
def ws_client() -> MagicMock:
    client = MagicMock(spec=BinanceFuturesWebSocket)
    return client


@pytest.fixture
def candle_service() -> AsyncMock:
    return AsyncMock(spec=CandleService)


@pytest.fixture
def cache() -> MagicMock:
    return MagicMock(spec=MarketDataCache)


@pytest.fixture
def feed(ws_client: MagicMock, candle_service: AsyncMock, cache: MagicMock) -> MarketDataFeed:
    return MarketDataFeed(ws_client=ws_client, candle_service=candle_service, cache=cache)


class TestSubscribeMarkPrice:
    async def test_subscribes_to_mark_price_stream_by_default(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
    ) -> None:
        await feed.subscribe("btcusdt", [Timeframe.H1])
        ws_client.subscribe_mark_price.assert_called_once()
        assert ws_client.subscribe_mark_price.call_args.args[0] == "BTCUSDT"

    async def test_can_be_disabled(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
    ) -> None:
        await feed.subscribe("BTCUSDT", [Timeframe.H1], subscribe_mark_price=False)
        ws_client.subscribe_mark_price.assert_not_called()

    async def test_mark_price_handler_publishes_event_with_full_payload(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[MarkPriceUpdatedEvent] = []

        async def _capture(event: MarkPriceUpdatedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(MarkPriceUpdatedEvent, _capture)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler(
            {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "50123.45",
                "P": "50120.00",
                "i": "50100.00",
                "r": "0.00010000",
                "T": 1_700_000_000_000,
            }
        )

        assert len(received) == 1
        event = received[0]
        assert event.symbol == "BTCUSDT"
        assert event.mark_price == Decimal("50123.45")
        assert event.index_price == Decimal("50100.00")
        assert event.funding_rate == Decimal("0.00010000")
        assert event.next_funding_time is not None

    async def test_mark_price_handler_tolerates_missing_optional_fields(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[MarkPriceUpdatedEvent] = []

        async def _capture(event: MarkPriceUpdatedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(MarkPriceUpdatedEvent, _capture)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler({"p": "50000.00"})

        assert len(received) == 1
        event = received[0]
        assert event.mark_price == Decimal("50000.00")
        assert event.index_price is None
        assert event.funding_rate is None
        assert event.next_funding_time is None

    async def test_mark_price_handler_logs_and_skips_when_price_missing(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[MarkPriceUpdatedEvent] = []

        async def _capture(event: MarkPriceUpdatedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(MarkPriceUpdatedEvent, _capture)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler({"e": "markPriceUpdate", "s": "BTCUSDT"})  # no "p"

        assert received == []


class TestSubscribeTicks:
    async def test_tick_stream_still_wired_independently_of_mark_price(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[TickReceivedEvent] = []

        async def _capture(event: TickReceivedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(TickReceivedEvent, _capture)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_agg_trade.call_args.args[1]

        await handler({"p": "50000.5", "q": "0.25", "m": True})

        assert len(received) == 1
        assert received[0].price == Decimal("50000.5")
        assert received[0].is_buyer_maker is True


class TestSubscribeBookTicker:
    """
    Regression coverage for OrderBookUpdatedEvent wiring: previously the
    event type existed but nothing ever published it (see CHANGELOG).
    """

    async def test_subscribes_to_book_ticker_stream_by_default(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
    ) -> None:
        await feed.subscribe("btcusdt", [Timeframe.H1])
        ws_client.subscribe_book_ticker.assert_called_once()
        assert ws_client.subscribe_book_ticker.call_args.args[0] == "BTCUSDT"

    async def test_can_be_disabled(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
    ) -> None:
        await feed.subscribe("BTCUSDT", [Timeframe.H1], subscribe_book_ticker=False)
        ws_client.subscribe_book_ticker.assert_not_called()

    async def test_handler_publishes_best_bid_ask_and_spread(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[OrderBookUpdatedEvent] = []

        async def _capture(event: OrderBookUpdatedEvent) -> None:
            received.append(event)

        isolated_event_bus.subscribe(OrderBookUpdatedEvent, _capture)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_book_ticker.call_args.args[1]

        await handler({"s": "BTCUSDT", "b": "50000.10", "B": "1.5", "a": "50000.50", "A": "2.0"})

        assert len(received) == 1
        event = received[0]
        assert event.symbol == "BTCUSDT"
        assert event.best_bid == Decimal("50000.10")
        assert event.best_ask == Decimal("50000.50")
        assert event.spread == Decimal("0.40")

    async def test_handler_updates_market_data_cache(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        cache: MagicMock,
    ) -> None:
        """
        Regression coverage: OrderBookUpdatedEvent was published but nothing
        ever cached the quote for synchronous lookup (e.g. the orchestrator's
        spread filter, which reads MarketDataCache.get_best_bid_ask rather
        than subscribing to the event bus).
        """
        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_book_ticker.call_args.args[1]

        await handler({"s": "BTCUSDT", "b": "50000.10", "B": "1.5", "a": "50000.50", "A": "2.0"})

        cache.put_best_bid_ask.assert_called_once_with(
            "BTCUSDT", Decimal("50000.10"), Decimal("50000.50")
        )

    async def test_handler_skips_when_bid_side_missing(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[OrderBookUpdatedEvent] = []
        isolated_event_bus.subscribe(OrderBookUpdatedEvent, received.append)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_book_ticker.call_args.args[1]

        # Bybit normalization omits "b"/"B" entirely rather than sending
        # a zero when a side hasn't populated yet.
        await handler({"s": "BTCUSDT", "a": "50000.50", "A": "2.0"})

        assert received == []


class TestFundingRateEvent:
    """
    Regression coverage for FundingRateEvent wiring: previously the event
    type existed but nothing ever published it.
    """

    async def test_first_funding_rate_seen_publishes_event(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[FundingRateEvent] = []
        isolated_event_bus.subscribe(FundingRateEvent, received.append)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler(
            {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "50000.00",
                "r": "0.00010000",
                "T": 1_700_000_000_000,
            }
        )

        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"
        assert received[0].rate == Decimal("0.00010000")

    async def test_unchanged_rate_on_next_tick_does_not_republish(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[FundingRateEvent] = []
        isolated_event_bus.subscribe(FundingRateEvent, received.append)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        msg = {
            "e": "markPriceUpdate",
            "s": "BTCUSDT",
            "p": "50000.00",
            "r": "0.00010000",
            "T": 1_700_000_000_000,
        }
        await handler(msg)
        await handler({**msg, "p": "50001.00"})  # mark price ticks, rate unchanged

        assert len(received) == 1  # not republished on the second tick

    async def test_rate_change_publishes_a_second_event(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[FundingRateEvent] = []
        isolated_event_bus.subscribe(FundingRateEvent, received.append)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler(
            {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "50000.00",
                "r": "0.00010000",
                "T": 1_700_000_000_000,
            }
        )
        await handler(
            {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "50001.00",
                "r": "0.00015000",
                "T": 1_700_028_800_000,
            }
        )

        assert len(received) == 2
        assert received[1].rate == Decimal("0.00015000")

    async def test_no_funding_rate_in_payload_publishes_nothing(
        self,
        feed: MarketDataFeed,
        ws_client: MagicMock,
        isolated_event_bus: EventBus,
    ) -> None:
        received: list[FundingRateEvent] = []
        isolated_event_bus.subscribe(FundingRateEvent, received.append)

        await feed.subscribe("BTCUSDT", [Timeframe.H1])
        handler = ws_client.subscribe_mark_price.call_args.args[1]

        await handler({"e": "markPriceUpdate", "s": "BTCUSDT", "p": "50000.00"})

        assert received == []
