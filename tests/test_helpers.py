"""Tests for helper utilities covering optimistic overlay behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.airzoneclouddaikin import helpers
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.helpers import (
    async_auto_exit_sleep_if_needed,
    build_device_info,
    optimistic_get,
    optimistic_invalidate,
    optimistic_set,
    parse_float,
)


def test_optimistic_overlay_value_before_expiration(hass: HomeAssistant) -> None:
    """Overlay reads should return the optimistic value prior to expiring."""
    optimistic_set(hass, "entry", "device", "temp", 23, ttl=5)

    result = optimistic_get(hass, "entry", "device", "temp", backend_value=18)

    assert result == 23


def test_optimistic_overlay_expires_after_ttl(hass: HomeAssistant) -> None:
    """Overlay should fall back to backend once the TTL has elapsed."""
    optimistic_set(hass, "entry", "device", "temp", 22, ttl=0)

    result = optimistic_get(hass, "entry", "device", "temp", backend_value=17)

    assert result == 17
    optimistic_bucket = hass.data["airzoneclouddaikin"]["entry"].get("optimistic", {})
    assert "device" not in optimistic_bucket


def test_optimistic_overlay_invalidate_removes_value(hass: HomeAssistant) -> None:
    """Explicit invalidation should clear overlays immediately."""
    optimistic_set(hass, "entry", "device", "mode", "cool", ttl=5)
    optimistic_invalidate(hass, "entry", "device", "mode")

    result = optimistic_get(hass, "entry", "device", "mode", backend_value="auto")

    assert result == "auto"


def test_optimistic_overlay_malformed_expiration(hass: HomeAssistant) -> None:
    """Malformed overlay metadata should fall back to the backend and self-heal."""
    optimistic_set(hass, "entry", "device", "temp", 21, ttl=5)

    optimistic_bucket = hass.data["airzoneclouddaikin"]["entry"].setdefault(
        "optimistic", {}
    )
    overlay = optimistic_bucket.setdefault("device", {}).setdefault("temp", {})
    overlay["expires"] = "bad"

    result = optimistic_get(hass, "entry", "device", "temp", backend_value=19)

    assert result == 19
    optimistic_bucket = hass.data["airzoneclouddaikin"]["entry"].get("optimistic", {})
    assert "device" not in optimistic_bucket


class DummyApi:
    """Stub API to capture scenary writes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def async_set_scenary(
        self, device_id: str, scenary: str, *, retry=True
    ) -> None:
        """Record a scenary write."""
        self.calls.append((device_id, scenary))


class DummyCoordinator:
    """Minimal coordinator stub with an API handle."""

    def __init__(self, api: DummyApi) -> None:
        self.api = api
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        """Record refresh requests."""
        self.refreshes += 1


