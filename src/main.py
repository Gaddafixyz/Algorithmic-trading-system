"""
Trading System — Main Entry Point.

Usage:
    python -m src.main                    # Uses TRADING_ENV from environment
    TRADING_ENV=paper python -m src.main  # Paper trading mode
    TRADING_ENV=testnet python -m src.main
"""

from __future__ import annotations

import asyncio
import logging
import sys
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import uvloop

from src.bootstrap.app import create_application
from src.bootstrap.dependency_container import wire_container
from src.bootstrap.lifecycle import LifecycleManager
from src.core.config import get_settings
from src.core.enums import Timeframe
from src.core.models import OHLCV
from src.domain.signal import Signal
from src.smart_money.confluence import ConfluenceWeights
from src.strategy.base_strategy import BaseStrategy
from src.strategy.orchestrator import StrategyOrchestrator
from src.strategy.signal_engine import SMCSignalEngine
from src.utils.profiling import timer

if TYPE_CHECKING:
    from src.machine_learning.signal_filter import SignalFilter
    from src.market_data.candles import CandleService

logger = logging.getLogger(__name__)


class SMCStrategy(BaseStrategy):
    """
    Concrete SMC strategy implementation.

    Runs the SMCSignalEngine on every candle close and returns
    signals when confluence score meets the minimum threshold.
    """

    def __init__(
        self,
        symbols: list[str],
        timeframes: list[Timeframe],
        htf_timeframe: Timeframe | None = None,
        ltf_timeframe: Timeframe | None = None,
        min_score: float = 60.0,
        signal_filter: SignalFilter | None = None,
        candle_service: CandleService | None = None,
        confluence_weights: ConfluenceWeights | None = None,
        swing_lookback: int | None = None,
        atr_period: int | None = None,
        ob_max_age_bars: int | None = None,
        fvg_min_size_atr_mult: float | None = None,
        premium_zone: Decimal | None = None,
        discount_zone: Decimal | None = None,
        rsi_period: int | None = None,
        ema_fast: int | None = None,
        ema_slow: int | None = None,
        stop_loss_method: str = "atr",
        use_mitigation_blocks: bool = False,
    ) -> None:
        super().__init__(min_candles=100)
        self._symbols = [s.upper() for s in symbols]
        self._timeframes = timeframes
        self._htf_timeframe = htf_timeframe
        self._ltf_timeframe = ltf_timeframe
        self._min_score = min_score
        self._engines: dict[str, SMCSignalEngine] = {}
        self._signal_filter = signal_filter
        self._signals_ml_blocked = 0
        # Per-factor confluence scoring weights — see
        # smart_money/confluence.py::ConfluenceWeights. None defers to
        # score_confluence()'s own default (the original fixed 100pt
        # budget). Threaded through to every SMCSignalEngine built in
        # initialize() so scripts/optimize.py can tune these alongside
        # min_score without reaching past this class.
        self._confluence_weights = confluence_weights
        # configs/strategy.yaml's `smc`/`indicators` blocks, threaded
        # through to every SMCSignalEngine built in initialize() — see
        # SMCSignalEngine.__init__'s matching kwargs for what each of
        # these ultimately controls. None means "use that engine's own
        # hardcoded default", so a strategy built without these kwargs
        # (e.g. existing tests that construct SMCStrategy directly)
        # behaves identically to before this wiring existed.
        self._swing_lookback = swing_lookback
        self._atr_period = atr_period
        self._ob_max_age_bars = ob_max_age_bars
        self._fvg_min_size_atr_mult = fvg_min_size_atr_mult
        self._premium_zone = premium_zone
        self._discount_zone = discount_zone
        self._rsi_period = rsi_period
        self._ema_fast = ema_fast
        self._ema_slow = ema_slow
        # "atr" is the pre-existing default (unchanged behavior); only
        # affects the no-OB fallback stop, see SMCSignalEngine's matching
        # kwarg / confluence.py's _calculate_trade_params.
        self._stop_loss_method = stop_loss_method
        self._use_mitigation_blocks = use_mitigation_blocks
        # Used to pull higher-timeframe (and, as of this change,
        # lower-timeframe) candles for context on every analysis call —
        # see on_candle(). SMCSignalEngine.analyze() accepts htf_candles as
        # an optional argument and htf_timeframe was already threaded
        # through to the engine's constructor, but nothing ever actually
        # fetched or passed HTF candles, and the orchestrator only ever
        # subscribes/routes strategy.timeframes (the entry TF) — so
        # htf_structure silently stayed MarketStructure.UNKNOWN for every
        # signal regardless of htf_timeframe config. This reference lets
        # on_candle pull HTF/LTF candles directly instead of relying on
        # the orchestrator's per-candle-close routing (which is keyed by
        # trigger timeframe, not by every timeframe a strategy might need
        # as context). The LTF tier had the same gap one level further:
        # MTFContext (src/strategy/multi_timeframe.py) supported an LTF
        # tier from the start, but ltf_timeframe didn't even exist as a
        # concept anywhere in this class or SMCSignalEngine — see
        # set_ltf_candles() below and CHANGELOG [1.3.2]'s scope note.
        self._candle_service = candle_service
        # Explicit override for HTF context, set via set_htf_candles().
        # BacktestEngine uses this instead of candle_service (which is None
        # in backtests) so it can hand the strategy only the HTF candles
        # that have actually closed as of the current bar — preserving the
        # engine's no-look-ahead guarantee. _htf_override_active
        # distinguishes "override supplied but empty/insufficient" from
        # "no override set, fall back to candle_service".
        self._htf_override_active = False
        self._htf_override: list[OHLCV] | None = None
        # LTF tier for MTFContext — same pattern as the HTF override above.
        # Previously there was no candle source anywhere for this tier at
        # all (not just live vs. backtest — ltf_timeframe didn't exist as
        # a concept in this class), so MTFContext.alignment_score() could
        # never see LTF data regardless of what was configured.
        self._ltf_override_active = False
        self._ltf_override: list[OHLCV] | None = None

    def set_htf_candles(self, htf_candles: list[OHLCV] | None) -> None:
        """Explicit HTF-context override — see docstring on _htf_override."""
        self._htf_override_active = True
        self._htf_override = htf_candles

    def set_ltf_candles(self, ltf_candles: list[OHLCV] | None) -> None:
        """Explicit LTF-context override — see docstring on _ltf_override."""
        self._ltf_override_active = True
        self._ltf_override = ltf_candles

    @property
    def strategy_id(self) -> str:
        return "smc_v1"

    @property
    def symbols(self) -> list[str]:
        return self._symbols

    @property
    def timeframes(self) -> list[Timeframe]:
        return self._timeframes

    @property
    def htf_timeframe(self) -> Timeframe | None:
        return self._htf_timeframe

    @property
    def ltf_timeframe(self) -> Timeframe | None:
        return self._ltf_timeframe

    async def initialize(self) -> None:
        await super().initialize()
        for symbol in self._symbols:
            for tf in self._timeframes:
                key = f"{symbol}_{tf.value}"
                # swing_lookback/atr_period aren't Optional on
                # SMCSignalEngine (they have real int defaults, not a
                # None-means-"use-my-own-default" convention like the
                # rest of these kwargs), so only override when a config
                # value was actually supplied.
                engine_kwargs: dict[str, Any] = {}
                if self._swing_lookback is not None:
                    engine_kwargs["swing_lookback"] = self._swing_lookback
                if self._atr_period is not None:
                    engine_kwargs["atr_period"] = self._atr_period
                self._engines[key] = SMCSignalEngine(
                    symbol=symbol,
                    timeframe=tf,
                    strategy_id=self.strategy_id,
                    htf_timeframe=self._htf_timeframe,
                    ltf_timeframe=self._ltf_timeframe,
                    min_score=self._min_score,
                    confluence_weights=self._confluence_weights,
                    ob_max_age_bars=self._ob_max_age_bars,
                    fvg_min_size_atr_mult=self._fvg_min_size_atr_mult,
                    premium_zone=self._premium_zone,
                    discount_zone=self._discount_zone,
                    rsi_period=self._rsi_period,
                    ema_fast=self._ema_fast,
                    ema_slow=self._ema_slow,
                    stop_loss_method=self._stop_loss_method,
                    use_mitigation_blocks=self._use_mitigation_blocks,
                    **engine_kwargs,
                )

    async def on_candle(
        self,
        symbol: str,
        timeframe: Timeframe,
        candles: list[OHLCV],
    ) -> Signal | None:
        self.record_candle(symbol, timeframe)

        if not self.is_warmed_up(symbol, timeframe):
            return None

        if self.has_position(symbol):
            return None

        if not self.can_generate_signal(symbol):
            return None

        key = f"{symbol}_{timeframe.value}"
        engine = self._engines.get(key)
        if engine is None:
            return None

        htf_candles: list[OHLCV] | None = None
        if self._htf_override_active:
            htf_candles = self._htf_override
        elif self._htf_timeframe is not None and self._candle_service is not None:
            htf_candles = self._candle_service.get_candles(
                symbol,
                self._htf_timeframe,
                limit=500,
            )

        ltf_candles: list[OHLCV] | None = None
        if self._ltf_override_active:
            ltf_candles = self._ltf_override
        elif self._ltf_timeframe is not None and self._candle_service is not None:
            ltf_candles = self._candle_service.get_candles(
                symbol,
                self._ltf_timeframe,
                limit=500,
            )

        # candle_processing_latency (in orchestrator.py) times this whole
        # on_candle() call, including the two get_candles() cache reads
        # above. timer() isolates just the SMC analysis pipeline itself
        # (market structure -> BOS/CHoCH -> order blocks -> FVGs ->
        # liquidity -> confluence scoring) so an operator tailing logs sees
        # an immediate SLOW warning for *this specific* symbol/timeframe
        # call, without needing to query Prometheus to know which part of
        # on_candle() regressed. 100ms default threshold: analyze() runs
        # once per closed candle (minutes apart at minimum), so even a
        # generous per-call budget here is nowhere near the exchange's
        # rate limits or the strategy's own timing requirements.
        with timer(f"SMCSignalEngine.analyze[{key}]"):
            signal = engine.analyze(candles, htf_candles=htf_candles, ltf_candles=ltf_candles)

        if signal is not None and self._signal_filter is not None:
            if not self._signal_filter.should_trade(signal, engine.last_analysis):
                self._signals_ml_blocked += 1
                logger.info(
                    "ML filter blocked %s %s signal (score=%.1f)",
                    symbol,
                    signal.direction,
                    signal.score,
                )
                return None

        if signal is not None:
            self.register_signal(symbol, signal)

        return signal

    @property
    def ml_filter_stats(self) -> dict[str, object]:
        """ML gate diagnostics — empty when no filter is configured."""
        if self._signal_filter is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "blocked": self._signals_ml_blocked,
            "decisions": self._signal_filter.decisions,
            "filter_rate": self._signal_filter.filter_rate,
            "min_probability": self._signal_filter.min_probability,
        }


