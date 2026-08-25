"""
Exchange reconciliation.

Periodically syncs local position/order state against exchange
to detect and correct any discrepancies.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.core.enums import AlertSeverity, OrderStatus
from src.core.events import AlertFiredEvent, OrderFilledEvent, get_event_bus
from src.core.interfaces import IExchangeClient, IOrderManager
from src.execution.position_manager import PositionManager

logger = logging.getLogger(__name__)


class ReconciliationService:
    """
    Reconciles local state against exchange state.

    Detects:
    - Positions filled on exchange but not recorded locally
    - Orders in terminal state that are still marked open locally
    - Discrepancies in position size/price
    """

    def __init__(
        self,
        exchange: IExchangeClient,
        position_manager: PositionManager,
        order_manager: IOrderManager,
    ) -> None:
        self._exchange = exchange
        self._positions = position_manager
        self._orders = order_manager
        self._bus = get_event_bus()
        self._reconcile_count: int = 0
        self._discrepancies_found: int = 0
        self._reconcile_failures: int = 0

    async def reconcile(self) -> dict[str, Any]:
        """
        Run full reconciliation cycle.

        This runs on a periodic scheduler, so a transient failure here (a
        network blip, an exchange hiccup) must never propagate and kill the
        scheduler loop — that would silently disable reconciliation for the
        rest of the process's life, which is exactly when it's needed most
        (e.g. right after the connection issue that caused the drift). Any
        unexpected error is caught, alerted on, and the cycle simply retries
        next tick.
        """
        self._reconcile_count += 1
        report: dict[str, Any] = {
            "cycle": self._reconcile_count,
            "position_discrepancies": [],
            "orphan_positions": [],
            "order_updates": [],
        }

        try:
            exchange_positions = await self._exchange.get_positions()
        except Exception as exc:
            self._reconcile_failures += 1
            logger.error(
                "Reconciliation cycle #%d aborted: could not fetch exchange positions: %s",
                self._reconcile_count,
                exc,
            )
            return report

        exchange_pos_map = {p.symbol: p for p in exchange_positions}
        matched_symbols: set[str] = set()

        for local_pos in self._positions.open_positions:
            exch_pos = exchange_pos_map.get(local_pos.symbol)
            matched_symbols.add(local_pos.symbol)

            if exch_pos is None:
                # The exchange no longer has this position — it was closed
                # by something the system didn't observe directly (SL/TP
                # fill during a WS gap, manual intervention, liquidation).
                # Leaving it "open" locally would keep contributing to
                # exposure/margin risk calculations indefinitely and could
                # block new signals for the symbol forever. Close it out
                # using the last known mark price as the best available
                # exit-price estimate, and alert loudly since the realized
                # P&L recorded this way is an approximation, not the actual
                # fill price.
                logger.critical(
                    "RECONCILE: local position %s not found on exchange — "
                    "force-closing locally at last mark price %s",
                    local_pos.symbol,
                    local_pos.mark_price,
                )
                report["position_discrepancies"].append(
                    {
                        "symbol": local_pos.symbol,
                        "issue": "not_on_exchange",
                        "local_qty": str(local_pos.quantity),
                        "action": "force_closed_at_mark_price",
                    }
                )
                self._discrepancies_found += 1
                try:
                    await self._positions.close_position(
                        local_pos.position_id,
                        exit_price=local_pos.mark_price,
                    )
                except Exception as exc:
                    logger.error(
                        "RECONCILE: failed to force-close orphaned local position %s: %s",
                        local_pos.symbol,
                        exc,
                    )
                await self._bus.publish(
                    AlertFiredEvent(
                        source="reconciliation",
                        alert_name="position_closed_externally",
                        severity=AlertSeverity.CRITICAL,
                        message=(
                            f"Position {local_pos.symbol} vanished from the exchange and was "
                            f"force-closed locally at an estimated price — verify realized P&L"
                        ),
                        context={"symbol": local_pos.symbol, "position_id": local_pos.position_id},
                    )
                )
            else:
                # Sync mark price
                local_pos.update_mark_price(exch_pos.mark_price)

                qty_diff = abs(local_pos.quantity - exch_pos.quantity)
                if qty_diff > Decimal("0.0001"):
                    logger.warning(
                        "RECONCILE: Position qty mismatch %s: local=%s exchange=%s",
                        local_pos.symbol,
                        local_pos.quantity,
                        exch_pos.quantity,
                    )
                    report["position_discrepancies"].append(
                        {
                            "symbol": local_pos.symbol,
                            "issue": "qty_mismatch",
                            "local": str(local_pos.quantity),
                            "exchange": str(exch_pos.quantity),
                        }
                    )
                    self._discrepancies_found += 1

        # Orphan exchange positions: exist on the exchange but the system has
        # no record of them (e.g. restarted without persisted state, manual
        # trade on the account, or a previous crash mid-entry). Silently
        # ignoring these means they're invisible to risk management — no
        # circuit breaker, no exposure accounting, no protective orders.
        # We can't safely guess strategy/signal linkage, so we surface a
        # critical alert for a human to reconcile rather than adopting or
        # closing a position we don't have provenance for.
        for symbol, exch_pos in exchange_pos_map.items():
            if symbol in matched_symbols:
                continue
            logger.critical(
                "RECONCILE: exchange position %s (qty=%s) has no local record — "
                "untracked by risk management",
                symbol,
                exch_pos.quantity,
            )
            report["orphan_positions"].append(
                {
                    "symbol": symbol,
                    "quantity": str(exch_pos.quantity),
                }
            )
            self._discrepancies_found += 1
            await self._bus.publish(
                AlertFiredEvent(
                    source="reconciliation",
                    alert_name="orphan_exchange_position",
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Exchange position {symbol} (qty={exch_pos.quantity}) exists with no "
                        f"local record — untracked by risk management, needs manual review"
                    ),
                    context={"symbol": symbol, "quantity": str(exch_pos.quantity)},
                )
            )

        # 2. Reconcile open orders
        # Batch by symbol: one get_open_orders() call per unique symbol rather
        # than one get_order() call per local order (O(unique_symbols) vs
        # O(open_orders) REST round-trips).  Orders not returned by the
        # exchange's open-orders list are either cancelled/filled and need
        # status reconciliation, or still processing — fall back to a direct
        # get_order() call for those only.
        local_open_orders = self._orders.get_open_orders()
        symbols_with_open_orders: set[str] = {o.symbol for o in local_open_orders}
        exchange_open_order_map: dict[str, Any] = {}  # exchange_order_id -> Order

        for sym in symbols_with_open_orders:
            try:
                exch_orders = await self._exchange.get_open_orders(sym)
                for exch_o in exch_orders:
                    if exch_o.exchange_order_id:
                        exchange_open_order_map[exch_o.exchange_order_id] = exch_o
            except Exception as exc:
                logger.warning("Reconcile: could not fetch open orders for %s: %s", sym, exc)

        for local_order in local_open_orders:
            # If present in the exchange's open-order list, status is still OPEN/PARTIALLY_FILLED
            if (
                local_order.exchange_order_id
                and local_order.exchange_order_id in exchange_open_order_map
            ):
                exch_order = exchange_open_order_map[local_order.exchange_order_id]
            else:
                # Not in open-order list — fetch directly to confirm terminal state
                try:
                    exch_order = await self._exchange.get_order(
                        local_order.symbol,
                        local_order.exchange_order_id or local_order.client_order_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Reconcile order check failed for %s: %s", local_order.client_order_id, exc
                    )
                    continue

            if exch_order.status == local_order.status:
                continue

            logger.info(
                "RECONCILE: Order %s status %s → %s",
                local_order.client_order_id,
                local_order.status,
                exch_order.status,
            )
            newly_filled = (
                exch_order.filled_quantity > local_order.filled_quantity
                and exch_order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
            )
            was_unfilled = local_order.filled_quantity <= Decimal("0")
            # A previously-partial fill that reconciliation discovers is now
            # fully filled also needs the fill pipeline to run (position
            # creation / protective orders), since the WS partial-fill handler
            # in ExecutionEngine._on_fill does not open a position until
            # order.is_terminal is True. Without this branch, an entry order
            # that the WS delivered as PARTIALLY_FILLED but whose FILLED echo
            # was missed leaves the book in a split state: the exchange has the
            # full position open but the system has no local position record.
            was_partial_now_full = (
                not was_unfilled
                and local_order.status == OrderStatus.PARTIALLY_FILLED
                and exch_order.status == OrderStatus.FILLED
            )

            # Bring the local order's fill data up to date, not just its
            # status flag — otherwise a fill discovered only through
            # reconciliation (e.g. missed while the user-data WS was down)
            # updates status but leaves filled_quantity/average_fill_price
            # stale, and nothing downstream (position creation, protective
            # orders) ever runs for that fill.
            local_order.filled_quantity = exch_order.filled_quantity
            local_order.average_fill_price = exch_order.average_fill_price
            local_order.total_fee = exch_order.total_fee
            try:
                local_order.transition_to(exch_order.status)
            except Exception:
                # Exchange and local state machines can disagree on the
                # exact path (e.g. exchange reports CANCELLED after a partial
                # fill that we already marked PARTIALLY_FILLED) — the fill
                # data above is still correct and more important than a
                # strict transition; force the status field directly rather
                # than dropping the whole update.
                local_order.status = exch_order.status

            if hasattr(self._orders, "persist_order"):
                await self._orders.persist_order(local_order)

            report["order_updates"].append(
                {
                    "order_id": local_order.client_order_id,
                    "new_status": exch_order.status.value,
                }
            )

            if newly_filled and (was_unfilled or was_partial_now_full):
                # Trigger the normal fill pipeline (position creation,
                # protective order placement) exactly as if the WS event had
                # arrived, instead of leaving a filled order with no position
                # ever created for it.
                fill_price = exch_order.average_fill_price
                if not fill_price or fill_price <= Decimal("0"):
                    # A zero/missing fill price would produce a Position with
                    # entry_price=0, failing Pydantic validation and leaving
                    # the exchange position untracked. Skip and alert instead of
                    # publishing a broken event that poisons the fill pipeline.
                    logger.error(
                        "RECONCILE: order %s has zero/missing average_fill_price — "
                        "cannot synthesize OrderFilledEvent; position will remain "
                        "untracked until the next reconciliation cycle supplies valid data",
                        local_order.client_order_id,
                    )
                    self._discrepancies_found += 1
                else:
                    await self._bus.publish(
                        OrderFilledEvent(
                            source="reconciliation",
                            order_id=local_order.order_id,
                            symbol=local_order.symbol,
                            side=local_order.side,
                            filled_quantity=exch_order.filled_quantity,
                            fill_price=fill_price,
                            fee=exch_order.total_fee,
                            fee_asset=exch_order.fee_asset,
                            is_partial=exch_order.status == OrderStatus.PARTIALLY_FILLED,
                        )
                    )

        if self._discrepancies_found:
            logger.info(
                "Reconciliation #%d complete: %d position discrepancies, %d orphan positions, "
                "%d order updates",
                self._reconcile_count,
                len(report["position_discrepancies"]),
                len(report["orphan_positions"]),
                len(report["order_updates"]),
            )
        else:
            logger.debug("Reconciliation #%d complete: no discrepancies", self._reconcile_count)
        return report

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "reconcile_cycles": self._reconcile_count,
            "discrepancies_total": self._discrepancies_found,
            "reconcile_failures": self._reconcile_failures,
        }
