"""Unit tests for src.indicators.momentum.

This module fed the SMC confluence scoring pipeline with no test coverage
at all prior to this file — a silent regression in any of these three
functions would have propagated straight into live signal scoring.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from src.core.models import OHLCV
from src.indicators.momentum import rate_of_change, stochastic, williams_r


def make_candles(n: int, base_price: float = 100.0, trend: float = 0.1) -> list[OHLCV]:
    """Generate synthetic OHLCV candles for testing."""
    candles = []
    now = datetime(2024, 1, 1, tzinfo=UTC)
    price = base_price
    for i in range(n):
        price += trend
        high = Decimal(str(price + 1.0))
        low = Decimal(str(price - 1.0))
        open_ = Decimal(str(price - 0.3))
        close = Decimal(str(price + 0.3))
        candles.append(
            OHLCV(
                timestamp=now + timedelta(hours=i),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal("100.0"),
            )
        )
    return candles


def make_flat_candles(n: int, price: float = 100.0) -> list[OHLCV]:
    """Candles with identical high/low/close every bar (zero range)."""
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        OHLCV(
            timestamp=now + timedelta(hours=i),
            open=Decimal(str(price)),
            high=Decimal(str(price)),
            low=Decimal(str(price)),
            close=Decimal(str(price)),
            volume=Decimal("100.0"),
        )
        for i in range(n)
    ]


class TestRateOfChange:
    def test_length_matches_input(self) -> None:
        candles = make_candles(30)
        roc = rate_of_change(candles, period=14)
        assert len(roc) == 30

    def test_first_period_values_are_nan(self) -> None:
        candles = make_candles(30)
        roc = rate_of_change(candles, period=14)
        assert all(math.isnan(v) for v in roc[:14])
        assert not math.isnan(roc[14])

    def test_positive_trend_gives_positive_roc(self) -> None:
        candles = make_candles(30, trend=1.0)
        roc = rate_of_change(candles, period=10)
        assert roc[-1] > 0

    def test_negative_trend_gives_negative_roc(self) -> None:
        candles = make_candles(30, trend=-1.0)
        roc = rate_of_change(candles, period=10)
        assert roc[-1] < 0

    def test_flat_price_gives_zero_roc(self) -> None:
        candles = make_flat_candles(20)
        roc = rate_of_change(candles, period=10)
        assert roc[-1] == pytest.approx(0.0)

    def test_zero_prior_price_does_not_raise_division_error(self) -> None:
        candles = make_flat_candles(20, price=0.0)
        # prev == 0.0 for every window; the guard must skip these rather
        # than dividing by zero.
        roc = rate_of_change(candles, period=10)
        assert all(math.isnan(v) for v in roc[10:])


class TestStochastic:
    def test_returns_k_and_d_of_correct_length(self) -> None:
        candles = make_candles(40)
        k, d = stochastic(candles, k_period=14, d_period=3)
        assert len(k) == 40
        assert len(d) == 40

    def test_k_is_bounded_0_to_100(self) -> None:
        candles = make_candles(40, trend=0.3)
        k, _ = stochastic(candles)
        valid = k[~np.isnan(k)]
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_flat_range_defaults_k_to_50(self) -> None:
        """When high == low over the lookback window, %K is undefined;
        the implementation defaults to the midpoint (50) rather than
        raising or dividing by zero."""
        candles = make_flat_candles(20)
        k, _ = stochastic(candles, k_period=14)
        assert k[13] == 50.0

    def test_d_is_sma_of_k_and_appears_after_k_period_plus_d_period(self) -> None:
        candles = make_candles(40, trend=0.5)
        k, d = stochastic(candles, k_period=14, d_period=3)
        # d needs d_period valid k values before it can be computed.
        first_valid_k = np.where(~np.isnan(k))[0][0]
        first_valid_d = np.where(~np.isnan(d))[0][0]
        assert first_valid_d == first_valid_k + 2  # SMA(3) needs 2 extra bars

    def test_too_few_candles_for_d_period_leaves_d_all_nan(self) -> None:
        candles = make_candles(15)  # only 2 valid %K values with k_period=14
        k, d = stochastic(candles, k_period=14, d_period=3)
        assert np.isnan(d).all()


class TestWilliamsR:
    def test_length_matches_input(self) -> None:
        candles = make_candles(30)
        wr = williams_r(candles, period=14)
        assert len(wr) == 30

    def test_bounded_between_negative_100_and_0(self) -> None:
        candles = make_candles(30, trend=0.4)
        wr = williams_r(candles)
        valid = wr[~np.isnan(wr)]
        assert (valid >= -100).all()
        assert (valid <= 0).all()

    def test_flat_range_is_nan_not_a_crash(self) -> None:
        candles = make_flat_candles(20)
        wr = williams_r(candles, period=14)
        # h == l for every window; the implementation leaves these NaN
        # rather than dividing by zero.
        assert math.isnan(wr[13])
