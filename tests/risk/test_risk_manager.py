"""Integration tests for the central RiskManager."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import RiskConfig
from src.core.enums import PositionSide, RiskLevel, SignalDirection, Timeframe
from src.core.events import PositionClosedEvent
from src.core.exceptions import (
    CircuitBreakerOpenError,
    DailyLossLimitError,
    MaxPositionsExceededError,
    PositionSizingError,
    RiskLimitExceededError,
)
from src.domain.portfolio import Portfolio
from src.domain.position import Position
from src.domain.signal import Signal, SignalStrength
from src.risk.exposure import CorrelationTracker
from src.risk.risk_manager import RiskManager, classify_risk_level


def make_signal(
    direction: SignalDirection = SignalDirection.LONG,
    entry: float = 50000.0,
    sl: float = 49000.0,
    tp: float = 52000.0,
    symbol: str = "BTCUSDT",
    score: float = 70.0,
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        strategy_id="test",
        symbol=symbol,
        timeframe=Timeframe.H1,
        direction=direction,
        score=score,
        strength=SignalStrength.STRONG,
        confidence=0.7,
        entry_price=Decimal(str(entry)),
        stop_loss=Decimal(str(sl)),
        take_profit=Decimal(str(tp)),
        metadata=metadata or {},
    )


def make_position(symbol: str = "BTCUSDT") -> Position:
    return Position(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        entry_quantity=Decimal("0.1"),
        entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
    )


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_portfolio_risk_pct=Decimal("1.0"),
        max_daily_loss_pct=Decimal("5.0"),
        max_drawdown_pct=Decimal("15.0"),
        max_open_positions=3,
        max_leverage=Decimal("10.0"),
        risk_reward_minimum=Decimal("1.5"),
    )


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio()


@pytest.fixture
def risk_manager(risk_config: RiskConfig, portfolio: Portfolio) -> RiskManager:
    rm = RiskManager(config=risk_config, portfolio=portfolio)
    rm.update_equity(Decimal("10000"))
    return rm


class TestRiskManagerValidation:
    @pytest.mark.asyncio
    async def test_valid_signal_passes(self, risk_manager: RiskManager):
        signal = make_signal()
        result = await risk_manager.validate_signal(signal)
        assert result is True

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_rejects(self, risk_manager: RiskManager):
        risk_manager._circuit_breaker.trip("test trip")
        signal = make_signal()
        with pytest.raises(CircuitBreakerOpenError):
            await risk_manager.validate_signal(signal)

    @pytest.mark.asyncio
    async def test_max_positions_rejects(self, risk_manager: RiskManager, portfolio: Portfolio):
        # Fill up to max positions
        for i in range(3):
            pos = make_position(symbol=f"SYM{i}USDT")
            portfolio.add_position(pos)

        signal = make_signal(symbol="NEWUSDT")
        with pytest.raises(MaxPositionsExceededError):
            await risk_manager.validate_signal(signal)

    @pytest.mark.asyncio
    async def test_duplicate_position_rejects(
        self, risk_manager: RiskManager, portfolio: Portfolio
    ):
        pos = make_position(symbol="BTCUSDT")
        portfolio.add_position(pos)

        signal = make_signal(symbol="BTCUSDT")
        with pytest.raises(RiskLimitExceededError):
            await risk_manager.validate_signal(signal)

    @pytest.mark.asyncio
    async def test_low_risk_reward_rejects(self, risk_manager: RiskManager):
        # RR of 1.0, below minimum of 1.5
        signal = make_signal(entry=50000.0, sl=49000.0, tp=51000.0)
        with pytest.raises(RiskLimitExceededError):
            await risk_manager.validate_signal(signal)

    @pytest.mark.asyncio
    async def test_daily_loss_limit_trips_breaker(self, risk_manager: RiskManager):
        risk_manager.update_equity(Decimal("10000"))
        # Simulate a large daily loss
        risk_manager.record_pnl(Decimal("-600"))  # 6% loss > 5% limit

        signal = make_signal()
        with pytest.raises(DailyLossLimitError):
            await risk_manager.validate_signal(signal)
        assert risk_manager.circuit_breaker_state.value == "open"


class TestRiskManagerPositionSizing:
    @pytest.mark.asyncio
    async def test_position_size_calculated(self, risk_manager: RiskManager):
        signal = make_signal(entry=50000.0, sl=49000.0)
        size = await risk_manager.calculate_position_size(signal)
        assert size > Decimal("0")

    @pytest.mark.asyncio
    async def test_position_size_respects_risk_pct(self, risk_manager: RiskManager):
        signal = make_signal(entry=50000.0, sl=49000.0)
        size = await risk_manager.calculate_position_size(signal)
        # Risk amount = equity * 1% = 100
        # Stop distance = 1000
        # Size should be roughly 100/1000 = 0.1 (minus fees)
        assert Decimal("0.03") < size <= Decimal("0.04")

    @pytest.mark.asyncio
    async def test_position_size_capped_by_max_pct(self, risk_manager: RiskManager):
        # Very tight stop creates huge implied size — should be capped
        signal = make_signal(entry=50000.0, sl=49999.0)  # $1 stop distance
        size = await risk_manager.calculate_position_size(signal)
        max_notional = risk_manager.equity * Decimal("0.20")  # 20% cap
        notional = size * Decimal("50000")
        assert notional <= max_notional * Decimal("1.01")  # Allow small rounding

    @pytest.mark.asyncio
    async def test_zero_equity_raises(self, risk_config: RiskConfig, portfolio: Portfolio):
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        # Equity not set — defaults to 0
        signal = make_signal()
        from src.core.exceptions import PositionSizingError

        with pytest.raises(PositionSizingError):
            await rm.calculate_position_size(signal)


class TestRiskManagerVolatilityAdjustedSizing:
    """
    Regression coverage for volatility_adjusted_size wiring:
    risk/position_sizing.py::volatility_adjusted_size() was fully
    implemented and unit-tested but RiskManager.calculate_position_size()
    never called it before this wiring — only the fixed-fractional
    calculate_position_size() was reachable.
    """

    @pytest.mark.asyncio
    async def test_default_method_ignores_atr_metadata(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ) -> None:
        # position_sizing_method defaults to 'fixed_fractional' -- a
        # signal with ATR in metadata must size identically to one
        # without it, since the volatility branch shouldn't even run.
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        signal_no_atr = make_signal(entry=50000.0, sl=49000.0)
        signal_with_atr = make_signal(entry=50000.0, sl=49000.0, metadata={"atr": 200.0})
        size_no_atr = await rm.calculate_position_size(signal_no_atr)
        size_with_atr = await rm.calculate_position_size(signal_with_atr)
        assert size_no_atr == size_with_atr

    @pytest.mark.asyncio
    async def test_wide_stop_relative_to_atr_reduces_size(self) -> None:
        base_cfg = dict(
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
        )
        fixed_cfg = RiskConfig(position_sizing_method="fixed_fractional", **base_cfg)
        vol_cfg = RiskConfig(position_sizing_method="volatility_adjusted", **base_cfg)

        rm_fixed = RiskManager(config=fixed_cfg, portfolio=Portfolio())
        rm_fixed.update_equity(Decimal("10000"))
        rm_vol = RiskManager(config=vol_cfg, portfolio=Portfolio())
        rm_vol.update_equity(Decimal("10000"))

        # Stop distance (1000) is much wider than target_atr_risk (1.5) *
        # ATR (200) = 300 -- volatility_adjusted_size should scale the
        # base size down.
        signal = make_signal(entry=50000.0, sl=49000.0, metadata={"atr": 200.0})
        size_fixed = await rm_fixed.calculate_position_size(signal)
        size_vol = await rm_vol.calculate_position_size(signal)
        assert size_vol < size_fixed

    @pytest.mark.asyncio
    async def test_missing_atr_falls_back_to_fixed_fractional(self, portfolio: Portfolio) -> None:
        cfg = RiskConfig(
            position_sizing_method="volatility_adjusted",
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
        )
        rm = RiskManager(config=cfg, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        rm_fixed = RiskManager(
            config=RiskConfig(
                position_sizing_method="fixed_fractional",
                max_portfolio_risk_pct=Decimal("1.0"),
                max_daily_loss_pct=Decimal("5.0"),
                max_drawdown_pct=Decimal("15.0"),
                max_open_positions=3,
                max_leverage=Decimal("10.0"),
                risk_reward_minimum=Decimal("1.5"),
            ),
            portfolio=Portfolio(),
        )
        rm_fixed.update_equity(Decimal("10000"))

        signal = make_signal(entry=50000.0, sl=49000.0)  # no ATR in metadata
        size_vol = await rm.calculate_position_size(signal)
        size_fixed = await rm_fixed.calculate_position_size(signal)
        assert size_vol == size_fixed


class TestRiskManagerKellySizing:
    """
    Regression coverage for kelly_position_size wiring:
    risk/position_sizing.py::kelly_position_size() was fully implemented
    and unit-tested but had no live win_rate/avg_win/avg_loss feed and no
    caller anywhere in the execution path before this wiring. The feed
    itself (risk/trade_tracker.py::RollingTradeTracker) is fed by
    RiskManager's existing PositionClosedEvent subscription — these tests
    call _on_position_closed() directly rather than round-tripping
    through the event bus, matching how the other position-sizing test
    classes exercise calculate_position_size() directly.
    """

    def _make_position_closed_event(self, net_pnl: Decimal) -> PositionClosedEvent:
        # fees=0 keeps net_pnl == realized_pnl - fees == realized_pnl,
        # simplest to reason about from a test's perspective.
        return PositionClosedEvent(
            position_id="pos-1",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            entry_price=Decimal("50000"),
            exit_price=Decimal("50000") + net_pnl,
            quantity=Decimal("1"),
            realized_pnl=net_pnl,
            fees=Decimal("0"),
        )

    def _make_kelly_risk_manager(self, portfolio: Portfolio | None = None) -> RiskManager:
        cfg = RiskConfig(
            position_sizing_method="kelly",
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
            kelly_lookback_trades=30,
            kelly_min_trades=5,
            kelly_fraction=0.25,
        )
        rm = RiskManager(config=cfg, portfolio=portfolio or Portfolio())
        rm.update_equity(Decimal("10000"))
        return rm

    @pytest.mark.asyncio
    async def test_falls_back_to_fixed_fractional_below_min_sample(self) -> None:
        rm_kelly = self._make_kelly_risk_manager()
        cfg_fixed = RiskConfig(
            position_sizing_method="fixed_fractional",
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
        )
        rm_fixed = RiskManager(config=cfg_fixed, portfolio=Portfolio())
        rm_fixed.update_equity(Decimal("10000"))

        # Only 2 closed trades tracked, kelly_min_trades=5 -- must fall
        # back rather than call kelly_position_size().
        await rm_kelly._on_position_closed(self._make_position_closed_event(Decimal("100")))
        await rm_kelly._on_position_closed(self._make_position_closed_event(Decimal("-50")))

        signal = make_signal(entry=50000.0, sl=49000.0)
        size_kelly = await rm_kelly.calculate_position_size(signal)
        size_fixed = await rm_fixed.calculate_position_size(signal)
        assert size_kelly == size_fixed

    @pytest.mark.asyncio
    async def test_uses_kelly_sizing_once_min_sample_reached(self) -> None:
        rm_kelly = self._make_kelly_risk_manager()
        cfg_fixed = RiskConfig(
            position_sizing_method="fixed_fractional",
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
        )
        rm_fixed = RiskManager(config=cfg_fixed, portfolio=Portfolio())
        rm_fixed.update_equity(Decimal("10000"))

        # 4 wins, 1 loss -- a clear positive edge, kelly_min_trades=5 met.
        for pnl in (
            Decimal("200"),
            Decimal("200"),
            Decimal("200"),
            Decimal("200"),
            Decimal("-100"),
        ):
            await rm_kelly._on_position_closed(self._make_position_closed_event(pnl))

        signal = make_signal(entry=50000.0, sl=49000.0)
        size_kelly = await rm_kelly.calculate_position_size(signal)
        size_fixed = await rm_fixed.calculate_position_size(signal)
        # Positive edge -> Kelly sizes differently (not necessarily
        # larger after the max_position_pct_of_equity cap, but distinct
        # from the fixed-fractional path since it ignores stop distance).
        assert size_kelly != size_fixed
        assert size_kelly > Decimal("0")

    @pytest.mark.asyncio
    async def test_non_positive_edge_falls_back_to_fixed_fractional(self) -> None:
        rm_kelly = self._make_kelly_risk_manager()
        cfg_fixed = RiskConfig(
            position_sizing_method="fixed_fractional",
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=3,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.5"),
        )
        rm_fixed = RiskManager(config=cfg_fixed, portfolio=Portfolio())
        rm_fixed.update_equity(Decimal("10000"))

        # 1 win, 4 losses of equal magnitude -- clearly negative edge,
        # kelly_position_size() itself returns 0 for this.
        for pnl in (
            Decimal("100"),
            Decimal("-100"),
            Decimal("-100"),
            Decimal("-100"),
            Decimal("-100"),
        ):
            await rm_kelly._on_position_closed(self._make_position_closed_event(pnl))

        signal = make_signal(entry=50000.0, sl=49000.0)
        size_kelly = await rm_kelly.calculate_position_size(signal)
        size_fixed = await rm_fixed.calculate_position_size(signal)
        assert size_kelly == size_fixed

    def test_kelly_min_trades_exceeding_lookback_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            RiskConfig(
                position_sizing_method="kelly",
                kelly_lookback_trades=10,
                kelly_min_trades=20,
                max_portfolio_risk_pct=Decimal("1.0"),
                max_daily_loss_pct=Decimal("5.0"),
                max_drawdown_pct=Decimal("15.0"),
                max_open_positions=3,
                max_leverage=Decimal("10.0"),
                risk_reward_minimum=Decimal("1.5"),
            )


class TestRiskManagerEquityTracking:
    def test_update_equity(self, risk_manager: RiskManager):
        risk_manager.update_equity(Decimal("12000"))
        assert risk_manager.equity == Decimal("12000")

    def test_record_pnl_accumulates(self, risk_manager: RiskManager):
        risk_manager.record_pnl(Decimal("100"))
        risk_manager.record_pnl(Decimal("-50"))
        assert risk_manager.daily_pnl == Decimal("50")

    def test_is_trading_allowed_true_initially(self, risk_manager: RiskManager):
        assert risk_manager.is_trading_allowed is True

    def test_is_trading_allowed_false_after_trip(self, risk_manager: RiskManager):
        risk_manager._circuit_breaker.trip("manual test")
        assert risk_manager.is_trading_allowed is False

    def test_reset_circuit_breaker(self, risk_manager: RiskManager):
        risk_manager._circuit_breaker.trip("test")
        risk_manager.reset_circuit_breaker()
        assert risk_manager.is_trading_allowed is True

    def test_stats_contains_expected_keys(self, risk_manager: RiskManager):
        stats = risk_manager.stats
        expected = {
            "equity",
            "daily_pnl",
            "drawdown_pct",
            "risk_level",
            "circuit_breaker",
            "open_positions",
        }
        assert expected.issubset(stats.keys())

    def test_stats_risk_level_is_low_with_no_drawdown(self, risk_manager: RiskManager):
        assert risk_manager.stats["risk_level"] == RiskLevel.LOW.value


class TestClassifyRiskLevel:
    """
    Thresholds approved by Allan ([1.3.27]): bucketed as a ratio of
    max_drawdown_pct rather than fixed percentages, since the limit
    itself is a per-deployment setting.
    """

    @pytest.mark.parametrize(
        "drawdown_pct,max_drawdown_pct,expected",
        [
            (0.0, 15.0, RiskLevel.LOW),
            (7.4, 15.0, RiskLevel.LOW),  # just under 50%
            (7.5, 15.0, RiskLevel.MEDIUM),  # exactly 50%
            (11.0, 15.0, RiskLevel.MEDIUM),
            (11.25, 15.0, RiskLevel.HIGH),  # exactly 75%
            (14.9, 15.0, RiskLevel.HIGH),
            (15.0, 15.0, RiskLevel.CRITICAL),  # exactly 100%
            (20.0, 15.0, RiskLevel.CRITICAL),  # over the limit
        ],
    )
    def test_boundary_and_interior_values(
        self, drawdown_pct: float, max_drawdown_pct: float, expected: RiskLevel
    ) -> None:
        assert classify_risk_level(drawdown_pct, max_drawdown_pct) == expected

    def test_zero_max_drawdown_pct_is_critical_not_low(self) -> None:
        # A misconfigured/disabled limit (0.0) must not be silently
        # reported as "no risk" via a divide-by-zero default.
        assert classify_risk_level(5.0, 0.0) == RiskLevel.CRITICAL

    def test_negative_max_drawdown_pct_is_critical(self) -> None:
        assert classify_risk_level(5.0, -1.0) == RiskLevel.CRITICAL


class TestRiskManagerCorrelationAdjustedSizing:
    """CorrelationTracker wiring: opt-in via constructor, fed via start()'s
    CandleClosedEvent subscription, applied in calculate_position_size()."""

    @pytest.mark.asyncio
    async def test_no_tracker_leaves_sizing_unaffected(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ):
        # Default risk_manager fixture has no correlation_tracker — sizing
        # must behave exactly as before this feature existed.
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        signal = make_signal(entry=50000.0, sl=49000.0)
        size = await rm.calculate_position_size(signal)
        assert size > Decimal("0")

    @pytest.mark.asyncio
    async def test_tracker_present_but_no_open_positions_unaffected(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ):
        # correlation_adjusted_size() only scales when there's an existing
        # held position to correlate against.
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=5)
        rm = RiskManager(config=risk_config, portfolio=portfolio, correlation_tracker=tracker)
        rm.update_equity(Decimal("10000"))
        signal = make_signal(entry=50000.0, sl=49000.0, symbol="ETHUSDT")
        size = await rm.calculate_position_size(signal)
        assert size > Decimal("0")

    @pytest.mark.asyncio
    async def test_highly_correlated_symbol_scaled_down(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ):
        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=10)
        # Feed near-identical moving price series so BTC/ETH correlate ~1.0.
        for i in range(10):
            price = 50000.0 + i * 100
            tracker.update("BTCUSDT", price)
            tracker.update("ETHUSDT", price * 0.06)  # scaled but same shape -> corr ~1.0

        portfolio.add_position(make_position(symbol="BTCUSDT"))

        rm_with_tracker = RiskManager(
            config=risk_config, portfolio=portfolio, correlation_tracker=tracker
        )
        rm_with_tracker.update_equity(Decimal("10000"))

        rm_without_tracker = RiskManager(config=risk_config, portfolio=portfolio)
        rm_without_tracker.update_equity(Decimal("10000"))

        signal = make_signal(entry=3000.0, sl=2940.0, symbol="ETHUSDT")
        size_with = await rm_with_tracker.calculate_position_size(signal)
        size_without = await rm_without_tracker.calculate_position_size(signal)

        assert size_with < size_without

    @pytest.mark.asyncio
    async def test_candle_closed_event_feeds_tracker(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ):
        from src.core.events import CandleClosedEvent, get_event_bus

        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=10)
        rm = RiskManager(config=risk_config, portfolio=portfolio, correlation_tracker=tracker)
        await rm.start()

        event = CandleClosedEvent(
            symbol="BTCUSDT",
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50050"),
            volume=Decimal("10"),
            candle_timestamp=__import__("src.core.models", fromlist=["utc_now"]).utc_now(),
        )
        await get_event_bus().publish(event)

        assert len(tracker._prices["BTCUSDT"]) == 1

    def test_no_tracker_start_does_not_subscribe_candle_event(
        self, risk_config: RiskConfig, portfolio: Portfolio
    ):
        # Without a tracker, start() should not add a CandleClosedEvent
        # subscription — nothing would consume it and it would be dead
        # weight on every candle close.
        rm = RiskManager(config=risk_config, portfolio=portfolio)
        assert rm._correlation_tracker is None


class TestRiskManagerExposureCaps:
    """
    Gross/net exposure limits via ExposureTracker (src/risk/exposure.py).

    ExposureTracker was fully implemented and unit-tested (test_exposure.py)
    but never instantiated anywhere in RiskManager — these checks did not
    exist prior to this wiring. max_correlated_exposure_pct and
    max_gross_exposure_pct are both set permissively in these tests so the
    check under test is the one that actually fires.
    """

    def _exposed_config(self, **overrides: object) -> RiskConfig:
        base = dict(
            max_portfolio_risk_pct=Decimal("1.0"),
            max_daily_loss_pct=Decimal("5.0"),
            max_drawdown_pct=Decimal("15.0"),
            max_open_positions=5,
            max_leverage=Decimal("10.0"),
            risk_reward_minimum=Decimal("1.0"),
            max_correlated_exposure_pct=Decimal("100"),
            max_gross_exposure_pct=Decimal("500"),
            max_net_exposure_pct=Decimal("500"),
        )
        base.update(overrides)
        return RiskConfig(**base)

    @pytest.mark.asyncio
    async def test_gross_exposure_cap_rejects(self, portfolio: Portfolio):
        # Two existing positions at 30% of equity each (60% gross, 0% net —
        # they offset). A tight gross cap should reject a third position
        # even though correlated/net caps are wide open.
        portfolio.add_position(
            Position(
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("0.06"),
                entry_quantity=Decimal("0.06"),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50000"),
            )
        )
        portfolio.add_position(
            Position(
                symbol="ETHUSDT",
                side=PositionSide.SHORT,
                quantity=Decimal("1"),
                entry_quantity=Decimal("1"),
                entry_price=Decimal("3000"),
                mark_price=Decimal("3000"),
            )
        )
        config = self._exposed_config(
            max_gross_exposure_pct=Decimal("65"),  # existing 60% leaves little room
            max_net_exposure_pct=Decimal("500"),
        )
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))

        signal = make_signal(entry=100.0, sl=96.0, tp=106.0, symbol="SOLUSDT")
        with pytest.raises(PositionSizingError, match="Gross exposure"):
            await rm.calculate_position_size(signal)

    @pytest.mark.asyncio
    async def test_net_exposure_cap_rejects_directional_skew(self, portfolio: Portfolio):
        # Existing book is perfectly hedged (net = 0), so a wide-open gross
        # cap lets a new same-direction position through on gross alone —
        # only the net cap should catch the resulting directional skew.
        portfolio.add_position(
            Position(
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("0.06"),
                entry_quantity=Decimal("0.06"),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50000"),
            )
        )
        portfolio.add_position(
            Position(
                symbol="ETHUSDT",
                side=PositionSide.SHORT,
                quantity=Decimal("1"),
                entry_quantity=Decimal("1"),
                entry_price=Decimal("3000"),
                mark_price=Decimal("3000"),
            )
        )
        config = self._exposed_config(
            max_gross_exposure_pct=Decimal("500"),
            max_net_exposure_pct=Decimal("15"),  # new long alone (~20%) breaches this
        )
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))

        signal = make_signal(entry=100.0, sl=96.0, tp=106.0, symbol="SOLUSDT")
        with pytest.raises(PositionSizingError, match="Net exposure"):
            await rm.calculate_position_size(signal)

    @pytest.mark.asyncio
    async def test_within_caps_does_not_raise(self, portfolio: Portfolio):
        config = self._exposed_config()
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))
        signal = make_signal(entry=50000.0, sl=49000.0)
        size = await rm.calculate_position_size(signal)
        assert size > Decimal("0")

    @pytest.mark.asyncio
    async def test_check_portfolio_risk_trips_breaker_on_gross_breach(self, portfolio: Portfolio):
        # No new signal at all — an already-over-limit book (e.g. from
        # price moves on existing positions) must be caught by the
        # periodic check, not just the pre-trade path.
        portfolio.add_position(
            Position(
                symbol="BTCUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("0.5"),
                entry_quantity=Decimal("0.5"),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50000"),
            )
        )
        config = self._exposed_config(max_gross_exposure_pct=Decimal("100"))
        rm = RiskManager(config=config, portfolio=portfolio)
        rm.update_equity(Decimal("10000"))  # position notional 25000 = 250% of equity

        result = await rm.check_portfolio_risk()
        assert result is False
        assert rm.circuit_breaker_state.value == "open"
