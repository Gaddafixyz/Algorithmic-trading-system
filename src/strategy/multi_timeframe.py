"""
Multi-timeframe analysis coordinator.

Provides a structured way to combine analysis from multiple timeframes:
- Higher timeframe (HTF) for bias
- Execution timeframe (ETF) for entry
- Lower timeframe (LTF) for precision entry and confirmation
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.core.enums import MarketStructure, Timeframe
from src.core.models import OHLCV
from src.smart_money.market_structure import (
    MarketStructureState,
    detect_market_structure,
)

logger = logging.getLogger(__name__)


# Standard timeframe relationships
HTF_MAP: dict[Timeframe, Timeframe] = {
    Timeframe.M5: Timeframe.H1,
    Timeframe.M15: Timeframe.H4,
    Timeframe.M30: Timeframe.H4,
    Timeframe.H1: Timeframe.D1,
    Timeframe.H4: Timeframe.D1,
    Timeframe.D1: Timeframe.W1,
}

LTF_MAP: dict[Timeframe, Timeframe] = {
    Timeframe.H4: Timeframe.H1,
    Timeframe.H1: Timeframe.M15,
    Timeframe.M15: Timeframe.M5,
    Timeframe.M5: Timeframe.M1,
}


class MTFContext:
    """
    Multi-timeframe context for signal qualification.

    Stores structure and analysis for multiple timeframes and
    provides alignment scoring.
    """

    def __init__(self) -> None:
        self._structures: dict[str, MarketStructureState] = {}
        self._candles: dict[str, list[OHLCV]] = {}

    def update(
        self,
        timeframe: Timeframe,
        candles: list[OHLCV],
        swing_lookback: int = 5,
    ) -> MarketStructureState:
        """Update analysis for a timeframe and return the structure."""
        key = timeframe.value
        self._candles[key] = candles
        structure = detect_market_structure(candles, swing_lookback)
        self._structures[key] = structure
        return structure

    def get_structure(self, timeframe: Timeframe) -> MarketStructureState | None:
        return self._structures.get(timeframe.value)

    def get_bias(self, timeframe: Timeframe) -> str:
        """Return 'bullish', 'bearish', or 'neutral' for a timeframe."""
        structure = self._structures.get(timeframe.value)
        if structure is None:
            return "neutral"
        if structure.structure == MarketStructure.BULLISH:
            return "bullish"
        if structure.structure == MarketStructure.BEARISH:
            return "bearish"
        return "neutral"

    def alignment_score(
        self,
        execution_tf: Timeframe,
        direction: str,
        htf: Timeframe | None = None,
        ltf: Timeframe | None = None,
    ) -> float:
        """
        Score 0–1 based on how many *available* timeframes align with
        `direction`.

        Checks execution TF, HTF, and LTF. `htf`/`ltf` default to the
        `HTF_MAP`/`LTF_MAP` lookup tables but should be passed explicitly
        whenever the caller has a separately-configured HTF (e.g.
        `SMCSignalEngine.htf_timeframe`), since that can diverge from the
        fixed default table — passing it explicitly avoids scoring against
        a timeframe that was never actually `update()`d.
        """
        tfs_to_check = [execution_tf]

        htf = htf if htf is not None else HTF_MAP.get(execution_tf)
        if htf:
            tfs_to_check.append(htf)

        ltf = ltf if ltf is not None else LTF_MAP.get(execution_tf)
        if ltf:
            tfs_to_check.append(ltf)

        # Denominator is the number of timeframes we actually have data
        # for, not the theoretical full set — otherwise alignment can
        # never reach 1.0 while any tier (e.g. LTF) isn't being fed,
        # silently understating conviction on every single call.
        available_tfs = [tf for tf in tfs_to_check if tf.value in self._structures]
        if not available_tfs:
            return 0.5

        aligned = sum(1 for tf in available_tfs if self.get_bias(tf) == direction)
        return aligned / len(available_tfs)

    def is_htf_aligned(
        self,
        execution_tf: Timeframe,
        direction: str,
        htf: Timeframe | None = None,
    ) -> bool:
        """
        True if the higher timeframe bias matches the trade direction.

        `htf` should be passed explicitly by callers with a configured
        HTF (see `alignment_score` docstring) — otherwise this falls back
        to the fixed `HTF_MAP` default, which may not match what was
        actually `update()`d.

        Returns True (allow the trade) when no HTF is defined *or* no HTF
        data has been fed yet — this is a confirmation filter layered on
        top of confluence scoring, not a substitute for having HTF data;
        it must not reject every signal just because HTF candles haven't
        arrived yet.
        """
        htf = htf if htf is not None else HTF_MAP.get(execution_tf)
        if htf is None or htf.value not in self._structures:
            return True
        return self.get_bias(htf) == direction

    def context_summary(self) -> dict[str, str]:
        """Return human-readable structure summary for all timeframes."""
        return {
            tf: self.get_bias(Timeframe(tf))
            for tf in self._structures
        }
