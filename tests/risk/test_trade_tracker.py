"""Unit tests for src.risk.trade_tracker.RollingTradeTracker."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.risk.trade_tracker import RollingTradeTracker


class TestConstruction:
    def test_rejects_non_positive_window(self) -> None:
        with pytest.raises(ValueError):
            RollingTradeTracker(window=0)

    def test_empty_tracker_has_zero_sample_size(self) -> None:
        tracker = RollingTradeTracker()
        assert tracker.sample_size == 0
        assert tracker.win_rate == 0.0
        assert tracker.avg_win == 0.0
        assert tracker.avg_loss == 0.0


class TestRecordAndStats:
    def test_win_rate_reflects_recorded_outcomes(self) -> None:
        tracker = RollingTradeTracker(window=10)
        tracker.record(Decimal("100"))
        tracker.record(Decimal("100"))
        tracker.record(Decimal("-50"))
        tracker.record(Decimal("-50"))
        assert tracker.win_rate == 0.5
        assert tracker.sample_size == 4

    def test_avg_win_only_averages_positive_outcomes(self) -> None:
        tracker = RollingTradeTracker(window=10)
        tracker.record(Decimal("100"))
        tracker.record(Decimal("300"))
        tracker.record(Decimal("-50"))
        assert tracker.avg_win == pytest.approx(200.0)

    def test_avg_loss_returns_positive_magnitude(self) -> None:
        tracker = RollingTradeTracker(window=10)
        tracker.record(Decimal("100"))
        tracker.record(Decimal("-50"))
        tracker.record(Decimal("-150"))
        assert tracker.avg_loss == pytest.approx(100.0)

    def test_zero_pnl_trade_counts_as_neither_win_nor_loss(self) -> None:
        tracker = RollingTradeTracker(window=10)
        tracker.record(Decimal("100"))
        tracker.record(Decimal("0"))
        tracker.record(Decimal("-50"))
        assert tracker.sample_size == 3
        assert tracker.win_rate == pytest.approx(1 / 3)


class TestRollingWindow:
    def test_older_trades_evicted_beyond_window(self) -> None:
        tracker = RollingTradeTracker(window=3)
        tracker.record(Decimal("-100"))  # will be evicted
        tracker.record(Decimal("100"))
        tracker.record(Decimal("100"))
        tracker.record(Decimal("100"))
        assert tracker.sample_size == 3
        assert tracker.win_rate == 1.0


class TestMinSample:
    def test_has_min_sample_false_below_threshold(self) -> None:
        tracker = RollingTradeTracker(window=30)
        tracker.record(Decimal("100"))
        assert tracker.has_min_sample(20) is False

    def test_has_min_sample_true_at_threshold(self) -> None:
        tracker = RollingTradeTracker(window=30)
        for _ in range(20):
            tracker.record(Decimal("10"))
        assert tracker.has_min_sample(20) is True