@pytest.mark.asyncio
async def test_auto_exit_sleep_when_expired(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired sleep scenary should auto-exit to occupied and schedule refresh."""
    scheduled: list[Callable[[], None]] = []

    def _schedule_stub(_hass: Any, _delay: float, _callback: Any) -> Callable[[], None]:
        def _cancel() -> None:
            return None

        scheduled.append(_cancel)
        return _cancel

    monkeypatch.setattr(helpers, "async_call_later", _schedule_stub)

    api = DummyApi()
    coordinator = DummyCoordinator(api)
    device_id = "device-1"
    entry_id = "entry-1"
    device = {"scenary": "sleep", "sleep_expired": True}

    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id=entry_id,
        device_id=device_id,
        device=device,
        coordinator=coordinator,
        reason="test",
        is_device_on=lambda: False,
        allow_away_handling=False,
    )

    assert api.calls == [(device_id, "occupied")]
    optimistic_bucket = hass.data[DOMAIN][entry_id]["optimistic"]
    overlay = optimistic_bucket[device_id]["scenary"]["value"]
    assert overlay == "occupied"
    assert callable(hass.data[DOMAIN][entry_id]["pending_refresh"])
    assert scheduled


@pytest.mark.asyncio
async def test_auto_exit_sleep_skips_active_session(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active sleep sessions should not auto-exit."""
    scheduled: list[Callable[[], None]] = []

    def _schedule_stub(_hass: Any, _delay: float, _callback: Any) -> Callable[[], None]:
        def _cancel() -> None:
            return None

        scheduled.append(_cancel)
        return _cancel

    monkeypatch.setattr(helpers, "async_call_later", _schedule_stub)

    api = DummyApi()
    coordinator = DummyCoordinator(api)
    device_id = "device-2"
    entry_id = "entry-2"
    device = {"scenary": "sleep", "sleep_expired": False}

    await async_auto_exit_sleep_if_needed(
        hass,
        entry_id=entry_id,
        device_id=device_id,
        device=device,
        coordinator=coordinator,
        reason="test",
        is_device_on=lambda: True,
        allow_away_handling=False,
    )

    assert api.calls == []
    optimistic_bucket = (
        hass.data.get(DOMAIN, {}).get(entry_id, {}).get("optimistic", {})
    )
    assert device_id not in optimistic_bucket
    assert "pending_refresh" not in hass.data.get(DOMAIN, {}).get(entry_id, {})
    assert not scheduled


def test_build_device_info_uses_snapshot_id_when_present() -> None:
    """Identifiers should use device["id"] when available, not the coordinator key."""
    device = {"id": "snap-1", "name": "Room", "mac": "AA:BB:CC:DD:EE:01"}
    info = build_device_info(device, "coordinator-key-mac")

    identifiers = info.get("identifiers")
    assert identifiers is not None
    assert ("airzoneclouddaikin", "snap-1") in identifiers
    assert ("airzoneclouddaikin", "coordinator-key-mac") not in identifiers


def test_build_device_info_falls_back_to_device_id() -> None:
    """When device["id"] is missing, identifiers must fall back to the coordinator key."""
    device: dict[str, str] = {"name": "Room", "mac": "AA:BB:CC:DD:EE:02"}
    info = build_device_info(device, "fallback-key")

    identifiers = info.get("identifiers")
    assert identifiers is not None
    assert ("airzoneclouddaikin", "fallback-key") in identifiers


@pytest.mark.parametrize(
    "value,precision,expected",
    [
        (None, None, None),
        ("23.5", None, 23.5),
        ("23,5", None, 23.5),
        ("abc", None, None),
        (23, None, 23.0),
        (23.5, None, 23.5),
        ("23.456", 1, 23.5),
        ("23.4567894", 6, 23.456789),
    ],
)
def test_parse_float(value: Any, precision: int | None, expected: float | None) -> None:
    """parse_float should handle None, strings, comma decimals, and optional rounding."""
    result = parse_float(value, precision=precision)
    assert result == expected


def test_parse_float_no_precision_preserves_full_float() -> None:
    """Without precision, the full parsed float is returned unchanged."""
    assert parse_float("23.456789") == 23.456789


def test_parse_float_unparseable_collections_return_none() -> None:
    """Values that cannot be converted to float must return None."""
    assert parse_float([]) is None
    assert parse_float({}) is None


def test_sync_overlays_confirm_only_explicit_received_fields(hass):
    from custom_components.airzoneclouddaikin.const import DOMAIN
    from custom_components.airzoneclouddaikin.helpers import confirm_optimistic

    hass.data.setdefault(DOMAIN, {})["synthetic-entry"] = {"sync_manager": object()}
    optimistic_set(hass, "synthetic-entry", "fake", "power", "1")
    optimistic_set(hass, "synthetic-entry", "fake", "mode", "2")
    data = {"fake": {"power": "1", "mode": "2"}}
    assert confirm_optimistic(hass, "synthetic-entry", data, {"fake": {"power"}})
    assert optimistic_get(hass, "synthetic-entry", "fake", "power", "0") == "0"
    assert optimistic_get(hass, "synthetic-entry", "fake", "mode", "0") == "2"


async def test_entity_expiry_requests_recovery_before_local_maintenance(hass):
    from unittest.mock import Mock

    from custom_components.airzoneclouddaikin.const import DOMAIN
    from custom_components.airzoneclouddaikin.helpers import (
        expire_optimistic,
        optimistic_get,
        optimistic_set,
    )

    manager = Mock()
    hass.data.setdefault(DOMAIN, {})["expiry-test"] = {"sync_manager": manager}
    optimistic_set(hass, "expiry-test", "fake", "power", "1", ttl=-1)
    assert optimistic_get(hass, "expiry-test", "fake", "power", "0") == "0"
    expire_optimistic(hass, "expiry-test")
    manager.request_recovery.assert_called_once_with("fake")


@pytest.mark.parametrize("manual", [False, True])
async def test_sleep_exit_serializes_pending_and_manual_scene(hass, manual):
    """An old decision cannot overwrite a later command or repeat an unknown write."""
    import asyncio
    from unittest.mock import AsyncMock

    device = {"scenary": "sleep", "sleep_expired": True}
    api = DummyApi()
    api.async_set_scenary = AsyncMock(side_effect=TimeoutError)
    coordinator = DummyCoordinator(api)
    coordinator.data = {"device": device}
    lock = helpers.acquire_device_lock(hass, "entry", "device")
    await lock.acquire()

    async def exit_sleep():
        await async_auto_exit_sleep_if_needed(
            hass,
            entry_id="entry",
            device_id="device",
            device=device,
            coordinator=coordinator,
            reason="test",
            is_device_on=lambda: False,
            allow_away_handling=False,
        )

    task = asyncio.create_task(exit_sleep())
    await asyncio.sleep(0)
    if manual:
        helpers.optimistic_set(hass, "entry", "device", "scenary", "vacant")
    lock.release()
    await task
    if manual:
        api.async_set_scenary.assert_not_awaited()
    else:
        # Old GET scene plus unrelated partial push must not replay HOME.
        device["local_temp"] = "23"
        await exit_sleep()
        api.async_set_scenary.assert_awaited_once_with(
            "device", "occupied", retry=False
        )
        assert (
            hass.data[DOMAIN]["entry"]["sleep_exit_pending"]["device"] == "unverified"
        )
    if cancel := hass.data[DOMAIN]["entry"].get("pending_refresh"):
        cancel()


async def test_new_observed_sleep_session_rearms_after_convergence(hass):
    """Only a newly observed Home scene retires an ambiguous session marker."""
    from types import SimpleNamespace

    from custom_components.airzoneclouddaikin import _derive_state

    entry = SimpleNamespace(entry_id="entry", options={})
    hass.data.setdefault(DOMAIN, {})["entry"] = {
        "sleep_exit_pending": {"device": "unverified"}
    }
    _derive_state(hass, entry, {"device": {"scenary": "sleep", "power": "0"}})
    assert "device" in hass.data[DOMAIN]["entry"]["sleep_exit_pending"]
    _derive_state(
        hass,
        entry,
        {"device": {"scenary": "occupied"}},
        fresh_scenary_ids=set(),
    )
    # Retained Home (e.g. a partial power-only push) cannot unlock ambiguity.
    assert "device" in hass.data[DOMAIN]["entry"]["sleep_exit_pending"]
    _derive_state(
        hass,
        entry,
        {"device": {"scenary": "occupied"}},
        fresh_scenary_ids={"device"},
    )
    assert not hass.data[DOMAIN]["entry"]["sleep_exit_pending"]
    _derive_state(hass, entry, {"device": {"scenary": "sleep"}})
    tracking = hass.data[DOMAIN]["entry"]["sleep_tracking"]["device"]
    assert tracking.sleep_started_at_utc is not None
    assert not tracking.force_exit_requested


async def test_local_sleep_survives_retained_scene_on_maintenance_and_partial_push(
    hass,
):
    """Only an explicitly received scenary may replace a new local session."""
    from types import SimpleNamespace

    from custom_components.airzoneclouddaikin import _derive_state

    entry = SimpleNamespace(entry_id="entry", options={})
    hass.data.setdefault(DOMAIN, {})["entry"] = {}
    occupied = {"device": {"scenary": "occupied", "power": "1", "sleep_time": 30}}

    _derive_state(
        hass,
        entry,
        occupied,
        fresh_scenary_ids={"device"},
        fresh_power_ids={"device"},
    )
    optimistic_set(hass, "entry", "device", "scenary", "sleep")
    tracking = hass.data[DOMAIN]["entry"]["sleep_tracking"]["device"]
    started = tracking.sleep_started_at_utc
    revision = tracking.session_revision
    assert started is not None

    # Simulate SyncManager.maintenance() retaining the prior REST scene.
    _derive_state(
        hass,
        entry,
        occupied,
        fresh_scenary_ids=set(),
        fresh_power_ids=set(),
    )
    assert tracking.sleep_started_at_utc == started
    assert tracking.session_revision == revision
    assert tracking.last_scenary == "sleep"

    # A genuine power-only patch is no scene confirmation either.
    _derive_state(
        hass,
        entry,
        {"device": {"scenary": "occupied", "power": "0", "sleep_time": 30}},
        fresh_scenary_ids=set(),
        fresh_power_ids={"device"},
    )
    assert tracking.sleep_started_at_utc == started
    assert tracking.last_scenary == "sleep"

    _derive_state(
        hass,
        entry,
        {"device": {"scenary": "sleep", "power": "1", "sleep_time": 30}},
        fresh_scenary_ids={"device"},
        fresh_power_ids={"device"},
    )
    assert tracking.sleep_started_at_utc == started
    assert tracking.session_revision == revision

    # A newly received occupied scene, unlike the retained one, ends tracking.
    _derive_state(
        hass,
        entry,
        occupied,
        fresh_scenary_ids={"device"},
        fresh_power_ids={"device"},
    )
    assert tracking.sleep_started_at_utc is None
    assert tracking.last_scenary == "occupied"


async def test_explicit_new_sleep_rearms_local_tracking(hass):
    from datetime import UTC, datetime

    from custom_components.airzoneclouddaikin import SleepTracking

    tracking = SleepTracking(
        last_scenary="sleep",
        sleep_started_at_utc=datetime(2020, 1, 1, tzinfo=UTC),
        force_exit_requested=True,
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = {
        "sleep_tracking": {"device": tracking},
        "sleep_exit_pending": {"device": "unverified"},
    }
    helpers.optimistic_set(hass, "entry", "device", "scenary", "sleep")
    assert not tracking.force_exit_requested
    assert tracking.sleep_started_at_utc.year > 2020
    assert not hass.data[DOMAIN]["entry"]["sleep_exit_pending"]


async def test_sleep_rejection_is_distinct_and_not_replayed(hass):
    from unittest.mock import AsyncMock

    from aiohttp import ClientResponseError

    api = DummyApi()
    api.async_set_scenary = AsyncMock(
        side_effect=ClientResponseError(None, (), status=422)
    )
    coordinator = DummyCoordinator(api)
    for _ in range(2):
        await async_auto_exit_sleep_if_needed(
            hass,
            entry_id="entry",
            device_id="device",
            device={"scenary": "sleep", "sleep_expired": True},
            coordinator=coordinator,
            reason="test",
            is_device_on=lambda: False,
            allow_away_handling=False,
        )
    assert hass.data[DOMAIN]["entry"]["sleep_exit_pending"]["device"] == "rejected"
    api.async_set_scenary.assert_awaited_once()
    if cancel := hass.data[DOMAIN]["entry"].get("pending_refresh"):
        cancel()
