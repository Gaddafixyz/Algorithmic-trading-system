"""Unit tests for core modules: events, validation, config, models."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    SignalDirection,
    Timeframe,
)
from src.core.events import (
    BaseEvent,
    CandleClosedEvent,
    EventBus,
    OrderFilledEvent,
    SignalGeneratedEvent,
    reset_event_bus,
)
from src.core.exceptions import (
    InvalidSymbolError,
    InvalidPriceError,
    InvalidQuantityError,
    ValidationError,
    DataQualityError,
    RiskLimitExceededError,
    CircuitBreakerOpenError,
)
from src.core.models import OHLCV, BookLevel, OrderBook, Price, Quantity, Percentage, SymbolFilter
from src.core.validation import (
    validate_symbol,
    validate_price,
    validate_quantity,
    validate_timeframe,
    validate_stop_loss,
    validate_take_profit,
    validate_ohlcv_consistency,
)
from src.core.registry import ComponentRegistry


class TestEventBus:
    def setup_method(self):
        reset_event_bus()
        self.bus = EventBus()

    @pytest.mark.asyncio
    async def test_subscribe_and_receive(self):
        received = []

        async def handler(event: BaseEvent) -> None:
            received.append(event)

        self.bus.subscribe(CandleClosedEvent, handler)

        event = CandleClosedEvent(
            source="test",
            symbol="BTCUSDT",
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("98"),
            close=Decimal("103"),
            volume=Decimal("1000"),
            candle_timestamp=datetime.now(tz=UTC),
        )
        await self.bus.publish(event)
        assert len(received) == 1
        assert received[0].symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_multiple_handlers(self):
        calls = []

        async def h1(e): calls.append("h1")
        async def h2(e): calls.append("h2")

        self.bus.subscribe(CandleClosedEvent, h1)
        self.bus.subscribe(CandleClosedEvent, h2)

        event = CandleClosedEvent(
            source="test",
            symbol="BTCUSDT",
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("98"),
            close=Decimal("103"),
            volume=Decimal("1000"),
            candle_timestamp=datetime.now(tz=UTC),
        )
        await self.bus.publish(event)
        assert "h1" in calls
        assert "h2" in calls

    @pytest.mark.asyncio
    async def test_failed_handler_does_not_block_others(self):
        results = []

        async def bad_handler(e):
            raise ValueError("intentional error")

        async def good_handler(e):
            results.append("good")

        self.bus.subscribe(CandleClosedEvent, bad_handler)
        self.bus.subscribe(CandleClosedEvent, good_handler)

        event = CandleClosedEvent(
            source="test",
            symbol="BTCUSDT",
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("98"),
            close=Decimal("103"),
            volume=Decimal("1000"),
            candle_timestamp=datetime.now(tz=UTC),
        )
        await self.bus.publish(event)
        assert "good" in results  # Good handler still ran

    @pytest.mark.asyncio
    async def test_subscribe_all_receives_all_events(self):
        received_types = []

        async def global_handler(e):
            received_types.append(type(e).__name__)

        self.bus.subscribe_all(global_handler)

        await self.bus.publish(CandleClosedEvent(
            source="test", symbol="BTC", timeframe="1h",
            open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"), close=Decimal("1.5"),
            volume=Decimal("100"), candle_timestamp=datetime.now(tz=UTC),
        ))
        assert "CandleClosedEvent" in received_types

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        calls = []

        async def handler(e):
            calls.append(1)

        self.bus.subscribe(CandleClosedEvent, handler)
        self.bus.unsubscribe(CandleClosedEvent, handler)

        await self.bus.publish(CandleClosedEvent(
            source="test", symbol="BTC", timeframe="1h",
            open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"), close=Decimal("1.5"),
            volume=Decimal("100"), candle_timestamp=datetime.now(tz=UTC),
        ))
        assert calls == []

    @pytest.mark.asyncio
    async def test_stats_updated(self):
        async def handler(e): pass
        self.bus.subscribe(CandleClosedEvent, handler)

        await self.bus.publish(CandleClosedEvent(
            source="test", symbol="BTC", timeframe="1h",
            open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"), close=Decimal("1.5"),
            volume=Decimal("100"), candle_timestamp=datetime.now(tz=UTC),
        ))
        assert self.bus.stats["published"] == 1

    @pytest.mark.asyncio
    async def test_no_handlers_no_error(self):
        # Publishing with no subscribers should not raise
        await self.bus.publish(CandleClosedEvent(
            source="test", symbol="BTC", timeframe="1h",
            open=Decimal("1"), high=Decimal("2"), low=Decimal("0.5"), close=Decimal("1.5"),
            volume=Decimal("100"), candle_timestamp=datetime.now(tz=UTC),
        ))


class TestValidation:
    def test_valid_symbol(self):
        assert validate_symbol("btcusdt") == "BTCUSDT"
        assert validate_symbol("ETHUSDT") == "ETHUSDT"
        assert validate_symbol("  SOLUSDT  ") == "SOLUSDT"

    def test_invalid_symbol_empty(self):
        with pytest.raises(InvalidSymbolError):
            validate_symbol("")

    def test_invalid_symbol_special_chars(self):
        with pytest.raises(InvalidSymbolError):
            validate_symbol("BTC-USDT")

    def test_valid_price(self):
        p = validate_price("50000.50")
        assert p == Decimal("50000.50")

    def test_zero_price_invalid(self):
        with pytest.raises(InvalidPriceError):
            validate_price(0)

    def test_negative_price_invalid(self):
        with pytest.raises(InvalidPriceError):
            validate_price(-100)

    def test_valid_quantity(self):
        q = validate_quantity("0.5")
        assert q == Decimal("0.5")

    def test_zero_quantity_invalid(self):
        with pytest.raises(InvalidQuantityError):
            validate_quantity(0)

    def test_negative_quantity_invalid(self):
        with pytest.raises(InvalidQuantityError):
            validate_quantity(-1)

    def test_valid_timeframe(self):
        tf = validate_timeframe("1h")
        assert tf == Timeframe.H1

    def test_invalid_timeframe(self):
        from src.core.exceptions import InvalidTimeframeError
        with pytest.raises(InvalidTimeframeError):
            validate_timeframe("7h")

    def test_valid_stop_loss_long(self):
        # Should not raise
        validate_stop_loss(Decimal("49000"), Decimal("50000"), is_long=True)

    def test_invalid_stop_loss_long_above_entry(self):
        with pytest.raises(ValidationError):
            validate_stop_loss(Decimal("51000"), Decimal("50000"), is_long=True)

    def test_valid_stop_loss_short(self):
        validate_stop_loss(Decimal("51000"), Decimal("50000"), is_long=False)

    def test_invalid_stop_loss_short_below_entry(self):
        with pytest.raises(ValidationError):
            validate_stop_loss(Decimal("49000"), Decimal("50000"), is_long=False)

    def test_valid_take_profit_long(self):
        validate_take_profit(Decimal("52000"), Decimal("50000"), is_long=True)

    def test_invalid_take_profit_long_below_entry(self):
        with pytest.raises(ValidationError):
            validate_take_profit(Decimal("48000"), Decimal("50000"), is_long=True)

    def test_ohlcv_consistency_valid(self):
        # Should not raise
        validate_ohlcv_consistency(
            Decimal("100"), Decimal("105"), Decimal("98"), Decimal("103"), Decimal("1000")
        )

    def test_ohlcv_high_below_low_invalid(self):
        with pytest.raises(DataQualityError):
            validate_ohlcv_consistency(
                Decimal("100"), Decimal("95"), Decimal("98"), Decimal("103"), Decimal("1000")
            )


class TestModels:
    def test_price_positive(self):
        p = Price(value=Decimal("100"))
        assert p.value == Decimal("100")

    def test_price_negative_raises(self):
        with pytest.raises(Exception):
            Price(value=Decimal("-1"))

    def test_price_arithmetic(self):
        p1 = Price(value=Decimal("100"))
        p2 = Price(value=Decimal("50"))
        assert (p1 + p2).value == Decimal("150")
        assert (p1 - p2).value == Decimal("50")

    def test_quantity_positive(self):
        q = Quantity(value=Decimal("1.5"))
        assert q.value == Decimal("1.5")

    def test_quantity_zero_raises(self):
        with pytest.raises(Exception):
            Quantity(value=Decimal("0"))

    def test_percentage_valid_range(self):
        p = Percentage(value=Decimal("50"))
        assert p.as_fraction == Decimal("0.5")

    def test_percentage_out_of_range_raises(self):
        with pytest.raises(Exception):
            Percentage(value=Decimal("101"))

    def test_ohlcv_properties(self):
        candle = OHLCV(
            timestamp=datetime.now(tz=UTC),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("1000"),
        )
        assert candle.is_bullish
        assert not candle.is_bearish
        assert candle.range_size == Decimal("15")
        assert candle.body_size == Decimal("5")
        assert candle.mid_price == Decimal("102.5")
        assert candle.upper_wick == Decimal("5")
        assert candle.lower_wick == Decimal("5")

    def test_symbol_filter_round_price(self):
        sf = SymbolFilter(
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
            step_size=Decimal("0.001"),
            min_price=Decimal("0.01"),
            max_price=Decimal("1000000"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        rounded = sf.round_price(Decimal("50000.567"))
        assert rounded == Decimal("50000.57")

    def test_symbol_filter_round_quantity(self):
        sf = SymbolFilter(
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
            step_size=Decimal("0.001"),
            min_price=Decimal("0.01"),
            max_price=Decimal("1000000"),
            tick_size=Decimal("0.01"),
            min_notional=Decimal("5"),
        )
        rounded = sf.round_quantity(Decimal("1.2345"))
        assert rounded == Decimal("1.234")

    def test_order_book_best_bid_ask(self):
        book = OrderBook(
            symbol="BTCUSDT",
            bids=[
                BookLevel(price=Decimal("49950"), quantity=Decimal("1")),
                BookLevel(price=Decimal("49900"), quantity=Decimal("2")),
            ],
            asks=[
                BookLevel(price=Decimal("50050"), quantity=Decimal("1")),
                BookLevel(price=Decimal("50100"), quantity=Decimal("2")),
            ],
            timestamp=datetime.now(tz=UTC),
            last_update_id=12345,
        )
        assert book.best_bid == Decimal("49950")
        assert book.best_ask == Decimal("50050")
        assert book.spread == Decimal("100")
        assert book.mid_price == Decimal("50000")


class TestExceptions:
    def test_invalid_symbol_error(self):
        exc = InvalidSymbolError("BTC-USDT")
        assert "BTC-USDT" in str(exc)
        assert exc.context["symbol"] == "BTC-USDT"

    def test_risk_limit_exceeded_error(self):
        exc = RiskLimitExceededError("max_drawdown", 16.0, 15.0)
        assert exc.limit_name == "max_drawdown"
        assert exc.current == 16.0
        assert exc.limit == 15.0
        assert "16.0" in str(exc)

    def test_circuit_breaker_open_error(self):
        exc = CircuitBreakerOpenError("daily loss exceeded")
        assert "daily loss exceeded" in str(exc)


class TestComponentRegistry:
    def test_register_and_get(self):
        reg = ComponentRegistry()
        obj = object()
        reg.register("my_component", obj)
        assert reg.get("my_component", object) is obj

    def test_get_wrong_type_raises(self):
        reg = ComponentRegistry()
        reg.register("my_str", "hello")
        with pytest.raises(TypeError):
            reg.get("my_str", int)

    def test_get_missing_raises(self):
        reg = ComponentRegistry()
        with pytest.raises(KeyError):
            reg.get("missing", object)

    def test_get_optional_returns_none(self):
        reg = ComponentRegistry()
        result = reg.get_optional("missing", object)
        assert result is None

    def test_duplicate_register_raises(self):
        reg = ComponentRegistry()
        reg.register("comp", object())
        with pytest.raises(ValueError):
            reg.register("comp", object())

    def test_overwrite_allowed(self):
        reg = ComponentRegistry()
        obj1 = object()
        obj2 = object()
        reg.register("comp", obj1)
        reg.register("comp", obj2, overwrite=True)
        assert reg.get("comp", object) is obj2

    def test_unregister(self):
        reg = ComponentRegistry()
        reg.register("comp", object())
        assert reg.unregister("comp") is True
        assert "comp" not in reg

    def test_list_registered(self):
        reg = ComponentRegistry()
        reg.register("a", object())
        reg.register("b", object())
        names = reg.list_registered()
        assert set(names) == {"a", "b"}


class TestTimeframe:
    def test_seconds_property(self):
        assert Timeframe.H1.seconds == 3600
        assert Timeframe.D1.seconds == 86400
        assert Timeframe.M15.seconds == 900

    def test_milliseconds_property(self):
        assert Timeframe.H1.milliseconds == 3_600_000

    def test_all_timeframes_have_seconds(self):
        for tf in Timeframe:
            assert tf.seconds > 0
            assert tf.milliseconds == tf.seconds * 1000
