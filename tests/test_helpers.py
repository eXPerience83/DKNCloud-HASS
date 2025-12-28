"""Tests for helper utilities covering optimistic overlay behavior."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Stub minimal external modules to import the helper without full Home Assistant deps.
aiohttp_stub = types.ModuleType("aiohttp")


class _ClientResponseError(Exception):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args)


aiohttp_stub.ClientResponseError = _ClientResponseError
sys.modules.setdefault("aiohttp", aiohttp_stub)

ha_module = types.ModuleType("homeassistant")
helpers_module = types.ModuleType("homeassistant.helpers")
core_module = types.ModuleType("homeassistant.core")


class _HomeAssistant:
    loop: Any
    data: dict[str, Any]


core_module.HomeAssistant = _HomeAssistant

helpers_event_module = types.ModuleType("homeassistant.helpers.event")


def _async_call_later(*_args: Any, **_kwargs: Any) -> Callable[[], None]:
    raise NotImplementedError


helpers_event_module.async_call_later = _async_call_later
helpers_update_module = types.ModuleType("homeassistant.helpers.update_coordinator")


class _DataUpdateCoordinator:  # pragma: no cover - stub only
    pass


helpers_update_module.DataUpdateCoordinator = _DataUpdateCoordinator

ha_module.helpers = helpers_module
sys.modules.setdefault("homeassistant", ha_module)
sys.modules.setdefault("homeassistant.core", core_module)
helpers_module.event = helpers_event_module
helpers_module.update_coordinator = helpers_update_module
sys.modules.setdefault("homeassistant.helpers", helpers_module)
sys.modules.setdefault("homeassistant.helpers.event", helpers_event_module)
sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator", helpers_update_module
)

custom_components_module = types.ModuleType("custom_components")
custom_components_module.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components_module)

airzone_package = types.ModuleType("custom_components.airzoneclouddaikin")
airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]
sys.modules.setdefault("custom_components.airzoneclouddaikin", airzone_package)

helpers_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.helpers",
    ROOT / "custom_components" / "airzoneclouddaikin" / "helpers.py",
)
helpers_module = importlib.util.module_from_spec(helpers_spec)
assert helpers_spec is not None and helpers_spec.loader is not None
sys.modules[helpers_spec.name] = helpers_module
helpers_spec.loader.exec_module(helpers_module)
optimistic_get = helpers_module.optimistic_get
optimistic_set = helpers_module.optimistic_set
optimistic_invalidate = helpers_module.optimistic_invalidate
async_auto_exit_sleep_if_needed = helpers_module.async_auto_exit_sleep_if_needed
DOMAIN = helpers_module.DOMAIN


class _LoopStub:
    """Event loop stub exposing monotonic time control."""

    def __init__(self) -> None:
        self._time = 0.0

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class DummyHass:
    """Minimal Home Assistant stub for helper testing."""

    def __init__(self) -> None:
        self.loop = _LoopStub()
        self.data: dict[str, dict[str, dict[str, Any]]] = {}


@pytest.fixture
def hass_stub() -> DummyHass:
    return DummyHass()


def test_optimistic_overlay_value_before_expiration(hass_stub: DummyHass) -> None:
    """Overlay reads should return the optimistic value prior to expiring."""

    optimistic_set(hass_stub, "entry", "device", "temp", 23, ttl=5)

    result = optimistic_get(hass_stub, "entry", "device", "temp", backend_value=18)
    assert result == 23


def test_optimistic_overlay_expires_after_ttl(hass_stub: DummyHass) -> None:
    """Overlay should fall back to backend once the TTL has elapsed."""

    optimistic_set(hass_stub, "entry", "device", "temp", 22, ttl=2)

    hass_stub.loop.advance(2.5)
    result = optimistic_get(hass_stub, "entry", "device", "temp", backend_value=17)
    assert result == 17
    optimistic_bucket = hass_stub.data["airzoneclouddaikin"]["entry"].get(
        "optimistic", {}
    )
    assert "device" not in optimistic_bucket


def test_optimistic_overlay_invalidate_removes_value(hass_stub: DummyHass) -> None:
    """Explicit invalidation should clear overlays immediately."""

    optimistic_set(hass_stub, "entry", "device", "mode", "cool", ttl=5)
    optimistic_invalidate(hass_stub, "entry", "device", "mode")

    result = optimistic_get(hass_stub, "entry", "device", "mode", backend_value="auto")
    assert result == "auto"


def test_optimistic_overlay_malformed_expiration(hass_stub: DummyHass) -> None:
    """Malformed overlay metadata should fall back to the backend and self-heal."""

    optimistic_set(hass_stub, "entry", "device", "temp", 21, ttl=5)

    optimistic_bucket = hass_stub.data["airzoneclouddaikin"]["entry"].setdefault(
        "optimistic", {}
    )
    overlay = optimistic_bucket.setdefault("device", {}).setdefault("temp", {})
    overlay["expires"] = "bad"

    result = optimistic_get(hass_stub, "entry", "device", "temp", backend_value=19)
    assert result == 19

    optimistic_bucket = hass_stub.data["airzoneclouddaikin"]["entry"].get(
        "optimistic", {}
    )
    assert "device" not in optimistic_bucket


class DummyApi:
    """Stub API to capture scenary writes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def async_set_scenary(self, device_id: str, scenary: str) -> None:
        self.calls.append((device_id, scenary))


