"""Tests for Airzone power switch proxy vs fallback behavior."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Stub Home Assistant modules used by switch.py ---

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)


class HomeAssistant:  # pragma: no cover - type stub only
    """Minimal HomeAssistant stub for type hints."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.services: Any = types.SimpleNamespace()


core_module.HomeAssistant = HomeAssistant

# Exceptions module (needed before importing switch)
exceptions_module = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)

config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)

if not hasattr(exceptions_module, "HomeAssistantError"):

    class HomeAssistantError(Exception):
        """Base Home Assistant error stub."""

    exceptions_module.HomeAssistantError = HomeAssistantError

if not hasattr(exceptions_module, "ServiceNotFound"):

    class ServiceNotFound(exceptions_module.HomeAssistantError):  # type: ignore[misc]
        """Raised when a service cannot be found."""

    exceptions_module.ServiceNotFound = ServiceNotFound

if not hasattr(config_entries_module, "ConfigEntry"):

    class ConfigEntry:
        """Minimal ConfigEntry stub."""

        entry_id = "entry"

    config_entries_module.ConfigEntry = ConfigEntry

if not hasattr(config_entries_module, "SOURCE_REAUTH"):
    config_entries_module.SOURCE_REAUTH = "reauth"

# components.switch
switch_component = sys.modules.setdefault(
    "homeassistant.components.switch",
    types.ModuleType("homeassistant.components.switch"),
)

if not hasattr(switch_component, "SwitchEntity"):

    class SwitchEntity:  # pragma: no cover - base entity stub
        async def async_write_ha_state(self) -> None:
            return None

    switch_component.SwitchEntity = SwitchEntity

# helpers.entity_registry
entity_registry_module = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)

if not hasattr(entity_registry_module, "async_get"):

    class DummyEntityRegistry:
        def async_get_entity_id(
            self, _domain: str, _platform: str, _unique_id: str
        ) -> str | None:
            return None

    def async_get(_hass: Any) -> DummyEntityRegistry:
        return DummyEntityRegistry()

    entity_registry_module.async_get = async_get

# helpers.device_registry
device_registry_module = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)

if not hasattr(device_registry_module, "DeviceInfo"):

    class DeviceInfo(dict):  # pragma: no cover - minimal stub
        """Dict-based DeviceInfo stub."""

    device_registry_module.DeviceInfo = DeviceInfo
    device_registry_module.CONNECTION_NETWORK_MAC = "mac"

# helpers.update_coordinator
helpers_module = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)

helpers_update_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)

if not hasattr(helpers_update_module, "CoordinatorEntity"):

    class CoordinatorEntity:  # pragma: no cover - stub only
        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator

        async def async_added_to_hass(self) -> None:  # type: ignore[empty-body]
            return None

        def __class_getitem__(cls, _item: Any) -> type:
            return cls

    helpers_update_module.CoordinatorEntity = CoordinatorEntity


if not hasattr(helpers_update_module, "DataUpdateCoordinator"):

    class DataUpdateCoordinator:  # pragma: no cover - stub only
        def __init__(self, hass: HomeAssistant) -> None:
            self.hass = hass

    helpers_update_module.DataUpdateCoordinator = DataUpdateCoordinator

helpers_module.update_coordinator = helpers_update_module

# helpers.event (used indirectly by helpers.schedule_post_write_refresh)
helpers_event_module = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)

if not hasattr(helpers_event_module, "async_call_later"):

    def async_call_later(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
        return None

    helpers_event_module.async_call_later = async_call_later

helpers_module.event = helpers_event_module

# Wire top-level helpers module
ha_module.helpers = helpers_module
sys.modules.setdefault("homeassistant.helpers", helpers_module)
sys.modules.setdefault("homeassistant.helpers.event", helpers_event_module)

# aiohttp stub (needed by __init__ import chain)
aiohttp_module = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

if not hasattr(aiohttp_module, "ClientResponseError"):

    class ClientResponseError(Exception):
        """Placeholder aiohttp.ClientResponseError stub."""

    aiohttp_module.ClientResponseError = ClientResponseError

# --- Package wiring for custom_components ---

custom_components_module = sys.modules.setdefault(
    "custom_components", types.ModuleType("custom_components")
)
custom_components_module.__path__ = [str(ROOT / "custom_components")]

airzone_package = sys.modules.setdefault(
    "custom_components.airzoneclouddaikin",
    types.ModuleType("custom_components.airzoneclouddaikin"),
)
airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]

