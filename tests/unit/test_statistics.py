"""Unit tests for src.indicators.statistics.

Backs zscore/correlation/regression-slope features consumed elsewhere
(e.g. machine_learning/features.py, analytics). No test file referenced
this module before this one.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.indicators.statistics import (
    linear_regression_slope,
    pearson_correlation,
    rolling_max,
    rolling_mean,
    rolling_min,
    rolling_std,
    zscore,
)


class TestRollingMean:
    def test_length_matches_input(self) -> None:
        values = np.arange(20, dtype=float)
        result = rolling_mean(values, period=5)
        assert len(result) == 20

    def test_first_period_minus_one_values_are_nan(self) -> None:
        values = np.arange(20, dtype=float)
        result = rolling_mean(values, period=5)
        assert all(math.isnan(v) for v in result[:4])
        assert not math.isnan(result[4])

    def test_matches_manual_average(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = rolling_mean(values, period=3)
        assert result[2] == pytest.approx((1.0 + 2.0 + 3.0) / 3)
        assert result[4] == pytest.approx((3.0 + 4.0 + 5.0) / 3)


class TestRollingStd:
    def test_constant_series_has_zero_std(self) -> None:
        values = np.full(10, 5.0)
        result = rolling_std(values, period=4)
        assert result[-1] == pytest.approx(0.0)

    def test_uses_sample_ddof_by_default(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0])
        result = rolling_std(values, period=4, ddof=1)
        assert result[-1] == pytest.approx(np.std(values, ddof=1))


class TestRollingMaxMin:
    def test_rolling_max_tracks_window_peak(self) -> None:
        values = np.array([1.0, 5.0, 2.0, 8.0, 3.0])
        result = rolling_max(values, period=3)
        assert result[2] == 5.0  # max(1,5,2)
        assert result[4] == 8.0  # max(2,8,3)

    def test_rolling_min_tracks_window_trough(self) -> None:
        values = np.array([5.0, 1.0, 4.0, 0.0, 3.0])
        result = rolling_min(values, period=3)
        assert result[2] == 1.0  # min(5,1,4)
        assert result[4] == 0.0  # min(4,0,3)


class TestZscore:
    def test_constant_series_gives_nan_not_division_by_zero(self) -> None:
        values = np.full(10, 5.0)
        result = zscore(values, period=4)
        # std == 0 for a constant series; implementation must guard this
        # rather than raising or returning inf.
        assert math.isnan(result[-1])

    def test_value_above_mean_gives_positive_zscore(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 100.0])
        result = zscore(values, period=4)
        assert result[-1] > 0

    def test_matches_manual_calculation(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = zscore(values, period=4)
        window = values[1:5]
        expected = (values[4] - np.mean(window)) / np.std(window, ddof=1)
        assert result[4] == pytest.approx(expected)


class TestPearsonCorrelation:
    def test_perfectly_correlated_series_returns_one(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        assert pearson_correlation(x, y) == pytest.approx(1.0)

    def test_perfectly_anticorrelated_series_returns_negative_one(self) -> None:
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        y = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
        assert pearson_correlation(x, y) == pytest.approx(-1.0)

    def test_fewer_than_two_points_returns_zero(self) -> None:
        assert pearson_correlation(np.array([1.0]), np.array([2.0])) == 0.0
        assert pearson_correlation(np.array([]), np.array([])) == 0.0


class TestLinearRegressionSlope:
    def test_length_matches_input(self) -> None:
        values = np.arange(20, dtype=float)
        result = linear_regression_slope(values, period=5)
        assert len(result) == 20

    def test_rising_line_gives_positive_slope(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = linear_regression_slope(values, period=5)
        assert result[-1] == pytest.approx(1.0)

    def test_falling_line_gives_negative_slope(self) -> None:
        values = np.array([5.0, 4.0, 3.0, 2.0, 1.0])
        result = linear_regression_slope(values, period=5)
        assert result[-1] == pytest.approx(-1.0)

    def test_flat_line_gives_zero_slope(self) -> None:
        values = np.full(6, 3.0)
        result = linear_regression_slope(values, period=5)
        assert result[-1] == pytest.approx(0.0, abs=1e-9)

    def test_window_containing_nan_is_skipped(self) -> None:
        values = np.array([1.0, 2.0, np.nan, 4.0, 5.0])
        result = linear_regression_slope(values, period=5)
        assert math.isnan(result[-1])
