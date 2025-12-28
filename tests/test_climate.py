"""Tests for Airzone climate HEAT_COOL exposure gating."""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from enum import Enum, IntFlag
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
components_module = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)
ha_module.components = getattr(ha_module, "components", components_module)

climate_module = sys.modules.setdefault(
    "homeassistant.components.climate",
    types.ModuleType("homeassistant.components.climate"),
)

climate_const_module = sys.modules.setdefault(
    "homeassistant.components.climate.const",
    types.ModuleType("homeassistant.components.climate.const"),
)

const_module = sys.modules.setdefault(
    "homeassistant.const", types.ModuleType("homeassistant.const")
)

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)

exceptions_module = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)
config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)
aiohttp_client_module = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)
dt_module = sys.modules.setdefault(
    "homeassistant.util.dt", types.ModuleType("homeassistant.util.dt")
)

helpers_module = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)

helpers_event_module = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
helpers_translation_module = sys.modules.setdefault(
    "homeassistant.helpers.translation",
    types.ModuleType("homeassistant.helpers.translation"),
)
device_registry_module = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)

helpers_update_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)

if not hasattr(helpers_event_module, "async_call_later"):

    def async_call_later(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError

    helpers_event_module.async_call_later = async_call_later

if not hasattr(helpers_translation_module, "async_get_translations"):

    async def async_get_translations(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    helpers_translation_module.async_get_translations = async_get_translations

helpers_module.event = helpers_event_module
helpers_module.translation = helpers_translation_module

aiohttp_module = sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

if not hasattr(aiohttp_module, "ClientResponseError"):

    class _ClientResponseError(Exception):
        pass

    aiohttp_module.ClientResponseError = _ClientResponseError


if not hasattr(core_module, "callback"):

    def callback(func):
        return func

    core_module.callback = callback

if not hasattr(exceptions_module, "ConfigEntryAuthFailed"):

    class ConfigEntryAuthFailed(Exception):
        pass

    exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed

if not hasattr(config_entries_module, "SOURCE_REAUTH"):
    config_entries_module.SOURCE_REAUTH = "reauth"

if not hasattr(config_entries_module, "ConfigEntry"):

    class ConfigEntry:
        def __init__(self) -> None:
            self.entry_id = "entry"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}
            self.version = 1
            self.unique_id: str | None = None

    config_entries_module.ConfigEntry = ConfigEntry

if not hasattr(aiohttp_client_module, "async_get_clientsession"):

    async def async_get_clientsession(*_args: Any, **_kwargs: Any) -> object:
        return object()

    aiohttp_client_module.async_get_clientsession = async_get_clientsession

helpers_module.aiohttp_client = aiohttp_client_module

if not hasattr(dt_module, "utcnow"):

    from datetime import datetime

    def utcnow() -> datetime:
        return datetime.utcnow()

    dt_module.utcnow = utcnow

if not hasattr(core_module, "HomeAssistant"):

    class HomeAssistant:
        loop: Any
        data: dict[str, Any]

    core_module.HomeAssistant = HomeAssistant


if not hasattr(const_module, "ATTR_TEMPERATURE"):
    const_module.ATTR_TEMPERATURE = "temperature"

if not hasattr(const_module, "PRECISION_WHOLE"):
    const_module.PRECISION_WHOLE = 1

if not hasattr(const_module, "CONF_USERNAME"):
    const_module.CONF_USERNAME = "username"

if not hasattr(const_module, "UnitOfTemperature"):

    class UnitOfTemperature(str, Enum):
        CELSIUS = "°C"

    const_module.UnitOfTemperature = UnitOfTemperature


if not hasattr(climate_const_module, "HVACMode"):

    class HVACMode(str, Enum):
        OFF = "off"
        COOL = "cool"
        HEAT = "heat"
        FAN_ONLY = "fan_only"
        DRY = "dry"
        HEAT_COOL = "heat_cool"

    climate_const_module.HVACMode = HVACMode

if not hasattr(climate_const_module, "ClimateEntityFeature"):

    class ClimateEntityFeature(IntFlag):
        TARGET_TEMPERATURE = 1
        FAN_MODE = 2

    climate_const_module.ClimateEntityFeature = ClimateEntityFeature

if not hasattr(climate_module, "ClimateEntity"):

    class ClimateEntity:  # pragma: no cover - stub only
        """Minimal ClimateEntity stub."""

        _attr_hvac_mode: HVACMode | None = None

        def __init__(self) -> None:
            self.hass: Any = None

    climate_module.ClimateEntity = ClimateEntity

if not hasattr(climate_module, "HVACMode"):
    climate_module.HVACMode = climate_const_module.HVACMode

if not hasattr(climate_module, "ClimateEntityFeature"):
    climate_module.ClimateEntityFeature = climate_const_module.ClimateEntityFeature


if not hasattr(device_registry_module, "CONNECTION_NETWORK_MAC"):
    device_registry_module.CONNECTION_NETWORK_MAC = "network_mac"

if not hasattr(device_registry_module, "DeviceInfo"):

    @dataclass
    class DeviceInfo:  # pragma: no cover - stub only
        identifiers: set[tuple[str, str]]
        manufacturer: str | None = None
        model: str | None = None
        sw_version: str | None = None
        name: str | None = None
        connections: set[tuple[str, str]] | None = None

    device_registry_module.DeviceInfo = DeviceInfo

helpers_module.device_registry = device_registry_module
sys.modules.setdefault("homeassistant.helpers.device_registry", device_registry_module)

if not hasattr(helpers_update_module, "DataUpdateCoordinator"):

    class DataUpdateCoordinator:  # pragma: no cover - stub only
        ...

    helpers_update_module.DataUpdateCoordinator = DataUpdateCoordinator

if not hasattr(helpers_update_module, "CoordinatorEntity"):

    class CoordinatorEntity:  # pragma: no cover - stub only
        """Coordinator entity stub that captures the coordinator reference."""

        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def __class_getitem__(cls, _item: Any) -> type[CoordinatorEntity]:
            return cls

    helpers_update_module.CoordinatorEntity = CoordinatorEntity

helpers_module.update_coordinator = helpers_update_module
sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator", helpers_update_module
)

