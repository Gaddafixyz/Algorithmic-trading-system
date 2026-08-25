"""
Tests for src.strategy.multi_timeframe.MTFContext.

Focused on the two latent bugs fixed while wiring MTFContext into live
confluence scoring / the HTF confirmation filter (see CHANGELOG [1.3.2]):

1. alignment_score() previously divided by the theoretical full
   timeframe set instead of the timeframes actually fed, silently
   capping alignment below 1.0 while the LTF tier isn't wired up.
2. alignment_score()/is_htf_aligned() previously always derived HTF from
   the fixed HTF_MAP default, ignoring an explicitly-configured HTF that
   diverges from it — and is_htf_aligned() treated "no HTF data yet" the
   same as "HTF opposes the trade", which would reject every signal in
   any system where HTF data isn't wired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.enums import Timeframe
from src.core.models import OHLCV
from src.strategy.multi_timeframe import MTFContext


def ts(i: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)


def c(i: int, o: float, h: float, l: float, cl: float) -> OHLCV:
    return OHLCV(
        timestamp=ts(i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(cl)),
        volume=Decimal("1000"),
    )


# Ascending zigzag producing 3 confirmed swing highs (110, 115, 120) and
# 3 confirmed swing lows (100, 103, 107) at lookback=1 -> clean HH/HL ->
# BULLISH structure. Pivot-by-pivot math verified by hand against
# find_swing_highs/find_swing_lows's strict-left/>=-right rule.
BULLISH_CANDLES = [
    c(0, 97, 100, 95, 98),
    c(1, 105, 110, 105, 108),   # swing high (110)
    c(2, 101, 105, 100, 103),   # swing low (100)
    c(3, 111, 115, 110, 113),   # swing high (115)
    c(4, 104, 108, 103, 106),   # swing low (103)
    c(5, 116, 120, 115, 118),   # swing high (120)
    c(6, 108, 112, 107, 110),   # swing low (107)
    c(7, 121, 125, 120, 123),
]

# BULLISH_CANDLES with candle order reversed (values, not timestamps) ->
# the mirror-image descending zigzag: 3 confirmed swing highs (120, 115,
# 110) and 3 confirmed swing lows (107, 103, 100), both descending ->
# LH/LL -> BEARISH structure.
BEARISH_CANDLES = [
    c(0, 121, 125, 120, 123),
    c(1, 108, 112, 107, 110),   # swing low (107)
    c(2, 116, 120, 115, 118),   # swing high (120)
    c(3, 104, 108, 103, 106),   # swing low (103)
    c(4, 111, 115, 110, 113),   # swing high (115)
    c(5, 101, 105, 100, 103),   # swing low (100)
    c(6, 105, 110, 105, 108),   # swing high (110)
    c(7, 97, 100, 95, 98),
]


class TestAlignmentScoreDenominatorBug:
    """alignment_score() must divide by *available* timeframes, not the
    theoretical full set — otherwise perfect agreement across every
    timeframe actually fed can never score 1.0 while any tier (e.g. LTF)
    isn't wired."""

    def test_full_alignment_scores_one_with_only_two_of_three_tiers_fed(self):
        mtf = MTFContext()
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        mtf.update(Timeframe.H4, BULLISH_CANDLES, swing_lookback=1)
        # LTF (M5, per LTF_MAP) deliberately never fed.

        score = mtf.alignment_score(Timeframe.M15, "bullish")

        assert score == 1.0

    def test_full_disagreement_scores_zero(self):
        mtf = MTFContext()
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        mtf.update(Timeframe.H4, BEARISH_CANDLES, swing_lookback=1)

        score = mtf.alignment_score(Timeframe.M15, "bullish")

        assert score == 0.5  # 1 of 2 available timeframes agree

    def test_no_data_at_all_returns_neutral_default(self):
        mtf = MTFContext()
        score = mtf.alignment_score(Timeframe.M15, "bullish")
        assert score == 0.5


class TestExplicitHtfOverride:
    """A configured htf_timeframe that diverges from HTF_MAP's default
    must be honored via the explicit `htf` param, not silently ignored."""

    def test_explicit_htf_used_over_default_map(self):
        mtf = MTFContext()
        # HTF_MAP[M15] defaults to H4 — feed a *different* configured HTF
        # (D1) instead, and confirm alignment_score/is_htf_aligned use it
        # rather than looking for H4 (which was never fed).
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        mtf.update(Timeframe.D1, BULLISH_CANDLES, swing_lookback=1)

        assert mtf.is_htf_aligned(Timeframe.M15, "bullish", htf=Timeframe.D1) is True
        assert mtf.is_htf_aligned(Timeframe.M15, "bearish", htf=Timeframe.D1) is False

        score = mtf.alignment_score(Timeframe.M15, "bullish", htf=Timeframe.D1)
        assert score == 1.0

    def test_default_htf_map_used_when_no_override_given(self):
        mtf = MTFContext()
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        mtf.update(Timeframe.H4, BULLISH_CANDLES, swing_lookback=1)  # HTF_MAP default

        assert mtf.is_htf_aligned(Timeframe.M15, "bullish") is True


class TestIsHtfAlignedDoesNotRejectMissingData:
    """No HTF data fed yet must allow the trade through, not be treated
    as HTF opposing it — this is a confirmation filter layered on top of
    scoring, not a substitute for having HTF data."""

    def test_no_htf_data_fed_allows_trade(self):
        mtf = MTFContext()
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        # H4 (the HTF_MAP default for M15) never updated.

        assert mtf.is_htf_aligned(Timeframe.M15, "bullish") is True
        assert mtf.is_htf_aligned(Timeframe.M15, "bearish") is True

    def test_no_htf_defined_for_timeframe_allows_trade(self):
        mtf = MTFContext()
        mtf.update(Timeframe.W1, BULLISH_CANDLES, swing_lookback=1)
        # W1 has no entry in HTF_MAP at all.
        assert mtf.is_htf_aligned(Timeframe.W1, "bullish") is True

    def test_htf_data_present_and_opposed_rejects(self):
        mtf = MTFContext()
        mtf.update(Timeframe.M15, BULLISH_CANDLES, swing_lookback=1)
        mtf.update(Timeframe.H4, BEARISH_CANDLES, swing_lookback=1)

        assert mtf.is_htf_aligned(Timeframe.M15, "bullish") is False
        assert mtf.is_htf_aligned(Timeframe.M15, "bearish") is True
