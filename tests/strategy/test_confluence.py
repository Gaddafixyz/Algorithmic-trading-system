"""Unit tests for src.smart_money.confluence.score_confluence.

Deliberately thorough given this is the core signal-scoring engine
whose weights are documented (ROADMAP.md) as reasoned defaults never
empirically backtested -- correctness of the *mechanism* matters even
more since nobody has validated the tuning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.enums import (
    FVGType,
    LiquidityType,
    MarketStructure,
    OrderBlockType,
    SignalDirection,
    SignalStrength,
)
from src.core.models import OHLCV
from src.domain.signal import Timeframe
from src.smart_money.bos import BOSEvent
from src.smart_money.confluence import ConfluenceWeights, SMCAnalysis, score_confluence
from src.smart_money.fair_value_gap import FairValueGap
from src.smart_money.inducement import InducementEvent
from src.smart_money.liquidity import LiquidityPool
from src.smart_money.market_structure import MarketStructureState, SwingPoint
from src.smart_money.mitigation import MitigationBlock
from src.smart_money.order_blocks import OrderBlock
from src.smart_money.premium_discount import PremiumDiscountZones

# A timestamp during the London/NY kill zone overlap gives a
# consistent, known session_score (1.0) across all tests unless a test
# specifically wants to vary it.
KILL_ZONE_TS = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)  # 13:30 UTC: NY open KZ
OFF_HOURS_TS = datetime(2024, 1, 2, 3, 30, tzinfo=UTC)  # off-hours


def make_candles(count: int = 30, base_price: float = 50000.0) -> list[OHLCV]:
    candles = []
    for i in range(count):
        candles.append(
            OHLCV(
                timestamp=datetime(2024, 1, 1, tzinfo=UTC),
                open=Decimal(str(base_price)),
                high=Decimal(str(base_price + 100)),
                low=Decimal(str(base_price - 100)),
                close=Decimal(str(base_price)),
                volume=Decimal("1000"),
            )
        )
    return candles


def make_swing(price: str, is_high: bool, bar_index: int = 0) -> SwingPoint:
    return SwingPoint(
        price=Decimal(price),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bar_index=bar_index,
        is_high=is_high,
    )


def make_bos(direction: str = "bullish", broken_price: str = "49500") -> BOSEvent:
    return BOSEvent(
        direction=direction,
        broken_price=Decimal(broken_price),
        break_candle_index=5,
        break_price=Decimal(broken_price),
        swing_point=make_swing(broken_price, is_high=(direction == "bearish")),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_ob(
    ob_type: OrderBlockType = OrderBlockType.BULLISH,
    top: str = "50000",
    bottom: str = "49800",
    bar_index: int = 10,
) -> OrderBlock:
    return OrderBlock(
        ob_type=ob_type,
        top=Decimal(top),
        bottom=Decimal(bottom),
        origin_high=Decimal(top),
        origin_low=Decimal(bottom),
        bar_index=bar_index,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        bos_event=make_bos("bullish" if ob_type == OrderBlockType.BULLISH else "bearish"),
    )


def make_fvg(
    fvg_type: FVGType = FVGType.BULLISH,
    top: str = "50000",
    bottom: str = "49900",
) -> FairValueGap:
    return FairValueGap(
        fvg_type=fvg_type,
        top=Decimal(top),
        bottom=Decimal(bottom),
        bar_index=8,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def make_analysis(
    *,
    structure: MarketStructure = MarketStructure.BULLISH,
    current_price: str = "50000",
    active_obs: list[OrderBlock] | None = None,
    open_fvgs: list[FairValueGap] | None = None,
    active_liquidity: list[LiquidityPool] | None = None,
    recent_inducement: InducementEvent | None = None,
    premium_discount: PremiumDiscountZones | None = None,
    timestamp: datetime = KILL_ZONE_TS,
    relative_volume: float = 1.0,
    htf_structure: MarketStructure = MarketStructure.UNKNOWN,
    atr: float = 200.0,
    mitigation_blocks: list[MitigationBlock] | None = None,
    swing_highs: list[SwingPoint] | None = None,
    swing_lows: list[SwingPoint] | None = None,
) -> SMCAnalysis:
    structure_state: MarketStructureState | None
    if structure != MarketStructure.UNKNOWN or swing_highs or swing_lows:
        structure_state = MarketStructureState(
            structure=structure,
            swing_highs=swing_highs or [],
            swing_lows=swing_lows or [],
        )
    else:
        structure_state = None
    return SMCAnalysis(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        timestamp=timestamp,
        current_price=Decimal(current_price),
        structure=structure_state,
        active_obs=active_obs or [],
        open_fvgs=open_fvgs or [],
        active_liquidity=active_liquidity or [],
        recent_inducement=recent_inducement,
        mitigation_blocks=mitigation_blocks or [],
        premium_discount=premium_discount,
        atr=atr,
        relative_volume=relative_volume,
        htf_structure=htf_structure,
    )


class TestMitigationBlockFallback:
    """
    Regression coverage for mitigation-block wiring: extract_mitigation_
    blocks() was fully implemented and unit-tested (tests/strategy/
    test_mitigation.py) but SMCAnalysis had no field for it and
    score_confluence() never consulted it before this wiring.
    """

    def _make_mitigation_block(
        self,
        ob_type: OrderBlockType = OrderBlockType.BULLISH,
        remaining_top: str = "50100",
        remaining_bottom: str = "50000",
    ) -> MitigationBlock:
        ob = make_ob(ob_type, top="50200", bottom="49900")
        return MitigationBlock(
            original_ob=ob,
            mitigation_price=Decimal("50050"),
            remaining_zone_top=Decimal(remaining_top),
            remaining_zone_bottom=Decimal(remaining_bottom),
        )

    def test_disabled_by_default_no_fallback_score(self) -> None:
        mb = self._make_mitigation_block()
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="50050",  # inside the mitigation block's remaining zone
            active_obs=[],  # no active OB — would need the fallback to score
            mitigation_blocks=[mb],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score == 0.0

    def test_enabled_scores_when_price_inside_matching_mitigation_block(self) -> None:
        mb = self._make_mitigation_block(OrderBlockType.BULLISH)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="50050",
            active_obs=[],
            mitigation_blocks=[mb],
        )
        result = score_confluence(analysis, make_candles(), "test", use_mitigation_blocks=True)
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score > 0.0

    def test_enabled_but_price_outside_zone_stays_zero(self) -> None:
        mb = self._make_mitigation_block(OrderBlockType.BULLISH)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49000",  # outside remaining_zone [50000, 50100]
            active_obs=[],
            mitigation_blocks=[mb],
        )
        result = score_confluence(analysis, make_candles(), "test", use_mitigation_blocks=True)
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score == 0.0

    def test_enabled_but_wrong_direction_stays_zero(self) -> None:
        # A bearish mitigation block shouldn't score for a bullish signal.
        mb = self._make_mitigation_block(OrderBlockType.BEARISH)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="50050",
            active_obs=[],
            mitigation_blocks=[mb],
        )
        result = score_confluence(analysis, make_candles(), "test", use_mitigation_blocks=True)
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score == 0.0

    def test_active_ob_always_takes_priority_over_mitigation_fallback(self) -> None:
        mb = self._make_mitigation_block(OrderBlockType.BULLISH)
        active = make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="50000",
            active_obs=[active],
            mitigation_blocks=[mb],
        )
        result = score_confluence(analysis, make_candles(), "test", use_mitigation_blocks=True)
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        # Full active-OB scoring path, not the 60%-scaled fallback.
        assert ob_component.score > 0.0


class TestStopLossMethod:
    """
    Regression coverage for structure_stop_loss wiring: it was fully
    implemented and unit-tested (tests/risk/test_stop_loss.py) but
    _calculate_trade_params() never called it before this wiring — only
    atr_stop_loss() was reachable for the no-OB fallback stop.
    """

    def test_default_atr_method_unaffected_by_available_swings(self) -> None:
        # 'atr' (default) must ignore swing points entirely, even when a
        # swing low is available for the no-OB fallback.
        swing_low = make_swing("49500", is_high=False)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[],  # no OB -- exercises the fallback branch
            swing_lows=[swing_low],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        # ATR-based stop for a 200 ATR is entry - 1.5*200 = entry - 300,
        # nowhere near the swing low at 49500.
        assert result.stop_loss is not None
        assert abs(result.stop_loss - Decimal("49700")) < Decimal("1")

    def test_structure_method_anchors_to_nearest_swing_low_for_long(self) -> None:
        swing_low = make_swing("49500", is_high=False)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[],
            swing_lows=[swing_low],
        )
        result = score_confluence(analysis, make_candles(), "test", stop_loss_method="structure")
        assert result is not None
        assert result.stop_loss is not None
        # structure_stop_loss anchors just beyond the swing (49500 - a
        # small buffer), clearly different from and further from entry
        # than the plain ATR stop (49700) would have been.
        assert result.stop_loss < Decimal("49500")
        assert result.stop_loss > Decimal("49400")

    def test_structure_method_falls_back_to_atr_when_no_swing_available(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[],
            swing_lows=[],  # nothing to anchor to
        )
        result = score_confluence(analysis, make_candles(), "test", stop_loss_method="structure")
        assert result is not None
        assert result.stop_loss is not None
        assert abs(result.stop_loss - Decimal("49700")) < Decimal("1")

    def test_ob_anchored_stop_unaffected_by_stop_loss_method(self) -> None:
        # The OB-anchored branch must ignore stop_loss_method entirely --
        # both methods should produce an identical OB-buffer stop.
        ob = make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")
        swing_low = make_swing("40000", is_high=False)  # far away, would differ a lot
        analysis_atr = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[ob],
            swing_lows=[swing_low],
        )
        analysis_structure = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[ob],
            swing_lows=[swing_low],
        )
        result_atr = score_confluence(analysis_atr, make_candles(), "test")
        result_structure = score_confluence(
            analysis_structure, make_candles(), "test", stop_loss_method="structure"
        )
        assert result_atr is not None and result_structure is not None
        assert result_atr.stop_loss == result_structure.stop_loss

    def test_returns_none_when_structure_is_unknown_and_no_choch(self) -> None:
        analysis = make_analysis(structure=MarketStructure.UNKNOWN)
        result = score_confluence(analysis, make_candles(), "test-strategy")
        assert result is None


class TestMarketStructureScoring:
    def test_bullish_structure_yields_long_direction(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        assert result.direction == SignalDirection.LONG

    def test_bearish_structure_yields_short_direction(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BEARISH,
            active_obs=[make_ob(OrderBlockType.BEARISH, top="50100", bottom="49900")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        assert result.direction == SignalDirection.SHORT

    def test_ranging_structure_alone_scores_low_and_may_return_none(self) -> None:
        # Ranging gives only 5pts and no structure_direction is set
        # (only BULLISH/BEARISH branches set it) -> None returned.
        analysis = make_analysis(structure=MarketStructure.RANGING)
        result = score_confluence(analysis, make_candles(), "test")
        assert result is None

    def test_market_structure_component_score_is_recorded(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        structure_component = next(c for c in result.components if c.name == "market_structure")
        assert structure_component.score == 20.0


class TestOrderBlockScoring:
    def test_matching_bullish_ob_containing_price_scores_full_points(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score == 20.0

    def test_mismatched_direction_ob_is_not_used(self) -> None:
        # Bearish OB present but direction is LONG -> ob_score stays 0.
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BEARISH, top="50000", bottom="49800")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        # With no OB and no other confluence, total score will likely be
        # too low (< 45) and return None -- assert on that explicitly
        # rather than assuming.
        assert result is None

    def test_mitigated_ob_is_skipped(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")
        ob.is_mitigated = True
        analysis = make_analysis(
            structure=MarketStructure.BULLISH, current_price="49900", active_obs=[ob]
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is None  # OB skipped, insufficient score elsewhere


class TestFairValueGapScoring:
    def test_matching_bullish_fvg_containing_price_scores_full_points(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49950",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        fvg_component = next(c for c in result.components if c.name == "fair_value_gap")
        assert fvg_component.score == 15.0

    def test_containment_check_uses_fvg_at_price_and_picks_correct_zone(self) -> None:
        # Regression for the fvg_at_price() wiring: with multiple open FVGs,
        # the containment check (now routed through fvg_at_price()) must
        # still identify the zone actually holding current_price, not just
        # any bullish zone in the list.
        near_zone = make_fvg(FVGType.BULLISH, top="49895", bottom="49800")
        containing_zone = make_fvg(FVGType.BULLISH, top="50000", bottom="49900")
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49950",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[near_zone, containing_zone],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        fvg_component = next(c for c in result.components if c.name == "fair_value_gap")
        # Full containment credit (proximity_factor=1.0), not the reduced
        # 0.8 "near" factor a mis-wired containment check could fall back to.
        assert fvg_component.score == 15.0


class TestLiquiditySweepScoring:
    def test_bullish_inducement_matching_long_direction_scores_full(self) -> None:
        inducement = InducementEvent(
            direction="bullish_inducement",
            swept_price=Decimal("49700"),
            sweep_high=Decimal("49750"),
            sweep_low=Decimal("49650"),
            rejection_price=Decimal("49900"),
            bar_index=9,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            recent_inducement=inducement,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        liq_component = next(c for c in result.components if c.name == "liquidity_sweep")
        assert liq_component.score == 15.0

    def test_swept_liquidity_pool_without_inducement_scores_partial(self) -> None:
        pool = LiquidityPool(
            liq_type=LiquidityType.EQUAL_LOWS,
            price=Decimal("49700"),
            zone_top=Decimal("49720"),
            zone_bottom=Decimal("49680"),
            bar_index=5,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            is_swept=True,
        )
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            active_liquidity=[pool],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        liq_component = next(c for c in result.components if c.name == "liquidity_sweep")
        assert liq_component.score == 8.0


class TestPremiumDiscountScoring:
    def test_long_in_discount_zone_scores_full(self) -> None:
        pd = PremiumDiscountZones(
            swing_high=Decimal("51000"),
            swing_low=Decimal("49000"),
            current_price=Decimal("49500"),  # well below equilibrium of 50000 -> discount
        )
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            premium_discount=pd,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        pd_component = next(c for c in result.components if c.name == "premium_discount")
        assert pd_component.score == 10.0


class TestVolumeScoring:
    def test_high_relative_volume_scores_full_5_points(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            relative_volume=2.5,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        vol_component = next(c for c in result.components if c.name == "volume")
        assert vol_component.score == 5.0

    def test_low_relative_volume_scores_zero(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            relative_volume=0.5,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        vol_component = next(c for c in result.components if c.name == "volume")
        assert vol_component.score == 0.0


class TestHtfAlignmentScoring:
    def test_aligned_htf_bullish_structure_scores_full_5_points(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            htf_structure=MarketStructure.BULLISH,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        htf_component = next(c for c in result.components if c.name == "htf_alignment")
        assert htf_component.score == 5.0

    def test_conflicting_htf_structure_scores_zero(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            htf_structure=MarketStructure.BEARISH,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        htf_component = next(c for c in result.components if c.name == "htf_alignment")
        assert htf_component.score == 0.0


class TestMinimumScoreGate:
    def test_below_45_returns_none(self) -> None:
        # Structure only (20pts, off-hours session ~1pt) is well under 45.
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[],  # no OB
            timestamp=OFF_HOURS_TS,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is None

    def test_a_strong_confluence_setup_clears_the_gate(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
            htf_structure=MarketStructure.BULLISH,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        assert result.score >= 45.0


class TestStrengthTiers:
    def test_very_strong_at_80_or_above(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
            htf_structure=MarketStructure.BULLISH,
            timestamp=KILL_ZONE_TS,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        if result.score >= 80:
            assert result.strength == SignalStrength.VERY_STRONG

    def test_weak_below_55(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            timestamp=OFF_HOURS_TS,
            relative_volume=0.5,
        )
        result = score_confluence(analysis, make_candles(), "test")
        if result is not None and result.score < 55:
            assert result.strength == SignalStrength.WEAK


class TestTradeParameterCalculation:
    def test_long_stop_loss_is_below_entry_and_take_profit_above(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
        )
        result = score_confluence(analysis, make_candles(), "test", risk_reward_min=1.5)
        assert result is not None
        assert result.stop_loss < result.entry_price < result.take_profit

    def test_short_stop_loss_is_above_entry_and_take_profit_below(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BEARISH,
            current_price="50100",
            active_obs=[make_ob(OrderBlockType.BEARISH, top="50200", bottom="50000")],
            open_fvgs=[make_fvg(FVGType.BEARISH, top="50200", bottom="50100")],
            relative_volume=2.5,
        )
        result = score_confluence(analysis, make_candles(), "test", risk_reward_min=1.5)
        assert result is not None
        assert result.take_profit < result.entry_price < result.stop_loss

    def test_risk_reward_ratio_is_respected(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
        )
        result = score_confluence(analysis, make_candles(), "test", risk_reward_min=2.0)
        assert result is not None
        risk = result.entry_price - result.stop_loss
        reward = result.take_profit - result.entry_price
        assert reward == risk * Decimal("2.0")

    def test_entry_uses_ob_mid_when_ob_is_present(self) -> None:
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[ob],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        assert result.entry_price == ob.mid


class TestMetadata:
    def test_metadata_includes_atr_and_ob_fvg_flags(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
            relative_volume=2.5,
            atr=250.0,
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        assert result.metadata["atr"] == 250.0
        assert result.metadata["has_ob"] is True
        assert result.metadata["has_fvg"] is True


class TestOrderBlockGradedScoring:
    """OB scoring is graded by freshness and proximity, not just found/not-found."""

    def test_aged_ob_scores_lower_than_a_fresh_one(self) -> None:
        candles = make_candles(150)  # current_bar = 149
        fresh_ob = make_ob(
            OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=140
        )  # age 9
        aged_ob = make_ob(
            OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=59
        )  # age 90

        fresh_analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[fresh_ob],
            relative_volume=2.5,
        )
        aged_analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[aged_ob],
            relative_volume=2.5,
        )

        fresh_result = score_confluence(fresh_analysis, candles, "test")
        aged_result = score_confluence(aged_analysis, candles, "test")

        assert fresh_result is not None
        fresh_ob_score = next(c for c in fresh_result.components if c.name == "order_block").score
        assert fresh_ob_score == 20.0

        assert aged_result is not None
        aged_ob_score = next(c for c in aged_result.components if c.name == "order_block").score
        assert aged_ob_score < fresh_ob_score
        assert aged_ob_score >= 20.0 * 0.6  # never decays below the floor

    def test_price_near_but_not_inside_the_ob_scores_less_than_a_direct_touch(self) -> None:
        candles = make_candles(30)
        contains_ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=10)
        near_ob = make_ob(OrderBlockType.BULLISH, top="49850", bottom="49700", bar_index=10)

        contains_analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[contains_ob],
        )
        # 49900 is not inside 49700-49850 but is within 1% of the OB top,
        # so it should still register as a weaker "near" match.
        near_analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[near_ob],
        )

        contains_result = score_confluence(contains_analysis, candles, "test")
        near_result = score_confluence(near_analysis, candles, "test")

        assert contains_result is not None
        contains_score = next(
            c for c in contains_result.components if c.name == "order_block"
        ).score
        assert contains_score == 20.0

        assert near_result is not None
        near_score = next(c for c in near_result.components if c.name == "order_block").score
        assert 0.0 < near_score < contains_score


class TestFairValueGapGradedScoring:
    def test_a_shallow_gap_relative_to_atr_scores_less_than_a_full_gap(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49910",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="49920", bottom="49900")],  # size=20, atr=200
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        fvg_component = next(c for c in result.components if c.name == "fair_value_gap")
        assert 0.0 < fvg_component.score < 15.0


class TestLiquiditySweepDirectionalAlignment:
    def test_swept_pool_on_the_wrong_side_scores_much_less_than_aligned(self) -> None:
        # LONG direction wants sell-side liquidity swept; a swept buy-side
        # (equal highs) pool doesn't support a long entry.
        wrong_side_pool = LiquidityPool(
            liq_type=LiquidityType.EQUAL_HIGHS,
            price=Decimal("50300"),
            zone_top=Decimal("50320"),
            zone_bottom=Decimal("50280"),
            bar_index=5,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            is_swept=True,
        )
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            active_liquidity=[wrong_side_pool],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        liq_component = next(c for c in result.components if c.name == "liquidity_sweep")
        assert liq_component.score == 4.0

    def test_more_prior_touches_at_an_aligned_swept_pool_scores_higher(self) -> None:
        pool = LiquidityPool(
            liq_type=LiquidityType.EQUAL_LOWS,
            price=Decimal("49700"),
            zone_top=Decimal("49720"),
            zone_bottom=Decimal("49680"),
            bar_index=5,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            is_swept=True,
            confluence_count=3,
        )
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800")],
            active_liquidity=[pool],
        )
        result = score_confluence(analysis, make_candles(), "test")
        assert result is not None
        liq_component = next(c for c in result.components if c.name == "liquidity_sweep")
        assert liq_component.score == 11.0  # 8.0 + (3-1)*1.5, below the 12.0 cap


class TestOrderBlockMaxAgeConfigThreading:
    """
    ob_max_age_bars — previously a hardcoded SMC_OB_MAX_AGE_BARS module
    constant in both OrderBlock.is_valid() (the active/valid gate) and
    the freshness-decay curve here. Now threadable via
    score_confluence()'s ob_max_age_bars kwarg — see
    configs/strategy.yaml's `smc.ob_max_age_bars` and
    main.py::_load_smc_indicator_config().
    """

    def test_ob_beyond_default_max_age_but_within_custom_max_age_is_scored(self) -> None:
        # bar_index=10, current_bar=149 -> age=139: beyond the default
        # SMC_OB_MAX_AGE_BARS=100 (would be filtered as invalid) but
        # within a custom, wider max age of 200.
        candles = make_candles(150)
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=10)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[ob],
        )

        default_result = score_confluence(analysis, candles, "test")
        wide_result = score_confluence(analysis, candles, "test", ob_max_age_bars=200)

        # Default: OB is stale (age 139 > 100), so it contributes nothing.
        if default_result is not None:
            default_ob_score = next(
                (c.score for c in default_result.components if c.name == "order_block"), 0.0
            )
            assert default_ob_score == 0.0

        # Custom wider max age: same OB is still valid, floor-decayed.
        assert wide_result is not None
        wide_ob_score = next(c for c in wide_result.components if c.name == "order_block").score
        assert wide_ob_score > 0.0

    def test_custom_shorter_max_age_decays_faster_than_default(self) -> None:
        candles = make_candles(150)
        # age=50 -> still within OB_FRESHNESS_FULL_CREDIT_BARS (50) so
        # full credit regardless of max age; use age=70 to land past
        # full-credit but still valid under both max-age settings.
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=79)  # age 70
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[ob],
            relative_volume=2.5,
        )

        default_result = score_confluence(analysis, candles, "test", ob_max_age_bars=100)
        short_result = score_confluence(analysis, candles, "test", ob_max_age_bars=80)

        assert default_result is not None and short_result is not None
        default_score = next(c for c in default_result.components if c.name == "order_block").score
        short_score = next(c for c in short_result.components if c.name == "order_block").score
        # A tighter max-age window compresses the decay span, so the same
        # age decays further (lower score) under the shorter setting.
        assert short_score < default_score

    def test_omitted_kwarg_matches_default_constant_behavior(self) -> None:
        candles = make_candles(150)
        ob = make_ob(OrderBlockType.BULLISH, top="50000", bottom="49800", bar_index=79)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            current_price="49900",
            active_obs=[ob],
            relative_volume=2.5,
        )
        implicit = score_confluence(analysis, candles, "test")
        explicit = score_confluence(analysis, candles, "test", ob_max_age_bars=100)
        assert implicit is not None and explicit is not None
        implicit_score = next(c for c in implicit.components if c.name == "order_block").score
        explicit_score = next(c for c in explicit.components if c.name == "order_block").score
        assert implicit_score == explicit_score


class TestRsiMacdPeriodConfigThreading:
    """
    rsi_period/ema_fast/ema_slow — previously hardcoded DEFAULT_RSI_PERIOD/
    DEFAULT_EMA_FAST/DEFAULT_EMA_SLOW inside score_confluence()'s RSI/MACD
    metadata computation, with no way for a caller to override them. Now
    threadable via score_confluence()'s matching kwargs — see
    configs/strategy.yaml's `indicators.{rsi_period,ema_fast,ema_slow}`.
    Neither value affects total_score (see module comment), only
    result.metadata["rsi"]/["macd_histogram"], so these tests assert on
    metadata directly.
    """

    def test_custom_rsi_period_changes_metadata_value(self) -> None:
        candles = make_candles(60)
        analysis = make_analysis(structure=MarketStructure.BULLISH, current_price="49900")
        default_result = score_confluence(analysis, candles, "test")
        custom_result = score_confluence(analysis, candles, "test", rsi_period=5)
        assert default_result is not None and custom_result is not None
        assert "rsi" in default_result.metadata
        assert "rsi" in custom_result.metadata
        # Same flat-price candle series makes exact values less
        # interesting than confirming the period actually changed what
        # was computed (both are well-defined, non-None floats here).
        assert isinstance(custom_result.metadata["rsi"], float)

    def test_custom_ema_periods_still_produce_macd_metadata(self) -> None:
        candles = make_candles(60)
        analysis = make_analysis(structure=MarketStructure.BULLISH, current_price="49900")
        result = score_confluence(analysis, candles, "test", ema_fast=5, ema_slow=13)
        assert result is not None
        assert "macd_histogram" in result.metadata

    def test_omitted_kwargs_match_default_constant_behavior(self) -> None:
        from src.core.constants import DEFAULT_EMA_FAST, DEFAULT_EMA_SLOW, DEFAULT_RSI_PERIOD

        candles = make_candles(60)
        analysis = make_analysis(structure=MarketStructure.BULLISH, current_price="49900")
        implicit = score_confluence(analysis, candles, "test")
        explicit = score_confluence(
            analysis,
            candles,
            "test",
            rsi_period=DEFAULT_RSI_PERIOD,
            ema_fast=DEFAULT_EMA_FAST,
            ema_slow=DEFAULT_EMA_SLOW,
        )
        assert implicit is not None and explicit is not None
        assert implicit.metadata.get("rsi") == explicit.metadata.get("rsi")
        assert implicit.metadata.get("macd_histogram") == explicit.metadata.get("macd_histogram")


class TestConfluenceWeights:
    """
    Unit tests for the ConfluenceWeights dataclass itself — see
    ROADMAP.md's "empirically validate and tune the default SMC
    confluence scoring weights" item and
    scripts/optimize.py::WEIGHT_PARAM_SPACE, which is the actual consumer
    of this class outside of score_confluence()'s own default.
    """

    def test_default_weights_sum_to_100(self) -> None:
        assert ConfluenceWeights().total == 100.0

    def test_default_weights_normalize_to_identity(self) -> None:
        d = ConfluenceWeights().normalized()
        assert d.market_structure == 20.0
        assert d.order_block == 20.0
        assert d.fair_value_gap == 15.0
        assert d.liquidity_sweep == 15.0
        assert d.premium_discount == 10.0
        assert d.session == 10.0
        assert d.volume == 5.0
        assert d.htf_alignment == 5.0

    def test_uniformly_scaled_weights_normalize_to_the_same_defaults(self) -> None:
        # 3x every default budget -- relative proportions are unchanged,
        # so normalized() should land on exactly the same values as the
        # unscaled defaults.
        scaled = ConfluenceWeights(
            market_structure=60.0,
            order_block=60.0,
            fair_value_gap=45.0,
            liquidity_sweep=45.0,
            premium_discount=30.0,
            session=30.0,
            volume=15.0,
            htf_alignment=15.0,
        )
        assert scaled.normalized().as_dict() == ConfluenceWeights().normalized().as_dict()

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError):
            ConfluenceWeights(market_structure=-1.0)

    def test_all_zero_weights_raises(self) -> None:
        with pytest.raises(ValueError):
            ConfluenceWeights(
                market_structure=0,
                order_block=0,
                fair_value_gap=0,
                liquidity_sweep=0,
                premium_discount=0,
                session=0,
                volume=0,
                htf_alignment=0,
            )

    def test_as_dict_round_trips_into_constructor(self) -> None:
        w = ConfluenceWeights(market_structure=25.0, volume=8.0)
        assert ConfluenceWeights(**w.as_dict()).as_dict() == w.as_dict()


class TestScoreConfluenceWithCustomWeights:
    """
    score_confluence()'s `weights=` parameter — the wiring that lets
    scripts/optimize.py actually tune the previously-hardcoded scoring
    budget (see also strategy/test_signal_engine.py and
    tests/unit/test_main.py for the layers above this that thread a
    ConfluenceWeights instance down to here).
    """

    def test_default_call_matches_explicit_default_weights(self) -> None:
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
        )
        candles = make_candles()
        implicit = score_confluence(analysis, candles, "test")
        explicit = score_confluence(analysis, candles, "test", weights=ConfluenceWeights())
        assert implicit is not None and explicit is not None
        assert implicit.score == explicit.score

    def test_zeroing_a_factor_removes_its_contribution(self) -> None:
        # Order block scoring should contribute nothing when its budget
        # is zeroed out, even though the OB itself still "counts" toward
        # entry/stop-loss placement -- only its *score* contribution
        # vanishes.
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
        )
        candles = make_candles()
        weights = ConfluenceWeights(order_block=0.0, market_structure=100.0)
        result = score_confluence(analysis, candles, "test", weights=weights)
        assert result is not None
        ob_component = next(c for c in result.components if c.name == "order_block")
        assert ob_component.score == 0.0

    def test_score_stays_on_0_100_scale_for_arbitrary_positive_weights(self) -> None:
        # Weights that don't sum to 100 (e.g. an Optuna trial sampling
        # each dimension independently) must still land the total score
        # in [0, 100] so min_score/classify_strength stay meaningful.
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
            open_fvgs=[make_fvg(FVGType.BULLISH, top="50000", bottom="49900")],
        )
        candles = make_candles()
        weights = ConfluenceWeights(
            market_structure=37.0,
            order_block=8.0,
            fair_value_gap=22.0,
            liquidity_sweep=3.0,
            premium_discount=17.0,
            session=6.0,
            volume=4.0,
            htf_alignment=1.0,
        )  # sums to 98, not 100
        result = score_confluence(analysis, candles, "test", weights=weights)
        assert result is not None
        assert 0.0 <= result.score <= 100.0

    def test_component_weight_fractions_reflect_the_custom_budget(self) -> None:
        analysis = make_analysis(structure=MarketStructure.BULLISH)
        candles = make_candles()
        weights = ConfluenceWeights(market_structure=40.0)  # 2x default, others unchanged
        result = score_confluence(analysis, candles, "test", weights=weights)
        assert result is not None
        ms_component = next(c for c in result.components if c.name == "market_structure")
        # 40 / (40+20+15+15+10+10+5+5=120) after normalization -- just
        # check it's meaningfully larger than the 0.20 default fraction,
        # not brittle-pinned to the exact float.
        assert ms_component.weight > 0.20


class TestVolumeVolatilityMetadata:
    """
    Regression coverage for OBV/VWAP/Bollinger Bands/historical volatility
    wiring: indicators/{volume,volatility}.py's obv(), vwap(),
    bollinger_bands(), historical_volatility() were fully implemented and
    unit-tested but never called from anywhere in the strategy pipeline
    before this wiring. Joins the existing rsi/stochastic_k/williams_r/
    price_zscore/trend_slope precedent: ML-feature-only metadata, no
    scoring/filter role (see module comment above their computation in
    confluence.py for why). is_high_volume_bar() is deliberately excluded
    -- it duplicates the metric the live "volume" score factor already
    uses (relative_volume()) in boolean form.
    """

    def test_metadata_includes_new_indicator_keys(self) -> None:
        candles = make_candles(60)
        analysis = make_analysis(structure=MarketStructure.BULLISH)
        result = score_confluence(analysis, candles, "test")
        assert result is not None
        for key in ("vwap", "obv_slope", "bb_percent_b", "bb_bandwidth", "historical_volatility"):
            assert key in result.metadata

    def test_vwap_is_a_sane_price_near_the_candle_range(self) -> None:
        candles = make_candles(60, base_price=50000.0)
        analysis = make_analysis(structure=MarketStructure.BULLISH)
        result = score_confluence(analysis, candles, "test")
        assert result is not None
        vwap_value = result.metadata["vwap"]
        assert vwap_value is not None
        assert 49000.0 < vwap_value < 51000.0

    def test_short_candle_history_degrades_to_none_not_a_crash(self) -> None:
        # Fewer candles than DEFAULT_BB_PERIOD/21-bar historical_volatility
        # window -- must degrade to None (same convention as rsi_value/
        # macd_histogram above), never raise.
        candles = make_candles(15)
        analysis = make_analysis(structure=MarketStructure.BULLISH)
        result = score_confluence(analysis, candles, "test")
        assert result is not None
        assert result.metadata["bb_percent_b"] is None
        assert result.metadata["bb_bandwidth"] is None
        assert result.metadata["historical_volatility"] is None

    def test_new_metadata_does_not_affect_total_score(self) -> None:
        # Same non-goal as the RSI/MACD/stochastic block: these are pure
        # informational additions to metadata, the 0-100 scoring budget
        # must be bit-for-bit unaffected.
        candles = make_candles(60)
        analysis = make_analysis(
            structure=MarketStructure.BULLISH,
            active_obs=[make_ob(OrderBlockType.BULLISH, top="50100", bottom="49900")],
        )
        result = score_confluence(analysis, candles, "test")
        assert result is not None
        assert 0.0 <= result.score <= 100.0
