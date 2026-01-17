"""Tests for number entity payload shapes."""

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

# --- Stub Home Assistant modules used by number.py ---

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)


class HomeAssistant:  # pragma: no cover - type stub only
    """Minimal HomeAssistant stub for type hints."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


core_module.HomeAssistant = HomeAssistant

config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)

if not hasattr(config_entries_module, "ConfigEntry"):

    class ConfigEntry:
        """Minimal ConfigEntry stub."""

        entry_id = "entry"

    config_entries_module.ConfigEntry = ConfigEntry

number_component = sys.modules.setdefault(
    "homeassistant.components.number",
    types.ModuleType("homeassistant.components.number"),
)

if not hasattr(number_component, "NumberEntity"):

    class NumberEntity:  # pragma: no cover - base entity stub
        def async_write_ha_state(self) -> None:
            return None

    number_component.NumberEntity = NumberEntity

if not hasattr(number_component, "NumberMode"):

    class NumberMode:
        """NumberMode stub."""

        SLIDER = "slider"

    number_component.NumberMode = NumberMode

helpers_entity_module = sys.modules.setdefault(
    "homeassistant.helpers.entity", types.ModuleType("homeassistant.helpers.entity")
)

if not hasattr(helpers_entity_module, "EntityCategory"):

    class EntityCategory:
        """EntityCategory stub."""

        CONFIG = "config"

    helpers_entity_module.EntityCategory = EntityCategory

helpers_device_registry_module = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)

if not hasattr(helpers_device_registry_module, "DeviceInfo"):

    class DeviceInfo(dict):  # pragma: no cover - minimal stub
        """Dict-based DeviceInfo stub."""

    helpers_device_registry_module.DeviceInfo = DeviceInfo
    helpers_device_registry_module.CONNECTION_NETWORK_MAC = "mac"

helpers_update_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)

if not hasattr(helpers_update_module, "CoordinatorEntity"):

    class CoordinatorEntity:  # pragma: no cover - stub only
        def __init__(self, coordinator: Any) -> None:
            self.coordinator = coordinator

        def __class_getitem__(cls, _item: Any) -> type:
            return cls

    helpers_update_module.CoordinatorEntity = CoordinatorEntity

const_module = sys.modules.setdefault(
    "homeassistant.const", types.ModuleType("homeassistant.const")
)

if not hasattr(const_module, "UnitOfTemperature"):

    class UnitOfTemperature:
        """UnitOfTemperature stub."""

        CELSIUS = "°C"

    const_module.UnitOfTemperature = UnitOfTemperature

if not hasattr(const_module, "UnitOfTime"):

    class UnitOfTime:
        """UnitOfTime stub."""

        MINUTES = "min"

    const_module.UnitOfTime = UnitOfTime

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
    """Lightweight AirzoneCoordinator replacement for number imports."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.hass: HomeAssistant | None = None


airzone_init_stub.AirzoneCoordinator = _AirzoneCoordinatorStub
sys.modules["custom_components.airzoneclouddaikin.__init__"] = airzone_init_stub

airzone_api_stub = types.ModuleType("custom_components.airzoneclouddaikin.airzone_api")


class AirzoneAPI:  # pragma: no cover - type stub
    """Minimal AirzoneAPI stub."""


airzone_api_stub.AirzoneAPI = AirzoneAPI
sys.modules["custom_components.airzoneclouddaikin.airzone_api"] = airzone_api_stub

helpers_stub = types.ModuleType("custom_components.airzoneclouddaikin.helpers")


def acquire_device_lock(*_args: Any, **_kwargs: Any) -> asyncio.Lock:
    return asyncio.Lock()


def clamp_number(value: float, *_args: Any, **_kwargs: Any) -> float:
    return value


def optimistic_get(
    _hass: Any, _entry_id: str, _device_id: str, _field: str, backend_value: Any
) -> Any:
    return backend_value


def optimistic_set(*_args: Any, **_kwargs: Any) -> None:
    return None


def optimistic_invalidate(*_args: Any, **_kwargs: Any) -> None:
    return None


async def async_auto_exit_sleep_if_needed(*_args: Any, **_kwargs: Any) -> None:
    return None


def schedule_post_write_refresh(*_args: Any, **_kwargs: Any) -> None:
    return None


helpers_stub.acquire_device_lock = acquire_device_lock
helpers_stub.clamp_number = clamp_number
helpers_stub.optimistic_get = optimistic_get
helpers_stub.optimistic_set = optimistic_set
helpers_stub.optimistic_invalidate = optimistic_invalidate
helpers_stub.async_auto_exit_sleep_if_needed = async_auto_exit_sleep_if_needed
helpers_stub.schedule_post_write_refresh = schedule_post_write_refresh
sys.modules["custom_components.airzoneclouddaikin.helpers"] = helpers_stub

# --- Import the real number implementation ---

number_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.number",
    ROOT / "custom_components" / "airzoneclouddaikin" / "number.py",
)
assert number_spec and number_spec.loader
number_module_impl = importlib.util.module_from_spec(number_spec)
sys.modules[number_spec.name] = number_module_impl
number_spec.loader.exec_module(number_module_impl)

DKNSleepTimeNumber = number_module_impl.DKNSleepTimeNumber
DKNUnoccupiedHeatMinNumber = number_module_impl.DKNUnoccupiedHeatMinNumber
DKNUnoccupiedCoolMaxNumber = number_module_impl.DKNUnoccupiedCoolMaxNumber


class DummyCoordinator:
    """Minimal coordinator stub exposing Airzone data and hass."""

    def __init__(self, data: dict[str, dict[str, Any]], hass: HomeAssistant) -> None:
        self.data = data
        self.hass = hass


class DummyAPI:
    """Capture payloads sent to put_device_fields."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def put_device_fields(self, device_id: str, payload: dict[str, Any]) -> None:
        self.calls.append((device_id, payload))


def _make_entity(
    entity_cls: type[Any],
    *,
    device_data: dict[str, Any],
) -> tuple[Any, DummyAPI]:
    entry_id = "entry"
    device_id = "dev1"

    hass = HomeAssistant()
    hass.data.setdefault("airzoneclouddaikin", {}).setdefault(entry_id, {})[
        "optimistic"
    ] = {}

    coordinator = DummyCoordinator({device_id: device_data}, hass)
    api = DummyAPI()

    entity = entity_cls(
        coordinator=coordinator,
        api=api,
        entry_id=entry_id,
        device_id=device_id,
    )
    entity.hass = hass

    return entity, api


def test_sleep_time_payload_is_root_level() -> None:
    """Sleep time should send a root-level payload."""

    entity, api = _make_entity(DKNSleepTimeNumber, device_data={"sleep_time": 30})

    asyncio.run(entity.async_set_native_value(40))

    assert api.calls == [("dev1", {"sleep_time": 40})]


def test_unoccupied_heat_min_payload_is_root_level() -> None:
    """Unoccupied heat min should send a root-level payload."""

    entity, api = _make_entity(
        DKNUnoccupiedHeatMinNumber, device_data={"min_temp_unoccupied": 16}
    )

    asyncio.run(entity.async_set_native_value(18))

    assert api.calls == [("dev1", {"min_temp_unoccupied": 18})]


def test_unoccupied_cool_max_payload_is_root_level() -> None:
    """Unoccupied cool max should send a root-level payload."""

    entity, api = _make_entity(
        DKNUnoccupiedCoolMaxNumber, device_data={"max_temp_unoccupied": 26}
    )

    asyncio.run(entity.async_set_native_value(28))

    assert api.calls == [("dev1", {"max_temp_unoccupied": 28})]
