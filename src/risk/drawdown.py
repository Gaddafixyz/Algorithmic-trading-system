"""Drawdown tracker with peak equity tracking."""

from __future__ import annotations

from typing import Any


class DrawdownTracker:
    """
    Tracks equity peak and current drawdown.

    Updated on every equity change. Reports both current and
    maximum (historical worst) drawdown.
    """

    def __init__(self) -> None:
        self._peak: float = 0.0
        self._current: float = 0.0
        self._max_drawdown: float = 0.0
        self._history: list[tuple[float, float]] = []  # (equity, drawdown_pct)

    def update(self, equity: float) -> None:
        """Update with the latest equity value."""
        self._current = equity
        if equity > self._peak:
            self._peak = equity

        if self._peak > 0:
            dd = (self._peak - equity) / self._peak * 100.0
            if dd > self._max_drawdown:
                self._max_drawdown = dd
            self._history.append((equity, dd))
            if len(self._history) > 10_000:
                self._history = self._history[-5_000:]

    @property
    def current_drawdown_pct(self) -> float:
        if self._peak <= 0:
            return 0.0
        return (self._peak - self._current) / self._peak * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        return self._max_drawdown

    @property
    def peak_equity(self) -> float:
        return self._peak

    @property
    def recovery_factor(self) -> float:
        """Net profit / max drawdown (higher is better)."""
        if self._max_drawdown == 0:
            return 0.0
        net_profit = self._current - (self._history[0][0] if self._history else self._current)
        return net_profit / (self._max_drawdown / 100 * self._peak) if self._peak > 0 else 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "current_equity": self._current,
            "peak_equity": self._peak,
            "current_drawdown_pct": round(self.current_drawdown_pct, 4),
            "max_drawdown_pct": round(self._max_drawdown, 4),
        }
