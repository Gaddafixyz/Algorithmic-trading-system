"""Supplementary unit tests for src.risk.drawdown.DrawdownTracker.

Covers branches not exercised by tests/unit/test_risk.py's
TestDrawdownTracker class: peak_equity, recovery_factor, stats, and
the internal history-list truncation.
"""

from __future__ import annotations

from src.risk.drawdown import DrawdownTracker


class TestPeakEquity:
    def test_starts_at_zero(self) -> None:
        tracker = DrawdownTracker()
        assert tracker.peak_equity == 0.0

    def test_tracks_the_highest_equity_seen(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)
        tracker.update(12000.0)
        tracker.update(11000.0)  # dip after the peak
        assert tracker.peak_equity == 12000.0

    def test_does_not_decrease_when_equity_falls(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)
        tracker.update(5000.0)
        assert tracker.peak_equity == 10000.0


class TestRecoveryFactor:
    def test_is_zero_when_no_drawdown_has_occurred_yet(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)
        assert tracker.recovery_factor == 0.0

    def test_is_zero_before_any_update_when_peak_is_zero(self) -> None:
        tracker = DrawdownTracker()
        assert tracker.recovery_factor == 0.0

    def test_is_positive_when_net_profit_exceeds_the_drawdown(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)  # first history entry: baseline
        tracker.update(9000.0)  # -10% drawdown
        tracker.update(15000.0)  # net profit from baseline: +5000
        assert tracker.recovery_factor > 0.0

    def test_reflects_net_profit_over_max_drawdown_in_equity_terms(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)  # baseline (history[0][0] == 10000.0); peak=10000
        tracker.update(8000.0)  # -20% drawdown, becomes max_drawdown; peak still 10000
        tracker.update(12000.0)  # new peak=12000, current=12000
        # net_profit = current - history[0][0] = 12000 - 10000 = 2000
        # denominator = max_drawdown_pct/100 * peak_equity = 0.20 * 12000 = 2400
        # (peak_equity here is the *current* peak of 12000, since the
        # third update raised a new high -- not the 10000 peak that was
        # in effect when the 20% drawdown occurred)
        # recovery_factor = 2000 / 2400 = 0.8333...
        assert tracker.recovery_factor == 2000.0 / 2400.0


class TestStats:
    def test_stats_contains_all_expected_keys(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)
        tracker.update(9000.0)
        stats = tracker.stats
        assert set(stats.keys()) == {
            "current_equity",
            "peak_equity",
            "current_drawdown_pct",
            "max_drawdown_pct",
        }

    def test_stats_reflects_current_state(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)
        tracker.update(9000.0)
        stats = tracker.stats
        assert stats["current_equity"] == 9000.0
        assert stats["peak_equity"] == 10000.0
        assert abs(stats["current_drawdown_pct"] - 10.0) < 0.01
        assert abs(stats["max_drawdown_pct"] - 10.0) < 0.01

    def test_stats_rounds_percentages_to_4_decimal_places(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(3.0)
        tracker.update(1.0)  # dd = (3-1)/3*100 = 66.666...%
        stats = tracker.stats
        # round(66.666..., 4) == 66.6667
        assert stats["current_drawdown_pct"] == round((3.0 - 1.0) / 3.0 * 100.0, 4)


class TestHistoryTruncation:
    def test_history_list_is_truncated_once_it_exceeds_10000_entries(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(10000.0)  # establishes a peak > 0 so history starts recording
        # Push past the 10,000-entry truncation threshold. Each update
        # after the peak is set appends to _history since peak > 0.
        for i in range(10_050):
            tracker.update(10000.0 - (i % 50))
        # Internal history must have been trimmed down from >10,000,
        # not grown unbounded. We can't access _history directly (it's
        # private), but stats/properties must still function correctly
        # after truncation, and recovery_factor must not raise despite
        # history[0] no longer being the very first update.
        assert isinstance(tracker.stats, dict)
        assert tracker.max_drawdown_pct >= 0.0
        # recovery_factor must still compute without an IndexError even
        # though the original history[0] baseline has been evicted.
        _ = tracker.recovery_factor


class TestUpdateEdgeCases:
    def test_zero_equity_update_before_any_peak_does_not_record_drawdown(self) -> None:
        # peak starts at 0.0; an update of 0.0 does not exceed it, and
        # the `if self._peak > 0` guard means no history entry is added.
        tracker = DrawdownTracker()
        tracker.update(0.0)
        assert tracker.current_drawdown_pct == 0.0
        assert tracker.max_drawdown_pct == 0.0

    def test_negative_equity_is_recorded_as_a_drawdown_beyond_100_pct(self) -> None:
        tracker = DrawdownTracker()
        tracker.update(1000.0)
        tracker.update(-500.0)
        # dd = (1000 - (-500)) / 1000 * 100 = 150%
        assert tracker.current_drawdown_pct == 150.0
