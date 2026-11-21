"""Deterministic Sleep expiry contract: no live HVAC or cloud requests."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant

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
        power_evidence_ids={"device": evidence},
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
    assert not _observe(hass, entry, data, "0", "get:before-settle")
    assert not tracking.expiry_verified
    assert coordinator.api.async_set_scenary.await_count == 0
    assert coordinator.api.send_event.await_count == 0

    freezer.tick(timedelta(seconds=2))
    assert not _observe(hass, entry, data, "0", "get:after-settle-one")
    assert _observe(hass, entry, data, "0", "get:after-settle-two")
    assert tracking.expiry_verified

    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id=entry.entry_id,
        device_id="device",
        device=data["device"],
        coordinator=coordinator,
        reason="expiry",
        is_device_on=lambda: False,
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
    assert _observe(hass, entry, data, "0", "get:new-request")


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
    assert not _observe(hass, entry, data, "0", "get:early")
    freezer.tick(timedelta(minutes=1, seconds=31))
    assert not _observe(hass, entry, data, "0", "get:one")
    assert _observe(hass, entry, data, "0", "get:two")
    assert tracking.expiry_verified


async def test_changed_duration_disables_automatic_cleanup(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "1")
    data["device"]["sleep_time"] = 6
    _observe(hass, entry, data, "0", "get:duration-edit")
    assert tracking.duration_invalidated
    freezer.tick(timedelta(hours=1))
    assert not _observe(hass, entry, data, "0", "get:late-one")
    assert not _observe(hass, entry, data, "0", "get:late-two")
    assert data["device"]["sleep_estimated_end"] is None
    assert data["device"]["sleep_remaining_estimated"] is None


async def test_newer_power_intent_invalidates_prior_expiry_pair(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "0")
    freezer.tick(timedelta(minutes=5, seconds=31))
    assert not _observe(hass, entry, data, "0", "get:one")
    bucket = hass.data[DOMAIN][entry.entry_id]
    bucket.setdefault("power_intents", {})["device"] = 1
    assert not _observe(hass, entry, data, "0", "get:two")
    assert tracking.expiry_off_reports == 1
    assert not data["device"]["sleep_expiry_verified"]
    assert _observe(hass, entry, data, "0", "get:three")


async def test_on_after_expiry_preserves_scene_until_off_confirmed(hass, freezer):
    entry, _coordinator, data, tracking, _manager = _new_session(hass, "1")
    freezer.tick(timedelta(minutes=5, seconds=31))
    _observe(hass, entry, data, "1", "get:on-one")
    assert not _observe(hass, entry, data, "1", "get:on-two")
    assert not tracking.expiry_verified
    assert data["device"]["sleep_expiry_verified"] is False


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
