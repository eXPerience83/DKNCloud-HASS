"""Exercise the synchronization owner through Home Assistant's real lifecycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import push_adapter
from custom_components.airzoneclouddaikin.const import DOMAIN


def _arm_synthetic_expired_timer(bucket, dev_id="dev1") -> None:
    """Pass expiry + settle for a current session, without fabricating OFF."""
    tracking = bucket["sleep_tracking"][dev_id]
    device = bucket["coordinator"].data[dev_id]
    minutes = int(device.get("sleep_time") or 30)
    started = integration.dt_util.utcnow() - timedelta(minutes=minutes + 1)
    tracking.last_scenary = "sleep"
    tracking.sleep_started_at_utc = started
    tracking.cleanup_started_at_utc = started
    tracking.expected_minutes = minutes
    tracking.start_known = True
    tracking.duration_invalidated = False
    tracking.expiry_off_reports = 0
    tracking.expiry_revisions = None
    tracking.expiry_probe_requested = True


def _mark_synthetic_sleep_off(bucket, dev_id="dev1") -> None:
    """Simulate two independent OFFs received *after* the timer deadline."""
    _arm_synthetic_expired_timer(bucket, dev_id)
    tracking = bucket["sleep_tracking"][dev_id]
    tracking.observed_on = True
    tracking.consecutive_off_reports = 2
    tracking.expiry_off_reports = 2
    tracking.expiry_revisions = integration.sleep_intent_revisions(bucket, dev_id)
    bucket["coordinator"].data[dev_id]["sleep_expiry_verified"] = True


def _assert_occupied_write(api):
    """A cleanup must be non-retried and guarded at network dispatch."""
    api.async_set_scenary.assert_awaited_once()
    args, kwargs = api.async_set_scenary.await_args
    assert args == ("dev1", "occupied")
    assert kwargs["retry"] is False
    assert callable(kwargs["on_start"])


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


async def test_explicit_refresh_counts_single_off_snapshot_once(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """The coordinator must not treat one refreshed GET as two OFF reports."""
    first = {**sample_device, "scenary": "occupied", "power": "1"}
    off = {**sample_device, "scenary": "sleep", "power": "0"}
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])

    async def fetch(_installation, *, on_start, **_kwargs):
        on_start()
        return [first if api.fetch_devices.await_count == 1 else off]

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
        assert tracking.observed_on
        _arm_synthetic_expired_timer(bucket)

        # One GET publishes once, then returns via refresh_sync. The latter
        # must reuse derived data without treating the same GET as a second OFF.
        await coordinator.async_refresh()
        assert api.fetch_devices.await_count == 2
        assert tracking.consecutive_off_reports == 1
        assert not coordinator.data["dev1"]["sleep_off_confirmed"]
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_called_once_with("dev1", require_fresh=True)

        # A genuinely separate follow-up OFF Pusher report can confirm it.
        await manager.event("dev1", {"power": "0"}, generation)
        await hass.async_block_till_done()
        _assert_occupied_write(api)
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
        _arm_synthetic_expired_timer(bucket)
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
        recovery.assert_called_once_with("dev1", require_fresh=True)

        # Same PUSH payload is a duplicate, not a second independent OFF.
        await manager.event("dev1", {"power": "0"}, generation)
        await hass.async_block_till_done()
        assert tracking.expiry_off_reports == 1
        api.async_set_scenary.assert_not_awaited()
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            fresh_scenary_ids=set(),
            power_observations={"dev1": (100, integration.dt_util.utcnow())},
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        _assert_occupied_write(api)
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
        assert bucket["optimistic"]["dev1"]["scenary"]["value"] == "occupied"
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


async def test_current_sleep_off_needs_on_then_two_fresh_off_reports(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """Intermediate OFF never cancels Sleep; expiry needs fresh OFF reports."""
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
    recovery.assert_not_called()
    await manager.event("dev1", {"power": "0"}, generation)
    await hass.async_block_till_done()
    api.async_set_scenary.assert_not_awaited()

    # Advance this synthetic session, without waiting in real time.
    _arm_synthetic_expired_timer(bucket)
    for token in (100, 101):
        integration._derive_state(
            hass,
            entry,
            entry.runtime_data.coordinator.data,
            fresh_power_ids={"dev1"},
            fresh_scenary_ids=set(),
            power_observations={"dev1": (token, integration.dt_util.utcnow())},
        )
    entry.runtime_data.coordinator.async_update_listeners()
    await hass.async_block_till_done()
    _assert_occupied_write(api)
    assert bucket["sleep_exit_pending"]["dev1"] == "accepted"

    # Repeated OFF and maintenance cannot replay an already accepted PUT.
    await manager.event("dev1", {"power": "0"}, generation)
    manager.maintenance()
    await hass.async_block_till_done()
    api.async_set_scenary.assert_awaited_once()
    await manager.event("dev1", {"scenary": "occupied"}, generation)
    assert "dev1" not in bucket["sleep_exit_pending"]
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
    assert not tracking.observed_on
    assert tracking.consecutive_off_reports == 0

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
    await manager.event("dev1", {"scenary": "sleep", "power": "1"}, generation)
    _arm_synthetic_expired_timer(bucket)
    monkeypatch.setattr(manager, "request_recovery", Mock())
    await manager.event("dev1", {"power": "0", "update_date": "first"}, generation)
    await manager.event("dev1", {"power": "0", "update_date": "second"}, generation)
    await hass.async_block_till_done()
    assert bucket["sleep_tracking"]["dev1"].consecutive_off_reports == 2
    assert entry.runtime_data.coordinator.data["dev1"]["scenary"] == "sleep"
    assert entry.runtime_data.coordinator.data["dev1"]["sleep_expiry_verified"]
    assert not bucket.get("sleep_expiry_task")
    api.async_set_scenary.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stale_power_snapshot_cannot_trigger_sleep_cleanup(
    hass, monkeypatch, dkn_config_entry_factory, sample_device
):
    """Only two *fresh* OFF reports following this session's ON authorize Home."""
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
            fresh_scenary_ids={"dev1"},
        )
        tracking = bucket["sleep_tracking"]["dev1"]
        assert tracking.observed_on
        assert tracking.consecutive_off_reports == 0
        _arm_synthetic_expired_timer(bucket)

        # A first newly received OFF is insufficient; request readback.
        device["power"] = "0"
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            fresh_scenary_ids=set(),
            power_observations={"dev1": (100, integration.dt_util.utcnow())},
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert tracking.consecutive_off_reports == 1
        assert not device["sleep_off_confirmed"]
        api.async_set_scenary.assert_not_awaited()
        recovery.assert_called_once_with("dev1", require_fresh=True)

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
        assert tracking.consecutive_off_reports == 1
        assert not device["sleep_off_confirmed"]
        assert "sleep_snapshot_fresh" not in device
        api.async_set_scenary.assert_not_awaited()

        # A separate genuine fresh OFF completes the same-session pair.
        integration._derive_state(
            hass,
            entry,
            coordinator.data,
            fresh_power_ids={"dev1"},
            fresh_scenary_ids=set(),
            power_observations={"dev1": (101, integration.dt_util.utcnow())},
        )
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert tracking.consecutive_off_reports == 2
        assert device["sleep_off_confirmed"]
        _assert_occupied_write(api)
        # One recovery follows the first OFF, and another verifies the scene PUT.
        assert recovery.call_count == 2
        recovery.assert_called_with("dev1", require_fresh=True)
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "invalidation", ["power_intents", "scene_preset_intents", "evidence"]
)
async def test_sleep_expiry_recheck_clears_before_lock_then_remains_eligible(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, invalidation
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
    from custom_components.airzoneclouddaikin import helpers

    reached_lock = asyncio.Event()

    def observe_lock(*args):
        reached_lock.set()
        return lock

    monkeypatch.setattr(helpers, "acquire_device_lock", observe_lock)
    await lock.acquire()
    try:
        coordinator.data["dev1"].update(
            scenary="sleep",
            sleep_off_confirmed=True,
            power="0",
        )
        _mark_synthetic_sleep_off(bucket)
        coordinator.async_update_listeners()
        await asyncio.wait_for(reached_lock.wait(), 2)
        first_task = bucket["sleep_expiry_task"]
        assert not first_task.done()
        if invalidation == "power_intents":
            bucket.setdefault("power_intents", {})["dev1"] = 1
        elif invalidation == "scene_preset_intents":
            bucket.setdefault("scene_preset_intents", {})["dev1"] = 1
        else:
            tracking.expiry_off_reports = 0
            coordinator.data["dev1"]["sleep_expiry_verified"] = False
    finally:
        lock.release()

    await asyncio.wait_for(first_task, timeout=5)
    assert not tracking.force_exit_requested
    assert "dev1" not in bucket.get("sleep_exit_pending", {})
    api.async_set_scenary.assert_not_awaited()
    recovery.assert_not_called()

    # Expiry recurs in the same Sleep session: cleanup is still eligible.
    _mark_synthetic_sleep_off(bucket)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    _assert_occupied_write(api)
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
            coordinator.data["dev1"]["sleep_expiry_verified"] = False
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        api._request.assert_not_awaited()
        if next_action == "manual":
            assert await hass.config_entries.async_unload(entry.entry_id)
            return
        coordinator.data["dev1"]["sleep_expiry_verified"] = True
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


@pytest.mark.parametrize("first_off_from_push", [True, False])
async def test_sleep_corroboration_requires_an_independent_later_get(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, first_off_from_push
):
    """An existing worker must complete Sleep's corroboration with a newer GET."""
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
        _arm_synthetic_expired_timer(bucket)
        manager.request_recovery("dev1")
        worker = manager._quiet_tasks["dev1"]
        await asyncio.wait_for(first_started.wait(), 2)
        if first_off_from_push:
            await manager.event("dev1", {"power": "0"}, generation)
        release_first.set()
        next_read = asyncio.create_task(next_started.wait())
        try:
            done, _ = await asyncio.wait(
                (worker, next_read), timeout=2, return_when=asyncio.FIRST_COMPLETED
            )
            assert done == {next_read}
            assert track.consecutive_off_reports == 1
            assert not entry.runtime_data.coordinator.data["dev1"][
                "sleep_off_confirmed"
            ]
            api.async_set_scenary.assert_not_awaited()
        finally:
            next_read.cancel()
            await asyncio.gather(next_read, return_exceptions=True)
        release_next.set()
        await asyncio.wait_for(worker, 2)
        assert track.consecutive_off_reports == 2
        assert entry.runtime_data.coordinator.data["dev1"]["sleep_off_confirmed"]
        assert calls == 3  # bootstrap plus two independent recovery reads
        api.async_set_scenary.assert_not_awaited()
    finally:
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


@pytest.mark.parametrize(
    ("action", "source"),
    [
        ("rearm", "get"),
        ("rearm", "push"),
        ("rearm", "push_wins"),
        ("rearm", "replayed_push"),
        ("rearm", "reload"),
        ("climate", "get"),
        ("mode", "get"),
        ("mode", "p2_failed"),
        ("mode", "already_on"),
        ("mode", "active_sleep"),
        ("mode", "unknown_sleep"),
        ("mode", "cleanup_disabled"),
        ("mode", "ambiguous"),
        ("switch", "get"),
        ("fallback", "get"),
        ("climate", "newer_on"),
        ("switch", "newer_on"),
        ("fallback", "newer_on"),
        ("climate", "ambiguous"),
    ],
)
async def test_sleep_home_barrier_through_entity_services(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, action, source
):
    """Use the real HA entry, entity services, field merge and RecoveryPool."""
    initial = {**sample_device, "scenary": "sleep", "power": "0", "sleep_time": 30}
    if source == "already_on":
        initial.update(power="1", mode="2")
    home = {**initial, "scenary": "occupied"}
    old_started, release_old, new_started, release_new, demanded = (
        asyncio.Event() for _ in range(5)
    )
    sequence = []
    calls = 0
    api = Mock()

    async def fetch(_installation, *, retry=True, on_start=None):
        nonlocal calls
        calls += 1
        if on_start is not None:
            on_start()
        if calls == 1:
            return [initial]
        if calls == 2:
            sequence.append("GET old")
            old_started.set()
            await release_old.wait()
        else:
            sequence.append("GET fresh")
            new_started.set()
            await release_new.wait()
        return [home]

    async def put(_device, payload, **kwargs):
        if guard := kwargs.get("on_start"):
            guard()
        sequence.append(payload["device"]["scenary"])
        if source == "ambiguous":
            raise TimeoutError("Unknown Home PUT outcome")

    async def send(payload, **kwargs):
        if guard := kwargs.get("on_start"):
            guard()
        option = payload["event"]["option"]
        assert option == (
            "P2"
            if source == "already_on" or sequence[-1:] == ["P1"] and action == "mode"
            else "P1"
        )
        if option == "P1":
            assert payload["event"]["value"] == 1
        sequence.append(option)
        if option == "P2" and source == "p2_failed":
            raise HomeAssistantError("Synthetic P2 failure")

    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(side_effect=fetch)
    api.put_device_fields = AsyncMock(side_effect=put)
    api.send_event = AsyncMock(side_effect=send)
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    entry = dkn_config_entry_factory(
        options={"sleep_cleanup_enabled": False} if source == "cleanup_disabled" else {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    manager = bucket["sync_manager"]
    registry = er.async_get(hass)
    platform = "climate" if action in {"rearm", "climate", "mode"} else "switch"
    unique_id = "dev1_climate" if platform == "climate" else "dev1_power"
    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id
    if action == "fallback":
        hass.states.async_remove(
            registry.async_get_entity_id("climate", DOMAIN, "dev1_climate")
        )

    now = 40.0

    async def sleep(delay):
        nonlocal now
        now += delay

    manager._clock = manager._pool._clock = manager._budget._clock = lambda: now
    manager._budget._sleep = sleep
    request = manager.request_recovery

    def observe_request(device, *, require_fresh=False):
        worker = request(device, require_fresh=require_fresh)
        if require_fresh:
            demanded.set()
        return worker

    monkeypatch.setattr(manager, "request_recovery", observe_request)
    service = None
    service_name, values = {
        "rearm": ("set_preset_mode", {"preset_mode": "sleep"}),
        "mode": ("set_hvac_mode", {"hvac_mode": "cool"}),
    }.get(action, ("turn_on", {}))
    replayed_home = {"scenary": "occupied", "update_date": "2026-11-01T00:00:00Z"}
    try:
        if source == "newer_on":
            _mark_synthetic_sleep_off(bucket)
            lock = integration.acquire_device_lock(hass, entry.entry_id, "dev1")
            commands = []
            try:
                async with lock, asyncio.timeout(2):
                    for revision in (1, 2):
                        commands.append(
                            asyncio.create_task(
                                hass.services.async_call(
                                    platform,
                                    service_name,
                                    {"entity_id": entity_id},
                                    blocking=True,
                                )
                            )
                        )
                        while bucket.get("power_intents", {}).get("dev1") != revision:
                            await asyncio.sleep(0)
                old, latest = await asyncio.wait_for(
                    asyncio.gather(*commands, return_exceptions=True), 2
                )
                assert old is None
                assert isinstance(latest, HomeAssistantError)
                assert "Confirm Home" in str(latest)
                assert demanded.is_set()
                api.send_event.assert_not_awaited()
                api.put_device_fields.assert_not_awaited()
            finally:
                release_old.set()
                release_new.set()
                for command in commands:
                    command.cancel()
                await asyncio.gather(*commands, return_exceptions=True)
            return
        if source in {
            "already_on",
            "active_sleep",
            "unknown_sleep",
            "cleanup_disabled",
        }:
            if source == "cleanup_disabled":
                _mark_synthetic_sleep_off(bucket)
            elif source == "active_sleep":
                bucket["sleep_tracking"]["dev1"].start_known = True
            await hass.services.async_call(
                platform,
                service_name,
                {"entity_id": entity_id, **values},
                blocking=True,
            )
            assert sequence == (["P2"] if source == "already_on" else ["P1", "P2"])
            api.put_device_fields.assert_not_awaited()
            return
        if source == "replayed_push":
            await manager.event("dev1", replayed_home, manager.state.generation)
            await manager.event("dev1", {"scenary": "sleep"}, manager.state.generation)
        manager.request_recovery("dev1")
        await asyncio.wait_for(old_started.wait(), 2)
        _mark_synthetic_sleep_off(bucket)
        service = asyncio.create_task(
            hass.services.async_call(
                platform,
                service_name,
                {"entity_id": entity_id, **values},
                blocking=True,
            )
        )
        await asyncio.wait_for(demanded.wait(), 2)
        assert sequence == ["GET old", "occupied"]
        if source == "ambiguous":
            with pytest.raises(HomeAssistantError, match="Confirm Home"):
                await asyncio.wait_for(service, 2)
            assert "dev1" in bucket["sleep_exit_pending"]
            assert api.send_event.await_count == 0
            assert api.put_device_fields.await_count == 1
            demanded.clear()
            service = asyncio.create_task(
                hass.services.async_call(
                    platform,
                    service_name,
                    {"entity_id": entity_id, **values},
                    blocking=True,
                )
            )
            await asyncio.wait_for(demanded.wait(), 2)
        release_old.set()
        await asyncio.wait_for(new_started.wait(), 2)
        assert sequence == ["GET old", "occupied", "GET fresh"]
        if source == "reload":
            assert await hass.config_entries.async_unload(entry.entry_id)
            await asyncio.wait_for(service, 2)
            release_new.set()
            assert await hass.config_entries.async_setup(entry.entry_id)
            new_bucket = hass.data[DOMAIN][entry.entry_id]
            assert new_bucket is not bucket
            assert not new_bucket.get("sleep_exit_pending")
            api.put_device_fields.assert_awaited_once()
            return
        if source == "replayed_push":
            confirmation = bucket["sleep_exit_pending"]["dev1"]
            await manager.event("dev1", replayed_home, manager.state.generation)
            assert bucket["sleep_exit_pending"]["dev1"] is confirmation
            assert not confirmation.done()
            assert api.put_device_fields.await_count == 1
            assert not service.done()
        if source in {"push", "push_wins"}:
            await manager.event(
                "dev1",
                {"scenary": "occupied" if source == "push" else "sleep"},
                manager.state.generation,
            )
        if source != "push":
            release_new.set()
        if source == "p2_failed":
            with pytest.raises(HomeAssistantError, match="Synthetic P2 failure"):
                await asyncio.wait_for(service, 2)
            assert sequence == ["GET old", "occupied", "GET fresh", "P1", "P2"]
            assert bucket["optimistic"]["dev1"]["power"]["value"] == "1"
            return
        await asyncio.wait_for(service, 2)
        if source == "push_wins":
            assert sequence == ["GET old", "occupied", "GET fresh"]
            assert "dev1" in bucket["sleep_exit_pending"]
        else:
            assert sequence == [
                "GET old",
                "occupied",
                "GET fresh",
                "sleep" if action == "rearm" else "P1",
                *(["P2"] if action == "mode" else []),
            ]
    finally:
        release_old.set()
        release_new.set()
        if service is not None and not service.done():
            service.cancel()
        if service is not None:
            await asyncio.gather(service, return_exceptions=True)
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_poll_started_before_scene_readback_cannot_confirm_home(
    hass, dkn_config_entry_factory, sample_device
):
    """Legacy polling checks the same dispatch boundary as shared recovery."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    initial = {**sample_device, "scenary": "sleep", "power": "0"}
    arrived, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_installation, *, on_start):
        nonlocal calls
        calls += 1
        on_start()
        if calls == 1:
            return [initial]
        if calls == 2:
            arrived.set()
            await release.wait()
        return [{**initial, "scenary": "occupied"}]

    api = Mock(
        fetch_installations=AsyncMock(return_value=[{"installation_id": "fake"}]),
        fetch_devices=AsyncMock(side_effect=fetch),
    )
    await integration._async_update_data(hass, entry, api)
    bucket = hass.data[DOMAIN][entry.entry_id]
    old_poll = asyncio.create_task(integration._async_update_data(hass, entry, api))
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        confirmation = hass.loop.create_future()
        bucket["sleep_exit_pending"] = {"dev1": confirmation}
        release.set()
        assert (await asyncio.wait_for(old_poll, 2))["dev1"]["scenary"] == "occupied"
        assert not confirmation.done()
        await integration._async_update_data(hass, entry, api)
        assert confirmation.result() is True
    finally:
        release.set()
        await asyncio.gather(old_poll, return_exceptions=True)


@pytest.mark.parametrize("uses_push_manager", [False, True])
async def test_duration_get_predating_edit_cannot_invalidate_new_sleep(
    hass,
    dkn_config_entry_factory,
    sample_device,
    monkeypatch,
    uses_push_manager,
):
    """An otherwise fresh response can carry duration older than the edit."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    initial = {**sample_device, "power": "0", "scenary": "occupied", "sleep_time": 30}
    arrived, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_installation, *, retry=True, on_start=None):
        nonlocal calls
        calls += 1
        if on_start is not None:
            on_start()
        if calls == 2:
            arrived.set()
            await release.wait()
        return [
            initial | {"sleep_time": 40, "scenary": "sleep"}
            if calls >= 3
            else dict(initial)
        ]

    api = Mock(
        fetch_installations=AsyncMock(return_value=[{"installation_id": "fake"}]),
        fetch_devices=AsyncMock(side_effect=fetch),
        put_device_fields=AsyncMock(),
    )
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    if uses_push_manager:
        monkeypatch.setattr(
            integration, "build_verified_adapter", Mock(connect=AsyncMock())
        )
        monkeypatch.setattr(integration.SyncManager, "start", Mock())
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry.runtime_data.coordinator
    if uses_push_manager:
        manager = bucket["sync_manager"]
        now = 40.0

        async def sleep(delay):
            nonlocal now
            now += delay

        manager._clock = manager._pool._clock = manager._budget._clock = lambda: now
        manager._budget._sleep = sleep

        async def refresh():
            await manager.recover("dev1")
    else:
        refresh = coordinator.async_refresh

    registry = er.async_get(hass)
    number = registry.async_get_entity_id("number", DOMAIN, "dev1_sleep_time")
    climate = registry.async_get_entity_id("climate", DOMAIN, "dev1_climate")
    old = asyncio.create_task(refresh())
    try:
        await asyncio.wait_for(arrived.wait(), 5)
        await hass.services.async_call(
            "number", "set_value", {"entity_id": number, "value": 40}, blocking=True
        )
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": climate, "preset_mode": "sleep"},
            blocking=True,
        )
        assert [call.args for call in api.put_device_fields.await_args_list] == [
            ("dev1", {"sleep_time": 40}),
            ("dev1", {"device": {"scenary": "sleep"}}),
        ]
        assert coordinator.data["dev1"]["sleep_time"] == 30
        tracking = bucket["sleep_tracking"]["dev1"]
        session = tracking.session_revision
        assert tracking.expected_minutes == 40
        release.set()
        await asyncio.wait_for(old, 5)
        assert coordinator.data["dev1"]["sleep_time"] == 30
        assert tracking.start_known and not tracking.duration_invalidated
        await refresh()
        assert coordinator.data["dev1"]["sleep_time"] == 40
        assert tracking.start_known and not tracking.duration_invalidated
        assert tracking.expected_minutes == 40
        assert tracking.session_revision == session
    finally:
        release.set()
        await asyncio.gather(old, return_exceptions=True)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("action", ["climate", "switch", "fallback", "mode"])
async def test_cleanup_http_422_surfaces_actionable_wake_error(
    hass,
    monkeypatch,
    dkn_config_entry_factory,
    sample_device,
    socket_enabled,
    aiohttp_server,
    action,
):
    """A real rejected cleanup must surface through the HA wake service."""
    from aiohttp import web

    from custom_components.airzoneclouddaikin import airzone_api

    device = sample_device | {"scenary": "sleep", "power": "0", "sleep_time": 30}
    requests = []

    async def installations(_request):
        return web.json_response([{"installation_id": "fake"}])

    async def devices(_request):
        return web.json_response([device])

    async def command(request):
        requests.append((request.method, request.path, await request.json()))
        return web.Response(status=422)

    app = web.Application()
    app.router.add_get("/installation_relations", installations)
    app.router.add_get("/devices", devices)
    app.router.add_put("/devices/dev1", command)
    app.router.add_post("/events", command)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    bucket = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry.runtime_data.coordinator
    registry = er.async_get(hass)
    climate = registry.async_get_entity_id("climate", DOMAIN, "dev1_climate")
    switch = registry.async_get_entity_id("switch", DOMAIN, "dev1_power")
    assert climate and switch
    try:
        _mark_synthetic_sleep_off(bucket)
        coordinator.async_update_listeners()
        await asyncio.wait_for(bucket["sleep_expiry_task"], 5)
        assert bucket["sleep_exit_pending"]["dev1"] == "rejected"
        await coordinator.async_refresh()
        assert coordinator.data["dev1"]["scenary"] == "sleep"

        if action == "fallback":
            hass.states.async_remove(climate)
        with pytest.raises(HomeAssistantError, match="Confirm Home before turning on"):
            await hass.services.async_call(
                "climate" if action in {"climate", "mode"} else "switch",
                "set_hvac_mode" if action == "mode" else "turn_on",
                {
                    "entity_id": climate if action in {"climate", "mode"} else switch,
                    **({"hvac_mode": "cool"} if action == "mode" else {}),
                },
                blocking=True,
            )
        assert requests == [
            ("PUT", "/devices/dev1", {"device": {"scenary": "occupied"}})
        ]
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "source",
    [
        "old_get",
        "current_get",
        "old_push",
        "old_power_only",
        "replayed_get_power",
        "old_scene_get",
        "undated_transition",
    ],
)
async def test_sleep_votes_require_current_dispatch_or_nonreplayed_push(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, source
):
    initial = {**sample_device, "scenary": "sleep", "power": "0", "sleep_time": 30}
    waiting, writer, dispatched, reply = (asyncio.Event() for _ in range(4))
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "fake"}])
    api.fetch_devices = AsyncMock(return_value=[initial])
    api.async_set_scenary = AsyncMock()
    api.send_event = AsyncMock()
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
    tracking = bucket["sleep_tracking"]["dev1"]
    monkeypatch.setattr(manager, "request_recovery", Mock())

    async def fetch(_installation, *, on_start, **_kwargs):
        waiting.set()
        await writer.wait()
        on_start()
        dispatched.set()
        await reply.wait()
        return [dict(initial)]

    task = None
    try:
        if source == "undated_transition":
            await manager.event("dev1", {"scenary": "occupied"}, generation)
            await manager.event(
                "dev1",
                {"scenary": "sleep", "update_date": "2026-11-01T00:00:02Z"},
                generation,
            )
            assert tracking.last_scenary == "sleep"
            await manager.event("dev1", {"scenary": "occupied"}, generation)
            assert tracking.last_scenary == "occupied"
            return
        if source in {"old_scene_get", "replayed_get_power"}:
            if source == "old_scene_get":
                initial["scenary"] = "occupied"
            else:
                replay = {
                    "scenary": "sleep",
                    "power": "0",
                    "update_date": "2026-11-01T00:00:01Z",
                }
                await manager.event("dev1", replay, generation)
                bucket["uncertain_power"]["dev1"] = object()
                initial["power"] = "1"
            api.fetch_devices.side_effect = fetch
            task = asyncio.create_task(manager.recover("dev1"))
            writer.set()
            await asyncio.wait_for(dispatched.wait(), 3)
            if source == "old_scene_get":
                bucket["sleep_exit_pending"] = {"dev1": "unverified"}
            else:
                await manager.event("dev1", replay, generation)
            reply.set()
            await asyncio.wait_for(task, 3)
            if source == "old_scene_get":
                assert bucket["sleep_exit_pending"] == {"dev1": "unverified"}
            else:
                assert "dev1" not in bucket["uncertain_power"]
                assert manager.state.data["dev1"]["power"] == "1"
            return
        if source in {"old_push", "old_power_only"}:
            replays = [
                {
                    "power": "0",
                    "scenary": "sleep",
                    "update_date": f"2026-11-01T00:00:0{i}Z",
                }
                for i in range(2)
            ]
            if source == "old_power_only":
                for patch in replays:
                    patch.pop("scenary")
            for patch in replays:
                await manager.event("dev1", patch, generation)
            await manager.event("dev1", {"scenary": "occupied"}, generation)
            await manager.event(
                "dev1",
                {"scenary": "sleep", "update_date": "2026-11-01T00:00:02Z"},
                generation,
            )
            _arm_synthetic_expired_timer(bucket)
            for patch in replays:
                await manager.event("dev1", patch, generation)
        else:
            _arm_synthetic_expired_timer(bucket)
            api.fetch_devices.side_effect = fetch
            task = asyncio.create_task(manager.recover("dev1"))
            async with asyncio.timeout(3):
                await waiting.wait()
                if source == "old_get":
                    writer.set()
                    await dispatched.wait()
                bucket["power_intents"] = {"dev1": 1}
                writer.set()
                reply.set()
                await task

        assert tracking.expiry_off_reports == int(source == "current_get")
        api.async_set_scenary.assert_not_awaited()
        if source == "current_get":
            await manager.event("dev1", {"power": "1"}, generation)
            assert tracking.expiry_off_reports == 0
        # New undated scene messages can still contribute independent power.
        await manager.event("dev1", {"power": "0", "scenary": "sleep"}, generation)
        assert tracking.expiry_off_reports == 1
        await manager.event(
            "dev1", {"power": "0", "update_date": "2026-11-01T00:00:05Z"}, generation
        )
        await hass.async_block_till_done()
        _assert_occupied_write(api)
        api.send_event.assert_not_awaited()
    finally:
        writer.set()
        reply.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        assert await hass.config_entries.async_unload(entry.entry_id)
