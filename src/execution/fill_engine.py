"""Fill engine: processes exchange fill notifications into domain events."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from src.core.events import OrderFilledEvent, get_event_bus
from src.domain.order import Fill, Order

logger = logging.getLogger(__name__)


class FillEngine:
    """
    Processes raw fill data from exchange WebSocket user-data stream
    and updates local order state.
    """

    def __init__(self, order_manager: Any) -> None:
        self._orders = order_manager
        self._bus = get_event_bus()
        self._fills_processed: int = 0

    async def process_execution_report(self, msg: dict[str, Any]) -> None:
        """
        Process a Binance ORDER_TRADE_UPDATE event.

        Called from the WebSocket user-data stream handler.
        """
        event_type = msg.get("e", "")
        if event_type != "ORDER_TRADE_UPDATE":
            return

        order_data = msg.get("o", msg)

        # ``x`` is the execution type: only "TRADE" events carry a genuine
        # incremental fill.  NEW / CANCELED / EXPIRED / REPLACED events also
        # echo the *last* fill price/qty fields (l/L) from when the order was
        # previously partially filled, so processing them here would double-
        # count fills for partially-filled orders that later get cancelled.
        execution_type = str(order_data.get("x", ""))
        if execution_type != "TRADE":
            return

        client_id = str(order_data.get("c", ""))
        filled_qty = Decimal(str(order_data.get("l", "0")))  # Last filled qty
        fill_price = Decimal(str(order_data.get("L", "0")))   # Last fill price
        fee = Decimal(str(order_data.get("n", "0")))
        fee_asset = str(order_data.get("N", "USDT"))
        is_maker = order_data.get("m", False)

        order = await self._orders.get_order_by_client_id(client_id)
        if order is None:
            logger.debug("Fill for unknown order client_id=%s", client_id)
            return

        if filled_qty > Decimal("0") and fill_price > Decimal("0"):
            fill = Fill(
                price=fill_price,
                quantity=filled_qty,
                fee=fee,
                fee_asset=fee_asset,
                is_maker=is_maker,
            )
            order.add_fill(fill)
            self._fills_processed += 1

            if hasattr(self._orders, "persist_order"):
                await self._orders.persist_order(order)

            await self._bus.publish(OrderFilledEvent(
                source="fill_engine",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                filled_quantity=filled_qty,
                fill_price=fill_price,
                fee=fee,
                fee_asset=fee_asset,
                is_partial=not order.is_terminal,
            ))

            logger.info(
                "Fill processed: %s qty=%s @%s fee=%s %s",
                order.symbol, filled_qty, fill_price, fee, fee_asset,
            )
