"""Unit tests for src.core.enums.Timeframe.higher_timeframe.

Added to support the MTF alignment filter's configurable
mtf_multiplier (see strategy/filters.py::filter_by_mtf_alignment and
StrategyOrchestrator's mtf_alignment_filter/mtf_multiplier).
"""

from __future__ import annotations

import pytest

from src.core.enums import Timeframe


class TestHigherTimeframe:
    def test_exact_multiple_match(self) -> None:
        assert Timeframe.H1.higher_timeframe(4) == Timeframe.H4
        assert Timeframe.M15.higher_timeframe(4) == Timeframe.H1

    def test_multiplier_one_returns_self(self) -> None:
        assert Timeframe.H1.higher_timeframe(1) == Timeframe.H1

    def test_rounds_up_to_next_available_timeframe(self) -> None:
        # H2 * 4 = 8h; no exact 8h step... but H8 exists exactly, so use
        # H2 * 5 = 10h instead, which has no exact match and must round
        # up to H12 (12h is the smallest available duration >= 10h).
        assert Timeframe.H2.higher_timeframe(5) == Timeframe.H12

    def test_returns_none_when_nothing_is_long_enough(self) -> None:
        assert Timeframe.MN1.higher_timeframe(2) is None
        assert Timeframe.W1.higher_timeframe(100) is None

    def test_smallest_timeframe_large_multiplier(self) -> None:
        # 1m * 1440 = 1 day
        assert Timeframe.M1.higher_timeframe(1440) == Timeframe.D1

    def test_raises_on_multiplier_below_one(self) -> None:
        with pytest.raises(ValueError):
            Timeframe.H1.higher_timeframe(0)
        with pytest.raises(ValueError):
            Timeframe.H1.higher_timeframe(-1)
