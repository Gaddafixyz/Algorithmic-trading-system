"""
Standalone notifications worker — consumes trade/risk events over Redis
Streams and fans them out to configured notification channels.

This is the missing consumer side of the Redis Streams event bus.
`src/bootstrap/app.py` already mirrors every in-process event onto
`events:{EventType}` Redis Streams via `RedisEventBus` whenever
`EVENT_BUS_BACKEND=redis` is set (see ROADMAP.md's Redis Streams entry),
and `src/core/events_redis.py::RedisEventConsumer` exists specifically to
tail those streams from a separate process — but nothing in this codebase
ever instantiated it. That leaves the publish side fully wired and the
consume side built but never run: notifications defined in
`Application._setup_notifications` only fire for the single in-process
event bus, which means a horizontally-scaled deployment (multiple trading
processes per ROADMAP's "multi-process/multi-host" goal for this feature)
had no way to centralize notifications across them.

This script is that consumer. Run it as its own process/container,
separate from the trading engine:

    python scripts/notifications_worker.py \\
        --group notifications-worker \\
        --event-types OrderFilledEvent,PositionClosedEvent,RiskLimitBreachedEvent

Multiple instances with the same --group load-balance the stream (Redis
consumer groups) rather than each seeing every event twice, so this can
be scaled out independently of the trading engine itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config import Settings, get_settings
from src.core.enums import AlertSeverity, CircuitBreakerState
from src.core.events import (
    AlertFiredEvent,
    BaseEvent,
    CircuitBreakerStateChangedEvent,
    DailyLossLimitReachedEvent,
    OrderFilledEvent,
    PositionClosedEvent,
    RiskLimitBreachedEvent,
)
from src.core.events_redis import RedisEventConsumer
from src.monitoring.telemetry import configure_logging
from src.notifications.notifier import Notifier

logger = logging.getLogger(__name__)

# Event types this worker knows how to format. RedisEventConsumer will also
# happily forward anything else if requested via --event-types, but
# format_event() falls back to a generic one-liner for those.
DEFAULT_EVENT_TYPES = [
    "OrderFilledEvent",
    "PositionClosedEvent",
    "RiskLimitBreachedEvent",
    "CircuitBreakerStateChangedEvent",
    "DailyLossLimitReachedEvent",
    "AlertFiredEvent",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Redis Streams notifications worker")
    p.add_argument(
        "--group",
        default="notifications-worker",
        help="Redis consumer group name. Instances sharing a group load-balance "
        "the stream instead of each seeing every event.",
    )
    p.add_argument(
        "--consumer-name",
        default=None,
        help="Unique name for this instance within the group. "
        "Defaults to 'notifications-worker-<pid>'.",
    )
    p.add_argument(
        "--event-types",
        default=",".join(DEFAULT_EVENT_TYPES),
        help="Comma-separated event type names to subscribe to.",
    )
    p.add_argument(
        "--block-ms",
        type=int,
        default=5_000,
        help="How long each Redis XREADGROUP call blocks waiting for new events.",
    )
    return p.parse_args()


def build_notifier(settings: Settings) -> Notifier:
    """
    Wire up configured notification channels from settings.

    Mirrors Application._setup_notifications in src/bootstrap/app.py.
    Kept as a small, self-contained duplicate here rather than importing
    from Application, since building an Application pulls in the whole
    trading engine (exchange clients, DB, execution stack) that this
    notification-only process has no use for and shouldn't need to
    authenticate against.
    """
    notifier = Notifier()
    n = settings.notifications
    channels_enabled = False

    if n.telegram_bot_token and n.telegram_chat_id:
        from src.notifications.telegram import TelegramNotifier

        notifier.add_channel(
            TelegramNotifier(
                bot_token=n.telegram_bot_token.get_secret_value(),
                chat_id=n.telegram_chat_id,
            )
        )
        channels_enabled = True
        logger.info("Telegram notifications enabled")

    if n.discord_webhook_url:
        from src.notifications.discord import DiscordNotifier

        notifier.add_channel(DiscordNotifier(n.discord_webhook_url.get_secret_value()))
        channels_enabled = True
        logger.info("Discord notifications enabled")

    if n.slack_webhook_url:
        from src.notifications.slack import SlackNotifier

        notifier.add_channel(SlackNotifier(n.slack_webhook_url.get_secret_value()))
        channels_enabled = True
        logger.info("Slack notifications enabled")

    if n.email_enabled:
        from src.notifications.email import EmailNotifier

        notifier.add_channel(
            EmailNotifier(
                smtp_host=n.smtp_host,  # type: ignore[arg-type]
                smtp_port=n.smtp_port,
                username=n.smtp_user,  # type: ignore[arg-type]
                password=n.smtp_password.get_secret_value(),  # type: ignore[union-attr]
                recipient=n.alert_email,  # type: ignore[arg-type]
            )
        )
        channels_enabled = True
        logger.info("Email notifications enabled (%s)", n.alert_email)

    if not channels_enabled:
        logger.warning(
            "No notification channels configured — events will be logged but not "
            "delivered anywhere. Set TELEGRAM_BOT_TOKEN/DISCORD_WEBHOOK_URL/"
            "SLACK_WEBHOOK_URL/SMTP_* env vars to enable delivery."
        )

    return notifier


def format_event(event: BaseEvent) -> tuple[str, AlertSeverity]:
    """Render an event into a (message, severity) pair for Notifier.send()."""
    if isinstance(event, OrderFilledEvent):
        partial = " (partial)" if event.is_partial else ""
        return (
            f"✅ Fill{partial}: {event.symbol} {event.side.value} "
            f"{event.filled_quantity} @ {event.fill_price} "
            f"(fee {event.fee} {event.fee_asset})",
            AlertSeverity.INFO,
        )

    if isinstance(event, PositionClosedEvent):
        pnl_emoji = "🟢" if event.realized_pnl >= 0 else "🔴"
        return (
            f"{pnl_emoji} Position Closed: {event.symbol} {event.side.value} "
            f"{event.entry_price} → {event.exit_price} "
            f"| PnL: {event.realized_pnl} (fees {event.fees})",
            AlertSeverity.INFO,
        )

    if isinstance(event, RiskLimitBreachedEvent):
        return (
            f"⚠️ Risk Limit Breached: {event.limit_name} "
            f"({event.current_value} vs limit {event.limit_value})",
            event.severity,
        )

    if isinstance(event, CircuitBreakerStateChangedEvent):
        severity = (
            AlertSeverity.CRITICAL
            if event.new_state == CircuitBreakerState.OPEN
            else AlertSeverity.WARNING
        )
        return (
            f"🔌 Circuit Breaker: {event.previous_state.value} → "
            f"{event.new_state.value} ({event.reason})",
            severity,
        )

    if isinstance(event, DailyLossLimitReachedEvent):
        return (
            f"🛑 Daily Loss Limit Reached: {event.daily_pnl} " f"(limit {event.limit_pct}%)",
            AlertSeverity.CRITICAL,
        )

    if isinstance(event, AlertFiredEvent):
        return (f"🔔 {event.alert_name}: {event.message}", event.severity)

    # Unrecognized event type someone explicitly subscribed to via
    # --event-types: still forward it rather than silently dropping it.
    return (f"Event: {type(event).__name__} — {event.model_dump_json()}", AlertSeverity.INFO)


async def run_worker(
    settings: Settings,
    group: str,
    consumer_name: str,
    event_types: list[str],
    block_ms: int,
) -> None:
    notifier = build_notifier(settings)
    consumer = RedisEventConsumer(
        redis_url=settings.redis.url,
        group=group,
        event_types=event_types,
        consumer_name=consumer_name,
        block_ms=block_ms,
    )

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    async def consume() -> None:
        async for event in consumer.stream():
            try:
                message, severity = format_event(event)
                await notifier.send(message, severity)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to process event %s", type(event).__name__)

    logger.info(
        "Notifications worker started: group=%s consumer=%s event_types=%s",
        group,
        consumer_name,
        event_types,
    )
    consume_task = asyncio.create_task(consume(), name="notifications-worker.consume")
    try:
        await shutdown_event.wait()
    finally:
        logger.info("Shutdown requested — stopping notifications worker")
        consume_task.cancel()
        try:
            await consume_task
        except asyncio.CancelledError:
            pass
        await consumer.close()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    configure_logging(log_level=settings.log_level, json_logs=settings.is_production)

    consumer_name = args.consumer_name or f"notifications-worker-{os.getpid()}"
    event_types = [t.strip() for t in args.event_types.split(",") if t.strip()]

    try:
        asyncio.run(run_worker(settings, args.group, consumer_name, event_types, args.block_ms))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(0)
    except Exception:
        logger.critical("Notifications worker crashed", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
