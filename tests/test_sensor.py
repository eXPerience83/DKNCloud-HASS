"""Tests for sensor setup behaviors."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)


class HomeAssistant:  # pragma: no cover - stub only
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


core_module.HomeAssistant = HomeAssistant

components_module = sys.modules.setdefault(
    "homeassistant.components", types.ModuleType("homeassistant.components")
)

sensor_component = sys.modules.setdefault(
    "homeassistant.components.sensor",
    types.ModuleType("homeassistant.components.sensor"),
)


class SensorEntity:  # pragma: no cover - stub only
    pass


class SensorDeviceClass:  # pragma: no cover - stub only
    TEMPERATURE = "temperature"
    TIMESTAMP = "timestamp"


class SensorStateClass:  # pragma: no cover - stub only
    MEASUREMENT = "measurement"


sensor_component.SensorEntity = SensorEntity
sensor_component.SensorDeviceClass = SensorDeviceClass
sensor_component.SensorStateClass = SensorStateClass
components_module.sensor = sensor_component
ha_module.components = components_module

const_module = sys.modules.setdefault(
    "homeassistant.const", types.ModuleType("homeassistant.const")
)
const_module.UnitOfTemperature = types.SimpleNamespace(CELSIUS="°C")
const_module.UnitOfTime = types.SimpleNamespace(MINUTES="min")

helpers_module = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)

entity_module = sys.modules.setdefault(
    "homeassistant.helpers.entity", types.ModuleType("homeassistant.helpers.entity")
)


class EntityCategory:  # pragma: no cover - stub only
    DIAGNOSTIC = "diagnostic"


entity_module.EntityCategory = EntityCategory

update_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class CoordinatorEntity:  # pragma: no cover - stub only
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, _item: Any) -> type:
        return cls


class DataUpdateCoordinator:  # pragma: no cover - stub only
    pass


update_module.CoordinatorEntity = CoordinatorEntity
update_module.DataUpdateCoordinator = DataUpdateCoordinator

registry_module = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)


class DeviceInfo(dict):  # pragma: no cover - stub only
    """Dict-backed DeviceInfo stub."""


device_registry_module = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)
device_registry_module.CONNECTION_NETWORK_MAC = "mac"
device_registry_module.DeviceInfo = DeviceInfo

util_module = sys.modules.setdefault(
    "homeassistant.util", types.ModuleType("homeassistant.util")
)
dt_module = sys.modules.setdefault(
    "homeassistant.util.dt", types.ModuleType("homeassistant.util.dt")
)


def _noop_parse_datetime(_value: Any) -> None:
    return None


dt_module.parse_datetime = _noop_parse_datetime
util_module.dt = dt_module

event_module = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)


def async_call_later(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
    return None


event_module.async_call_later = async_call_later

helpers_module.entity = entity_module
helpers_module.update_coordinator = update_module
helpers_module.entity_registry = registry_module
helpers_module.device_registry = device_registry_module
helpers_module.event = event_module
ha_module.helpers = helpers_module

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
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}


airzone_init_stub.AirzoneCoordinator = _AirzoneCoordinatorStub
sys.modules["custom_components.airzoneclouddaikin.__init__"] = airzone_init_stub

sensor_spec = importlib.util.spec_from_file_location(
    "custom_components.airzoneclouddaikin.sensor",
    ROOT / "custom_components" / "airzoneclouddaikin" / "sensor.py",
)
assert sensor_spec and sensor_spec.loader
sensor_module = importlib.util.module_from_spec(sensor_spec)
sys.modules[sensor_spec.name] = sensor_module
sensor_spec.loader.exec_module(sensor_module)

DOMAIN = sensor_module.DOMAIN


class DummyCoordinator:
    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = data


class DummyRegistry:
    def __init__(self, entities: list[Any]) -> None:
        self._entities = entities
        self.removed: list[str] = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


class DummyHass:
    def __init__(self, coordinator: DummyCoordinator, entry_id: str) -> None:
        self.data = {DOMAIN: {entry_id: {"coordinator": coordinator}}}


class DummyEntry:
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.options = {"expose_pii_identifiers": False}
        self.data = {}


class RegistryEntity:
    def __init__(
        self, entity_id: str, unique_id: str, domain: str, platform: str
    ) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.domain = domain
        self.platform = platform


def test_async_setup_entry_removes_orphan_pii_entities_only() -> None:
    """Opt-out cleanup removes orphan PII entities even if coordinator snapshot is empty."""

    entry_id = "entry-1"
    coordinator = DummyCoordinator(data={})
    hass = DummyHass(coordinator, entry_id)
    entry = DummyEntry(entry_id)

    registry_entities = [
        RegistryEntity("sensor.olddevice_mac", "olddevice_mac", "sensor", DOMAIN),
        RegistryEntity(
            "sensor.olddevice_local_temp", "olddevice_local_temp", "sensor", DOMAIN
        ),
        RegistryEntity("sensor.external_mac", "other_mac", "sensor", "other_platform"),
        RegistryEntity(
            "binary_sensor.olddevice_mac", "olddevice_mac", "binary_sensor", DOMAIN
        ),
    ]
    reg = DummyRegistry(registry_entities)

    registry_module.async_get = lambda _hass: reg
    registry_module.async_entries_for_config_entry = (
        lambda _reg, _entry_id: registry_entities
    )

    added_entities: list[Any] = []

    async def _run_setup() -> None:
        await sensor_module.async_setup_entry(hass, entry, added_entities.extend)

    import asyncio

    asyncio.run(_run_setup())

    assert reg.removed == ["sensor.olddevice_mac"]
    assert added_entities == []
