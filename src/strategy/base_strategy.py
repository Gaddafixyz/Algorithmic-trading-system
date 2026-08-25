"""
Abstract base strategy with lifecycle hooks and warm-up management.

All concrete strategies extend this class and implement on_candle().
The base class handles:
- Indicator warm-up state tracking
- Multi-timeframe data access
- Position/signal tracking per symbol
- Structured logging
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.core.enums import Timeframe
from src.core.interfaces import ILifecycle, IStrategy
from src.core.models import OHLCV
from src.domain.position import Position
from src.domain.signal import Signal

logger = logging.getLogger(__name__)


class BaseStrategy(IStrategy, ILifecycle):
    """
    Base class for all trading strategies.

    Subclasses implement:
      - strategy_id (property)
      - symbols (property)
      - timeframes (property)
      - on_candle() — core analysis logic
    """

    def __init__(
        self,
        min_candles: int = 100,
        max_open_signals: int = 3,
    ) -> None:
        self._min_candles = min_candles
        self._max_open_signals = max_open_signals
        self._initialized: dict[tuple[str, str], bool] = {}
        self._candle_counts: dict[tuple[str, str], int] = {}
        self._active_signals: dict[str, Signal] = {}
        self._positions: dict[str, Position] = {}
        self._running = False

    # ─── IStrategy interface ──────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Called once at startup. Override to load external state."""
        self._running = True
        logger.info("Strategy '%s' initialized for symbols: %s", self.strategy_id, self.symbols)

    async def on_tick(self, tick: Any) -> None:
        """Default tick handler — override for tick-level logic."""

    async def on_position_closed(self, position: Position) -> None:
        """Called when a managed position closes."""
        self._positions.pop(position.symbol, None)
        # Release the active-signal slot immediately rather than relying on
        # it to self-heal via Signal.is_expired()'s max_age_seconds fallback
        # (default 3600s) — otherwise a symbol whose trade already closed
        # stays blocked from new signals for up to an hour for no reason.
        self.clear_signal(position.symbol)
        logger.info(
            "Strategy '%s': position closed for %s PnL=%s",
            self.strategy_id, position.symbol, position.net_realized_pnl,
        )

    # ─── Warm-up helpers ──────────────────────────────────────────────────────

    def is_warmed_up(self, symbol: str, timeframe: Timeframe) -> bool:
        """True if enough candles have been processed for this symbol/timeframe."""
        key = (symbol, timeframe.value)
        return self._candle_counts.get(key, 0) >= self._min_candles

    def record_candle(self, symbol: str, timeframe: Timeframe) -> None:
        """Increment the candle counter for warm-up tracking."""
        key = (symbol, timeframe.value)
        self._candle_counts[key] = self._candle_counts.get(key, 0) + 1

    # ─── Signal management ────────────────────────────────────────────────────

    def has_active_signal(self, symbol: str) -> bool:
        sig = self._active_signals.get(symbol)
        return sig is not None and not sig.is_expired()

    def register_signal(self, symbol: str, signal: Signal) -> None:
        self._active_signals[symbol] = signal

    def clear_signal(self, symbol: str) -> None:
        self._active_signals.pop(symbol, None)

    def can_generate_signal(self, symbol: str) -> bool:
        """True if we can generate a new signal (not already holding one)."""
        if self.has_active_signal(symbol):
            return False
        return len(self._active_signals) < self._max_open_signals

    # ─── Position tracking ────────────────────────────────────────────────────

    def has_position(self, symbol: str) -> bool:
        pos = self._positions.get(symbol)
        return pos is not None and pos.is_open

    def register_position(self, position: Position) -> None:
        self._positions[position.symbol] = position

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        await self.initialize()

    async def stop(self) -> None:
        self._running = False
        logger.info("Strategy '%s' stopped", self.strategy_id)

    # ─── To be implemented ────────────────────────────────────────────────────

    @property
    @abstractmethod
    def strategy_id(self) -> str: ...

    @property
    @abstractmethod
    def symbols(self) -> list[str]: ...

    @property
    @abstractmethod
    def timeframes(self) -> list[Timeframe]: ...

    @abstractmethod
    async def on_candle(
        self,
        symbol: str,
        timeframe: Timeframe,
        candles: list[OHLCV],
    ) -> Signal | None: ...
