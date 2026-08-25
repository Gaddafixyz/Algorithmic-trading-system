"""Unit tests for src.exchange.binance.orders.BinanceOrderManager."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.core.enums import InstrumentType, OrderSide, OrderStatus, OrderType
from src.core.exceptions import InvalidOrderError
from src.domain.instrument import Instrument
from src.domain.order import Order
from src.exchange.binance.orders import BinanceOrderManager
from src.exchange.binance.rest import BinanceFuturesRESTClient


def make_instrument() -> Instrument:
    return Instrument(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        instrument_type=InstrumentType.PERPETUAL,
        tick_size=Decimal("0.01"),
        min_price=Decimal("0.01"),
        max_price=Decimal("1000000"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
        min_notional=Decimal("5"),
        max_leverage=125,
        default_leverage=10,
    )


def make_order(status: OrderStatus = OrderStatus.OPEN) -> Order:
    order = Order(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.1"),
    )
    if status != OrderStatus.PENDING:
        order.transition_to(OrderStatus.SUBMITTED)
        if status != OrderStatus.SUBMITTED:
            order.transition_to(status)
    return order


@pytest.fixture
def rest_client() -> AsyncMock:
    return AsyncMock(spec=BinanceFuturesRESTClient)


@pytest.fixture
def manager(rest_client: AsyncMock) -> BinanceOrderManager:
    return BinanceOrderManager(rest_client)


class TestPlaceMarketOrder:
    async def test_rounds_quantity_and_forwards_to_the_client(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_market_order("BTCUSDT", OrderSide.BUY, Decimal("0.12345"), instrument)

        rest_client.place_order.assert_awaited_once()
        _, kwargs = rest_client.place_order.call_args
        assert kwargs["quantity"] == Decimal("0.123")  # rounded to step_size=0.001
        assert kwargs["order_type"] == OrderType.MARKET

    async def test_raises_on_quantity_validation_errors(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        instrument = make_instrument()
        with pytest.raises(InvalidOrderError, match="Invalid order params"):
            await manager.place_market_order(
                "BTCUSDT",
                OrderSide.BUY,
                Decimal("0.0001"),
                instrument,  # below min_qty
            )
        rest_client.place_order.assert_not_awaited()

    async def test_notional_only_errors_are_ignored_for_market_orders(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        # Market orders have no real price to check notional against;
        # the source explicitly filters out "Notional" errors for this
        # reason (documented in its own comment) rather than raising.
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()
        # A tiny quantity that's still >= min_qty but would fail a
        # notional check against a real price -- since we pass
        # min_price*2 as an estimate, this should not raise as long as
        # quantity itself is valid.
        await manager.place_market_order("BTCUSDT", OrderSide.BUY, Decimal("0.01"), instrument)
        rest_client.place_order.assert_awaited_once()


class TestPlaceLimitOrder:
    async def test_rounds_quantity_and_price_and_forwards_to_the_client(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_limit_order(
            "BTCUSDT", OrderSide.BUY, Decimal("0.12345"), Decimal("50000.005"), instrument
        )

        _, kwargs = rest_client.place_order.call_args
        assert kwargs["quantity"] == Decimal("0.123")
        assert kwargs["price"] == Decimal("50000.00")  # rounded to tick_size=0.01
        assert kwargs["order_type"] == OrderType.LIMIT

    async def test_raises_on_validation_errors_including_price(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        instrument = make_instrument()
        with pytest.raises(InvalidOrderError, match="Invalid order params for BTCUSDT"):
            await manager.place_limit_order(
                "BTCUSDT", OrderSide.BUY, Decimal("0.1"), Decimal("0"), instrument
            )
        rest_client.place_order.assert_not_awaited()

    async def test_forwards_a_custom_time_in_force(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()
        await manager.place_limit_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            Decimal("50000"),
            instrument,
            time_in_force="IOC",
        )
        _, kwargs = rest_client.place_order.call_args
        assert kwargs["time_in_force"] == "IOC"


class TestPlaceStopMarket:
    async def test_forwards_stop_price_and_defaults_reduce_only_true(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_stop_market(
            "BTCUSDT", OrderSide.SELL, Decimal("0.1"), Decimal("49000"), instrument
        )

        _, kwargs = rest_client.place_order.call_args
        assert kwargs["order_type"] == OrderType.STOP_MARKET
        assert kwargs["stop_price"] == Decimal("49000")
        assert kwargs["reduce_only"] is True


class TestPlaceTakeProfitMarket:
    async def test_forwards_stop_price_as_the_target(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_take_profit_market(
            "BTCUSDT", OrderSide.SELL, Decimal("0.1"), Decimal("52000"), instrument
        )

        _, kwargs = rest_client.place_order.call_args
        assert kwargs["order_type"] == OrderType.TAKE_PROFIT_MARKET
        assert kwargs["stop_price"] == Decimal("52000")


class TestPlaceBracketOrder:
    async def test_market_entry_places_all_three_legs_with_opposite_exit_side(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        result = await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
        )

        assert set(result.keys()) == {"entry", "stop_loss", "take_profit"}
        assert rest_client.place_order.await_count == 3
        calls = rest_client.place_order.call_args_list
        assert calls[0].kwargs["order_type"] == OrderType.MARKET
        assert calls[0].kwargs["side"] == OrderSide.BUY
        assert calls[1].kwargs["order_type"] == OrderType.STOP_MARKET
        assert calls[1].kwargs["side"] == OrderSide.SELL
        assert calls[1].kwargs["reduce_only"] is True
        assert calls[2].kwargs["order_type"] == OrderType.TAKE_PROFIT_MARKET
        assert calls[2].kwargs["side"] == OrderSide.SELL
        assert calls[2].kwargs["reduce_only"] is True

    async def test_limit_entry_used_when_entry_price_given(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
            entry_price=Decimal("50500"),
        )

        first_call = rest_client.place_order.call_args_list[0]
        assert first_call.kwargs["order_type"] == OrderType.LIMIT
        assert first_call.kwargs["price"] == Decimal("50500")

    async def test_sell_side_entry_uses_buy_side_exits(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.SELL,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("52000"),
            take_profit_price=Decimal("49000"),
        )

        calls = rest_client.place_order.call_args_list
        assert calls[1].kwargs["side"] == OrderSide.BUY
        assert calls[2].kwargs["side"] == OrderSide.BUY

    async def test_entry_failure_propagates_and_places_no_exit_legs(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.side_effect = RuntimeError("rejected")
        instrument = make_instrument()

        with pytest.raises(RuntimeError, match="rejected"):
            await manager.place_bracket_order(
                "BTCUSDT",
                OrderSide.BUY,
                Decimal("0.1"),
                instrument,
                stop_price=Decimal("49000"),
                take_profit_price=Decimal("52000"),
            )
        assert rest_client.place_order.await_count == 1

    async def test_stop_loss_failure_returns_only_entry(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.side_effect = [
            make_order(),
            RuntimeError("stop rejected"),
        ]
        instrument = make_instrument()

        result = await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
        )

        assert set(result.keys()) == {"entry"}
        assert rest_client.place_order.await_count == 2

    async def test_take_profit_failure_returns_entry_and_stop(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.side_effect = [
            make_order(),
            make_order(),
            RuntimeError("tp rejected"),
        ]
        instrument = make_instrument()

        result = await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
        )

        assert set(result.keys()) == {"entry", "stop_loss"}
        assert rest_client.place_order.await_count == 3

    async def test_client_order_id_prefix_applied_per_leg(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.place_order.return_value = make_order()
        instrument = make_instrument()

        await manager.place_bracket_order(
            "BTCUSDT",
            OrderSide.BUY,
            Decimal("0.1"),
            instrument,
            stop_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
            client_order_id_prefix="sig123",
        )

        calls = rest_client.place_order.call_args_list
        assert calls[0].kwargs["client_order_id"] == "sig123_entry"
        assert calls[1].kwargs["client_order_id"] == "sig123_sl"
        assert calls[2].kwargs["client_order_id"] == "sig123_tp"


class TestPollOrderUntilTerminal:
    async def test_returns_immediately_once_terminal(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.get_order.return_value = make_order(OrderStatus.FILLED)
        result = await manager.poll_order_until_terminal(
            "BTCUSDT", "123", poll_interval=0.001, max_polls=5
        )
        assert result.status == OrderStatus.FILLED
        rest_client.get_order.assert_awaited_once()

    async def test_polls_multiple_times_until_terminal(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        call_count = {"n": 0}

        async def side_effect(symbol: str, order_id: str) -> Order:
            call_count["n"] += 1
            if call_count["n"] < 3:
                return make_order(OrderStatus.OPEN)
            return make_order(OrderStatus.FILLED)

        rest_client.get_order.side_effect = side_effect
        result = await manager.poll_order_until_terminal(
            "BTCUSDT", "123", poll_interval=0.001, max_polls=10
        )
        assert result.status == OrderStatus.FILLED
        assert call_count["n"] == 3

    async def test_raises_timeout_error_after_exhausting_max_polls(
        self, manager: BinanceOrderManager, rest_client: AsyncMock
    ) -> None:
        rest_client.get_order.return_value = make_order(OrderStatus.OPEN)  # never terminal
        with pytest.raises(TimeoutError, match="did not reach terminal state"):
            await manager.poll_order_until_terminal(
                "BTCUSDT", "123", poll_interval=0.001, max_polls=3
            )
        assert rest_client.get_order.await_count == 3
