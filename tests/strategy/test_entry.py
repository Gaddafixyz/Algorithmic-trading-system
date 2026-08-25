"""Unit tests for src.strategy.entry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import OrderSide, OrderType, SignalDirection, SignalStrength, Timeframe
from src.domain.signal import Signal
from src.strategy.entry import get_entry_order_type, get_order_side, is_entry_valid


def make_signal(
    entry: str = "50000",
    sl: str = "49000",
    tp: str = "52000",
    entry_zone_high: str | None = None,
    entry_zone_low: str | None = None,
    expires_at: datetime | None = None,
    direction: SignalDirection = SignalDirection.LONG,
) -> Signal:
    return Signal(
        strategy_id="test",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        direction=direction,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal(entry),
        stop_loss=Decimal(sl),
        take_profit=Decimal(tp),
        entry_zone_high=Decimal(entry_zone_high) if entry_zone_high else None,
        entry_zone_low=Decimal(entry_zone_low) if entry_zone_low else None,
        expires_at=expires_at,
    )


class TestGetOrderSide:
    def test_long_maps_to_buy(self) -> None:
        assert get_order_side(SignalDirection.LONG) == OrderSide.BUY

    def test_short_maps_to_sell(self) -> None:
        assert get_order_side(SignalDirection.SHORT) == OrderSide.SELL


class TestGetEntryOrderType:
    def test_use_limit_false_always_returns_market(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert get_entry_order_type(signal, use_limit=False) == OrderType.MARKET

    def test_use_limit_true_with_a_zone_returns_limit(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert get_entry_order_type(signal, use_limit=True) == OrderType.LIMIT

    def test_use_limit_true_without_a_zone_returns_market(self) -> None:
        signal = make_signal()  # no entry zone
        assert get_entry_order_type(signal, use_limit=True) == OrderType.MARKET

    def test_defaults_to_use_limit_true(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert get_entry_order_type(signal) == OrderType.LIMIT


class TestIsEntryValid:
    def test_false_when_signal_is_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        signal = make_signal(expires_at=past)
        assert is_entry_valid(signal, Decimal("50000")) is False

    def test_true_within_an_entry_zone(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert is_entry_valid(signal, Decimal("50000")) is True

    def test_false_outside_an_entry_zone(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert is_entry_valid(signal, Decimal("50200")) is False

    def test_zone_boundaries_are_inclusive(self) -> None:
        signal = make_signal(entry_zone_high="50100", entry_zone_low="49900")
        assert is_entry_valid(signal, Decimal("50100")) is True
        assert is_entry_valid(signal, Decimal("49900")) is True

    def test_without_a_zone_uses_default_0_3_pct_tolerance(self) -> None:
        signal = make_signal(entry="50000")
        # tolerance = 50000 * 0.003 = 150
        assert is_entry_valid(signal, Decimal("50150")) is True
        assert is_entry_valid(signal, Decimal("50151")) is False

    def test_custom_tolerance_overrides_the_default(self) -> None:
        signal = make_signal(entry="50000")
        assert is_entry_valid(signal, Decimal("50500"), tolerance_atr=Decimal("1000")) is True
        assert is_entry_valid(signal, Decimal("50500"), tolerance_atr=Decimal("10")) is False
