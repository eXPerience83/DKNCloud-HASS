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

    async def async_set_scenary(self, device_id: str, scenary: str) -> None:
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
