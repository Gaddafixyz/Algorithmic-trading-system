"""
Redis Streams event-bus mirror.

The default ``EventBus`` (``core/events.py``) is a great fit for
in-process pub/sub but, by design, is invisible outside the process:
nothing running in a second container can see a ``PositionOpenedEvent``.
As the system grows (a separate notifications worker, a separate
analytics/replay service, a second trading process on a different
exchange), that boundary becomes a real limitation.

``RedisEventBus`` is a drop-in ``EventBus`` subclass: every ``publish()``
still dispatches to local, in-process handlers exactly as before (nothing
about existing call sites changes), and *additionally* mirrors the event
onto a Redis Stream so other processes can tail it with
``RedisEventConsumer``.

This intentionally does NOT turn the whole system into a distributed
event-sourcing architecture in one step — that would mean every
subscriber needs idempotency, ordering, and replay handling it doesn't
have today. It's the first, low-risk rung of that ladder: observability
and fan-out across process boundaries, with the existing in-process
semantics completely unchanged.

Usage::

    from src.core.events_redis import RedisEventBus, RedisEventConsumer

    bus = RedisEventBus(redis_url=settings.redis.url)
    bus.subscribe(OrderFilledEvent, my_handler)   # same as EventBus
    await bus.publish(some_event)                  # local + mirrored

    # In a separate process:
    consumer = RedisEventConsumer(redis_url=settings.redis.url, group="notifications")
    async for event in consumer.stream():
        ...
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from src.core.events import BaseEvent, EventBus

logger = logging.getLogger(__name__)

STREAM_MAXLEN_DEFAULT = 100_000  # approximate cap; Redis trims with '~'


def _event_registry() -> dict[str, type[BaseEvent]]:
    """
    Map event class names to classes for deserialization.

    Built lazily (not at import time) to avoid a circular import between
    ``events.py`` and this module, and so any events added later are
    picked up automatically.
    """
    import src.core.events as events_module

    return {
        name: obj
        for name, obj in vars(events_module).items()
        if isinstance(obj, type) and issubclass(obj, BaseEvent)
    }


class RedisEventBus(EventBus):
    """
    ``EventBus`` subclass that mirrors every published event onto a Redis
    Stream (one stream per event type: ``events:{EventTypeName}``) in
    addition to normal in-process dispatch.

    If Redis is unreachable, mirroring failures are logged and swallowed
    — a Redis outage must never take down local, in-process event
    dispatch (order fills, risk checks, etc. cannot depend on it).
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        stream_maxlen: int = STREAM_MAXLEN_DEFAULT,
    ) -> None:
        super().__init__()
        self._redis_url = redis_url
        self._stream_maxlen = stream_maxlen
        self._redis: Any = None
        self._mirror_errors = 0

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as redis

            self._redis = redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def publish(self, event: BaseEvent) -> None:
        """Dispatch locally (unchanged behavior), then mirror to Redis."""
        await super().publish(event)
        await self._mirror(event)

    async def _mirror(self, event: BaseEvent) -> None:
        try:
            r = await self._get_redis()
            stream_key = f"events:{event.event_type}"
            payload = {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "data": event.model_dump_json(),
            }
            await r.xadd(stream_key, payload, maxlen=self._stream_maxlen, approximate=True)
        except Exception as exc:
            self._mirror_errors += 1
            logger.warning(
                "RedisEventBus: failed to mirror %s (error #%d): %s",
                event.event_type,
                self._mirror_errors,
                exc,
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def stats(self) -> dict[str, int]:
        base = super().stats
        base["mirror_errors"] = self._mirror_errors
        return base


class RedisEventConsumer:
    """
    Tails one or more event streams from a separate process using a Redis
    consumer group (so multiple instances of the same service load-balance
    the stream rather than each seeing every event twice).

    Example — a standalone notifications worker that only cares about
    fills and risk breaches::

        consumer = RedisEventConsumer(
            redis_url=settings.redis.url,
            group="notifications-worker",
            event_types=["OrderFilledEvent", "RiskLimitBreachedEvent"],
        )
        async for event in consumer.stream():
            await notifier.notify(...)
    """

    def __init__(
        self,
        redis_url: str,
        group: str,
        event_types: list[str] | None = None,
        consumer_name: str = "consumer-1",
        block_ms: int = 5_000,
    ) -> None:
        self._redis_url = redis_url
        self._group = group
        self._event_types = event_types
        self._consumer_name = consumer_name
        self._block_ms = block_ms
        self._redis: Any = None
        self._registry = _event_registry()

    async def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio as redis

            self._redis = redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def _ensure_groups(self, streams: list[str]) -> None:
        r = await self._get_redis()
        for stream in streams:
            try:
                await r.xgroup_create(stream, self._group, id="0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def _deserialize(self, event_type: str, data_json: str) -> BaseEvent | None:
        cls = self._registry.get(event_type)
        if cls is None:
            logger.warning("RedisEventConsumer: unknown event type '%s', skipping", event_type)
            return None
        return cls.model_validate_json(data_json)

    async def stream(self) -> AsyncIterator[BaseEvent]:
        """Yield events from Redis as they arrive, forever, until cancelled."""
        r = await self._get_redis()
        event_types = self._event_types or list(self._registry.keys())
        streams = [f"events:{t}" for t in event_types]
        await self._ensure_groups(streams)

        stream_ids = dict.fromkeys(streams, ">")
        while True:
            try:
                results = await r.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams=stream_ids,
                    count=100,
                    block=self._block_ms,
                )
            except Exception as exc:
                logger.warning("RedisEventConsumer read error: %s", exc)
                continue

            for stream_key, messages in results or []:
                for message_id, fields in messages:
                    try:
                        event = self._deserialize(fields["event_type"], fields["data"])
                        if event is not None:
                            yield event
                    finally:
                        await r.xack(stream_key, self._group, message_id)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
