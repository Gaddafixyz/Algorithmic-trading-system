"""Unit tests for technical indicators."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from src.core.models import OHLCV
from src.indicators.atr import calculate_atr, current_atr
from src.indicators.ema import calculate_ema, ema_from_candles
from src.indicators.rsi import calculate_rsi, current_rsi, is_overbought, is_oversold
from src.indicators.sma import calculate_sma, current_sma
from src.indicators.macd import calculate_macd, macd_from_candles
from src.indicators.volume import relative_volume, obv, vwap
from src.indicators.volatility import bollinger_bands, historical_volatility
from src.core.exceptions import InsufficientDataError


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
        candles.append(OHLCV(
            timestamp=now + timedelta(hours=i),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=Decimal("100.0"),
        ))
    return candles


class TestATR:
    def test_basic_calculation(self):
        candles = make_candles(50)
        atr = calculate_atr(candles, period=14)
        assert len(atr) == 50
        # First 13 values should be NaN
        assert all(math.isnan(v) for v in atr[:13])
        # From index 13 onwards, should be valid
        assert all(not math.isnan(v) for v in atr[13:])
        # ATR should be positive
        assert all(v > 0 for v in atr[13:])

    def test_insufficient_data(self):
        candles = make_candles(5)
        with pytest.raises(InsufficientDataError):
            calculate_atr(candles, period=14)

    def test_current_atr(self):
        candles = make_candles(50)
        atr = current_atr(candles, period=14)
        assert isinstance(atr, float)
        assert atr > 0

    def test_trending_market_higher_atr(self):
        """Trending market should have higher ATR than flat market."""
        flat = make_candles(50, trend=0.0)
        trending = make_candles(50, trend=2.0)
        assert current_atr(trending) >= current_atr(flat)


class TestEMA:
    def test_basic_calculation(self):
        values = [float(i) for i in range(1, 31)]
        ema = calculate_ema(values, period=10)
        assert len(ema) == 30
        assert all(math.isnan(v) for v in ema[:9])
        assert all(not math.isnan(v) for v in ema[9:])

    def test_seed_equals_sma(self):
        """First EMA value should equal SMA of first `period` values."""
        values = [10.0] * 20
        ema = calculate_ema(values, period=10)
        sma = calculate_sma(values, period=10)
        # At index 9, EMA seed should equal SMA
        assert abs(ema[9] - sma[9]) < 1e-9

    def test_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            calculate_ema([1.0, 2.0], period=10)

    def test_constant_series(self):
        """EMA of constant series should equal that constant."""
        values = [5.0] * 30
        ema = calculate_ema(values, period=10)
        assert all(abs(v - 5.0) < 1e-9 for v in ema[9:])

    def test_from_candles(self):
        candles = make_candles(50)
        ema = ema_from_candles(candles, period=14)
        assert len(ema) == 50
        assert not math.isnan(ema[-1])


class TestRSI:
    def test_range(self):
        candles = make_candles(50)
        closes = [float(c.close) for c in candles]
        rsi = calculate_rsi(closes, period=14)
        valid = [v for v in rsi if not math.isnan(v)]
        assert all(0.0 <= v <= 100.0 for v in valid)

    def test_overbought_oversold(self):
        assert is_overbought(75.0)
        assert not is_overbought(65.0)
        assert is_oversold(25.0)
        assert not is_oversold(35.0)

    def test_uptrend_high_rsi(self):
        """Strong uptrend should produce RSI > 50."""
        candles = make_candles(60, trend=2.0)
        rsi = current_rsi(candles)
        assert rsi > 50.0

    def test_insufficient_data(self):
        with pytest.raises(InsufficientDataError):
            calculate_rsi([1.0, 2.0], period=14)


class TestSMA:
    def test_basic(self):
        values = [float(i) for i in range(1, 11)]  # 1..10
        sma = calculate_sma(values, period=3)
        # At index 2: mean(1,2,3) = 2.0
        assert abs(sma[2] - 2.0) < 1e-9
        # At index 9: mean(8,9,10) = 9.0
        assert abs(sma[9] - 9.0) < 1e-9

    def test_nan_before_period(self):
        values = [1.0] * 20
        sma = calculate_sma(values, period=5)
        assert all(math.isnan(v) for v in sma[:4])
        assert all(not math.isnan(v) for v in sma[4:])

    def test_current_sma(self):
        candles = make_candles(30)
        sma = current_sma(candles, period=20)
        assert isinstance(sma, float)


class TestMACD:
    def test_basic(self):
        candles = make_candles(60)
        result = macd_from_candles(candles)
        assert len(result.macd_line) == 60
        assert len(result.signal_line) == 60
        assert len(result.histogram) == 60
        # Should have valid values in the later portion
        assert not math.isnan(result.macd_line[-1])

    def test_histogram_is_macd_minus_signal(self):
        candles = make_candles(60)
        result = macd_from_candles(candles)
        i = -1
        if not (math.isnan(result.macd_line[i]) or math.isnan(result.signal_line[i])):
            expected = result.macd_line[i] - result.signal_line[i]
            assert abs(result.histogram[i] - expected) < 1e-9


class TestVolumeIndicators:
    def test_relative_volume(self):
        candles = make_candles(30)
        rv = relative_volume(candles)
        assert rv > 0.0
        # Flat volume should give RV ≈ 1.0
        assert abs(rv - 1.0) < 0.1

    def test_obv_uptrend(self):
        candles = make_candles(20, trend=1.0)
        obv_vals = obv(candles)
        # OBV should trend upward in bullish candles
        assert obv_vals[-1] > obv_vals[0]

    def test_vwap(self):
        candles = make_candles(10)
        vwap_price = vwap(candles)
        # VWAP should be within the price range
        all_closes = [float(c.close) for c in candles]
        assert min(all_closes) <= vwap_price <= max(all_closes) + 1.0


class TestBollingerBands:
    def test_structure(self):
        candles = make_candles(30)
        bb = bollinger_bands(candles)
        assert len(bb.upper) == 30
        assert len(bb.middle) == 30
        assert len(bb.lower) == 30
        # Upper must be >= middle >= lower where valid
        for i in range(19, 30):
            if not math.isnan(bb.upper[i]):
                assert bb.upper[i] >= bb.middle[i]
                assert bb.middle[i] >= bb.lower[i]

    def test_constant_price_zero_bandwidth(self):
        """With constant price, bandwidth should approach zero."""
        candles = [
            OHLCV(
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i),
                open=Decimal("100"), high=Decimal("100"),
                low=Decimal("100"), close=Decimal("100"),
                volume=Decimal("100"),
            )
            for i in range(30)
        ]
        bb = bollinger_bands(candles)
        assert abs(bb.bandwidth[-1]) < 1e-9

    def test_historical_volatility(self):
        candles = make_candles(50, trend=0.5)
        hv = historical_volatility(candles)
        assert isinstance(hv, float)
        assert 0.0 <= hv < 10.0  # Reasonable range
