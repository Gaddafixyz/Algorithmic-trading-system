"""
Smart Money Confluence Scoring Engine.

Aggregates all SMC signals into a weighted confluence score (0–100).
Higher scores = stronger conviction = larger potential position size.

Scoring factors and weights (each factor is graded within its budget
rather than all-or-nothing, so two "found" setups aren't treated as
equally strong — see the per-factor comments below):
  Market structure alignment     20pts
  Order block presence           20pts  (graded: freshness x proximity)
  Fair value gap alignment       15pts  (graded: gap size/ATR x proximity)
  Liquidity sweep confirmation   15pts  (graded: direction alignment x touch count)
  Premium/discount zone          10pts
  Kill zone / session            10pts
  Volume confirmation             5pts
  Multi-timeframe alignment       5pts
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np

from src.core.constants import (
    DEFAULT_BB_PERIOD,
    DEFAULT_EMA_FAST,
    DEFAULT_EMA_SLOW,
    DEFAULT_RSI_PERIOD,
    SMC_OB_MAX_AGE_BARS,
)
from src.core.enums import MarketStructure, SignalDirection, SignalStrength
from src.core.exceptions import InsufficientDataError
from src.core.models import OHLCV
from src.domain.signal import SignalComponent, Timeframe
from src.indicators.atr import current_atr
from src.indicators.macd import macd_from_candles
from src.indicators.momentum import stochastic, williams_r
from src.indicators.rsi import current_rsi
from src.indicators.statistics import linear_regression_slope, zscore
from src.indicators.volatility import bollinger_bands, historical_volatility
from src.indicators.volume import obv, vwap
from src.risk.stop_loss import atr_stop_loss, structure_stop_loss
from src.risk.take_profit import fixed_rr_take_profit, liquidity_target
from src.smart_money.bos import BOSEvent
from src.smart_money.choch import CHoCHEvent
from src.smart_money.fair_value_gap import FairValueGap, fvg_at_price
from src.smart_money.inducement import InducementEvent
from src.smart_money.liquidity import LiquidityPool
from src.smart_money.market_structure import MarketStructureState
from src.smart_money.mitigation import MitigationBlock
from src.smart_money.order_blocks import OrderBlock
from src.smart_money.premium_discount import PremiumDiscountZones
from src.smart_money.sessions import session_score
from src.strategy.scoring import classify_strength

logger = logging.getLogger(__name__)

# Order blocks younger than this keep full freshness credit; beyond it,
# their contribution decays linearly down to a floor by SMC_OB_MAX_AGE_BARS
# (an order block near the end of its validity window is a weaker POI than
# one that formed a few candles ago).
OB_FRESHNESS_FULL_CREDIT_BARS = 50
OB_FRESHNESS_FLOOR = 0.6

# A FVG whose size is at least half of ATR is considered a "real" imbalance
# worth full credit; smaller gaps are graded down proportionally.
FVG_FULL_CREDIT_SIZE_ATR_RATIO = 0.5


@dataclass(frozen=True)
class ConfluenceWeights:
    """
    Tunable point budgets for each confluence factor (see module docstring).

    Defaults reproduce the original hardcoded scoring exactly (they sum to
    100, matching the fixed budget the rest of the module — `min_score`
    gates, `classify_strength()` thresholds, `ml_filter` score features —
    was designed and tested against).

    `score_confluence()` always works with `.normalized()` weights, so any
    positive combination passed in (e.g. from an Optuna search space in
    `scripts/optimize.py::score_confluence` callers) still yields a 0–100
    score with those same downstream thresholds intact, regardless of what
    the 8 raw values sum to.
    """

    market_structure: float = 20.0
    order_block: float = 20.0
    fair_value_gap: float = 15.0
    liquidity_sweep: float = 15.0
    premium_discount: float = 10.0
    session: float = 10.0
    volume: float = 5.0
    htf_alignment: float = 5.0

    def __post_init__(self) -> None:
        if any(v < 0 for v in self._values):
            raise ValueError("ConfluenceWeights values must be non-negative")
        if sum(self._values) <= 0:
            raise ValueError("ConfluenceWeights must sum to a positive value")

    @property
    def _values(self) -> tuple[float, ...]:
        return (
            self.market_structure,
            self.order_block,
            self.fair_value_gap,
            self.liquidity_sweep,
            self.premium_discount,
            self.session,
            self.volume,
            self.htf_alignment,
        )

    @property
    def total(self) -> float:
        return sum(self._values)

    def normalized(self) -> ConfluenceWeights:
        """Rescaled so the 8 budgets sum to exactly 100."""
        factor = 100.0 / self.total
        return ConfluenceWeights(*(v * factor for v in self._values))

    def as_dict(self) -> dict[str, float]:
        return {
            "market_structure": self.market_structure,
            "order_block": self.order_block,
            "fair_value_gap": self.fair_value_gap,
            "liquidity_sweep": self.liquidity_sweep,
            "premium_discount": self.premium_discount,
            "session": self.session,
            "volume": self.volume,
            "htf_alignment": self.htf_alignment,
        }


# The pre-tuning defaults — identical to the literals this module used
# before weights became configurable, so any caller that doesn't pass
# `weights=` to `score_confluence()` sees byte-for-byte the same scores
# as before this change.
DEFAULT_CONFLUENCE_WEIGHTS = ConfluenceWeights()


@dataclass
class SMCAnalysis:
    """Full SMC analysis result for a single symbol/timeframe."""

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    current_price: Decimal

    # Structure
    structure: MarketStructureState | None = None
    recent_bos: BOSEvent | None = None
    recent_choch: CHoCHEvent | None = None

    # Points of Interest
    active_obs: list[OrderBlock] = field(default_factory=list)
    open_fvgs: list[FairValueGap] = field(default_factory=list)
    active_liquidity: list[LiquidityPool] = field(default_factory=list)
    recent_inducement: InducementEvent | None = None
    # Unfilled remainder of order blocks that have been partially
    # mitigated (see smart_money/mitigation.py::extract_mitigation_blocks).
    # Empty unless the caller populates it (signal_engine.py's analyze()
    # does); only consulted by score_confluence()'s order-block factor
    # when use_mitigation_blocks=True, as a fallback when no active OB is
    # found — see that function for why active_obs above never contains
    # a mitigated OB even though the mitigated OB itself is still
    # useful as a reference.
    mitigation_blocks: list[MitigationBlock] = field(default_factory=list)

    # Context
    premium_discount: PremiumDiscountZones | None = None
    atr: float = 0.0
    relative_volume: float = 1.0

    # HTF context
    htf_structure: MarketStructure = MarketStructure.UNKNOWN
    htf_bias: str = "neutral"  # "bullish", "bearish", "neutral"

    # Multi-timeframe alignment context (execution TF + HTF, and LTF if the
    # caller feeds it): an `src.strategy.multi_timeframe.MTFContext`
    # instance, duck-typed here (`Any`) rather than imported by name —
    # `strategy/` is an outer layer than `smart_money/` per
    # ARCHITECTURE.md, so this module must not import from it. Optional so
    # existing callers/tests that construct SMCAnalysis directly keep
    # working — the HTF scoring section below falls back to htf_structure
    # when this is None.
    mtf_context: Any = None

    # Explicit HTF/LTF overrides, threaded through from
    # SMCSignalEngine.htf_timeframe/ltf_timeframe, so the alignment_score()
    # call below can pass them through instead of relying on MTFContext's
    # HTF_MAP/LTF_MAP defaults. Those defaults only match what was
    # actually `update()`d when the configured HTF/LTF happens to equal
    # the fixed map's entry for `timeframe` — passing them explicitly
    # avoids silently scoring against a timeframe key that's never fed
    # (see multi_timeframe.py::MTFContext.alignment_score docstring).
    htf_timeframe: Timeframe | None = None
    ltf_timeframe: Timeframe | None = None


@dataclass
class ConfluenceResult:
    """Result of confluence scoring."""

    score: float  # 0–100
    direction: SignalDirection
    strength: SignalStrength
    components: list[SignalComponent] = field(default_factory=list)
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def score_confluence(
    analysis: SMCAnalysis,
    candles: list[OHLCV],
    strategy_id: str,
    risk_reward_min: float = 1.5,
    weights: ConfluenceWeights | None = None,
    rsi_period: int | None = None,
    ema_fast: int | None = None,
    ema_slow: int | None = None,
    ob_max_age_bars: int | None = None,
    stop_loss_method: str = "atr",
    use_mitigation_blocks: bool = False,
) -> ConfluenceResult | None:
    """
    Score SMC confluence factors and produce a trade setup if valid.

    Returns None if no valid setup is found or score < minimum threshold.

    Args:
        weights: Per-factor point budgets. Defaults to
            `DEFAULT_CONFLUENCE_WEIGHTS` (the original fixed 100pt budget:
            20/20/15/15/10/10/5/5 — see module docstring). Always used
            `.normalized()`, so any positive combination still produces a
            0–100 score.
        rsi_period: Period for the informational/filter RSI value below.
            Defaults to DEFAULT_RSI_PERIOD (configs/strategy.yaml's
            `indicators.rsi_period`) when omitted.
        ema_fast: Fast EMA period for the informational/filter MACD
            histogram below. Defaults to DEFAULT_EMA_FAST
            (`indicators.ema_fast`) when omitted.
        ema_slow: Slow EMA period for the informational/filter MACD
            histogram below. Defaults to DEFAULT_EMA_SLOW
            (`indicators.ema_slow`) when omitted.
        ob_max_age_bars: Max order-block age (in bars) for both the
            active/valid filter and the freshness-decay curve in the
            order-block scoring factor below. Defaults to
            SMC_OB_MAX_AGE_BARS (`smc.ob_max_age_bars`) when omitted.
        stop_loss_method: Only affects the *no-OB fallback* stop (when
            there's no order block to anchor to) — the OB-anchored branch
            always uses its own ATR-scaled buffer regardless of this
            setting (see _calculate_trade_params' inline comment for why).
            "atr" (default, unchanged behavior): risk/stop_loss.py's
            atr_stop_loss(). "structure": place the stop just beyond the
            nearest opposing swing point (risk/stop_loss.py's
            structure_stop_loss()) when one exists, falling back to
            atr_stop_loss() when it doesn't (no swing point, or the
            resulting stop would sit on the wrong side of entry).
        use_mitigation_blocks: When True and no active order block scores
            (ob_score stays 0), fall back to checking whether price sits
            inside the unfilled remainder of a *mitigated* OB (see
            smart_money/mitigation.py::extract_mitigation_blocks and
            SMCAnalysis.mitigation_blocks). Scored at a flat 60% of what a
            fully-fresh, price-inside active OB would earn — a mitigated
            OB is a weaker reference than a fresh one since part of it has
            already been filled, so it shouldn't outscore or match an
            active OB, only fill in when there isn't one. False (default)
            preserves the exact pre-existing score for every signal.
    """
    weights = (weights or DEFAULT_CONFLUENCE_WEIGHTS).normalized()
    components: list[SignalComponent] = []
    total_score = 0.0
    direction: SignalDirection | None = None
    atr = analysis.atr or current_atr(candles)
    ob_max_age = ob_max_age_bars if ob_max_age_bars is not None else SMC_OB_MAX_AGE_BARS

    # RSI and MACD histogram — computed for informational/filter use only
    # (see strategy/filters.py::filter_by_rsi_exhaustion and
    # filter_by_macd_trend, wired opt-in in StrategyOrchestrator). Neither
    # value feeds into total_score: the scoring budget above is a fixed,
    # tested 100pts across the existing SMC factors, and folding momentum
    # indicators into it would silently change every existing score.
    # Both indicators.{rsi,macd} modules were previously fully implemented
    # and unit-tested but never called from anywhere in the strategy
    # pipeline; this is that wiring. None on insufficient candle history
    # (both filters below already no-op on None, mirroring how
    # filter_by_minimum_atr no-ops on atr=0).
    try:
        rsi_value: float | None = current_rsi(
            candles, period=rsi_period if rsi_period is not None else DEFAULT_RSI_PERIOD
        )
    except InsufficientDataError:
        rsi_value = None

    macd_histogram: float | None = None
    try:
        macd_result = macd_from_candles(
            candles,
            fast=ema_fast if ema_fast is not None else DEFAULT_EMA_FAST,
            slow=ema_slow if ema_slow is not None else DEFAULT_EMA_SLOW,
        )
    except InsufficientDataError:
        macd_result = None
    if (
        macd_result is not None
        and len(macd_result.histogram)
        and not np.isnan(macd_result.histogram[-1])
    ):
        macd_histogram = float(macd_result.histogram[-1])

    # Stochastic %K, Williams %R, rolling price z-score, rolling regression
    # slope — indicators/{momentum,statistics}.py, same "computed but never
    # called from the live strategy path" state as RSI/MACD above until
    # now. Unlike RSI/MACD, none of these get a dedicated
    # accept/reject filter: %K is functionally a second overbought/oversold
    # oscillator alongside RSI, and stacking multiple near-duplicate
    # exhaustion gates would mostly just double-count the same signal
    # while cluttering config. Instead all four go into signal.metadata
    # purely as ML feature inputs (see machine_learning/features.py) —
    # extract_features() already reads several confluence-metadata keys
    # this way (in_kill_zone, mtf_aligned), and the ML filter is the one
    # consumer built to make actual use of extra numeric context without
    # needing a hand-picked cutoff. rate_of_change() is intentionally not
    # included: it's redundant with the z-score/slope pair below for this
    # purpose (all three answer "how extended is price"), and three highly
    # correlated momentum features add training-time collinearity without
    # adding information.
    stochastic_k: float | None = None
    k_arr, _ = stochastic(candles)
    if len(k_arr) and not np.isnan(k_arr[-1]):
        stochastic_k = float(k_arr[-1])

    williams_r_value: float | None = None
    wr_arr = williams_r(candles)
    if len(wr_arr) and not np.isnan(wr_arr[-1]):
        williams_r_value = float(wr_arr[-1])

    price_zscore: float | None = None
    trend_slope: float | None = None
    closes = np.array([float(c.close) for c in candles])
    if len(closes) >= 2:
        z_arr = zscore(closes, period=min(20, len(closes)))
        if len(z_arr) and not np.isnan(z_arr[-1]):
            price_zscore = float(z_arr[-1])

        slope_period = min(20, len(closes))
        slope_arr = linear_regression_slope(closes, period=slope_period)
        if len(slope_arr) and not np.isnan(slope_arr[-1]):
            trend_slope = float(slope_arr[-1])

    # OBV, VWAP, Bollinger %B/bandwidth, historical volatility —
    # indicators/{volume,volatility}.py, same "computed but never called
    # from the live strategy path" state as the stochastic/Williams %R/
    # z-score/slope block above until now, and joining it for the same
    # reason: ML feature inputs rather than a new hand-picked filter.
    # is_high_volume_bar() is deliberately excluded here — it's a
    # threshold wrapper around relative_volume(), which already has a
    # live scoring role (the "volume" confluence factor below); adding it
    # as a feature/filter would just double-count that same metric in
    # boolean form. Raw OBV (a running unnormalized total, not a scaled
    # oscillator) is intentionally not included as a feature — comparable
    # across bars of one series but not across symbols, so on its own
    # it's not a like-for-like ML input the way the OBV *slope* below is;
    # only the slope is stored.
    vwap_value: float | None = None
    try:
        vwap_value = vwap(candles)
    except (ZeroDivisionError, ValueError):
        vwap_value = None

    obv_slope: float | None = None
    try:
        obv_arr = obv(candles)
        if len(obv_arr) >= 2:
            slope_window = min(20, len(obv_arr))
            obv_slope_arr = linear_regression_slope(obv_arr, period=slope_window)
            if len(obv_slope_arr) and not np.isnan(obv_slope_arr[-1]):
                raw_slope = float(obv_slope_arr[-1])
                # OBV is a running unnormalized total, so its slope's
                # magnitude scales with the asset's typical volume —
                # comparable bar-to-bar within one series (which is all
                # the earlier "raw OBV excluded, only the slope is
                # useful" reasoning needs) but not across symbols as a
                # raw number. Dividing by the average bar volume over the
                # same window converts it to "OBV momentum in units of
                # average volume per bar", which is comparable across
                # symbols the same way relative_volume already is.
                avg_vol = float(np.mean([float(c.volume) for c in candles[-slope_window:]]))
                obv_slope = raw_slope / avg_vol if avg_vol > 0 else None
    except (ValueError, IndexError):
        obv_slope = None

    bb_percent_b: float | None = None
    bb_bandwidth: float | None = None
    if len(candles) >= DEFAULT_BB_PERIOD:
        bb = bollinger_bands(candles, period=DEFAULT_BB_PERIOD)
        if len(bb.percent_b) and not np.isnan(bb.percent_b[-1]):
            bb_percent_b = float(bb.percent_b[-1])
        if len(bb.bandwidth) and not np.isnan(bb.bandwidth[-1]):
            bb_bandwidth = float(bb.bandwidth[-1])

    hist_volatility: float | None = None
    if len(candles) >= 21:
        hv = historical_volatility(candles)
        hist_volatility = hv if hv > 0.0 else None

    # ─── 1. Market Structure (20 pts) ─────────────────────────────────────────
    structure_score = 0.0
    structure_direction: str | None = None

    if analysis.structure and analysis.structure.structure != MarketStructure.UNKNOWN:
        if analysis.structure.structure == MarketStructure.BULLISH:
            structure_score = weights.market_structure
            structure_direction = "bullish"
        elif analysis.structure.structure == MarketStructure.BEARISH:
            structure_score = weights.market_structure
            structure_direction = "bearish"
        else:
            structure_score = weights.market_structure * 0.25  # Ranging — reduced score

    if analysis.recent_choch:
        # CHoCH increases score but flips direction
        structure_score = min(
            weights.market_structure, structure_score + weights.market_structure * 0.25
        )
        structure_direction = analysis.recent_choch.direction

    components.append(
        SignalComponent(
            name="market_structure",
            score=structure_score,
            weight=weights.market_structure / 100.0,
            description=f"Structure: {analysis.structure.structure if analysis.structure else 'unknown'}",
        )
    )
    total_score += structure_score

    if structure_direction is None:
        return None  # No clear directional bias
    direction = SignalDirection.LONG if structure_direction == "bullish" else SignalDirection.SHORT

    # ─── 2. Order Block (20 pts, graded by freshness + proximity) ────────────
    ob_score = 0.0
    relevant_ob: OrderBlock | None = None
    current_bar = len(candles) - 1

    for ob in analysis.active_obs:
        if not ob.is_valid(current_bar, ob_max_age):
            continue

        if direction == SignalDirection.LONG and ob.ob_type.value == "bullish":
            contains = ob.contains_price(analysis.current_price)
            near = ob.top >= analysis.current_price * Decimal("0.99")
        elif direction == SignalDirection.SHORT and ob.ob_type.value == "bearish":
            contains = ob.contains_price(analysis.current_price)
            near = ob.bottom <= analysis.current_price * Decimal("1.01")
        else:
            continue

        if not (contains or near):
            continue

        age = current_bar - ob.bar_index
        if age <= OB_FRESHNESS_FULL_CREDIT_BARS:
            freshness = 1.0
        else:
            decay_span = max(1, ob_max_age - OB_FRESHNESS_FULL_CREDIT_BARS)
            decay_progress = (age - OB_FRESHNESS_FULL_CREDIT_BARS) / decay_span
            freshness = max(OB_FRESHNESS_FLOOR, 1.0 - (1.0 - OB_FRESHNESS_FLOOR) * decay_progress)

        proximity = 1.0 if contains else 0.75
        candidate_score = weights.order_block * freshness * proximity

        if candidate_score > ob_score:
            ob_score = candidate_score
            relevant_ob = ob

    if use_mitigation_blocks and ob_score == 0.0:
        # No active OB scored — fall back to the unfilled remainder of a
        # mitigated one, if price is currently inside it. Doesn't touch
        # relevant_ob/entry-price calc below: a mitigation block is a
        # scoring-only reference, not a new entry anchor, so
        # _calculate_trade_params still falls through to its FVG/
        # current-price/no-OB paths exactly as if ob_score had stayed 0.
        for mb in analysis.mitigation_blocks:
            ob_type_value = mb.original_ob.ob_type.value
            if direction == SignalDirection.LONG and ob_type_value != "bullish":
                continue
            if direction == SignalDirection.SHORT and ob_type_value != "bearish":
                continue
            if not mb.contains_price(analysis.current_price):
                continue

            candidate_score = weights.order_block * 0.6
            if candidate_score > ob_score:
                ob_score = candidate_score

    ob_score = round(ob_score, 2)
    components.append(
        SignalComponent(
            name="order_block",
            score=ob_score,
            weight=weights.order_block / 100.0,
            description=(
                f"OB: {'found (fresh)' if ob_score >= 19.0 else 'found (aged/off-zone)' if relevant_ob else 'none'}"
            ),
        )
    )
    total_score += ob_score

    # ─── 3. Fair Value Gap (15 pts, graded by gap size + proximity) ──────────
    fvg_score = 0.0
    relevant_fvg: FairValueGap | None = None

    # Containment (price is currently inside the zone) is computed once via
    # the shared fvg_at_price() utility rather than re-deriving it per-FVG
    # below; "near" (not yet touched, but close) still needs its own check
    # since that's a proximity query fvg_at_price() doesn't answer.
    contained_fvgs = fvg_at_price(analysis.open_fvgs, analysis.current_price)

    for fvg in analysis.open_fvgs:
        if direction == SignalDirection.LONG and fvg.fvg_type.value == "bullish":
            contains = fvg in contained_fvgs
            near = abs(float(fvg.top - analysis.current_price)) < atr
        elif direction == SignalDirection.SHORT and fvg.fvg_type.value == "bearish":
            contains = fvg in contained_fvgs
            near = abs(float(fvg.bottom - analysis.current_price)) < atr
        else:
            continue

        if not (contains or near):
            continue

        # A gap smaller than half of ATR is a shallow imbalance and less
        # likely to be a meaningful institutional footprint.
        size_ratio = (float(fvg.size) / atr) if atr > 0 else FVG_FULL_CREDIT_SIZE_ATR_RATIO
        size_factor = min(1.0, FVG_FULL_CREDIT_SIZE_ATR_RATIO + size_ratio)
        proximity_factor = 1.0 if contains else 0.8
        candidate_score = weights.fair_value_gap * size_factor * proximity_factor

        if candidate_score > fvg_score:
            fvg_score = candidate_score
            relevant_fvg = fvg

    fvg_score = round(fvg_score, 2)
    components.append(
        SignalComponent(
            name="fair_value_gap",
            score=fvg_score,
            weight=weights.fair_value_gap / 100.0,
            description=f"FVG: {'aligned' if relevant_fvg else 'none'}",
        )
    )
    total_score += fvg_score

    # ─── 4. Liquidity Sweep (15 pts) ──────────────────────────────────────────
    # A swept pool only supports the trade if it was swept on the side that
    # would actually flush out counter-trend stops for this direction: a
    # LONG wants sell-side (equal lows / SSL) liquidity taken, a SHORT wants
    # buy-side (equal highs / BSL) liquidity taken. A swept pool on the
    # *wrong* side isn't confirmation and previously was incorrectly scored
    # the same as an aligned sweep.
    liq_score = 0.0
    liq_scale = weights.liquidity_sweep / 15.0  # original budget for this factor

    if analysis.recent_inducement:
        ind = analysis.recent_inducement
        if (
            direction == SignalDirection.LONG
            and ind.is_bullish
            or direction == SignalDirection.SHORT
            and ind.is_bearish
        ):
            liq_score = 15.0 * liq_scale
    else:
        aligned_swept = [
            p
            for p in analysis.active_liquidity
            if p.is_swept
            and (
                (direction == SignalDirection.LONG and p.is_sell_side)
                or (direction == SignalDirection.SHORT and p.is_buy_side)
            )
        ]
        if aligned_swept:
            # More prior touches at the swept level = a bigger stop cluster
            # was likely resting there, i.e. a more meaningful grab.
            best_touches = max(p.confluence_count for p in aligned_swept)
            liq_score = min(12.0, 8.0 + max(0, best_touches - 1) * 1.5) * liq_scale
        elif any(p.is_swept for p in analysis.active_liquidity):
            # Something was swept, but not on the side that supports this
            # direction — weak, non-directional signal only.
            liq_score = 4.0 * liq_scale

    components.append(
        SignalComponent(
            name="liquidity_sweep",
            score=liq_score,
            weight=weights.liquidity_sweep / 100.0,
            description=f"Liquidity: {'swept' if liq_score > 0 else 'none'}",
        )
    )
    total_score += liq_score

    # ─── 5. Premium/Discount Zone (10 pts) ────────────────────────────────────
    pd_score = 0.0
    pd_scale = weights.premium_discount / 10.0  # original budget for this factor
    if analysis.premium_discount:
        pd = analysis.premium_discount
        if (
            direction == SignalDirection.LONG
            and pd.is_discount
            or direction == SignalDirection.SHORT
            and pd.is_premium
        ):
            pd_score = 10.0 * pd_scale
        elif not pd.is_premium and not pd.is_discount:
            pd_score = 3.0 * pd_scale  # Equilibrium — partial score

    components.append(
        SignalComponent(
            name="premium_discount",
            score=pd_score,
            weight=weights.premium_discount / 100.0,
            description=f"Zone: {analysis.premium_discount.zone_label if analysis.premium_discount else 'unknown'}",
        )
    )
    total_score += pd_score

    # ─── 6. Session/Kill Zone (10 pts) ────────────────────────────────────────
    now = analysis.timestamp
    sess_score_val = session_score(now) * weights.session
    components.append(
        SignalComponent(
            name="session",
            score=sess_score_val,
            weight=weights.session / 100.0,
            description=f"Session score: {sess_score_val:.1f}/10",
        )
    )
    total_score += sess_score_val

    # ─── 7. Volume Confirmation (5 pts) ───────────────────────────────────────
    vol_score = 0.0
    vol_scale = weights.volume / 5.0  # original budget for this factor
    rv = analysis.relative_volume
    if rv >= 2.0:
        vol_score = 5.0 * vol_scale
    elif rv >= 1.5:
        vol_score = 3.0 * vol_scale
    elif rv >= 1.0:
        vol_score = 1.0 * vol_scale

    components.append(
        SignalComponent(
            name="volume",
            score=vol_score,
            weight=weights.volume / 100.0,
            description=f"Relative volume: {rv:.2f}x",
        )
    )
    total_score += vol_score

    # ─── 8. HTF/MTF Alignment (5 pts) ─────────────────────────────────────────
    htf_score = 0.0
    htf_scale = weights.htf_alignment / 5.0  # original budget for this factor
    direction_str = "bullish" if direction == SignalDirection.LONG else "bearish"
    htf_description = f"HTF: {analysis.htf_structure}"
    if analysis.mtf_context is not None:
        # MTFContext.alignment_score() grades how many of the *available*
        # timeframes (execution TF, HTF, and LTF if fed) agree with the
        # trade direction, rather than the single-HTF three-way check
        # below — a smoother, multi-timeframe-aware version of the same
        # factor. See src/strategy/multi_timeframe.py::MTFContext.
        alignment = analysis.mtf_context.alignment_score(
            analysis.timeframe,
            direction_str,
            htf=analysis.htf_timeframe,
            ltf=analysis.ltf_timeframe,
        )
        htf_score = alignment * 5.0 * htf_scale
        htf_description = f"MTF alignment: {alignment:.2f}"
    elif analysis.htf_structure != MarketStructure.UNKNOWN:
        if (
            direction == SignalDirection.LONG
            and analysis.htf_structure == MarketStructure.BULLISH
            or direction == SignalDirection.SHORT
            and analysis.htf_structure == MarketStructure.BEARISH
        ):
            htf_score = 5.0 * htf_scale
        elif analysis.htf_structure == MarketStructure.RANGING:
            htf_score = 1.0 * htf_scale

    components.append(
        SignalComponent(
            name="htf_alignment",
            score=htf_score,
            weight=weights.htf_alignment / 100.0,
            description=htf_description,
        )
    )
    total_score += htf_score

    # ─── Determine entry parameters ───────────────────────────────────────────
    entry_price, stop_loss, take_profit = _calculate_trade_params(
        analysis,
        direction,
        relevant_ob,
        relevant_fvg,
        atr,
        risk_reward_min,
        stop_loss_method=stop_loss_method,
    )

    if entry_price is None or stop_loss is None or take_profit is None:
        logger.debug("Cannot calculate valid trade params for %s", analysis.symbol)
        return None

    # Minimum score gate
    if total_score < 45.0:
        logger.debug("Score too low: %.1f for %s %s", total_score, analysis.symbol, direction)
        return None

    # Determine strength label — delegated to strategy/scoring.py's
    # classify_strength() rather than reimplemented inline, which is what
    # this used to do with a MODERATE threshold of >=55 that silently
    # disagreed with classify_strength()'s >=50. classify_strength() was
    # otherwise dead code; this was the one caller that should have used
    # it. Net effect: signals scored 50-54.9 now classify as MODERATE
    # instead of WEAK, which raises their position_size_multiplier()
    # (0.5x instead of whatever WEAK maps to) — a real, intentional
    # change to position sizing for that score band, not just a refactor.
    strength = classify_strength(total_score)

    return ConfluenceResult(
        score=round(total_score, 2),
        direction=direction,
        strength=strength,
        components=components,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata={
            "atr": atr,
            "has_ob": relevant_ob is not None,
            "has_fvg": relevant_fvg is not None,
            "htf_structure": analysis.htf_structure.value,
            "rsi": rsi_value,
            "macd_histogram": macd_histogram,
            "stochastic_k": stochastic_k,
            "williams_r": williams_r_value,
            "price_zscore": price_zscore,
            "trend_slope": trend_slope,
            "vwap": vwap_value,
            "obv_slope": obv_slope,
            "bb_percent_b": bb_percent_b,
            "bb_bandwidth": bb_bandwidth,
            "historical_volatility": hist_volatility,
            # Full open-FVG snapshot (not just the single best-aligned one
            # scored above) so exit-management can later classify zones as
            # partial-TP targets / reversal-risk warnings relative to
            # whichever direction the resulting position actually takes.
            # ExecutionEngine has no live SMCAnalysis reference once a
            # position is open, so this is the only channel that data has
            # to reach it — see execution_engine.py::_on_entry_filled.
            "open_fvgs": [
                {
                    "type": fvg.fvg_type.value,
                    "top": str(fvg.top),
                    "bottom": str(fvg.bottom),
                }
                for fvg in analysis.open_fvgs
            ],
        },
    )


def _calculate_trade_params(
    analysis: SMCAnalysis,
    direction: SignalDirection,
    ob: OrderBlock | None,
    fvg: FairValueGap | None,
    atr: float,
    risk_reward_min: float,
    stop_loss_method: str = "atr",
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Calculate entry, stop-loss, and take-profit levels."""
    price = analysis.current_price
    atr_d = Decimal(str(atr))

    # Entry: use OB mid if available, then FVG mid, then current price
    if ob is not None:
        entry = ob.mid
    elif fvg is not None:
        entry = fvg.mid
    else:
        entry = price

    # Stop loss: below OB bottom (long) or above OB top (short) + buffer.
    # The OB-anchored branch uses its own ATR-scaled buffer methodology,
    # distinct from risk/stop_loss.py's ob_stop_loss() (which buffers by a
    # % of price instead) — left as-is rather than swapped in, since that
    # would change every OB-anchored stop's placement, not just dedupe.
    # stop_loss_method is deliberately not consulted here either, for the
    # same reason: it only applies to the no-OB fallback below.
    buffer = atr_d * Decimal("0.3")

    def _structure_fallback(swing_price: Decimal | None) -> Decimal | None:
        """
        risk/stop_loss.py::structure_stop_loss() anchored to the nearest
        opposing swing point, when stop_loss_method='structure' and one
        is available. Returns None (caller falls back to atr_stop_loss)
        when there's no swing point yet — e.g. right after startup, before
        swing_lookback bars of history have accumulated any confirmed
        swings — same "not enough context yet" situation atr_stop_loss()
        exists to always cover.
        """
        if stop_loss_method != "structure" or swing_price is None:
            return None
        return structure_stop_loss(entry, direction, swing_price)

    if direction == SignalDirection.LONG:
        if ob is not None:
            sl = ob.bottom - buffer
        else:
            # No OB to anchor to — falls back to a plain ATR stop, unless
            # stop_loss_method='structure' and a swing low is available,
            # in which case the stop anchors just below it instead.
            swing_low = analysis.structure.last_swing_low if analysis.structure else None
            swing_price = swing_low.price if swing_low is not None else None
            sl = _structure_fallback(swing_price) or atr_stop_loss(entry, direction, atr_d)
        if sl >= entry:
            # Structure stop landed on the wrong side of entry (e.g. the
            # last swing low is above current price in a fast-moving
            # market) — fall back to the always-valid ATR stop rather
            # than reject the signal outright.
            sl = atr_stop_loss(entry, direction, atr_d)
            if sl >= entry:
                return None, None, None
    else:
        if ob is not None:
            sl = ob.top + buffer
        else:
            swing_high = analysis.structure.last_swing_high if analysis.structure else None
            swing_price = swing_high.price if swing_high is not None else None
            sl = _structure_fallback(swing_price) or atr_stop_loss(entry, direction, atr_d)
        if sl <= entry:
            sl = atr_stop_loss(entry, direction, atr_d)
            if sl <= entry:
                return None, None, None

    # Fixed risk/reward take-profit — delegated to risk/take_profit.py's
    # fixed_rr_take_profit() rather than reimplemented inline, which is
    # what this used to do (entry ± risk*risk_reward_min duplicated
    # exactly what that function already computes).
    tp = fixed_rr_take_profit(entry, sl, direction, risk_reward=Decimal(str(risk_reward_min)))

    # Prefer targeting the nearest un-swept liquidity pool in the trade's
    # favor over the blind fixed-RR target, when one exists and clearing
    # it still meets risk_reward_min. SMC theory holds price is drawn
    # toward resting, un-mitigated liquidity — a LONG targets buy-side
    # liquidity resting above entry (equal highs / BSL / swing highs), a
    # SHORT targets sell-side liquidity resting below. Falls back to the
    # fixed-RR target above when no qualifying pool exists, so this only
    # ever extends or matches the fixed-RR reward, never shortens it below
    # risk_reward_min.
    risk = abs(entry - sl)
    if risk > Decimal("0"):
        candidate_pools = [
            p
            for p in analysis.active_liquidity
            if not p.is_swept
            and (
                (direction == SignalDirection.LONG and p.is_buy_side and p.price > entry)
                or (direction == SignalDirection.SHORT and p.is_sell_side and p.price < entry)
            )
        ]
        if candidate_pools:
            nearest = min(candidate_pools, key=lambda p: abs(p.price - entry))
            liq_tp = liquidity_target(direction, nearest.price)
            liq_reward = abs(liq_tp - entry)
            if liq_reward / risk >= Decimal(str(risk_reward_min)):
                tp = liq_tp

    return entry, sl, tp
