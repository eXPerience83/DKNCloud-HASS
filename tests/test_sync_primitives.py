"""Synthetic tests for transport-independent synchronization ownership."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.airzoneclouddaikin.sync_scheduler import InformationScheduler
from custom_components.airzoneclouddaikin.sync_state import SyncState


def test_snapshot_cannot_overwrite_intervening_event():
    state = SyncState()
    state.snapshot(
        [{"id": "fake-device", "power": "0", "mode": "1"}], state.checkpoint()
    )
    checkpoint = state.checkpoint()
    assert state.event("fake-device", {"power": "1"}, state.generation)
    state.snapshot([{"id": "fake-device", "power": "0", "mode": "2"}], checkpoint)
    assert state.data["fake-device"] == {"id": "fake-device", "power": "1", "mode": "2"}
    assert not state.event("fake-device", {"power": "1"}, state.generation)
    assert not state.event("unknown", {"power": "1"}, state.generation)
    assert not state.event("fake-device", {"id": "wrong"}, state.generation)
    state.event("fake-device", {"mode": None}, state.generation)
    assert state.data["fake-device"]["mode"] == "2"
    state.invalidate()
    assert not state.snapshot([{"id": "fake-device", "power": "0"}], checkpoint)
    assert not state.event("fake-device", {"power": "0"}, 0)


async def test_fallback_delay_and_push_resynchronization():
    steps = []

    async def request():
        steps.append("information")

    async def recover():
        steps.append("get")

    async def sleep(seconds):
        steps.append(seconds)

    scheduler = InformationScheduler(
        request, recover, sleep=sleep, on_error=lambda _: 10
    )
    await scheduler.cycle()
    assert steps == ["information", 3.0, "get"]
    steps.clear()
    await scheduler.set_push(True)
    await scheduler.cycle()
    assert steps == ["get", "information"]
    await scheduler.close()


async def test_reconnection_fences_deferred_fallback():
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_):
        sleeping.set()
        await release.wait()

    recover = AsyncMock()
    scheduler = InformationScheduler(
        AsyncMock(), recover, sleep=sleep, on_error=lambda _: 10
    )
    cycle = asyncio.create_task(scheduler.cycle())
    await sleeping.wait()
    reconnect = asyncio.create_task(scheduler.set_push(True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(cycle, reconnect)
    assert recover.await_count == 1


async def test_start_is_unique_and_close_cancels_inflight_request():
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def request():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    scheduler = InformationScheduler(request, AsyncMock(), on_error=lambda _: 10)
    scheduler.start()
    await started.wait()
    task = scheduler._task
    scheduler.start()
    assert scheduler._task is task
    await scheduler.close()
    assert canceled.is_set()
    assert task.done()
    await scheduler.close()


@pytest.mark.parametrize("duration,wait", [(0, 10), (8, 2), (15, 10)])
async def test_nominal_cadence_skips_missed_ticks(duration, wait):
    clock = [0.0]
    waits = []

    async def request():
        clock[0] += duration

    async def sleep(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError

    scheduler = InformationScheduler(
        request, AsyncMock(), clock=lambda: clock[0], sleep=sleep, on_error=lambda _: 10
    )
    scheduler.status.push_active = True
    with pytest.raises(asyncio.CancelledError):
        await scheduler._run()
    assert waits == [wait]


async def test_timeout_does_not_trigger_immediate_retry():
    waits = []
    request = AsyncMock(side_effect=TimeoutError)

    async def sleep(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError

    scheduler = InformationScheduler(
        request, AsyncMock(), clock=lambda: 0, sleep=sleep, on_error=lambda _: 40
    )
    with pytest.raises(asyncio.CancelledError):
        await scheduler._run()
    assert request.await_count == 1
    assert waits == [40]
    assert scheduler.status.failures == 1


async def test_lost_connection_during_resync_cannot_enable_push():
    entered, release = asyncio.Event(), asyncio.Event()

    async def recover():
        entered.set()
        await release.wait()

    scheduler = InformationScheduler(AsyncMock(), recover, on_error=lambda _: 10)
    reconnect = asyncio.create_task(scheduler.set_push(True))
    await entered.wait()
    await scheduler.set_push(False)
    release.set()
    await reconnect
    assert not scheduler.status.push_active


async def test_recovery_pool_coalesces_and_closes_owned_reads():
    from custom_components.airzoneclouddaikin.recovery_pool import RecoveryPool

    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        entered.set()
        await release.wait()
        return {"synthetic": True}

    fetch_mock = AsyncMock(side_effect=fetch)
    pool = RecoveryPool(fetch_mock, clock=lambda: 0)
    first = asyncio.create_task(pool.get("fake-installation", 0))
    await entered.wait()
    second = asyncio.create_task(pool.get("fake-installation", 0))
    await asyncio.sleep(0)
    release.set()
    assert await first == await second == {"synthetic": True}
    assert fetch_mock.await_count == 1
    await pool.close()
    with pytest.raises(asyncio.CancelledError):
        await pool.get("fake-installation", 0)
