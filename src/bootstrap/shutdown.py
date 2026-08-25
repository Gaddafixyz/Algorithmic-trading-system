"""
Graceful shutdown sequence.

Executes shutdown steps in reverse dependency order:
1. Stop accepting new signals
2. Cancel all open orders
3. Close all positions (configurable)
4. Flush pending data to database
5. Disconnect WebSocket
6. Disconnect REST client
7. Close database
"""

from __future__ import annotations

import logging

from src.bootstrap.app import Application
from src.core.events import SystemShutdownEvent, get_event_bus
from src.core.exceptions import ShutdownError

logger = logging.getLogger(__name__)


async def shutdown(
    app: Application,
    strategy_orchestrator: object | None = None,
    *,
    close_positions: bool = False,
) -> None:
    """
    Execute graceful shutdown.

    Args:
        app: Running Application instance.
        strategy_orchestrator: Active orchestrator to stop.
        close_positions: If True, attempt to close all open positions at market.
    """
    logger.info("=== Trading System Shutdown ===")
    # Each step below is independently try/excepted and never re-raises
    # mid-sequence -- teardown must always run to completion. Failures
    # are collected here instead, and surfaced as a single ShutdownError
    # (if any occurred) only after every step has already run.
    _failures: list[str] = []

    # 0. Announce shutdown. SystemShutdownEvent existed as schema only —
    # published first, before any teardown step, so a subscriber (e.g. a
    # notifier) can still reach the network/DB to report the shutdown is
    # starting, before those connections are torn down below.
    reason = "graceful shutdown requested"
    try:
        await get_event_bus().publish(SystemShutdownEvent(reason=reason))
    except Exception as exc:
        # Must never block teardown — this is best-effort notice only.
        logger.warning("Failed to publish SystemShutdownEvent: %s", exc)

    # 1. Stop strategies
    if strategy_orchestrator is not None:
        try:
            await strategy_orchestrator.stop()  # type: ignore[union-attr]
            logger.info("✓ Strategy orchestrator stopped")
        except Exception as exc:
            logger.error("Error stopping orchestrator: %s", exc)
            _failures.append(f"orchestrator: {exc}")

    # 2. Stop execution engine
    try:
        await app.execution_engine.stop()
        logger.info("✓ Execution engine stopped")
    except Exception as exc:
        logger.error("Error stopping execution engine: %s", exc)
        _failures.append(f"execution engine: {exc}")

    # 2b. Stop background scheduler (equity resync, etc.)
    try:
        await app.scheduler.stop()
        logger.info("✓ Scheduler stopped")
    except Exception as exc:
        logger.error("Error stopping scheduler: %s", exc)
        _failures.append(f"scheduler: {exc}")

    # 3. Cancel all open orders
    try:
        cancelled = await app.order_manager.cancel_all_orders()
        logger.info("✓ Cancelled %d open orders", cancelled)
    except Exception as exc:
        logger.error("Error cancelling orders: %s", exc)
        _failures.append(f"cancel orders: {exc}")

    # 4. Optionally close positions
    #
    # This must place a real reduce-only MARKET order on the exchange, not
    # just update local bookkeeping. Calling position_manager.close_position()
    # directly against a cached mark price (the previous implementation)
    # never touched the exchange at all — it would mark the position closed
    # locally while the real position stayed open and, since step 3 already
    # cancelled its stop-loss/take-profit orders, now completely unprotected.
    # The WebSocket user-data stream is still connected at this point (it's
    # only stopped in step 6 below), so the resulting fill is picked up by
    # FillEngine -> OrderManager -> ExecutionEngine._on_fill/_on_exit_filled
    # exactly like any other reduce-only exit fill, which is what actually
    # calls position_manager.close_position with the real fill price.
    if close_positions:
        from src.core.enums import OrderSide, OrderType
        from src.domain.order import Order

        for pos in app.position_manager.open_positions:
            try:
                instrument = app.instruments.get(pos.symbol.upper())
                qty = instrument.round_quantity(pos.quantity) if instrument else pos.quantity
                close_order = Order(
                    symbol=pos.symbol,
                    side=OrderSide.SELL if pos.is_long else OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=qty,
                    reduce_only=True,
                    signal_id=getattr(pos, "signal_id", None),
                    strategy_id=getattr(pos, "strategy_id", None),
                )
                await app.order_manager.submit_order(close_order)
                logger.info(
                    "Submitted market close order for %s (position_id=%s, qty=%s)",
                    pos.symbol,
                    pos.position_id,
                    qty,
                )
            except Exception as exc:
                logger.error("Error closing position %s: %s", pos.symbol, exc)
                _failures.append(f"close position {pos.symbol}: {exc}")

    # 5. Stop monitoring
    try:
        await app.heartbeat.stop()
        app.watchdog.stop()
        app.dashboard.stop()
        await app.dashboard.stop_ws()
        logger.info("✓ Monitoring stopped")
    except Exception as exc:
        logger.error("Error stopping monitoring: %s", exc)
        _failures.append(f"monitoring: {exc}")

    # 6. Disconnect WebSocket and clean up listen key
    try:
        await app.ws_client.stop()
        logger.info("✓ WebSocket disconnected")
    except Exception as exc:
        logger.error("Error disconnecting WebSocket: %s", exc)
        _failures.append(f"websocket disconnect: {exc}")

    if app.paper_exchange_client is not None:
        try:
            await app.paper_exchange_client.stop()
            logger.info(
                "✓ Paper exchange client stopped (final equity: %s USDT)",
                app.paper_exchange_client.equity,
            )
        except Exception as exc:
            logger.error("Error stopping paper exchange client: %s", exc)
            _failures.append(f"paper exchange client: {exc}")

    # Delete the listen key so Binance can free server-side resources.
    # Must happen after WebSocket is stopped (key is no longer needed).
    if app._listen_key:
        try:
            await app.rest_client.delete_listen_key(app._listen_key)
            app._listen_key = None
            logger.info("✓ Listen key deleted")
        except Exception as exc:
            logger.warning("Failed to delete listen key (non-critical): %s", exc)

    # 7. Disconnect REST
    try:
        await app.rest_client.disconnect()
        logger.info("✓ REST client disconnected")
    except Exception as exc:
        logger.error("Error disconnecting REST: %s", exc)
        _failures.append(f"REST disconnect: {exc}")

    # 8. Close database
    try:
        await app.db.disconnect()
        logger.info("✓ Database disconnected")
    except Exception as exc:
        logger.error("Error closing database: %s", exc)
        _failures.append(f"database close: {exc}")

    if _failures:
        logger.error("=== Shutdown complete with %d error(s) ===", len(_failures))
        raise ShutdownError(
            f"Shutdown completed with {len(_failures)} error(s): " + "; ".join(_failures)
        )

    logger.info("=== Shutdown complete ===")
