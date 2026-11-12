"""Exercise the synchronization owner through Home Assistant's real lifecycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import entity_registry as er

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import push_adapter
from custom_components.airzoneclouddaikin.const import DOMAIN


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

        async def fetch_devices(self, installation, *, retry=True):
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


async def test_explicit_refresh_preserves_derived_scenary_in_coordinator(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A coordinator refresh must return the scene fields it just published."""
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
        assert coordinator.data["dev1"]["effective_scenary"] == "occupied"
        assert coordinator.data["dev1"]["sleep_expired"] is False
        await coordinator.async_refresh()
        assert coordinator.data["dev1"]["scenary"] == "sleep"
        assert coordinator.data["dev1"]["effective_scenary"] == "sleep"
        assert coordinator.data["dev1"]["sleep_expired"] is False
        assert api.fetch_devices.await_count == 2
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_explicit_refresh_counts_single_off_snapshot_once(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """The coordinator must not treat one refreshed GET as two OFF reports."""
    first = {**sample_device, "scenary": "occupied", "power": "1"}
    off = {**sample_device, "scenary": "sleep", "power": "0"}
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=[[first], [off]])
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
        assert tracking.observed_on

        # One GET publishes once, then returns via refresh_sync. The latter
        # must reuse derived data without treating the same GET as a second OFF.
        await coordinator.async_refresh()
        assert api.fetch_devices.await_count == 2
        assert tracking.consecutive_off_reports == 1
        assert not coordinator.data["dev1"]["sleep_off_confirmed"]
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_called_once_with("dev1")

        # A genuinely separate follow-up OFF Pusher report can confirm it.
        await manager.event("dev1", {"power": "0"}, generation)
        await hass.async_block_till_done()
        api.async_set_scenary.assert_awaited_once_with("dev1", "occupied", retry=False)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_inflight_get_discarded_power_cannot_confirm_pushed_off(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """An old GET power rejected by SyncState is not a second OFF report."""
    entered, release = asyncio.Event(), asyncio.Event()
    first = {**sample_device, "scenary": "occupied", "power": "1"}
    old_get = {**sample_device, "scenary": "sleep", "power": "1"}
    calls = 0

    async def fetch(_installation, **_kwargs):
        nonlocal calls
        calls += 1
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
        assert tracking.observed_on
        refresh = asyncio.create_task(coordinator.async_refresh())
        await asyncio.wait_for(entered.wait(), timeout=3)

        # GET began with an ON power value, but a newer Pusher OFF wins.
        await manager.event("dev1", {"power": "0"}, generation)
        assert tracking.consecutive_off_reports == 1
        api.async_set_scenary.assert_not_awaited()
        release.set()
        await asyncio.wait_for(refresh, timeout=3)

        # The returned GET did not apply its stale power field.
        assert manager.state.data["dev1"]["power"] == "0"
        assert "power" not in manager.touched.get("dev1", set())
        assert tracking.consecutive_off_reports == 1
        assert coordinator.data["dev1"]["sleep_off_confirmed"] is False
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_called_once_with("dev1")

        await manager.event("dev1", {"power": "0"}, generation)
        await hass.async_block_till_done()
        api.async_set_scenary.assert_awaited_once_with("dev1", "occupied", retry=False)
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
    api.on_write_auth_failure()
    assert manager._auth_failed
    api.on_write_auth_failure()
    entry.async_start_reauth_if_available.assert_called_once_with(hass)
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
        sleep_expired=True,
        sleep_off_confirmed=True,
        power="0",
    )
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
            coordinator.data["dev1"]["sleep_expired"] = False
            coordinator.data["dev1"]["sleep_off_confirmed"] = False
        lock.release()
        await hass.async_block_till_done()
        api.async_set_scenary.assert_not_awaited()
        if stop == "race_new_sleep":
            assert not bucket["sleep_tracking"]["dev1"].force_exit_requested
            release.set()
            coordinator.data["dev1"]["sleep_expired"] = True
            coordinator.data["dev1"]["sleep_off_confirmed"] = True
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
        assert bucket["optimistic"]["dev1"]["scenary"]["value"] == "occupied"
        recovery.assert_called_once_with("dev1")
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


