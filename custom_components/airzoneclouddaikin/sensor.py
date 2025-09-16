"""Sensor platform for DKN Cloud for HASS (Airzone Cloud).

Consistent creation off coordinator.data (dict by device_id).
Core sensors enabled-by-default so they are visible out-of-the-box.
No I/O in properties; updates come from the coordinator.

Privacy: do not expose PIN as a sensor; redact secrets in logs.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# (attribute, friendly name, icon, enabled_by_default, device_class, state_class)
CORE_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    (
        "local_temp",
        "Local Temperature",
        "mdi:thermometer",
        True,
        "temperature",
        "measurement",
    ),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand", True, None, None),
    ("scenary", "Scenary", "mdi:account-clock", True, None, None),
    ("modes", "Supported Modes (Bitmask)", "mdi:toggle-switch", True, None, None),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True, None, None),
    ("firmware", "Firmware Version", "mdi:chip", True, None, None),
    ("brand", "Brand/Model", "mdi:factory", True, None, None),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True, None, None),
    ("cold_consign", "Cool Setpoint", "mdi:snowflake", True, None, None),
    ("heat_consign", "Heat Setpoint", "mdi:fire", True, None, None),
    ("cold_speed", "Cool Fan Speed", "mdi:fan", True, None, None),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True, None, None),
]

# Additional diagnostics (disabled by default to reduce UI noise)
DIAG_SENSORS: list[tuple[str, str, str, bool, str | None, str | None]] = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", False, None, None),
    ("power", "Power State (Raw)", "mdi:power", False, None, None),
    ("units", "Units", "mdi:ruler", False, None, None),
    (
        "min_temp_unoccupied",
        "Min Temp Unoccupied",
        "mdi:thermometer-low",
        False,
        None,
        None,
    ),
    (
        "max_temp_unoccupied",
        "Max Temp Unoccupied",
        "mdi:thermometer-high",
        False,
        None,
        None,
    ),
    (
        "min_limit_cold",
        "Min Limit Cool",
        "mdi:thermometer-chevron-down",
        False,
        None,
        None,
    ),
    (
        "max_limit_cold",
        "Max Limit Cool",
        "mdi:thermometer-chevron-up",
        False,
        None,
        None,
    ),
    (
        "min_limit_heat",
        "Min Limit Heat",
        "mdi:thermometer-chevron-down",
        False,
        None,
        None,
    ),
    (
        "max_limit_heat",
        "Max Limit Heat",
        "mdi:thermometer-chevron-up",
        False,
        None,
        None,
    ),
]


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzoneSensor] = []
    for device_id in list(coordinator.data.keys()):
        for spec in CORE_SENSORS + DIAG_SENSORS:
            entities.append(AirzoneSensor(coordinator, device_id, *spec))

    async_add_entities(entities)


class AirzoneSensor(CoordinatorEntity, SensorEntity):
    """Generic read-only sensor surfacing fields from device snapshot."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        device_id: str,
        attribute: str,
        friendly: str,
        icon: str,
        enabled_by_default: bool,
        dev_class: str | None,
        state_class: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attribute = attribute
        self._attr_name = friendly
        self._attr_icon = icon
        self._attr_unique_id = f"{device_id}_{attribute}"
        self._attr_entity_category = (
            EntityCategory.DIAGNOSTIC if (attribute not in ("local_temp",)) else None
        )
        self._attr_should_poll = False
        self._attr_native_unit_of_measurement = (
            UnitOfTemperature.CELSIUS if attribute in ("local_temp",) else None
        )
        # Device & state classes
        if dev_class:
            try:
                self._attr_device_class = getattr(SensorDeviceClass, dev_class.upper())
            except Exception:
                self._attr_device_class = None
        if state_class:
            try:
                self._attr_state_class = getattr(SensorStateClass, state_class.upper())
            except Exception:
                self._attr_state_class = None
        # Default visibility
        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def device_info(self):
        dev = self._device
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "manufacturer": "Daikin / Airzone",
            "model": dev.get("brand") or "Airzone DKN",
            "sw_version": dev.get("firmware") or "",
            "name": dev.get("name") or "Airzone Device",
        }

    @property
    def _device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._device_id, {})

    @property
    def available(self) -> bool:
        return bool(self._device)

    @property
    def native_value(self) -> Any:
        val = self._device.get(self._attribute)
        # NOTE: Fix - parse temperature/setpoints as float to avoid ValueError on "23.5"
        # and return a numeric value instead of None/unknown.
        if self._attribute in ("local_temp", "cold_consign", "heat_consign"):
            try:
                # Accept both "23,5" and "23.5"
                return float(str(val).replace(",", ".")) if val is not None else None
            except Exception:
                return None
        return val