def build_strategy(settings: object, candle_service: CandleService | None = None) -> SMCStrategy:
    """Build the trading strategy from configuration."""
    from pathlib import Path

    import yaml

    config_path = Path("configs/strategy.yaml")
    if config_path.exists():
        with config_path.open() as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    symbols = cfg.get("symbols", ["BTCUSDT", "ETHUSDT"])
    tf_str = cfg.get("timeframe", "1h")
    htf_str = cfg.get("htf_timeframe", "4h")
    ltf_str = cfg.get("ltf_timeframe", "15m")
    min_score = float(cfg.get("min_confluence_score", 60.0))

    try:
        timeframe = Timeframe(tf_str)
    except ValueError:
        timeframe = Timeframe.H1

    try:
        htf_timeframe = Timeframe(htf_str)
    except ValueError:
        htf_timeframe = Timeframe.H4

    # ltf_timeframe is optional — an absent/invalid key disables the LTF
    # tier (MTFContext.alignment_score() already degrades gracefully to
    # scoring against whichever tiers *are* fed), rather than falling back
    # to a guessed default the way timeframe/htf_timeframe do, since an
    # unwanted LTF fetch has a real cost (an extra candle_service/REST
    # call on every analysis) that shouldn't be paid silently.
    ltf_timeframe: Timeframe | None
    try:
        ltf_timeframe = Timeframe(ltf_str) if ltf_str else None
    except ValueError:
        logger.warning("Invalid ltf_timeframe in strategy.yaml: %r — LTF tier disabled.", ltf_str)
        ltf_timeframe = None

    signal_filter = _build_signal_filter(cfg.get("ml_filter", {}))
    confluence_weights = _load_confluence_weights(cfg.get("scoring_weights", {}))
    smc_indicator_cfg = _load_smc_indicator_config(cfg)

    return SMCStrategy(
        symbols=symbols,
        timeframes=[timeframe],
        htf_timeframe=htf_timeframe,
        ltf_timeframe=ltf_timeframe,
        min_score=min_score,
        signal_filter=signal_filter,
        candle_service=candle_service,
        confluence_weights=confluence_weights,
        **smc_indicator_cfg,
    )


