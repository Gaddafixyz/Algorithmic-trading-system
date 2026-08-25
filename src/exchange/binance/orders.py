"""
Binance order management helpers.

Provides higher-level order operations built on top of the REST client:
- Order submission with validation
- Bracket order placement (entry + stop + target)
- Order status polling
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from src.core.enums import OrderSide, OrderType, PositionSide
from src.core.exceptions import InvalidOrderError
from src.domain.instrument import Instrument
from src.domain.order import Order
from src.exchange.binance.rest import BinanceFuturesRESTClient

logger = logging.getLogger(__name__)


class BinanceOrderManager:
    """
    High-level order management with validation and bracket support.
    """

    def __init__(self, client: BinanceFuturesRESTClient) -> None:
        self._client = client

    async def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        instrument: Instrument,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
        position_side: str = "BOTH",
    ) -> Order:
        """Place a market order with quantity validation."""
        qty = instrument.round_quantity(quantity)
        errors = instrument.validate_order_params(qty, instrument.min_price * Decimal("2"))
        if errors:
            # Use a reasonable price estimate for notional check;
            # market orders don't have a price, so we skip that check here.
            qty_errors = [e for e in errors if "Notional" not in e]
            if qty_errors:
                raise InvalidOrderError(f"Invalid order params: {qty_errors}")

        return await self._client.place_order(
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=qty,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            position_side=position_side,
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        instrument: Instrument,
        *,
        reduce_only: bool = False,
        client_order_id: str | None = None,
        position_side: str = "BOTH",
        time_in_force: str = "GTC",
    ) -> Order:
        """Place a limit order with full validation."""
        qty = instrument.round_quantity(quantity)
        p = instrument.round_price(price)
        errors = instrument.validate_order_params(qty, p)
        if errors:
            raise InvalidOrderError(f"Invalid order params for {symbol}: {errors}")

        return await self._client.place_order(
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=p,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            position_side=position_side,
            time_in_force=time_in_force,
        )

    async def place_stop_market(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        stop_price: Decimal,
        instrument: Instrument,
        *,
        reduce_only: bool = True,
        client_order_id: str | None = None,
        position_side: str = "BOTH",
    ) -> Order:
        """Place a stop-market order (typically for stop-loss)."""
        qty = instrument.round_quantity(quantity)
        sp = instrument.round_price(stop_price)

        return await self._client.place_order(
            symbol=symbol,
            side=side,
            order_type=OrderType.STOP_MARKET,
            quantity=qty,
            stop_price=sp,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            position_side=position_side,
        )

    async def place_take_profit_market(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        stop_price: Decimal,
        instrument: Instrument,
        *,
        reduce_only: bool = True,
        client_order_id: str | None = None,
        position_side: str = "BOTH",
    ) -> Order:
        """Place a take-profit-market order."""
        qty = instrument.round_quantity(quantity)
        sp = instrument.round_price(stop_price)

        return await self._client.place_order(
            symbol=symbol,
            side=side,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            quantity=qty,
            stop_price=sp,
            reduce_only=reduce_only,
            client_order_id=client_order_id,
            position_side=position_side,
        )

    async def place_bracket_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        instrument: Instrument,
        *,
        stop_price: Decimal,
        take_profit_price: Decimal,
        entry_price: Decimal | None = None,
        client_order_id_prefix: str | None = None,
        position_side: str = "BOTH",
    ) -> dict[str, Order]:
        """
        Place an entry order plus its protective stop-loss and take-profit
        (the "bracket" this class's module docstring has always claimed as
        a capability, but which — until now — was never actually
        implemented as a single operation; callers had only the four
        individual place_* methods above to compose it themselves, and no
        caller ever did).

        Entry is a market order when ``entry_price`` is omitted, a limit
        order otherwise. The two exit orders use the *opposite* side from
        entry (a long's stop/target are both sells) and are always
        ``reduce_only`` — they must never be able to open or flip a
        position, only close the one this call just opened.

        Failure handling: if the entry fails, nothing further is
        attempted and the exception propagates — there's no position to
        protect yet. If the entry succeeds but either exit leg fails, this
        does **not** attempt to unwind the entry (e.g. by market-closing
        it) — that would be a second guess at a decision (flatten vs.
        retry the missing leg vs. leave it and protect manually) that
        belongs to the caller, not this helper. Instead it returns
        immediately with whatever subset of ``{"entry", "stop_loss",
        "take_profit"}`` actually landed, so the caller can see exactly
        what's live and decide. A position with a filled entry and a
        missing stop is the single most dangerous state this method can
        leave behind, so this is deliberately conservative rather than
        trying to be clever about recovery on the exchange's behalf.
        """
        exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

        def _cid(leg: str) -> str | None:
            if client_order_id_prefix is None:
                return None
            return f"{client_order_id_prefix}_{leg}"

        result: dict[str, Order] = {}

        if entry_price is None:
            entry = await self.place_market_order(
                symbol, side, quantity, instrument,
                client_order_id=_cid("entry"), position_side=position_side,
            )
        else:
            entry = await self.place_limit_order(
                symbol, side, quantity, entry_price, instrument,
                client_order_id=_cid("entry"), position_side=position_side,
            )
        result["entry"] = entry

        try:
            result["stop_loss"] = await self.place_stop_market(
                symbol, exit_side, quantity, stop_price, instrument,
                client_order_id=_cid("sl"), position_side=position_side,
            )
        except Exception as exc:
            logger.error(
                "Bracket order for %s: entry %s landed but stop-loss leg "
                "FAILED (%s) — position is currently unprotected. "
                "Caller must place a stop manually or close the position.",
                symbol, entry.client_order_id, exc,
            )
            return result

        try:
            result["take_profit"] = await self.place_take_profit_market(
                symbol, exit_side, quantity, take_profit_price, instrument,
                client_order_id=_cid("tp"), position_side=position_side,
            )
        except Exception as exc:
            logger.error(
                "Bracket order for %s: entry and stop-loss landed but "
                "take-profit leg FAILED (%s) — position is protected by "
                "the stop but has no target order.",
                symbol, entry.client_order_id, exc,
            )
            return result

        return result

    async def poll_order_until_terminal(
        self,
        symbol: str,
        order_id: str,
        *,
        poll_interval: float = 0.5,
        max_polls: int = 60,
    ) -> Order:
        """
        Poll order status until it reaches a terminal state.

        Raises TimeoutError if max_polls is exceeded.
        """
        for _ in range(max_polls):
            order = await self._client.get_order(symbol, order_id)
            if order.is_terminal:
                return order
            await asyncio.sleep(poll_interval)
        raise TimeoutError(
            f"Order {order_id} did not reach terminal state after "
            f"{max_polls * poll_interval:.0f}s"
        )
