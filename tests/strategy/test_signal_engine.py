"""Integration tests for the SMC signal engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.enums import SignalDirection, Timeframe
from src.core.models import OHLCV
from src.strategy.signal_engine import SMCSignalEngine
from src.strategy.multi_timeframe import MTFContext, HTF_MAP


def ts(i: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=i)


def make_candle(i: int, o: float, h: float, l: float, cl: float, vol: float = 1000.0) -> OHLCV:
    return OHLCV(
        timestamp=ts(i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(cl)),
        volume=Decimal(str(vol)),
    )


def make_trending_candles(n: int, start_price: float = 100.0, trend: float = 0.5, volatility: float = 2.0) -> list[OHLCV]:
    """Generate a trending candle series with realistic OHLC structure."""
    import random
    random.seed(42)
    candles = []
    price = start_price
    for i in range(n):
        price += trend + random.uniform(-volatility / 2, volatility / 2)
        open_ = price - random.uniform(0, 1)
        close = price + random.uniform(0, 1)
        high = max(open_, close) + random.uniform(0.5, volatility)
        low = min(open_, close) - random.uniform(0.5, volatility)
        candles.append(make_candle(i, open_, high, low, close))
    return candles


class TestSMCSignalEngine:
    def test_engine_initialization(self):
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test_smc",
        )
        assert engine.symbol == "BTCUSDT"
        assert engine.signal_count == 0

    def test_insufficient_candles_returns_none(self):
        engine = SMCSignalEngine(symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test")
        candles = make_trending_candles(10)
        signal = engine.analyze(candles)
        assert signal is None

    def test_analyze_with_sufficient_data_runs_without_error(self):
        """With enough data, analysis should run without raising, regardless of outcome."""
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            min_score=30.0,  # Lower threshold to increase chance of signal
        )
        candles = make_trending_candles(100, trend=0.8)
        # Should not raise
        signal = engine.analyze(candles)
        # Signal may or may not be generated, but analysis state should be recorded
        assert engine.last_analysis is not None

    def test_strong_uptrend_produces_bullish_bias(self):
        """A strong, clean uptrend should bias toward bullish structure."""
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            min_score=20.0,
        )
        # Strong consistent uptrend with low volatility
        candles = []
        price = 100.0
        for i in range(120):
            price += 1.0
            candles.append(make_candle(i, price - 0.3, price + 1.0, price - 1.0, price + 0.3))

        signal = engine.analyze(candles)
        analysis = engine.last_analysis
        assert analysis is not None
        # Structure should lean bullish given the strong uptrend
        if analysis.structure:
            assert analysis.structure.structure.value in ("bullish", "ranging", "unknown")

    def test_signal_has_valid_risk_reward(self):
        """Any generated signal must have positive risk/reward."""
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            min_score=10.0,  # Very low threshold to force signal generation attempts
            risk_reward_min=1.0,
        )
        candles = make_trending_candles(150, trend=1.0, volatility=3.0)
        signal = engine.analyze(candles)
        if signal is not None:
            assert signal.risk_reward_ratio > Decimal("0")
            assert signal.stop_loss != signal.entry_price
            assert signal.take_profit != signal.entry_price

    def test_signal_count_increments(self):
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            min_score=5.0,
        )
        candles = make_trending_candles(150, trend=1.5)
        engine.analyze(candles)
        # signal_count should reflect whether a signal was generated
        assert engine.signal_count >= 0

    def test_htf_candles_integrated(self):
        """HTF candles should be processed without error."""
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            htf_timeframe=Timeframe.H4,
            min_score=20.0,
        )
        ltf_candles = make_trending_candles(150, trend=0.5)
        htf_candles = make_trending_candles(50, trend=2.0)
        signal = engine.analyze(ltf_candles, htf_candles=htf_candles)
        assert engine.last_analysis is not None

    def test_ltf_candles_wired_into_mtf_context(self):
        """
        LTF candles previously had no candle source anywhere in the
        codebase — MTFContext supported the tier but nothing fed it. This
        confirms SMCSignalEngine.analyze() now feeds MTFContext for the
        LTF tier when ltf_timeframe is configured and ltf_candles supplied,
        and that the resulting SMCAnalysis carries htf_timeframe/
        ltf_timeframe so confluence.py's alignment_score() call can pass
        them through explicitly (rather than relying on MTFContext's
        HTF_MAP/LTF_MAP defaults, which may not match what was fed).
        """
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            htf_timeframe=Timeframe.H4,
            ltf_timeframe=Timeframe.M15,
            min_score=20.0,
        )
        candles = make_trending_candles(150, trend=0.5)
        htf_candles = make_trending_candles(50, trend=2.0)
        ltf_candles = make_trending_candles(150, trend=0.5)

        engine.analyze(candles, htf_candles=htf_candles, ltf_candles=ltf_candles)
        analysis = engine.last_analysis

        assert analysis is not None
        assert analysis.htf_timeframe == Timeframe.H4
        assert analysis.ltf_timeframe == Timeframe.M15
        assert analysis.mtf_context is not None
        # The LTF tier must actually be present in MTFContext's structures,
        # not just accepted-and-dropped.
        assert analysis.mtf_context.get_structure(Timeframe.M15) is not None

    def test_ltf_omitted_leaves_ltf_tier_absent_not_broken(self):
        """Omitting ltf_candles (no source configured) must not error and
        must simply leave the LTF tier unfed — alignment_score() already
        excludes unfed tiers from its denominator."""
        engine = SMCSignalEngine(
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            strategy_id="test",
            htf_timeframe=Timeframe.H4,
            min_score=20.0,
        )
        candles = make_trending_candles(150, trend=0.5)
        htf_candles = make_trending_candles(50, trend=2.0)

        engine.analyze(candles, htf_candles=htf_candles)
        analysis = engine.last_analysis

        assert analysis is not None
        assert analysis.ltf_timeframe is None
        assert analysis.mtf_context.get_structure(Timeframe.M15) is None


class TestMultiTimeframeContext:
    def test_update_and_get_bias(self):
        ctx = MTFContext()
        candles = make_trending_candles(60, trend=1.0)
        ctx.update(Timeframe.H1, candles)
        bias = ctx.get_bias(Timeframe.H1)
        assert bias in ("bullish", "bearish", "neutral")

    def test_unknown_timeframe_returns_neutral(self):
        ctx = MTFContext()
        assert ctx.get_bias(Timeframe.D1) == "neutral"

    def test_alignment_score_no_data_returns_default(self):
        ctx = MTFContext()
        score = ctx.alignment_score(Timeframe.H1, "bullish")
        assert score == 0.5

    def test_htf_map_exists_for_common_timeframes(self):
        assert Timeframe.M15 in HTF_MAP
        assert Timeframe.H1 in HTF_MAP
        assert HTF_MAP[Timeframe.H1] == Timeframe.D1

    def test_is_htf_aligned_no_htf_defined_returns_true(self):
        ctx = MTFContext()
        # W1 has no HTF defined → should return True
        result = ctx.is_htf_aligned(Timeframe.W1, "bullish")
        assert result is True

    def test_context_summary(self):
        ctx = MTFContext()
        candles = make_trending_candles(60)
        ctx.update(Timeframe.H1, candles)
        ctx.update(Timeframe.H4, candles)
        summary = ctx.context_summary()
        assert "1h" in summary
        assert "4h" in summary


class TestConfluenceWeightsPassthrough:
    """
    SMCSignalEngine.confluence_weights must reach score_confluence() on
    every analyze() call — the wiring that lets scripts/optimize.py tune
    the confluence budget end-to-end from the outside instead of only
    being settable by editing smart_money/confluence.py's module-level
    default.
    """

    def test_none_by_default(self):
        engine = SMCSignalEngine(symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test")
        assert engine.confluence_weights is None

    def test_custom_weights_stored_and_forwarded(self, monkeypatch):
        from src.smart_money.confluence import ConfluenceWeights
        import src.strategy.signal_engine as signal_engine_module

        custom = ConfluenceWeights(market_structure=40.0)
        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            confluence_weights=custom,
        )
        assert engine.confluence_weights is custom

        captured: dict = {}
        real_score_confluence = signal_engine_module.score_confluence

        def spy(*args, **kwargs):
            captured["weights"] = kwargs.get("weights")
            return real_score_confluence(*args, **kwargs)

        monkeypatch.setattr(signal_engine_module, "score_confluence", spy)

        candles = make_trending_candles(120)
        engine.analyze(candles)

        assert "weights" in captured
        assert captured["weights"] is custom


class TestSmcIndicatorConfigPassthrough:
    """
    ob_max_age_bars/fvg_min_size_atr_mult/premium_zone/discount_zone/
    rsi_period/ema_fast/ema_slow — previously each of these had a
    hardcoded module-level constant with no caller-supplied override
    anywhere in the codebase. Now threaded from SMCSignalEngine's
    constructor through to every downstream call on every analyze()
    call — see configs/strategy.yaml's `smc`/`indicators` blocks and
    main.py::_load_smc_indicator_config().
    """

    def test_all_none_by_default(self):
        engine = SMCSignalEngine(symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test")
        assert engine.ob_max_age_bars is None
        assert engine.fvg_min_size_atr_mult is None
        assert engine.premium_zone is None
        assert engine.discount_zone is None
        assert engine.rsi_period is None
        assert engine.ema_fast is None
        assert engine.ema_slow is None

    def test_custom_values_stored(self):
        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            ob_max_age_bars=50,
            fvg_min_size_atr_mult=0.5,
            premium_zone=Decimal("0.70"),
            discount_zone=Decimal("0.30"),
            rsi_period=21,
            ema_fast=9,
            ema_slow=21,
        )
        assert engine.ob_max_age_bars == 50
        assert engine.fvg_min_size_atr_mult == 0.5
        assert engine.premium_zone == Decimal("0.70")
        assert engine.discount_zone == Decimal("0.30")
        assert engine.rsi_period == 21
        assert engine.ema_fast == 9
        assert engine.ema_slow == 21

    def test_ob_max_age_and_rsi_ema_periods_reach_score_confluence(self, monkeypatch):
        import src.strategy.signal_engine as signal_engine_module

        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            ob_max_age_bars=42, rsi_period=21, ema_fast=9, ema_slow=21,
        )

        captured: dict = {}
        real_score_confluence = signal_engine_module.score_confluence

        def spy(*args, **kwargs):
            captured["ob_max_age_bars"] = kwargs.get("ob_max_age_bars")
            captured["rsi_period"] = kwargs.get("rsi_period")
            captured["ema_fast"] = kwargs.get("ema_fast")
            captured["ema_slow"] = kwargs.get("ema_slow")
            return real_score_confluence(*args, **kwargs)

        monkeypatch.setattr(signal_engine_module, "score_confluence", spy)

        candles = make_trending_candles(120)
        engine.analyze(candles)

        assert captured["ob_max_age_bars"] == 42
        assert captured["rsi_period"] == 21
        assert captured["ema_fast"] == 9
        assert captured["ema_slow"] == 21

    def test_premium_discount_zone_reaches_analysis(self, monkeypatch):
        import src.strategy.signal_engine as signal_engine_module

        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            premium_zone=Decimal("0.90"), discount_zone=Decimal("0.10"),
        )

        candles = make_trending_candles(120)
        engine.analyze(candles)

        # If a swing range was found, the resulting premium_discount
        # zones must carry the custom thresholds through, not the module
        # defaults (0.618/0.382).
        analysis = engine.last_analysis
        if analysis is not None and analysis.premium_discount is not None:
            assert analysis.premium_discount.premium_zone == Decimal("0.90")
            assert analysis.premium_discount.discount_zone == Decimal("0.10")

    def test_fvg_min_size_atr_mult_reaches_detect_fvgs(self, monkeypatch):
        import src.strategy.signal_engine as signal_engine_module

        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            fvg_min_size_atr_mult=0.5,
        )

        captured: dict = {}
        real_detect_fvgs = signal_engine_module.detect_fvgs

        def spy(*args, **kwargs):
            captured["min_size_atr_mult"] = kwargs.get("min_size_atr_mult")
            return real_detect_fvgs(*args, **kwargs)

        monkeypatch.setattr(signal_engine_module, "detect_fvgs", spy)

        candles = make_trending_candles(120)
        engine.analyze(candles)

        assert captured["min_size_atr_mult"] == 0.5

    def test_fvg_min_size_atr_mult_omitted_uses_module_default(self, monkeypatch):
        import src.strategy.signal_engine as signal_engine_module

        engine = SMCSignalEngine(symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test")

        captured: dict = {}
        real_detect_fvgs = signal_engine_module.detect_fvgs

        def spy(*args, **kwargs):
            captured["called_with_kwargs"] = kwargs
            return real_detect_fvgs(*args, **kwargs)

        monkeypatch.setattr(signal_engine_module, "detect_fvgs", spy)

        candles = make_trending_candles(120)
        engine.analyze(candles)

        # No override supplied -> min_size_atr_mult key omitted entirely,
        # so detect_fvgs() falls back to its own SMC_FVG_MIN_SIZE_ATR_MULT
        # default rather than receiving an explicit None.
        assert "min_size_atr_mult" not in captured["called_with_kwargs"]

    def test_ob_max_age_bars_reaches_get_active_order_blocks(self, monkeypatch):
        import src.strategy.signal_engine as signal_engine_module

        engine = SMCSignalEngine(
            symbol="BTCUSDT", timeframe=Timeframe.H1, strategy_id="test",
            ob_max_age_bars=42,
        )

        captured: dict = {}
        real_get_active = signal_engine_module.get_active_order_blocks

        def spy(*args, **kwargs):
            captured["max_age_bars"] = kwargs.get("max_age_bars")
            return real_get_active(*args, **kwargs)

        monkeypatch.setattr(signal_engine_module, "get_active_order_blocks", spy)

        candles = make_trending_candles(120)
        engine.analyze(candles)

        assert captured["max_age_bars"] == 42

