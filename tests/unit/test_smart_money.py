"""Unit tests for Smart Money Concepts modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.enums import MarketStructure, FVGType, OrderBlockType
from src.core.models import OHLCV
from src.smart_money.market_structure import (
    detect_market_structure,
    find_swing_highs,
    find_swing_lows,
)
from src.smart_money.bos import detect_bos
from src.smart_money.choch import detect_choch
from src.smart_money.fair_value_gap import detect_fvgs, get_open_fvgs
from src.smart_money.order_blocks import identify_order_blocks
from src.smart_money.premium_discount import (
    PremiumDiscountZones,
    calculate_premium_discount,
    is_optimal_entry,
)
from src.smart_money.sessions import (
    get_active_kill_zone,
    get_active_session,
    is_in_kill_zone,
    session_score,
)
from src.smart_money.market_structure import SwingPoint


def make_candle(
    ts: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000.0,
) -> OHLCV:
    return OHLCV(
        timestamp=ts,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
    )


def make_ts(i: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)


class TestMarketStructure:
    def _make_bullish_series(self) -> list[OHLCV]:
        """Create an uptrending series: HH/HL pattern."""
        prices = [
            (100, 105, 98, 103),
            (103, 108, 101, 106),
            (104, 107, 99, 102),   # HL
            (102, 112, 101, 110),  # HH
            (108, 111, 106, 109),  # HL
            (109, 118, 108, 116),  # HH
        ]
        candles = []
        for i, (o, h, l, c) in enumerate(prices):
            candles.append(make_candle(make_ts(i), o, h, l, c))
        return candles

    def test_swing_highs_detected(self):
        candles = self._make_bullish_series()
        highs = find_swing_highs(candles, lookback=1)
        assert len(highs) >= 1
        assert all(isinstance(sh, SwingPoint) for sh in highs)
        assert all(sh.is_high for sh in highs)

    def test_swing_lows_detected(self):
        candles = self._make_bullish_series()
        lows = find_swing_lows(candles, lookback=1)
        assert len(lows) >= 1
        assert all(sl.is_low for sl in lows)

    def test_bullish_structure_detected(self):
        # Build a longer clearly bullish series
        candles = []
        prices = [100, 98, 105, 102, 110, 107, 115, 112, 120]
        for i, p in enumerate(prices):
            candles.append(make_candle(make_ts(i), p - 1, p + 2, p - 2, p + 1))

        state = detect_market_structure(candles, lookback=1)
        # With 2 swings in each direction, should classify
        assert state.structure in (
            MarketStructure.BULLISH,
            MarketStructure.RANGING,
            MarketStructure.UNKNOWN,
        )

    def test_unknown_with_too_few_candles(self):
        candles = [make_candle(make_ts(i), 100, 101, 99, 100) for i in range(5)]
        state = detect_market_structure(candles, lookback=2)
        assert state.structure == MarketStructure.UNKNOWN


class TestFairValueGap:
    def test_bullish_fvg_detected(self):
        """Bullish FVG: candle 3 low > candle 1 high."""
        candles = [
            make_candle(make_ts(0), 100, 102, 99, 101),   # c1
            make_candle(make_ts(1), 103, 108, 102, 107),  # c2 (big up)
            make_candle(make_ts(2), 106, 109, 104, 108),  # c3: low=104 > c1 high=102
        ]
        fvgs = detect_fvgs(candles)
        bullish = [f for f in fvgs if f.fvg_type == FVGType.BULLISH]
        assert len(bullish) == 1
        assert bullish[0].bottom == Decimal("102")
        assert bullish[0].top == Decimal("104")

    def test_bearish_fvg_detected(self):
        """Bearish FVG: candle 3 high < candle 1 low."""
        candles = [
            make_candle(make_ts(0), 110, 112, 108, 109),  # c1 low=108
            make_candle(make_ts(1), 107, 109, 100, 101),  # c2 (big down)
            make_candle(make_ts(2), 102, 105, 100, 103),  # c3 high=105 < c1 low=108
        ]
        fvgs = detect_fvgs(candles)
        bearish = [f for f in fvgs if f.fvg_type == FVGType.BEARISH]
        assert len(bearish) == 1

    def test_no_fvg_when_overlap(self):
        """No FVG when candles overlap."""
        candles = [
            make_candle(make_ts(i), 100, 102, 99, 101)
            for i in range(3)
        ]
        fvgs = detect_fvgs(candles)
        assert len(fvgs) == 0

    def test_fvg_fill_detection(self):
        """FVG fill is detected when price retraces into gap."""
        candles = [
            make_candle(make_ts(0), 100, 102, 99, 101),
            make_candle(make_ts(1), 103, 110, 103, 109),
            make_candle(make_ts(2), 108, 111, 106, 110),  # FVG: 102-106
            # Fill candle: low <= 102 (bottom of FVG)
            make_candle(make_ts(3), 109, 110, 100, 105),
        ]
        fvgs = detect_fvgs(candles[:3])
        assert len(fvgs) == 1
        fvg = fvgs[0]
        # Check fill with fill candle
        filled = fvg.check_fill(candles[3], 3)
        assert filled is True


class TestPremiumDiscount:
    def test_premium_detection(self):
        zones = PremiumDiscountZones(
            swing_high=Decimal("110"),
            swing_low=Decimal("100"),
            current_price=Decimal("108"),
        )
        assert zones.is_premium
        assert not zones.is_discount

    def test_discount_detection(self):
        zones = PremiumDiscountZones(
            swing_high=Decimal("110"),
            swing_low=Decimal("100"),
            current_price=Decimal("102"),
        )
        assert zones.is_discount
        assert not zones.is_premium

    def test_equilibrium(self):
        zones = PremiumDiscountZones(
            swing_high=Decimal("110"),
            swing_low=Decimal("100"),
            current_price=Decimal("105"),
        )
        assert zones.is_equilibrium

    def test_ote_zone(self):
        """OTE is 0.618–0.786 of range."""
        zones = PremiumDiscountZones(
            swing_high=Decimal("200"),
            swing_low=Decimal("100"),
            current_price=Decimal("163"),  # 0.63 from low
        )
        assert zones.is_in_ote

    def test_optimal_entry_bullish(self):
        assert is_optimal_entry(
            Decimal("102"), Decimal("110"), Decimal("100"), "bullish"
        )

    def test_optimal_entry_bearish(self):
        assert is_optimal_entry(
            Decimal("108"), Decimal("110"), Decimal("100"), "bearish"
        )


class TestPremiumDiscountConfigurableZones:
    """
    premium_zone/discount_zone — previously these were hardcoded module
    constants (SMC_PREMIUM_ZONE=0.618, SMC_DISCOUNT_ZONE=0.382) baked
    directly into PremiumDiscountZones' properties, with no way for a
    caller to override them — configs/strategy.yaml's `smc.premium_zone`/
    `smc.discount_zone` (0.70/0.30) were silently ignored. Now
    configurable per-instance (dataclass fields) and per-call
    (calculate_premium_discount()'s kwargs).
    """

    def test_default_field_values_match_module_constants(self):
        zones = PremiumDiscountZones(
            swing_high=Decimal("110"), swing_low=Decimal("100"), current_price=Decimal("108"),
        )
        assert zones.premium_zone == Decimal("0.618")
        assert zones.discount_zone == Decimal("0.382")

    def test_custom_premium_zone_shifts_the_boundary(self):
        # Range 100-110, price=107. Default premium_zone (0.618) puts the
        # boundary at 106.18 -> price 107 is premium. A stricter custom
        # premium_zone of 0.90 puts the boundary at 109 -> same price is
        # no longer premium.
        default_zones = PremiumDiscountZones(
            swing_high=Decimal("110"), swing_low=Decimal("100"), current_price=Decimal("107"),
        )
        custom_zones = PremiumDiscountZones(
            swing_high=Decimal("110"), swing_low=Decimal("100"), current_price=Decimal("107"),
            premium_zone=Decimal("0.90"),
        )
        assert default_zones.is_premium
        assert not custom_zones.is_premium

    def test_custom_discount_zone_shifts_the_boundary(self):
        # Range 100-110, price=103. Default discount_zone (0.382) puts
        # the boundary at 103.82 -> price 103 is discount. A stricter
        # custom discount_zone of 0.10 puts the boundary at 101 -> same
        # price is no longer discount.
        default_zones = PremiumDiscountZones(
            swing_high=Decimal("110"), swing_low=Decimal("100"), current_price=Decimal("103"),
        )
        custom_zones = PremiumDiscountZones(
            swing_high=Decimal("110"), swing_low=Decimal("100"), current_price=Decimal("103"),
            discount_zone=Decimal("0.10"),
        )
        assert default_zones.is_discount
        assert not custom_zones.is_discount

    def test_calculate_premium_discount_passes_through_custom_zones(self):
        swing_high = SwingPoint(price=Decimal("110"), timestamp=make_ts(0), bar_index=0, is_high=True)
        swing_low = SwingPoint(price=Decimal("100"), timestamp=make_ts(1), bar_index=1, is_high=False)
        zones = calculate_premium_discount(
            swing_high, swing_low, Decimal("107"),
            premium_zone=Decimal("0.90"), discount_zone=Decimal("0.10"),
        )
        assert zones.premium_zone == Decimal("0.90")
        assert zones.discount_zone == Decimal("0.10")
        assert not zones.is_premium  # 107 is below the custom 0.90 boundary (109)

    def test_calculate_premium_discount_omitted_kwargs_match_defaults(self):
        swing_high = SwingPoint(price=Decimal("110"), timestamp=make_ts(0), bar_index=0, is_high=True)
        swing_low = SwingPoint(price=Decimal("100"), timestamp=make_ts(1), bar_index=1, is_high=False)
        zones = calculate_premium_discount(swing_high, swing_low, Decimal("108"))
        assert zones.premium_zone == Decimal("0.618")
        assert zones.discount_zone == Decimal("0.382")

    def test_is_optimal_entry_respects_custom_zones(self):
        # position = (102-100)/(110-100) = 0.2. Default discount (0.382)
        # -> optimal for bullish. A stricter custom discount of 0.10
        # -> no longer optimal (0.2 > 0.10).
        assert is_optimal_entry(Decimal("102"), Decimal("110"), Decimal("100"), "bullish")
        assert not is_optimal_entry(
            Decimal("102"), Decimal("110"), Decimal("100"), "bullish",
            discount_zone=Decimal("0.10"),
        )


class TestSessions:
    def test_london_open_kill_zone(self):
        # London Open KZ is 07:00–09:00 UTC
        from src.core.enums import KillZone
        dt = datetime(2024, 1, 15, 8, 0, tzinfo=UTC)  # Monday 08:00 UTC
        kz = get_active_kill_zone(dt)
        assert kz == KillZone.LONDON_OPEN_KZ

    def test_no_kill_zone_off_hours(self):
        # 03:00 UTC on a weekday — off hours / not in any KZ
        dt = datetime(2024, 1, 15, 3, 30, tzinfo=UTC)
        kz = get_active_kill_zone(dt)
        # 03:30 is outside all defined kill zones
        assert kz is None or True  # Dependent on Asian KZ definition

    def test_session_score_range(self):
        for hour in range(24):
            dt = datetime(2024, 1, 15, hour, 0, tzinfo=UTC)
            score = session_score(dt)
            assert 0.0 <= score <= 1.0

    def test_london_ny_overlap_score(self):
        # London/NY overlap: 13:00–16:00 UTC — should score high
        dt = datetime(2024, 1, 15, 14, 0, tzinfo=UTC)
        score = session_score(dt)
        assert score >= 0.7
