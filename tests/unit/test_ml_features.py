"""Unit tests for src.machine_learning.features."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from src.core.enums import (
    FVGType,
    LiquidityType,
    MarketStructure,
    OrderBlockType,
    SignalDirection,
    SignalStrength,
    Timeframe,
)
from src.domain.signal import Signal
from src.machine_learning.features import (
    FEATURE_NAMES,
    N_FEATURES,
    extract_features,
    features_to_dict,
)
from src.smart_money.bos import BOSEvent
from src.smart_money.confluence import SMCAnalysis
from src.smart_money.fair_value_gap import FairValueGap
from src.smart_money.liquidity import LiquidityPool
from src.smart_money.market_structure import SwingPoint
from src.smart_money.order_blocks import OrderBlock
from src.smart_money.premium_discount import PremiumDiscountZones


def make_signal(**kwargs) -> Signal:
    defaults = dict(
        strategy_id="test_strategy",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        direction=SignalDirection.LONG,
        score=70.0,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def make_bos_event(direction: str = "bullish") -> BOSEvent:
    swing = SwingPoint(
        price=Decimal("49500"),
        timestamp=datetime.now(tz=UTC),
        bar_index=10,
        is_high=False,
    )
    return BOSEvent(
        direction=direction,
        broken_price=Decimal("49500"),
        break_candle_index=12,
        break_price=Decimal("49600"),
        swing_point=swing,
        timestamp=datetime.now(tz=UTC),
    )


def make_order_block(**kwargs) -> OrderBlock:
    defaults = dict(
        ob_type=OrderBlockType.BULLISH,
        top=Decimal("50100"),
        bottom=Decimal("49900"),
        origin_high=Decimal("50150"),
        origin_low=Decimal("49850"),
        bar_index=10,
        timestamp=datetime.now(tz=UTC),
        bos_event=make_bos_event(),
    )
    defaults.update(kwargs)
    return OrderBlock(**defaults)


def make_fvg(**kwargs) -> FairValueGap:
    defaults = dict(
        fvg_type=FVGType.BULLISH,
        top=Decimal("50050"),
        bottom=Decimal("49950"),
        bar_index=11,
        timestamp=datetime.now(tz=UTC),
    )
    defaults.update(kwargs)
    return FairValueGap(**defaults)


def make_liquidity_pool(**kwargs) -> LiquidityPool:
    defaults = dict(
        liq_type=LiquidityType.EQUAL_HIGHS,
        price=Decimal("51000"),
        zone_top=Decimal("51050"),
        zone_bottom=Decimal("50950"),
        bar_index=8,
        timestamp=datetime.now(tz=UTC),
    )
    defaults.update(kwargs)
    return LiquidityPool(**defaults)


def make_analysis(**kwargs) -> SMCAnalysis:
    defaults = dict(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        timestamp=datetime.now(tz=UTC),
        current_price=Decimal("50000"),
    )
    defaults.update(kwargs)
    return SMCAnalysis(**defaults)


class TestExtractFeaturesShape:
    def test_returns_correct_length_and_dtype(self) -> None:
        vec = extract_features(make_signal(), make_analysis())
        assert vec.shape == (N_FEATURES,)
        assert vec.dtype == np.float32

    def test_all_values_are_finite(self) -> None:
        vec = extract_features(make_signal(), make_analysis())
        assert np.all(np.isfinite(vec))


class TestExtractFeaturesWithNoneAnalysis:
    """
    Regression coverage for the fixed type-annotation bug: extract_features
    was implemented to gracefully degrade every analysis-derived feature to
    a safe default when ``analysis=None`` (as should_trade()'s docstring
    always promised), but the parameter was incorrectly typed as
    non-optional ``SMCAnalysis``. These tests pin down that the documented,
    already-implemented behavior actually works end-to-end.
    """

    def test_none_analysis_does_not_raise(self) -> None:
        vec = extract_features(make_signal(), None)
        assert vec.shape == (N_FEATURES,)

    def test_none_analysis_produces_finite_values(self) -> None:
        vec = extract_features(make_signal(), None)
        assert np.all(np.isfinite(vec))

    def test_none_analysis_zeroes_structure_features(self) -> None:
        vec = extract_features(make_signal(), None)
        d = features_to_dict(vec)
        assert d["structure_bullish"] == 0.0
        assert d["structure_bearish"] == 0.0
        assert d["has_bos"] == 0.0
        assert d["has_choch"] == 0.0
        assert d["has_ob"] == 0.0
        assert d["has_fvg"] == 0.0
        assert d["has_liquidity_sweep"] == 0.0

    def test_none_analysis_defaults_relative_volume_to_one(self) -> None:
        vec = extract_features(make_signal(), None)
        d = features_to_dict(vec)
        assert d["relative_volume"] == 1.0

    def test_none_analysis_uses_signal_entry_price_for_atr_normalisation(self) -> None:
        # With no analysis, atr=1.0 (default) and price falls back to entry.
        sig = make_signal(entry_price=Decimal("50000"))
        vec = extract_features(sig, None)
        d = features_to_dict(vec)
        assert d["atr_normalised"] == pytest.approx(1.0 / 50000.0)


class TestExtractFeaturesWithFullAnalysis:
    def test_bullish_structure_sets_structure_bullish(self) -> None:
        analysis = make_analysis(htf_structure=MarketStructure.BULLISH, htf_bias="bullish")
        sig = make_signal(direction=SignalDirection.LONG)
        vec = extract_features(sig, analysis)
        d = features_to_dict(vec)
        assert d["structure_bullish"] == 1.0
        assert d["structure_bearish"] == 0.0
        assert d["htf_aligned"] == 1.0

    def test_bos_and_choch_flags(self) -> None:
        analysis = make_analysis(recent_bos=make_bos_event(), recent_choch=None)
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["has_bos"] == 1.0
        assert d["has_choch"] == 0.0

    def test_active_order_block_sets_ob_features(self) -> None:
        ob = make_order_block(top=Decimal("50200"), bottom=Decimal("49800"))
        analysis = make_analysis(active_obs=[ob], atr=100.0, current_price=Decimal("50000"))
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["has_ob"] == 1.0
        assert d["ob_size_atr"] == pytest.approx(400.0 / 100.0)

    def test_open_fvg_sets_fvg_features(self) -> None:
        fvg = make_fvg(top=Decimal("50050"), bottom=Decimal("49950"))
        analysis = make_analysis(open_fvgs=[fvg], atr=50.0, current_price=Decimal("50000"))
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["has_fvg"] == 1.0
        assert d["fvg_size_atr"] == pytest.approx(100.0 / 50.0)

    def test_swept_liquidity_pool_sets_sweep_flag(self) -> None:
        pool = make_liquidity_pool(is_swept=True)
        analysis = make_analysis(active_liquidity=[pool])
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["has_liquidity_sweep"] == 1.0

    def test_no_active_obs_or_fvgs_zeroes_those_features(self) -> None:
        analysis = make_analysis(active_obs=[], open_fvgs=[])
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["has_ob"] == 0.0
        assert d["has_fvg"] == 0.0

    def test_premium_discount_zone_long_in_discount(self) -> None:
        pd = PremiumDiscountZones(
            swing_high=Decimal("52000"),
            swing_low=Decimal("48000"),
            current_price=Decimal("48500"),
        )
        analysis = make_analysis(premium_discount=pd)
        sig = make_signal(direction=SignalDirection.LONG)
        vec = extract_features(sig, analysis)
        d = features_to_dict(vec)
        assert d["in_discount_zone"] == 1.0

    def test_relative_volume_passed_through(self) -> None:
        analysis = make_analysis(relative_volume=2.5)
        vec = extract_features(make_signal(), analysis)
        d = features_to_dict(vec)
        assert d["relative_volume"] == pytest.approx(2.5)


class TestFeaturesToDict:
    def test_round_trips_all_feature_names(self) -> None:
        vec = extract_features(make_signal(), make_analysis())
        d = features_to_dict(vec)
        assert set(d.keys()) == set(FEATURE_NAMES)

    def test_wrong_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Expected"):
            features_to_dict(np.zeros(3, dtype=np.float32))


class TestHtfAlignmentAgreementCases:
    """
    TestExtractFeaturesWithFullAnalysis only exercises the long+bullish
    "aligned" case — filling in the remaining direction x bias
    combinations so a regression that e.g. inverted the boolean
    expression wouldn't slip through with 3/4 cases untested.
    """

    def test_long_signal_against_bearish_bias_is_not_aligned(self) -> None:
        analysis = make_analysis(htf_bias="bearish")
        sig = make_signal(direction=SignalDirection.LONG)
        d = features_to_dict(extract_features(sig, analysis))
        assert d["htf_aligned"] == 0.0

    def test_short_signal_against_bearish_bias_is_aligned(self) -> None:
        analysis = make_analysis(htf_bias="bearish")
        sig = make_signal(direction=SignalDirection.SHORT)
        d = features_to_dict(extract_features(sig, analysis))
        assert d["htf_aligned"] == 1.0

    def test_short_signal_against_bullish_bias_is_not_aligned(self) -> None:
        analysis = make_analysis(htf_bias="bullish")
        sig = make_signal(direction=SignalDirection.SHORT)
        d = features_to_dict(extract_features(sig, analysis))
        assert d["htf_aligned"] == 0.0


class TestOrderBlockAgeNormalisation:
    """
    OrderBlock (src/smart_money/order_blocks.py) is a plain @dataclass
    with no `age_bars` field — extract_features reads it via
    `getattr(ob, "age_bars", 0) or 0`, which means ob_age_bars is
    currently *always* 0.0 for every real OrderBlock the SMC engine
    produces, regardless of how old the block actually is. This isn't
    necessarily wrong (0 is a valid, if uninformative, default), but it
    means the age-normalisation logic below the getattr — the min(...,
    1.0) cap in particular — is presently dead code against the real
    domain schema. Pinning this down so a future OrderBlock.age_bars
    addition is a deliberate, visible decision rather than something
    that silently starts changing model inputs.
    """

    def test_real_order_block_has_no_age_bars_attribute(self) -> None:
        ob = make_order_block()
        assert not hasattr(ob, "age_bars")

    def test_ob_age_bars_feature_is_always_zero_for_a_real_order_block(self) -> None:
        analysis = make_analysis(active_obs=[make_order_block()])
        d = features_to_dict(extract_features(make_signal(), analysis))
        assert d["ob_age_bars"] == 0.0


class TestSignalMetadataDerivedFeatures:
    def test_kill_zone_flag_read_from_signal_metadata(self) -> None:
        sig = make_signal(metadata={"in_kill_zone": True})
        d = features_to_dict(extract_features(sig, None))
        assert d["in_kill_zone"] == 1.0

    def test_kill_zone_defaults_to_false_when_metadata_key_absent(self) -> None:
        sig = make_signal(metadata={})
        d = features_to_dict(extract_features(sig, None))
        assert d["in_kill_zone"] == 0.0

    def test_mtf_aligned_falls_back_to_htf_aligned_when_absent_from_metadata(self) -> None:
        analysis = make_analysis(htf_bias="bullish")
        sig = make_signal(direction=SignalDirection.LONG, metadata={})
        d = features_to_dict(extract_features(sig, analysis))
        assert d["mtf_aligned"] == d["htf_aligned"] == 1.0

    def test_mtf_aligned_explicit_metadata_value_overrides_the_fallback(self) -> None:
        analysis = make_analysis(htf_bias="bullish")
        sig = make_signal(direction=SignalDirection.LONG, metadata={"mtf_aligned": 0.0})
        d = features_to_dict(extract_features(sig, analysis))
        assert d["htf_aligned"] == 1.0
        assert d["mtf_aligned"] == 0.0

    def test_n_components_is_normalised_by_ten(self) -> None:
        sig = make_signal(components=["ob", "fvg", "bos"])
        d = features_to_dict(extract_features(sig, None))
        assert d["n_components"] == pytest.approx(0.3)

    def test_direction_long_flag_for_both_directions(self) -> None:
        long_d = features_to_dict(
            extract_features(make_signal(direction=SignalDirection.LONG), None)
        )
        short_d = features_to_dict(
            extract_features(make_signal(direction=SignalDirection.SHORT), None)
        )
        assert long_d["direction_long"] == 1.0
        assert short_d["direction_long"] == 0.0


class TestVolumeVolatilityMetadataDerivedFeatures:
    """
    Regression coverage for vwap/obv_slope/bb_percent_b/bb_bandwidth/
    historical_volatility joining FEATURE_NAMES: previously computed by
    confluence.py and stored in signal.metadata (see
    tests/strategy/test_confluence.py's TestVolumeVolatilityMetadata) but
    never consumed by extract_features() -- these values sat in metadata
    with no feature-vector caller until this wiring. Note: adding these
    to FEATURE_NAMES changes N_FEATURES, which invalidates any
    previously-trained model against trainer.py's schema guard (see
    tests/unit/test_trainer.py's schema-mismatch coverage) -- an
    intentional, documented consequence, not a bug.
    """

    def test_absent_metadata_degrades_to_neutral_defaults(self) -> None:
        sig = make_signal(metadata={})
        d = features_to_dict(extract_features(sig, None))
        assert d["vwap_distance_pct"] == 0.0
        assert d["obv_slope_normalised"] == 0.0
        assert d["bb_percent_b"] == 0.5
        assert d["bb_bandwidth"] == 0.0
        assert d["historical_volatility"] == 0.0

    def test_vwap_distance_pct_reflects_metadata_value(self) -> None:
        # entry_price=50000 (make_signal default), vwap=49500 -> price is
        # ~1.01% above VWAP.
        sig = make_signal(metadata={"vwap": 49500.0})
        d = features_to_dict(extract_features(sig, None))
        assert d["vwap_distance_pct"] == pytest.approx(1.0101, abs=0.001)

    def test_obv_slope_is_squashed_into_bounded_range(self) -> None:
        sig = make_signal(metadata={"obv_slope": 1_000_000.0})
        d = features_to_dict(extract_features(sig, None))
        assert -1.0 < d["obv_slope_normalised"] < 1.0

    def test_bb_percent_b_and_bandwidth_pass_through(self) -> None:
        sig = make_signal(metadata={"bb_percent_b": 0.85, "bb_bandwidth": 0.12})
        d = features_to_dict(extract_features(sig, None))
        assert d["bb_percent_b"] == pytest.approx(0.85)
        assert d["bb_bandwidth"] == pytest.approx(0.12)

    def test_historical_volatility_passes_through(self) -> None:
        sig = make_signal(metadata={"historical_volatility": 0.45})
        d = features_to_dict(extract_features(sig, None))
        assert d["historical_volatility"] == pytest.approx(0.45)


class TestTargetDistanceZeroPriceGuard:
    def test_zero_entry_price_does_not_raise_a_zero_division_error(self) -> None:
        # extract_features guards target_distance_pct's division with
        # `if entry else 0.0` specifically for a falsy (zero) entry price.
        # Signal itself has no explicit gt=0 constraint on entry_price, so
        # this is reachable in practice (e.g. malformed/backfilled
        # historical data) rather than a purely defensive-only branch.
        sig = make_signal(
            entry_price=Decimal("0"),
            stop_loss=Decimal("-100"),
            take_profit=Decimal("100"),
        )
        d = features_to_dict(extract_features(sig, None))
        assert d["target_distance_pct"] == 0.0