def _load_smc_indicator_config(cfg: dict) -> dict[str, Any]:
    """
    Read strategy.yaml's `smc` and `indicators` blocks into the kwargs
    SMCStrategy/SMCSignalEngine expect.

    Both blocks previously existed in the config file but had no reader
    anywhere in the codebase — `swing_lookback`, `ob_max_age_bars`,
    `fvg_min_size_atr_mult`, `premium_zone`, `discount_zone`,
    `rsi_period`, `ema_fast`, `ema_slow` were all silently hardcoded
    module constants regardless of what was written here. This is that
    wiring, mirroring _load_confluence_weights()'s fail-open pattern:
    an absent/malformed value falls back to that field's original
    hardcoded default (logged as a warning) rather than raising, so a
    missing/invalid config never crashes startup.

    Note `premium_zone`/`discount_zone` in particular previously
    diverged from strategy.yaml's own shipped values (SMC_PREMIUM_ZONE/
    SMC_DISCOUNT_ZONE were 0.618/0.382, but the file has shipped 0.70/
    0.30 since [1.3.x]) — after this wiring, the file's values are what
    actually takes effect, a real (and intended) behavior change.

    Returns only the keys that were successfully parsed; keys omitted
    here fall back to SMCStrategy's own per-field defaults (None ->
    that field's hardcoded constant), same convention as
    _load_confluence_weights() returning None wholesale on an empty
    block.
    """
    result: dict[str, Any] = {}

    smc_cfg = cfg.get("smc") or {}
    indicators_cfg = cfg.get("indicators") or {}

    def _int(block: dict, key: str) -> None:
        if key not in block:
            return
        try:
            result[key] = int(block[key])
        except (TypeError, ValueError):
            logger.warning("Invalid %s in strategy.yaml: %r — using default.", key, block[key])

    def _float(block: dict, key: str) -> None:
        if key not in block:
            return
        try:
            result[key] = float(block[key])
        except (TypeError, ValueError):
            logger.warning("Invalid %s in strategy.yaml: %r — using default.", key, block[key])

    def _decimal(block: dict, key: str) -> None:
        if key not in block:
            return
        try:
            result[key] = Decimal(str(block[key]))
        except (InvalidOperation, TypeError, ValueError):
            logger.warning("Invalid %s in strategy.yaml: %r — using default.", key, block[key])

    _int(smc_cfg, "swing_lookback")
    _int(smc_cfg, "ob_max_age_bars")
    _float(smc_cfg, "fvg_min_size_atr_mult")
    _decimal(smc_cfg, "premium_zone")
    _decimal(smc_cfg, "discount_zone")

    # Sanity check: a premium threshold at or below the discount threshold
    # would make every price simultaneously premium and discount (or
    # neither), which is nonsensical — fail open to defaults rather than
    # silently producing zone classifications no one intended.
    if "premium_zone" in result and "discount_zone" in result:
        if result["premium_zone"] <= result["discount_zone"]:
            logger.warning(
                "strategy.yaml smc.premium_zone (%s) <= smc.discount_zone (%s) "
                "— both must be in (0, 1) with premium > discount; using defaults for both.",
                result["premium_zone"],
                result["discount_zone"],
            )
            del result["premium_zone"]
            del result["discount_zone"]

    _int(indicators_cfg, "atr_period")
    _int(indicators_cfg, "rsi_period")
    _int(indicators_cfg, "ema_fast")
    _int(indicators_cfg, "ema_slow")

    # stop_loss_method: only affects the no-OB fallback stop in
    # confluence.py's _calculate_trade_params — "atr" (default) is the
    # pre-existing behavior. Validated here (rather than left to
    # SMCSignalEngine to silently no-op on garbage) since an invalid
    # value would otherwise just fall through the method's if/else and
    # behave as "atr" anyway, masking a config typo.
    stop_loss_method = smc_cfg.get("stop_loss_method", "atr")
    if stop_loss_method not in ("atr", "structure"):
        logger.warning(
            "Invalid smc.stop_loss_method in strategy.yaml: %r — using 'atr'.",
            stop_loss_method,
        )
        stop_loss_method = "atr"
    result["stop_loss_method"] = stop_loss_method

    # use_mitigation_blocks: order-block scoring fallback to a mitigated
    # OB's unfilled remainder when no active OB scores. False (default)
    # preserves the exact pre-existing order-block score for every
    # signal — see score_confluence()'s docstring for what True adds.
    use_mitigation_blocks = smc_cfg.get("use_mitigation_blocks", False)
    if not isinstance(use_mitigation_blocks, bool):
        logger.warning(
            "Invalid smc.use_mitigation_blocks in strategy.yaml: %r — using False.",
            use_mitigation_blocks,
        )
        use_mitigation_blocks = False
    result["use_mitigation_blocks"] = use_mitigation_blocks

    return result


