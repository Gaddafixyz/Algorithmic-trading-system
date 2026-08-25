"""Unit tests for src.analytics.advanced_metrics."""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.analytics.advanced_metrics import (
    deflated_sharpe_ratio,
    historical_cvar,
    historical_var,
    omega_ratio,
    parametric_var,
    probabilistic_sharpe_ratio,
    tail_ratio,
    ulcer_index,
)


class TestProbabilisticSharpeRatio:
    def test_uninformative_with_too_little_data(self):
        assert probabilistic_sharpe_ratio([0.01, 0.02]) == 0.5

    def test_zero_variance_returns_uninformative(self):
        assert probabilistic_sharpe_ratio([0.01] * 50) == 0.5

    def test_strong_consistent_positive_returns_high_psr(self):
        rng = np.random.default_rng(42)
        returns = rng.normal(loc=0.002, scale=0.005, size=500)
        psr = probabilistic_sharpe_ratio(returns)
        assert 0.9 < psr <= 1.0

    def test_noisy_near_zero_returns_uninformative_psr(self):
        rng = np.random.default_rng(7)
        returns = rng.normal(loc=0.0, scale=0.02, size=30)
        psr = probabilistic_sharpe_ratio(returns)
        assert 0.0 <= psr <= 1.0


class TestDeflatedSharpeRatio:
    def test_single_trial_equals_psr_at_benchmark_zero(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(loc=0.001, scale=0.01, size=252)
        dsr = deflated_sharpe_ratio(returns, n_trials=1)
        psr = probabilistic_sharpe_ratio(returns, benchmark_sharpe=0.0)
        assert dsr == pytest.approx(psr, abs=1e-9)

    def test_more_trials_deflates_the_ratio(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(loc=0.001, scale=0.01, size=252)
        dsr_1 = deflated_sharpe_ratio(returns, n_trials=1)
        dsr_200 = deflated_sharpe_ratio(returns, n_trials=200)
        assert dsr_200 <= dsr_1

    def test_rejects_invalid_trial_count(self):
        with pytest.raises(ValueError):
            deflated_sharpe_ratio([0.01, 0.02, 0.03], n_trials=0)


class TestOmegaRatio:
    def test_all_gains_is_infinite(self):
        assert omega_ratio([0.01, 0.02, 0.03]) == float("inf")

    def test_symmetric_returns_near_one(self):
        returns = [0.01, -0.01, 0.02, -0.02]
        assert omega_ratio(returns, threshold=0.0) == pytest.approx(1.0)

    def test_more_gains_than_losses(self):
        returns = [0.05, 0.05, 0.05, -0.01]
        assert omega_ratio(returns) > 1.0


class TestTailRatio:
    def test_symmetric_distribution_near_one(self):
        rng = np.random.default_rng(3)
        returns = rng.normal(0, 0.01, 1000)
        assert tail_ratio(returns) == pytest.approx(1.0, abs=0.35)

    def test_insufficient_data_returns_zero(self):
        assert tail_ratio([0.01]) == 0.0


class TestUlcerIndex:
    def test_monotonic_growth_has_zero_ulcer(self):
        curve = [100, 105, 110, 120, 130]
        assert ulcer_index(curve) == 0.0

    def test_drawdown_produces_positive_ulcer(self):
        curve = [100, 120, 90, 80, 100]
        assert ulcer_index(curve) > 0.0

    def test_short_series_is_zero(self):
        assert ulcer_index([100]) == 0.0


class TestVarCvar:
    def test_historical_var_is_positive_for_losses(self):
        rng = np.random.default_rng(5)
        returns = rng.normal(-0.001, 0.02, 1000)
        var = historical_var(returns, confidence=0.95)
        assert var > 0

    def test_cvar_at_least_as_large_as_var(self):
        rng = np.random.default_rng(5)
        returns = rng.normal(-0.001, 0.02, 1000)
        var = historical_var(returns, confidence=0.95)
        cvar = historical_cvar(returns, confidence=0.95)
        assert cvar >= var

    def test_parametric_var_scales_with_std(self):
        low = parametric_var(mean_return=0.0, std_return=0.01, confidence=0.95)
        high = parametric_var(mean_return=0.0, std_return=0.05, confidence=0.95)
        assert high > low

    def test_parametric_var_never_negative(self):
        assert parametric_var(mean_return=0.5, std_return=0.001, confidence=0.95) >= 0.0
