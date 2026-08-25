"""
Targeted unit tests for src/main.py.

Scope note: src/main.py (SMCStrategy, build_strategy, app wiring helpers)
otherwise has no dedicated test module yet — that's a separate, pre-
existing coverage gap tracked in CHANGELOG.md, not something this file
attempts to close in full. These tests cover only the confluence-weights
config wiring added alongside ROADMAP.md's "empirically validate and
tune the default SMC confluence scoring weights" item.
"""

from __future__ import annotations

from decimal import Decimal

from src.core.enums import Timeframe
from src.main import SMCStrategy, _load_confluence_weights, _load_smc_indicator_config
from src.smart_money.confluence import ConfluenceWeights


class TestLoadConfluenceWeights:
    """_load_confluence_weights() — reads strategy.yaml's `scoring_weights`
    block, previously present in the config file but never read by any
    code path."""

    def test_empty_config_returns_none(self) -> None:
        assert _load_confluence_weights({}) is None

    def test_none_config_returns_none(self) -> None:
        assert _load_confluence_weights(None) is None

    def test_full_override_builds_matching_weights(self) -> None:
        cfg = {
            "market_structure": 0.25,
            "order_block": 0.20,
            "fair_value_gap": 0.15,
            "liquidity_sweep": 0.15,
            "premium_discount": 0.10,
            "session": 0.10,
            "volume": 0.03,
            "htf_alignment": 0.02,
        }
        weights = _load_confluence_weights(cfg)
        assert weights is not None
        assert weights.market_structure == 0.25
        assert weights.volume == 0.03

    def test_partial_override_keeps_defaults_for_missing_keys(self) -> None:
        weights = _load_confluence_weights({"market_structure": 30.0})
        assert weights is not None
        defaults = ConfluenceWeights()
        assert weights.market_structure == 30.0
        assert weights.order_block == defaults.order_block
        assert weights.volume == defaults.volume

    def test_invalid_negative_value_fails_open_to_none(self) -> None:
        # A malformed config shouldn't crash startup -- falls back to
        # score_confluence()'s own default, same "fail open" pattern as
        # _build_signal_filter().
        weights = _load_confluence_weights({"market_structure": -5.0})
        assert weights is None

    def test_fractional_config_values_still_normalize_correctly(self) -> None:
        # strategy.yaml's scoring_weights block already ships with
        # fractions summing to 1.0 (0.20, 0.20, 0.15, ...) rather than
        # the point-budget scale (20.0, 20.0, 15.0, ...) ConfluenceWeights'
        # field defaults use. normalized() only cares about relative
        # proportions, so this should round-trip to the same effective
        # weights as the point-budget equivalent.
        fractional_cfg = {
            "market_structure": 0.20,
            "order_block": 0.20,
            "fair_value_gap": 0.15,
            "liquidity_sweep": 0.15,
            "premium_discount": 0.10,
            "session": 0.10,
            "volume": 0.05,
            "htf_alignment": 0.05,
        }
        fractional = _load_confluence_weights(fractional_cfg).normalized()
        point_budget = ConfluenceWeights().normalized()
        assert fractional.as_dict() == point_budget.as_dict()


class TestSMCStrategyConfluenceWeights:
    async def test_defaults_to_none(self) -> None:
        strategy = SMCStrategy(symbols=["BTCUSDT"], timeframes=[Timeframe.H1])
        assert strategy._confluence_weights is None

    async def test_custom_weights_reach_the_signal_engine(self) -> None:
        custom = ConfluenceWeights(market_structure=35.0)
        strategy = SMCStrategy(
            symbols=["BTCUSDT"],
            timeframes=[Timeframe.H1],
            confluence_weights=custom,
        )
        await strategy.initialize()
        engine = strategy._engines["BTCUSDT_1h"]
        assert engine.confluence_weights is custom


