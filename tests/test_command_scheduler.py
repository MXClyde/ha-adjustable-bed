"""Tests for the device-scoped command scheduler."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.adjustable_bed.command_scheduler import (
    ALL_COMMAND_RESOURCES,
    CommandIntent,
    CommandOutcome,
    CommandState,
    DeviceCommandScheduler,
    PreparedCommandInvalidated,
    command_resources,
    current_command_context,
)


async def test_different_resources_queue_without_cancelling_active() -> None:
    scheduler = DeviceCommandScheduler("queue")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    events: list[str] = []

    async def first(context) -> None:
        events.append("back:start")
        first_started.set()
        await release_first.wait()
        assert not context.cancel_event.is_set()
        events.append("back:end")

    async def second(_context) -> None:
        events.append("legs")

    first_task = asyncio.create_task(
        scheduler.execute(
            CommandIntent(
                first,
                resources=command_resources("motor:back"),
                replacement_key="motor:back",
            )
        )
    )
    await first_started.wait()
    second_task = asyncio.create_task(
        scheduler.execute(
            CommandIntent(
                second,
                resources=command_resources("motor:legs"),
                replacement_key="motor:legs",
            )
        )
    )
    await asyncio.sleep(0)

    assert events == ["back:start"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert events == ["back:start", "back:end", "legs"]


async def test_same_resource_replaces_only_after_active_cleanup() -> None:
    scheduler = DeviceCommandScheduler("replace")
    first_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    events: list[str] = []

    async def first(context) -> None:
        events.append("first:start")
        first_started.set()
        await context.cancel_event.wait()
        events.append("first:cleanup")
        cleanup_started.set()
        await cleanup_release.wait()
        events.append("first:done")

    async def replacement(_context) -> None:
        events.append("replacement")

    first_task = asyncio.create_task(
        scheduler.execute(
            CommandIntent(
                first,
                resources=command_resources("motor:back"),
                replacement_key="motor:back",
            )
        )
    )
    await first_started.wait()
    replacement_task = asyncio.create_task(
        scheduler.execute(
            CommandIntent(
                replacement,
                resources=command_resources("motor:back"),
                replacement_key="motor:back",
            )
        )
    )
    await cleanup_started.wait()

    assert events == ["first:start", "first:cleanup"]
    cleanup_release.set()
    await asyncio.gather(first_task, replacement_task)
    assert events == ["first:start", "first:cleanup", "first:done", "replacement"]


async def test_stop_epoch_invalidates_active_and_queued_work() -> None:
    scheduler = DeviceCommandScheduler("stop")
    active_started = asyncio.Event()
    queued_ran = False

    async def active(context) -> None:
        active_started.set()
        await context.cancel_event.wait()

    async def queued(_context) -> None:
        nonlocal queued_ran
        queued_ran = True

    active_handle = await scheduler.enqueue(
        CommandIntent(
            active,
            resources=command_resources("motor:back"),
            replacement_key="motor:back",
            cancel_running=False,
        )
    )
    await active_started.wait()
    queued_handle = await scheduler.enqueue(
        CommandIntent(
            queued,
            resources=command_resources("motor:legs"),
            replacement_key="motor:legs",
            cancel_running=False,
        )
    )

    assert scheduler.request_stop() == 1
    await asyncio.gather(active_handle.future, queued_handle.future)

    assert active_handle.outcome is CommandOutcome.STOPPED
    assert queued_handle.outcome is CommandOutcome.STOPPED
    assert not queued_ran


async def test_command_admitted_during_stop_waits_for_safety_lane() -> None:
    scheduler = DeviceCommandScheduler("stop-barrier")
    stop_epoch = scheduler.request_stop()
    ran = False

    async def command(_context) -> None:
        nonlocal ran
        ran = True

    task = asyncio.create_task(scheduler.execute(CommandIntent(command)))
    await asyncio.sleep(0.01)
    assert not ran

    scheduler.finish_stop(stop_epoch)
    await task
    assert ran


async def test_only_newest_overlapping_stop_releases_safety_lane() -> None:
    scheduler = DeviceCommandScheduler("overlapping-stop")
    first_epoch = scheduler.request_stop()
    second_epoch = scheduler.request_stop()
    ran = False

    async def command(_context) -> None:
        nonlocal ran
        ran = True

    task = asyncio.create_task(scheduler.execute(CommandIntent(command)))
    scheduler.finish_stop(first_epoch)
    await asyncio.sleep(0.01)
    assert not ran

    scheduler.finish_stop(second_epoch)
    await task
    assert ran


async def test_prepared_command_does_not_run_before_commit() -> None:
    scheduler = DeviceCommandScheduler("prepared")
    ran = False

    async def operation(_context) -> None:
        nonlocal ran
        ran = True

    handle = await scheduler.enqueue(
        CommandIntent(
            operation,
            resources=command_resources("motor:back"),
            group_id="group-1",
        ),
        prepared=True,
    )
    await scheduler.wait_ready(handle)
    assert handle.state is CommandState.PREPARED
    assert not ran

    scheduler.commit(handle)
    await scheduler.wait_prepared_result(handle)
    assert ran
    assert handle.outcome is CommandOutcome.COMPLETED


async def test_prepared_command_invalidated_by_stop_never_runs() -> None:
    scheduler = DeviceCommandScheduler("prepared-stop")
    ran = False

    async def operation(_context) -> None:
        nonlocal ran
        ran = True

    handle = await scheduler.enqueue(CommandIntent(operation, group_id="group-1"), prepared=True)
    await scheduler.wait_ready(handle)
    scheduler.request_stop(ALL_COMMAND_RESOURCES)

    with pytest.raises(PreparedCommandInvalidated):
        await scheduler.wait_prepared_result(handle)
    assert not ran


async def test_caller_cancellation_drops_queued_one_shot() -> None:
    scheduler = DeviceCommandScheduler("caller")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    queued_ran = False

    async def first(_context) -> None:
        first_started.set()
        await release_first.wait()

    async def queued(_context) -> None:
        nonlocal queued_ran
        queued_ran = True

    first_task = asyncio.create_task(scheduler.execute(CommandIntent(first, cancel_running=False)))
    await first_started.wait()
    queued_task = asyncio.create_task(
        scheduler.execute(
            CommandIntent(
                queued,
                resources=command_resources("motor:legs"),
                cancel_running=False,
            )
        )
    )
    await asyncio.sleep(0)
    queued_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued_task

    release_first.set()
    await first_task
    assert not queued_ran


async def test_operation_receives_task_local_context() -> None:
    scheduler = DeviceCommandScheduler("context")
    seen = None

    async def operation(context) -> None:
        nonlocal seen
        seen = current_command_context()
        assert seen is context

    await scheduler.execute(
        CommandIntent(
            operation,
            resources=command_resources("motor:back"),
            pulse_count=42,
            pulse_delay_ms=17,
        )
    )

    assert seen is not None
    assert seen.pulse_count == 42
    assert seen.pulse_delay_ms == 17
    assert current_command_context() is None


async def test_context_copied_to_background_task_expires_with_command() -> None:
    scheduler = DeviceCommandScheduler("background-context")
    inspect_context = asyncio.Event()
    background_context = object()

    async def background() -> None:
        nonlocal background_context
        await inspect_context.wait()
        background_context = current_command_context()

    async def operation(_context) -> None:
        asyncio.create_task(background())

    await scheduler.execute(CommandIntent(operation))
    inspect_context.set()
    await asyncio.sleep(0)

    assert background_context is None
