"""Unit tests for src.risk.exposure."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from src.core.enums import PositionSide
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.risk.exposure import CorrelationTracker, ExposureTracker


def make_position(
    symbol: str,
    side: PositionSide,
    quantity: str,
    entry_price: str,
    mark_price: str | None = None,
) -> Position:
    return Position(
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        entry_quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        mark_price=Decimal(mark_price if mark_price is not None else entry_price),
    )


class TestExposureTrackerGrossAndNet:
    def test_gross_exposure_pct_is_zero_with_no_positions(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        assert tracker.gross_exposure_pct(portfolio, Decimal("10000")) == 0.0

    def test_gross_exposure_pct_reflects_total_notional_over_equity(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.2", "50000")
        )
        # notional = 0.2 * 50000 = 10000; equity = 10000 -> 100%
        assert tracker.gross_exposure_pct(portfolio, Decimal("10000")) == 100.0

    def test_gross_exposure_pct_returns_zero_for_non_positive_equity(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.2", "50000")
        )
        assert tracker.gross_exposure_pct(portfolio, Decimal("0")) == 0.0
        assert tracker.gross_exposure_pct(portfolio, Decimal("-100")) == 0.0

    def test_net_exposure_pct_nets_long_and_short_notional(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.2", "50000")
        )  # +10000 notional
        portfolio.add_position(
            make_position("ETHUSDT", PositionSide.SHORT, "2", "2000")
        )  # -4000 notional
        # net = 10000 - 4000 = 6000; equity = 10000 -> 60%
        assert tracker.net_exposure_pct(portfolio, Decimal("10000")) == 60.0

    def test_net_exposure_pct_returns_zero_for_non_positive_equity(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        assert tracker.net_exposure_pct(portfolio, Decimal("0")) == 0.0


class TestExposureTrackerLimits:
    def test_is_within_limits_true_when_under_the_max(self) -> None:
        tracker = ExposureTracker(max_gross_exposure_pct=200.0)
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.1", "50000")
        )
        # gross = 5000/10000*100 = 50% < 200%
        assert tracker.is_within_limits(portfolio, Decimal("10000")) is True

    def test_is_within_limits_false_when_over_the_max(self) -> None:
        tracker = ExposureTracker(max_gross_exposure_pct=50.0)
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.2", "50000")
        )
        # gross = 10000/10000*100 = 100% > 50%
        assert tracker.is_within_limits(portfolio, Decimal("10000")) is False

    def test_is_within_limits_at_exactly_the_boundary_is_true(self) -> None:
        tracker = ExposureTracker(max_gross_exposure_pct=100.0)
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.2", "50000")
        )
        # gross = exactly 100%, boundary check is inclusive (<=).
        assert tracker.is_within_limits(portfolio, Decimal("10000")) is True


class TestExposureTrackerConcentration:
    def test_symbol_concentration_reflects_that_symbols_notional_over_equity(
        self,
    ) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.1", "50000")
        )
        portfolio.add_position(
            make_position("ETHUSDT", PositionSide.LONG, "1", "3000")
        )
        # BTC notional = 5000; equity = 10000 -> 50%
        assert tracker.symbol_concentration(portfolio, "BTCUSDT", Decimal("10000")) == 50.0

    def test_symbol_concentration_is_zero_for_a_symbol_with_no_positions(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        assert tracker.symbol_concentration(portfolio, "SOLUSDT", Decimal("10000")) == 0.0

    def test_symbol_concentration_returns_zero_for_non_positive_equity(self) -> None:
        tracker = ExposureTracker()
        portfolio = Portfolio()
        portfolio.add_position(
            make_position("BTCUSDT", PositionSide.LONG, "0.1", "50000")
        )
        assert tracker.symbol_concentration(portfolio, "BTCUSDT", Decimal("0")) == 0.0


class TestCorrelationTrackerConstruction:
    def test_raises_for_window_below_2(self) -> None:
        with pytest.raises(ValueError, match="window must be >= 2"):
            CorrelationTracker(symbols=["BTCUSDT"], window=1)

    def test_symbols_property_returns_a_copy(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"])
        symbols = tracker.symbols
        symbols.append("SHOULD_NOT_LEAK")
        assert tracker.symbols == ["BTCUSDT", "ETHUSDT"]

    def test_window_property_reflects_constructor_argument(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=30)
        assert tracker.window == 30

    def test_repr_includes_symbols_window_and_observation_counts(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=10)
        tracker.update("BTCUSDT", 100.0)
        text = repr(tracker)
        assert "BTCUSDT" in text
        assert "window=10" in text


class TestCorrelationTrackerUpdate:
    def test_ignores_unknown_symbols_without_raising(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"])
        tracker.update("UNKNOWN", 100.0)  # must not raise
        assert tracker.returns("BTCUSDT").size == 0

    def test_known_symbol_price_is_recorded(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=10)
        tracker.update("BTCUSDT", 100.0)
        tracker.update("BTCUSDT", 110.0)
        returns = tracker.returns("BTCUSDT")
        assert returns.size == 1
        assert returns[0] == pytest.approx(math.log(110.0 / 100.0))


class TestCorrelationTrackerReturns:
    def test_fewer_than_two_prices_yields_empty_returns(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"])
        tracker.update("BTCUSDT", 100.0)
        assert tracker.returns("BTCUSDT").size == 0

    def test_returns_are_log_returns_of_consecutive_prices(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=10)
        for price in [100.0, 105.0, 103.0]:
            tracker.update("BTCUSDT", price)
        returns = tracker.returns("BTCUSDT")
        assert returns.size == 2
        assert returns[0] == pytest.approx(math.log(105.0 / 100.0))
        assert returns[1] == pytest.approx(math.log(103.0 / 105.0))

    def test_non_positive_prices_are_dropped_not_passed_to_log(self) -> None:
        # A zero or negative price must never reach np.log (which would
        # produce NaN/-inf and poison every downstream correlation).
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=10)
        for price in [100.0, 0.0, 105.0, 110.0]:
            tracker.update("BTCUSDT", price)
        returns = tracker.returns("BTCUSDT")
        assert not any(math.isnan(r) or math.isinf(r) for r in returns)

    def test_window_caps_the_number_of_stored_prices(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=3)
        for price in [100.0, 101.0, 102.0, 103.0, 104.0]:
            tracker.update("BTCUSDT", price)
        # window=3 -> maxlen=4 prices retained -> 3 returns max.
        assert tracker.returns("BTCUSDT").size == 3


class TestCorrelationTrackerMatrix:
    def test_self_correlation_is_always_1(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT"], window=10)
        tracker.update("BTCUSDT", 100.0)
        tracker.update("BTCUSDT", 101.0)
        matrix = tracker.matrix()
        assert matrix["BTCUSDT"]["BTCUSDT"] == 1.0

    def test_insufficient_data_produces_nan(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=10)
        tracker.update("BTCUSDT", 100.0)  # only one price -> zero returns
        tracker.update("ETHUSDT", 2000.0)
        matrix = tracker.matrix()
        assert math.isnan(matrix["BTCUSDT"]["ETHUSDT"])

    def test_perfectly_correlated_series_yields_correlation_near_1(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=20)
        prices = [100.0, 101.0, 102.5, 101.0, 103.0, 105.0]
        for p in prices:
            tracker.update("A", p)
            tracker.update("B", p * 2.0)  # identical relative moves, scaled
        matrix = tracker.matrix()
        assert matrix["A"]["B"] == pytest.approx(1.0, abs=1e-6)

    def test_matrix_is_symmetric(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=20)
        for a, b in [(100.0, 50.0), (102.0, 49.0), (101.0, 51.0), (105.0, 48.0)]:
            tracker.update("A", a)
            tracker.update("B", b)
        matrix = tracker.matrix()
        assert matrix["A"]["B"] == pytest.approx(matrix["B"]["A"])


class TestCorrelationTrackerHighestCorrelatedPair:
    def test_returns_none_with_insufficient_data(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=10)
        assert tracker.highest_correlated_pair() is None

    def test_identifies_the_pair_with_the_strongest_absolute_correlation(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B", "C"], window=20)
        # A and B move identically (scaled); C moves independently/inversely.
        a_prices = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
        c_prices = [50.0, 48.0, 49.5, 46.0, 47.0, 44.0]
        for a, c in zip(a_prices, c_prices):
            tracker.update("A", a)
            tracker.update("B", a * 3.0)
            tracker.update("C", c)
        result = tracker.highest_correlated_pair()
        assert result is not None
        sym_a, sym_b, corr = result
        assert {sym_a, sym_b} == {"A", "B"}
        assert corr == pytest.approx(1.0, abs=1e-6)


class TestCorrelationTrackerMatrixZeroVariance:
    def test_constant_price_series_yields_nan_without_raising_a_warning(self) -> None:
        # Regression test: a symbol reporting a constant price (e.g. a
        # stale or illiquid feed) has zero-variance returns, which would
        # otherwise cause np.corrcoef to divide by zero internally. This
        # project's pytest config treats RuntimeWarning as an error
        # (filterwarnings = ["error", ...]), so this test would fail the
        # suite outright if the guard added to matrix() regressed.
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=10)
        for _ in range(5):
            tracker.update("BTCUSDT", 100.0)  # constant -> zero-variance returns
            tracker.update("ETHUSDT", 50.0)

        matrix = tracker.matrix()  # must not raise/warn
        assert math.isnan(matrix["BTCUSDT"]["ETHUSDT"])
        assert math.isnan(matrix["ETHUSDT"]["BTCUSDT"])

    def test_one_constant_and_one_varying_series_yields_nan(self) -> None:
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=10)
        btc_prices = [100.0, 101.0, 99.0, 102.0, 98.0]
        for b in btc_prices:
            tracker.update("BTCUSDT", b)
            tracker.update("ETHUSDT", 50.0)  # constant

        matrix = tracker.matrix()  # must not raise/warn
        assert math.isnan(matrix["BTCUSDT"]["ETHUSDT"])


class TestCorrelationTrackerDiversificationRatio:
    def test_returns_nan_with_insufficient_data(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=10)
        assert math.isnan(tracker.diversification_ratio())

    def test_is_close_to_zero_for_perfectly_correlated_symbols(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=20)
        prices = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
        for p in prices:
            tracker.update("A", p)
            tracker.update("B", p * 2.0)
        # diversification_ratio = 1 - mean(|corr|) ≈ 1 - 1 = 0 for
        # perfectly correlated series.
        assert tracker.diversification_ratio() == pytest.approx(0.0, abs=1e-6)

    def test_is_in_the_valid_0_to_1_range_for_realistic_data(self) -> None:
        tracker = CorrelationTracker(symbols=["A", "B"], window=20)
        a_prices = [100.0, 102.0, 99.0, 105.0, 98.0, 110.0]
        b_prices = [50.0, 49.0, 51.0, 47.0, 52.0, 45.0]
        for a, b in zip(a_prices, b_prices):
            tracker.update("A", a)
            tracker.update("B", b)
        ratio = tracker.diversification_ratio()
        assert 0.0 <= ratio <= 1.0
