"""
Feature extraction for the SMC signal filter.

Converts an ``SMCAnalysis`` + ``Signal`` pair into a flat numeric feature
vector suitable for scikit-learn / XGBoost classifiers.

Design principles:
- All features are dimensionless (ratios, booleans, z-scores) so the
  model generalises across instruments and price levels.
- No future data is used — all values are derived from the analysis
  snapshot that was available when the signal was emitted.
- Feature names are stable strings so saved models can be validated
  against the expected schema at load time.
"""

from __future__ import annotations

# Avoid a hard import of SMCAnalysis / Signal at module level to keep
# this module importable even when strategy layers are not initialised.
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from src.domain.signal import Signal
    from src.smart_money.confluence import SMCAnalysis


# ─── Feature schema ───────────────────────────────────────────────────────────

FEATURE_NAMES: list[str] = [
    # ── Confluence score ──────────────────────────────────────────────────────
    "confluence_score",  # 0–100 raw score
    "confidence",  # 0–1 model confidence at generation time
    # ── Risk/reward ───────────────────────────────────────────────────────────
    "risk_reward_ratio",  # TP dist / SL dist
    "stop_distance_pct",  # SL distance as % of entry price
    "target_distance_pct",  # TP distance as % of entry price
    # ── Market structure ─────────────────────────────────────────────────────
    "structure_bullish",  # 1 if HTF structure == bullish
    "structure_bearish",  # 1 if HTF structure == bearish
    "htf_aligned",  # 1 if signal direction matches HTF bias
    "has_bos",  # 1 if a BOS is present
    "has_choch",  # 1 if a CHoCH is present
    # ── Order blocks ──────────────────────────────────────────────────────────
    "has_ob",  # 1 if an active, unmitigated OB is present
    "ob_distance_atr",  # distance from price to nearest OB (in ATR)
    "ob_size_atr",  # OB height in ATR units
    "ob_age_bars",  # bars since OB was formed (normalised 0–1)
    # ── Fair value gaps ───────────────────────────────────────────────────────
    "has_fvg",  # 1 if an open FVG aligns with direction
    "fvg_distance_atr",  # distance to nearest aligned FVG (in ATR)
    "fvg_size_atr",  # FVG height in ATR units
    # ── Liquidity ─────────────────────────────────────────────────────────────
    "has_liquidity_sweep",  # 1 if a liquidity pool was recently swept
    "n_active_liquidity_pools",  # count of active pools (normalised 0–1 / 5)
    # ── Premium / discount zone ───────────────────────────────────────────────
    "in_discount_zone",  # 1 if long setup is in discount zone
    "in_premium_zone",  # 1 if short setup is in premium zone
    "pd_zone_score",  # raw premium/discount zone score
    # ── Volume ────────────────────────────────────────────────────────────────
    "relative_volume",  # current bar volume / rolling average
    # ── Volatility ────────────────────────────────────────────────────────────
    "atr_normalised",  # ATR / price (dimensionless)
    # ── Session ───────────────────────────────────────────────────────────────
    "in_kill_zone",  # 1 if current time is inside a kill zone
    # ── Multi-timeframe ───────────────────────────────────────────────────────
    "mtf_aligned",  # 1 if HTF and LTF structures agree
    # ── Signal metadata ───────────────────────────────────────────────────────
    "direction_long",  # 1 for LONG, 0 for SHORT
    "n_components",  # number of contributing SMC components
    # ── Momentum / statistics (confluence.py metadata, dimensionless) ────────
    # All five default to 0.0 (neutral) when the underlying indicator had
    # insufficient candle history — extract_features() must never raise on
    # a short/degraded analysis, matching every other feature above.
    "rsi_normalised",  # RSI / 100 → [0, 1], 0.5 = neutral
    "macd_histogram_atr",  # MACD histogram / ATR, dimensionless
    "stochastic_k_normalised",  # Stochastic %K / 100 → [0, 1]
    "williams_r_normalised",  # (Williams %R + 100) / 100 → [0, 1]
    "price_zscore",  # rolling z-score of close, already dimensionless
    "trend_slope_atr",  # rolling regression slope / ATR, dimensionless
    # ── Volume/volatility (confluence.py metadata, dimensionless) ────────────
    # vwap/obv_slope/bb_percent_b/bb_bandwidth/historical_volatility joined
    # signal.metadata in a later session than the block above, same
    # "computed but never called from the live strategy path, ML-feature
    # input only" treatment. All five default to a neutral value below on
    # insufficient candle history, matching every feature above.
    "vwap_distance_pct",  # (price - VWAP) / VWAP, dimensionless
    "obv_slope_normalised",  # OBV regression slope, scaled by volume SMA
    "bb_percent_b",  # Bollinger %B, already dimensionless
    "bb_bandwidth",  # Bollinger bandwidth / price, dimensionless
    "historical_volatility",  # annualised, already dimensionless (fraction)
]

