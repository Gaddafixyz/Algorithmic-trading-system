"""
Paper-trading exchange client.

Implements ``IExchangeClient`` so paper trading runs through the exact same
``RiskManager`` -> ``ExecutionEngine`` -> ``OrderManager`` -> ``FillEngine``
pipeline as live trading, rather than a parallel simulator that could drift
from (or silently bypass) real risk enforcement.

Design:
- Market-data reads (candles, ticker, order book, exchange info) are
  delegated straight through to a real, credentialed ``IExchangeClient``
  (Binance or Bybit) so paper trading sees genuine live prices.
- Order actions (place/cancel/get) are simulated entirely in-memory: a
  MARKET order fills immediately at the last known mark price; LIMIT,
  STOP_MARKET/STOP_LIMIT, and TAKE_PROFIT_MARKET/TAKE_PROFIT_LIMIT orders
  rest until the live mark-price stream (subscribed directly via the real
  WebSocket client's ``subscribe_mark_price``) crosses their trigger.
- Fills are delivered to whatever handler is registered via
  ``subscribe_fills`` (normally ``FillEngine.process_execution_report``),
  using the exact same Binance-shaped ``ORDER_TRADE_UPDATE`` dict that both
  live exchange adapters normalize to, so ``FillEngine`` needs no paper-mode
  branch at all.
- Fill delivery is deferred via ``asyncio.create_task`` rather than awaited
  inline from ``place_order``. ``OrderManager.submit_order`` only registers
  the order's ``client_order_id`` -> ``order_id`` mapping *after*
  ``place_order`` returns; delivering a synchronous fill from inside
  ``place_order`` would race that registration and the fill would be
  dropped as "unknown order".
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any

from src.core.enums import OrderSide, OrderStatus, OrderType, PositionSide
from src.core.exceptions import OrderNotFoundError
from src.domain.order import Order
from src.domain.position import Position

logger = logging.getLogger(__name__)

# Order types that rest and trigger off a stop/trigger price rather than
# filling immediately.
_TRIGGER_TYPES = frozenset(
    {
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.TAKE_PROFIT_MARKET,
        OrderType.TAKE_PROFIT_LIMIT,
    }
)
_STOP_TYPES = frozenset({OrderType.STOP_MARKET, OrderType.STOP_LIMIT})
_TAKE_PROFIT_TYPES = frozenset({OrderType.TAKE_PROFIT_MARKET, OrderType.TAKE_PROFIT_LIMIT})
_MARKET_FILL_TYPES = frozenset({OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET})


def _trigger_direction(order_type: OrderType, side: OrderSide) -> str:
    """
    Which way price must move to trigger this resting order.

    STOP orders and TAKE_PROFIT orders trigger in *opposite* directions for
    the same side, because they protect opposite tails of the same
    position:
    - STOP_MARKET/STOP_LIMIT SELL (protective stop on a long) triggers on a
      *drop* to/through the trigger price. The BUY-side stop (protecting a
      short) triggers on a *rise*.
    - TAKE_PROFIT_MARKET/TAKE_PROFIT_LIMIT SELL (locking in a long's gain)
      triggers on a *rise*; the BUY-side take-profit (short) triggers on a
      *drop*.
    - A plain resting LIMIT BUY fills on a drop to/through its price; a
      resting LIMIT SELL fills on a rise to/through its price.

    Getting this backwards silently converts every stop-loss into a
    take-profit and vice versa — this exact bug was caught during initial
    development.
    """
    if order_type in _STOP_TYPES:
        return "down" if side == OrderSide.SELL else "up"
    if order_type in _TAKE_PROFIT_TYPES:
        return "up" if side == OrderSide.SELL else "down"
    if order_type == OrderType.LIMIT:
        return "down" if side == OrderSide.BUY else "up"
    return ""


def _crosses(new_price: Decimal, trigger_price: Decimal, direction: str) -> bool:
    if direction == "down":
        return new_price <= trigger_price
    if direction == "up":
        return new_price >= trigger_price
    return False


class _PaperPosition:
    """Minimal average-cost position ledger, local to the paper client."""

    __slots__ = ("symbol", "quantity", "entry_price", "mark_price", "realized_pnl")

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.quantity = Decimal("0")  # signed: positive = long, negative = short
        self.entry_price = Decimal("0")
        self.mark_price = Decimal("0")
        self.realized_pnl = Decimal("0")


class PaperExchangeClient:
    """
    Simulated exchange satisfying ``IExchangeClient`` for paper trading.

    Not a subclass of anything by design (structural typing via
    ``IExchangeClient``'s ``Protocol``) — see ``core/interfaces.py``.
    """

    provider = "paper"

    def __init__(
        self,
        delegate: Any,
        ws_client: Any,
        symbols: list[str],
        initial_equity: Decimal = Decimal("10000"),
        taker_fee_rate: Decimal = Decimal("0.0004"),
    ) -> None:
        self._delegate = delegate
        self._ws_client = ws_client
        self._symbols = list(symbols)
        self.equity = initial_equity
        self._taker_fee_rate = taker_fee_rate

        self._mark_prices: dict[str, Decimal] = {}
        self._positions: dict[str, _PaperPosition] = {
            symbol: _PaperPosition(symbol) for symbol in self._symbols
        }
        self._orders: dict[str, Order] = {}  # order_id -> Order
        self._client_id_index: dict[str, str] = {}  # client_order_id -> order_id
        self._resting_order_ids: dict[str, set[str]] = {
            symbol: set() for symbol in self._symbols
        }

        self._fill_handler: Any = None
        self._sub_ids: list[str] = []
        self._started = False

        # Deferred market-order fills (see the module docstring on why
        # they're deferred) are spawned via asyncio.create_task() rather
        # than awaited inline. A bare create_task() call with no stored
        # reference risks the task being garbage-collected mid-flight
        # (the event loop only holds a weak reference), and any exception
        # it raises would otherwise surface only as an asyncio "Task
        # exception was never retrieved" warning at GC time -- silent as
        # far as this app's own logging is concerned. Tracked here and
        # given a done-callback (_on_background_task_done) instead.
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ─── Lifecycle ──────────────────────────────────────────────────────

    def _spawn_fill_simulation(self, order_id: str, mark: Decimal | None) -> None:
        """
        Fire-and-forget a deferred _simulate_fill(), tracked so it can't
        be silently GC'd mid-flight and so any exception it raises is
        actually logged through this app's normal logging path instead
        of only via asyncio's default "Task exception was never
        retrieved" handler.
        """
        task = asyncio.create_task(self._simulate_fill(order_id, mark))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "Deferred paper fill simulation failed", exc_info=exc
            )

    def subscribe_fills(self, handler: Any) -> None:
        """
        Register the callable that receives simulated fills.

        Mirrors ``IWebSocketStreamClient.subscribe_user_data`` — normally
        wired to ``FillEngine.process_execution_report`` in startup.py, the
        same handler live trading uses.
        """
        self._fill_handler = handler

    async def start(self) -> None:
        """Subscribe to live mark-price updates for every traded symbol."""
        if self._started:
            return
        for symbol in self._symbols:
            sub_id = self._ws_client.subscribe_mark_price(
                symbol, self._make_mark_price_handler(symbol)
            )
            self._sub_ids.append(sub_id)
        self._started = True
        logger.info(
            "PaperExchangeClient started: %d symbols, initial equity=%s",
            len(self._symbols), self.equity,
        )

    async def stop(self) -> None:
        self._started = False
        if self._background_tasks:
            # Cancel rather than await-to-completion: stop() is teardown,
            # not a "let paper fills finish" step -- shutdown.py's own
            # per-step try/except wraps this call, so a lingering fill
            # simulation must not delay or fail the rest of teardown.
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    async def connect(self) -> None:
        # Market-data connectivity is owned by the delegate; nothing extra
        # to connect for order simulation itself.
        return None

    async def disconnect(self) -> None:
        # Never disconnect the shared delegate client here — app.py owns
        # its lifecycle independently (it's also used for market data).
        return None

    # ─── Account ────────────────────────────────────────────────────────

    async def get_account_balance(self) -> dict[str, Decimal]:
        return {"USDT": self.equity}

    async def get_positions(self) -> list[Position]:
        result: list[Position] = []
        for pos in self._positions.values():
            if pos.quantity == 0:
                continue
            side = PositionSide.LONG if pos.quantity > 0 else PositionSide.SHORT
            result.append(
                Position(
                    symbol=pos.symbol,
                    side=side,
                    quantity=abs(pos.quantity),
                    entry_quantity=abs(pos.quantity),
                    entry_price=pos.entry_price,
                    mark_price=pos.mark_price or pos.entry_price,
                    realized_pnl=pos.realized_pnl,
                )
            )
        return result

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        logger.debug("Paper mode: set_leverage(%s, %d) — no-op", symbol, leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        logger.debug("Paper mode: set_margin_type(%s, %s) — no-op", symbol, margin_type)

    # ─── Orders ─────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        price: Decimal | None,
        *,
        stop_price: Decimal | None = None,
        time_in_force: str = "GTC",
        reduce_only: bool = False,
        client_order_id: str | None = None,
    ) -> Order:
        order_id = f"paper_{uuid.uuid4().hex[:16]}"
        client_order_id = client_order_id or order_id

        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            reduce_only=reduce_only,
            exchange_order_id=order_id,
            status=OrderStatus.OPEN,
        )
        self._orders[order_id] = order
        self._client_id_index[client_order_id] = order_id
        self._resting_order_ids.setdefault(symbol, set()).add(order_id)

        if order_type == OrderType.MARKET:
            mark = self._mark_prices.get(symbol)
            # Deferred: see module docstring — must not race
            # OrderManager.submit_order's post-place_order bookkeeping.
            self._spawn_fill_simulation(order_id, mark)

        return order

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        internal_id = self._client_id_index.get(order_id, order_id)
        order = self._orders.get(internal_id)
        if order is None or order.is_terminal:
            return False
        order.transition_to(OrderStatus.CANCELLED)
        self._resting_order_ids.get(symbol, set()).discard(internal_id)
        return True

    async def get_order(self, symbol: str, order_id: str) -> Order:
        internal_id = self._client_id_index.get(order_id, order_id)
        order = self._orders.get(internal_id)
        if order is None:
            raise OrderNotFoundError(order_id, symbol)
        return order

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            o
            for o in self._orders.values()
            if not o.is_terminal and (symbol is None or o.symbol == symbol)
        ]

    # ─── Market data (delegated to the real, credentialed client) ──────

    async def get_candles(self, symbol: str, timeframe: Any, limit: int, start_time: Any = None) -> Any:
        return await self._delegate.get_candles(symbol, timeframe, limit, start_time)

    async def get_ticker(self, symbol: str) -> Any:
        return await self._delegate.get_ticker(symbol)

    async def get_order_book(self, symbol: str, depth: int) -> Any:
        return await self._delegate.get_order_book(symbol, depth)

    async def get_exchange_info(self) -> Any:
        return await self._delegate.get_exchange_info()

    # ─── Fill simulation ────────────────────────────────────────────────

    def _make_mark_price_handler(self, symbol: str) -> Any:
        async def _on_mark_price(msg: dict[str, Any]) -> None:
            price_raw = msg.get("p")
            if price_raw is None:
                return
            price = Decimal(str(price_raw))
            self._mark_prices[symbol] = price
            pos = self._positions.get(symbol)
            if pos is not None:
                pos.mark_price = price
            await self._check_triggers(symbol, price)

        return _on_mark_price

    async def _check_triggers(self, symbol: str, price: Decimal) -> None:
        for order_id in list(self._resting_order_ids.get(symbol, ())):
            order = self._orders.get(order_id)
            if order is None or order.is_terminal:
                self._resting_order_ids[symbol].discard(order_id)
                continue
            if order.order_type == OrderType.MARKET:
                continue  # already filled synchronously at submission

            trigger_price = order.stop_price if order.order_type in _TRIGGER_TYPES else order.price
            if trigger_price is None:
                continue
            direction = _trigger_direction(order.order_type, order.side)
            if not _crosses(price, trigger_price, direction):
                continue

            # STOP_MARKET/TAKE_PROFIT_MARKET fill at the prevailing mark
            # price once triggered (natural slippage past the trigger,
            # matching real exchange behaviour); LIMIT and *_LIMIT orders
            # fill at their specified price.
            fill_price = price if order.order_type in _MARKET_FILL_TYPES else trigger_price
            self._resting_order_ids[symbol].discard(order_id)
            await self._simulate_fill(order_id, fill_price)

    async def _simulate_fill(self, order_id: str, fill_price: Decimal | None) -> None:
        order = self._orders.get(order_id)
        if order is None or order.is_terminal or fill_price is None:
            if order is not None and fill_price is None:
                logger.warning(
                    "Paper order %s cannot fill: no mark price known for %s yet",
                    order.client_order_id, order.symbol,
                )
            return

        qty = order.quantity
        fee = (fill_price * qty * self._taker_fee_rate).quantize(Decimal("0.00000001"))

        self._apply_fill_to_ledger(order, fill_price, qty, fee)

        if self._fill_handler is None:
            logger.warning(
                "Paper fill for %s generated but no fill handler registered "
                "(subscribe_fills was never called)", order.client_order_id,
            )
            return

        # Same Binance-shaped ORDER_TRADE_UPDATE dict both live adapters
        # normalize to — FillEngine.process_execution_report needs no
        # paper-mode branch.
        msg = {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "x": "TRADE",
                "c": order.client_order_id,
                "l": str(qty),
                "L": str(fill_price),
                "n": str(fee),
                "N": "USDT",
                "m": False,
            },
        }
        await self._fill_handler(msg)

    def _apply_fill_to_ledger(
        self, order: Order, fill_price: Decimal, qty: Decimal, fee: Decimal
    ) -> None:
        """
        Update the internal position ledger and paper equity for one fill.

        Equity moves by realized P&L on any closed/reduced portion, minus
        the simulated fee, on every fill. Unrealized P&L on still-open
        positions is intentionally *not* folded into ``equity`` here — it's
        tracked on the ``Position`` objects returned by ``get_positions()``
        (via ``mark_price``), matching how ``RiskManager``/``Portfolio``
        already separate realized equity from unrealized P&L for live
        exchanges.
        """
        pos = self._positions.setdefault(order.symbol, _PaperPosition(order.symbol))
        signed_qty = qty if order.side == OrderSide.BUY else -qty
        realized_before = pos.realized_pnl

        if pos.quantity == 0 or (pos.quantity > 0) == (signed_qty > 0):
            # Opening or adding to a position: weighted-average entry price.
            new_qty = pos.quantity + signed_qty
            if new_qty != 0:
                pos.entry_price = (
                    (pos.entry_price * abs(pos.quantity) + fill_price * qty) / abs(new_qty)
                )
            pos.quantity = new_qty
        else:
            # Reducing or flipping a position: realize P&L on the closed
            # portion at the existing average entry price.
            closing_qty = min(qty, abs(pos.quantity))
            direction = Decimal("1") if pos.quantity > 0 else Decimal("-1")
            pos.realized_pnl += direction * (fill_price - pos.entry_price) * closing_qty
            pos.quantity += signed_qty
            if pos.quantity == 0:
                pos.entry_price = Decimal("0")
            elif (pos.quantity > 0) != (direction > 0):
                # Flipped through zero: the remainder opens a new position
                # at this fill's price.
                pos.entry_price = fill_price

        pnl_delta = pos.realized_pnl - realized_before
        self.equity += pnl_delta - fee
