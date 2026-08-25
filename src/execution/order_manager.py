"""
Order Manager.

Maintains local state of all orders and bridges to the exchange client.
Provides idempotent submission and deduplication.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.core.events import (
    OrderCancelledEvent,
    OrderRejectedEvent,
    OrderStatusChangedEvent,
    OrderSubmittedEvent,
    get_event_bus,
)
from src.core.exceptions import (
    DuplicateOrderError,
    ExchangeConnectionError,
    ExchangeTimeoutError,
    MaxRetriesExceededError,
    OrderNotFoundError,
    OrderSubmissionError,
)
from src.domain.order import Order, OrderStatus
from src.core.interfaces import IExchangeClient
from src.utils.profiling import RollingTimer

logger = logging.getLogger(__name__)


class OrderManager:
    """
    Local order registry + exchange bridge.

    Tracks all orders in memory. Bridges local Order objects to
    exchange API calls. Detects duplicate submissions.
    """

    def __init__(self, exchange: IExchangeClient, db: Any | None = None) -> None:
        self._exchange = exchange
        self._orders: dict[str, Order] = {}              # order_id → Order
        self._client_id_map: dict[str, str] = {}         # client_order_id → order_id
        self._submitted_ids: set[str] = set()
        self._submitted_count: int = 0
        self._cancelled_count: int = 0
        # RollingTimer tracked the last 100 samples of *some* operation's
        # latency but had no caller anywhere in src/, so submit-side
        # latency was only ever visible per-call in DEBUG logs (see
        # place_order timing below) with no rolling avg/p99 available to
        # an operator glancing at the dashboard. Tracks the exchange
        # round-trip for order submission specifically — the leg most
        # exposed to exchange-side degradation and the one that most
        # directly affects fill quality/slippage.
        self._submit_latency = RollingTimer(window=100)
        # Optional Database for persisting order state. Without this, no
        # order/fill is ever written to the database — combined with a
        # process restart, all local order state is lost with nothing to
        # recover from beyond exchange reconciliation.
        self._db = db
        # Event bus — OrderSubmittedEvent/OrderStatusChangedEvent/
        # OrderRejectedEvent/OrderCancelledEvent previously existed as
        # schema only, with no producer anywhere in the codebase, so
        # nothing downstream (dashboards, notifiers, audit consumers)
        # ever saw an order-lifecycle event. publish_sync is used since
        # every call site below is itself synchronous state mutation.
        self._bus = get_event_bus()

    def _publish_status_change(
        self, order: Order, previous_status: OrderStatus
    ) -> None:
        if previous_status == order.status:
            return
        self._bus.publish_sync(
            OrderStatusChangedEvent(
                order_id=order.order_id,
                symbol=order.symbol,
                previous_status=previous_status,
                new_status=order.status,
            )
        )

    async def persist_order(self, order: Order) -> None:
        """Public hook for external callers (FillEngine, reconciliation) to
        persist an order after they mutate its fill state directly."""
        await self._persist(order)

    async def _persist(self, order: Order) -> None:
        """
        Best-effort persistence of the current order state.

        Persistence failures must never break the trading path — they're
        logged and swallowed rather than propagated.
        """
        if self._db is None:
            return
        try:
            from src.persistence.orders import OrderRepository

            async with self._db.session() as session:
                await OrderRepository(session).save_order(order)
        except Exception as exc:
            logger.warning("Failed to persist order %s: %s", order.client_order_id, exc)

    async def submit_order(self, order: Order) -> Order:
        """
        Submit an order to the exchange.

        Raises:
            DuplicateOrderError: If client_order_id was already submitted.
            OrderSubmissionError: On exchange rejection.
        """
        if order.client_order_id in self._submitted_ids:
            raise DuplicateOrderError(order.client_order_id)

        self._submitted_ids.add(order.client_order_id)
        self._orders[order.order_id] = order

        try:
            prev = order.status
            order.transition_to(OrderStatus.SUBMITTED)
            self._publish_status_change(order, prev)
            self._bus.publish_sync(
                OrderSubmittedEvent(
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    price=order.price,
                    order_type=order.order_type,
                )
            )
            _t0 = time.perf_counter()
            exchanged = await self._exchange.place_order(
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                price=order.price,
                stop_price=order.stop_price,
                time_in_force=order.time_in_force.value,
                reduce_only=order.reduce_only,
                client_order_id=order.client_order_id,
            )
            self._submit_latency.record((time.perf_counter() - _t0) * 1000)

            # Sync exchange-assigned ID back to local order
            order.exchange_order_id = exchanged.exchange_order_id
            prev = order.status
            order.transition_to(OrderStatus.OPEN)
            self._publish_status_change(order, prev)
            self._client_id_map[order.client_order_id] = order.order_id
            self._submitted_count += 1
            await self._persist(order)
            logger.info("Order submitted: %s → exchange_id=%s", order.client_order_id, order.exchange_order_id)
            return order

        except (ExchangeTimeoutError, ExchangeConnectionError, MaxRetriesExceededError) as exc:
            # Ambiguous failure: the request may never have reached Binance,
            # or Binance may have received and executed it but the response
            # never made it back to us. Blindly marking this FAILED and
            # freeing client_order_id (as done below for definite rejections)
            # would let a caller retry with a fresh order and end up with two
            # live orders on the exchange for one signal. Check first.
            logger.warning(
                "Ambiguous failure submitting %s (%s) — checking exchange before giving up",
                order.client_order_id, exc,
            )
            existing = await self._check_order_landed(order)
            if existing is not None:
                order.exchange_order_id = existing.exchange_order_id
                prev = order.status
                order.transition_to(OrderStatus.OPEN)
                self._publish_status_change(order, prev)
                self._client_id_map[order.client_order_id] = order.order_id
                self._submitted_count += 1
                await self._persist(order)
                logger.info(
                    "Order %s actually landed on exchange despite ambiguous response "
                    "(exchange_id=%s) — treating as submitted",
                    order.client_order_id, order.exchange_order_id,
                )
                return order

            prev = order.status
            order.transition_to(OrderStatus.FAILED)
            self._publish_status_change(order, prev)
            order.error_message = str(exc)
            self._submitted_ids.discard(order.client_order_id)
            self._orders.pop(order.order_id, None)
            await self._persist(order)
            self._bus.publish_sync(
                OrderRejectedEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    reason=str(exc),
                )
            )
            logger.error(
                "Order submission failed for %s and exchange confirms it never landed: %s",
                order.client_order_id, exc,
            )
            raise OrderSubmissionError(str(exc)) from exc

        except Exception as exc:
            prev = order.status
            order.transition_to(OrderStatus.FAILED)
            self._publish_status_change(order, prev)
            order.error_message = str(exc)
            self._submitted_ids.discard(order.client_order_id)
            # Remove the pre-registered entry so FAILED orders don't
            # accumulate in _orders indefinitely in long-running processes.
            self._orders.pop(order.order_id, None)
            await self._persist(order)
            self._bus.publish_sync(
                OrderRejectedEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    reason=str(exc),
                )
            )
            logger.error("Order submission failed for %s: %s", order.client_order_id, exc)
            raise OrderSubmissionError(str(exc)) from exc

    async def _check_order_landed(self, order: Order) -> Order | None:
        """
        After an ambiguous (timeout/connection) submission failure, ask the
        exchange directly whether the order actually exists.

        Returns the exchange's copy of the order if found, else None. Any
        further error while checking is treated as "could not confirm" — the
        caller falls back to the safer (though not risk-free) assumption that
        the order failed, rather than looping here indefinitely.
        """
        try:
            return await self._exchange.get_order(order.symbol, order.client_order_id)
        except OrderNotFoundError:
            return None
        except Exception as exc:
            logger.error(
                "Could not confirm order %s state with exchange after ambiguous "
                "failure: %s — assuming it did not land",
                order.client_order_id, exc,
            )
            return None

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel a local + exchange order."""
        order = self._orders.get(order_id)
        if order is None:
            logger.warning("Cancel requested for unknown order %s", order_id)
            return False

        if order.is_terminal:
            logger.debug("Order %s already in terminal state %s", order_id, order.status)
            return False

        exchange_id = order.exchange_order_id or order.client_order_id
        cancelled = await self._exchange.cancel_order(symbol, exchange_id)
        if cancelled:
            prev = order.status
            order.transition_to(OrderStatus.CANCELLED)
            self._publish_status_change(order, prev)
            self._cancelled_count += 1
            await self._persist(order)
            # Evict from the live-order registry so terminal CANCELLED orders don't
            # accumulate in _orders indefinitely in long-running processes. Every SL/TP
            # cancellation at position close adds an entry here; without eviction, _orders
            # grows without bound and every get_open_orders() O(n) scan slows down.
            # The order has already been persisted to DB (above) and the event emitted
            # below, so no data is lost. Pattern mirrors the FAILED order eviction in
            # submit_order() (lines ~181, ~204).
            self._orders.pop(order.order_id, None)
            self._client_id_map.pop(order.client_order_id, None)
            self._bus.publish_sync(
                OrderCancelledEvent(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    reason="cancel_order requested",
                )
            )
        return cancelled

    async def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    async def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        order_id = self._client_id_map.get(client_order_id)
        return self._orders.get(order_id) if order_id else None

    async def cancel_all_orders(self, symbol: str | None = None) -> int:
        """Cancel all open orders, optionally filtered by symbol."""
        count = 0
        for order in list(self._orders.values()):
            if not order.is_open:
                continue
            if symbol and order.symbol != symbol:
                continue
            if await self.cancel_order(order.order_id, order.symbol):
                count += 1
        return count

    def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o for o in self._orders.values()
            if o.is_open and (symbol is None or o.symbol == symbol)
        ]

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_orders": len(self._orders),
            "submitted": self._submitted_count,
            "cancelled": self._cancelled_count,
            "open": len(self.get_open_orders()),
            "submit_latency_ms": {
                "avg": round(self._submit_latency.avg_ms, 1),
                "p99": round(self._submit_latency.p99_ms, 1),
            },
        }
