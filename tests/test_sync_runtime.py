"""Exercise the synchronization owner through Home Assistant's real lifecycle."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

from homeassistant.helpers import entity_registry as er

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.helpers import optimistic_set


async def test_verified_connector_runtime_and_silent_local_clock(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, freezer
):
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_scheduler.FALLBACK_READ_DELAY", 0
    )
    device = {**sample_device, "connection_date": datetime.now(UTC).isoformat()}
    information = AsyncMock()
    fetches = []

    class API:
        def __init__(self, *args, **kwargs):
            self.on_write_auth_failure = None
            self.request_information = information

        async def fetch_installations(self):
            return [{"installation_id": "synthetic-installation"}]

        async def fetch_devices(self, installation, *, retry=True):
            fetches.append(installation)
            return [dict(device)]

    monkeypatch.setattr(integration, "AirzoneAPI", API)
    subscribed = asyncio.Event()

    async def connect(manager):
        await manager.subscription("dev1", True)
        await manager._resync_tasks["dev1"]
        await manager.event("dev1", {"power": "0"}, manager.state.generation)
        subscribed.set()
        await asyncio.Event().wait()

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    monkeypatch.setattr(
        integration,
        "build_verified_adapter",
        lambda api, session: Mock(connect=connect),
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.wait_for(subscribed.wait(), 5)
    coordinator = entry.runtime_data.coordinator
    manager = hass.data[DOMAIN][entry.entry_id]["sync_manager"]
    assert coordinator.update_interval is None
    assert coordinator.data["dev1"]["power"] == "0"
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
    optimistic_set(hass, entry.entry_id, "dev1", "disabled_field", "pending")
    freezer.tick(timedelta(seconds=601))
    manager.maintenance()
    await hass.async_block_till_done()
    assert hass.states.get(online_id).state == "off"
    notify.assert_not_called()
    freezer.tick(timedelta(seconds=91))
    manager.maintenance()
    await hass.async_block_till_done()
    notify.assert_called_once()
    assert "disabled_field" not in hass.data[DOMAIN][entry.entry_id]["optimistic"].get(
        "dev1", {}
    )
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
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not replacement._tasks
