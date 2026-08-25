"""Unit tests for src.paper_trading.paper_exchange_client.PaperExchangeClient."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.enums import OrderSide, OrderStatus, OrderType
from src.core.exceptions import OrderNotFoundError
from src.paper_trading.paper_exchange_client import PaperExchangeClient, _crosses, _trigger_direction


def _make_ws_client() -> MagicMock:
    """A ws_client stub that captures subscribe_mark_price handlers by symbol."""
    handlers: dict[str, list] = {}

    def _subscribe_mark_price(symbol, handler):
        handlers.setdefault(symbol, []).append(handler)
        return f"sub_{symbol}"

    ws = MagicMock()
    ws.subscribe_mark_price.side_effect = _subscribe_mark_price
    ws._handlers = handlers
    return ws


async def _push_price(ws: MagicMock, symbol: str, price: str) -> None:
    for handler in ws._handlers.get(symbol, []):
        await handler({"e": "markPriceUpdate", "s": symbol, "p": price})


@pytest.fixture
def delegate() -> AsyncMock:
    d = AsyncMock()
    d.get_candles.return_value = []
    return d


@pytest.fixture
def ws_client() -> MagicMock:
    return _make_ws_client()


@pytest.fixture
async def client(delegate, ws_client) -> PaperExchangeClient:
    c = PaperExchangeClient(
        delegate=delegate,
        ws_client=ws_client,
        symbols=["BTCUSDT"],
        initial_equity=Decimal("10000"),
    )
    fills: list[dict] = []

    async def _handler(msg):
        fills.append(msg)

    c.subscribe_fills(_handler)
    c._test_fills = fills  # type: ignore[attr-defined]
    await c.start()
    return c


class TestTriggerDirection:
    def test_stop_market_sell_triggers_downward(self):
        assert _trigger_direction(OrderType.STOP_MARKET, OrderSide.SELL) == "down"

    def test_stop_market_buy_triggers_upward(self):
        assert _trigger_direction(OrderType.STOP_MARKET, OrderSide.BUY) == "up"

    def test_take_profit_sell_triggers_upward(self):
        assert _trigger_direction(OrderType.TAKE_PROFIT_MARKET, OrderSide.SELL) == "up"

    def test_take_profit_buy_triggers_downward(self):
        assert _trigger_direction(OrderType.TAKE_PROFIT_MARKET, OrderSide.BUY) == "down"

    def test_stop_and_take_profit_are_opposite_for_same_side(self):
        # Regression guard for the exact bug this logic was designed to catch:
        # a stop-loss and a take-profit on the same side must never trigger
        # in the same direction.
        for side in (OrderSide.BUY, OrderSide.SELL):
            assert _trigger_direction(OrderType.STOP_MARKET, side) != _trigger_direction(
                OrderType.TAKE_PROFIT_MARKET, side
            )

    def test_crosses_down(self):
        assert _crosses(Decimal("99"), Decimal("100"), "down") is True
        assert _crosses(Decimal("101"), Decimal("100"), "down") is False

    def test_crosses_up(self):
        assert _crosses(Decimal("101"), Decimal("100"), "up") is True
        assert _crosses(Decimal("99"), Decimal("100"), "up") is False


@pytest.mark.asyncio
class TestMarketOrders:
    async def test_market_order_fills_at_mark_price(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="c1",
        )
        # Fill delivery is deferred via asyncio.create_task — let it run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert len(client._test_fills) == 1
        fill = client._test_fills[0]["o"]
        assert fill["x"] == "TRADE"
        assert fill["c"] == "c1"
        assert Decimal(fill["L"]) == Decimal("50000")
        assert Decimal(fill["l"]) == Decimal("0.1")

    async def test_market_order_before_any_price_does_not_crash_and_does_not_fill(
        self, client
    ):
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="c_no_price",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client._test_fills == []

    async def test_deferred_fill_does_not_race_order_registration(self, client, ws_client):
        """
        place_order's fill must be scheduled (create_task), not delivered
        synchronously — otherwise a caller that registers client_order_id
        bookkeeping *after* place_order returns (as OrderManager.submit_order
        does) would see the fill arrive for an "unknown" order.
        """
        await _push_price(ws_client, "BTCUSDT", "50000")
        registered_before_fill = []

        order = await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="c2",
        )
        # Simulate OrderManager's post-place_order bookkeeping happening here,
        # before the event loop yields to the deferred fill task.
        registered_before_fill.append(order.client_order_id)
        assert client._test_fills == []  # fill must not have landed yet

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(client._test_fills) == 1


@pytest.mark.asyncio
class TestStopAndLimitOrders:
    async def test_stop_market_sell_fills_on_price_drop(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.STOP_MARKET, Decimal("0.1"),
            None, stop_price=Decimal("49000"), client_order_id="sl1",
        )
        await asyncio.sleep(0)
        assert client._test_fills == []  # not triggered yet

        await _push_price(ws_client, "BTCUSDT", "48900")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(client._test_fills) == 1
        assert Decimal(client._test_fills[0]["o"]["L"]) == Decimal("48900")

    async def test_stop_market_sell_does_not_fire_on_price_rise(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.STOP_MARKET, Decimal("0.1"),
            None, stop_price=Decimal("49000"), client_order_id="sl2",
        )
        await _push_price(ws_client, "BTCUSDT", "51000")
        await asyncio.sleep(0)
        assert client._test_fills == []

    async def test_take_profit_market_sell_fills_on_price_rise(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.TAKE_PROFIT_MARKET, Decimal("0.1"),
            None, stop_price=Decimal("51000"), client_order_id="tp1",
        )
        await _push_price(ws_client, "BTCUSDT", "51100")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(client._test_fills) == 1

    async def test_limit_order_fills_at_limit_price_not_mark_price(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.LIMIT, Decimal("0.1"),
            Decimal("49500"), client_order_id="lim1",
        )
        await _push_price(ws_client, "BTCUSDT", "49400")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert Decimal(client._test_fills[0]["o"]["L"]) == Decimal("49500")


@pytest.mark.asyncio
class TestOrderQueries:
    async def test_cancel_order_removes_resting_order(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        order = await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.STOP_MARKET, Decimal("0.1"),
            None, stop_price=Decimal("49000"), client_order_id="c3",
        )
        cancelled = await client.cancel_order("BTCUSDT", order.order_id)
        assert cancelled is True

        await _push_price(ws_client, "BTCUSDT", "48000")
        await asyncio.sleep(0)
        assert client._test_fills == []  # cancelled — must not fill

    async def test_get_order_unknown_raises(self, client):
        with pytest.raises(OrderNotFoundError):
            await client.get_order("BTCUSDT", "does-not-exist")

    async def test_get_open_orders_filters_by_symbol(self, client):
        await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.STOP_MARKET, Decimal("0.1"),
            None, stop_price=Decimal("49000"), client_order_id="c4",
        )
        open_orders = await client.get_open_orders("BTCUSDT")
        assert len(open_orders) == 1
        assert client.get_open_orders is not None  # sanity


@pytest.mark.asyncio
class TestLedgerAndEquity:
    async def test_equity_decreases_by_fee_on_open(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        starting_equity = client.equity
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("1"), None,
            client_order_id="c5",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client.equity < starting_equity  # fee-only impact on open

    async def test_realized_pnl_applied_on_close(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("1"), None,
            client_order_id="open1",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        equity_after_open = client.equity

        await _push_price(ws_client, "BTCUSDT", "51000")
        await client.place_order(
            "BTCUSDT", OrderSide.SELL, OrderType.MARKET, Decimal("1"), None,
            reduce_only=True, client_order_id="close1",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # +1000 gross profit on 1 BTC, minus closing fee, should now be
        # reflected in equity versus right after opening.
        assert client.equity > equity_after_open

    async def test_get_positions_reflects_open_position(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.5"), None,
            client_order_id="c6",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTCUSDT"
        assert positions[0].quantity == Decimal("0.5")

    async def test_get_account_balance_matches_equity(self, client):
        balance = await client.get_account_balance()
        assert balance["USDT"] == client.equity


@pytest.mark.asyncio
class TestBackgroundFillTaskTracking:
    """
    place_order's deferred market-fill task is tracked in
    _background_tasks (not a bare, unreferenced create_task) so it can't
    be silently garbage-collected mid-flight and so a raised exception is
    actually logged rather than only surfacing via asyncio's default
    "Task exception was never retrieved" handler at GC time.
    """

    async def test_task_is_tracked_while_pending(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="track1",
        )
        assert len(client._background_tasks) == 1

    async def test_task_is_removed_from_tracking_once_done(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="track2",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client._background_tasks == set()

    async def test_exception_in_simulate_fill_is_logged_not_swallowed(
        self, client, ws_client, caplog
    ):
        import logging

        client._fill_handler = AsyncMock(side_effect=RuntimeError("handler boom"))
        await _push_price(ws_client, "BTCUSDT", "50000")

        with caplog.at_level(logging.ERROR):
            await client.place_order(
                "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
                client_order_id="track3",
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "Deferred paper fill simulation failed" in caplog.text
        assert client._background_tasks == set()

    async def test_stop_cancels_pending_background_tasks(self, client, ws_client):
        await _push_price(ws_client, "BTCUSDT", "50000")
        await client.place_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.1"), None,
            client_order_id="track4",
        )
        assert len(client._background_tasks) == 1

        await client.stop()  # must not raise, must not hang

        assert client._background_tasks == set()

    async def test_stop_with_no_pending_tasks_is_a_noop(self, client):
        assert client._background_tasks == set()
        await client.stop()  # must not raise
        assert client._background_tasks == set()
