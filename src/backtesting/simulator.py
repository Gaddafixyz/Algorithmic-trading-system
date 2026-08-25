"""
Simulated broker for backtesting.

Fills orders at next-bar open with slippage and commission applied.
Tracks equity, positions, and closed trades.

Enhancements over the naive fill model:
- Funding-rate accrual: applied to open positions at 8-hour intervals,
  matching Binance Futures settlement cadence.
- Partial-fill simulation: large orders relative to bar volume receive
  only a fraction of the requested quantity, with the remainder queued
  for the following bars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.backtesting.commission import CommissionModel, PercentageCommission
from src.backtesting.slippage import FixedSlippage, SlippageModel
from src.core.enums import PositionSide, SignalDirection
from src.core.models import OHLCV
from src.domain.position import Position
from src.domain.signal import Signal
from src.domain.trade import Trade
from src.risk.position_sizing import calculate_position_size

logger = logging.getLogger(__name__)

# Binance settles funding every 8 hours (00:00, 08:00, 16:00 UTC).
_FUNDING_INTERVAL_HOURS: int = 8


@dataclass
class FundingRate:
    """A single historical funding-rate record."""

    timestamp: datetime  # settlement time (UTC)
    symbol: str
    rate: Decimal  # e.g. Decimal("0.0001") = 0.01 %


@dataclass
class PartialFillModel:
    """
    Controls how large orders are filled relative to bar volume.

    When ``volume_participation`` is set, an order is filled only up to
    ``volume_participation * bar_volume`` per bar.  The remainder is
    requeued as a ``PendingOrder`` for the next bar, preventing unrealistic
    instantaneous fills of orders that dwarf the available liquidity.

    Set ``volume_participation = Decimal("1")`` (or ``None``) to disable
    partial fills and always fill in full — the legacy behaviour.
    """

    volume_participation: Decimal = Decimal("0.1")  # 10 % of bar volume
    enabled: bool = True


@dataclass
class PendingOrder:
    """An order waiting to be filled at the next bar's open."""

    signal: Signal
    quantity: Decimal  # remaining quantity to fill
    is_entry: bool = True
    is_partial_remainder: bool = False  # True when re-queued after a partial fill


