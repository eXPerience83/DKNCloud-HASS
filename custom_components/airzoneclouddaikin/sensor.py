"""Sensor platform for DKN Cloud for HASS (Airzone Cloud).

This module exposes a small set of user-facing sensors and a richer set of
diagnostic sensors. It reads all values from the DataUpdateCoordinator snapshot
(no I/O in entity properties).

Key design choices:
- **Privacy first**: do NOT expose PIN, MAC, token, email or coordinates as sensors.
  MAC is only used in `device_info.connections` so HA can link devices; it is not
  shown as a sensor.
- **Correct types/units**: integers for temperatures and minutes; timestamps as
  timezone-aware datetimes; avoid float artifacts like "30.0" for minutes.
- **Minimal defaults**: Only a few sensors are enabled by default. Others are
  available under the Diagnostic category.
"""

from __future__ import annotations

import logging
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
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up Airzone Cloud sensors from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No integration data for entry %s", entry.entry_id)
        return

    coordinator: DataUpdateCoordinator = data["coordinator"]
    devices: list[dict[str, Any]] = data["devices"]

    entities: list[SensorEntity] = []

    for dev in devices:
        # Core/user-facing sensors
        entities.append(LocalTemperatureSensor(coordinator, dev))
        entities.append(SleepTimerSensor(coordinator, dev))  # visible by default
        entities.append(ScenarySensor(coordinator, dev))  # visible by default

        # Diagnostic sensors (disabled by default unless specified)
        entities.extend(
            [
                TimestampSensor(
                    coordinator,
                    dev,
                    key="connection_date",
                    name="Connection Date",
                    enabled_by_default=False,
                ),
                TimestampSensor(
                    coordinator,
                    dev,
                    key="update_date",
                    name="Device Update Date",
                    enabled_by_default=False,
                ),
                IntSensor(
                    coordinator,
                    dev,
                    key="availables_speeds",
                    name="Available Fan Speeds",
                    icon="mdi:fan",
                    enabled_by_default=False,
                ),
                TextSensor(
                    coordinator,
                    dev,
                    key="modes",
                    name="Supported Modes (Bitmask)",
                    icon="mdi:toggle-switch",
                    enabled_by_default=False,
                ),
                IntSensor(
                    coordinator,
                    dev,
                    key="min_temp_unoccupied",
                    name="Min Temperature (Unoccupied)",
                    icon="mdi:thermometer-low",
                    unit=UnitOfTemperature.CELSIUS,
                    enabled_by_default=False,
                ),
                IntSensor(
                    coordinator,
                    dev,
                    key="max_temp_unoccupied",
                    name="Max Temperature (Unoccupied)",
                    icon="mdi:thermometer-high",
                    unit=UnitOfTemperature.CELSIUS,
                    enabled_by_default=False,
                ),
                TextSensor(
                    coordinator,
                    dev,
                    key="machine_errors",
                    name="Machine Errors",
                    icon="mdi:alert-octagon",
                    enabled_by_default=False,
                ),
                TextSensor(
                    coordinator,
                    dev,
                    key="firmware",
                    name="Firmware Version",
                    icon="mdi:chip",
                    enabled_by_default=False,
                ),
                TextSensor(
                    coordinator,
                    dev,
                    key="brand",
                    name="Brand/Model",
                    icon="mdi:factory",
                    enabled_by_default=False,
                ),
                TextSensor(
                    coordinator,
                    dev,
                    key="time_zone",
                    name="Time Zone",
                    icon="mdi:earth",
                    enabled_by_default=False,
                ),
            ]
        )

    async_add_entities(entities)


# === Base entity ==================================================================


@dataclass
class _DeviceContext:
    device_id: str
    mac: str | None
    name: str


class AirzoneBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for DKN sensors that read from the coordinator snapshot.

    All children must implement `native_value` reading from keys.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC  # overridden for user-facing

    def __init__(
        self, coordinator: DataUpdateCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._ctx = _DeviceContext(
            device_id=str(device.get("id", "")),
            mac=(device.get("mac") or None),
            name=str(device.get("name") or "Airzone Device"),
        )
        # Unique ID is derived in child classes (suffix per-sensor)

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the entity to the physical device.

        Privacy note:
          - We do not expose PIN or any PII here.
          - MAC is used strictly as a connection identifier for HA's registry.
        """
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._ctx.device_id)},
            "name": self._ctx.name,
            "manufacturer": "Daikin / Airzone",
        }
        if self._ctx.mac:
            # Safe: device registry connection, not a sensor
            info["connections"] = {("mac", self._ctx.mac)}
        return info

    def _find_device_snapshot(self) -> dict[str, Any] | None:
        """Get the latest device dict from coordinator data by id."""
        devices: list[dict[str, Any]] = self.coordinator.data.get("devices", [])
        for dev in devices:
            if str(dev.get("id")) == self._ctx.device_id:
                return dev
        return None

    @property
    def available(self) -> bool:
        return self._find_device_snapshot() is not None

    def _get_raw(self, key: str) -> Any:
        dev = self._find_device_snapshot()
        return None if dev is None else dev.get(key)


# === Concrete sensors ==============================================================


class LocalTemperatureSensor(AirzoneBaseSensor):
    """Local ambient temperature as integer °C.

    Shown by default and not diagnostic.
    """

    _attr_has_entity_name = True
    _attr_name = "Local Temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = None  # user-facing

    def __init__(
        self, coordinator: DataUpdateCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._ctx.device_id}_local_temperature"

    @property
    def native_value(self) -> int | None:
        raw = self._get_raw("local_temp")
        if raw is None:
            return None
        try:
            # API may send "27.0" as string -> cast to int (1°C steps device)
            return int(float(raw))
        except (ValueError, TypeError):
            _LOGGER.debug(
                "Invalid local_temp value %r for device %s", raw, self._ctx.device_id
            )
            return None


class SleepTimerSensor(AirzoneBaseSensor):
    """Sleep timer minutes. Enabled by default and user-facing.

    Even when `scenary` is not 'sleep', this is visible to help forecast behavior.
    """

    _attr_has_entity_name = True
    _attr_name = "Sleep Timer"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = None  # user-facing

    def __init__(
        self, coordinator: DataUpdateCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._ctx.device_id}_sleep_timer"

    @property
    def native_value(self) -> int | None:
        raw = self._get_raw("sleep_time")
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None


class ScenarySensor(AirzoneBaseSensor):
    """Current scenary (occupied/sleep/unoccupied)."""

    _attr_has_entity_name = True
    _attr_name = "Scenary"

    def __init__(
        self, coordinator: DataUpdateCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{self._ctx.device_id}_scenary"

    @property
    def native_value(self) -> str | None:
        val = self._get_raw("scenary")
        return str(val) if val is not None else None


class TimestampSensor(AirzoneBaseSensor):
    """Generic timestamp sensor for ISO strings from the API."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        device: dict[str, Any],
        *,
        key: str,
        name: str,
        enabled_by_default: bool = False,
    ) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{self._ctx.device_id}_{key}"
        if not enabled_by_default:
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> datetime | None:
        raw = self._get_raw(self._key)
        if not raw:
            return None
        # Use HA's robust parser to get timezone-aware datetime
        try:
            dt = dt_util.parse_datetime(str(raw))
        except Exception:  # noqa: BLE001
            dt = None
        return dt


class IntSensor(AirzoneBaseSensor):
    """Generic integer sensor (optionally with unit)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        device: dict[str, Any],
        *,
        key: str,
        name: str,
        icon: str | None = None,
        unit: str | None = None,
        enabled_by_default: bool = False,
    ) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{self._ctx.device_id}_{key}"
        if icon:
            self._attr_icon = icon
        if unit:
            self._attr_native_unit_of_measurement = unit
        self._attr_entity_registry_enabled_default = enabled_by_default
        # Keep as diagnostic
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        raw = self._get_raw(self._key)
        if raw is None or raw == "":
            return None
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            return None


class TextSensor(AirzoneBaseSensor):
    """Generic text sensor for diagnostic attributes."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        device: dict[str, Any],
        *,
        key: str,
        name: str,
        icon: str | None = None,
        enabled_by_default: bool = False,
    ) -> None:
        super().__init__(coordinator, device)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{self._ctx.device_id}_{key}"
        if icon:
            self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled_by_default
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        raw = self._get_raw(self._key)
        if raw is None:
            return None
        val = str(raw).strip()
        return val or None
