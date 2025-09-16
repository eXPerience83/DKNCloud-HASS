"""Sensors for DKN Cloud for HASS (Airzone Cloud).

Changes in this revision:
- Read devices from `coordinator.data` (dict keyed by device_id), not from a "devices" list.
- Normalize value types (int temperature, int minutes) to avoid "32,0 ºC" or "30,0" displays.
- Make "Sleep Timer (min)" enabled by default (user-requested).
- Add diagnostic sensors for MAC Address and PIN (disabled by default). Exposing PIN can be sensitive;
  it is off by default and flagged as a diagnostic sensor; enable only if you understand the risk.
- Keep timestamp sensors for "Connection Date" and "Device Update Date" but disabled by default.
- Set proper device_class/unit for duration (minutes) to comply with HA expectations.

All sensors use `device_class`/`state_class`/units that match HA expectations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Create sensors per device from the coordinator snapshot."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        return
    coordinator: DataUpdateCoordinator = data["coordinator"]

    entities: list[SensorEntity] = []
    for device_id in coordinator.data.keys():
        entities.extend(
            [
                LocalTemperatureSensor(coordinator, device_id),
                SleepTimerSensor(coordinator, device_id),
                ScenarySensor(coordinator, device_id),
                ConnectionDateSensor(coordinator, device_id),
                UpdateDateSensor(coordinator, device_id),
                MacAddressSensor(coordinator, device_id),
                PinSensor(coordinator, device_id),
            ]
        )

    async_add_entities(entities)


@dataclass
class _Ctx:
    device_id: str
    name: str
    mac: str | None


class _BaseSensor(CoordinatorEntity, SensorEntity):
    """Base helper for sensors tied to a single device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        dev = coordinator.data.get(device_id, {})
        self._ctx = _Ctx(
            device_id=str(device_id),
            name=str(dev.get("name") or "Airzone Device"),
            mac=(dev.get("mac") or None),
        )
        self._attr_device_info = self._device_info()

    def _device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._ctx.device_id)},
            "name": self._ctx.name,
            "manufacturer": "Daikin / Airzone",
        }
        if self._ctx.mac:
            # Home Assistant prefers a set of (connection_type, identifier) tuples
            info["connections"] = {("mac", self._ctx.mac)}
        return info

    @property
    def _device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._ctx.device_id, {})

    def _raw(self, key: str) -> Any:
        return self._device.get(key)


# --- Concrete sensors ------------------------------------------------------------


class LocalTemperatureSensor(_BaseSensor):
    """Ambient temperature as integer °C (HA UI shows whole degrees)."""

    _attr_name = "Local Temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_local_temp"

    @property
    def native_value(self) -> int | None:
        raw = self._raw("local_temp")
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            return None


class SleepTimerSensor(_BaseSensor):
    """Minutes remaining for sleep timer."""

    _attr_name = "Sleep Timer (min)"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = True  # user-requested visible by default
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_sleep_time"

    @property
    def native_value(self) -> int | None:
        raw = self._raw("sleep_time")
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError):
            return None


class ScenarySensor(_BaseSensor):
    """Current scenary string (occupied, sleep, etc.)."""

    _attr_name = "Scenary"
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_scenary"

    @property
    def native_value(self) -> str | None:
        val = self._raw("scenary")
        return str(val) if val is not None else None


class ConnectionDateSensor(_BaseSensor):
    """Timestamp of last connection reported by device (often updated frequently)."""

    _attr_name = "Connection Date"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False  # noisy; keep disabled by default
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_connection_date"

    @property
    def native_value(self) -> datetime | None:
        raw = self._raw("connection_date")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None


class UpdateDateSensor(_BaseSensor):
    """Device-reported update timestamp (can be stale on some firmwares)."""

    _attr_name = "Device Update Date"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False  # often very old values
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_update_date"

    @property
    def native_value(self) -> datetime | None:
        raw = self._raw("update_date")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None


class MacAddressSensor(_BaseSensor):
    """Device MAC address (diagnostic)."""

    _attr_name = "MAC Address"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_mac"

    @property
    def native_value(self) -> str | None:
        val = self._raw("mac")
        return str(val) if val is not None else None


class PinSensor(_BaseSensor):
    """Device PIN (diagnostic; disabled by default – may be sensitive)."""

    _attr_name = "PIN"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_unique_id: str

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{self._ctx.device_id}_pin"

    @property
    def native_value(self) -> str | None:
        val = self._raw("pin")
        return str(val) if val is not None else None