def _load_confluence_weights(weights_cfg: dict) -> ConfluenceWeights | None:
    """
    Build a ConfluenceWeights override from strategy.yaml's `scoring_weights`
    block, if present.

    That block previously existed in the config file but had no reader
    anywhere in the codebase — `score_confluence()` used its own hardcoded
    literals regardless of what was written there, so editing it silently
    did nothing. This is that wiring.

    Only keys actually present in the config override
    ConfluenceWeights' defaults; missing keys keep the original 20/20/15/
    15/10/10/5/5 point budget for that factor. Returns None (→
    score_confluence()'s own default) when the block is empty/absent, so
    an unmodified config file behaves identically to before this wiring.

    ConfluenceWeights.normalized() (applied inside score_confluence())
    rescales whatever's returned here to sum to 100 regardless of the
    input scale, so the fractional values already in strategy.yaml
    (0.20, 0.15, ...) work as-is — no unit conversion needed between the
    config file's 0–1 fractions and the point-budget values
    ConfluenceWeights' field defaults happen to use.
    """
    if not weights_cfg:
        return None
    defaults = ConfluenceWeights()
    try:
        return ConfluenceWeights(
            market_structure=float(weights_cfg.get("market_structure", defaults.market_structure)),
            order_block=float(weights_cfg.get("order_block", defaults.order_block)),
            fair_value_gap=float(weights_cfg.get("fair_value_gap", defaults.fair_value_gap)),
            liquidity_sweep=float(weights_cfg.get("liquidity_sweep", defaults.liquidity_sweep)),
            premium_discount=float(weights_cfg.get("premium_discount", defaults.premium_discount)),
            session=float(weights_cfg.get("session", defaults.session)),
            volume=float(weights_cfg.get("volume", defaults.volume)),
            htf_alignment=float(weights_cfg.get("htf_alignment", defaults.htf_alignment)),
        )
    except ValueError:
        logger.warning(
            "Invalid scoring_weights in strategy.yaml (must be non-negative, "
            "not all zero) — falling back to default confluence weights.",
        )
        return None


