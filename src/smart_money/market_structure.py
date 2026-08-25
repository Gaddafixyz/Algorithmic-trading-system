"""
Smart Money Concepts: Market Structure Analysis.

Identifies:
- Swing highs and swing lows (pivot points)
- Higher highs / higher lows (bullish structure)
- Lower highs / lower lows (bearish structure)
- Current market structure bias

This is the foundation for BOS (Break of Structure) and CHoCH
(Change of Character) detection, which build on top of identified swings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.core.constants import SMC_SWING_LOOKBACK
from src.core.enums import MarketStructure
from src.core.models import OHLCV

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwingPoint:
    """A confirmed swing high or swing low."""

    price: Decimal
    timestamp: datetime
    bar_index: int
    is_high: bool       # True = swing high, False = swing low
    strength: int = 1   # Number of bars confirming on each side

    @property
    def is_low(self) -> bool:
        return not self.is_high

    def __str__(self) -> str:
        kind = "HIGH" if self.is_high else "LOW"
        return f"Swing{kind}({self.price} @ {self.timestamp.strftime('%Y-%m-%d %H:%M')})"


@dataclass
class MarketStructureState:
    """Running state of market structure analysis."""

    structure: MarketStructure = MarketStructure.UNKNOWN
    swing_highs: list[SwingPoint] = field(default_factory=list)
    swing_lows: list[SwingPoint] = field(default_factory=list)

    @property
    def last_swing_high(self) -> SwingPoint | None:
        return self.swing_highs[-1] if self.swing_highs else None

    @property
    def last_swing_low(self) -> SwingPoint | None:
        return self.swing_lows[-1] if self.swing_lows else None

    @property
    def prev_swing_high(self) -> SwingPoint | None:
        return self.swing_highs[-2] if len(self.swing_highs) >= 2 else None

    @property
    def prev_swing_low(self) -> SwingPoint | None:
        return self.swing_lows[-2] if len(self.swing_lows) >= 2 else None


def find_swing_highs(
    candles: list[OHLCV],
    lookback: int = SMC_SWING_LOOKBACK,
) -> list[SwingPoint]:
    """
    Identify swing highs in candle data.

    A swing high is a bar whose high is strictly greater than the
    `lookback` bars on each side.
    """
    swings: list[SwingPoint] = []
    n = len(candles)

    for i in range(lookback, n - lookback):
        pivot_high = candles[i].high
        left = [candles[i - j].high for j in range(1, lookback + 1)]
        right = [candles[i + j].high for j in range(1, lookback + 1)]

        if all(pivot_high > h for h in left) and all(pivot_high >= h for h in right):
            swings.append(SwingPoint(
                price=pivot_high,
                timestamp=candles[i].timestamp,
                bar_index=i,
                is_high=True,
                strength=lookback,
            ))

    return swings


def find_swing_lows(
    candles: list[OHLCV],
    lookback: int = SMC_SWING_LOOKBACK,
) -> list[SwingPoint]:
    """
    Identify swing lows in candle data.

    A swing low is a bar whose low is strictly less than the
    `lookback` bars on each side.
    """
    swings: list[SwingPoint] = []
    n = len(candles)

    for i in range(lookback, n - lookback):
        pivot_low = candles[i].low
        left = [candles[i - j].low for j in range(1, lookback + 1)]
        right = [candles[i + j].low for j in range(1, lookback + 1)]

        if all(pivot_low < lo for lo in left) and all(pivot_low <= lo for lo in right):
            swings.append(SwingPoint(
                price=pivot_low,
                timestamp=candles[i].timestamp,
                bar_index=i,
                is_high=False,
                strength=lookback,
            ))

    return swings


def detect_market_structure(
    candles: list[OHLCV],
    lookback: int = SMC_SWING_LOOKBACK,
) -> MarketStructureState:
    """
    Analyse market structure from a candle series.

    Classifies structure as bullish (HH/HL), bearish (LH/LL), or ranging.
    Requires at least 2 swing highs and 2 swing lows for classification.
    """
    state = MarketStructureState()
    state.swing_highs = find_swing_highs(candles, lookback)
    state.swing_lows = find_swing_lows(candles, lookback)

    if len(state.swing_highs) < 2 or len(state.swing_lows) < 2:
        state.structure = MarketStructure.UNKNOWN
        return state

    # Compare last two swing highs and last two swing lows
    hh = state.swing_highs[-1].price > state.swing_highs[-2].price  # Higher high
    hl = state.swing_lows[-1].price > state.swing_lows[-2].price    # Higher low
    lh = state.swing_highs[-1].price < state.swing_highs[-2].price  # Lower high
    ll = state.swing_lows[-1].price < state.swing_lows[-2].price    # Lower low

    if hh and hl:
        state.structure = MarketStructure.BULLISH
    elif lh and ll:
        state.structure = MarketStructure.BEARISH
    else:
        state.structure = MarketStructure.RANGING

    logger.debug(
        "Market structure: %s (HH=%s HL=%s LH=%s LL=%s)",
        state.structure, hh, hl, lh, ll,
    )
    return state


def is_higher_timeframe_bullish(
    htf_candles: list[OHLCV],
    lookback: int = SMC_SWING_LOOKBACK,
) -> bool:
    state = detect_market_structure(htf_candles, lookback)
    return state.structure == MarketStructure.BULLISH


def is_higher_timeframe_bearish(
    htf_candles: list[OHLCV],
    lookback: int = SMC_SWING_LOOKBACK,
) -> bool:
    state = detect_market_structure(htf_candles, lookback)
    return state.structure == MarketStructure.BEARISH
