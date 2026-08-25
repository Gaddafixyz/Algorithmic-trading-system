"""Unit tests for src.smart_money.order_blocks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.constants import SMC_OB_MAX_AGE_BARS
from src.core.enums import OrderBlockType
from src.core.models import OHLCV
from src.smart_money.bos import BOSEvent
from src.smart_money.market_structure import SwingPoint
from src.smart_money.order_blocks import (
    OrderBlock,
    get_active_order_blocks,
    identify_order_blocks,
    update_order_block_mitigation,
)


def candle(o: str, h: str, low: str, c: str, index: int = 0) -> OHLCV:
    return OHLCV(
        open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c),
        volume=Decimal("100"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
    )


def make_bos(direction: str, break_candle_index: int) -> BOSEvent:
    swing = SwingPoint(
        price=Decimal("100"), timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bar_index=0, is_high=(direction == "bearish"),
    )
    return BOSEvent(
        direction=direction, broken_price=Decimal("100"),
        break_candle_index=break_candle_index, break_price=Decimal("100"),
        swing_point=swing, timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_ob(
    ob_type: OrderBlockType = OrderBlockType.BULLISH,
    top: str = "100", bottom: str = "98", bar_index: int = 5,
) -> OrderBlock:
    return OrderBlock(
        ob_type=ob_type, top=Decimal(top), bottom=Decimal(bottom),
        origin_high=Decimal(top), origin_low=Decimal(bottom),
        bar_index=bar_index, timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bos_event=make_bos("bullish" if ob_type == OrderBlockType.BULLISH else "bearish", 10),
    )


class TestOrderBlockProperties:
    def test_mid_is_the_average_of_top_and_bottom(self) -> None:
        ob = make_ob(top="102", bottom="98")
        assert ob.mid == Decimal("100")

    def test_size_is_top_minus_bottom(self) -> None:
        ob = make_ob(top="102", bottom="98")
        assert ob.size == Decimal("4")

    def test_contains_price_true_within_bounds_inclusive(self) -> None:
        ob = make_ob(top="102", bottom="98")
        assert ob.contains_price(Decimal("98")) is True
        assert ob.contains_price(Decimal("102")) is True
        assert ob.contains_price(Decimal("100")) is True

    def test_contains_price_false_outside_bounds(self) -> None:
        ob = make_ob(top="102", bottom="98")
        assert ob.contains_price(Decimal("97")) is False
        assert ob.contains_price(Decimal("103")) is False


class TestIsValid:
    def test_false_when_already_mitigated(self) -> None:
        ob = make_ob(bar_index=5)
        ob.is_mitigated = True
        assert ob.is_valid(current_bar_index=6) is False

    def test_true_within_max_age(self) -> None:
        ob = make_ob(bar_index=5)
        assert ob.is_valid(current_bar_index=5 + SMC_OB_MAX_AGE_BARS) is True

    def test_false_beyond_max_age(self) -> None:
        ob = make_ob(bar_index=5)
        assert ob.is_valid(current_bar_index=5 + SMC_OB_MAX_AGE_BARS + 1) is False


class TestIsValidCustomMaxAge:
    """
    max_age_bars — previously is_valid() always compared against the
    hardcoded SMC_OB_MAX_AGE_BARS module constant with no way to
    override it. Now an optional param (None -> same constant, so
    default behavior is unchanged) — see
    configs/strategy.yaml's `smc.ob_max_age_bars`.
    """

    def test_none_matches_default_constant_behavior(self) -> None:
        ob = make_ob(bar_index=5)
        current = 5 + SMC_OB_MAX_AGE_BARS
        assert ob.is_valid(current, max_age_bars=None) == ob.is_valid(current)

    def test_custom_max_age_extends_validity_window(self) -> None:
        ob = make_ob(bar_index=5)
        current = 5 + SMC_OB_MAX_AGE_BARS + 50
        # Beyond the default constant...
        assert ob.is_valid(current) is False
        # ...but still within a custom, wider window.
        assert ob.is_valid(current, max_age_bars=SMC_OB_MAX_AGE_BARS + 100) is True

    def test_custom_max_age_shrinks_validity_window(self) -> None:
        ob = make_ob(bar_index=5)
        current = 5 + 40
        # Within the default constant...
        assert ob.is_valid(current) is True
        # ...but beyond a custom, tighter window.
        assert ob.is_valid(current, max_age_bars=30) is False

    def test_mitigated_ob_still_invalid_regardless_of_custom_max_age(self) -> None:
        ob = make_ob(bar_index=5)
        ob.is_mitigated = True
        assert ob.is_valid(current_bar_index=6, max_age_bars=1000) is False


class TestCheckMitigationBullish:
    def test_price_at_or_below_the_50_pct_level_mitigates(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98")  # 50% = 100
        c = candle("101", "101", "99", "100", index=10)
        result = ob.check_mitigation(c, bar_index=10)
        assert result is True
        assert ob.is_mitigated is True
        assert ob.mitigation_bar_index == 10
        assert ob.mitigation_price == Decimal("99")

    def test_price_above_the_50_pct_level_does_not_mitigate(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98")
        c = candle("101", "102", "100.5", "101", index=10)  # low=100.5 > 100
        result = ob.check_mitigation(c, bar_index=10)
        assert result is False
        assert ob.is_mitigated is False

    def test_already_mitigated_short_circuits_to_true(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98")
        ob.is_mitigated = True
        ob.mitigation_bar_index = 5
        c = candle("200", "201", "199", "200", index=99)  # irrelevant price
        result = ob.check_mitigation(c, bar_index=99)
        assert result is True
        assert ob.mitigation_bar_index == 5  # unchanged, not overwritten


class TestCheckMitigationBearish:
    def test_price_at_or_above_the_50_pct_level_mitigates(self) -> None:
        ob = make_ob(OrderBlockType.BEARISH, top="102", bottom="98")  # 50% = 100
        c = candle("99", "100", "98", "99", index=10)
        result = ob.check_mitigation(c, bar_index=10)
        assert result is True
        assert ob.mitigation_price == Decimal("100")

    def test_price_below_the_50_pct_level_does_not_mitigate(self) -> None:
        ob = make_ob(OrderBlockType.BEARISH, top="102", bottom="98")
        c = candle("98", "99.5", "97", "98", index=10)  # high=99.5 < 100
        result = ob.check_mitigation(c, bar_index=10)
        assert result is False


class TestIdentifyOrderBlocksBullish:
    def test_finds_the_last_bearish_candle_before_a_bullish_bos(self) -> None:
        candles = [
            candle("100", "101", "99", "99.5", 0),   # bearish (close < open)
            candle("99.5", "100.5", "99", "100", 1),  # bullish
            candle("100", "108", "99.8", "107", 2),   # the impulsive move
        ]
        bos = make_bos("bullish", break_candle_index=2)
        result = identify_order_blocks(candles, [bos])
        assert len(result) == 1
        assert result[0].ob_type == OrderBlockType.BULLISH
        assert result[0].bar_index == 0  # the bearish candle
        assert result[0].top == Decimal("100")   # max(open, close)
        assert result[0].bottom == Decimal("99.5")  # min(open, close)

    def test_no_bearish_candle_found_within_lookback_produces_nothing(self) -> None:
        # All candles bullish -> no OB candle available.
        candles = [
            candle("99", "100", "98.5", "99.8", 0),
            candle("99.8", "101", "99.5", "100.5", 1),
            candle("100.5", "108", "100", "107", 2),
        ]
        bos = make_bos("bullish", break_candle_index=2)
        result = identify_order_blocks(candles, [bos])
        assert result == []


class TestIdentifyOrderBlocksBearish:
    def test_finds_the_last_bullish_candle_before_a_bearish_bos(self) -> None:
        candles = [
            candle("99", "101", "98.5", "100.5", 0),  # bullish (close > open)
            candle("100.5", "101", "99", "99.5", 1),   # bearish
            candle("99.5", "100", "92", "93", 2),      # the impulsive move down
        ]
        bos = make_bos("bearish", break_candle_index=2)
        result = identify_order_blocks(candles, [bos])
        assert len(result) == 1
        assert result[0].ob_type == OrderBlockType.BEARISH
        assert result[0].bar_index == 0


class TestIdentifyOrderBlocksLookbackWindow:
    def test_respects_max_lookback_and_does_not_search_further_back(self) -> None:
        # The only bearish candle is 3 bars before the BOS, but
        # max_lookback=1 restricts the search to 1 bar back.
        candles = [
            candle("100", "101", "99", "99.5", 0),    # bearish, but too far back
            candle("99.5", "100.5", "99", "100", 1),  # bullish
            candle("100", "100.8", "99.8", "100.5", 2),  # bullish
            candle("100.5", "108", "100.3", "107", 3),   # the BOS move
        ]
        bos = make_bos("bullish", break_candle_index=3)
        result = identify_order_blocks(candles, [bos], max_lookback=1)
        assert result == []


class TestIdentifyOrderBlocksDeduplication:
    def test_two_bos_events_pointing_to_the_same_ob_candle_produce_one_ob(self) -> None:
        candles = [
            candle("100", "101", "99", "99.5", 0),   # bearish OB candle
            candle("99.5", "100.5", "99", "100", 1),
            candle("100", "108", "99.8", "107", 2),
            candle("107", "115", "106.8", "114", 3),
        ]
        bos_a = make_bos("bullish", break_candle_index=2)
        bos_b = make_bos("bullish", break_candle_index=3)
        result = identify_order_blocks(candles, [bos_a, bos_b], max_lookback=5)
        # Both BOS events scan backward and would find the same bar-0
        # candle; dedup means only one OB is produced for that bar.
        bar_indices = [ob.bar_index for ob in result]
        assert len(bar_indices) == len(set(bar_indices))


class TestIdentifyOrderBlocksSortOrder:
    def test_results_are_sorted_most_recent_bar_first(self) -> None:
        candles = [
            candle("100", "101", "99", "99.5", 0),
            candle("99.5", "100.5", "99", "100", 1),
            candle("100", "101.5", "99.8", "99.9", 2),  # bearish
            candle("99.9", "108", "99.7", "107", 3),
            candle("107", "115", "106.8", "114", 4),
        ]
        bos_early = make_bos("bullish", break_candle_index=1)
        bos_late = make_bos("bullish", break_candle_index=3)
        result = identify_order_blocks(candles, [bos_early, bos_late], max_lookback=5)
        assert len(result) >= 2
        bar_indices = [ob.bar_index for ob in result]
        assert bar_indices == sorted(bar_indices, reverse=True)


class TestUpdateOrderBlockMitigation:
    def test_marks_an_ob_mitigated_when_a_later_candle_crosses_the_threshold(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98", bar_index=0)  # 50% = 100
        candles = [
            candle("100", "101", "99", "100", 0),
            candle("100.5", "101", "100.2", "100.8", 1),  # low=100.2 > 100: not yet mitigated
            candle("100.5", "101", "99", "99.5", 2),      # low=99 <= 100 -> mitigated here
        ]
        update_order_block_mitigation([ob], candles)
        assert ob.is_mitigated is True
        assert ob.mitigation_bar_index == 2

    def test_stops_scanning_once_mitigated_does_not_overwrite_later(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98", bar_index=0)
        candles = [
            candle("100", "101", "99", "100", 0),
            candle("100", "101", "99", "99.5", 1),   # mitigates here first
            candle("100", "101", "50", "60", 2),     # would also mitigate, must not overwrite
        ]
        update_order_block_mitigation([ob], candles)
        assert ob.mitigation_bar_index == 1

    def test_already_mitigated_obs_are_skipped_entirely(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98", bar_index=0)
        ob.is_mitigated = True
        ob.mitigation_bar_index = 0
        candles = [candle("100", "101", "50", "60", 5)]
        update_order_block_mitigation([ob], candles)
        assert ob.mitigation_bar_index == 0  # unchanged

    def test_respects_the_start_bar_parameter(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="102", bottom="98", bar_index=0)  # 50% = 100
        candles = [
            candle("100", "101", "99", "100", 0),
            candle("100", "101", "50", "60", 1),          # would mitigate but skipped by start_bar
            candle("100.5", "101", "100.2", "100.8", 2),  # low=100.2 > 100: genuinely no mitigation
        ]
        update_order_block_mitigation([ob], candles, start_bar=2)
        assert ob.is_mitigated is False


class TestGetActiveOrderBlocks:
    def test_excludes_mitigated_obs(self) -> None:
        active_ob = make_ob(bar_index=5)
        mitigated_ob = make_ob(bar_index=6)
        mitigated_ob.is_mitigated = True
        result = get_active_order_blocks([active_ob, mitigated_ob], current_bar_index=7)
        assert result == [active_ob]

    def test_excludes_obs_beyond_max_age(self) -> None:
        old_ob = make_ob(bar_index=0)
        result = get_active_order_blocks([old_ob], current_bar_index=SMC_OB_MAX_AGE_BARS + 100)
        assert result == []

    def test_custom_max_age_bars_threaded_through(self) -> None:
        ob = make_ob(bar_index=0)
        # Beyond the default constant, but within a custom wider window.
        result = get_active_order_blocks(
            [ob],
            current_bar_index=SMC_OB_MAX_AGE_BARS + 50,
            max_age_bars=SMC_OB_MAX_AGE_BARS + 100,
        )
        assert result == [ob]

    def test_none_max_age_bars_matches_default(self) -> None:
        ob = make_ob(bar_index=0)
        current = SMC_OB_MAX_AGE_BARS + 100
        assert get_active_order_blocks([ob], current, max_age_bars=None) == \
            get_active_order_blocks([ob], current)