airzone_init_stub = types.ModuleType("custom_components.airzoneclouddaikin.__init__")


class _AirzoneCoordinatorStub:
    """Lightweight AirzoneCoordinator replacement for switch imports."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.hass: HomeAssistant | None = None


airzone_init_stub.AirzoneCoordinator = _AirzoneCoordinatorStub
sys.modules["custom_components.airzoneclouddaikin.__init__"] = airzone_init_stub

# --- Import the real switch implementation ---

switch_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.switch",
    ROOT / "custom_components" / "airzoneclouddaikin" / "switch.py",
)
assert switch_spec and switch_spec.loader
switch_module_impl = importlib.util.module_from_spec(switch_spec)
sys.modules[switch_spec.name] = switch_module_impl
switch_spec.loader.exec_module(switch_module_impl)

AirzonePowerSwitch = switch_module_impl.AirzonePowerSwitch
DOMAIN = switch_module_impl.DOMAIN
ServiceNotFound = exceptions_module.ServiceNotFound  # type: ignore[attr-defined]


class DummyCoordinator:
    """Minimal coordinator stub exposing Airzone data and hass."""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = data
        self.hass: HomeAssistant | None = None


class DummyHass(HomeAssistant):
    """HomeAssistant stub with a controllable services layer."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def _async_call(
            domain: str,
            service: str,
            service_data: dict[str, Any],
            blocking: bool = False,
            context: Any | None = None,
        ) -> None:
            self.calls.append((domain, service, service_data))
            # Actual behavior (return / raise) is controlled per test
            return None

        self.services.async_call = _async_call  # type: ignore[assignment]


def _make_switch(
    device_snapshot: dict[str, Any],
    *,
    climate_entity_id: str = "climate.device",
) -> tuple[AirzonePowerSwitch, DummyHass]:
    """Helper to instantiate an AirzonePowerSwitch with stubs attached."""

    entry_id = "entry"
    device_id = device_snapshot.get("id", "device")

    hass = DummyHass()
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})["optimistic"] = {}

    coordinator = DummyCoordinator({device_id: device_snapshot})
    coordinator.hass = hass

    entity = AirzonePowerSwitch(coordinator, entry_id, device_id)
    entity.hass = hass
    entity.context = None  # type: ignore[assignment]
    entity._climate_entity_id = climate_entity_id

    # Avoid relying on the real entity registry in tests
    def _fake_resolve(self: AirzonePowerSwitch) -> str | None:
        return self._climate_entity_id

    entity._resolve_climate_entity_id = _fake_resolve.__get__(  # type: ignore[assignment]
        entity,
        AirzonePowerSwitch,
    )

    return entity, hass


def test_turn_on_timeout_keeps_proxy_and_calls_fallback() -> None:
    """Timeouts in the climate proxy should not clear the cached entity id."""

    device = {"id": "dev1", "name": "Zone", "power": "0"}
    entity, hass = _make_switch(device)

    async def failing_call(
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = False,
        context: Any | None = None,
    ) -> None:
        raise TimeoutError("boom")

    hass.services.async_call = failing_call  # type: ignore[assignment]

    called: dict[str, bool] = {"fallback": False}

    async def fake_fallback(self: AirzonePowerSwitch) -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback.__get__(  # type: ignore[assignment]
        entity,
        AirzonePowerSwitch,
    )

    asyncio.run(entity.async_turn_on())

    # Proxy should still be cached for next command
    assert entity._climate_entity_id == "climate.device"
    # And we must have fallen back to direct P1 control
    assert called["fallback"] is True


