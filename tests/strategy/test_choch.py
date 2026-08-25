"""Unit tests for src.smart_money.choch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import MarketStructure
from src.core.models import OHLCV
from src.smart_money.choch import detect_choch, latest_choch
from src.smart_money.market_structure import MarketStructureState, SwingPoint


def candle(o: str, h: str, low: str, c: str, index: int = 0) -> OHLCV:
    return OHLCV(
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("100"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
    )


def swing(price: str, bar_index: int, is_high: bool) -> SwingPoint:
    return SwingPoint(
        price=Decimal(price),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bar_index=bar_index,
        is_high=is_high,
    )


class TestUnknownStructure:
    def test_returns_empty_list_immediately(self) -> None:
        structure = MarketStructureState(structure=MarketStructure.UNKNOWN)
        candles = [candle("100", "102", "99", "101", i) for i in range(5)]
        assert detect_choch(candles, structure) == []


class TestBearishChochInBullishStructure:
    def test_close_below_a_higher_low_triggers_bearish_choch(self) -> None:
        hl = swing("100", bar_index=2, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [
            candle("105", "106", "104", "105", 0),
            candle("104", "106", "103", "104", 1),
            candle("101", "103", "100", "101", 2),  # the swing low itself
            candle("100", "101", "97", "98", 3),  # closes below 100 -> CHoCH
        ]
        events = detect_choch(candles, structure)
        assert len(events) == 1
        assert events[0].direction == "bearish"
        assert events[0].broken_price == Decimal("100")
        assert events[0].break_candle_index == 3
        assert events[0].prior_structure == MarketStructure.BULLISH

    def test_close_above_the_swing_low_does_not_trigger(self) -> None:
        hl = swing("100", bar_index=2, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [
            candle("105", "106", "104", "105", 0),
            candle("104", "106", "103", "104", 1),
            candle("101", "103", "100", "101", 2),
            candle("102", "104", "100.5", "103", 3),  # still above 100
        ]
        assert detect_choch(candles, structure) == []

    def test_a_swing_low_only_counts_once_it_is_in_the_past(self) -> None:
        # sl.bar_index >= i guard: the swing itself, or a swing formed
        # after the current candle, must not be checked against.
        hl = swing("100", bar_index=5, is_high=False)  # future relative to candle 0
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [candle("90", "92", "89", "91", 0)]  # closes below 100 but sl is at index 5
        assert detect_choch(candles, structure) == []


class TestBullishChochInBearishStructure:
    def test_close_above_a_lower_high_triggers_bullish_choch(self) -> None:
        lh = swing("100", bar_index=2, is_high=True)
        structure = MarketStructureState(structure=MarketStructure.BEARISH, swing_highs=[lh])
        candles = [
            candle("95", "96", "94", "95", 0),
            candle("96", "98", "95", "97", 1),
            candle("99", "100", "97", "99", 2),  # the swing high itself
            candle("100", "102", "99", "101", 3),  # closes above 100 -> CHoCH
        ]
        events = detect_choch(candles, structure)
        assert len(events) == 1
        assert events[0].direction == "bullish"
        assert events[0].broken_price == Decimal("100")
        assert events[0].prior_structure == MarketStructure.BEARISH

    def test_close_below_the_swing_high_does_not_trigger(self) -> None:
        lh = swing("100", bar_index=2, is_high=True)
        structure = MarketStructureState(structure=MarketStructure.BEARISH, swing_highs=[lh])
        candles = [
            candle("95", "96", "94", "95", 0),
            candle("96", "98", "95", "97", 1),
            candle("99", "100", "97", "99", 2),
            candle("98", "99.5", "97", "98.5", 3),  # still below 100
        ]
        assert detect_choch(candles, structure) == []


class TestRequireCloseParameter:
    def test_require_close_false_uses_wick_extremes_instead_of_close(self) -> None:
        hl = swing("100", bar_index=1, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        # Close stays above 100, but the low wick dips below it.
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("102", "103", "100", "101", 1),
            candle("101", "102", "98", "100.5", 2),  # low=98 < 100, close=100.5 > 100
        ]
        require_close_true = detect_choch(candles, structure, require_close=True)
        require_close_false = detect_choch(candles, structure, require_close=False)
        assert require_close_true == []
        assert len(require_close_false) == 1


class TestBrokenSwingSkipGuard:
    def test_a_swing_is_only_reported_broken_once(self) -> None:
        hl = swing("100", bar_index=1, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("101", "102", "100", "100.5", 1),
            candle("99", "100", "97", "98", 2),  # breaks below 100
            candle("97", "98", "95", "96", 3),  # still below 100, same swing already broken
        ]
        events = detect_choch(candles, structure)
        assert len(events) == 1  # not reported again on candle 3


class TestMultipleSwingsAndOrdering:
    def test_events_are_returned_sorted_by_break_candle_index(self) -> None:
        hl1 = swing("100", bar_index=1, is_high=False)
        hl2 = swing("95", bar_index=3, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl1, hl2])
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("101", "102", "100", "100.5", 1),
            candle("99", "100", "96", "97", 2),  # breaks hl1 (100)
            candle("96", "97", "94", "95.5", 3),
            candle("94", "95", "90", "91", 4),  # breaks hl2 (95)
        ]
        events = detect_choch(candles, structure)
        assert len(events) == 2
        assert [e.break_candle_index for e in events] == [2, 4]


class TestLatestChoch:
    def test_returns_none_when_no_choch_detected(self) -> None:
        structure = MarketStructureState(structure=MarketStructure.UNKNOWN)
        candles = [candle("100", "101", "99", "100", 0)]
        assert latest_choch(candles, structure) is None

    def test_returns_the_most_recent_event(self) -> None:
        hl1 = swing("100", bar_index=1, is_high=False)
        hl2 = swing("95", bar_index=3, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl1, hl2])
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("101", "102", "100", "100.5", 1),
            candle("99", "100", "96", "97", 2),
            candle("96", "97", "94", "95.5", 3),
            candle("94", "95", "90", "91", 4),
        ]
        result = latest_choch(candles, structure)
        assert result is not None
        assert result.break_candle_index == 4


class TestCHoCHEventProperties:
    def test_is_bullish_and_is_bearish_reflect_direction(self) -> None:
        hl = swing("100", bar_index=1, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("101", "102", "100", "100.5", 1),
            candle("99", "100", "96", "97", 2),
        ]
        event = detect_choch(candles, structure)[0]
        assert event.is_bearish is True
        assert event.is_bullish is False

    def test_str_includes_direction_and_broken_price(self) -> None:
        hl = swing("100", bar_index=1, is_high=False)
        structure = MarketStructureState(structure=MarketStructure.BULLISH, swing_lows=[hl])
        candles = [
            candle("103", "104", "101", "102", 0),
            candle("101", "102", "100", "100.5", 1),
            candle("99", "100", "96", "97", 2),
        ]
        event = detect_choch(candles, structure)[0]
        text = str(event)
        assert "BEARISH" in text
        assert "100" in text
