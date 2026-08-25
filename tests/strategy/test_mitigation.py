"""Unit tests for src.smart_money.mitigation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.core.enums import OrderBlockType
from src.smart_money.bos import BOSEvent
from src.smart_money.market_structure import SwingPoint
from src.smart_money.mitigation import extract_mitigation_blocks
from src.smart_money.order_blocks import OrderBlock


def make_bos(direction: str = "bullish") -> BOSEvent:
    swing = SwingPoint(
        price=Decimal("49500"),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bar_index=0,
        is_high=(direction == "bearish"),
    )
    return BOSEvent(
        direction=direction,
        broken_price=Decimal("49500"),
        break_candle_index=5,
        break_price=Decimal("49500"),
        swing_point=swing,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_ob(
    ob_type: OrderBlockType,
    top: str,
    bottom: str,
    is_mitigated: bool = False,
    mitigation_price: str | None = None,
) -> OrderBlock:
    return OrderBlock(
        ob_type=ob_type,
        top=Decimal(top),
        bottom=Decimal(bottom),
        origin_high=Decimal(top),
        origin_low=Decimal(bottom),
        bar_index=10,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bos_event=make_bos("bullish" if ob_type == OrderBlockType.BULLISH else "bearish"),
        is_mitigated=is_mitigated,
        mitigation_price=Decimal(mitigation_price) if mitigation_price else None,
    )


class TestBullishMitigation:
    def test_remaining_zone_is_above_the_mitigation_price(self) -> None:
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49900",
        )
        result = extract_mitigation_blocks([ob])
        assert len(result) == 1
        assert result[0].remaining_zone_top == Decimal("50000")
        assert result[0].remaining_zone_bottom == Decimal("49900")

    def test_mitigation_price_below_ob_bottom_uses_the_ob_bottom(self) -> None:
        # max(ob.bottom, ob.mitigation_price) -- if mitigation_price is
        # somehow below the OB's own bottom, the remaining zone should
        # not extend past the OB's actual boundary.
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49700",
        )
        result = extract_mitigation_blocks([ob])
        assert result[0].remaining_zone_bottom == Decimal("49800")


class TestBearishMitigation:
    def test_remaining_zone_is_below_the_mitigation_price(self) -> None:
        ob = make_ob(
            OrderBlockType.BEARISH,
            top="50200",
            bottom="50000",
            is_mitigated=True,
            mitigation_price="50100",
        )
        result = extract_mitigation_blocks([ob])
        assert len(result) == 1
        assert result[0].remaining_zone_top == Decimal("50100")
        assert result[0].remaining_zone_bottom == Decimal("50000")

    def test_mitigation_price_above_ob_top_uses_the_ob_top(self) -> None:
        ob = make_ob(
            OrderBlockType.BEARISH,
            top="50200",
            bottom="50000",
            is_mitigated=True,
            mitigation_price="50300",
        )
        result = extract_mitigation_blocks([ob])
        assert result[0].remaining_zone_top == Decimal("50200")


class TestGuardClauses:
    def test_unmitigated_ob_is_skipped(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800", is_mitigated=False)
        assert extract_mitigation_blocks([ob]) == []

    def test_mitigated_ob_with_no_mitigation_price_is_skipped(self) -> None:
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price=None,
        )
        assert extract_mitigation_blocks([ob]) == []

    def test_degenerate_zero_size_remaining_zone_is_excluded(self) -> None:
        # mitigation_price exactly at the OB top means remaining_top ==
        # remaining_bottom == top, which fails the `remaining_top >
        # remaining_bottom` guard (not >=) and must be excluded rather
        # than producing a zero-size MitigationBlock.
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="50000",
        )
        assert extract_mitigation_blocks([ob]) == []


class TestMultipleBlocks:
    def test_processes_a_mix_of_mitigated_and_unmitigated_obs(self) -> None:
        mitigated = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49900",
        )
        unmitigated = make_ob(OrderBlockType.BEARISH, top="51000", bottom="50800")
        result = extract_mitigation_blocks([mitigated, unmitigated])
        assert len(result) == 1
        assert result[0].original_ob is mitigated


class TestMitigationBlockProperties:
    def test_zone_size_is_top_minus_bottom(self) -> None:
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49900",
        )
        result = extract_mitigation_blocks([ob])[0]
        assert result.zone_size == Decimal("100")

    def test_contains_price_true_within_the_remaining_zone(self) -> None:
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49900",
        )
        result = extract_mitigation_blocks([ob])[0]
        assert result.contains_price(Decimal("49950")) is True

    def test_contains_price_false_outside_the_remaining_zone(self) -> None:
        ob = make_ob(
            OrderBlockType.BULLISH,
            top="50000",
            bottom="49800",
            is_mitigated=True,
            mitigation_price="49900",
        )
        result = extract_mitigation_blocks([ob])[0]
        assert result.contains_price(Decimal("49850")) is False