class BacktestBroker:
    """
    Simulated broker for backtesting.

    Orders placed on bar N are filled at bar N+1's open.
    This is a conservative assumption that avoids fill-at-signal-bar bias.
    """

    def __init__(
        self,
        initial_equity: Decimal,
        commission: CommissionModel | None = None,
        slippage: SlippageModel | None = None,
        risk_pct: Decimal = Decimal("1.0"),
        max_positions: int = 5,
        funding_rates: list[FundingRate] | None = None,
        partial_fill: PartialFillModel | None = None,
        sizing_fee_rate: Decimal = Decimal("0.0004"),
    ) -> None:
        self._equity = initial_equity
        self._initial_equity = initial_equity
        self._commission = commission or PercentageCommission()
        self._slippage = slippage or FixedSlippage()
        self._risk_pct = risk_pct
        self._max_positions = max_positions
        # Fee rate assumed by the fixed-fractional sizing formula itself
        # (see place_signal below) — separate from self._commission,
        # which charges the *actual* commission on each fill. Defaults to
        # the same 0.0004 (Binance taker) both PercentageCommission and
        # risk/position_sizing.py::calculate_position_size() already
        # default to independently, so a caller using the defaults for
        # both sees no behavioural change from before this parameter
        # existed.
        self._sizing_fee_rate = sizing_fee_rate

        # Sort funding rates chronologically for efficient lookup
        self._funding_rates: list[FundingRate] = sorted(
            funding_rates or [], key=lambda r: r.timestamp
        )
        self._funding_index: int = 0  # pointer into _funding_rates
        self._last_bar_ts: datetime | None = None

        self._partial_fill = partial_fill or PartialFillModel()

        self._positions: dict[str, Position] = {}
        self._pending: list[PendingOrder] = []
        self._closed_trades: list[Trade] = []
        self._bar_count = 0

    def place_signal(self, signal: Signal, current_candle: OHLCV) -> None:
        """Queue an entry order from a strategy signal."""
        if signal.symbol in self._positions:
            logger.debug("Already in position for %s", signal.symbol)
            return
        if len(self._positions) >= self._max_positions:
            logger.debug("Max positions reached — skipping signal for %s", signal.symbol)
            return

        # Size by fixed fractional risk — delegated to
        # risk/position_sizing.py::calculate_position_size(), the exact
        # function RiskManager.calculate_position_size() uses live, so a
        # backtest sizes positions identically to how the live system
        # would size the same signal. This used to reimplement the
        # formula inline as `equity * risk_pct/100 / stop_distance`,
        # which silently omitted the fee adjustment the live formula
        # applies (effective_risk_per_unit = stop_distance +
        # 2*entry_price*fee_rate) — the same "two implementations
        # quietly drift apart" bug already found and fixed once in this
        # codebase for Sharpe/Sortino's std ddof convention
        # (analytics/performance.py). The drift here was small (a
        # fraction of a percent of quantity at typical stop distances)
        # but meant backtests were systematically sizing very slightly
        # larger — and therefore risking very slightly more per trade —
        # than live trading would for an identical signal.
        stop_dist = abs(signal.entry_price - signal.stop_loss)
        if stop_dist <= Decimal("0"):
            return
        quantity = calculate_position_size(
            equity=self._equity,
            risk_pct=self._risk_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            fee_rate=self._sizing_fee_rate,
        )

        if quantity <= Decimal("0"):
            return

        self._pending.append(PendingOrder(signal=signal, quantity=quantity))
        logger.debug("Queued order: %s qty=%s", signal.symbol, quantity)

    def process_pending_orders(self, candle: OHLCV) -> None:
        """
        Process the current bar:
        1. Apply any funding-rate settlements that fall between the previous
           bar's timestamp and this bar's timestamp.
        2. Fill pending orders (with partial-fill capping if enabled).
        3. Check stop-loss / take-profit exits for all open positions.
        """
        self._bar_count += 1

        # ── 1. Funding-rate accrual ───────────────────────────────────────────
        self._apply_funding_rates(candle)
        self._last_bar_ts = candle.timestamp

        # ── 2. Fill pending orders ────────────────────────────────────────────
        to_process = list(self._pending)
        self._pending.clear()

        newly_filled_symbols: set[str] = set()

        for order in to_process:
            signal = order.signal
            already_open = signal.symbol in self._positions

            # Non-remainder orders skip if a position already exists
            if already_open and not order.is_partial_remainder:
                continue

            is_long = signal.direction == SignalDirection.LONG
            fill_price = self._slippage.apply(candle.open, is_long, candle)

            # ── Partial fill: cap quantity to a fraction of bar volume ────────
            fill_qty = order.quantity
            if (
                self._partial_fill.enabled
                and candle.volume is not None
                and candle.volume > Decimal("0")
            ):
                max_qty = candle.volume * self._partial_fill.volume_participation
                if fill_qty > max_qty:
                    fill_qty = max_qty
                    remaining = order.quantity - fill_qty
                    # Re-queue the unfilled remainder (allowed to add to open pos)
                    self._pending.append(
                        PendingOrder(
                            signal=signal,
                            quantity=remaining,
                            is_partial_remainder=True,
                        )
                    )
                    logger.debug(
                        "Partial fill: %s filled=%s remaining=%s",
                        signal.symbol,
                        fill_qty,
                        remaining,
                    )

            if fill_qty <= Decimal("0"):
                self._pending.append(order)
                continue

            commission = self._commission.calculate(fill_qty, fill_price)
            cost = fill_qty * fill_price + commission

            if cost > self._equity:
                logger.debug(
                    "Insufficient equity for %s: need %s have %s",
                    signal.symbol,
                    cost,
                    self._equity,
                )
                continue

            # Deduct entry commission immediately; tracked in pos.total_fees for
            # reporting.  _close_position must NOT re-subtract pos.total_fees.
            self._equity -= commission

            if already_open and order.is_partial_remainder:
                # Add fill to existing position (volume-weighted average entry)
                pos = self._positions[signal.symbol]
                prev_notional = pos.entry_price * pos.quantity
                new_notional = fill_price * fill_qty
                new_qty = pos.quantity + fill_qty
                avg_entry = (prev_notional + new_notional) / new_qty if new_qty else pos.entry_price
                object.__setattr__(pos, "quantity", new_qty)
                object.__setattr__(pos, "entry_quantity", pos.entry_quantity + fill_qty)
                object.__setattr__(pos, "entry_price", avg_entry)
                object.__setattr__(pos, "total_fees", pos.total_fees + commission)
                object.__setattr__(pos, "mark_price", fill_price)
                logger.debug(
                    "Partial fill added to %s pos: +qty=%s avg_entry=%s",
                    signal.symbol,
                    fill_qty,
                    avg_entry,
                )
            else:
                # New position
                if len(self._positions) >= self._max_positions:
                    # Refund the already-deducted commission and skip
                    self._equity += commission
                    logger.debug("Max positions reached — skipping %s", signal.symbol)
                    continue
                pos_side = PositionSide.LONG if is_long else PositionSide.SHORT
                pos = Position(
                    symbol=signal.symbol,
                    side=pos_side,
                    quantity=fill_qty,
                    entry_quantity=fill_qty,
                    entry_price=fill_price,
                    mark_price=fill_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    # total_fees tracks the entry commission already deducted
                    total_fees=commission,
                    # Threaded through so the resulting Trade can be traced
                    # back to the Signal that generated it (see
                    # _close_position below) — the live path already does
                    # this in execution_engine.py; the backtest path was
                    # silently dropping both fields, which meant no
                    # backtest Trade could ever be joined back to its
                    # originating Signal (e.g. for ML training-label
                    # generation or per-signal-component win-rate analysis).
                    strategy_id=signal.strategy_id,
                    signal_id=signal.signal_id,
                )
                self._positions[signal.symbol] = pos
                newly_filled_symbols.add(signal.symbol)
                logger.debug("Filled ENTRY: %s qty=%s @%s", signal.symbol, fill_qty, fill_price)

        # ── 3. Check stop/TP exits ────────────────────────────────────────────
        # Exclude symbols that just filled on this bar to avoid same-bar
        # entry+exit look-ahead artefacts.
        self._check_exits(candle, exclude_symbols=newly_filled_symbols)

    def _apply_funding_rates(self, candle: OHLCV) -> None:
        """
        Accrue funding-rate payments for all open positions.

        Iterates through the sorted funding-rate list and applies any
        settlement timestamps that fall within the half-open interval
        ``(last_bar_timestamp, candle.timestamp]``.  Using the bar's open
        timestamp as the settlement boundary keeps accrual causally correct.

        Funding is charged against ``position.funding_pnl`` and deducted
        from ``self._equity``.  A positive rate benefits shorts and costs
        longs; a negative rate does the opposite — matching Binance's
        convention.
        """
        if not self._funding_rates or not self._positions:
            return

        prev_ts = self._last_bar_ts
        cur_ts = candle.timestamp

        while self._funding_index < len(self._funding_rates):
            fr = self._funding_rates[self._funding_index]
            # Only apply settlements that occurred before this bar's timestamp
            if fr.timestamp > cur_ts:
                break
            # Skip settlements that were already processed (≤ previous bar)
            if prev_ts is not None and fr.timestamp <= prev_ts:
                self._funding_index += 1
                continue

            # Apply funding to each matching-symbol open position
            for symbol, pos in self._positions.items():
                if symbol.upper() != fr.symbol.upper():
                    continue
                # funding_payment = rate * notional (positive = longs pay shorts)
                notional = pos.quantity * pos.mark_price
                payment = fr.rate * notional
                if pos.is_long:
                    funding_pnl_delta = -payment  # long pays
                else:
                    funding_pnl_delta = payment  # short receives

                # Mutate mutable model field directly
                object.__setattr__(
                    pos,
                    "funding_pnl",
                    pos.funding_pnl + funding_pnl_delta,
                )
                self._equity += funding_pnl_delta
                logger.debug(
                    "Funding: %s rate=%s notional=%s payment=%s",
                    symbol,
                    fr.rate,
                    notional,
                    funding_pnl_delta,
                )

            self._funding_index += 1

    def _check_exits(self, candle: OHLCV, exclude_symbols: set[str] | None = None) -> None:
        """Check if any position's SL or TP was hit this bar."""
        exclude = exclude_symbols or set()
        for symbol in list(self._positions.keys()):
            if symbol in exclude:
                continue
            pos = self._positions[symbol]
            pos.update_mark_price(candle.close)

            exited = False
            exit_price = candle.close

            if pos.is_long:
                # Stop hit
                if pos.stop_loss and candle.low <= pos.stop_loss:
                    exit_price = min(candle.open, pos.stop_loss)
                    exited = True
                # TP hit
                elif pos.take_profit and candle.high >= pos.take_profit:
                    exit_price = (
                        min(candle.open, pos.take_profit)
                        if candle.open >= pos.take_profit
                        else pos.take_profit
                    )
                    exited = True
            elif pos.stop_loss and candle.high >= pos.stop_loss:
                exit_price = max(candle.open, pos.stop_loss)
                exited = True
            elif pos.take_profit and candle.low <= pos.take_profit:
                exit_price = (
                    max(candle.open, pos.take_profit)
                    if candle.open <= pos.take_profit
                    else pos.take_profit
                )
                exited = True

            if exited:
                self._close_position(pos, exit_price, candle)
                del self._positions[symbol]

    def _close_position(self, pos: Position, exit_price: Decimal, candle: OHLCV) -> None:
        """
        Close position, apply exit commission, and record trade.

        Fee accounting:
          - Entry commission was already deducted from ``self._equity`` at fill
            time and is tracked in ``pos.total_fees``.
          - Only the *exit* commission is deducted here; entry commission must
            NOT be subtracted again to avoid double-counting.
          - Total fees stored on the Trade record = entry + exit commissions.

        Timestamps:
          - ``closed_at`` is taken from ``candle.timestamp`` (the bar time)
            rather than wall-clock time so backtest analytics are consistent
            with the historical timeline.
        """
        is_long = pos.is_long
        fill_price = self._slippage.apply(exit_price, not is_long, candle)
        exit_commission = self._commission.calculate(pos.quantity, fill_price)

        if is_long:
            gross_pnl = (fill_price - pos.entry_price) * pos.quantity
        else:
            gross_pnl = (pos.entry_price - fill_price) * pos.quantity

        # Entry commission already deducted at fill; only deduct exit commission
        net_pnl = gross_pnl - exit_commission + pos.funding_pnl
        self._equity += net_pnl

        trade = Trade.from_position(
            position_id=pos.position_id,
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=fill_price,
            gross_pnl=gross_pnl,
            fees=pos.total_fees + exit_commission,  # entry + exit
            funding_pnl=pos.funding_pnl,
            opened_at=pos.opened_at,
            closed_at=candle.timestamp,  # deterministic bar time
            strategy_id=pos.strategy_id,
            signal_id=pos.signal_id,
        )
        self._closed_trades.append(trade)
        logger.debug(
            "Closed %s %s: entry=%s exit=%s gross=%s funding=%s net=%s",
            pos.symbol,
            pos.side,
            pos.entry_price,
            fill_price,
            gross_pnl,
            pos.funding_pnl,
            net_pnl,
        )

    @property
    def equity(self) -> Decimal:
        """Current equity including unrealized PnL."""
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return self._equity + unrealized

    @property
    def closed_trades(self) -> list[Trade]:
        return list(self._closed_trades)

    @property
    def open_positions(self) -> list[Position]:
        return list(self._positions.values())
