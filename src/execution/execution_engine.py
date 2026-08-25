"""
Execution Engine.

Translates validated signals into orders and manages the full
order/position lifecycle:
  1. Receive SignalGeneratedEvent
  2. Validate with RiskManager
  3. Size position
  4. Submit entry order
  5. Track fills and update position
  6. Place stop-loss and take-profit orders
  7. Manage trailing stop / breakeven (see _on_tick — re-evaluated on
     every TickReceivedEvent for symbols with an open position; disable
     via manage_trailing_exits=False). Position mark price itself is kept
     current independently via _on_mark_price / MarkPriceUpdatedEvent.
  8. Close position on exit signal or SL/TP hit
  9. Optionally (track_funding_pnl=True) settle funding-rate payments
     against open positions — see _on_funding_rate.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from src.core.enums import AlertSeverity, OrderSide, OrderType, PositionSide, SignalDirection
from src.core.events import (
    AlertFiredEvent,
    FundingRateEvent,
    MarkPriceUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrderRejectedEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    PositionUpdatedEvent,
    SignalExpiredEvent,
    SignalGeneratedEvent,
    TickReceivedEvent,
    get_event_bus,
)
from src.core.exceptions import TakeProfitPlacementError
from src.core.interfaces import IOrderManager, IRiskManager
from src.domain.instrument import Instrument
from src.domain.order import Fill, Order
from src.domain.position import Position
from src.domain.portfolio import Portfolio
from src.domain.signal import Signal
from src.domain.trade import Trade
from src.execution.position_manager import PositionManager
from src.risk.take_profit import partial_take_profit_levels
from src.strategy.entry import get_entry_order_type, get_order_side, is_entry_valid
from src.strategy.exit import evaluate_exit_actions

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """
    Coordinates signal → order → position lifecycle.

    Subscribes to SignalGeneratedEvents and OrderFilledEvents.
    """

    def __init__(
        self,
        order_manager: IOrderManager,
        position_manager: PositionManager,
        risk_manager: IRiskManager,
        portfolio: Portfolio,
        instruments: dict[str, Instrument],
        *,
        use_limit_entries: bool = True,
        use_oco_exits: bool = True,
        manage_trailing_exits: bool = True,
        breakeven_activation_rr: float = 1.0,
        partial_tp_levels: list[tuple[Decimal, Decimal]] | None = None,
        track_funding_pnl: bool = False,
    ) -> None:
        self._orders = order_manager
        self._positions = position_manager
        self._risk = risk_manager
        self._portfolio = portfolio
        self._instruments = instruments
        self._use_limit_entries = use_limit_entries
        self._use_oco_exits = use_oco_exits
        self._manage_trailing_exits = manage_trailing_exits
        self._breakeven_activation_rr = breakeven_activation_rr
        # (rr_multiple, qty_fraction) pairs, resolved to absolute prices per
        # position at entry-fill time (see _on_entry_filled) since RR is
        # relative to each position's own entry/stop distance. None/empty
        # disables partial take-profit entirely — evaluate_exit_actions
        # treats an empty partial_tp_levels list the same as not passing one.
        self._partial_tp_levels_rr = partial_tp_levels or []
        # FundingRateEvent has been published since market_data/feeds.py
        # gained a dedicated funding stream, but nothing ever subscribed:
        # Position.add_funding()/funding_pnl existed with no caller, so
        # funding_pnl stayed permanently 0 on every position and
        # Trade.net_pnl was silently missing a real, continuous PnL
        # component for perpetual futures. Opt-in / default False per
        # the standing "new behavior is opt-in" policy even though this
        # is pure accounting accuracy (it changes reported PnL numbers,
        # not any trading decision) — see _on_funding_rate.
        self._track_funding_pnl = track_funding_pnl
        self._bus = get_event_bus()
        self._running = False
        self._orders_submitted = 0
        self._orders_failed = 0
        self._pending_signals: dict[str, Signal] = {}  # client_order_id -> signal
        # position_id -> stop-loss order_id currently live at the exchange,
        # so a trail/breakeven update knows which order to cancel/replace.
        self._active_stop_orders: dict[str, str] = {}
        # position_id -> take-profit order_id currently live at the
        # exchange. Tracked separately from _active_stop_orders (which is
        # also read/written by the trailing-stop replace path) so that on
        # position close we know exactly which OCO sibling order, if any,
        # is still resting on the exchange and needs to be cancelled — see
        # _on_position_closed.
        self._active_tp_orders: dict[str, str] = {}
        # symbol -> last observed trade-tick price. Kept independently of
        # manage_trailing_exits (which only gates *exit* re-evaluation) so
        # that entry submission always has a fresh price to re-validate
        # against, even when trailing-stop management is disabled.
        self._last_price: dict[str, Decimal] = {}

    async def start(self) -> None:
        self._bus.subscribe(SignalGeneratedEvent, self._on_signal)
        self._bus.subscribe(OrderFilledEvent, self._on_fill)
        self._bus.subscribe(OrderCancelledEvent, self._on_order_cancelled)
        self._bus.subscribe(OrderRejectedEvent, self._on_order_rejected)
        self._bus.subscribe(MarkPriceUpdatedEvent, self._on_mark_price)
        self._bus.subscribe(PositionClosedEvent, self._on_position_closed)
        if self._track_funding_pnl:
            self._bus.subscribe(FundingRateEvent, self._on_funding_rate)
        # Always subscribed (not gated by manage_trailing_exits) — this
        # handler both maintains the _last_price staleness cache used by
        # entry submission and, when manage_trailing_exits is enabled,
        # drives trailing-stop/breakeven re-evaluation.
        self._bus.subscribe(TickReceivedEvent, self._on_tick)
        self._running = True
        logger.info("ExecutionEngine started")

    async def stop(self) -> None:
        self._running = False
        logger.info("ExecutionEngine stopped")

    # ─── Event handlers ───────────────────────────────────────────────────────

    async def _on_signal(self, event: Any) -> None:
        """Handle an incoming trade signal."""
        if not isinstance(event, SignalGeneratedEvent):
            return
        if not self._running:
            return

        symbol = event.symbol
        instrument = self._instruments.get(symbol)
        if instrument is None:
            logger.warning("No instrument config for %s — skipping signal", symbol)
            return

        # Re-construct signal from event for risk validation.
        # atr is carried on the event so the resulting position's entry_atr
        # tag has a valid baseline for tick-level trailing-stop management.
        # signal_id is only forwarded when the event actually carries one
        # (older/test events may not) — passing an empty string through
        # would otherwise defeat Signal's default_factory and persist a
        # blank id, so omission falls back to a freshly minted uuid4 the
        # same way a bare Signal() construction always has.
        # generated_at is backfilled from the event's own timestamp (every
        # BaseEvent is stamped at construction, i.e. when the orchestrator
        # originally published it) rather than left to default to "now".
        # Without this, the is_expired() check just below was dead code:
        # a Signal reconstructed with a fresh generated_at can never
        # appear expired relative to itself, no matter how long the event
        # actually sat on the bus. max_age_seconds isn't carried on the
        # event, so it still falls back to Signal's 1h default here — the
        # same fallback already documented in orchestrator.py's
        # _on_position_closed.
        signal_kwargs: dict[str, Any] = {"generated_at": event.timestamp}
        if event.signal_id:
            signal_kwargs["signal_id"] = event.signal_id
        signal = Signal(
            strategy_id=event.strategy_id,
            symbol=event.symbol,
            timeframe=event.timeframe,  # type: ignore[arg-type]
            direction=event.direction,
            score=event.score,
            strength="moderate",  # type: ignore[arg-type]
            confidence=event.confidence,
            entry_price=event.entry_price,
            stop_loss=event.stop_loss,
            take_profit=event.take_profit,
            atr=event.atr,
            metadata={"open_fvgs": event.open_fvgs} if event.open_fvgs else {},
            **signal_kwargs,
        )

        # Expiry guard: the event bus can buffer signals during congestion;
        # reject any that have aged out before we attempt execution.
        if signal.is_expired():
            logger.warning(
                "SignalGeneratedEvent for %s arrived expired (generated_at=%s, "
                "max_age=%.0fs) — discarding without submission",
                symbol, signal.generated_at.isoformat(), signal.max_age_seconds,
            )
            await self._bus.publish(
                SignalExpiredEvent(
                    source="execution_engine",
                    symbol=symbol,
                    signal_id=signal.signal_id,
                    reason="expired_in_transit",
                )
            )
            return

        # A prior signal for this symbol may already have an entry order
        # in flight (submitted but not yet filled). Position-level dedup in
        # RiskManager only catches an already-open *position*, not a pending
        # order — without this check, two signals arriving before the first
        # fill confirmation would stack two entries for the same symbol.
        if any(s.symbol == symbol for s in self._pending_signals.values()) or any(
            o.symbol == symbol for o in self._orders.get_open_orders()
        ):
            logger.warning("Entry already pending for %s — skipping duplicate signal", symbol)
            return

        try:
            await self._risk.validate_signal(signal)
            position_size = await self._risk.calculate_position_size(signal)
            position_size = instrument.round_quantity(position_size)

            if position_size <= Decimal("0"):
                logger.warning("Sized to zero for %s — skipping", symbol)
                return

            await self._submit_entry(signal, position_size, instrument)

        except Exception as exc:
            logger.warning("Signal rejected for %s: %s", symbol, exc)
            self._orders_failed += 1

    async def _on_fill(self, event: Any) -> None:
        """Handle an order fill event."""
        if not isinstance(event, OrderFilledEvent):
            return

        order = await self._orders.get_order(event.order_id)
        if order is None:
            return

        signal = self._pending_signals.get(order.client_order_id)

        if order.is_terminal and signal is not None:
            await self._on_entry_filled(order, signal)
            self._pending_signals.pop(order.client_order_id, None)
            return

        if order.is_terminal and order.reduce_only:
            # A reduce_only order with no pending entry signal is a
            # stop-loss/take-profit (or manual reduce) exit fill. Without
            # this branch, the only place that ever closed a position was
            # the orphan-position fallback inside reconciliation — meaning a
            # position stopped out on the exchange stayed "open" locally
            # indefinitely, still counted in exposure/margin and blocking
            # new entries for the symbol.
            await self._on_exit_filled(order)

    async def _on_order_cancelled(self, event: Any) -> None:
        """
        Evict any pending entry signal when its order is cancelled.

        Without this, a LIMIT entry order that expires or is explicitly
        cancelled leaves its signal in _pending_signals indefinitely.
        The duplicate-signal guard in _on_signal filters by symbol across
        _pending_signals.values(), so the stale entry blocks every new
        signal for that symbol until Signal.max_age_seconds elapses (up
        to 1 hour by default). The guard exists to prevent double-entry
        on rapid re-signals — but only while the order is genuinely still
        alive. Once the order reaches a terminal state, the slot must be
        released immediately.
        """
        if not isinstance(event, OrderCancelledEvent):
            return
        order = await self._orders.get_order(event.order_id)
        if order is None:
            return
        # Only evict from pending_signals if this was an entry (non-reduce_only) order.
        # Reduce-only SL/TP cancels are handled by _on_position_closed and should
        # not touch _pending_signals.
        if not order.reduce_only:
            removed = self._pending_signals.pop(order.client_order_id, None)
            if removed is not None:
                logger.info(
                    "Cleared pending signal for %s after entry order cancellation "
                    "(order_id=%s, client_order_id=%s)",
                    order.symbol, order.order_id, order.client_order_id,
                )

    async def _on_order_rejected(self, event: Any) -> None:
        """
        Evict any pending entry signal when its order is rejected.

        OrderRejectedEvent fires from OrderManager when the exchange
        returns a definite rejection (not an ambiguous timeout) or when
        an ambiguous timeout is confirmed as not-landed. Without this
        handler, a rejected entry order causes the same symbol-blocking
        pending_signals leak as a cancelled order (see _on_order_cancelled).
        """
        if not isinstance(event, OrderRejectedEvent):
            return
        order = await self._orders.get_order(event.order_id)
        if order is None:
            return
        if not order.reduce_only:
            removed = self._pending_signals.pop(order.client_order_id, None)
            if removed is not None:
                logger.info(
                    "Cleared pending signal for %s after entry order rejection "
                    "(order_id=%s, reason=%s)",
                    order.symbol, order.order_id, event.reason,
                )

    async def _on_exit_filled(self, order: Order) -> None:
        """Apply a reduce-only exit fill (stop-loss, take-profit, or partial take-profit) to the matching local position."""
        positions = self._positions.get_by_symbol(order.symbol)
        if not positions:
            logger.warning(
                "Exit fill for %s but no open local position to close (order_id=%s)",
                order.symbol, order.order_id,
            )
            return

        # Under normal operation only one position per symbol is open at a
        # time (entries for a symbol are blocked while one is pending/open),
        # so matching by symbol is unambiguous in practice.
        position = positions[0]
        exit_price = order.average_fill_price or position.mark_price
        # Pass the order's actual filled quantity rather than relying on
        # close_position()'s full-remaining-size default. A full SL/TP
        # exit order is always sized to position.quantity so this behaves
        # identically for that case — but a partial-take-profit order is
        # deliberately undersized (see _submit_partial_tp), and without
        # this the position would be fully closed on the first partial
        # fill instead of just reduced.
        close_qty = order.filled_quantity if order.filled_quantity > Decimal("0") else None

        trade = await self._positions.close_position(
            position.position_id,
            exit_price=exit_price,
            qty=close_qty,
            fee=order.total_fee,
        )
        # Any remaining OCO sibling order (e.g. the take-profit still
        # resting on the exchange after the stop-loss filled, or vice
        # versa) is cancelled inside _on_position_closed, which
        # close_position() only triggers via PositionClosedEvent once the
        # position is fully closed (trade is not None).

        if trade is not None:
            logger.info(
                "Position closed via exit-order fill: %s order_id=%s exit_price=%s",
                order.symbol, order.order_id, exit_price,
            )
            return

        logger.info(
            "Partial exit fill applied: %s order_id=%s qty=%s remaining=%s",
            order.symbol, order.order_id, close_qty, position.quantity,
        )
        await self._resize_protective_orders(position)

    async def _on_position_closed(self, event: Any) -> None:
        """
        Cancel any OCO sibling exit order still resting on the exchange
        once a position is fully closed, and drop this engine's tracking
        of that position's protective orders.

        A stop-loss and take-profit are placed as two independent
        reduce_only orders (see _place_exit_orders), not a true
        exchange-side OCO pair — the exchange itself does not cancel one
        leg when the other fills. Without this handler, whichever leg
        didn't fire (or, on a reconciliation-driven force-close, whichever
        legs never fired) is left resting on the exchange indefinitely. In
        the worst case that stale reduce_only order later fills against an
        unrelated future position opened on the same symbol, silently
        cutting it short at a stale price the trader never intended.

        Fires for every path that closes a position — a normal SL/TP fill
        (_on_exit_filled), an exit signal, or reconciliation force-closing
        a position that vanished from the exchange — since PositionManager.
        close_position() publishes PositionClosedEvent uniformly in every
        case and this engine subscribes to it directly rather than only
        handling cleanup inline after its own fill path.
        """
        if not isinstance(event, PositionClosedEvent):
            return

        position_id = event.position_id
        sl_order_id = self._active_stop_orders.pop(position_id, None)
        tp_order_id = self._active_tp_orders.pop(position_id, None)

        for order_id in (sl_order_id, tp_order_id):
            if order_id is None:
                continue
            try:
                cancelled = await self._orders.cancel_order(order_id, event.symbol)
                if cancelled:
                    logger.info(
                        "Cancelled leftover exit order %s for %s after position close",
                        order_id, event.symbol,
                    )
            except Exception as exc:
                # The order may still be resting live on the exchange.
                # Surface this loudly rather than swallowing it — it's a
                # dangling reduce_only order that could misfire later.
                logger.error(
                    "Failed to cancel leftover exit order %s for %s after position "
                    "close — it may still be resting on the exchange: %s",
                    order_id, event.symbol, exc,
                )
                await self._bus.publish(AlertFiredEvent(
                    source="execution_engine",
                    alert_name="stale_exit_order_cancel_failed",
                    severity=AlertSeverity.WARNING,
                    message=(
                        f"Failed to cancel leftover exit order {order_id} for "
                        f"{event.symbol} ({position_id}) after position close: {exc}"
                    ),
                    context={
                        "symbol": event.symbol,
                        "position_id": position_id,
                        "order_id": order_id,
                    },
                ))

        # Evict the closed position from Portfolio so it no longer contributes
        # to gross_exposure, open_position_count, or has_position_for_symbol()
        # checks. Without this, Portfolio.positions grows without bound in long-
        # running processes and closed positions silently inflate every exposure
        # and deduplication check the risk manager runs against the portfolio.
        self._portfolio.remove_position(position_id)

    # ─── Live exit management (trailing stop / breakeven) ─────────────────────

    async def _on_tick(self, event: Any) -> None:
        """
        Re-evaluate trailing-stop / breakeven on each trade tick for
        symbols with an open position.

        This handler drives *when* to re-evaluate the trailing-stop/
        breakeven logic (trade ticks arrive far more frequently than the
        exchange's markPrice stream, so using them here keeps trail
        updates responsive) using the last-trade price as the reference
        for that decision. It does NOT update position.mark_price — that
        is exchange-computed margin/PnL data and is sourced exclusively
        from MarkPriceUpdatedEvent (see _on_mark_price) so it can never
        silently drift from what Binance actually uses for liquidation.

        Hard stop/TP execution stays the exchange's job via the resting
        STOP_MARKET/TAKE_PROFIT_MARKET orders placed at entry (and is
        confirmed back to us through _on_exit_filled) — this handler only
        ever tightens the protective stop, it never force-closes locally,
        so we can't diverge from what actually happened on the exchange.
        """
        if not isinstance(event, TickReceivedEvent):
            return
        if not self._running:
            return

        self._last_price[event.symbol] = event.price

        if not self._manage_trailing_exits:
            return

        positions = self._positions.get_by_symbol(event.symbol)
        if not positions:
            return

        for position in positions:
            try:
                await self._manage_position_exit(position, event.price)
            except Exception as exc:
                logger.exception(
                    "Exit management error for %s (position_id=%s): %s",
                    position.symbol, position.position_id, exc,
                )

    async def _on_mark_price(self, event: Any) -> None:
        """
        Update each open position's mark price from the exchange's
        dedicated mark-price stream.

        This is the single source of truth for position.mark_price: it's
        the exchange-computed, funding-adjusted price actually used for
        unrealized PnL, margin, and liquidation — not an approximation
        from trade ticks or candle closes. Unlike trailing-stop management
        (_on_tick), this always runs regardless of manage_trailing_exits,
        since stale mark price is a financial-safety issue (RiskManager's
        exposure/margin checks read position.mark_price), not just a
        trailing-stop feature.
        """
        if not isinstance(event, MarkPriceUpdatedEvent):
            return
        if not self._running:
            return

        positions = self._positions.get_by_symbol(event.symbol)
        if not positions:
            return

        self._positions.update_mark_price(event.symbol, event.mark_price)

        # Publish PositionUpdatedEvent for every affected open position so
        # dashboards and subscribers see live unrealized-PnL changes without
        # polling. Previously PositionUpdatedEvent existed as schema only —
        # no producer anywhere published it, so unrealized P&L was only ever
        # visible by directly reading PositionManager state between
        # PositionOpenedEvent and PositionClosedEvent.
        for pos in positions:
            await self._bus.publish(
                PositionUpdatedEvent(
                    source="execution_engine",
                    position_id=pos.position_id,
                    symbol=pos.symbol,
                    quantity=pos.quantity,
                    unrealized_pnl=pos.unrealized_pnl,
                )
            )

    async def _on_funding_rate(self, event: Any) -> None:
        """
        Settle a funding-rate update against every open position on the
        symbol (see PositionManager.apply_funding for the payment math).

        Only subscribed when track_funding_pnl=True (see __init__) — off
        by default so this is a strictly additive, opt-in accounting
        change: it never touches signal selection, stops, or sizing.
        """
        if not isinstance(event, FundingRateEvent):
            return
        if not self._running:
            return

        positions = self._positions.get_by_symbol(event.symbol)
        if not positions:
            return

        self._positions.apply_funding(event.symbol, event.rate)
        logger.info(
            "Funding settled: %s rate=%s positions=%d",
            event.symbol, event.rate, len(positions),
        )

    async def _manage_position_exit(self, position: Position, current_price: Decimal) -> None:
        """Evaluate and, if warranted, push an updated stop-loss for one position."""
        entry_atr_raw = position.tags.get("entry_atr")
        atr = Decimal(str(entry_atr_raw)) if entry_atr_raw is not None else Decimal("0")

        untaken_levels = [
            (price, fraction)
            for i, (price, fraction) in enumerate(position.partial_tp_plan)
            if i not in position.partial_tp_taken
        ]

        def _zones(tag_key: str) -> list[tuple[Decimal, Decimal]]:
            out = []
            for z in position.tags.get(tag_key) or []:
                try:
                    out.append((Decimal(z["bottom"]), Decimal(z["top"])))
                except (KeyError, TypeError, InvalidOperation):
                    continue
            return out

        fvg_target_zones = _zones("fvg_target_zones")
        fvg_reversal_zones = _zones("fvg_reversal_zones")
        fvg_target_taken = set(position.tags.get("fvg_target_taken", []))

        plan = evaluate_exit_actions(
            position, current_price, atr,
            breakeven_activation_rr=self._breakeven_activation_rr,
            partial_tp_levels=untaken_levels or None,
            fvg_target_zones=fvg_target_zones or None,
            fvg_reversal_zones=fvg_reversal_zones or None,
            fvg_target_taken=fvg_target_taken or None,
        )

        # Stop/TP crossing is left to the exchange-side orders; see
        # docstring on _on_tick.
        if plan.should_close_position:
            return

        for price, fraction in plan.triggered_partial_tps:
            idx = next(
                (
                    i for i, (p, f) in enumerate(position.partial_tp_plan)
                    if p == price and f == fraction and i not in position.partial_tp_taken
                ),
                None,
            )
            if idx is None:
                continue
            # Mark taken immediately, before the order even lands, so a
            # slow submission (or this method being re-entered on the next
            # tick before the fill event arrives) can't submit the same
            # level twice.
            position.partial_tp_taken.append(idx)
            await self._submit_partial_tp(position, fraction)

        for _price, fraction, zone_idx in plan.fvg_target_partial_tps:
            taken = position.tags.setdefault("fvg_target_taken", [])
            if zone_idx in taken:
                continue
            taken.append(zone_idx)
            await self._submit_partial_tp(position, fraction)

        is_breakeven = False
        new_stop: Decimal | None = None
        regime: str | None = None

        if plan.move_to_breakeven and not position.breakeven_triggered:
            # Compute the target price without mutating the position yet —
            # we only want to commit set_breakeven()'s side effects
            # (stop_loss + breakeven_triggered) once the exchange order is
            # confirmed replaced, so a failed order swap can't leave local
            # state claiming breakeven is active when it isn't live.
            if position.entry_quantity > Decimal("0"):
                fee_per_unit = position.total_fees / position.entry_quantity
                new_stop = (
                    position.entry_price + fee_per_unit if position.is_long
                    else position.entry_price - fee_per_unit
                )
                is_breakeven = True
        elif plan.new_stop_loss is not None and position.stop_loss is not None:
            improves = (
                (position.is_long and plan.new_stop_loss > position.stop_loss)
                or (position.is_short and plan.new_stop_loss < position.stop_loss)
            )
            if improves:
                new_stop = plan.new_stop_loss
                # Only meaningful for the trailing-stop path — breakeven's
                # target price isn't regime-derived, so leave `regime` as
                # None there and let the log line omit it.
                regime = plan.volatility_regime

        if plan.fvg_reversal_risk:
            logger.info(
                "FVG reversal-risk zone entered for %s (position_id=%s) at price=%s",
                position.symbol, position.position_id, current_price,
            )

        if new_stop is not None:
            await self._replace_stop_loss_order(
                position, new_stop, is_breakeven=is_breakeven, regime=regime,
            )

    async def _submit_partial_tp(self, position: Position, fraction: Decimal) -> None:
        """Submit a reduce-only MARKET order to take partial profit at a triggered ladder level."""
        instrument = self._instruments.get(position.symbol)
        if instrument is None:
            logger.warning(
                "No instrument config for %s — cannot submit partial take-profit",
                position.symbol,
            )
            return

        qty = instrument.round_quantity(
            min(position.entry_quantity * fraction, position.quantity)
        )
        if qty <= Decimal("0"):
            return

        close_side = OrderSide.SELL if position.is_long else OrderSide.BUY
        order = Order(
            symbol=position.symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=qty,
            reduce_only=True,
            signal_id=position.signal_id,
            strategy_id=position.strategy_id,
        )
        try:
            await self._orders.submit_order(order)
            logger.info(
                "Partial take-profit submitted for %s: qty=%s (fraction=%s of entry size)",
                position.symbol, qty, fraction,
            )
        except Exception as exc:
            # Non-fatal: the position's full stop-loss/take-profit remain
            # live and protective either way, this only misses locking in
            # the partial gain. Log loudly rather than silently dropping it.
            logger.error(
                "Partial take-profit order failed for %s (qty=%s): %s",
                position.symbol, qty, exc,
            )

    async def _resize_protective_orders(self, position: Position) -> None:
        """
        Cancel and replace the resting SL/TP orders to match position.quantity.

        The exchange-side stop-loss and take-profit orders are sized to
        position.quantity at the time they were last placed/replaced. A
        partial take-profit fill shrinks position.quantity without
        touching those resting orders — left alone, either one would try
        to close more than what's actually left of the position the next
        time it triggers. Called immediately after every partial fill
        (see _on_exit_filled) so the resting orders always track current
        size.
        """
        instrument = self._instruments.get(position.symbol)
        if instrument is None or position.quantity <= Decimal("0"):
            return
        qty = instrument.round_quantity(position.quantity)
        if qty <= Decimal("0"):
            return

        close_side = OrderSide.SELL if position.is_long else OrderSide.BUY

        old_sl_id = self._active_stop_orders.get(position.position_id)
        if old_sl_id is not None and position.stop_loss is not None:
            try:
                await self._orders.cancel_order(old_sl_id, position.symbol)
                new_sl = Order(
                    symbol=position.symbol,
                    side=close_side,
                    order_type=OrderType.STOP_MARKET,
                    quantity=qty,
                    stop_price=instrument.round_price(position.stop_loss),
                    reduce_only=True,
                    signal_id=position.signal_id,
                    strategy_id=position.strategy_id,
                )
                submitted = await self._orders.submit_order(new_sl)
                self._active_stop_orders[position.position_id] = submitted.order_id
            except Exception as exc:
                logger.critical(
                    "UNPROTECTED POSITION: failed to resize stop-loss for %s after "
                    "partial take-profit fill (position_id=%s): %s",
                    position.symbol, position.position_id, exc,
                )
                await self._bus.publish(AlertFiredEvent(
                    source="execution_engine",
                    alert_name="unprotected_position",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Position {position.symbol} ({position.position_id}) stop-loss "
                        f"resize failed after a partial take-profit fill: {exc}"
                    ),
                    context={
                        "symbol": position.symbol,
                        "position_id": position.position_id,
                        "new_quantity": str(qty),
                    },
                ))

        old_tp_id = self._active_tp_orders.get(position.position_id)
        if old_tp_id is not None and position.take_profit is not None:
            try:
                await self._orders.cancel_order(old_tp_id, position.symbol)
                new_tp = Order(
                    symbol=position.symbol,
                    side=close_side,
                    order_type=OrderType.TAKE_PROFIT_MARKET,
                    quantity=qty,
                    stop_price=instrument.round_price(position.take_profit),
                    reduce_only=True,
                    signal_id=position.signal_id,
                    strategy_id=position.strategy_id,
                )
                submitted = await self._orders.submit_order(new_tp)
                self._active_tp_orders[position.position_id] = submitted.order_id
            except Exception as exc:
                # Stop-loss (handled above) is what protects capital; a
                # failed TP resize just means gains beyond this point
                # won't auto-lock-in, not an unbounded-risk situation.
                logger.error(
                    "Failed to resize take-profit for %s after partial take-profit "
                    "fill (position_id=%s): %s — stop-loss remains protective",
                    position.symbol, position.position_id, exc,
                )

    async def _replace_stop_loss_order(
        self, position: Position, new_stop: Decimal, *,
        is_breakeven: bool = False, regime: str | None = None,
    ) -> None:
        """Cancel the live stop-loss order and submit a new one at new_stop."""
        instrument = self._instruments.get(position.symbol)
        if instrument is None:
            logger.warning(
                "No instrument config for %s — cannot update stop-loss", position.symbol,
            )
            return

        old_order_id = self._active_stop_orders.get(position.position_id)
        if old_order_id is not None:
            try:
                await self._orders.cancel_order(old_order_id, position.symbol)
            except Exception as exc:
                # If the cancel fails (e.g. it already filled — the stop
                # was hit right as we tried to move it), don't submit a
                # second, conflicting stop order; let the fill flow through
                # _on_fill / _on_exit_filled normally instead.
                logger.warning(
                    "Failed to cancel prior stop-loss order %s for %s: %s — "
                    "skipping stop update this tick",
                    old_order_id, position.symbol, exc,
                )
                return

        close_side = OrderSide.SELL if position.is_long else OrderSide.BUY
        new_sl_order = Order(
            symbol=position.symbol,
            side=close_side,
            order_type=OrderType.STOP_MARKET,
            quantity=position.quantity,
            stop_price=instrument.round_price(new_stop),
            reduce_only=True,
            signal_id=position.signal_id,
            strategy_id=position.strategy_id,
        )
        try:
            submitted = await self._orders.submit_order(new_sl_order)
        except Exception as exc:
            logger.critical(
                "UNPROTECTED POSITION: failed to replace stop-loss for %s "
                "(position_id=%s) after cancelling old stop: %s",
                position.symbol, position.position_id, exc,
            )
            await self._bus.publish(AlertFiredEvent(
                source="execution_engine",
                alert_name="unprotected_position",
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"Position {position.symbol} ({position.position_id}) has no "
                    f"stop-loss after a trailing/breakeven update failed: {exc}"
                ),
                context={
                    "symbol": position.symbol,
                    "position_id": position.position_id,
                    "attempted_stop": str(new_stop),
                },
            ))
            return

        self._active_stop_orders[position.position_id] = submitted.order_id
        position.stop_loss = new_stop
        if is_breakeven:
            position.breakeven_triggered = True
        logger.info(
            "Stop-loss updated for %s: %s (position_id=%s%s%s)",
            position.symbol, new_stop, position.position_id,
            ", breakeven" if is_breakeven else "",
            f", regime={regime}" if regime is not None else "",
        )

    # ─── Entry ────────────────────────────────────────────────────────────────

    async def _submit_entry(
        self,
        signal: Signal,
        quantity: Decimal,
        instrument: Instrument,
    ) -> None:
        """Submit the entry order for a signal."""
        side = get_order_side(signal.direction)
        order_type = get_entry_order_type(signal, use_limit=self._use_limit_entries)

        # Price staleness re-check. is_entry_valid() existed as a
        # standalone helper but was never called anywhere — a signal can
        # sit for an unbounded stretch between generation and this point
        # (event-bus backlog, risk-validation latency, position-sizing
        # awaits) and price may have moved away from the intended entry
        # zone/level in the meantime. That's especially dangerous for a
        # MARKET order, where there's no limit price to bound the fill.
        # Skipped only on cold start, before this engine has observed a
        # single tick for the symbol — better to submit against the
        # signal's own price than block every entry until first tick.
        last_price = self._last_price.get(signal.symbol)
        if last_price is not None and not is_entry_valid(signal, last_price):
            logger.warning(
                "Entry no longer valid for %s: signal entry=%s zone=[%s,%s] "
                "current=%s — discarding stale signal",
                signal.symbol, signal.entry_price,
                signal.entry_zone_low, signal.entry_zone_high, last_price,
            )
            self._orders_failed += 1
            return

        if order_type == OrderType.LIMIT:
            price = instrument.round_price(signal.entry_price)
        else:
            price = None

        order = Order(
            symbol=signal.symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
        )

        submitted = await self._orders.submit_order(order)
        self._pending_signals[submitted.client_order_id] = signal
        self._orders_submitted += 1
        logger.info(
            "Entry submitted: %s %s qty=%s price=%s order_id=%s",
            signal.symbol, side, quantity, price, submitted.client_order_id,
        )

    # ─── Post-fill ────────────────────────────────────────────────────────────

    async def _on_entry_filled(self, order: Order, signal: Signal) -> None:
        """Create position and place protective orders after entry fill."""
        if order.average_fill_price is None:
            logger.error("Fill without average price for %s", order.order_id)
            return

        pos_side = PositionSide.LONG if order.is_buy else PositionSide.SHORT

        tags: dict[str, Any] = {"entry_atr": str(signal.atr)} if signal.atr is not None else {}

        # Snapshot open FVGs from the signal's metadata (see
        # confluence.py::score_confluence) and split into "target" zones
        # (same type as this position's direction — bullish FVG for a
        # long, bearish for a short) vs. "reversal" zones (opposing type)
        # so exit.py's evaluate_exit_actions can use them without needing
        # a live SMCAnalysis reference, which ExecutionEngine intentionally
        # doesn't hold once a position is open. This is a static entry-time
        # snapshot, same tradeoff as entry_atr above — zones don't get
        # refreshed as new FVGs form later in the trade's life.
        target_zones: list[dict[str, str]] = []
        reversal_zones: list[dict[str, str]] = []
        for raw_fvg in (signal.metadata or {}).get("open_fvgs", []):
            try:
                top = Decimal(str(raw_fvg["top"]))
                bottom = Decimal(str(raw_fvg["bottom"]))
                fvg_type = raw_fvg["type"]
            except (KeyError, TypeError, InvalidOperation):
                continue
            zone = {"top": str(top), "bottom": str(bottom)}
            same_direction = (
                (pos_side == PositionSide.LONG and fvg_type == "bullish")
                or (pos_side == PositionSide.SHORT and fvg_type == "bearish")
            )
            (target_zones if same_direction else reversal_zones).append(zone)
        if target_zones:
            tags["fvg_target_zones"] = target_zones
        if reversal_zones:
            tags["fvg_reversal_zones"] = reversal_zones

        position = Position(
            symbol=order.symbol,
            side=pos_side,
            quantity=order.filled_quantity,
            entry_quantity=order.filled_quantity,
            entry_price=order.average_fill_price,
            mark_price=order.average_fill_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            total_fees=order.total_fee,
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            # entry_atr seeds tick-level trailing-stop management (see
            # _on_tick) — the strategy engine doesn't push ATR updates
            # once a position is open, so this is a reasonable static
            # proxy for volatility over the life of the trade.
            tags=tags,
        )

        self._portfolio.add_position(position)
        self._positions.register(position)

        if self._partial_tp_levels_rr and signal.stop_loss is not None:
            # Resolve each configured (rr_multiple, qty_fraction) level to
            # an absolute price for this specific position — delegated to
            # risk/take_profit.py's partial_take_profit_levels() rather
            # than reimplemented inline, which is what this used to do
            # (entry ± risk*rr duplicated exactly what that function
            # already computes). Only computed when stop_distance > 0
            # (partial_take_profit_levels itself doesn't guard a zero-risk
            # stop, and a zero-width ladder is meaningless anyway).
            stop_distance = abs(position.entry_price - signal.stop_loss)
            if stop_distance > Decimal("0"):
                position.partial_tp_plan = partial_take_profit_levels(
                    position.entry_price,
                    signal.stop_loss,
                    signal.direction,
                    levels=self._partial_tp_levels_rr,
                )

        await self._bus.publish(PositionOpenedEvent(
            source="execution_engine",
            position_id=position.position_id,
            symbol=position.symbol,
            side=pos_side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            strategy_id=position.strategy_id,
        ))

        instrument = self._instruments.get(order.symbol)
        if instrument and self._use_oco_exits:
            # This runs inside an OrderFilledEvent handler, which the event
            # bus invokes via asyncio.gather(return_exceptions=True) — any
            # exception raised here is only *logged*, never re-raised to a
            # caller who could act on it. A naive call would let a failed
            # protective-order placement leave a live, leveraged position
            # with no stop-loss and nobody the wiser. Catch it ourselves and
            # escalate loudly instead.
            try:
                await self._place_exit_orders(position, signal, instrument)
            except TakeProfitPlacementError as exc:
                # Stop-loss is confirmed live; only the take-profit leg
                # failed. This is a real but lower-severity problem than
                # an unprotected position: capital is not at unbounded
                # risk, gains simply won't auto-lock-in. Fire a distinct,
                # lower-severity alert rather than the same CRITICAL
                # unprotected_position alert used for a failed stop-loss.
                await self._bus.publish(AlertFiredEvent(
                    source="execution_engine",
                    alert_name="take_profit_placement_failed",
                    severity=AlertSeverity.WARNING,
                    message=str(exc),
                    context={
                        "symbol": position.symbol,
                        "position_id": position.position_id,
                        "quantity": str(position.quantity),
                        "entry_price": str(position.entry_price),
                    },
                ))
            except Exception as exc:
                logger.critical(
                    "UNPROTECTED POSITION: failed to place exit orders for %s "
                    "(position_id=%s) after entry fill: %s",
                    position.symbol, position.position_id, exc,
                )
                await self._bus.publish(AlertFiredEvent(
                    source="execution_engine",
                    alert_name="unprotected_position",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Position {position.symbol} ({position.position_id}) opened "
                        f"without stop-loss/take-profit protection: {exc}"
                    ),
                    context={
                        "symbol": position.symbol,
                        "position_id": position.position_id,
                        "quantity": str(position.quantity),
                        "entry_price": str(position.entry_price),
                    },
                ))

        logger.info(
            "Position opened: %s %s qty=%s entry=%s SL=%s TP=%s",
            position.symbol, pos_side, position.quantity,
            position.entry_price, signal.stop_loss, signal.take_profit,
        )

    async def _place_exit_orders(
        self,
        position: Position,
        signal: Signal,
        instrument: Instrument,
    ) -> None:
        """
        Place stop-loss and take-profit orders.

        The stop-loss is the critical leg — without it a leveraged position
        is exposed to unbounded loss — so it gets its own retry budget
        separate from the take-profit order. A take-profit failure still logs
        loudly (the position is protected but can't lock in gains
        automatically) rather than being swallowed.
        """
        close_side = OrderSide.SELL if position.is_long else OrderSide.BUY
        attempts = 3

        last_exc: Exception | None = None
        submitted_sl: Order | None = None
        for attempt in range(1, attempts + 1):
            # A fresh Order (and thus a fresh client_order_id) each
            # attempt — reusing one Order object across retries was a
            # previously-fixed bug that regressed here: submit_order()
            # transitions the order to SUBMITTED immediately, and a failed
            # attempt leaves it in the terminal FAILED state, so retry 2
            # would raise OrderStateError before ever reaching the
            # exchange, collapsing this into a single real attempt. A
            # fresh client_order_id per attempt doesn't reopen the
            # duplicate-order risk that pattern exists to prevent —
            # OrderManager.submit_order()'s own ambiguous-failure handling
            # (_check_order_landed) already confirms with the exchange
            # before treating a timeout/connection failure as a genuine
            # non-submission, independent of which client_order_id is used.
            sl = Order(
                symbol=position.symbol,
                side=close_side,
                order_type=OrderType.STOP_MARKET,
                quantity=position.quantity,
                stop_price=instrument.round_price(signal.stop_loss),
                reduce_only=True,
                signal_id=signal.signal_id,
                strategy_id=signal.strategy_id,
            )
            try:
                submitted_sl = await self._orders.submit_order(sl)
                self._active_stop_orders[position.position_id] = submitted_sl.order_id
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 - must not let SL placement die silently
                last_exc = exc
                logger.warning(
                    "Stop-loss placement attempt %d/%d failed for %s: %s",
                    attempt, attempts, position.symbol, exc,
                )
        if last_exc is not None:
            raise last_exc

        # Take-profit order
        tp = Order(
            symbol=position.symbol,
            side=close_side,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            quantity=position.quantity,
            stop_price=instrument.round_price(signal.take_profit),
            reduce_only=True,
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
        )
        try:
            submitted_tp = await self._orders.submit_order(tp)
            self._active_tp_orders[position.position_id] = submitted_tp.order_id
        except Exception as exc:
            # The position IS protected (stop-loss is live) — this is a
            # degraded-but-safe state, not a full unprotected-position
            # emergency, so it doesn't need the same retry budget. Still
            # surfaced loudly since gains won't be auto-locked-in. Raised
            # as a distinct exception type so the caller
            # (_on_entry_filled) can tell this apart from a genuine
            # unprotected-position failure and fire the correct-severity
            # alert rather than treating every exit-order failure
            # identically.
            logger.error(
                "Take-profit placement failed for %s (stop-loss is in place): %s",
                position.symbol, exc,
            )
            raise TakeProfitPlacementError(position.symbol, exc) from exc

        logger.debug(
            "Exit orders placed for %s: SL=%s TP=%s",
            position.symbol, signal.stop_loss, signal.take_profit,
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "orders_submitted": self._orders_submitted,
            "orders_failed": self._orders_failed,
            "pending_signals": len(self._pending_signals),
            "running": self._running,
        }
