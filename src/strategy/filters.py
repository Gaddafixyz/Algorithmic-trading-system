"""
Strategy signal filters.

Filters block signal generation under adverse conditions:
- Weekend / low-liquidity periods
- Excessive spread
- Extreme volatility
- Correlated positions already open

`apply_all_filters` always applies the score/RR/ATR checks; the spread,
weekend, and correlation checks only run when the caller supplies the
data they need (best_bid/best_ask, check_weekend=True, open_symbols),
so existing callers passing just a signal keep their current behavior.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.core.enums import SignalDirection
from src.core.models import OHLCV
from src.domain.signal import Signal
from src.indicators.rsi import is_overbought, is_oversold
from src.smart_money.killzones import should_trade_now

logger = logging.getLogger(__name__)


def filter_by_spread(
    signal: Signal,
    best_bid: float,
    best_ask: float,
    max_spread_pct: float = 0.05,
) -> bool:
    """Return True if spread is acceptable (below max_spread_pct %)."""
    if best_ask == 0 or best_bid == 0:
        return True  # No spread data — allow
    spread_pct = (best_ask - best_bid) / best_bid * 100
    if spread_pct > max_spread_pct:
        logger.debug(
            "Signal filtered: spread %.4f%% > max %.4f%%",
            spread_pct, max_spread_pct,
        )
        return False
    return True


def filter_by_minimum_atr(
    signal: Signal,
    atr: float,
    min_atr_pct: float = 0.1,
) -> bool:
    """Return True if ATR is sufficient (market is not dead)."""
    if atr == 0:
        return True
    entry = float(signal.entry_price)
    atr_pct = atr / entry * 100
    if atr_pct < min_atr_pct:
        logger.debug(
            "Signal filtered: ATR %% %.4f < min %.4f%%",
            atr_pct, min_atr_pct,
        )
        return False
    return True


def filter_by_rsi_exhaustion(
    signal: Signal,
    rsi_value: float | None,
    overbought: float = 80.0,
    oversold: float = 20.0,
) -> bool:
    """
    Return True unless RSI shows the move already exhausted in the
    signal's own direction: a LONG entering into RSI overbought, or a
    SHORT entering into RSI oversold. Doesn't block the opposite case
    (e.g. a LONG at oversold RSI is a reversal setup, not exhaustion) —
    this is specifically a "don't chase an already-extended move" guard,
    not a general overbought/oversold filter.

    rsi_value=None (insufficient candle history — see
    smart_money/confluence.py's InsufficientDataError guard) always
    passes, mirroring filter_by_minimum_atr's atr=0 no-op.
    """
    if rsi_value is None:
        return True
    if signal.direction == SignalDirection.LONG and is_overbought(rsi_value, overbought):
        logger.debug(
            "Signal filtered: RSI %.1f >= overbought %.1f for LONG %s",
            rsi_value, overbought, signal.symbol,
        )
        return False
    if signal.direction == SignalDirection.SHORT and is_oversold(rsi_value, oversold):
        logger.debug(
            "Signal filtered: RSI %.1f <= oversold %.1f for SHORT %s",
            rsi_value, oversold, signal.symbol,
        )
        return False
    return True


def filter_by_macd_trend(
    signal: Signal,
    macd_histogram: float | None,
) -> bool:
    """
    Return True unless the MACD histogram actively disagrees with the
    signal's direction (negative histogram on a LONG, positive on a
    SHORT) — a coarse trend-confirmation gate, not a standalone signal
    generator. A histogram of exactly 0.0 (or None, on insufficient
    candle history) passes, same no-op-on-missing-data convention as
    filter_by_minimum_atr/filter_by_rsi_exhaustion.
    """
    if macd_histogram is None:
        return True
    if signal.direction == SignalDirection.LONG and macd_histogram < 0:
        logger.debug(
            "Signal filtered: MACD histogram %.6f < 0 for LONG %s",
            macd_histogram, signal.symbol,
        )
        return False
    if signal.direction == SignalDirection.SHORT and macd_histogram > 0:
        logger.debug(
            "Signal filtered: MACD histogram %.6f > 0 for SHORT %s",
            macd_histogram, signal.symbol,
        )
        return False
    return True


def filter_by_weekend() -> bool:
    """Return False on weekends (low liquidity in crypto still, but standard filter)."""
    now = datetime.now(tz=UTC)
    return now.weekday() < 5  # 5=Sat, 6=Sun


def filter_by_kill_zone(require_kill_zone: bool = True) -> bool:
    """
    Return True if the current UTC time is an appropriate ICT kill-zone
    window to enter a trade (see smart_money/killzones.py::should_trade_now).

    require_kill_zone=True restricts entries to the active London/New York
    open/close kill zones; False relaxes this to any session scoring >= 0.4
    (see should_trade_now). Same "evaluate now, no signal-specific inputs"
    shape as filter_by_weekend.
    """
    now = datetime.now(tz=UTC)
    return should_trade_now(now, require_kill_zone=require_kill_zone)


def filter_low_score(signal: Signal, min_score: float = 60.0) -> bool:
    """Return True if signal score meets the minimum threshold."""
    return signal.score >= min_score


def filter_low_rr(signal: Signal, min_rr: float = 1.5) -> bool:
    """Return True if risk/reward ratio is acceptable."""
    return float(signal.risk_reward_ratio) >= min_rr


def filter_by_correlated_positions(
    signal: Signal,
    open_symbols: list[str],
    correlation_groups: dict[str, list[str]] | None = None,
    max_correlated_positions: int = 2,
) -> bool:
    """
    Return True if taking this signal would not exceed the max number of
    concurrently-open positions within its correlation group.

    Args:
        signal: The candidate signal.
        open_symbols: Symbols with currently open positions (duplicates
            allowed if a symbol can have multiple open positions).
        correlation_groups: Maps a symbol to the list of symbols considered
            correlated with it (should include itself). If not provided,
            falls back to treating only exact-symbol duplicates as
            correlated (e.g. two BTCUSDT positions at once).
        max_correlated_positions: Max allowed open positions within the
            correlated group before new signals in that group are blocked.
    """
    if correlation_groups is None:
        correlated = {signal.symbol}
    else:
        correlated = set(correlation_groups.get(signal.symbol, [signal.symbol]))

    correlated_open_count = sum(1 for s in open_symbols if s in correlated)
    if correlated_open_count >= max_correlated_positions:
        logger.debug(
            "Signal filtered: %d correlated position(s) already open for %s (max %d)",
            correlated_open_count, signal.symbol, max_correlated_positions,
        )
        return False
    return True


def apply_all_filters(
    signal: Signal,
    *,
    min_score: float = 60.0,
    min_rr: float = 1.5,
    atr: float = 0.0,
    best_bid: float | None = None,
    best_ask: float | None = None,
    max_spread_pct: float = 0.05,
    check_weekend: bool = False,
    open_symbols: list[str] | None = None,
    correlation_groups: dict[str, list[str]] | None = None,
    max_correlated_positions: int = 2,
) -> tuple[bool, list[str]]:
    """
    Apply all standard signal filters.

    Spread, weekend, and correlated-position checks are opt-in (only run
    when their inputs are supplied) so this stays safe to call with just a
    signal and score/RR thresholds, as before.

    Returns (passed, list_of_rejection_reasons).
    """
    reasons: list[str] = []

    if not filter_low_score(signal, min_score):
        reasons.append(f"score {signal.score:.1f} < {min_score}")

    if not filter_low_rr(signal, min_rr):
        reasons.append(f"RR {signal.risk_reward_ratio:.2f} < {min_rr}")

    if atr > 0 and not filter_by_minimum_atr(signal, atr):
        reasons.append("ATR too low")

    if best_bid is not None and best_ask is not None:
        if not filter_by_spread(signal, best_bid, best_ask, max_spread_pct):
            reasons.append(f"spread > {max_spread_pct}%")

    if check_weekend and not filter_by_weekend():
        reasons.append("weekend low-liquidity period")

    if open_symbols is not None:
        if not filter_by_correlated_positions(
            signal, open_symbols, correlation_groups, max_correlated_positions
        ):
            reasons.append(f"max correlated positions ({max_correlated_positions}) reached")

    passed = len(reasons) == 0
    return passed, reasons
