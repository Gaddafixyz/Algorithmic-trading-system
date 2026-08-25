"""
SMC Signal Engine: Full Smart Money Concepts strategy implementation.

Orchestrates all SMC analysis modules to produce trade signals:
1. Multi-timeframe market structure analysis
2. Order block identification
3. Fair value gap detection
4. Liquidity sweep confirmation
5. Premium/discount zone filtering
6. Session/kill zone timing
7. Confluence scoring and signal generation
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.core.enums import MarketStructure, SignalDirection, Timeframe
from src.core.models import OHLCV
from src.domain.signal import Signal
from src.indicators.atr import calculate_atr
from src.indicators.volume import relative_volume
from src.smart_money.bos import detect_bos
from src.smart_money.choch import detect_choch
from src.smart_money.confluence import (
    ConfluenceWeights,
    SMCAnalysis,
    score_confluence,
)
from src.smart_money.fair_value_gap import detect_fvgs, get_open_fvgs, update_fvg_fills
from src.smart_money.inducement import detect_inducement
from src.smart_money.liquidity import (
    get_active_liquidity,
    identify_equal_highs,
    identify_equal_lows,
    identify_swing_liquidity,
    update_liquidity_sweeps,
)
from src.smart_money.market_structure import detect_market_structure
from src.smart_money.mitigation import extract_mitigation_blocks
from src.smart_money.order_blocks import (
    get_active_order_blocks,
    identify_order_blocks,
    update_order_block_mitigation,
)
from src.smart_money.premium_discount import calculate_premium_discount
from src.strategy.multi_timeframe import MTFContext

logger = logging.getLogger(__name__)


class SMCSignalEngine:
    """
    Full Smart Money Concepts signal generation engine.

    Instantiate once per symbol; call analyze() on each new closed candle.
    Maintains running state (OBs, FVGs, liquidity pools) between calls.
    """

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        strategy_id: str,
        htf_timeframe: Timeframe | None = None,
        ltf_timeframe: Timeframe | None = None,
        min_score: float = 55.0,
        risk_reward_min: float = 1.5,
        atr_period: int = 14,
        swing_lookback: int = 5,
        confluence_weights: ConfluenceWeights | None = None,
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
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy_id = strategy_id
        self.htf_timeframe = htf_timeframe
        self.ltf_timeframe = ltf_timeframe
        self.min_score = min_score
        self.risk_reward_min = risk_reward_min
        self.atr_period = atr_period
        self.swing_lookback = swing_lookback
        # Defaults to the module's original fixed 100pt budget inside
        # score_confluence() itself when None — see
        # smart_money/confluence.py::DEFAULT_CONFLUENCE_WEIGHTS. Exposed
        # here so callers that need to tune per-factor scoring weights
        # (scripts/optimize.py) don't have to reach past this engine.
        self.confluence_weights = confluence_weights
        # configs/strategy.yaml's `smc`/`indicators` blocks — previously
        # each of these had a hardcoded module-level constant with no
        # caller-supplied override anywhere in the codebase (order-block
        # max age, FVG min-size filter, premium/discount zone thresholds,
        # RSI period, MACD's fast/slow EMA periods). None here means "use
        # that module's own constant", so an engine built without these
        # kwargs behaves identically to before this wiring existed — see
        # build_strategy() in main.py, which is the one caller that reads
        # strategy.yaml and threads real values through.
        self.ob_max_age_bars = ob_max_age_bars
        self.fvg_min_size_atr_mult = fvg_min_size_atr_mult
        self.premium_zone = premium_zone
        self.discount_zone = discount_zone
        self.rsi_period = rsi_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        # Only affects the no-OB fallback stop; the OB-anchored branch is
        # unaffected regardless (see confluence.py's
        # _calculate_trade_params for why). "atr" is the pre-existing
        # default — an engine built without this kwarg behaves identically
        # to before this wiring existed.
        self.stop_loss_method = stop_loss_method
        # False (default) preserves the exact pre-existing order-block
        # score for every signal; see score_confluence's docstring for
        # what True adds.
        self.use_mitigation_blocks = use_mitigation_blocks

        # Running state
        self._last_analysis: SMCAnalysis | None = None
        self._signal_count: int = 0
        self._mtf = MTFContext()

    def analyze(
        self,
        candles: list[OHLCV],
        htf_candles: list[OHLCV] | None = None,
        ltf_candles: list[OHLCV] | None = None,
    ) -> Signal | None:
        """
        Run full SMC analysis on latest candles.

        Call this on each new closed candle.

        Args:
            candles: Candle series for the primary timeframe (oldest first).
            htf_candles: Optional higher-timeframe candles for bias.
            ltf_candles: Optional lower-timeframe candles for the MTFContext
                LTF tier. Purely additive — `alignment_score()` degrades to
                scoring against whichever of {execution TF, HTF} tiers are
                actually fed when this is omitted, so callers that don't
                supply it see unchanged behavior.

        Returns:
            A Signal if a valid setup is found, else None.
        """
        if len(candles) < self.atr_period + self.swing_lookback + 3:
            return None

        current_price = candles[-1].close
        now = candles[-1].timestamp

        # ─── 1. Market Structure ──────────────────────────────────────────────
        structure = detect_market_structure(candles, self.swing_lookback)

        # ─── 2. BOS / CHoCH ───────────────────────────────────────────────────
        bos_events = detect_bos(
            candles,
            structure.swing_highs,
            structure.swing_lows,
        )
        choch_events = detect_choch(candles, structure)
        recent_bos = bos_events[-1] if bos_events else None
        recent_choch = choch_events[-1] if choch_events else None

        # ─── 3. Order Blocks ──────────────────────────────────────────────────
        order_blocks = identify_order_blocks(candles, bos_events)
        update_order_block_mitigation(order_blocks, candles)
        active_obs = get_active_order_blocks(
            order_blocks, len(candles) - 1, max_age_bars=self.ob_max_age_bars
        )
        # Always computed (cheap — a single pass over order_blocks) and
        # attached to the analysis regardless of use_mitigation_blocks, so
        # the data is available to any future consumer; only
        # score_confluence()'s order-block factor actually reads it, and
        # only when self.use_mitigation_blocks is True.
        mitigation_blocks = extract_mitigation_blocks(order_blocks)

        # ─── 4. Fair Value Gaps ───────────────────────────────────────────────
        atr_values_arr = calculate_atr(candles, self.atr_period)
        atr_values = [float(v) if not __import__("math").isnan(v) else 0.0 for v in atr_values_arr]
        fvg_kwargs: dict[str, Any] = {}
        if self.fvg_min_size_atr_mult is not None:
            fvg_kwargs["min_size_atr_mult"] = self.fvg_min_size_atr_mult
        fvgs = detect_fvgs(candles, atr_values=atr_values, **fvg_kwargs)
        update_fvg_fills(fvgs, candles)
        open_fvgs = get_open_fvgs(fvgs)

        # ─── 5. Liquidity ─────────────────────────────────────────────────────
        swing_liq = identify_swing_liquidity(structure.swing_highs, structure.swing_lows)
        equal_highs = identify_equal_highs(structure.swing_highs)
        equal_lows = identify_equal_lows(structure.swing_lows)
        all_liquidity = swing_liq + equal_highs + equal_lows

        update_liquidity_sweeps(all_liquidity, candles)
        active_liq = get_active_liquidity(all_liquidity)

        # ─── 6. Inducement ────────────────────────────────────────────────────
        inducements = detect_inducement(candles, all_liquidity)
        recent_inducement = inducements[-1] if inducements else None

        # ─── 7. Premium / Discount ────────────────────────────────────────────
        pd_zones = None
        if structure.last_swing_high and structure.last_swing_low:
            pd_zones = calculate_premium_discount(
                structure.last_swing_high,
                structure.last_swing_low,
                current_price,
                premium_zone=self.premium_zone,
                discount_zone=self.discount_zone,
            )

        # ─── 8. ATR and Volume ────────────────────────────────────────────────
        atr = (
            float(atr_values_arr[-1])
            if not __import__("math").isnan(float(atr_values_arr[-1]))
            else 0.0
        )
        rv = relative_volume(candles)

        # ─── 9. HTF Context ───────────────────────────────────────────────────
        htf_structure = MarketStructure.UNKNOWN
        if htf_candles and len(htf_candles) >= self.swing_lookback * 2 + 2:
            htf_ms = detect_market_structure(htf_candles, self.swing_lookback)
            htf_structure = htf_ms.structure

        # Feed MTFContext for both the confluence-scoring alignment factor
        # (below, via SMCAnalysis.mtf_context) and the post-score hard
        # filter (step 13). Uses self.htf_timeframe explicitly rather than
        # MTFContext's HTF_MAP default, since htf_timeframe is separately
        # configurable (configs/strategy.yaml) and can diverge from it.
        self._mtf.update(self.timeframe, candles, self.swing_lookback)
        if (
            self.htf_timeframe is not None
            and htf_candles
            and len(htf_candles) >= self.swing_lookback * 2 + 2
        ):
            self._mtf.update(self.htf_timeframe, htf_candles, self.swing_lookback)
        # LTF tier: same pattern as HTF above, using self.ltf_timeframe
        # explicitly rather than MTFContext's LTF_MAP default for the same
        # reason. Purely additive to alignment_score() — omitting
        # ltf_candles (caller didn't wire a source) leaves LTF simply
        # absent from `_structures`, which alignment_score() already
        # handles by excluding it from the denominator.
        if (
            self.ltf_timeframe is not None
            and ltf_candles
            and len(ltf_candles) >= self.swing_lookback * 2 + 2
        ):
            self._mtf.update(self.ltf_timeframe, ltf_candles, self.swing_lookback)

        # ─── 10. Assemble Analysis ────────────────────────────────────────────
        analysis = SMCAnalysis(
            symbol=self.symbol,
            timeframe=self.timeframe,
            timestamp=now,
            current_price=current_price,
            structure=structure,
            recent_bos=recent_bos,
            recent_choch=recent_choch,
            active_obs=active_obs,
            open_fvgs=open_fvgs,
            active_liquidity=active_liq,
            recent_inducement=recent_inducement,
            mitigation_blocks=mitigation_blocks,
            premium_discount=pd_zones,
            atr=atr,
            relative_volume=rv,
            htf_structure=htf_structure,
            mtf_context=self._mtf,
            htf_timeframe=self.htf_timeframe,
            ltf_timeframe=self.ltf_timeframe,
        )
        self._last_analysis = analysis

        # ─── 11. Score Confluence ─────────────────────────────────────────────
        result = score_confluence(
            analysis,
            candles,
            self.strategy_id,
            self.risk_reward_min,
            weights=self.confluence_weights,
            rsi_period=self.rsi_period,
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            ob_max_age_bars=self.ob_max_age_bars,
            stop_loss_method=self.stop_loss_method,
            use_mitigation_blocks=self.use_mitigation_blocks,
        )

        if result is None or result.score < self.min_score:
            logger.debug(
                "%s %s: no signal (score=%.1f min=%.1f)",
                self.symbol,
                self.timeframe,
                result.score if result else 0.0,
                self.min_score,
            )
            return None

        if result.entry_price is None or result.stop_loss is None or result.take_profit is None:
            return None

        # ─── 12. HTF Confirmation Filter ─────────────────────────────────────
        # Hard reject: a signal that clears min_score on the graded
        # confluence factors (including the smoothed MTF alignment factor
        # above) can still fire against a clearly-opposed HTF trend, since
        # partial alignment credit lets other factors carry the score. This
        # is the binary confirmation gate on top of that — separate from,
        # not a replacement for, the graded scoring factor.
        direction_str = "bullish" if result.direction == SignalDirection.LONG else "bearish"
        if not self._mtf.is_htf_aligned(self.timeframe, direction_str, htf=self.htf_timeframe):
            logger.debug(
                "%s %s: signal rejected, against HTF bias (direction=%s, htf=%s)",
                self.symbol,
                self.timeframe,
                direction_str,
                self.htf_timeframe,
            )
            return None

        # ─── 13. Build Signal ─────────────────────────────────────────────────
        self._signal_count += 1
        signal = Signal(
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            timeframe=self.timeframe,
            direction=result.direction,
            score=result.score,
            strength=result.strength,
            confidence=min(1.0, result.score / 100.0),
            entry_price=result.entry_price,
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            components=result.components,
            atr=Decimal(str(atr)),
            market_structure=structure.structure.value,
            metadata=result.metadata,
        )

        logger.info(
            "Signal generated: %s %s score=%.1f entry=%s SL=%s TP=%s RR=%.2f",
            self.symbol,
            result.direction,
            result.score,
            result.entry_price,
            result.stop_loss,
            result.take_profit,
            signal.risk_reward_ratio,
        )

        return signal

    @property
    def last_analysis(self) -> SMCAnalysis | None:
        return self._last_analysis

    @property
    def signal_count(self) -> int:
        return self._signal_count
