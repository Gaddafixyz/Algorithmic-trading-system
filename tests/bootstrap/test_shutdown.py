"""Unit tests for src.bootstrap.shutdown.shutdown().

Uses a duck-typed fake Application (MagicMock/AsyncMock attributes) so
each teardown step's ordering and error-isolation can be verified without
constructing a real Application graph. Each step is independently
try/except-wrapped in the implementation, so the central behavior under
test is: one failing step must never prevent the rest from running.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bootstrap.shutdown import shutdown
from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, SystemShutdownEvent, reset_event_bus, set_event_bus
from src.core.exceptions import ShutdownError


@pytest.fixture
def isolated_event_bus():  # type: ignore[no-untyped-def]
    reset_event_bus()
    bus = EventBus()
    set_event_bus(bus)
    yield bus
    reset_event_bus()


def _fake_app() -> MagicMock:
    app = MagicMock()
    app.execution_engine.stop = AsyncMock()
    app.scheduler.stop = AsyncMock()
    app.order_manager.cancel_all_orders = AsyncMock(return_value=0)
    app.heartbeat.stop = AsyncMock()
    app.watchdog.stop = MagicMock()
    app.dashboard.stop = MagicMock()
    app.dashboard.stop_ws = AsyncMock()
    app.ws_client.stop = AsyncMock()
    app.rest_client.delete_listen_key = AsyncMock()
    app.rest_client.disconnect = AsyncMock()
    app.db.disconnect = AsyncMock()
    app.position_manager.open_positions = []
    app.instruments = {}
    app._listen_key = None
    # None by default (live wiring); tests exercising the paper-mode
    # teardown path override this — see TestPaperExchangeClientTeardown.
    app.paper_exchange_client = None
    return app


class TestShutdownAnnouncement:
    async def test_publishes_system_shutdown_event_before_any_teardown(
        self, isolated_event_bus: EventBus
    ) -> None:
        seen: list[str] = []

        async def _on_shutdown(event: object) -> None:
            seen.append("event_published")

        isolated_event_bus.subscribe(SystemShutdownEvent, _on_shutdown)
        app = _fake_app()
        app.execution_engine.stop = AsyncMock(side_effect=lambda: seen.append("engine_stopped"))

        await shutdown(app)

        assert seen[0] == "event_published"

    async def test_publish_failure_does_not_block_teardown(
        self, isolated_event_bus: EventBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.bootstrap.shutdown.get_event_bus",
            MagicMock(side_effect=RuntimeError("bus unavailable")),
        )
        app = _fake_app()
        await shutdown(app)  # must not raise
        app.db.disconnect.assert_awaited_once()


class TestOrchestratorStop:
    async def test_stops_orchestrator_when_provided(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        orchestrator = MagicMock()
        orchestrator.stop = AsyncMock()
        await shutdown(app, orchestrator)
        orchestrator.stop.assert_awaited_once()

    async def test_orchestrator_stop_failure_does_not_block_teardown(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        orchestrator = MagicMock()
        orchestrator.stop = AsyncMock(side_effect=RuntimeError("boom"))
        # A tracked failure -> ShutdownError raised at the very end, but
        # only after every remaining step (db.disconnect included) has
        # already run -- see TestShutdownErrorAggregation for the
        # dedicated raise-behavior coverage.
        with pytest.raises(ShutdownError, match="orchestrator"):
            await shutdown(app, orchestrator)
        app.db.disconnect.assert_awaited_once()

    async def test_no_orchestrator_is_a_noop(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        await shutdown(app, None)  # must not raise


class TestErrorIsolationBetweenSteps:
    """Each teardown step is independently wrapped; a failure at any one
    step must never prevent later steps from running."""

    @pytest.mark.parametrize(
        "failing_attr",
        [
            "execution_engine.stop",
            "scheduler.stop",
            "order_manager.cancel_all_orders",
            "heartbeat.stop",
            "ws_client.stop",
            "rest_client.disconnect",
        ],
    )
    async def test_step_failure_still_reaches_database_disconnect(
        self, isolated_event_bus: EventBus, failing_attr: str
    ) -> None:
        app = _fake_app()
        obj, attr = app, failing_attr
        *path, leaf = failing_attr.split(".")
        for part in path:
            obj = getattr(obj, part)
        setattr(obj, leaf, AsyncMock(side_effect=RuntimeError("boom")))

        # Every one of these is a tracked failure -> ShutdownError raised
        # at the end, but not until database disconnect (the very last
        # step) has already run.
        with pytest.raises(ShutdownError):
            await shutdown(app)

        app.db.disconnect.assert_awaited_once()

    async def test_watchdog_or_dashboard_stop_failure_still_reaches_ws_disconnect(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app.watchdog.stop = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(ShutdownError, match="monitoring"):
            await shutdown(app)
        app.ws_client.stop.assert_awaited_once()
        app.db.disconnect.assert_awaited_once()


class TestClosePositions:
    """
    close_positions=True must place a real reduce-only MARKET order via
    OrderManager — never call position_manager.close_position() directly
    against a cached mark price. That would only update local bookkeeping
    and leave the real exchange position open (and, since step 3 already
    cancelled its SL/TP orders, unprotected). The resulting fill is picked
    up normally by FillEngine/ExecutionEngine while the WS is still
    connected, which is what actually closes the position locally.
    """

    async def test_close_positions_false_never_touches_open_positions(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        pos = MagicMock()
        app.position_manager.open_positions = [pos]
        app.order_manager.submit_order = AsyncMock()

        await shutdown(app, close_positions=False)

        app.order_manager.submit_order.assert_not_awaited()

    async def test_close_positions_true_submits_reduce_only_market_order(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        pos = MagicMock(
            position_id="pos-1", symbol="BTCUSDT", quantity=Decimal("0.5"), is_long=True,
        )
        app.position_manager.open_positions = [pos]
        app.instruments = {}
        app.order_manager.submit_order = AsyncMock()

        await shutdown(app, close_positions=True)

        app.order_manager.submit_order.assert_awaited_once()
        submitted = app.order_manager.submit_order.await_args.args[0]
        assert submitted.symbol == "BTCUSDT"
        assert submitted.order_type == OrderType.MARKET
        assert submitted.reduce_only is True
        assert submitted.side == OrderSide.SELL  # long position closes with a SELL
        assert submitted.quantity == Decimal("0.5")

    async def test_close_positions_true_uses_buy_side_for_short_position(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        pos = MagicMock(
            position_id="pos-1", symbol="BTCUSDT", quantity=Decimal("0.5"), is_long=False,
        )
        app.position_manager.open_positions = [pos]
        app.instruments = {}
        app.order_manager.submit_order = AsyncMock()

        await shutdown(app, close_positions=True)

        submitted = app.order_manager.submit_order.await_args.args[0]
        assert submitted.side == OrderSide.BUY  # short position closes with a BUY

    async def test_one_position_failing_to_close_does_not_block_the_rest(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        pos1 = MagicMock(
            position_id="pos-1", symbol="BTCUSDT", quantity=Decimal("0.5"), is_long=True,
        )
        pos2 = MagicMock(
            position_id="pos-2", symbol="ETHUSDT", quantity=Decimal("1"), is_long=True,
        )
        app.position_manager.open_positions = [pos1, pos2]
        app.instruments = {}
        app.order_manager.submit_order = AsyncMock(
            side_effect=[RuntimeError("boom"), MagicMock()]
        )

        with pytest.raises(ShutdownError, match="BTCUSDT"):
            await shutdown(app, close_positions=True)

        assert app.order_manager.submit_order.await_count == 2


class TestListenKeyCleanup:
    async def test_deletes_listen_key_when_present(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        app._listen_key = "abc123listenkey"

        await shutdown(app)

        app.rest_client.delete_listen_key.assert_awaited_once_with("abc123listenkey")
        assert app._listen_key is None

    async def test_no_listen_key_is_a_noop(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        app._listen_key = None

        await shutdown(app)

        app.rest_client.delete_listen_key.assert_not_awaited()

    async def test_listen_key_deletion_happens_after_websocket_stop(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app._listen_key = "abc123"
        order: list[str] = []
        app.ws_client.stop = AsyncMock(side_effect=lambda: order.append("ws_stopped"))
        app.rest_client.delete_listen_key = AsyncMock(
            side_effect=lambda key: order.append("listen_key_deleted")
        )

        await shutdown(app)

        assert order == ["ws_stopped", "listen_key_deleted"]

    async def test_listen_key_deletion_failure_is_non_fatal(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app._listen_key = "abc123"
        app.rest_client.delete_listen_key = AsyncMock(side_effect=RuntimeError("boom"))

        await shutdown(app)  # must not raise

        app.rest_client.disconnect.assert_awaited_once()
        app.db.disconnect.assert_awaited_once()


class TestFullSequence:
    async def test_happy_path_calls_every_step_once(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        orchestrator = MagicMock()
        orchestrator.stop = AsyncMock()

        await shutdown(app, orchestrator)

        orchestrator.stop.assert_awaited_once()
        app.execution_engine.stop.assert_awaited_once()
        app.scheduler.stop.assert_awaited_once()
        app.order_manager.cancel_all_orders.assert_awaited_once()
        app.heartbeat.stop.assert_awaited_once()
        app.watchdog.stop.assert_called_once()
        app.dashboard.stop.assert_called_once()
        app.dashboard.stop_ws.assert_awaited_once()
        app.ws_client.stop.assert_awaited_once()
        app.rest_client.disconnect.assert_awaited_once()
        app.db.disconnect.assert_awaited_once()


class TestShutdownErrorAggregation:
    """
    ShutdownError is raised once, after every step has already run,
    summarizing whatever failed -- it must never be raised mid-sequence
    (that would defeat the whole point of independently try/excepting
    each step) and non-critical steps (SystemShutdownEvent publish,
    listen-key deletion) must never contribute to it.
    """

    async def test_happy_path_does_not_raise(self, isolated_event_bus: EventBus) -> None:
        app = _fake_app()
        await shutdown(app)  # must not raise

    async def test_multiple_failures_are_all_included_in_the_message(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app.execution_engine.stop = AsyncMock(side_effect=RuntimeError("engine boom"))
        app.ws_client.stop = AsyncMock(side_effect=RuntimeError("ws boom"))

        with pytest.raises(ShutdownError) as exc_info:
            await shutdown(app)

        message = str(exc_info.value)
        assert "engine boom" in message
        assert "ws boom" in message
        assert "2 error" in message

    async def test_non_critical_step_failures_never_raise(
        self, isolated_event_bus: EventBus, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SystemShutdownEvent publish failure and listen-key deletion
        # failure are both explicitly non-critical (logger.warning, not
        # logger.error) and must never surface as a ShutdownError even
        # though every other step succeeds.
        monkeypatch.setattr(
            "src.bootstrap.shutdown.get_event_bus",
            MagicMock(side_effect=RuntimeError("bus unavailable")),
        )
        app = _fake_app()
        app._listen_key = "abc123"
        app.rest_client.delete_listen_key = AsyncMock(side_effect=RuntimeError("boom"))

        await shutdown(app)  # must not raise

    async def test_database_close_failure_alone_still_raises(
        self, isolated_event_bus: EventBus
    ) -> None:
        # The very last step failing is still a tracked failure, even
        # though there's nothing after it left to protect.
        app = _fake_app()
        app.db.disconnect = AsyncMock(side_effect=RuntimeError("db boom"))

        with pytest.raises(ShutdownError, match="database close"):
            await shutdown(app)


class TestPaperExchangeClientTeardown:
    async def test_stops_paper_exchange_client_when_present(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app.paper_exchange_client = MagicMock()
        app.paper_exchange_client.stop = AsyncMock()
        app.paper_exchange_client.equity = Decimal("10250")

        await shutdown(app)

        app.paper_exchange_client.stop.assert_awaited_once()

    async def test_paper_client_stop_failure_does_not_block_rest_of_teardown(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        app.paper_exchange_client = MagicMock()
        app.paper_exchange_client.stop = AsyncMock(side_effect=RuntimeError("boom"))
        app.paper_exchange_client.equity = Decimal("10250")

        with pytest.raises(ShutdownError, match="paper exchange client"):
            await shutdown(app)

        app.rest_client.disconnect.assert_awaited_once()
        app.db.disconnect.assert_awaited_once()

    async def test_no_paper_client_is_a_noop(
        self, isolated_event_bus: EventBus
    ) -> None:
        app = _fake_app()
        assert app.paper_exchange_client is None

        await shutdown(app)  # must not raise