async def test_current_sleep_off_needs_on_then_two_fresh_off_reports(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """A retained startup OFF must not cause cleanup; confirmed ON→OFF may."""
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
    recovery = Mock()
    monkeypatch.setattr(manager, "request_recovery", recovery)
    generation = manager.state.generation

    # A restored prior OFF with Sleep cannot terminate a new local session.
    await manager.event("dev1", {"scenary": "sleep", "power": "0"}, generation)
    await manager.event("dev1", {"power": "0"}, generation)
    await hass.async_block_till_done()
    api.async_set_scenary.assert_not_awaited()

    # Observe ON in this session, then require corroboration of a new OFF.
    await manager.event("dev1", {"power": "1"}, generation)
    await manager.event("dev1", {"power": "0"}, generation)
    assert not bucket["sleep_tracking"]["dev1"].force_exit_requested
    api.async_set_scenary.assert_not_awaited()
    recovery.assert_called_once_with("dev1")
    await manager.event("dev1", {"power": "0"}, generation)
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once_with("dev1", "occupied", retry=False)
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"

    # Repeated OFF and maintenance cannot replay an already accepted PUT.
    await manager.event("dev1", {"power": "0"}, generation)
    manager.maintenance()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once()
    await manager.event("dev1", {"scenary": "occupied"}, generation)
    assert "dev1" not in bucket["sleep_exit_pending"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_power_snapshot_cannot_trigger_sleep_cleanup(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """Cached OFF from a failed installation must not authorize a scene PUT."""
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
    coordinator.data["dev1"].update(
        scenary="sleep",
        power="0",
        sleep_expired=True,
        sleep_off_confirmed=False,
        sleep_snapshot_fresh=False,
    )
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_not_awaited()

    coordinator.data["dev1"]["sleep_snapshot_fresh"] = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    # Expired local UI status alone cannot authorize an automatic PUT.
    api.async_set_scenary.assert_not_awaited()

    coordinator.data["dev1"]["sleep_off_confirmed"] = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once_with("dev1", "occupied", retry=False)
    recovery.assert_called_once_with("dev1")
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
            sleep_expired=True,
            sleep_off_confirmed=True,
            power="0",
        )
        coordinator.async_update_listeners()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first_task = bucket["sleep_expiry_task"]
        assert not first_task.done()
        coordinator.data["dev1"]["sleep_expired"] = False
        coordinator.data["dev1"]["sleep_off_confirmed"] = False
    finally:
        lock.release()

    await asyncio.wait_for(first_task, timeout=5)
    assert not tracking.force_exit_requested
    assert "dev1" not in bucket.get("sleep_exit_pending", {})
    api.async_set_scenary.assert_not_awaited()
    recovery.assert_not_called()

    # Expiry recurs in the same Sleep session: cleanup is still eligible.
    coordinator.data["dev1"]["sleep_expired"] = True
    coordinator.data["dev1"]["sleep_off_confirmed"] = True
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once_with("dev1", "occupied", retry=False)
    assert tracking.force_exit_requested
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    recovery.assert_called_once_with("dev1")
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
        sleep_expired=True,
        sleep_off_confirmed=True,
        power="0",
    )
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
            coordinator.data["dev1"]["sleep_expired"] = False
            coordinator.data["dev1"]["sleep_off_confirmed"] = False
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        api._request.assert_not_awaited()
        if next_action == "manual":
            assert await hass.config_entries.async_unload(entry.entry_id)
            return
        coordinator.data["dev1"]["sleep_expired"] = True
        coordinator.data["dev1"]["sleep_off_confirmed"] = True
    api._cooldown_until = 0
    bucket["sleep_exit_retry_after"]["dev1"] = 0
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    api._request.assert_awaited_once()
    assert tracking.force_exit_requested
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"
    recovery.assert_called_once_with("dev1")
    assert await hass.config_entries.async_unload(entry.entry_id)
