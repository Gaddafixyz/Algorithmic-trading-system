"""Portfolio exposure, concentration risk, and multi-symbol correlation tracking."""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal

import numpy as np

from src.domain.portfolio import Portfolio
from src.indicators.statistics import pearson_correlation
from src.utils.cache import TTLCache

logger = logging.getLogger(__name__)

# Single-slot cache key for CorrelationTracker's computed matrix. Only one
# matrix is ever cached per tracker instance, so a fixed key is sufficient.
_MATRIX_CACHE_KEY = "matrix"


class ExposureTracker:
    """Tracks gross and net portfolio exposure."""

    def __init__(self, max_gross_exposure_pct: float = 200.0) -> None:
        self._max_gross = max_gross_exposure_pct

    def gross_exposure_pct(self, portfolio: Portfolio, equity: Decimal) -> float:
        if equity <= Decimal("0"):
            return 0.0
        return float(portfolio.gross_exposure / equity * Decimal("100"))

    def net_exposure_pct(self, portfolio: Portfolio, equity: Decimal) -> float:
        if equity <= Decimal("0"):
            return 0.0
        return float(portfolio.net_exposure / equity * Decimal("100"))

    def is_within_limits(self, portfolio: Portfolio, equity: Decimal) -> bool:
        return self.gross_exposure_pct(portfolio, equity) <= self._max_gross

    def symbol_concentration(self, portfolio: Portfolio, symbol: str, equity: Decimal) -> float:
        """Return a single symbol's notional as % of equity."""
        positions = portfolio.get_positions_for_symbol(symbol)
        notional = sum(p.notional_value for p in positions)
        if equity <= Decimal("0"):
            return 0.0
        return float(notional / equity * Decimal("100"))