def test_turn_on_service_not_found_drops_proxy() -> None:
    """ServiceNotFound must decouple the proxy and still call the fallback."""

    device = {"id": "dev1", "name": "Zone", "power": "0"}
    entity, hass = _make_switch(device)

    async def failing_call(
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = False,
        context: Any | None = None,
    ) -> None:
        raise ServiceNotFound("service not found")

    hass.services.async_call = failing_call  # type: ignore[assignment]

    called: dict[str, bool] = {"fallback": False}

    async def fake_fallback(self: AirzonePowerSwitch) -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback.__get__(  # type: ignore[assignment]
        entity,
        AirzonePowerSwitch,
    )

    asyncio.run(entity.async_turn_on())

    # ServiceNotFound should clear the cached climate proxy
    assert entity._climate_entity_id is None
    # Fallback must still be executed
    assert called["fallback"] is True


def test_turn_on_homeassistant_error_keeps_proxy_and_calls_fallback() -> None:
    """HomeAssistantError in the climate proxy should not clear the cached entity id."""

    device = {"id": "dev1", "name": "Zone", "power": "0"}
    entity, hass = _make_switch(device)

    from homeassistant.exceptions import HomeAssistantError

    async def failing_call(
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = False,
        context: Any | None = None,
    ) -> None:
        raise HomeAssistantError("transient failure")

    hass.services.async_call = failing_call  # type: ignore[assignment]

    called: dict[str, bool] = {"fallback": False}

    async def fake_fallback(self: AirzonePowerSwitch) -> None:
        called["fallback"] = True

    entity._fallback_turn_on = fake_fallback.__get__(  # type: ignore[assignment]
        entity,
        AirzonePowerSwitch,
    )

    asyncio.run(entity.async_turn_on())

    assert entity._climate_entity_id == "climate.device"
    assert called["fallback"] is True


def test_turn_off_unexpected_error_drops_proxy_and_calls_fallback() -> None:
    """Unexpected exceptions in the climate proxy should drop the cached entity id."""

    device = {"id": "dev1", "name": "Zone", "power": "1"}
    entity, hass = _make_switch(device)

    async def failing_call(
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = False,
        context: Any | None = None,
    ) -> None:
        raise RuntimeError("unexpected boom")

    hass.services.async_call = failing_call  # type: ignore[assignment]

    called: dict[str, bool] = {"fallback": False}

    async def fake_fallback(self: AirzonePowerSwitch) -> None:
        called["fallback"] = True

    entity._fallback_turn_off = fake_fallback.__get__(  # type: ignore[assignment]
        entity,
        AirzonePowerSwitch,
    )

    asyncio.run(entity.async_turn_off())

    assert entity._climate_entity_id is None
    assert called["fallback"] is True


def test_send_event_logs_and_reraises_on_failure() -> None:
    """_send_event should log and re-raise errors from the API client."""

    device = {"id": "dev1", "name": "Zone", "power": "0"}
    entity, _hass = _make_switch(device)

    class DummyAPI:
        async def send_event(self, _payload: dict[str, Any]) -> None:
            raise RuntimeError("P1 failed")

    entity.coordinator.api = DummyAPI()  # type: ignore[attr-defined]

    async def invoke() -> None:
        await entity._send_event("P1", 1)

    try:
        asyncio.run(invoke())
    except RuntimeError as err:
        assert "P1 failed" in str(err)
    else:  # pragma: no cover - safety net
        raise AssertionError("_send_event did not re-raise API error")


def test_backend_power_is_on_normalizes_values() -> None:
    """_backend_power_is_on should normalize common truthy/falsey values."""
    truthy_values = [True, "true", "on", "yes", 1, "1", 2, "2"]
    falsey_values = [False, "false", "off", "no", 0, "0", "", "none", None]

    for value in truthy_values:
        device = {"id": "dev1", "name": "Zone", "power": value}
        entity, _hass = _make_switch(device)
        assert entity._backend_power_is_on() is True

    for value in falsey_values:
        device = {"id": "dev1", "name": "Zone", "power": value}
        entity, _hass = _make_switch(device)
        assert entity._backend_power_is_on() is False
