"""Exercise real Home Assistant Store I/O and the Sleep entry lifecycle."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState
from homeassistant.helpers import entity_registry as er, storage
from homeassistant.util.file import WriteError
from pytest_homeassistant.common import async_fire_time_changed

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.sleep import sleep_cleanup_due

_REAL_FILE_IO = storage._FileStoreIO


@pytest.fixture(autouse=True)
def real_sleep_store(monkeypatch, tmp_path, hass_storage):
    """Use the disk backend for DKN only, keeping unrelated HA stores isolated."""
    mock_io = storage._FileStoreIO

    def store_io(hass, key, path, private, atomic_writes):
        if key.startswith(f"{DOMAIN}_sleep_"):
            return _REAL_FILE_IO(hass, key, str(tmp_path / key), private, atomic_writes)
        return mock_io(hass, key, path, private, atomic_writes)

    monkeypatch.setattr(storage, "_FileStoreIO", store_io)


@pytest.fixture
def backend(monkeypatch, sample_device):
    device = sample_device | {"scenary": "occupied", "power": "0", "sleep_time": 30}
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "site"}])

    async def devices(*_args, on_start=None, **_kwargs):
        if on_start:
            on_start()
        return [device.copy()]

    async def scene(_device, fields, **kwargs):
        if guard := kwargs.get("on_start"):
            guard()
        device["scenary"] = fields["device"]["scenary"]

    api.fetch_devices = AsyncMock(side_effect=devices)
    api.put_device_fields = AsyncMock(side_effect=scene)

    async def cleanup(device_id, value, **kwargs):
        await scene(device_id, {"device": {"scenary": value}}, **kwargs)

    api.async_set_scenary = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(integration, "build_verified_adapter", Mock(return_value=None))
    return api, device


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return hass.data[DOMAIN][entry.entry_id]


async def _select_sleep(hass):
    entity_id = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, "dev1_climate"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": entity_id, "preset_mode": "sleep"},
        blocking=True,
    )


def _store(hass, entry):
    return storage.Store(hass, 1, f"{DOMAIN}_sleep_{entry.entry_id}")


@pytest.mark.parametrize("blocked_write", [False, True])
async def test_reload_waits_for_owned_sleep_snapshot(
    hass, monkeypatch, dkn_config_entry_factory, backend, blocked_write
):
    entry = dkn_config_entry_factory()
    bucket = await _setup(hass, entry)
    store = _store(hass, entry)
    started_write, release_write = asyncio.Event(), asyncio.Event()
    started_reload, loaded_successor = asyncio.Event(), asyncio.Event()
    write = _REAL_FILE_IO.async_write
    load = storage.Store.async_load
    writes = []

    async def gated_write(io, mode, data):
        if io._key == store.key:
            if not writes:
                started_write.set()
                if blocked_write:
                    await release_write.wait()
            writes.append(json.loads(data)["data"]["sessions"])
        await write(io, mode, data)

    async def observe_load(current):
        if current.key == store.key:
            loaded_successor.set()
        return await load(current)

    async def reload():
        started_reload.set()
        return await hass.config_entries.async_reload(entry.entry_id)

    monkeypatch.setattr(_REAL_FILE_IO, "async_write", gated_write)
    monkeypatch.setattr(storage.Store, "async_load", observe_load)
    reload_task = None
    try:
        await _select_sleep(hass)
        original = bucket["sleep_tracking"]["dev1"].sleep_started_at_utc
        assert not started_write.is_set()
        if blocked_write:
            async with asyncio.timeout(5):
                await started_write.wait()
        reload_task = asyncio.create_task(reload())
        await started_reload.wait()
        if blocked_write:
            # Successor bootstrap must wait for the blocked physical write.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(loaded_successor.wait(), 0.1)
        release_write.set()
        assert await reload_task
        successor = hass.data[DOMAIN][entry.entry_id]
        assert successor is not bucket
        tracking = successor["sleep_tracking"]["dev1"]
        assert tracking.confidence == "restored"
        assert tracking.sleep_started_at_utc == original
        assert writes == [
            {
                "dev1": {
                    "started_utc": original.isoformat(),
                    "duration_minutes": 30,
                    "generation": tracking.session_revision,
                }
            }
        ]
    finally:
        release_write.set()
        if reload_task is not None:
            await asyncio.gather(reload_task, return_exceptions=True)
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_unload_keeps_owned_save_live(
    hass, monkeypatch, dkn_config_entry_factory, backend
):
    entry = dkn_config_entry_factory()
    bucket = await _setup(hass, entry)
    store = _store(hass, entry)
    written = asyncio.Event()
    write = _REAL_FILE_IO.async_write

    async def observe_write(io, mode, data):
        await write(io, mode, data)
        if io._key == store.key:
            written.set()

    monkeypatch.setattr(_REAL_FILE_IO, "async_write", observe_write)
    unload_platforms = hass.config_entries.async_unload_platforms
    try:
        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
        )
        assert not await integration.async_unload_entry(hass, entry)
        assert hass.data[DOMAIN][entry.entry_id] is bucket
        assert not bucket["closing"]
        await _select_sleep(hass)
        async with asyncio.timeout(5):
            await written.wait()
        stored = await store.async_load()
        assert stored["sessions"]["dev1"]["duration_minutes"] == 30
    finally:
        monkeypatch.setattr(
            hass.config_entries, "async_unload_platforms", unload_platforms
        )
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_store_write_failure_recovers_without_persisted_clock(
    hass, monkeypatch, dkn_config_entry_factory, backend, caplog, tmp_path
):
    entry = dkn_config_entry_factory()
    await _setup(hass, entry)
    store = _store(hass, entry)
    path = tmp_path / store.key
    await hass.async_add_executor_job(path.mkdir)
    write = _REAL_FILE_IO.async_write
    failures = []

    async def observe_error(io, mode, data):
        try:
            await write(io, mode, data)
        except WriteError:
            if io._key == store.key:
                failures.append(io._key)
                await hass.async_add_executor_job(path.rmdir)
            raise

    monkeypatch.setattr(_REAL_FILE_IO, "async_write", observe_error)
    try:
        await _select_sleep(hass)
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert failures == [store.key]
        assert f"Error writing config for {store.key}" in caplog.text
        tracking = hass.data[DOMAIN][entry.entry_id]["sleep_tracking"]["dev1"]
        assert tracking.confidence == "startup_upper_bound"
        assert not tracking.start_known
        assert not tracking.expiry_verified
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "record",
    [
        "expired",
        "partial",
        "invalid_clock",
        "corrupt",
        "missing",
        "incompatible",
        "duration",
        "scene",
    ],
)
async def test_sleep_store_recovery(
    hass, dkn_config_entry_factory, backend, freezer, tmp_path, record
):
    now = datetime(2026, 11, 22, 12, tzinfo=UTC)
    freezer.move_to(now)
    api, device = backend
    device.update(
        scenary="occupied" if record == "scene" else "sleep",
        sleep_time=60 if record == "duration" else 30,
    )
    if record == "partial":
        device.pop("power")
    entry = dkn_config_entry_factory()
    store = _store(hass, entry)
    started = now + timedelta(hours=1 if record == "invalid_clock" else -1)
    if record != "missing":
        path = tmp_path / store.key
        payload = {
            "version": 2 if record == "incompatible" else 1,
            "minor_version": 1,
            "key": store.key,
            "data": {
                "entry_id": entry.entry_id,
                "sessions": {
                    "dev1": {
                        "started_utc": started.isoformat(),
                        "duration_minutes": 30,
                        "generation": 4,
                    }
                },
            },
        }

        def seed():
            path.write_text("{" if record == "corrupt" else json.dumps(payload))

        await hass.async_add_executor_job(seed)
        storage.get_internal_store_manager(hass).async_invalidate(store.key)
    bucket = await _setup(hass, entry)
    try:
        tracking = bucket["sleep_tracking"]["dev1"]
        coordinator = bucket["coordinator"]
        state = coordinator.data["dev1"]
        assert tracking.expiry_off_reports == 0
        assert not state["sleep_expiry_verified"]
        if record == "duration":
            assert tracking.duration_invalidated
            assert state["sleep_estimated_end"] is None
        elif record == "scene":
            assert state["sleep_estimated_end"] is None
        elif record == "partial":
            assert tracking.confidence == "restored"
            assert state["sleep_estimated_end"] is None
            assert sleep_cleanup_due(tracking) is None
            freezer.move_to(now + timedelta(minutes=10))
            device["power"] = "0"
            await coordinator.async_refresh()
            assert coordinator.data["dev1"]["sleep_remaining_estimated"] == 0
            assert sleep_cleanup_due(tracking) == now + timedelta(
                minutes=40, seconds=30
            )
        elif record != "expired":
            assert tracking.confidence == "startup_upper_bound"
            assert not tracking.start_known
        else:
            assert tracking.confidence == "restored"
            assert tracking.sleep_started_at_utc == started
            assert state["sleep_remaining_estimated"] == 0
            assert sleep_cleanup_due(tracking) == now + timedelta(
                minutes=30, seconds=30
            )
            for minute in (29, 30):
                freezer.move_to(now + timedelta(minutes=minute))
                await coordinator.async_refresh()
            assert tracking.expiry_off_reports == 0
            api.async_set_scenary.assert_not_awaited()
            # Publication is late, but the two observations were received early.
            due = sleep_cleanup_due(tracking)
            freezer.move_to(due + timedelta(seconds=1))
            for identity in (("early-push",), 100):
                integration._derive_state(
                    hass,
                    entry,
                    coordinator.data,
                    fresh_power_ids={"dev1"},
                    fresh_scenary_ids={"dev1"},
                    power_observations={"dev1": (identity, due - timedelta(seconds=1))},
                )
            assert tracking.expiry_off_reports == 0
            api.async_set_scenary.assert_not_awaited()
            for identity in (("fresh-push",), 101):
                integration._derive_state(
                    hass,
                    entry,
                    coordinator.data,
                    fresh_power_ids={"dev1"},
                    fresh_scenary_ids={"dev1"},
                    power_observations={"dev1": (identity, due + timedelta(seconds=1))},
                )
            assert tracking.expiry_verified
            coordinator.async_update_listeners()
            await hass.async_block_till_done()
            api.async_set_scenary.assert_awaited_once()
            assert api.async_set_scenary.await_args.args == ("dev1", "occupied")
            api.put_device_fields.assert_not_awaited()
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_sleep_saves_are_entry_scoped_and_retired_callback_is_inert(
    hass, dkn_config_entry_factory, backend
):
    first = dkn_config_entry_factory()
    second = dkn_config_entry_factory(entry_id="dkn-second", email="second@example.com")
    _, device = backend
    old = await _setup(hass, first)
    device.update(id="dev2", mac="00:00:00:00:00:02")
    other = await _setup(hass, second)
    try:
        integration.optimistic_set(hass, first.entry_id, "dev1", "scenary", "sleep")
        integration.optimistic_set(hass, second.entry_id, "dev2", "scenary", "sleep")
        await hass.config_entries.async_unload(first.entry_id)
        saved = await _store(hass, first).async_load()
        assert saved == {
            "entry_id": first.entry_id,
            "sessions": {
                "dev1": {
                    "started_utc": old["sleep_tracking"][
                        "dev1"
                    ].sleep_started_at_utc.isoformat(),
                    "duration_minutes": 30,
                    "generation": old["sleep_tracking"]["dev1"].session_revision,
                }
            },
        }
        device.update(id="dev1", mac="AA:BB:CC:DD:EE:FF", scenary="sleep")
        successor = await hass.config_entries.async_setup(first.entry_id)
        assert successor
        old["sleep_tracking"].clear()
        old["save_sleep_sessions"]()
        async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=2))
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(second.entry_id)
        other_saved = await _store(hass, second).async_load()
        assert other_saved["entry_id"] == second.entry_id
        assert set(other_saved["sessions"]) == {"dev2"}
        assert other["closing"]
        assert await _store(hass, first).async_load() == saved
    finally:
        for entry in (first, second):
            if entry.entry_id in hass.data.get(DOMAIN, {}):
                assert await hass.config_entries.async_unload(entry.entry_id)


async def test_deferred_write_cannot_follow_a_replaced_bucket(
    hass, dkn_config_entry_factory, backend
):
    entry = dkn_config_entry_factory()
    old = await _setup(hass, entry)
    try:
        await _select_sleep(hass)
        successor = {"coordinator": object()}
        hass.data[DOMAIN][entry.entry_id] = successor
        async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=2))
        await hass.async_block_till_done()
        assert hass.data[DOMAIN][entry.entry_id] is successor
        assert await _store(hass, entry).async_load() is None
    finally:
        hass.data[DOMAIN][entry.entry_id] = old
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_ha_shutdown_preserves_pending_sleep_snapshot(
    hass, dkn_config_entry_factory, backend
):
    entry = dkn_config_entry_factory()
    bucket = await _setup(hass, entry)
    previous_state = hass.state
    try:
        await _select_sleep(hass)
        store = _store(hass, entry)
        assert await store.async_load() is None
        hass.set_state(CoreState.stopping)
        hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        assert bucket["closing"]
        assert await store.async_load() is None

        hass.set_state(CoreState.final_write)
        hass.bus.async_fire_internal(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await hass.async_block_till_done()
        saved = await store.async_load()
        assert saved["sessions"]["dev1"]["duration_minutes"] == 30
    finally:
        hass.set_state(previous_state)
        assert await hass.config_entries.async_unload(entry.entry_id)