N_FEATURES: int = len(FEATURE_NAMES)


def extract_features(
    signal: Signal,
    analysis: SMCAnalysis | None,
    max_ob_age_bars: int = 100,
) -> np.ndarray:
    """
    Extract a fixed-length feature vector from a signal + analysis pair.

    Args:
        signal: The trading signal produced by the SMC engine.
        analysis: The full SMC analysis snapshot at signal generation time,
            or ``None`` if unavailable — every analysis-derived feature
            below degrades to a safe default (0 / neutral / entry price)
            rather than raising.
        max_ob_age_bars: Cap used to normalise OB age to [0, 1].

    Returns:
        1-D ``numpy`` array of shape ``(N_FEATURES,)`` with dtype ``float32``.
        All values are finite — missing / unknown fields default to 0.
    """
    from src.core.enums import MarketStructure, SignalDirection

    f: dict[str, float] = {}

    # ── Confluence & confidence ───────────────────────────────────────────────
    f["confluence_score"] = float(signal.score)
    f["confidence"] = float(signal.confidence)

    # ── Risk/reward ───────────────────────────────────────────────────────────
    f["risk_reward_ratio"] = float(signal.risk_reward_ratio)
    f["stop_distance_pct"] = float(signal.stop_distance_pct)
    entry = float(signal.entry_price)
    tp = float(signal.take_profit)
    f["target_distance_pct"] = abs(tp - entry) / entry * 100.0 if entry else 0.0

    # ── Market structure ──────────────────────────────────────────────────────
    htf = analysis.htf_structure if analysis else MarketStructure.UNKNOWN
    f["structure_bullish"] = 1.0 if htf == MarketStructure.BULLISH else 0.0
    f["structure_bearish"] = 1.0 if htf == MarketStructure.BEARISH else 0.0
    htf_bias = (analysis.htf_bias if analysis else "neutral").lower()
    is_long = signal.direction == SignalDirection.LONG
    f["htf_aligned"] = (
        1.0
        if (is_long and htf_bias == "bullish") or (not is_long and htf_bias == "bearish")
        else 0.0
    )
    f["has_bos"] = 1.0 if (analysis and analysis.recent_bos) else 0.0
    f["has_choch"] = 1.0 if (analysis and analysis.recent_choch) else 0.0

    # ── Order blocks ──────────────────────────────────────────────────────────
    atr = analysis.atr if (analysis and analysis.atr) else 1.0
    price = float(analysis.current_price) if analysis else entry
    obs = analysis.active_obs if analysis else []
    if obs:
        ob = obs[0]
        ob_mid = float(ob.mid)
        f["has_ob"] = 1.0
        f["ob_distance_atr"] = abs(price - ob_mid) / atr if atr else 0.0
        f["ob_size_atr"] = float(ob.top - ob.bottom) / atr if atr else 0.0
        age = getattr(ob, "age_bars", 0) or 0
        f["ob_age_bars"] = min(age / max(max_ob_age_bars, 1), 1.0)
    else:
        f["has_ob"] = 0.0
        f["ob_distance_atr"] = 0.0
        f["ob_size_atr"] = 0.0
        f["ob_age_bars"] = 0.0

    # ── Fair value gaps ───────────────────────────────────────────────────────
    fvgs = analysis.open_fvgs if analysis else []
    if fvgs:
        fvg = fvgs[0]
        fvg_mid = float(fvg.mid)
        f["has_fvg"] = 1.0
        f["fvg_distance_atr"] = abs(price - fvg_mid) / atr if atr else 0.0
        f["fvg_size_atr"] = float(fvg.top - fvg.bottom) / atr if atr else 0.0
    else:
        f["has_fvg"] = 0.0
        f["fvg_distance_atr"] = 0.0
        f["fvg_size_atr"] = 0.0

    # ── Liquidity ─────────────────────────────────────────────────────────────
    liq_pools = analysis.active_liquidity if analysis else []
    swept = [p for p in liq_pools if getattr(p, "is_swept", False)]
    f["has_liquidity_sweep"] = 1.0 if swept else 0.0
    f["n_active_liquidity_pools"] = min(len(liq_pools) / 5.0, 1.0)

    # ── Premium / discount ────────────────────────────────────────────────────
    pd = analysis.premium_discount if analysis else None
    if pd is not None:
        f["in_discount_zone"] = 1.0 if (is_long and getattr(pd, "is_discount", False)) else 0.0
        f["in_premium_zone"] = 1.0 if (not is_long and getattr(pd, "is_premium", False)) else 0.0
        f["pd_zone_score"] = float(getattr(pd, "zone_score", 0.0))
    else:
        f["in_discount_zone"] = 0.0
        f["in_premium_zone"] = 0.0
        f["pd_zone_score"] = 0.0

    # ── Volume ────────────────────────────────────────────────────────────────
    f["relative_volume"] = float(analysis.relative_volume) if analysis else 1.0

    # ── Volatility ────────────────────────────────────────────────────────────
    f["atr_normalised"] = atr / price if price else 0.0

    # ── Session ───────────────────────────────────────────────────────────────
    # Kill-zone information is embedded in the signal's components or metadata
    kill = signal.metadata.get("in_kill_zone", False)
    f["in_kill_zone"] = 1.0 if kill else 0.0

    # ── MTF alignment ─────────────────────────────────────────────────────────
    f["mtf_aligned"] = float(signal.metadata.get("mtf_aligned", f["htf_aligned"]))

    # ── Signal metadata ───────────────────────────────────────────────────────
    f["direction_long"] = 1.0 if is_long else 0.0
    f["n_components"] = float(len(signal.components)) / 10.0  # normalise

    # ── Momentum / statistics ─────────────────────────────────────────────────
    # Populated by smart_money/confluence.py::score_confluence() into
    # signal.metadata; None (insufficient candle history at generation
    # time) degrades to the same neutral defaults used everywhere else in
    # this function rather than raising.
    rsi = signal.metadata.get("rsi")
    f["rsi_normalised"] = float(rsi) / 100.0 if rsi is not None else 0.5

    macd_hist = signal.metadata.get("macd_histogram")
    f["macd_histogram_atr"] = float(macd_hist) / atr if (macd_hist is not None and atr) else 0.0

    stoch_k = signal.metadata.get("stochastic_k")
    f["stochastic_k_normalised"] = float(stoch_k) / 100.0 if stoch_k is not None else 0.5

    wr = signal.metadata.get("williams_r")
    f["williams_r_normalised"] = (float(wr) + 100.0) / 100.0 if wr is not None else 0.5

    zsc = signal.metadata.get("price_zscore")
    f["price_zscore"] = float(zsc) if zsc is not None else 0.0

    slope = signal.metadata.get("trend_slope")
    f["trend_slope_atr"] = float(slope) / atr if (slope is not None and atr) else 0.0

    # ── Volume/volatility ─────────────────────────────────────────────────────
    # Populated by smart_money/confluence.py::score_confluence() into
    # signal.metadata, same block/None-degrades-to-neutral convention as
    # rsi/macd/stochastic/williams_r/price_zscore/trend_slope above.
    vwap_val = signal.metadata.get("vwap")
    f["vwap_distance_pct"] = (
        (entry - float(vwap_val)) / float(vwap_val) * 100.0
        if (vwap_val is not None and float(vwap_val) != 0.0)
        else 0.0
    )

    obv_slope_val = signal.metadata.get("obv_slope")
    # Already dimensionless (OBV slope / average bar volume — see
    # confluence.py's computation) but still theoretically unbounded, so
    # squash with tanh the same way an unbounded ratio elsewhere in this
    # module would need to be to stay well-behaved as model input.
    f["obv_slope_normalised"] = (
        float(np.tanh(float(obv_slope_val))) if obv_slope_val is not None else 0.0
    )

    bb_pb = signal.metadata.get("bb_percent_b")
    f["bb_percent_b"] = float(bb_pb) if bb_pb is not None else 0.5

    bb_bw = signal.metadata.get("bb_bandwidth")
    f["bb_bandwidth"] = float(bb_bw) if bb_bw is not None else 0.0

    hv = signal.metadata.get("historical_volatility")
    f["historical_volatility"] = float(hv) if hv is not None else 0.0

    return np.array([f[name] for name in FEATURE_NAMES], dtype=np.float32)


def features_to_dict(vec: np.ndarray) -> dict[str, float]:
    """Convert a feature vector back to a named dictionary (for inspection)."""
    if len(vec) != N_FEATURES:
        raise ValueError(f"Expected {N_FEATURES} features, got {len(vec)}")
    return dict(zip(FEATURE_NAMES, vec.tolist(), strict=True))
