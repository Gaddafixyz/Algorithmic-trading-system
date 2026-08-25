"""Unit tests for src.smart_money.inducement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import LiquidityType
from src.core.models import OHLCV
from src.smart_money.inducement import detect_inducement
from src.smart_money.liquidity import LiquidityPool


def candle(o: str, h: str, low: str, c: str, index: int = 0) -> OHLCV:
    return OHLCV(
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("100"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
    )


def make_pool(
    liq_type: LiquidityType,
    price: str,
    bar_index: int = 0,
    is_swept: bool = False,
) -> LiquidityPool:
    return LiquidityPool(
        liq_type=liq_type,
        price=Decimal(price),
        zone_top=Decimal(price) + Decimal("10"),
        zone_bottom=Decimal(price) - Decimal("10"),
        bar_index=bar_index,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        is_swept=is_swept,
    )


class TestBearishInducement:
    def test_wick_above_buy_side_liquidity_with_rejection_close_triggers(self) -> None:
        # body = |98 - 99| = 1; upper_wick = 105 - 99 = 6; ratio 6.0 >= 2.0
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),  # sweeps above 100, closes below
        ]
        events = detect_inducement(candles, [pool])
        assert len(events) == 1
        assert events[0].direction == "bearish_inducement"
        assert events[0].swept_price == Decimal("100")
        assert events[0].bar_index == 1

    def test_marks_the_pool_as_swept_with_sweep_metadata(self) -> None:
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),
        ]
        detect_inducement(candles, [pool])
        assert pool.is_swept is True
        assert pool.sweep_bar_index == 1
        assert pool.sweep_price == Decimal("105")

    def test_insufficient_wick_to_body_ratio_does_not_trigger(self) -> None:
        # body = |95 - 99| = 4; upper_wick = 100.5 - 99 = 1.5; ratio = 0.375 < 2.0
        pool = make_pool(LiquidityType.BSL, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("95", "100.5", "94.9", "99", 1),
        ]
        events = detect_inducement(candles, [pool])
        assert events == []


class TestBullishInducement:
    def test_wick_below_sell_side_liquidity_with_rejection_close_triggers(self) -> None:
        # body = |102 - 101| = 1; lower_wick = 101 - 95 = 6; ratio 6.0 >= 2.0
        pool = make_pool(LiquidityType.EQUAL_LOWS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("102", "103", "95", "101", 1),  # sweeps below 100, closes above
        ]
        events = detect_inducement(candles, [pool])
        assert len(events) == 1
        assert events[0].direction == "bullish_inducement"
        assert events[0].swept_price == Decimal("100")

    def test_marks_the_pool_as_swept_with_sweep_low_price(self) -> None:
        pool = make_pool(LiquidityType.SSL, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("102", "103", "95", "101", 1),
        ]
        detect_inducement(candles, [pool])
        assert pool.is_swept is True
        assert pool.sweep_price == Decimal("95")


class TestGuardClauses:
    def test_already_swept_pool_is_skipped(self) -> None:
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0, is_swept=True)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),
        ]
        events = detect_inducement(candles, [pool])
        assert events == []

    def test_pool_formed_at_or_after_the_current_bar_is_skipped(self) -> None:
        # bar_index >= i guard: a pool that formed on/after the candle
        # being examined cannot have been swept by it (would be
        # look-ahead / non-causal).
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=1)
        candles = [
            candle("98", "105", "97.5", "99", 0),  # would otherwise sweep, but pool formed later
        ]
        events = detect_inducement(candles, [pool])
        assert events == []

    def test_doji_candle_uses_1_pct_of_range_as_body_substitute(self) -> None:
        # A true doji (open == close) would divide-by-zero-ish reasoning
        # without a substitute; verify it doesn't crash and produces a
        # sensible result rather than a spurious always-true match.
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("99", "105", "98", "99", 1),  # open == close == 99 (doji)
        ]
        # range_size = 105-98 = 7; substitute body = 0.07
        # upper_wick = 105 - 99 = 6; ratio = 6/0.07 ≈ 85.7 >= 2.0 -> triggers
        events = detect_inducement(candles, [pool])
        assert len(events) == 1


class TestMultiplePoolsAndCandles:
    def test_only_matching_side_pools_are_considered(self) -> None:
        # A sell-side pool must not trigger a bearish-inducement check
        # (that requires is_buy_side), and vice versa.
        sell_side_pool = make_pool(LiquidityType.EQUAL_LOWS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),  # sweeps above 100 (buy-side pattern)
        ]
        events = detect_inducement(candles, [sell_side_pool])
        assert events == []  # sell-side pool doesn't match a buy-side-style sweep

    def test_processes_multiple_independent_pools(self) -> None:
        buy_pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        sell_pool = make_pool(LiquidityType.EQUAL_LOWS, price="90", bar_index=0)
        candles = [
            candle("95", "96", "94", "95.5", 0),
            candle("98", "105", "97.5", "99", 1),  # bearish inducement vs buy_pool
            candle("92", "93", "85", "91", 2),  # bullish inducement vs sell_pool
        ]
        events = detect_inducement(candles, [buy_pool, sell_pool])
        directions = {e.direction for e in events}
        assert "bearish_inducement" in directions
        assert "bullish_inducement" in directions


class TestInducementEventProperties:
    def test_is_bullish_and_is_bearish_reflect_direction(self) -> None:
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),
        ]
        event = detect_inducement(candles, [pool])[0]
        assert event.is_bearish is True
        assert event.is_bullish is False

    def test_str_includes_direction_and_swept_price(self) -> None:
        pool = make_pool(LiquidityType.EQUAL_HIGHS, price="100", bar_index=0)
        candles = [
            candle("100", "101", "99", "100.5", 0),
            candle("98", "105", "97.5", "99", 1),
        ]
        event = detect_inducement(candles, [pool])[0]
        text = str(event)
        assert "bearish_inducement" in text
        assert "100" in text
