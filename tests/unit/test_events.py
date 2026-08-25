"""Unit tests for src.core.events.EventBus."""

from __future__ import annotations

import asyncio

from src.core.events import BaseEvent, EventBus


class _PingEvent(BaseEvent):
    """Minimal concrete event for bus tests."""

    payload: str = "ping"


class TestPublish:
    async def test_publish_calls_all_subscribed_handlers(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def handler_a(event: BaseEvent) -> None:
            calls.append("a")

        async def handler_b(event: BaseEvent) -> None:
            calls.append("b")

        bus.subscribe(_PingEvent, handler_a)
        bus.subscribe(_PingEvent, handler_b)

        await bus.publish(_PingEvent())

        assert sorted(calls) == ["a", "b"]
        assert bus.stats["published"] == 1
        assert bus.stats["errors"] == 0

    async def test_publish_isolates_handler_exceptions(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def failing_handler(event: BaseEvent) -> None:
            raise ValueError("boom")

        async def ok_handler(event: BaseEvent) -> None:
            calls.append("ok")

        bus.subscribe(_PingEvent, failing_handler)
        bus.subscribe(_PingEvent, ok_handler)

        await bus.publish(_PingEvent())

        assert calls == ["ok"]
        assert bus.stats["errors"] == 1

    async def test_publish_with_no_handlers_is_a_noop(self) -> None:
        bus = EventBus()
        await bus.publish(_PingEvent())
        assert bus.stats["published"] == 1


class TestPublishSync:
    """
    Regression tests for the fire-and-forget publish_sync() path.

    publish_sync() previously scheduled its task via a bare
    ``loop.create_task(...)`` with no stored reference (RUF006:
    asyncio-dangling-task). Per the asyncio docs, an unreferenced task can
    be garbage-collected mid-execution, and any exception it raises is
    only ever logged at destruction with no way for the bus to observe or
    report it. These tests pin down the fixed behaviour: the task is
    tracked until completion and handler exceptions are reflected in
    ``stats()``.
    """

    async def test_publish_sync_schedules_and_completes_delivery(self) -> None:
        bus = EventBus()
        received: list[BaseEvent] = []

        async def handler(event: BaseEvent) -> None:
            received.append(event)

        bus.subscribe(_PingEvent, handler)

        event = _PingEvent()
        bus.publish_sync(event)

        # Task is scheduled but hasn't necessarily run yet.
        assert len(bus._background_tasks) == 1  # noqa: SLF001

        # Let the scheduled task run to completion.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert received == [event]
        assert bus.stats["published"] == 1

    async def test_publish_sync_task_is_reaped_after_completion(self) -> None:
        bus = EventBus()
        bus.publish_sync(_PingEvent())

        assert len(bus._background_tasks) == 1  # noqa: SLF001

        # Allow the task to finish and its done-callback to fire.
        for _ in range(5):
            await asyncio.sleep(0)

        assert bus._background_tasks == set()  # noqa: SLF001

    async def test_publish_sync_handler_exception_is_surfaced_not_swallowed(self) -> None:
        bus = EventBus()

        async def failing_handler(event: BaseEvent) -> None:
            raise RuntimeError("handler exploded")

        bus.subscribe(_PingEvent, failing_handler)
        bus.publish_sync(_PingEvent())

        for _ in range(5):
            await asyncio.sleep(0)

        # The exception is caught inside publish() itself (isolated per
        # handler), so it surfaces via the bus's own error count rather
        # than an unhandled-task-exception log line.
        assert bus.stats["errors"] == 1
        assert bus._background_tasks == set()  # noqa: SLF001

    async def test_publish_sync_without_running_loop_runs_synchronously(self) -> None:
        bus = EventBus()
        received: list[BaseEvent] = []

        async def handler(event: BaseEvent) -> None:
            received.append(event)

        bus.subscribe(_PingEvent, handler)
        event = _PingEvent()

        def call_outside_loop() -> None:
            bus.publish_sync(event)

        await asyncio.to_thread(call_outside_loop)

        assert received == [event]


class TestSubscribeUnsubscribe:
    async def test_subscribe_all_receives_every_event_type(self) -> None:
        bus = EventBus()
        received: list[str] = []

        async def catch_all(event: BaseEvent) -> None:
            received.append(event.event_type)

        bus.subscribe_all(catch_all)
        await bus.publish(_PingEvent())

        assert received == ["_PingEvent"]

    async def test_unsubscribe_removes_handler(self) -> None:
        bus = EventBus()
        calls: list[str] = []

        async def handler(event: BaseEvent) -> None:
            calls.append("called")

        bus.subscribe(_PingEvent, handler)
        bus.unsubscribe(_PingEvent, handler)
        await bus.publish(_PingEvent())

        assert calls == []

    def test_unsubscribe_missing_handler_logs_warning_not_raises(self) -> None:
        bus = EventBus()

        async def handler(event: BaseEvent) -> None:
            pass

        # Should not raise even though `handler` was never subscribed.
        bus.unsubscribe(_PingEvent, handler)
