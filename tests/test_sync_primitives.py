"""Synthetic tests for transport-independent synchronization ownership."""

import asyncio
from unittest.mock import AsyncMock, Mock

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
    assert steps == ["information", 10.0, "get"]
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
    scheduler.status.push_active = True
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


async def test_pool_closes_fetch_after_its_last_waiter_is_cancelled():
    from custom_components.airzoneclouddaikin.recovery_pool import RecoveryPool

    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    pool = RecoveryPool(fetch)
    waiter = asyncio.create_task(pool.get("fake", 0))
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not cleaned.is_set()
    await pool.close()
    assert cleaned.is_set()


@pytest.mark.parametrize("failed", [False, True])
async def test_cancelled_last_waiter_retires_completed_shielded_read(failed):
    """A shielded GET outliving its final waiter must release pool ownership."""
    from custom_components.airzoneclouddaikin.recovery_pool import RecoveryPool

    entered, release = asyncio.Event(), asyncio.Event()
    fetches = 0

    async def fetch(_installation):
        nonlocal fetches
        fetches += 1
        entered.set()
        await release.wait()
        if failed:
            raise TimeoutError("orphaned read failed")
        return {"power": "1"}

    pool = RecoveryPool(fetch, clock=lambda: 0.0)
    waiter = asyncio.create_task(pool.get("installation", 0.0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        owned = pool._pending["installation"].task
        assert owned is not None
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not owned.done()

        release.set()
        if failed:
            with pytest.raises(TimeoutError, match="orphaned read failed"):
                await asyncio.wait_for(owned, timeout=2)
        else:
            assert await asyncio.wait_for(owned, timeout=2) == {"power": "1"}
        # Done callbacks must also retire the read with no live get() waiter.
        await asyncio.sleep(0)
        assert "installation" not in pool._pending

        # The next request must start a separate GET, not reuse orphan data.
        if not failed:
            assert await pool.get("installation", 0.0) == {"power": "1"}
            assert fetches == 2
    finally:
        release.set()
        await pool.close()


async def test_backoff_is_measured_after_slow_failure():
    now, waits = [0.0], []

    async def request():
        now[0] += 35
        raise TimeoutError

    async def sleep(delay):
        waits.append(delay)
        raise asyncio.CancelledError

    scheduler = InformationScheduler(
        request, AsyncMock(), clock=lambda: now[0], sleep=sleep, on_error=lambda _: 40
    )
    scheduler.status.push_active = True
    with pytest.raises(asyncio.CancelledError):
        await scheduler._run()
    assert waits == [40]


async def test_ineligible_failed_read_does_not_poison_later_waiter():
    from custom_components.airzoneclouddaikin.recovery_pool import RecoveryPool

    entered, release = asyncio.Event(), asyncio.Event()
    now, attempts = [0.0], [0]

    async def fetch(_):
        attempts[0] += 1
        if attempts[0] == 1:
            entered.set()
            await release.wait()
            raise TimeoutError
        return "recovered"

    pool = RecoveryPool(fetch, lambda: now[0])
    first = asyncio.create_task(pool.get("fake", 0))
    await entered.wait()
    now[0] = 10
    second = asyncio.create_task(pool.get("fake", 10))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(TimeoutError):
        await first
    assert await second == "recovered"
    assert attempts[0] == 2
    await pool.close()


@pytest.mark.parametrize("status", [400, 403, 404, 422, 423, 500])
async def test_rejected_information_still_reads_fallback_snapshot(status):
    from aiohttp import ClientResponseError

    recover = AsyncMock()
    request = AsyncMock(side_effect=ClientResponseError(None, (), status=status))
    sleep = AsyncMock()
    scheduler = InformationScheduler(
        request, recover, sleep=sleep, on_error=lambda _: 10
    )
    await scheduler.cycle()
    assert scheduler.status.failures == 1
    sleep.assert_awaited_once_with(10)
    recover.assert_awaited_once()


async def test_fallback_get_survives_information_timeout_without_resend():
    now = [0.0]
    request, recovery = AsyncMock(side_effect=TimeoutError), AsyncMock()

    async def sleep(seconds):
        now[0] += seconds

    scheduler = InformationScheduler(
        request, recovery, clock=lambda: now[0], sleep=sleep, on_error=lambda _: 100
    )
    await scheduler.cycle()
    await scheduler.cycle()
    assert request.await_count == 1
    assert recovery.await_count == 2
    assert scheduler.status.failures == 1
    assert scheduler._information_due == 100


async def test_invalid_snapshot_does_not_increase_information_backoff():
    information_error, recovery_error = Mock(return_value=300), Mock(return_value=20)
    scheduler = InformationScheduler(
        AsyncMock(),
        AsyncMock(side_effect=ValueError("Invalid snapshot")),
        sleep=AsyncMock(),
        on_error=information_error,
        on_recovery_error=recovery_error,
    )
    await scheduler.cycle()
    information_error.assert_not_called()
    recovery_error.assert_called_once()
    assert scheduler.status.failures == 0
