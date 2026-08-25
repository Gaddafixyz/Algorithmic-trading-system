"""Unit tests for src.smart_money.imbalance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.models import OHLCV
from src.smart_money.imbalance import detect_imbalances


def candle(o: str, h: str, low: str, c: str, index: int = 0) -> OHLCV:
    return OHLCV(
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("100"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
    )


class TestBullishImbalance:
    def test_detects_a_gap_where_c3_body_bottom_exceeds_c1_body_top(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)  # body top = 101
        c2 = candle("101", "108", "100", "107", 1)  # the impulse candle
        c3 = candle("110", "112", "109", "111", 2)  # body bottom = 110 > 101

        result = detect_imbalances([c1, c2, c3])
        assert len(result) == 1
        assert result[0].direction == "bullish"
        assert result[0].top == Decimal("110")
        assert result[0].bottom == Decimal("101")

    def test_bar_index_and_timestamp_reference_the_middle_candle(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)

        result = detect_imbalances([c1, c2, c3])
        assert result[0].bar_index == 1
        assert result[0].timestamp == c2.timestamp


class TestBearishImbalance:
    def test_detects_a_gap_where_c3_body_top_is_below_c1_body_bottom(self) -> None:
        c1 = candle("110", "112", "109", "111", 0)  # body bottom = 110
        c2 = candle("109", "110", "102", "103", 1)  # the impulse candle
        c3 = candle("100", "101", "98", "99", 2)  # body top = 100 < 110

        result = detect_imbalances([c1, c2, c3])
        assert len(result) == 1
        assert result[0].direction == "bearish"
        assert result[0].top == Decimal("110")
        assert result[0].bottom == Decimal("100")


class TestNoImbalance:
    def test_overlapping_bodies_produce_no_imbalance(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "103", "100", "102", 1)
        c3 = candle("100.5", "103", "99.5", "101", 2)  # body overlaps c1's body

        result = detect_imbalances([c1, c2, c3])
        assert result == []

    def test_fewer_than_3_candles_returns_empty(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "103", "100", "102", 1)
        assert detect_imbalances([c1, c2]) == []
        assert detect_imbalances([c1]) == []
        assert detect_imbalances([]) == []


class TestMultipleImbalances:
    def test_detects_more_than_one_gap_in_a_longer_series(self) -> None:
        candles = [
            candle("100", "102", "99", "101", 0),
            candle("101", "108", "100", "107", 1),
            candle("110", "112", "109", "111", 2),  # bullish gap vs candle 0
            candle("111", "113", "110", "112", 3),
            candle("115", "120", "114", "119", 4),  # another impulse
            candle("125", "127", "124", "126", 5),  # bullish gap vs candle 3
        ]
        result = detect_imbalances(candles)
        assert len(result) >= 1
        assert all(imb.direction == "bullish" for imb in result)


class TestImbalanceProperties:
    def test_size_is_top_minus_bottom(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)
        result = detect_imbalances([c1, c2, c3])
        assert result[0].size == Decimal("110") - Decimal("101")

    def test_mid_is_the_midpoint(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)
        result = detect_imbalances([c1, c2, c3])
        assert result[0].mid == (Decimal("110") + Decimal("101")) / Decimal("2")

    def test_contains_price_true_within_the_zone(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)
        result = detect_imbalances([c1, c2, c3])
        assert result[0].contains_price(Decimal("105")) is True

    def test_contains_price_false_outside_the_zone(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)
        result = detect_imbalances([c1, c2, c3])
        assert result[0].contains_price(Decimal("200")) is False

    def test_is_filled_defaults_to_false(self) -> None:
        c1 = candle("100", "102", "99", "101", 0)
        c2 = candle("101", "108", "100", "107", 1)
        c3 = candle("110", "112", "109", "111", 2)
        result = detect_imbalances([c1, c2, c3])
        assert result[0].is_filled is False