class DummyCoordinator:
    """Minimal coordinator stub with an API handle."""

    def __init__(self, api: DummyApi) -> None:
        self.api = api

    async def async_request_refresh(self) -> None:
        return None


def test_auto_exit_sleep_when_expired(
    hass_stub: DummyHass, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled: list[Callable[[], None]] = []

    def _schedule_stub(_hass: Any, _delay: float, _callback: Any) -> Callable[[], None]:
        def _cancel() -> None:
            return None

        scheduled.append(_cancel)
        return _cancel

    monkeypatch.setattr(helpers_module, "async_call_later", _schedule_stub)

    api = DummyApi()
    coordinator = DummyCoordinator(api)
    device_id = "device-1"
    entry_id = "entry-1"
    device = {"scenary": "sleep", "sleep_expired": True}

    asyncio.run(
        async_auto_exit_sleep_if_needed(
            hass_stub,
            entry_id=entry_id,
            device_id=device_id,
            device=device,
            coordinator=coordinator,
            reason="test",
            is_device_on=lambda: False,
            allow_away_handling=False,
        )
    )

    assert api.calls == [(device_id, "occupied")]
    optimistic_bucket = hass_stub.data[DOMAIN][entry_id]["optimistic"]
    overlay = optimistic_bucket[device_id]["scenary"]["value"]
    assert overlay == "occupied"
    assert callable(hass_stub.data[DOMAIN][entry_id]["pending_refresh"])
    assert scheduled


def test_auto_exit_sleep_skips_active_session(
    hass_stub: DummyHass, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled: list[Callable[[], None]] = []

    def _schedule_stub(_hass: Any, _delay: float, _callback: Any) -> Callable[[], None]:
        def _cancel() -> None:
            return None

        scheduled.append(_cancel)
        return _cancel

    monkeypatch.setattr(helpers_module, "async_call_later", _schedule_stub)

    api = DummyApi()
    coordinator = DummyCoordinator(api)
    device_id = "device-2"
    entry_id = "entry-2"
    device = {"scenary": "sleep", "sleep_expired": False}

    asyncio.run(
        async_auto_exit_sleep_if_needed(
            hass_stub,
            entry_id=entry_id,
            device_id=device_id,
            device=device,
            coordinator=coordinator,
            reason="test",
            is_device_on=lambda: True,
            allow_away_handling=False,
        )
    )

    assert api.calls == []
    optimistic_bucket = (
        hass_stub.data.get(DOMAIN, {}).get(entry_id, {}).get("optimistic", {})
    )
    assert device_id not in optimistic_bucket
    assert "pending_refresh" not in hass_stub.data.get(DOMAIN, {}).get(entry_id, {})
    assert not scheduled
