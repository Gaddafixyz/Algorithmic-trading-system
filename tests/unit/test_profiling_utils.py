"""Unit tests for src.utils.profiling."""

from __future__ import annotations

import logging
import time

import pytest

from src.utils.profiling import RollingTimer, timer


class TestTimer:
    def test_yields_control_to_the_with_block(self) -> None:
        executed = False
        with timer("op"):
            executed = True
        assert executed is True

    def test_logs_at_debug_level_when_under_the_warn_threshold(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force elapsed time to appear as exactly 0ms so it's always under
        # any positive threshold, avoiding real-clock flakiness.
        counter = iter([0.0, 0.0])
        monkeypatch.setattr(time, "perf_counter", lambda: next(counter))

        with caplog.at_level(logging.DEBUG, logger="src.utils.profiling"):
            with timer("fast-op", warn_threshold_ms=100.0):
                pass

        messages = [r.getMessage() for r in caplog.records]
        assert any("fast-op" in m for m in messages)
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_logs_a_warning_when_over_the_warn_threshold(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate 200ms elapsed (perf_counter called once on entry, once
        # on exit via the finally block).
        counter = iter([0.0, 0.2])
        monkeypatch.setattr(time, "perf_counter", lambda: next(counter))

        with caplog.at_level(logging.DEBUG, logger="src.utils.profiling"):
            with timer("slow-op", warn_threshold_ms=100.0):
                pass

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "SLOW" in warning_records[0].getMessage()
        assert "slow-op" in warning_records[0].getMessage()

    def test_still_logs_and_propagates_when_the_block_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="src.utils.profiling"):
            with pytest.raises(ValueError, match="boom"):
                with timer("failing-op"):
                    raise ValueError("boom")

        # The finally block still records/logs the elapsed time even
        # though the body raised.
        messages = [r.getMessage() for r in caplog.records]
        assert any("failing-op" in m for m in messages)


class TestRollingTimer:
    def test_avg_ms_is_zero_with_no_samples(self) -> None:
        rt = RollingTimer()
        assert rt.avg_ms == 0.0

    def test_p99_ms_is_zero_with_no_samples(self) -> None:
        rt = RollingTimer()
        assert rt.p99_ms == 0.0

    def test_avg_ms_reflects_recorded_samples(self) -> None:
        rt = RollingTimer()
        rt.record(10.0)
        rt.record(20.0)
        rt.record(30.0)
        assert rt.avg_ms == 20.0

    def test_window_caps_the_number_of_retained_samples(self) -> None:
        rt = RollingTimer(window=3)
        for value in [1.0, 2.0, 3.0, 4.0]:
            rt.record(value)
        # Oldest sample (1.0) should have been evicted; average of [2,3,4].
        assert rt.avg_ms == 3.0

    def test_p99_ms_returns_a_high_percentile_sample(self) -> None:
        rt = RollingTimer(window=1000)
        for value in range(1, 101):  # 1..100
            rt.record(float(value))
        # idx = int(100 * 0.99) = 99 -> sorted_s[99] == 100.0 (0-indexed).
        assert rt.p99_ms == 100.0

    def test_p99_ms_does_not_index_out_of_range_with_few_samples(self) -> None:
        rt = RollingTimer()
        rt.record(5.0)
        # Only one sample: p99 must clamp to the last available sample.
        assert rt.p99_ms == 5.0