custom_components_module = sys.modules.setdefault(
    "custom_components", types.ModuleType("custom_components")
)
custom_components_module.__path__ = [str(ROOT / "custom_components")]

airzone_package = sys.modules.setdefault(
    "custom_components.airzoneclouddaikin",
    types.ModuleType("custom_components.airzoneclouddaikin"),
)
airzone_package.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]

airzone_init_module = sys.modules.setdefault(
    "custom_components.airzoneclouddaikin.__init__",
    types.ModuleType("custom_components.airzoneclouddaikin.__init__"),
)

if not hasattr(airzone_init_module, "AirzoneCoordinator"):

    class AirzoneCoordinator:  # pragma: no cover - stub only
        data: dict[str, dict[str, Any]] | None = None

    airzone_init_module.AirzoneCoordinator = AirzoneCoordinator

climate_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.climate",
    ROOT / "custom_components" / "airzoneclouddaikin" / "climate.py",
)
assert climate_spec and climate_spec.loader
climate_module_impl = importlib.util.module_from_spec(climate_spec)
sys.modules[climate_spec.name] = climate_module_impl
climate_spec.loader.exec_module(climate_module_impl)

AirzoneClimate = climate_module_impl.AirzoneClimate
HVACMode = climate_const_module.HVACMode
DOMAIN = climate_module_impl.DOMAIN


class DummyConfigEntries:
    def async_get_entry(self, _entry_id: str) -> None:
        return None


class DummyHass:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.config_entries = DummyConfigEntries()


class DummyCoordinator:
    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = data
        self.hass: DummyHass | None = None


def _make_climate(
    device_snapshot: dict[str, Any], *, heat_cool_opt_in: bool
) -> AirzoneClimate:
    entry_id = "entry"
    device_id = "device"
    hass = DummyHass()
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})[
        "heat_cool_opt_in"
    ] = heat_cool_opt_in

    coordinator = DummyCoordinator({device_id: device_snapshot})
    coordinator.hass = hass

    entity = AirzoneClimate(coordinator, entry_id, device_id)
    entity.hass = hass
    return entity


def test_heat_cool_hidden_without_device_support() -> None:
    """HEAT_COOL must remain hidden when the device bitmask lacks P2=4."""

    device = {
        "name": "Zone",  # name only for completeness
        "modes": "11101",  # P2=1,2,3,5 enabled; P2=4 disabled
        "mode": "1",
        "power": "1",
    }
    entity = _make_climate(device, heat_cool_opt_in=True)

    modes = entity.hvac_modes
    assert modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]


def test_heat_cool_exposed_when_device_supports_and_opt_in_enabled() -> None:
    """HEAT_COOL should be exposed when both support and opt-in are true."""

    device = {
        "name": "Zone",
        "modes": "11111",  # Includes P2=4
        "mode": "1",
        "power": "1",
    }
    entity = _make_climate(device, heat_cool_opt_in=True)

    modes = entity.hvac_modes
    assert modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.HEAT_COOL,
        HVACMode.DRY,
    ]


def test_heat_cool_retained_when_current_mode_loses_support() -> None:
    """The current HEAT_COOL mode should remain visible even if support disappears."""

    device = {
        "name": "Zone",
        "modes": "11101",  # P2=4 disabled post-refresh
        "mode": "4",
        "power": "1",
    }
    entity = _make_climate(device, heat_cool_opt_in=True)

    modes = entity.hvac_modes
    assert modes == [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
        HVACMode.HEAT_COOL,
    ]