class CorrelationTracker:
    """
    Tracks rolling price-return correlations across multiple symbols.

    Uses a fixed-length sliding window of close-to-close log returns.
    The correlation matrix is expensive to compute (O(n²) pairwise Pearson
    correlations) and was previously recomputed on *every* call to
    ``matrix()`` regardless of whether new data had arrived — a `_dirty`
    flag was set on `update()` and cleared at the end of `matrix()` but
    never actually checked, so the "recomputed lazily" claim this docstring
    used to make was false. `diversification_ratio()` and
    `highest_correlated_pair()` each call `matrix()` independently, so a
    single portfolio-risk report (or dashboard poll) could recompute the
    full matrix 2-3x back to back with unchanged data, on top of
    `RiskManager.calculate_position_size()` calling it again per signal.

    The matrix is now cached via `TTLCache` (single slot, keyed by
    `_MATRIX_CACHE_KEY`): `update()` explicitly invalidates the cache
    entry the moment new data arrives (so a read right after an `update()`
    is never stale), and a short TTL is kept as a safety net in case the
    cache is ever read without going through `update()` first.

    Usage::

        tracker = CorrelationTracker(symbols=["BTCUSDT", "ETHUSDT"], window=60)
        for candle in btc_candles:
            tracker.update("BTCUSDT", float(candle.close))
        for candle in eth_candles:
            tracker.update("ETHUSDT", float(candle.close))

        corr = tracker.matrix()          # dict[str, dict[str, float]]
        print(corr["BTCUSDT"]["ETHUSDT"])  # e.g. 0.87
    """

    def __init__(self, symbols: list[str], window: int = 60) -> None:
        """
        Args:
            symbols: Trading pairs to track.
            window: Number of *returns* to keep (window+1 prices stored).
            min_periods: Minimum data points before a valid correlation
                is reported; pairs with fewer observations return NaN.
        """
        if window < 2:
            raise ValueError(f"window must be >= 2, got {window}")
        self._symbols = list(symbols)
        self._window = window
        # Store raw close prices; returns are derived on demand
        self._prices: dict[str, deque[float]] = {s: deque(maxlen=window + 1) for s in symbols}
        # TTL is a safety net only — update() invalidates this explicitly
        # on every new data point, so 5s of staleness would only ever be
        # observed if update() were somehow bypassed.
        self._cache: TTLCache[dict[str, dict[str, float]]] = TTLCache(default_ttl=5.0, max_size=1)

    def update(self, symbol: str, close_price: float) -> None:
        """
        Ingest a new close price for *symbol*.

        Silently ignores symbols that were not declared at construction time,
        so callers don't need to guard against unknown pairs.
        """
        if symbol not in self._prices:
            logger.debug("CorrelationTracker: ignoring unknown symbol %s", symbol)
            return
        self._prices[symbol].append(close_price)
        self._cache.delete(_MATRIX_CACHE_KEY)

    def returns(self, symbol: str) -> np.ndarray:
        """
        Return log-return series for *symbol* (length = min(n-1, window)).

        Guards against non-positive prices (corrupted market data, feed
        errors, or a symbol that briefly reports 0/negative): ``log()`` of
        such values would silently produce NaN/-inf and poison every
        downstream correlation, so any non-positive price is logged and
        excluded from the series rather than passed to ``np.log``.
        """
        raw_prices = list(self._prices[symbol])
        prices = np.array(raw_prices, dtype=float)
        if len(prices) < 2:
            return np.array([], dtype=float)

        valid_mask = prices > 0.0
        if not valid_mask.all():
            n_invalid = int((~valid_mask).sum())
            logger.warning(
                "CorrelationTracker: dropping %d non-positive price(s) for %s "
                "before log-return calculation (corrupted market data?)",
                n_invalid,
                symbol,
            )
            prices = prices[valid_mask]
            if len(prices) < 2:
                return np.array([], dtype=float)

        return np.diff(np.log(prices))

    def matrix(self) -> dict[str, dict[str, float]]:
        """
        Compute and return the pairwise Pearson correlation matrix.

        Each value is a float in [-1, 1] or NaN when insufficient data.
        The matrix is symmetric: ``corr[A][B] == corr[B][A]``.
        """
        cached = self._cache.get(_MATRIX_CACHE_KEY)
        if cached is not None:
            return cached

        # Collect aligned return series (trim to common length)
        series: dict[str, np.ndarray] = {sym: self.returns(sym) for sym in self._symbols}
        min_len = min((len(s) for s in series.values()), default=0)

        result: dict[str, dict[str, float]] = {}
        for sym_a in self._symbols:
            result[sym_a] = {}
            for sym_b in self._symbols:
                if sym_a == sym_b:
                    result[sym_a][sym_b] = 1.0
                    continue
                a = series[sym_a][-min_len:] if min_len else series[sym_a]
                b = series[sym_b][-min_len:] if min_len else series[sym_b]
                if len(a) < 2 or len(b) < 2:
                    result[sym_a][sym_b] = float("nan")
                    continue
                if np.std(a) == 0.0 or np.std(b) == 0.0:
                    # A zero-variance return series (e.g. a stale feed
                    # reporting a constant price) makes correlation
                    # mathematically undefined. scipy's pearsonr (used by
                    # pearson_correlation below) would compute this anyway
                    # via an internal divide-by-zero, which resolves to
                    # NaN but not before raising a warning -- surfacing
                    # here explicitly avoids that and is consistent with
                    # the existing "insufficient data -> NaN" contract.
                    result[sym_a][sym_b] = float("nan")
                    continue
                # Previously reimplemented via np.corrcoef here instead of
                # reusing indicators/statistics.py's pearson_correlation —
                # the same divergent-implementation-of-the-same-math
                # pattern that once caused analytics/performance.py's
                # inline Sharpe/Sortino to disagree with sharpe.py over
                # ddof. Both compute the identical statistic; this just
                # removes the duplicate.
                result[sym_a][sym_b] = pearson_correlation(a, b)

        self._cache.set(_MATRIX_CACHE_KEY, result)
        return result

    def highest_correlated_pair(self) -> tuple[str, str, float] | None:
        """
        Return the most correlated symbol pair (excluding self-correlation).

        Returns ``(symbol_a, symbol_b, correlation)`` or ``None`` if fewer
        than two symbols have enough data.
        """
        corr = self.matrix()
        best: tuple[str, str, float] | None = None
        seen: set[frozenset[str]] = set()
        for a in self._symbols:
            for b in self._symbols:
                if a == b:
                    continue
                key = frozenset({a, b})
                if key in seen:
                    continue
                seen.add(key)
                val = corr[a][b]
                if np.isnan(val):
                    continue
                if best is None or abs(val) > abs(best[2]):
                    best = (a, b, val)
        return best

    def diversification_ratio(self) -> float:
        """
        Simple diversification score: 1 − mean(|off-diagonal correlations|).

        Returns a value in [0, 1] where 1 = perfectly uncorrelated portfolio.
        Returns NaN when insufficient data for any pair.
        """
        corr = self.matrix()
        off_diagonal: list[float] = []
        for a in self._symbols:
            for b in self._symbols:
                if a != b:
                    val = corr[a][b]
                    if not np.isnan(val):
                        off_diagonal.append(abs(val))
        if not off_diagonal:
            return float("nan")
        return 1.0 - float(np.mean(off_diagonal))

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    @property
    def window(self) -> int:
        return self._window

    def __repr__(self) -> str:
        lengths = {s: len(self._prices[s]) for s in self._symbols}
        return f"CorrelationTracker(symbols={self._symbols}, window={self._window}, obs={lengths})"