class TestLoadSmcIndicatorConfig:
    """
    _load_smc_indicator_config() — reads strategy.yaml's `smc` and
    `indicators` blocks, previously present in the config file but never
    read by any code path: `swing_lookback`, `ob_max_age_bars`,
    `fvg_min_size_atr_mult`, `premium_zone`, `discount_zone` (all under
    `smc`) and `atr_period`, `rsi_period`, `ema_fast`, `ema_slow` (all
    under `indicators`) were silently hardcoded module constants
    regardless of what was written here.
    """

    def test_empty_config_returns_empty_dict(self) -> None:
        assert _load_smc_indicator_config({}) == {}

    def test_missing_blocks_return_empty_dict(self) -> None:
        assert _load_smc_indicator_config({"symbols": ["BTCUSDT"]}) == {}

    def test_full_smc_block_parsed(self) -> None:
        cfg = {
            "smc": {
                "swing_lookback": 8,
                "ob_max_age_bars": 150,
                "fvg_min_size_atr_mult": 0.5,
                "premium_zone": 0.70,
                "discount_zone": 0.30,
            },
        }
        result = _load_smc_indicator_config(cfg)
        assert result["swing_lookback"] == 8
        assert result["ob_max_age_bars"] == 150
        assert result["fvg_min_size_atr_mult"] == 0.5
        assert result["premium_zone"] == Decimal("0.70")
        assert result["discount_zone"] == Decimal("0.30")

    def test_full_indicators_block_parsed(self) -> None:
        cfg = {
            "indicators": {
                "atr_period": 21,
                "rsi_period": 21,
                "ema_fast": 9,
                "ema_slow": 21,
            },
        }
        result = _load_smc_indicator_config(cfg)
        assert result["atr_period"] == 21
        assert result["rsi_period"] == 21
        assert result["ema_fast"] == 9
        assert result["ema_slow"] == 21

    def test_actual_shipped_strategy_yaml_values(self) -> None:
        # configs/strategy.yaml ships premium_zone=0.70/discount_zone=0.30
        # — these previously diverged from the hardcoded module constants
        # (0.618/0.382) with no way for the file's own values to ever
        # take effect. This locks in that the shipped file now parses to
        # exactly what's written in it.
        cfg = {
            "smc": {
                "swing_lookback": 5,
                "ob_max_age_bars": 100,
                "fvg_min_size_atr_mult": 0.3,
                "premium_zone": 0.70,
                "discount_zone": 0.30,
            },
            "indicators": {"atr_period": 14, "rsi_period": 14, "ema_fast": 12, "ema_slow": 26},
        }
        result = _load_smc_indicator_config(cfg)
        assert result == {
            "swing_lookback": 5,
            "ob_max_age_bars": 100,
            "fvg_min_size_atr_mult": 0.3,
            "premium_zone": Decimal("0.70"),
            "discount_zone": Decimal("0.30"),
            "atr_period": 14,
            "rsi_period": 14,
            "ema_fast": 12,
            "ema_slow": 26,
        }

    def test_malformed_int_value_dropped_not_raised(self) -> None:
        # Fails open (drops the one bad key) rather than crashing startup,
        # same convention as _load_confluence_weights()/_build_signal_filter().
        result = _load_smc_indicator_config({"smc": {"swing_lookback": "not_a_number"}})
        assert "swing_lookback" not in result

    def test_malformed_float_value_dropped_not_raised(self) -> None:
        result = _load_smc_indicator_config({"smc": {"fvg_min_size_atr_mult": "oops"}})
        assert "fvg_min_size_atr_mult" not in result

    def test_malformed_decimal_value_dropped_not_raised(self) -> None:
        result = _load_smc_indicator_config({"smc": {"premium_zone": "not_a_decimal"}})
        assert "premium_zone" not in result

    def test_premium_zone_not_greater_than_discount_zone_drops_both(self) -> None:
        # Nonsensical config (premium <= discount) fails open to defaults
        # for both fields rather than producing a zone classification
        # that's premium and discount simultaneously (or neither).
        result = _load_smc_indicator_config({"smc": {"premium_zone": 0.30, "discount_zone": 0.70}})
        assert "premium_zone" not in result
        assert "discount_zone" not in result

    def test_premium_zone_equal_to_discount_zone_drops_both(self) -> None:
        result = _load_smc_indicator_config({"smc": {"premium_zone": 0.50, "discount_zone": 0.50}})
        assert "premium_zone" not in result
        assert "discount_zone" not in result

    def test_only_premium_zone_present_is_kept(self) -> None:
        # No conflicting discount_zone to compare against, so the lone
        # valid value passes through.
        result = _load_smc_indicator_config({"smc": {"premium_zone": 0.70}})
        assert result["premium_zone"] == Decimal("0.70")

    def test_partial_smc_block_only_returns_present_keys(self) -> None:
        result = _load_smc_indicator_config({"smc": {"swing_lookback": 8}})
        assert result == {"swing_lookback": 8}


class TestSMCStrategySmcIndicatorConfigPassthrough:
    async def test_defaults_to_none(self) -> None:
        strategy = SMCStrategy(symbols=["BTCUSDT"], timeframes=[Timeframe.H1])
        assert strategy._ob_max_age_bars is None
        assert strategy._fvg_min_size_atr_mult is None
        assert strategy._premium_zone is None
        assert strategy._discount_zone is None
        assert strategy._rsi_period is None
        assert strategy._ema_fast is None
        assert strategy._ema_slow is None
        assert strategy._swing_lookback is None
        assert strategy._atr_period is None

    async def test_custom_values_reach_the_signal_engine(self) -> None:
        strategy = SMCStrategy(
            symbols=["BTCUSDT"],
            timeframes=[Timeframe.H1],
            ob_max_age_bars=50,
            fvg_min_size_atr_mult=0.5,
            premium_zone=Decimal("0.70"),
            discount_zone=Decimal("0.30"),
            rsi_period=21,
            ema_fast=9,
            ema_slow=21,
        )
        await strategy.initialize()
        engine = strategy._engines["BTCUSDT_1h"]
        assert engine.ob_max_age_bars == 50
        assert engine.fvg_min_size_atr_mult == 0.5
        assert engine.premium_zone == Decimal("0.70")
        assert engine.discount_zone == Decimal("0.30")
        assert engine.rsi_period == 21
        assert engine.ema_fast == 9
        assert engine.ema_slow == 21

    async def test_swing_lookback_and_atr_period_reach_the_signal_engine(self) -> None:
        # These two aren't Optional on SMCSignalEngine (real int defaults,
        # not None-means-default), so SMCStrategy.initialize() only
        # overrides them when a config value was actually supplied — this
        # confirms that override path, not just the None-passthrough path
        # the other fields use.
        strategy = SMCStrategy(
            symbols=["BTCUSDT"],
            timeframes=[Timeframe.H1],
            swing_lookback=8,
            atr_period=21,
        )
        await strategy.initialize()
        engine = strategy._engines["BTCUSDT_1h"]
        assert engine.swing_lookback == 8
        assert engine.atr_period == 21

    async def test_omitted_swing_lookback_and_atr_period_keep_engine_defaults(self) -> None:
        strategy = SMCStrategy(symbols=["BTCUSDT"], timeframes=[Timeframe.H1])
        await strategy.initialize()
        engine = strategy._engines["BTCUSDT_1h"]
        # SMCSignalEngine's own hardcoded defaults, unchanged.
        assert engine.swing_lookback == 5
        assert engine.atr_period == 14
