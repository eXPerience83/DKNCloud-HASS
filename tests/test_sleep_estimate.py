"""Focused tests for optional Sleep estimate storage."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import sleep_estimate
from custom_components.airzoneclouddaikin.const import DOMAIN


@pytest.mark.parametrize(
    "record",
    [
        None,
        {"start": "invalid", "minutes": 30},
        {"start": "2026-01-01T00:00:00", "minutes": 30},
        {"start": "2026-01-01T00:10:00+00:00", "minutes": 30},
        {"start": "2025-12-31T23:00:00+00:00", "minutes": 30},
        {"start": "2025-12-31T23:55:00+00:00", "minutes": True},
        {"start": "2025-12-31T23:55:00+00:00", "minutes": 0},
        {"start": "2025-12-31T23:55:00+00:00", "minutes": 10**20},
    ],
)
async def test_invalid_estimates_are_discarded(hass, monkeypatch, record):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    key = sleep_estimate._key("dev1")
    store = Mock(async_load=AsyncMock(return_value={key: record}))
    monkeypatch.setattr(sleep_estimate, "_store", Mock(return_value=store))
    estimates = sleep_estimate.SleepEstimateStore(hass, "entry")
    await estimates.async_load(now)
    assert (
        estimates.estimate(
            "dev1", now, 30, current_session_known=False, valid_scene=True
        )
        is None
    )
    store.async_delay_save.assert_called_once()
    assert store.async_delay_save.call_args.args[0]() == {}


async def test_store_records_only_significant_changes(hass, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = Mock(
        async_load=AsyncMock(return_value=None),
        async_save=AsyncMock(),
        async_remove=AsyncMock(),
    )
    monkeypatch.setattr(sleep_estimate, "_store", Mock(return_value=store))
    estimates = sleep_estimate.SleepEstimateStore(hass, "entry")
    await estimates.async_load(now)
    estimates.remember("dev1", now, 30)
    estimates.remember("dev1", now, 30)
    for minute in range(5):
        assert (
            estimates.estimate(
                "dev1",
                now + timedelta(minutes=minute),
                30,
                current_session_known=True,
                valid_scene=True,
            )
            is None
        )
    store.async_delay_save.assert_called_once()
    await estimates.async_flush()
    saved = store.async_save.call_args.args[0]
    assert saved == {
        sleep_estimate._key("dev1"): {"start": now.isoformat(), "minutes": 30}
    }
    assert "dev1" not in saved

    estimates.discard("dev1")
    estimates.discard("dev1")
    assert store.async_delay_save.call_count == 2
    await estimates.async_flush()
    store.async_save.assert_awaited_with({})
    await sleep_estimate.async_remove_estimates(hass, "entry")
    store.async_remove.assert_awaited_once()


async def test_storage_io_failure_does_not_prevent_estimation(hass, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = Mock(
        async_load=AsyncMock(side_effect=OSError),
        async_save=AsyncMock(side_effect=OSError),
        async_remove=AsyncMock(side_effect=OSError),
    )
    monkeypatch.setattr(sleep_estimate, "_store", Mock(return_value=store))
    estimates = sleep_estimate.SleepEstimateStore(hass, "entry")
    await estimates.async_load(now)
    estimates.remember("dev1", now, 30)
    await estimates.async_flush()
    await sleep_estimate.async_remove_estimates(hass, "entry")
    store.async_save.assert_awaited_once()


async def test_restored_estimate_is_entry_and_device_scoped(hass):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = sleep_estimate.SleepEstimateStore(hass, "entry-one")
    first.remember("dev1", now, 30)
    await first.async_flush()
    second = sleep_estimate.SleepEstimateStore(hass, "entry-two")
    await second.async_load(now)
    assert (
        second.estimate("dev1", now, 30, current_session_known=False, valid_scene=True)
        is None
    )
    reloaded = sleep_estimate.SleepEstimateStore(hass, "entry-one")
    await reloaded.async_load(now)
    assert (
        reloaded.estimate(
            "dev2", now, 30, current_session_known=False, valid_scene=True
        )
        is None
    )
    assert reloaded.estimate(
        "dev1", now, 30, current_session_known=False, valid_scene=True
    ) == now + timedelta(minutes=30)
    reloaded.retain_devices({"dev2"})
    await reloaded.async_flush()
    await sleep_estimate.async_remove_estimates(hass, "entry-one")
    await sleep_estimate.async_remove_estimates(hass, "entry-two")


async def test_remove_after_failed_unload_cancels_live_store(
    hass, monkeypatch, dkn_config_entry_factory
):
    """Removal must cancel the live Store's pending save, even on failed unload."""
    entry = dkn_config_entry_factory()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    estimates = sleep_estimate.SleepEstimateStore(hass, entry.entry_id)
    hass.data[DOMAIN] = {
        entry.entry_id: {"sleep_estimates": estimates, "sync_manager": Mock()}
    }
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
    )
    estimates.remember("dev1", now, 30)
    assert estimates._store._delay_handle is not None
    assert not await integration.async_unload_entry(hass, entry)
    await integration.async_remove_entry(hass, entry)
    assert estimates._store._delay_handle is None

    # A callback from the failed-unload runtime must not recreate the file.
    estimates.remember("dev1", now + timedelta(minutes=1), 30)
    await estimates.async_flush()
    assert estimates._store._delay_handle is None
    assert await sleep_estimate._store(hass, entry.entry_id).async_load() is None


async def test_remove_waits_for_live_store_write(hass, monkeypatch):
    """The original Store lock serializes deletion after a dispatched save."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    estimates = sleep_estimate.SleepEstimateStore(hass, "entry")
    started, release = asyncio.Event(), asyncio.Event()
    original_write = estimates._store._io.async_write

    async def blocked_write(*args):
        started.set()
        await release.wait()
        await original_write(*args)

    monkeypatch.setattr(estimates._store._io, "async_write", blocked_write)
    estimates.remember("dev1", now, 30)
    save = asyncio.create_task(estimates.async_flush())
    remove = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        remove = asyncio.create_task(estimates.async_remove())
        await asyncio.sleep(0)
        assert not remove.done()
        estimates.remember("dev2", now, 30)
        assert estimates._store._delay_handle is None
    finally:
        release.set()
        await asyncio.wait_for(save, 2)
        if remove is not None:
            await asyncio.wait_for(remove, 2)
    assert await sleep_estimate._store(hass, "entry").async_load() is None
