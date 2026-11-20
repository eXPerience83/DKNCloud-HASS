"""Synthetic integration tests for entry-scoped synchronization."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.airzoneclouddaikin.airzone_api import (
    AirzoneAPI,
    CommandNotSentCooldown,
)
from custom_components.airzoneclouddaikin.sync_manager import SyncManager


def manager(fetch=None):
    locks = {}
    publish = Mock()
    request = AsyncMock()
    reauth = Mock()
    time = [0.0]

    async def fast_sleep(seconds):
        time[0] += seconds
        await asyncio.sleep(0)

    instance = SyncManager(
        {"fake": {"id": "fake", "power": "0", "mode": "1"}},
        {"fake": "installation"},
        request,
        fetch or AsyncMock(return_value=[{"id": "fake", "power": "0"}]),
        publish,
        reauth,
        lambda d: locks.setdefault(d, asyncio.Lock()),
        connect=AsyncMock(),
        clock=lambda: time[0],
        sleep=fast_sleep,
    )

    class ImmediateBudget:
        async def run(self, operation):
            return await operation()

        def close(self):
            pass

        def mark_started(self):
            pass

        def diagnostics(self):
            return {}

    instance._budget = ImmediateBudget()
    return instance, publish, request, reauth


async def test_information_cooldown_never_queues_a_delayed_control():
    """Slow informational cooldown must not own the lock used by HVAC writes."""
    now = [0.0]
    waiting, release = asyncio.Event(), asyncio.Event()
    api = AirzoneAPI("synthetic@example.invalid", Mock(), token="synthetic")
    api._now = lambda: now[0]
    api._cooldown_until = 300.0
    api._request = AsyncMock(return_value={})

    async def sleep_in_cooldown(seconds):
        waiting.set()
        await release.wait()
        now[0] += seconds

    api._sleep = sleep_in_cooldown
    instance, _, _, _ = manager()
    instance._request = api.request_information
    information = asyncio.create_task(instance._information("fake"))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        # The real climate controls acquire the same per-device lock.
        async with asyncio.timeout(1):
            async with instance._locks("fake"):
                with pytest.raises(CommandNotSentCooldown):
                    await api.put_device_fields("fake", {"device": {"power": "1"}})
        api._request.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(information, timeout=1)
        await instance.close()

    # The rejected control never reappears when the informational wait ends.
    api._request.assert_awaited_once()
    assert api._request.await_args.args[0] == "POST"


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
    # The GET power was superseded by Pusher. Its presence in the raw
    # response must not become fresh OFF evidence downstream.
    assert "power" not in instance.touched["fake"]
    assert "mode" in instance.touched["fake"]
    assert publish.call_count == 2
    await instance.close()


async def test_superseded_get_only_updates_reception_not_applied_age():
    """An old GET whose power lost to Pusher must not claim applied freshness."""
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_installation):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return [{"id": "fake", "power": "0"}]
        return [{"id": "fake", "power": "1"}]

    instance, _, _, _ = manager(fetch)
    now = [30.0]
    instance._clock = lambda: now[0]
    instance._pool._clock = instance._clock
    recovery = asyncio.create_task(instance.recover("fake"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await instance.event("fake", {"power": "1"}, instance.state.generation)
        release.set()
        await asyncio.wait_for(recovery, timeout=2)

        # Only identity from the old response was accepted; its OFF lost to
        # the newer push. An HTTP GET was received, not a state-bearing GET.
        assert instance.touched == {"fake": {"id"}}
        assert instance.state.data["fake"]["power"] == "1"
        assert instance._last_http_get["fake"] == 30.0
        assert instance._last_received["fake"] == 30.0
        assert instance._last_get["fake"] == 0.0
        diagnostics = instance.diagnostics()["devices"]["device_1"]
        assert diagnostics["last_get_age"] == 0.0
        assert diagnostics["last_applied_get_age"] == 30.0

        # A separate GET with a state-bearing applied field advances it.
        now[0] = 45.0
        await instance.recover("fake")
        assert "power" in instance.touched["fake"]
        assert instance._last_get["fake"] == 45.0
        assert (
            instance.diagnostics()["devices"]["device_1"]["last_applied_get_age"] == 0.0
        )
    finally:
        release.set()
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


async def test_concurrent_subscription_callbacks_do_not_replace_live_resync():
    """The later ACK must wait for an earlier disconnect to retire its task."""
    instance, _, _, _ = manager()
    old_started = asyncio.Event()
    old_cancelling = asyncio.Event()
    release_old = asyncio.Event()

    async def old_resync():
        old_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            old_cancelling.set()
            await release_old.wait()

    previous = asyncio.create_task(old_resync())
    await asyncio.wait_for(old_started.wait(), timeout=2)
    instance._resync_tasks["fake"] = previous
    disconnected = asyncio.create_task(instance.subscription("fake", False))
    connected = None
    try:
        await asyncio.wait_for(old_cancelling.wait(), timeout=2)
        connected = asyncio.create_task(instance.subscription("fake", True))
        await asyncio.sleep(0)
        # Without a device-scoped lock the ACK starts a new resync while
        # disconnect is still awaiting cancellation of the old one.
        assert instance._resync_tasks["fake"] is previous

        release_old.set()
        await asyncio.wait_for(asyncio.gather(disconnected, connected), timeout=3)
        resync = instance._resync_tasks["fake"]
        await asyncio.wait_for(resync, timeout=3)
        assert instance.schedulers["fake"].status.push_active
        assert previous.done()
    finally:
        release_old.set()
        for task in (disconnected, connected):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (disconnected, connected) if task is not None),
            return_exceptions=True,
        )
        await instance.close()


async def test_subscription_does_not_start_resync_after_manager_close():
    """Closing during an awaited old-task cancellation must prevent rearm."""
    instance, _, _, _ = manager()
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def old_resync():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            # Model a task finishing its cleanup even if shutdown sends
            # another cancellation while the callback waits for it.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    old = asyncio.create_task(old_resync())
    await asyncio.wait_for(started.wait(), timeout=2)
    instance._resync_tasks["fake"] = old
    subscribing = asyncio.create_task(instance.subscription("fake", True))
    try:
        await asyncio.wait_for(cancelling.wait(), timeout=2)
        assert instance._resync_tasks["fake"] is old
        closing = asyncio.create_task(instance.close())
        await asyncio.sleep(0)
        assert instance._stopped
        assert not closing.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(subscribing, closing), timeout=3)
        assert instance._close_complete
        assert not instance._resync_tasks
        assert old.done()
    finally:
        release.set()
        if not subscribing.done():
            subscribing.cancel()
        await asyncio.gather(subscribing, return_exceptions=True)
        for task in instance._resync_tasks.values():
            task.cancel()
        await asyncio.gather(*instance._resync_tasks.values(), return_exceptions=True)
        await instance.close()


async def test_cancelled_subscription_retains_old_task_for_manager_close():
    """Callback cancellation cannot orphan its still-running old resync."""
    instance, _, _, _ = manager()
    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held_resync():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            # Model a task finishing its cleanup even if shutdown sends
            # another cancellation while the callback waits for it.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    old = asyncio.create_task(held_resync())
    await asyncio.wait_for(started.wait(), timeout=2)
    instance._resync_tasks["fake"] = old
    callback = asyncio.create_task(instance.subscription("fake", True))
    try:
        await asyncio.wait_for(cancelling.wait(), timeout=2)
        callback.cancel()
        await asyncio.sleep(0)
        # gather(old) propagates callback cancellation to the old task
        # and waits for its cleanup. Awaiting the callback *before* releasing
        # the cleanup event would deadlock this test on both HA suites.
        assert instance._resync_tasks["fake"] is old
        closing = asyncio.create_task(instance.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(callback, closing, return_exceptions=True),
            timeout=3,
        )
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert outcomes[1] is None
        assert old.done()
        assert not instance._resync_tasks
    finally:
        release.set()
        if not callback.done():
            callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)
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
    assert waits == [10]
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
            # A real subscribed connection requires generation fencing.
            await owner.subscription("fake", True)
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
    instance._sleep = timed_sleep
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


async def test_queued_request_is_fenced_when_authentication_expires():
    instance, _, request, _ = manager()
    await instance._limit.acquire()
    await instance._limit.acquire()
    queued = asyncio.create_task(instance._information("fake"))
    await asyncio.sleep(0)
    instance._auth_failed = True
    instance._limit.release()
    try:
        await queued
    except asyncio.CancelledError:
        pass
    request.assert_not_awaited()
    instance._limit.release()
    await instance.close()


async def test_fallback_devices_batch_one_get_after_all_information_is_ready():

    now, trace = [0.0], []

    async def sleep(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    async def request(device):
        trace.append((now[0], "information", device))
        now[0] += 0.2

    async def fetch(installation):
        trace.append((now[0], "get", installation))
        return [{"id": "a"}, {"id": "b"}]

    instance = SyncManager(
        {"a": {"id": "a"}, "b": {"id": "b"}},
        {"a": "shared", "b": "shared"},
        request,
        fetch,
        Mock(),
        Mock(),
        lambda _: asyncio.Lock(),
        connect=AsyncMock(),
        clock=lambda: now[0],
        sleep=sleep,
    )
    await instance._information("a")
    await instance._information("b")
    await asyncio.gather(instance.recover("a"), instance.recover("b"))
    assert [op[1] for op in trace] == ["information", "information", "get"]
    assert trace[-1][0] >= 20.2
    assert all(b[0] - a[0] >= 10 for a, b in zip(trace, trace[1:], strict=False))
    await instance.close()


async def test_recovery_normalizes_mac_fallback_without_proving_physical_freshness():
    instance, _, _, _ = manager(
        AsyncMock(return_value=[{"mac": " FA:KE ", "power": "1"}])
    )
    instance.state.data = {"fa:ke": {"id": "fa:ke", "power": "0"}}
    instance._installations = {"fa:ke": "installation"}
    instance.schedulers["fa:ke"] = instance.schedulers.pop("fake")
    instance._last_received = {"fa:ke": 0}
    await instance.recover("fa:ke")
    assert instance.state.data["fa:ke"]["power"] == "1"
    diagnostic = instance.diagnostics()["devices"]["device_1"]
    assert diagnostic["last_get_age"] == 0
    assert diagnostic["state_age"] is None
    assert diagnostic["physical_freshness_verified"] is False
    await instance.close()


async def test_authoritative_topology_change_requests_reload():
    instance, _, _, _ = manager()
    changed = Mock()
    instance._on_topology_change = changed
    instance._discover = AsyncMock(return_value={})
    await instance._discover_once()
    changed.assert_called_once()
    assert "fake" in instance.state.data  # retained until the coordinated reload
    await instance.close()


async def test_quiet_recovery_backoff_does_not_block_other_device():
    instance, _, _, _ = manager()
    instance.schedulers["other"] = instance.schedulers["fake"]
    instance._installations["other"] = "other-installation"
    instance.state.data["other"] = {"id": "other"}
    instance._last_received["other"] = 0
    instance._fetch = AsyncMock(return_value=[{"id": "other"}])
    instance._recovery_due["fake"] = 300
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        if delay >= 300:
            waiting.set()
            await release.wait()

    instance._sleep = sleep
    instance.request_recovery("fake")
    await waiting.wait()
    instance.request_recovery("other")
    await instance._quiet_tasks["other"]
    instance._fetch.assert_awaited_once_with("other-installation")
    assert not instance._quiet_tasks["fake"].done()
    await instance.close()


async def test_concurrent_close_waits_for_one_complete_teardown():
    """Unload and HA stop must not close the same owned resources twice."""
    instance, _, _, _ = manager()
    entered, release = asyncio.Event(), asyncio.Event()
    real_close = instance._pool.close
    close_calls = 0

    async def held_close():
        nonlocal close_calls
        close_calls += 1
        entered.set()
        await release.wait()
        await real_close()

    instance._pool.close = held_close
    first = asyncio.create_task(instance.close())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert instance._stopped
        second = asyncio.create_task(instance.close())
        await asyncio.sleep(0)
        assert close_calls == 1
        assert not second.done()
    finally:
        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            ),
            timeout=3,
        )

    assert close_calls == 1
    assert instance._close_complete
    assert not instance._tasks
    assert not instance._resync_tasks
    assert not instance._quiet_tasks
    assert not instance._recovery_waiters

    await instance.close()
    assert close_calls == 1


async def test_close_owns_explicit_recovery_waiting_for_backoff():
    instance, _, _, _ = manager()
    entered = asyncio.Event()

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    instance._sleep = sleep
    instance._recovery_due["fake"] = 300
    caller = asyncio.create_task(instance.recover("fake"))
    await entered.wait()
    await instance.close()
    assert caller.cancelled()
    assert not instance._recovery_waiters


async def test_supervisor_authorization_failure_stops_automatic_work():
    from aiohttp import ClientResponseError

    instance, _, request, reauth = manager()
    instance._connect = AsyncMock(side_effect=ClientResponseError(None, (), status=401))
    await instance._supervise()
    assert instance._auth_failed
    reauth.assert_called_once()
    assert instance._connect.await_count == 1
    try:
        await instance._information("fake")
    except asyncio.CancelledError:
        pass
    request.assert_not_awaited()
    await instance.close()


async def test_shared_virtual_clock_real_budget_multiple_installations():
    now, sleepers, trace = [0.0], [], []

    async def sleep(delay):
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        sleepers.append((now[0] + delay, future))
        await future

    async def information(device):
        trace.append((now[0], "info", device))
        if device == "a":
            raise TimeoutError

    async def fetch(installation):
        trace.append((now[0], "get", installation))
        await sleep(15)
        return [{"id": d} for d, i in relations.items() if i == installation]

    relations = {"a": "one", "b": "one", "c": "two"}
    locks = {d: asyncio.Lock() for d in relations}
    owner = SyncManager(
        {d: {"id": d} for d in relations},
        relations,
        information,
        fetch,
        Mock(),
        Mock(),
        locks.__getitem__,
        connect=AsyncMock(),
        clock=lambda: now[0],
        sleep=sleep,
    )
    tasks = [asyncio.create_task(s.cycle()) for s in owner.schedulers.values()]
    try:
        for tick in range(180):
            now[0] = tick
            for deadline, future in sleepers:
                if deadline <= now[0] and not future.done():
                    future.set_result(None)
            for _ in range(10):
                await asyncio.sleep(0)
            if all(t.done() for t in tasks):
                break
        assert all(t.done() for t in tasks)
        await asyncio.gather(*tasks)
        assert all(b[0] - a[0] >= 10 for a, b in zip(trace, trace[1:], strict=False))
        assert sorted(i for _, kind, i in trace if kind == "get") == ["one", "two"]
        assert owner.schedulers["a"].status.failures == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.close()


async def test_failed_push_attempts_do_not_compete_with_fallback_discovery():
    instance, _, _, _ = manager()
    discovery = AsyncMock(return_value={"fake": "installation"})
    instance._discover = discovery
    second = asyncio.Event()
    attempts = []

    async def connect(_):
        attempts.append(True)
        if len(attempts) == 1:
            raise ConnectionError
        second.set()
        await asyncio.Event().wait()

    instance._connect = connect
    task = asyncio.create_task(instance._supervise())
    instance._tasks.append(task)
    await second.wait()
    discovery.assert_not_awaited()
    assert instance._reconnect_discovery_pending
    await instance.close()


async def test_discovery_runs_after_successful_resynchronization():
    instance, _, _, _ = manager()
    instance._discover = AsyncMock(return_value={"fake": "installation"})
    instance._reconnect_discovery_pending = True
    await instance.subscription("fake", True)
    await instance._resync_tasks["fake"]
    await instance._topology_task
    instance._discover.assert_awaited_once()
    assert instance.schedulers["fake"].status.push_active
    await instance.close()


async def test_refresh_reports_authentication_without_disguising_external_cancel():
    import pytest
    from aiohttp import ClientResponseError
    from homeassistant.exceptions import ConfigEntryAuthFailed

    instance, _, _, reauth = manager(
        AsyncMock(side_effect=ClientResponseError(None, (), status=401))
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await instance.refresh()
    reauth.assert_called_once()
    await instance.close()
    with pytest.raises(asyncio.CancelledError):
        await instance.refresh()
    reauth.assert_called_once()

    entered = asyncio.Event()

    async def fetch(_):
        entered.set()
        await asyncio.Event().wait()

    instance, *_ = manager(fetch)
    task = asyncio.create_task(instance.refresh())
    await entered.wait()
    instance._auth_failed = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await instance.close()


async def test_ha_coordinator_records_recovery_authentication_error(hass):
    import logging

    from aiohttp import ClientResponseError
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    instance, _, _, reauth = manager(
        AsyncMock(side_effect=ClientResponseError(None, (), status=401))
    )
    coordinator = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name="synthetic",
        config_entry=None,
        update_method=instance.refresh,
    )
    await coordinator.async_refresh()
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert not coordinator.last_update_success
    reauth.assert_called_once()
    await instance.close()


async def test_failed_push_handshake_preserves_rest_checkpoint():
    instance, _, _, _ = manager()
    checkpoint = instance.state.checkpoint()

    async def failed(_):
        raise ConnectionError("synthetic")

    async def cancel_backoff(_):
        raise asyncio.CancelledError

    instance._connect = failed
    instance._sleep = cancel_backoff
    try:
        await instance._supervise()
    except asyncio.CancelledError:
        pass
    assert instance.state.generation == checkpoint[0]
    assert instance.state.snapshot([{"id": "fake", "power": "1"}], checkpoint)
    await instance.close()


async def test_repeated_disconnected_notice_preserves_deferred_fallback_get():
    instance, _, _, _ = manager()
    entered, release = asyncio.Event(), asyncio.Event()
    scheduler = instance.schedulers["fake"]

    async def wait(_):
        entered.set()
        await release.wait()

    scheduler._sleep = wait
    scheduler._request = AsyncMock()
    scheduler._fallback_recover = AsyncMock()
    cycle = asyncio.create_task(scheduler.cycle())
    await entered.wait()
    await instance.subscription("fake", False)
    release.set()
    await cycle
    scheduler._fallback_recover.assert_awaited_once()
    await instance.close()


async def test_transport_mode_transitions_are_per_device_and_do_not_fake_receipts():
    """Mode notifications follow PUSH, fallback and auth without new telemetry."""
    from aiohttp import ClientResponseError

    fetch = AsyncMock(
        return_value=[{"id": "a", "power": "1"}, {"id": "b", "power": "0"}]
    )
    request, publish, reauth = AsyncMock(), Mock(), Mock()
    locks = {}
    observations = []

    instance = SyncManager(
        {"a": {"id": "a", "power": "1"}, "b": {"id": "b", "power": "0"}},
        {"a": "installation", "b": "installation"},
        request,
        fetch,
        publish,
        reauth,
        lambda d: locks.setdefault(d, asyncio.Lock()),
        connect=AsyncMock(),
        on_mode_change=lambda: observations.append(
            (instance.mode("a"), instance.mode("b"))
        ),
    )

    class ImmediateBudget:
        async def run(self, operation):
            return await operation()

        def mark_started(self):
            pass

        def close(self):
            pass

        def diagnostics(self):
            return {}

    instance._budget = ImmediateBudget()
    try:
        assert instance.mode("a") == instance.mode("b") == "rest_fallback"
        assert instance.mode("unknown") is None
        received_before = dict(instance._last_received)
        await instance.subscription("a", True)
        assert instance.mode("a") == "resyncing"
        assert instance.mode("b") == "rest_fallback"
        await instance._resync_tasks["a"]
        await asyncio.sleep(0)
        assert instance.mode("a") == "push"
        assert observations[:2] == [
            ("resyncing", "rest_fallback"),
            ("push", "rest_fallback"),
        ]
        assert fetch.await_count == 1  # required subscription resync only
        # The resync GET can refresh both devices; local mode reads cannot.
        assert instance._last_received["b"] >= received_before["b"]
        reception_after_get = dict(instance._last_received)

        diagnostics = instance.diagnostics()
        assert diagnostics["devices"]["device_1"]["mode"] == instance.mode("a")
        assert diagnostics["devices"]["device_2"]["mode"] == instance.mode("b")
        assert fetch.await_count == 1  # mode and diagnostics are pure reads
        assert instance._last_received == reception_after_get

        await instance.subscription("a", False)
        assert instance.mode("a") == "rest_fallback"
        assert observations[-1] == ("rest_fallback", "rest_fallback")
        count = len(observations)
        await instance.subscription("a", False)
        assert len(observations) == count  # repeat disconnect is idempotent

        await instance.subscription("a", True)
        await instance._resync_tasks["a"]
        await asyncio.sleep(0)
        assert instance.mode("a") == "push"
        assert instance.mode("b") == "rest_fallback"

        with pytest.raises(asyncio.CancelledError):
            instance._error("a", ClientResponseError(None, (), status=401))
        assert instance.mode("a") == instance.mode("b") == "auth_required"
        assert observations[-1] == ("auth_required", "auth_required")
        reauth.assert_called_once()
        count = len(observations)
    finally:
        await instance.close()
    assert instance.mode("a") == instance.mode("b") == "stopped"
    assert len(observations) == count  # no callbacks after clean shutdown
    assert not instance._resync_tasks
    assert not instance._tasks


@pytest.mark.parametrize(
    ("demands", "advance", "devices", "expected_reads"),
    [
        pytest.param("before", 0, ("a", "b"), 1, id="compatible"),
        pytest.param("late", 2, ("a",), 2, id="late"),
        pytest.param("late", 0, ("a",), 2, id="same-tick"),
        pytest.param("late", 2, ("a", "a", "a"), 2, id="burst"),
        pytest.param("late", 2, ("a", "b", "a"), 2, id="shared-installation"),
        pytest.param("ordinary", 2, ("a", "a"), 1, id="watchdog"),
    ],
)
async def test_command_readback_freshness(demands, advance, devices, expected_reads):
    from custom_components.airzoneclouddaikin.const import DOMAIN
    from custom_components.airzoneclouddaikin.helpers import (
        request_ambiguous_control_readback,
    )

    now = 0.0
    started, release = asyncio.Event(), asyncio.Event()
    reads, sleeps = [], []

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    async def fetch(installation):
        reads.append((installation, now))
        if len(reads) == 1:
            started.set()
            await release.wait()
        return [{"id": "a", "power": "0"}, {"id": "b", "power": "0"}]

    publish, request = Mock(), AsyncMock()
    owner = SyncManager(
        {d: {"id": d, "power": "1"} for d in ("a", "b")},
        {"a": "shared", "b": "shared"},
        request,
        fetch,
        publish,
        Mock(),
        lambda _: asyncio.Lock(),
        connect=AsyncMock(),
        clock=lambda: now,
        sleep=sleep,
    )
    coordinator = Mock()
    bucket = {"coordinator": coordinator, "sync_manager": owner}
    hass = Mock(data={DOMAIN: {"entry": bucket}}, is_stopping=False)
    try:
        owner.request_recovery("a")
        first_task = owner._quiet_tasks["a"]
        if demands != "before":
            await asyncio.wait_for(started.wait(), 2)
        now += advance
        for device in devices:
            if demands == "ordinary":
                owner.request_recovery(device)
            else:
                request_ambiguous_control_readback(
                    hass, coordinator, entry_id="entry", device_id=device, bucket=bucket
                )
        assert owner._quiet_tasks["a"] is first_task
        await asyncio.wait_for(started.wait(), 2)
        if demands == "late":
            # A push after GET 1 starts wins against GET 1's older OFF.
            await owner.event("a", {"power": "1"}, owner.state.generation)
        release.set()
        await asyncio.wait_for(asyncio.gather(*owner._quiet_tasks.values()), 2)

        assert reads == [("shared", 0.0)] + (
            [("shared", advance + 10.0)] if expected_reads == 2 else []
        )
        assert sleeps == ([10.0] if expected_reads == 2 else [])
        publications = [call.args[0]["a"]["power"] for call in publish.call_args_list]
        assert publications == (["1", "1", "0"] if demands == "late" else ["0"])
        request.assert_not_awaited()
    finally:
        await owner.close()


async def test_demand_during_api_cooldown_shares_the_get():
    """A GET waiting inside AirzoneAPI has yet to acquire its dispatch stamp."""
    now = 10.0
    cooldown, release = asyncio.Event(), asyncio.Event()
    sleeps = []
    api = AirzoneAPI("synthetic@example.invalid", Mock(), token="synthetic")
    api._now = lambda: now
    api._cooldown_until = 30.0
    api._request = AsyncMock(return_value=[{"id": "a"}])

    async def wait_for_cooldown(delay):
        nonlocal now
        cooldown.set()
        await release.wait()
        now += delay

    async def reserved_wait(delay):
        nonlocal now
        sleeps.append(delay)
        now += delay

    api._sleep = wait_for_cooldown

    async def timed_fetch(installation, on_start):
        return await api.fetch_devices(installation, retry=False, on_start=on_start)

    owner = SyncManager(
        {"a": {"id": "a"}},
        {"a": "shared"},
        AsyncMock(),
        api.fetch_devices,
        Mock(),
        Mock(),
        lambda _: asyncio.Lock(),
        connect=AsyncMock(),
        clock=lambda: now,
        sleep=reserved_wait,
        timed_fetch=timed_fetch,
    )
    try:
        owner.request_recovery("a")
        await asyncio.wait_for(cooldown.wait(), 2)
        owner.request_recovery("a", require_fresh=True)
        release.set()
        await asyncio.wait_for(owner._quiet_tasks["a"], 2)
        api._request.assert_awaited_once()
        assert api._request.await_args.args[0] == "GET"
        assert sleeps == []
    finally:
        await owner.close()


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_late_recovery_respects_auth_and_backoff(status, monkeypatch):
    from aiohttp import ClientResponseError

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.random.uniform",
        lambda *_: 1,
    )
    now = 0.0
    started, release, waiting, advance = (asyncio.Event() for _ in range(4))
    reads, sleeps = [], []

    async def fetch(installation):
        reads.append((installation, now))
        if len(reads) == 1:
            started.set()
            await release.wait()
            raise ClientResponseError(None, (), status=status)
        return [{"id": "a"}]

    async def sleep(delay):
        nonlocal now
        sleeps.append(delay)
        waiting.set()
        await advance.wait()
        now += delay

    reauth, publish = Mock(), Mock()
    owner = SyncManager(
        {"a": {"id": "a"}},
        {"a": "shared"},
        AsyncMock(),
        fetch,
        publish,
        reauth,
        lambda _: asyncio.Lock(),
        connect=AsyncMock(),
        clock=lambda: now,
        sleep=sleep,
    )
    try:
        owner.request_recovery("a")
        worker = owner._quiet_tasks["a"]
        await asyncio.wait_for(started.wait(), 2)
        now = 1.0
        owner.request_recovery("a", require_fresh=True)
        release.set()
        if status == 401:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2)
            reauth.assert_called_once()
            owner.request_recovery("a", require_fresh=True)
            assert owner._quiet_tasks["a"] is worker
            assert sleeps == []
        else:
            await asyncio.wait_for(worker, 2)
            reauth.assert_not_called()
            assert owner._budget.diagnostics()["failures"] == 1
            assert owner._recovery_due == {"a": 21.0}
            owner.request_recovery("a", require_fresh=True)
            await asyncio.wait_for(waiting.wait(), 2)
            assert sleeps == [20.0]
            assert reads == [("shared", 0.0)]
            publish.assert_not_called()
            advance.set()
            await asyncio.wait_for(owner._quiet_tasks["a"], 2)
            assert reads[-1] == ("shared", 21.0)
            assert owner._recovery_due == {}
        assert len(reads) == (1 if status == 401 else 2)
        assert not owner._quiet_demands
    finally:
        await owner.close()


async def test_close_cancels_fresh_followup_waiting_for_request_budget():
    """A replacement runtime must not inherit the old entry's pending read."""
    now = 0.0
    started, release, waiting, cleaned = (asyncio.Event() for _ in range(4))
    reads = []

    async def fetch(installation):
        reads.append(installation)
        started.set()
        await release.wait()
        return [{"id": "a", "power": "0"}]

    async def wait_for_budget(_delay):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    def make_owner(publish):
        return SyncManager(
            {"a": {"id": "a", "power": "1"}},
            {"a": "shared"},
            AsyncMock(),
            fetch,
            publish,
            Mock(),
            lambda _: asyncio.Lock(),
            connect=AsyncMock(),
            clock=lambda: now,
            sleep=wait_for_budget,
        )

    old_publish, new_publish = Mock(), Mock()
    old, successor = make_owner(old_publish), make_owner(new_publish)
    try:
        old.request_recovery("a")
        worker = old._quiet_tasks["a"]
        await asyncio.wait_for(started.wait(), 2)
        now = 5.0
        old.request_recovery("a", require_fresh=True)
        release.set()
        await asyncio.wait_for(waiting.wait(), 2)
        assert old._budget.diagnostics()["wait_remaining"] == 10.0
        await old.close()
        assert cleaned.is_set()
        assert worker.cancelled()
        assert not old._quiet_tasks
        assert not old._quiet_demands
        assert not old._pool._pending
        assert not old._recovery_waiters
        old_publish.assert_called_once()
        new_publish.assert_not_called()
        assert reads == ["shared"]
        assert successor.state.data["a"]["power"] == "1"
    finally:
        await old.close()
        await successor.close()