def _build_signal_filter(ml_cfg: dict) -> SignalFilter | None:
    """
    Load the trained ML signal filter if enabled and its model file exists.

    Fails open (returns None, logs a warning) rather than raising, so a
    missing/corrupt model file degrades to "no ML gate" instead of
    crashing startup — consistent with SignalFilter.should_trade()'s own
    fail-open behavior on inference errors.
    """
    if not ml_cfg.get("enabled", False):
        return None

    from pathlib import Path

    model_path = Path(ml_cfg.get("model_path", "models/signal_filter.pkl"))
    if not model_path.exists():
        logger.warning(
            "ml_filter.enabled=true but model file not found at %s — "
            "running WITHOUT the ML gate. Train one with "
            "scripts/train_signal_filter.py.",
            model_path,
        )
        return None

    try:
        import joblib

        from src.machine_learning.signal_filter import SignalFilter

        model = joblib.load(model_path)
        sf = SignalFilter(
            model,
            min_probability=float(ml_cfg.get("min_probability", 0.55)),
            max_ob_age_bars=int(ml_cfg.get("max_ob_age_bars", 100)),
        )
        logger.info("ML signal filter loaded from %s: %r", model_path, sf)
        return sf
    except Exception as exc:
        logger.warning(
            "Failed to load ML signal filter from %s: %s — running WITHOUT it.", model_path, exc
        )
        return None


