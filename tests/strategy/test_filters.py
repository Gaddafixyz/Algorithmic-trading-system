"""Unit tests for src.strategy.filters."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import src.strategy.filters as filters_module
from src.core.enums import SignalDirection, SignalStrength, Timeframe
from src.domain.signal import Signal
from src.strategy.filters import (
    apply_all_filters,
    filter_by_correlated_positions,
    filter_by_kill_zone,
    filter_by_minimum_atr,
    filter_by_spread,
    filter_by_weekend,
    filter_low_rr,
    filter_low_score,
)


def make_signal(
    entry: str = "50000", sl: str = "49000", tp: str = "52000", score: float = 70.0,
) -> Signal:
    return Signal(
        strategy_id="test", symbol="BTCUSDT", timeframe=Timeframe.H1,
        direction=SignalDirection.LONG, score=score, strength=SignalStrength.STRONG,
        confidence=0.7, entry_price=Decimal(entry), stop_loss=Decimal(sl),
        take_profit=Decimal(tp),
    )


class TestFilterBySpread:
    def test_true_when_spread_is_within_the_max(self) -> None:
        signal = make_signal()
        # spread = (100.1 - 100) / 100 * 100 = 0.1% < 0.05%? No: 0.1 > 0.05
        # use tighter spread to genuinely pass
        assert filter_by_spread(signal, best_bid=100.0, best_ask=100.02, max_spread_pct=0.05) is True

    def test_false_when_spread_exceeds_the_max(self) -> None:
        signal = make_signal()
        assert filter_by_spread(signal, best_bid=100.0, best_ask=100.5, max_spread_pct=0.05) is False

    def test_true_when_no_spread_data_is_available(self) -> None:
        signal = make_signal()
        assert filter_by_spread(signal, best_bid=0.0, best_ask=100.0) is True
        assert filter_by_spread(signal, best_bid=100.0, best_ask=0.0) is True


class TestFilterByMinimumAtr:
    def test_true_when_atr_pct_meets_the_minimum(self) -> None:
        signal = make_signal(entry="50000")
        # atr_pct = 100/50000*100 = 0.2% >= 0.1%
        assert filter_by_minimum_atr(signal, atr=100.0, min_atr_pct=0.1) is True

    def test_false_when_atr_pct_is_below_the_minimum(self) -> None:
        signal = make_signal(entry="50000")
        # atr_pct = 10/50000*100 = 0.02% < 0.1%
        assert filter_by_minimum_atr(signal, atr=10.0, min_atr_pct=0.1) is False

    def test_true_when_atr_is_zero_no_data(self) -> None:
        signal = make_signal()
        assert filter_by_minimum_atr(signal, atr=0.0) is True


class TestFilterByWeekend:
    def test_true_on_a_weekday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 3, tzinfo=UTC)  # Wednesday

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_weekend() is True

    def test_false_on_a_saturday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 6, tzinfo=UTC)  # Saturday

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_weekend() is False

    def test_false_on_a_sunday(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 7, tzinfo=UTC)  # Sunday

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_weekend() is False


class TestFilterByKillZone:
    """
    Regression coverage for filter_by_kill_zone wiring: killzones.py's
    should_trade_now() was fully implemented and unit-tested
    (tests/strategy/test_killzones.py) but had no caller anywhere in the
    strategy pipeline before this wiring.
    """

    def test_true_during_an_active_kill_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 2, 14, 0, tzinfo=UTC)  # NY Open KZ

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_kill_zone() is True

    def test_false_outside_any_kill_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 2, 5, 30, tzinfo=UTC)  # off-hours

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_kill_zone() is False

    def test_relaxed_session_score_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 2, 18, 0, tzinfo=UTC)  # NY Lunch KZ

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        assert filter_by_kill_zone(require_kill_zone=False) is True


class TestFilterLowScore:
    def test_true_when_score_meets_the_minimum(self) -> None:
        signal = make_signal(score=60.0)
        assert filter_low_score(signal, min_score=60.0) is True

    def test_false_when_score_is_below_the_minimum(self) -> None:
        signal = make_signal(score=59.0)
        assert filter_low_score(signal, min_score=60.0) is False


class TestFilterLowRr:
    def test_true_when_rr_meets_the_minimum(self) -> None:
        # risk = 1000, reward = 2000 -> RR = 2.0 >= 1.5
        signal = make_signal(entry="50000", sl="49000", tp="52000")
        assert filter_low_rr(signal, min_rr=1.5) is True

    def test_false_when_rr_is_below_the_minimum(self) -> None:
        # risk = 1000, reward = 1000 -> RR = 1.0 < 1.5
        signal = make_signal(entry="50000", sl="49000", tp="51000")
        assert filter_low_rr(signal, min_rr=1.5) is False


class TestApplyAllFilters:
    def test_passes_with_no_rejection_reasons_when_all_pass(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal, min_score=60.0, min_rr=1.5)
        assert passed is True
        assert reasons == []

    def test_collects_multiple_rejection_reasons(self) -> None:
        signal = make_signal(score=40.0, entry="50000", sl="49000", tp="50500")
        passed, reasons = apply_all_filters(signal, min_score=60.0, min_rr=1.5)
        assert passed is False
        assert len(reasons) == 2
        assert any("score" in r for r in reasons)
        assert any("RR" in r for r in reasons)

    def test_atr_filter_only_applied_when_atr_is_positive(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal, atr=0.0)
        assert "ATR too low" not in reasons

    def test_low_atr_adds_a_rejection_reason(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal, atr=1.0)  # tiny ATR
        assert "ATR too low" in reasons
        assert passed is False

    def test_spread_check_skipped_when_bid_ask_not_supplied(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal)
        assert passed is True
        assert reasons == []

    def test_wide_spread_adds_a_rejection_reason_when_bid_ask_supplied(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(
            signal, best_bid=100.0, best_ask=100.5, max_spread_pct=0.05
        )
        assert passed is False
        assert any("spread" in r for r in reasons)

    def test_weekend_check_skipped_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 6, tzinfo=UTC)  # Saturday

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal)
        assert passed is True

    def test_weekend_check_applied_when_requested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return datetime(2024, 1, 6, tzinfo=UTC)  # Saturday

        monkeypatch.setattr(filters_module, "datetime", _FrozenDatetime)
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal, check_weekend=True)
        assert passed is False
        assert "weekend low-liquidity period" in reasons

    def test_correlated_positions_check_skipped_when_open_symbols_not_supplied(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(signal)
        assert passed is True

    def test_correlated_positions_check_applied_when_open_symbols_supplied(self) -> None:
        signal = make_signal(score=70.0, entry="50000", sl="49000", tp="52000")
        passed, reasons = apply_all_filters(
            signal, open_symbols=["BTCUSDT", "BTCUSDT"], max_correlated_positions=2,
        )
        assert passed is False
        assert any("correlated" in r for r in reasons)


class TestFilterByCorrelatedPositions:
    def test_true_when_below_the_max(self) -> None:
        signal = make_signal()
        assert filter_by_correlated_positions(signal, ["ETHUSDT"], max_correlated_positions=2) is True

    def test_false_when_at_or_above_the_max_for_exact_symbol(self) -> None:
        signal = make_signal()  # BTCUSDT
        assert filter_by_correlated_positions(
            signal, ["BTCUSDT", "BTCUSDT"], max_correlated_positions=2
        ) is False

    def test_uses_correlation_group_when_supplied(self) -> None:
        signal = make_signal()  # BTCUSDT
        groups = {"BTCUSDT": ["BTCUSDT", "ETHUSDT"]}
        assert filter_by_correlated_positions(
            signal, ["ETHUSDT"], correlation_groups=groups, max_correlated_positions=1
        ) is False

    def test_symbols_outside_the_group_do_not_count(self) -> None:
        signal = make_signal()  # BTCUSDT
        groups = {"BTCUSDT": ["BTCUSDT", "ETHUSDT"]}
        assert filter_by_correlated_positions(
            signal, ["SOLUSDT", "SOLUSDT"], correlation_groups=groups, max_correlated_positions=1
        ) is True
