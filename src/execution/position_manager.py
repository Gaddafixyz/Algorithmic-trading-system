"""Position lifecycle management and tracking."""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from typing import Any

from src.core.enums import PositionSide
from src.core.events import PositionClosedEvent, get_event_bus
from src.domain.position import Position
from src.domain.trade import Trade

logger = logging.getLogger(__name__)

# Maximum number of closed trades to retain in memory. Older entries are
# evicted once this limit is reached. All trades are still persisted to the
# database by _persist_trade() regardless of this cap, so historical data
# is not lost — only the in-memory summary list is bounded.
# Application.record_metrics_snapshot() iterates this list every 5 minutes;
# without a cap, a high-frequency system accumulates thousands of Trade
# objects, slowing every metrics snapshot and win-rate calculation.
_MAX_CLOSED_TRADES_IN_MEMORY: int = 1_000


class PositionManager:
    """
    Tracks open positions and generates Trade records on close.

    Maintained in-memory; persisted via repository on each change.
    """

    def __init__(self, db: Any | None = None, max_closed_trades: int = _MAX_CLOSED_TRADES_IN_MEMORY) -> None:
        self._positions: dict[str, Position] = {}  # position_id → Position
        # deque with maxlen evicts the oldest entry automatically when full,
        # bounding memory growth in long-running processes without requiring
        # explicit eviction logic. All trades are persisted to DB before
        # being appended here (see _persist_trade / close_position), so
        # eviction only affects the in-memory summary list.
        self._closed_trades: deque[Trade] = deque(maxlen=max_closed_trades)
        self._bus = get_event_bus()
        # Optional Database for persisting completed trades. Without this,
        # no trade is ever written to the database.
        self._db = db

    async def _persist_trade(self, trade: Trade) -> None:
        """Best-effort persistence; failures are logged, never propagated."""
        if self._db is None:
            return
        try:
            from src.persistence.trades import TradeRepository

            async with self._db.session() as session:
                await TradeRepository(session).save_trade(trade)
        except Exception as exc:
            logger.warning("Failed to persist trade %s: %s", trade.trade_id, exc)

    def register(self, position: Position) -> None:
        """Register a newly opened position."""
        self._positions[position.position_id] = position
        logger.info("Position registered: %s", position)

    def get(self, position_id: str) -> Position | None:
        return self._positions.get(position_id)

    def get_by_symbol(self, symbol: str) -> list[Position]:
        return [p for p in self._positions.values() if p.symbol == symbol and p.is_open]

    def update_mark_price(self, symbol: str, mark_price: Decimal) -> None:
        """Update mark price for all positions on a symbol."""
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.is_open:
                pos.update_mark_price(mark_price)

    def apply_funding(self, symbol: str, rate: Decimal) -> None:
        """
        Apply a funding-rate settlement to every open position on a symbol.

        Standard perpetual-futures convention: a positive rate means
        longs pay shorts, a negative rate means shorts pay longs. The
        payment is sized against the position's current notional
        (quantity * mark_price) — mark_price is used rather than a
        price carried on the funding event itself, since FundingRateEvent
        doesn't carry one and mark_price is kept live independently via
        update_mark_price()/MarkPriceUpdatedEvent, so it's never more
        than one ~1s tick stale at the moment funding settles.

        Feeds Position.add_funding(), which existed with a fully
        implemented funding_pnl field/total_pnl formula but no caller
        anywhere in the live path prior to this method — funding_pnl
        stayed permanently 0 for every position.
        """
        for pos in self._positions.values():
            if pos.symbol != symbol or not pos.is_open:
                continue
            notional = pos.quantity * pos.mark_price
            if pos.side == PositionSide.LONG:
                payment = -(notional * rate)
            else:
                payment = notional * rate
            pos.add_funding(payment)

    async def close_position(
        self,
        position_id: str,
        exit_price: Decimal,
        qty: Decimal | None = None,
        fee: Decimal = Decimal("0"),
    ) -> Trade | None:
        """
        Close (or partially close) a position and generate a Trade record.

        Returns the Trade record if the position is fully closed.
        """
        position = self._positions.get(position_id)
        if position is None or not position.is_open:
            return None

        close_qty = qty or position.quantity
        realized = position.add_partial_close(close_qty, exit_price, fee)

        if position.is_closed:
            trade = Trade.from_position(
                position_id=position.position_id,
                symbol=position.symbol,
                side=position.side,
                quantity=position.entry_quantity,
                entry_price=position.entry_price,
                exit_price=exit_price,
                gross_pnl=position.realized_pnl,
                fees=position.total_fees,
                opened_at=position.opened_at,
                closed_at=position.closed_at,  # type: ignore[arg-type]
                signal_id=position.signal_id,
                strategy_id=position.strategy_id,
                funding_pnl=position.funding_pnl,
            )
            self._closed_trades.append(trade)
            await self._persist_trade(trade)

            # Evict from the live-position registry so closed positions don't
            # accumulate in _positions indefinitely (unbounded memory growth in
            # long-running processes). The position object is already captured
            # in the Trade record and _closed_trades list above, so no data is
            # lost. Filtering by is_open in open_positions/get_by_symbol is not
            # sufficient: the dict still grows and every O(n) scan slows down.
            self._positions.pop(position.position_id, None)

            await self._bus.publish(PositionClosedEvent(
                source="position_manager",
                position_id=position.position_id,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.entry_quantity,
                realized_pnl=position.realized_pnl,
                fees=position.total_fees,
                strategy_id=position.strategy_id,
            ))

            logger.info(
                "Position closed: %s PnL=%s fee=%s",
                position, position.net_realized_pnl, position.total_fees,
            )
            return trade

        logger.debug(
            "Partial close: %s qty=%s remaining=%s pnl=%s",
            position.symbol, close_qty, position.quantity, realized,
        )
        return None

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.is_open]

    @property
    def closed_trades(self) -> list[Trade]:
        # Convert deque to list for callers that expect list semantics.
        # The deque is bounded by maxlen; this copy is O(n) but n is capped.
        return list(self._closed_trades)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "open_positions": len(self.open_positions),
            "closed_trades": len(self._closed_trades),
            "total_realized_pnl": str(
                sum(t.net_pnl for t in self._closed_trades)
            ),
        }
