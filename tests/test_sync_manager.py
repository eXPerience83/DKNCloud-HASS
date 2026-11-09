"""Synthetic integration tests for entry-scoped synchronization."""

import asyncio
from unittest.mock import AsyncMock, Mock

from custom_components.airzoneclouddaikin.sync_manager import SyncManager


def manager(fetch=None):
    locks = {}
    publish = Mock()
    request = AsyncMock()
    reauth = Mock()
    instance = SyncManager(
        {"fake": {"id": "fake", "power": "0", "mode": "1"}},
        {"fake": "installation"},
        request,
        fetch or AsyncMock(return_value=[{"id": "fake", "power": "0"}]),
        publish,
        reauth,
        lambda d: locks.setdefault(d, asyncio.Lock()),
        connect=AsyncMock(),
    )
    return instance, publish, request, reauth


async def test_event_during_get_wins_and_missing_fields_survive():
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        entered.set()
        await release.wait()
        return [{"id": "fake", "power": "0", "mode": "2"}]

    instance, publish, _, _ = manager(fetch)
    recovery = asyncio.create_task(instance.recover("fake"))
    await entered.wait()
    await instance.event("fake", {"power": "1"}, instance.state.generation)
    release.set()
    await recovery
    assert instance.state.data["fake"]["power"] == "1"
    assert instance.state.data["fake"]["mode"] == "2"
    assert publish.call_count == 2
    await instance.close()


async def test_subscribe_resynchronizes_then_information_has_no_get():
    fetch = AsyncMock(return_value=[{"id": "fake", "power": "0"}])
    instance, _, request, _ = manager(fetch)
    await instance.subscription("fake", True)
    await instance._resync_tasks["fake"]
    assert instance.schedulers["fake"].status.push_active
    await instance.schedulers["fake"].cycle()
    request.assert_awaited_once_with("fake")
    assert fetch.await_count == 1
    await instance.subscription("fake", False)
    assert not instance.schedulers["fake"].status.push_active
    await instance.close()


async def test_maintenance_does_not_fake_remote_reception():
    instance, publish, _, _ = manager()
    before = dict(instance._last_received)
    instance.maintenance()
    assert instance._last_received == before
    assert publish.call_args.args[1] is False
    await instance.close()
    publish.reset_mock()
    instance.maintenance()
    publish.assert_not_called()


async def test_diagnostics_are_anonymized():
    instance, _, _, _ = manager()
    text = str(instance.diagnostics())
    assert "fake" not in text
    assert "installation" not in text
    assert "device_1" in text
    await instance.close()


async def test_fallback_cycle_and_recovery_share_single_owner():
    fetch = AsyncMock(return_value=[{"id": "fake", "power": "0"}])
    instance, _, request, _ = manager(fetch)
    scheduler = instance.schedulers["fake"]
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    scheduler._sleep = sleep
    await scheduler.cycle()
    assert waits == [3]
    assert fetch.await_count == request.await_count == 1
    await instance.subscription("fake", True)
    await instance._resync_tasks["fake"]
    assert fetch.await_count == 2
    await scheduler.cycle()
    assert fetch.await_count == 2
    assert request.await_count == 2
    await instance.close()


async def test_auth_failure_stops_automatic_requests_once():
    from aiohttp import ClientResponseError

    instance, _, request, reauth = manager()
    error = ClientResponseError(None, (), status=401)
    request.side_effect = error
    instance.schedulers["fake"].status.failures = 1
    for _ in range(2):
        try:
            instance._error("fake", error)
        except asyncio.CancelledError:
            pass
    reauth.assert_called_once()
    try:
        await instance._information("fake")
    except asyncio.CancelledError:
        pass
    request.assert_not_awaited()
    await instance.close()


async def test_supervisor_reconnects_and_fences_old_events(monkeypatch):
    instance, _, _, _ = manager()
    attempts = []
    second = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.asyncio.sleep", sleep
    )

    async def connect(owner):
        attempts.append(owner.state.generation)
        if len(attempts) == 1:
            raise ConnectionError("synthetic-private-value")
        second.set()
        await asyncio.Event().wait()

    instance._connect = connect
    supervisor = asyncio.create_task(instance._supervise())
    instance._tasks.append(supervisor)
    await second.wait()
    assert attempts[1] > attempts[0]
    await instance.event("fake", {"power": "1"}, attempts[0])
    assert instance.state.data["fake"]["power"] == "0"
    await instance.close()
    assert supervisor.done()


async def test_failed_recovery_sets_device_backoff_and_clears_on_success(monkeypatch):
    fetch = AsyncMock(side_effect=[TimeoutError(), [{"id": "fake", "power": "1"}]])
    instance, _, _, _ = manager(fetch)
    clock = [0.0]
    instance._clock = lambda: clock[0]
    instance._pool._clock = instance._clock
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    # Keep RecoveryPool's zero-time coalescing yield operational.
    real_sleep = asyncio.sleep

    async def timed_sleep(seconds):
        if seconds:
            await sleep(seconds)
        else:
            await real_sleep(0)

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.asyncio.sleep", timed_sleep
    )
    try:
        await instance.recover("fake")
    except TimeoutError:
        pass
    assert instance._recovery_due["fake"] >= 10
    await instance.recover("fake")
    assert waits and waits[0] >= 10
    assert not instance._recovery_due
    assert instance.state.data["fake"]["power"] == "1"
    await instance.close()