def _load_min_atr_pct() -> float:
    """
    Read strategy.yaml's min_atr_pct for StrategyOrchestrator's dead-market
    filter. Mirrors build_strategy()'s own direct yaml read (strategy.yaml
    isn't merged into Settings the way risk.yaml/monitoring.yaml are).
    Returns 0.0 (filter disabled, current default behavior) on any error
    or if the key/file is absent.
    """
    from pathlib import Path

    import yaml

    config_path = Path("configs/strategy.yaml")
    if not config_path.exists():
        return 0.0
    try:
        with config_path.open() as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to read configs/strategy.yaml for min_atr_pct: %s", exc)
        return 0.0
    try:
        return float(cfg.get("min_atr_pct", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _load_orchestrator_filter_config() -> dict[str, Any]:
    """
    Read strategy.yaml's opt-in RSI/MACD confirmation filter settings for
    StrategyOrchestrator (see strategy/filters.py::filter_by_rsi_exhaustion
    / filter_by_macd_trend). Same read pattern and same disabled-by-default
    fallback as _load_min_atr_pct() above — both indicators.{rsi,macd}
    modules existed, fully implemented and unit-tested, with no caller
    anywhere in the strategy pipeline until this wiring.
    """
    from pathlib import Path

    import yaml

    defaults: dict[str, Any] = {
        "rsi_exhaustion_filter": False,
        "rsi_overbought": 80.0,
        "rsi_oversold": 20.0,
        "macd_trend_filter": False,
        "max_spread_pct": 0.0,
        "kill_zone_filter": False,
        "require_kill_zone": True,
    }
    config_path = Path("configs/strategy.yaml")
    if not config_path.exists():
        return defaults
    try:
        with config_path.open() as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to read configs/strategy.yaml for filter config: %s", exc)
        return defaults
    try:
        return {
            "rsi_exhaustion_filter": bool(cfg.get("rsi_exhaustion_filter", False)),
            "rsi_overbought": float(cfg.get("rsi_overbought", 80.0)),
            "rsi_oversold": float(cfg.get("rsi_oversold", 20.0)),
            "macd_trend_filter": bool(cfg.get("macd_trend_filter", False)),
            # Execution-quality guard: reject signals when the live spread
            # (MarketDataCache.get_best_bid_ask, fed by the book-ticker
            # stream — see market_data/feeds.py::_make_book_ticker_handler)
            # exceeds this percentage. 0 (default) disables the check,
            # same as min_atr_pct/rsi/macd above.
            "max_spread_pct": float(cfg.get("max_spread_pct", 0.0)),
            # Session-timing gate: reject signals outside an ICT kill zone
            # (smart_money/killzones.py::should_trade_now). False (default)
            # disables the check, same opt-in pattern as the filters above.
            "kill_zone_filter": bool(cfg.get("kill_zone_filter", False)),
            "require_kill_zone": bool(cfg.get("require_kill_zone", True)),
        }
    except (TypeError, ValueError):
        return defaults


async def async_main() -> None:
    """Async main: build and run the application."""
    settings = get_settings()

    # Build application
    app = create_application(settings)
    wire_container(app)

    # Build strategy and orchestrator
    strategy = build_strategy(settings, candle_service=app.candle_service)
    filter_cfg = _load_orchestrator_filter_config()
    orchestrator = StrategyOrchestrator(
        app.candle_service,
        position_manager=app.position_manager,
        db=app.db,
        min_atr_pct=_load_min_atr_pct(),
        rsi_exhaustion_filter=filter_cfg["rsi_exhaustion_filter"],
        rsi_overbought=filter_cfg["rsi_overbought"],
        rsi_oversold=filter_cfg["rsi_oversold"],
        macd_trend_filter=filter_cfg["macd_trend_filter"],
        market_data_cache=app.cache,
        max_spread_pct=filter_cfg["max_spread_pct"],
        kill_zone_filter=filter_cfg["kill_zone_filter"],
        require_kill_zone=filter_cfg["require_kill_zone"],
    )
    orchestrator.register_strategy(strategy)

    # Exposes orchestrator.stats (signal/filter counters, per-strategy
    # candle-processing latency) via Application._dashboard_metrics ->
    # GET /metrics and the WS snapshot broadcast. Previously the
    # orchestrator was only known to LifecycleManager, so this data was
    # unreachable from the dashboard entirely.
    app.orchestrator = orchestrator

    # Subscribe market data feeds for all strategy symbols/timeframes.
    # strategy.timeframes is deliberately only the entry timeframe(s) — the
    # orchestrator uses it to decide which CandleClosedEvents should
    # trigger on_candle(). The HTF (and LTF) timeframe is not a trigger
    # timeframe (a closed HTF/LTF candle shouldn't itself fire a fresh
    # signal check), but its candles still need to be warmed up and kept
    # live in the cache so SMCStrategy.on_candle can pull them for
    # HTF-bias / LTF-tier context — otherwise htf_candles/ltf_candles is
    # always empty and htf_structure never leaves UNKNOWN (and the LTF
    # tier never appears in MTFContext) regardless of config. This is the
    # actual candle *source* for both tiers; the get_candles() calls in
    # on_candle() only work because this subscription keeps the cache fed.
    htf = getattr(strategy, "htf_timeframe", None)
    ltf = getattr(strategy, "ltf_timeframe", None)
    feed_timeframes = list(strategy.timeframes)
    if htf is not None and htf not in feed_timeframes:
        feed_timeframes.append(htf)
    if ltf is not None and ltf not in feed_timeframes:
        feed_timeframes.append(ltf)

    for symbol in strategy.symbols:
        await app.market_feed.subscribe(
            symbol=symbol,
            timeframes=feed_timeframes,
        )

    # Run lifecycle (blocks until shutdown signal)
    lifecycle = LifecycleManager(app, orchestrator)
    await lifecycle.run()


def main() -> None:
    """Entry point: install uvloop and run async main."""
    uvloop.install()
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
