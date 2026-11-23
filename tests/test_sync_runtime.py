"""Exercise the synchronization owner through Home Assistant's real lifecycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import entity_registry as er

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import push_adapter
from custom_components.airzoneclouddaikin.const import DOMAIN


def _mark_synthetic_sleep_off(bucket, dev_id="dev1") -> None:
    """Mark current Sleep ON/OFF evidence for narrowly scoped lock tests."""
    tracking = bucket["sleep_tracking"][dev_id]
    tracking.last_scenary = "sleep"
    tracking.sleep_started_at_utc = integration.dt_util.utcnow() - timedelta(minutes=6)
    tracking.expected_minutes = 5
    tracking.start_known = True
    tracking.off_observation_scope = integration.sleep_scope(bucket, dev_id)


async def test_verified_connector_runtime_and_silent_local_clock(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, freezer
):
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_scheduler.FALLBACK_READ_DELAY", 0
    )
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.FALLBACK_READ_DELAY", 0
    )
    device = {**sample_device, "connection_date": datetime.now(UTC).isoformat()}
    information = AsyncMock()
    fetches = []

    class API:
        def __init__(self, *args, **kwargs):
            self.on_write_auth_failure = None
            self.suspend = Mock()
            self.request_information = information
            self.authorize_channel = AsyncMock(return_value="synthetic-signature")

        async def fetch_installations(self, *, retry=True):
            return [{"installation_id": "synthetic-installation"}]

        async def fetch_devices(self, installation, *, retry=True, on_start=None):
            if on_start is not None:
                on_start()
            fetches.append(installation)
            return [dict(device)]

    monkeypatch.setattr(integration, "AirzoneAPI", API)
    from custom_components.airzoneclouddaikin.request_budget import RequestBudget

    budget_clock = [0.0]

    async def budget_sleep(seconds):
        budget_clock[0] += seconds

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.RequestBudget",
        lambda clock, sleep: RequestBudget(lambda: budget_clock[0], budget_sleep),
    )
    subscribed = asyncio.Event()

    class SyntheticWire:
        def __init__(
            self,
            session,
            url,
            channels,
            authorize,
            on_device,
            on_subscription,
            **kwargs,
        ):
            assert "synthetic-app" in url
            self.channels, self.authorize = channels, authorize
            self.on_device, self.on_subscription = on_device, on_subscription

        async def connect(self):
            for channel, device_id in self.channels.items():
                assert await self.authorize("1.2", channel) == "synthetic-signature"
                await self.on_subscription(device_id, True)
                owner = self.on_subscription.__self__
                await owner._resync_tasks[device_id]
                await self.on_device(
                    device_id, json.dumps({"id": device_id, "power": "0"})
                )
            subscribed.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(push_adapter, "PUSHER_APP_KEY", "synthetic-app")
    monkeypatch.setattr(push_adapter, "PusherClient", SyntheticWire)
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        integration,
        "build_verified_adapter",
        push_adapter.build_verified_adapter,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(subscribed.wait(), 5)
    coordinator = entry.runtime_data.coordinator
    manager = hass.data[DOMAIN][entry.entry_id]["sync_manager"]
    assert coordinator.update_interval is None
    assert coordinator.data["dev1"]["power"] == "0"
    registry = er.async_get(hass)
    sync_id = registry.async_get_entity_id("sensor", DOMAIN, "dev1_sync_mode")
    assert sync_id is not None
    await hass.async_block_till_done()
    assert hass.states.get(sync_id).state == "push"
    assert (
        "Synchronization Mode" in hass.states.get(sync_id).attributes["friendly_name"]
    )
    assert manager.mode("dev1") == manager.diagnostics()["devices"]["device_1"]["mode"]

    # Transport changes refresh the new entity, without changing WServer state.
    reads = len(fetches)
    await manager.subscription("dev1", False)
    await hass.async_block_till_done()
    assert hass.states.get(sync_id).state == "rest_fallback"
    assert len(fetches) == reads
    await manager.subscription("dev1", True)
    await manager._resync_tasks["dev1"]
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    assert hass.states.get(sync_id).state == "push"
    assert len(fetches) == reads + 1  # only the existing resync GET

    with monkeypatch.context() as patch:
        patch.setattr(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        )
        assert not await integration.async_unload_entry(hass, entry)
        assert not manager._stopped
        assert manager._tasks
    initial_reads = len(fetches)
    await manager.schedulers["dev1"].cycle()
    assert len(fetches) == initial_reads
    assert information.await_count >= 1
    age = dict(manager._last_received)
    registry = er.async_get(hass)
    online_id = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, "dev1_wserver_online"
    )
    assert hass.states.get(online_id).state == "on"
    notify = Mock()
    monkeypatch.setattr(integration.persistent_notification, "async_create", notify)
    freezer.tick(timedelta(seconds=601))
    manager.maintenance()
    await hass.async_block_till_done()
    assert hass.states.get(online_id).state == "off"
    notify.assert_not_called()
    freezer.tick(timedelta(seconds=91))
    manager.maintenance()
    await hass.async_block_till_done()
    notify.assert_called_once()

    assert manager._last_received == age
    assert len(fetches) == initial_reads
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not manager._tasks
    assert all(s._task is None for s in manager.schedulers.values())

    # The adapter is rebuilt on reload rather than retaining obsolete account state.
    registry_ids = {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    subscribed.clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(subscribed.wait(), 5)
    replacement = hass.data[DOMAIN][entry.entry_id]["sync_manager"]
    assert replacement is not manager
    assert entry.runtime_data.coordinator.update_interval is None
    assert registry_ids == {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert hass.states.get(sync_id).state == "push"
    registry.async_update_entity(sync_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert registry.async_get(sync_id).disabled_by == er.RegistryEntryDisabler.USER
    with monkeypatch.context() as patch:
        patch.setattr(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        )
        assert not await integration.async_unload_entry(hass, entry)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert replacement._stopped
        assert not replacement._tasks
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not replacement._tasks


async def test_explicit_refresh_preserves_raw_scene_without_old_local_display(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A coordinator refresh must not resurrect the removed legacy scene display."""
    first = {**sample_device, "scenary": "occupied"}
    second = {**sample_device, "scenary": "sleep"}
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=[[first], [second]])
    api.request_information = AsyncMock()
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    try:
        assert "effective_scenary" not in coordinator.data["dev1"]
        assert "sleep_expired" not in coordinator.data["dev1"]
        await coordinator.async_refresh()
        assert coordinator.data["dev1"]["scenary"] == "sleep"
        assert "effective_scenary" not in coordinator.data["dev1"]
        assert "sleep_expired" not in coordinator.data["dev1"]
        assert api.fetch_devices.await_count == 2
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_explicit_refresh_one_applied_off_get_authorizes_cleanup(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """One eligible GET authorizes Home and requests only post-write readback."""
    first = {**sample_device, "scenary": "occupied", "power": "1"}
    off = {**sample_device, "scenary": "sleep", "power": "0"}
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    responses = iter(([first], [off]))

    async def fetch(_installation, *, retry=True, on_start=None):
        if on_start:
            on_start()
        return next(responses)

    api.fetch_devices = AsyncMock(side_effect=fetch)
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    recovery = Mock()
    monkeypatch.setattr(manager, "request_recovery", recovery)
    generation = manager.state.generation
    try:
        # The observed ON must belong to the same Sleep session.
        await manager.event("dev1", {"scenary": "sleep", "power": "1"}, generation)
        tracking = bucket["sleep_tracking"]["dev1"]
        _mark_synthetic_sleep_off(bucket)
        tracking.expected_minutes = int(off["sleep_time"])
        tracking.sleep_started_at_utc -= timedelta(hours=2)
        tracking.off_observation_scope = None
        tracking.overdue_probe_requested = True

        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert api.fetch_devices.await_count == 2
        assert coordinator.data["dev1"]["sleep_off_confirmed"]
        api.async_set_scenary.assert_awaited_once_with(
            "dev1", "occupied", retry=False, on_start=ANY, on_in_flight=ANY
        )
        # The only recovery demand confirms the PUT, after its acceptance.
        recovery.assert_called_once_with("dev1", require_fresh=True)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_inflight_get_discarded_power_cannot_confirm_pushed_off(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A GET missing applied power cannot pair with a partial OFF PUSH."""
    entered, release = asyncio.Event(), asyncio.Event()
    first = {**sample_device, "scenary": "occupied", "power": "1"}
    old_get = {**sample_device, "scenary": "sleep", "power": "1"}
    calls = 0

    async def fetch(_installation, **_kwargs):
        nonlocal calls
        calls += 1
        if on_start := _kwargs.get("on_start"):
            on_start()
        if calls == 1:
            return [first]
        entered.set()
        await release.wait()
        return [old_get]

    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=fetch)
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    recovery = Mock()
    monkeypatch.setattr(manager, "request_recovery", recovery)
    generation = manager.state.generation
    refresh = None
    try:
        await manager.event("dev1", {"scenary": "sleep", "power": "1"}, generation)
        tracking = bucket["sleep_tracking"]["dev1"]
        _mark_synthetic_sleep_off(bucket)
        tracking.expected_minutes = int(old_get["sleep_time"])
        tracking.off_observation_scope = None
        refresh = asyncio.create_task(coordinator.async_refresh())
        await asyncio.wait_for(entered.wait(), timeout=3)

        # GET began with an ON power value, but a newer Pusher OFF wins.
        await manager.event("dev1", {"power": "0"}, generation)
        assert tracking.off_observation_scope is None
        api.async_set_scenary.assert_not_awaited()
        release.set()
        await asyncio.wait_for(refresh, timeout=3)

        # The returned GET did not apply its stale power field.
        assert manager.state.data["dev1"]["power"] == "0"
        assert "power" not in manager.touched.get("dev1", set())
        assert tracking.off_observation_scope is None
        assert coordinator.data["dev1"]["sleep_off_confirmed"] is False
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_not_called()

        await manager.event("dev1", {"power": "0"}, generation)
        await hass.async_block_till_done()
        api.async_set_scenary.assert_not_awaited()
    finally:
        release.set()
        if refresh is not None and not refresh.done():
            await asyncio.wait_for(refresh, timeout=3)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_queued_topology_discovery_is_fenced_after_auth_failure(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    from custom_components.airzoneclouddaikin.request_budget import RequestBudget

    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    manager = hass.data[DOMAIN][entry.entry_id]["sync_manager"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(_):
        entered.set()
        await release.wait()
        manager._budget._next = 0

    manager._budget = RequestBudget(sleep=wait)
    manager._budget._next = manager._clock() + 10
    discovery = asyncio.create_task(manager._discover())
    await entered.wait()
    api.fetch_installations.reset_mock()
    monkeypatch.setattr(entry, "async_start_reauth_if_available", Mock())
    entry._async_set_state(hass, ConfigEntryState.FAILED_UNLOAD, None)
    seen_modes = []
    unlisten = entry.runtime_data.coordinator.async_add_listener(
        lambda: seen_modes.append(manager.mode("dev1"))
    )
    try:
        assert manager.mode("dev1") == "rest_fallback"
        api.on_write_auth_failure()
        assert manager._auth_failed
        assert seen_modes == ["auth_required"]
        api.on_write_auth_failure()
        assert seen_modes == ["auth_required"]  # 401 retries are idempotent
        entry.async_start_reauth_if_available.assert_called_once_with(hass)
    finally:
        unlisten()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await discovery
    api.fetch_installations.assert_not_awaited()
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "stop", [True, False, None, "race", "race_expired", "race_new_sleep"]
)
async def test_sleep_cleanup_is_joined_and_cannot_reenter_on_close(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, stop
):
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writes = []

    async def pending_write(*_, **kwargs):
        entered.set()
        try:
            if stop is not None:
                await release.wait()
            writes.append("write")
        finally:
            cleaned.set()

    api.async_set_scenary = AsyncMock(side_effect=pending_write)
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry.runtime_data.coordinator
    recovery = Mock()
    monkeypatch.setattr(bucket["sync_manager"], "request_recovery", recovery)
    coordinator.data["dev1"].update(
        scenary="sleep",
        sleep_off_confirmed=True,
        power="0",
    )
    _mark_synthetic_sleep_off(bucket)
    if stop in {"race", "race_expired", "race_new_sleep"}:
        lock = integration.acquire_device_lock(hass, entry.entry_id, "dev1")
        await lock.acquire()
        coordinator.async_update_listeners()
        await asyncio.sleep(0)
        integration.optimistic_set(
            hass,
            entry.entry_id,
            "dev1",
            "scenary",
            "sleep" if stop == "race_new_sleep" else "vacant",
            ttl=-1 if stop == "race_expired" else 30,
        )
        if stop == "race_expired":
            from custom_components.airzoneclouddaikin.helpers import optimistic_get

            optimistic_get(hass, entry.entry_id, "dev1", "scenary", "sleep")
        if stop == "race_new_sleep":
            coordinator.data["dev1"]["sleep_off_confirmed"] = False
        lock.release()
        await hass.async_block_till_done()
        api.async_set_scenary.assert_not_awaited()
        if stop == "race_new_sleep":
            assert not bucket["sleep_tracking"]["dev1"].force_exit_requested
            release.set()
            coordinator.data["dev1"]["sleep_off_confirmed"] = True
            _mark_synthetic_sleep_off(bucket)
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            api.async_set_scenary.assert_awaited_once()
        else:
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            api.async_set_scenary.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        return
    coordinator.async_update_listeners()
    await asyncio.wait_for(entered.wait(), 5)
    task = bucket["sleep_expiry_task"]
    if stop is None:
        await task
        assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
        recovery.assert_called_once_with("dev1", require_fresh=True)
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert api.async_set_scenary.await_count == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
        return
    if stop:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
    else:
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert cleaned.is_set() and task.done()
    release.set()
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert not writes
    assert api.async_set_scenary.await_count == 1
    if stop:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sleep_pending_requires_explicit_remote_scene_to_clear(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A Pusher power-only patch must not resolve an ambiguous scene PUT."""
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    device_id = "dev1"
    generation = manager.state.generation
    pending = bucket.setdefault("sleep_exit_pending", {})
    pending[device_id] = "unverified"

    # An unchanged, cached Home scene in a power-only event is not an ACK.
    await manager.event(device_id, {"power": "0"}, generation)
    assert pending[device_id] == "unverified"

    # An explicit Home report confirms the remote scene and releases the fence.
    await manager.event(device_id, {"scenary": "occupied"}, generation)
    assert device_id not in pending

    # Start a new ambiguity. Unrelated patches must not clear the fence
    # even when the retained raw scene is Home.
    pending[device_id] = "unverified"
    await manager.event(device_id, {"power": "1"}, generation)
    assert pending[device_id] == "unverified"
    await manager.event(device_id, {"scenary": "sleep"}, generation)
    assert pending[device_id] == "unverified"
    await manager.event(device_id, {"scenary": "vacant"}, generation)
    assert device_id not in pending
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_complete_late_push_authorizes_cleanup_when_sleep_starts_off(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """Only complete post-deadline telemetry authorizes cleanup of an OFF-only session."""
    now = integration.dt_util.utcnow()
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: now)
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    generation = manager.state.generation

    off = {**sample_device, "scenary": "sleep", "power": "0", "sleep_time": 5}
    manager.state.data["dev1"]["sleep_time"] = 5

    async def fetch(_installation, *, retry=True, on_start=None):
        if on_start:
            on_start()
        return [off]

    api.fetch_devices.side_effect = fetch
    await manager.event("dev1", {"scenary": "sleep", "power": "0"}, generation)
    budget_now = 0.0

    async def elapse(delay):
        nonlocal budget_now
        budget_now += delay

    manager._clock = manager._pool._clock = manager._budget._clock = lambda: budget_now
    manager._budget._sleep = elapse
    await manager._read("fake")
    assert bucket["sleep_tracking"]["dev1"].end is None
    registry = er.async_get(hass)
    remaining = registry.async_get_entity_id(
        "sensor", DOMAIN, "dev1_sleep_remaining_estimated"
    )
    end = registry.async_get_entity_id("sensor", DOMAIN, "dev1_sleep_estimated_end")
    assert remaining is not None and hass.states.get(remaining).state == "unknown"
    assert end is not None and registry.async_get(end).disabled

    integration.optimistic_set(hass, entry.entry_id, "dev1", "scenary", "sleep")
    track = bucket["sleep_tracking"]["dev1"]
    await manager._read("fake")
    assert track.off_observation_scope is None
    assert hass.states.get(remaining).state == "5"
    now = track.end + timedelta(seconds=29)
    await manager.event("dev1", {"power": "0", "scenary": "sleep"}, generation)
    assert track.off_observation_scope is None
    api.async_set_scenary.assert_not_awaited()
    reads_before_expiry = api.fetch_devices.await_count

    now += timedelta(seconds=2)
    manager.maintenance()
    await manager.event("dev1", {"local_temp": "24"}, generation)
    await manager.event("dev1", {"power": "0"}, generation)
    await hass.async_block_till_done()
    assert track.off_observation_scope is None
    assert api.fetch_devices.await_count == reads_before_expiry
    api.async_set_scenary.assert_not_awaited()

    await manager.event("dev1", {"power": "0", "scenary": "sleep"}, generation)
    await hass.async_block_till_done()
    assert track.off_observation_scope == integration.sleep_scope(bucket, "dev1")
    api.async_set_scenary.assert_awaited_once_with(
        "dev1", "occupied", retry=False, on_start=ANY, on_in_flight=ANY
    )
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    await manager.event("dev1", {"scenary": "sleep", "power": "0"}, generation)
    await manager.event("dev1", {"scenary": "occupied"}, generation)
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    off["scenary"] = "occupied"
    await manager.refresh()
    assert "dev1" not in bucket["sleep_exit_pending"]
    api.async_set_scenary.assert_awaited_once()
    api.send_event.assert_not_called()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_rejected_scene_power_cannot_auto_cleanup_new_sleep(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """An explicit scene conflicting with optimistic Sleep supplies no OFF evidence."""
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    generation = manager.state.generation
    integration.optimistic_set(hass, entry.entry_id, "dev1", "scenary", "sleep")
    tracking = bucket["sleep_tracking"]["dev1"]

    for power in ("1", "0", "0"):
        await manager.event("dev1", {"scenary": "occupied", "power": power}, generation)
    assert tracking.last_scenary == "sleep"
    assert tracking.evidence_revision == 0
    assert tracking.off_observation_scope is None

    # The delayed Sleep scene-only acknowledgement does not reuse old OFF.
    await manager.event("dev1", {"scenary": "sleep"}, generation)
    await hass.async_block_till_done()
    assert not entry.runtime_data.coordinator.data["dev1"]["sleep_off_confirmed"]
    api.async_set_scenary.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_disabled_automatic_cleanup_keeps_raw_sleep_after_confirmed_off(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """An explicit opt-out prevents an unsolicited occupied write."""
    from custom_components.airzoneclouddaikin.const import CONF_SLEEP_CLEANUP_ENABLED

    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory(options={CONF_SLEEP_CLEANUP_ENABLED: False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    generation = manager.state.generation
    await manager.event("dev1", {"scenary": "sleep", "power": "0"}, generation)
    _mark_synthetic_sleep_off(bucket)
    entry.runtime_data.coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert bucket["sleep_tracking"]["dev1"].off_observation_scope is not None
    assert entry.runtime_data.coordinator.data["dev1"]["scenary"] == "sleep"
    assert not bucket.get("sleep_expiry_task")
    api.async_set_scenary.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_power_snapshot_cannot_trigger_sleep_cleanup(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A partial GET and a cached re-publication cannot authorize cleanup."""
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    bucket = hass.data[DOMAIN][entry.entry_id]
    recovery = Mock()
    monkeypatch.setattr(bucket["sync_manager"], "request_recovery", recovery)
    try:
        device = coordinator.data["dev1"]
        device.update(scenary="sleep", power="1")
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            sleep_gets={"dev1": integration._sleep_get_context(bucket)},
            fresh_scenary_ids={"dev1"},
        )
        tracking = bucket["sleep_tracking"]["dev1"]
        assert tracking.evidence_revision > 0
        assert tracking.off_observation_scope is None

        # A GET applying OFF alone cannot reuse the cached scene.
        device["power"] = "0"
        tracking.overdue_probe_requested = True
        tracking.start_known = True
        tracking.expected_minutes = int(device["sleep_time"])
        tracking.sleep_started_at_utc = integration.dt_util.utcnow() - timedelta(
            hours=2
        )
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            sleep_gets={"dev1": integration._sleep_get_context(bucket)},
            fresh_scenary_ids=set(),
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert tracking.off_observation_scope is None
        assert not device["sleep_off_confirmed"]
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_not_called()

        # A fallback/cache re-publication of the same OFF has no fresh
        # power evidence, even if the coordinator notifies its listeners.
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids=set(),
            fresh_scenary_ids=set(),
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert tracking.off_observation_scope is None
        assert not device["sleep_off_confirmed"]
        assert "sleep_snapshot_fresh" not in device
        api.async_set_scenary.assert_not_awaited()

        # A GET applying both fields authorizes cleanup.
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            sleep_gets={"dev1": integration._sleep_get_context(bucket)},
            fresh_scenary_ids={"dev1"},
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert tracking.off_observation_scope is not None
        assert device["sleep_off_confirmed"]
        api.async_set_scenary.assert_awaited_once_with(
            "dev1", "occupied", retry=False, on_start=ANY, on_in_flight=ANY
        )
        recovery.assert_called_once_with("dev1", require_fresh=True)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sleep_expiry_recheck_clears_before_lock_then_remains_eligible(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """An expiry cleared during a lock wait must not latch the session."""
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api.async_set_scenary = AsyncMock(return_value={})
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry.runtime_data.coordinator
    tracking = bucket["sleep_tracking"]["dev1"]
    recovery = Mock()
    monkeypatch.setattr(bucket["sync_manager"], "request_recovery", recovery)

    lock = integration.acquire_device_lock(hass, entry.entry_id, "dev1")
    await lock.acquire()
    try:
        coordinator.data["dev1"].update(
            scenary="sleep",
            sleep_off_confirmed=True,
            power="0",
        )
        _mark_synthetic_sleep_off(bucket)
        coordinator.async_update_listeners()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first_task = bucket["sleep_expiry_task"]
        assert not first_task.done()
        tracking.off_observation_scope = None
    finally:
        lock.release()

    await asyncio.wait_for(first_task, timeout=5)
    assert not tracking.force_exit_requested
    assert "dev1" not in bucket.get("sleep_exit_pending", {})
    api.async_set_scenary.assert_not_awaited()
    recovery.assert_not_called()

    # Evidence from the same raw OFF snapshot cannot override an accepted ON
    # whose overlay still disagrees with that snapshot.
    integration.optimistic_set(hass, entry.entry_id, "dev1", "power", "1")
    _mark_synthetic_sleep_off(bucket)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_not_awaited()
    integration.helpers.optimistic_invalidate(hass, entry.entry_id, "dev1", "power")

    # Expiry recurs in the same Sleep session: cleanup is still eligible.
    _mark_synthetic_sleep_off(bucket)
    coordinator.data["dev1"]["sleep_off_confirmed"] = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once_with(
        "dev1", "occupied", retry=False, on_start=ANY, on_in_flight=ANY
    )
    assert tracking.force_exit_requested
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    recovery.assert_called_once_with("dev1", require_fresh=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("next_action", ["retry", "rearm", "manual", "stop"])
async def test_unsent_sleep_cleanup_waits_for_cooldown_then_remains_eligible(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, next_action
):
    from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI

    api = AirzoneAPI("synthetic@example.invalid", Mock(), token="synthetic")
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[sample_device])
    api._request = AsyncMock(return_value={})
    api._cooldown_until = hass.loop.time() + 600
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry.runtime_data.coordinator
    recovery = Mock()
    monkeypatch.setattr(bucket["sync_manager"], "request_recovery", recovery)
    coordinator.data["dev1"].update(
        scenary="sleep",
        sleep_off_confirmed=True,
        power="0",
    )
    _mark_synthetic_sleep_off(bucket)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    tracking = bucket["sleep_tracking"]["dev1"]
    assert not tracking.force_exit_requested
    assert not bucket["sleep_exit_pending"]
    api._request.assert_not_awaited()
    recovery.assert_not_called()
    first_task = bucket["sleep_expiry_task"]
    for _ in range(3):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
    assert bucket["sleep_expiry_task"] is first_task
    if next_action == "stop":
        assert await hass.config_entries.async_unload(entry.entry_id)
        api._request.assert_not_awaited()
        return
    if next_action in {"rearm", "manual"}:
        previous = tracking.session_revision
        integration.optimistic_set(
            hass,
            entry.entry_id,
            "dev1",
            "scenary",
            "sleep" if next_action == "rearm" else "vacant",
        )
        if next_action == "rearm":
            assert tracking.session_revision > previous
            assert not tracking.force_exit_requested
            assert "dev1" not in bucket["sleep_exit_retry_after"]
            coordinator.data["dev1"]["sleep_off_confirmed"] = False
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        api._request.assert_not_awaited()
        if next_action == "manual":
            assert await hass.config_entries.async_unload(entry.entry_id)
            return
        coordinator.data["dev1"]["sleep_off_confirmed"] = True
        _mark_synthetic_sleep_off(bucket)
    api._cooldown_until = 0
    bucket["sleep_exit_retry_after"]["dev1"] = 0
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api._request.assert_awaited_once()
    assert tracking.force_exit_requested
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    recovery.assert_called_once_with("dev1", require_fresh=True)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sleep_get_dispatched_before_deadline_is_ineligible_on_late_receipt(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A GET dispatched before the deadline cannot authorize cleanup on late receipt."""
    now = integration.dt_util.utcnow()
    monkeypatch.setattr(integration.dt_util, "utcnow", lambda: now)
    api = Mock()
    initial = {**sample_device, "scenary": "sleep", "power": "1"}
    off = {**initial, "power": "0"}
    first_started, release_first = asyncio.Event(), asyncio.Event()
    next_started, release_next = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_installation, *, retry=True, on_start=None):
        nonlocal calls
        calls += 1
        if on_start is not None:
            on_start()
        if calls == 1:
            return [initial]
        if calls == 2:
            first_started.set()
            await release_first.wait()
            return [off]
        next_started.set()
        await release_next.wait()
        return [off]

    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=fetch)
    api.async_set_scenary = AsyncMock()
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory(
        options={integration.CONF_SLEEP_CLEANUP_ENABLED: False}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    clock = 40.0

    async def sleep(delay):
        nonlocal clock
        clock += delay

    manager._clock = manager._pool._clock = manager._budget._clock = lambda: clock
    manager._budget._sleep = sleep
    track = bucket["sleep_tracking"]["dev1"]
    generation = manager.state.generation
    try:
        await manager.event("dev1", {"scenary": "sleep", "power": "1"}, generation)
        _mark_synthetic_sleep_off(bucket)
        track.expected_minutes = int(off["sleep_time"])
        track.off_observation_scope = None
        now = track.end + timedelta(seconds=29)
        manager.request_recovery("dev1")
        worker = manager._quiet_tasks["dev1"]
        await asyncio.wait_for(first_started.wait(), 2)
        now += timedelta(seconds=2)
        release_first.set()
        await asyncio.wait_for(worker, 2)
        assert track.off_observation_scope is None
        assert not entry.runtime_data.coordinator.data["dev1"]["sleep_off_confirmed"]
        assert calls == 2

        manager.request_recovery("dev1")
        worker = manager._quiet_tasks["dev1"]
        await asyncio.wait_for(next_started.wait(), 2)
        release_next.set()
        await asyncio.wait_for(worker, 2)
        assert track.off_observation_scope == integration.sleep_scope(bucket, "dev1")
        assert entry.runtime_data.coordinator.data["dev1"]["sleep_off_confirmed"]
        assert calls == 3
        api.async_set_scenary.assert_not_awaited()
    finally:
        release_first.set()
        release_next.set()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_get_predating_sleep_exit_readback_cannot_clear_pending_scene(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """Merge the old scene for display, but require newer evidence for the write."""
    api = Mock()
    initial = {**sample_device, "scenary": "sleep", "power": "1"}
    occupied = {**initial, "scenary": "occupied"}
    first_started, release_first, next_started, release_next = (
        asyncio.Event() for _ in range(4)
    )
    calls = 0

    async def fetch(_installation, *, retry=True, on_start=None):
        nonlocal calls
        calls += 1
        if on_start is not None:
            on_start()
        if calls == 1:
            return [initial]
        if calls == 2:
            first_started.set()
            await release_first.wait()
        else:
            next_started.set()
            await release_next.wait()
        return [occupied]

    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=fetch)
    api.async_set_scenary = AsyncMock()
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    now = 40.0

    async def sleep(delay):
        nonlocal now
        now += delay

    manager._clock = manager._pool._clock = manager._budget._clock = lambda: now
    manager._budget._sleep = sleep
    try:
        manager.request_recovery("dev1")
        worker = manager._quiet_tasks["dev1"]
        await asyncio.wait_for(first_started.wait(), 2)
        pending = bucket.setdefault("sleep_exit_pending", {})
        pending["dev1"] = "unverified"
        manager.request_recovery("dev1", require_fresh=True)
        release_first.set()
        await asyncio.wait_for(next_started.wait(), 2)
        assert manager.state.data["dev1"]["scenary"] == "occupied"
        assert pending == {"dev1": "unverified"}

        release_next.set()
        await asyncio.wait_for(worker, 2)
        assert pending == {}
        assert calls == 3
        api.async_set_scenary.assert_not_awaited()
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
