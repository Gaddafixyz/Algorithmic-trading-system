"""
Rolling live trade-outcome tracker.

risk/position_sizing.py::kelly_position_size() needs a live win_rate/
avg_win/avg_loss estimate to size a new position. analytics/expectancy.py
and analytics/performance.py already compute those same statistics, but
only as a batch report over a closed ``list[Trade]`` pulled from
persistence — not something RiskManager can query synchronously on the
critical path of validating a new signal, and not incrementally updated
as trades close live.

This is the live, incrementally-updated equivalent: a fixed-size rolling
window of realized net P&L per closed position, fed by RiskManager's
existing PositionClosedEvent subscription (see risk_manager.py's
_on_position_closed — record_pnl() for the daily-loss breaker and this
tracker's record() now both run off the same event). win_rate/avg_win/
avg_loss are recomputed on read from whatever's currently in the window,
so this intentionally holds no derived state that could drift from the
raw outcomes.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal


class RollingTradeTracker:
    """
    Tracks the last `window` closed-trade net P&L values in memory.

    Not persisted across restarts — a freshly started process has an
    empty window until enough live trades close, which is exactly why
    kelly_position_size() callers must gate on has_min_sample() first and
    fall back to a non-Kelly sizing method until then (see
    RiskManager.calculate_position_size()).
    """

    def __init__(self, window: int = 30) -> None:
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window}")
        self._window = window
        self._outcomes: deque[Decimal] = deque(maxlen=window)

    def record(self, net_pnl: Decimal) -> None:
        """Record one closed trade's realized net P&L (after fees)."""
        self._outcomes.append(net_pnl)

    @property
    def sample_size(self) -> int:
        return len(self._outcomes)

    def has_min_sample(self, min_trades: int) -> bool:
        return self.sample_size >= min_trades

    @property
    def win_rate(self) -> float:
        """Fraction of tracked trades with net_pnl > 0. 0.0 on an empty window."""
        if not self._outcomes:
            return 0.0
        wins = sum(1 for p in self._outcomes if p > Decimal("0"))
        return wins / len(self._outcomes)

    @property
    def avg_win(self) -> float:
        """Average net P&L of winning trades in the window. 0.0 if there are none."""
        wins = [float(p) for p in self._outcomes if p > Decimal("0")]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        """Average magnitude (positive) of losing trades' net P&L. 0.0 if there are none."""
        losses = [float(-p) for p in self._outcomes if p < Decimal("0")]
        return sum(losses) / len(losses) if losses else 0.0
