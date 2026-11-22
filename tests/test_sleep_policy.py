"""Deterministic Sleep expiry contract: no live HVAC or cloud requests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.airzoneclouddaikin import _derive_state
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.helpers import (
    async_auto_exit_sleep_if_needed,
    optimistic_set,
)


def _new_session(hass: HomeAssistant, initial_power: str):
    """Select Sleep from occupied without changing P1."""
    entry = SimpleNamespace(entry_id="sleep-spec", options={})
    api = SimpleNamespace(async_set_scenary=AsyncMock(), send_event=AsyncMock())
    coordinator = SimpleNamespace(data={}, api=api)
    manager = Mock()
    bucket = {
        "coordinator": coordinator,
        "sync_manager": manager,
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = bucket
    data = {
        "device": {
            "id": "device",
            "scenary": "occupied",
            "power": initial_power,
            "sleep_time": 5,
        }
    }
    _derive_state(
        hass,
        entry,
        data,
        fresh_power_ids={"device"},
        fresh_scenary_ids={"device"},
    )
    coordinator.data = data
    optimistic_set(hass, entry.entry_id, "device", "scenary", "sleep")
    data["device"]["scenary"] = "sleep"
    _derive_state(
        hass,
        entry,
        data,
        fresh_power_ids={"device"},
        fresh_scenary_ids={"device"},
    )
    tracking = bucket["sleep_tracking"]["device"]
    assert tracking.start_known
    assert tracking.expected_minutes == 5
    assert tracking.confidence == "local_ack"
    return entry, coordinator, data, tracking, manager


def _observe(hass, entry, data, power, evidence):
    data["device"]["power"] = power
    _derive_state(
        hass,
        entry,
        data,
        fresh_power_ids={"device"},
        fresh_scenary_ids={"device"},
        power_observations={"device": (evidence, dt_util.utcnow())},
    )
    return data["device"]["sleep_expiry_verified"]


@pytest.mark.parametrize(
    ("initial", "transitions"),
    [
        ("0", [(240, "1"), (311, "0")]),  # supervised A
        ("1", [(30, "0"), (240, "1"), (312, "0")]),  # revised B
        ("1", [(120, "0")]),  # C
        ("0", []),  # Sleep started OFF, never ON
    ],
)
async def test_original_timer_survives_intermediate_power_changes(
    hass, freezer, initial, transitions
):
    """Neither early OFF nor a later ON can trigger premature Home."""
    entry, coordinator, data, tracking, manager = _new_session(hass, initial)
    t = 0
    for seconds, power in transitions:
        freezer.tick(timedelta(seconds=seconds - t))
        t = seconds
        assert not _observe(hass, entry, data, power, f"push:{t}")
        assert not tracking.expiry_verified

    if t < 329:
        freezer.tick(timedelta(seconds=329 - t))
    assert not _observe(hass, entry, data, "0", 100)
    assert not tracking.expiry_verified
    assert coordinator.api.async_set_scenary.await_count == 0
    assert coordinator.api.send_event.await_count == 0

    freezer.tick(timedelta(seconds=2))
    assert not _observe(hass, entry, data, "0", 101)
    assert _observe(hass, entry, data, "0", 102)
    assert tracking.expiry_verified

    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id=entry.entry_id,
        device_id="device",
        device=data["device"],
        coordinator=coordinator,
        reason="expiry",
        allow_away_handling=False,
    )
    coordinator.api.async_set_scenary.assert_awaited_once()
    args, kwargs = coordinator.api.async_set_scenary.await_args
    assert args == ("device", "occupied")
    assert kwargs["retry"] is False
    assert callable(kwargs["on_start"])
    coordinator.api.send_event.assert_not_awaited()
    assert (
        hass.data[DOMAIN][entry.entry_id]["sleep_exit_pending"]["device"] == "accepted"
    )


async def test_duplicate_push_and_cached_state_never_confirm_expiry(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "0")
    freezer.tick(timedelta(minutes=5, seconds=31))

    assert not _observe(hass, entry, data, "0", "push:same")
    assert not _observe(hass, entry, data, "0", "push:same")
    _derive_state(hass, entry, data, fresh_power_ids=set(), fresh_scenary_ids=set())
    assert tracking.expiry_off_reports == 1
    assert not data["device"]["sleep_expiry_verified"]
    assert _observe(hass, entry, data, "0", 103)


async def test_unknown_start_uses_fresh_observation_upper_bound(hass, freezer):
    entry = SimpleNamespace(entry_id="unknown-sleep", options={})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"sync_manager": Mock()}
    data = {
        "device": {
            "id": "device",
            "scenary": "sleep",
            "power": "0",
            "sleep_time": 5,
        }
    }
    _derive_state(
        hass,
        entry,
        data,
        fresh_power_ids={"device"},
        fresh_scenary_ids={"device"},
    )
    tracking = hass.data[DOMAIN][entry.entry_id]["sleep_tracking"]["device"]
    assert not tracking.start_known
    assert tracking.confidence == "startup_upper_bound"

    freezer.tick(timedelta(minutes=4))
    assert not _observe(hass, entry, data, "0", 104)
    freezer.tick(timedelta(minutes=1, seconds=31))
    assert not _observe(hass, entry, data, "0", 105)
    assert _observe(hass, entry, data, "0", 106)
    assert tracking.expiry_verified


async def test_changed_duration_disables_automatic_cleanup(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "1")
    data["device"]["sleep_time"] = 6
    _observe(hass, entry, data, "0", 107)
    assert tracking.duration_invalidated
    freezer.tick(timedelta(hours=1))
    assert not _observe(hass, entry, data, "0", 108)
    assert not _observe(hass, entry, data, "0", 109)
    assert data["device"]["sleep_estimated_end"] is None
    assert data["device"]["sleep_remaining_estimated"] is None


async def test_newer_power_intent_invalidates_prior_expiry_pair(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "0")
    freezer.tick(timedelta(minutes=5, seconds=31))
    assert not _observe(hass, entry, data, "0", 110)
    bucket = hass.data[DOMAIN][entry.entry_id]
    bucket.setdefault("power_intents", {})["device"] = 1
    assert not _observe(hass, entry, data, "0", 111)
    assert tracking.expiry_off_reports == 1
    assert not data["device"]["sleep_expiry_verified"]
    assert _observe(hass, entry, data, "0", 112)


async def test_on_after_expiry_stays_read_only_until_off_confirmed(hass, freezer):
    entry, coordinator, data, tracking, _ = _new_session(hass, "1")
    freezer.tick(timedelta(seconds=331))
    _observe(hass, entry, data, "1", 100)
    assert not _observe(hass, entry, data, "1", 101)
    assert not data["device"]["sleep_overdue"]
    freezer.tick(timedelta(seconds=270))
    _observe(hass, entry, data, "1", 102)
    assert not data["device"]["sleep_overdue"]
    assert not _observe(hass, entry, data, "1", 103)
    assert data["device"]["sleep_overdue"]
    assert not tracking.expiry_verified
    assert not await async_auto_exit_sleep_if_needed(
        hass,
        entry_id=entry.entry_id,
        device_id="device",
        device=data["device"],
        coordinator=coordinator,
        reason="expiry",
        allow_away_handling=False,
    )
    coordinator.api.async_set_scenary.assert_not_awaited()
    coordinator.api.send_event.assert_not_awaited()


async def test_sensor_estimates_are_local_not_physical_confirmation(
    hass, dummy_coordinator_factory
):
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import UnitOfTime

    from custom_components.airzoneclouddaikin.sensor import AirzoneSensor

    coordinator = dummy_coordinator_factory(
        {
            "device": {
                "id": "device",
                "scenary": "sleep",
                "power": "1",
                "sleep_remaining_estimated": 0,
                "sleep_estimated_end": "2026-11-21T19:00:00+00:00",
                "sleep_estimate_source": "startup_upper_bound",
            }
        }
    )
    remaining = AirzoneSensor(
        coordinator,
        "device",
        "sleep_remaining_estimated",
        "Sleep Time Remaining (Estimated)",
        "mdi:timer-sand",
        True,
        None,
        "measurement",
    )
    end = AirzoneSensor(
        coordinator,
        "device",
        "sleep_estimated_end",
        "Sleep Expected End",
        "mdi:clock-end",
        True,
        "timestamp",
        None,
    )
    assert remaining.native_value == 0
    assert remaining.native_unit_of_measurement == UnitOfTime.MINUTES
    assert remaining.extra_state_attributes == {
        "estimate_source": "startup_upper_bound"
    }
    assert end.device_class == SensorDeviceClass.TIMESTAMP
    assert end.native_value.isoformat().startswith("2026-11-21T")
    assert end.extra_state_attributes == remaining.extra_state_attributes


@pytest.mark.parametrize("interruption", ["deadline", "get", "on", "get_replay"])
async def test_sleep_off_observation_cannot_vote_twice(hass, freezer, interruption):
    entry, _, data, tracking, _ = _new_session(hass, "0")
    old = 1000 if interruption == "get_replay" else "push:old"
    if interruption not in {"deadline", "get"}:
        freezer.tick(timedelta(seconds=331))
    assert not _observe(hass, entry, data, "0", old)
    if interruption in {"deadline", "get"}:
        freezer.tick(timedelta(seconds=331))
        if interruption == "get":
            assert not _observe(hass, entry, data, "0", 1001)
    else:
        if interruption == "get_replay":
            assert _observe(hass, entry, data, "0", 1001)
        assert not _observe(hass, entry, data, "1", "push:on")

    assert not _observe(hass, entry, data, "0", old)
    assert tracking.expiry_off_reports == (interruption == "get")
    if interruption != "get":
        assert not _observe(hass, entry, data, "0", 1002)
    assert _observe(hass, entry, data, "0", 1003)


@pytest.mark.parametrize(
    "sources", [("push:a", "push:b"), (1000, 1001), ("push:a", 1000)]
)
async def test_independent_off_pair_authorizes_cleanup(hass, freezer, sources):
    entry, _, data, _, _ = _new_session(hass, "0")
    freezer.tick(timedelta(seconds=331))
    assert not _observe(hass, entry, data, "0", sources[0])
    assert not _observe(hass, entry, data, "0", sources[0])
    assert _observe(hass, entry, data, "0", sources[1])


@pytest.mark.parametrize(
    "invalidation", ["deadline", "power_intents", "scene_preset_intents"]
)
async def test_get_keeps_its_receipt_context_across_slow_installation(
    hass, freezer, invalidation
):
    import asyncio

    from custom_components.airzoneclouddaikin import _async_update_data

    entry, coordinator, data, tracking, _ = _new_session(hass, "0")
    received_fast = asyncio.Event()
    release_slow = asyncio.Event()

    async def fetch(installation, *, on_start):
        on_start()
        if installation == "fast":
            received_fast.set()
            return [dict(data["device"])]
        await release_slow.wait()
        return []

    api = SimpleNamespace(
        fetch_installations=AsyncMock(return_value=[{"id": "fast"}, {"id": "slow"}]),
        fetch_devices=fetch,
    )
    freezer.tick(timedelta(seconds=329 if invalidation == "deadline" else 331))
    refresh = asyncio.create_task(_async_update_data(hass, entry, api))
    try:
        async with asyncio.timeout(3):
            await received_fast.wait()
            if invalidation == "deadline":
                freezer.tick(timedelta(seconds=2))
            else:
                bucket = hass.data[DOMAIN][entry.entry_id]
                bucket.setdefault(invalidation, {})["device"] = 1
            release_slow.set()
            coordinator.data = await refresh
    finally:
        release_slow.set()
        await asyncio.gather(refresh, return_exceptions=True)

    assert tracking.expiry_off_reports == 0
    assert not _observe(hass, entry, coordinator.data, "0", 1000)
    assert _observe(hass, entry, coordinator.data, "0", 1001)


@pytest.mark.parametrize("interruption", ["power_on", "connection_timeout"])
async def test_sleep_cleanup_writer_rechecks_live_off(
    hass, freezer, monkeypatch, aiohttp_server, socket_enabled, interruption
):
    import asyncio

    from aiohttp import ConnectionTimeoutError, web

    from custom_components.airzoneclouddaikin import (
        airzone_api,
        async_create_runtime_session,
    )

    entry, coordinator, data, _, manager = _new_session(hass, "0")
    freezer.tick(timedelta(seconds=331))
    assert not _observe(hass, entry, data, "0", "push:off1")
    assert _observe(hass, entry, data, "0", "push:off2")
    connecting = asyncio.Event()
    release = asyncio.Event()
    received = []

    async def handler(request):
        received.append(request.method)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_put("/devices/device", handler)
    server = await aiohttp_server(app, access_log=None)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))
    session = async_create_runtime_session(hass)
    connect = session.connector.connect

    async def blocked_connect(*args, **kwargs):
        connecting.set()
        await release.wait()
        if interruption == "connection_timeout":
            raise ConnectionTimeoutError("synthetic connection timeout")
        return await connect(*args, **kwargs)

    monkeypatch.setattr(session.connector, "connect", blocked_connect)
    coordinator.api = airzone_api.AirzoneAPI("synthetic@example.invalid", session)
    manager.request_recovery.reset_mock()

    async def cleanup():
        return await async_auto_exit_sleep_if_needed(
            hass,
            entry_id=entry.entry_id,
            device_id="device",
            device=data["device"],
            coordinator=coordinator,
            reason="expiry",
            allow_away_handling=False,
        )

    task = asyncio.create_task(cleanup())
    try:
        async with asyncio.timeout(3):
            await connecting.wait()
            if interruption == "power_on":
                assert not _observe(hass, entry, data, "1", "push:on")
            release.set()
            attempted = await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert received == []
    assert attempted is False
    manager.request_recovery.assert_not_called()
    monkeypatch.setattr(session.connector, "connect", connect)
    if interruption == "power_on":
        assert not _observe(hass, entry, data, "0", "push:off3")
        assert _observe(hass, entry, data, "0", "push:off4")
    assert await cleanup() is True
    assert received == ["PUT"]


async def test_saturated_push_history_keeps_votes_and_can_use_get(
    hass, freezer, monkeypatch
):
    import custom_components.airzoneclouddaikin as integration

    monkeypatch.setattr(integration, "_MAX_SLEEP_PUSH_IDENTITIES", 2)
    entry, _, data, tracking, _ = _new_session(hass, "0")
    assert not _observe(hass, entry, data, "0", "push:early")
    freezer.tick(timedelta(seconds=331))
    assert not _observe(hass, entry, data, "0", "push:eligible")
    assert not _observe(hass, entry, data, "0", "push:overflow")
    assert tracking.expiry_off_reports == 1
    assert _observe(hass, entry, data, "0", 1000)

    optimistic_set(hass, entry.entry_id, "device", "scenary", "sleep")
    freezer.tick(timedelta(seconds=331))
    assert not _observe(hass, entry, data, "0", "push:early")
    assert _observe(hass, entry, data, "0", "push:eligible")


async def test_accepted_duration_and_new_sleep_ignore_cached_minutes(hass):
    """A maintenance publication must not contradict the accepted new duration."""
    entry, coordinator, data, tracking, _ = _new_session(hass, "0")
    optimistic_set(hass, entry.entry_id, "device", "sleep_time", 10)
    optimistic_set(hass, entry.entry_id, "device", "scenary", "sleep")
    assert tracking.expected_minutes == 10
    _derive_state(hass, entry, data, fresh_power_ids=set(), fresh_scenary_ids=set())
    assert not tracking.duration_invalidated
    assert tracking.start_known
