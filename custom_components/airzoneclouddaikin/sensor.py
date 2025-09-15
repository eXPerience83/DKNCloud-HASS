"""Sensor platform for DKN Cloud for HASS (Airzone Cloud).

Key improvements:
- Use CoordinatorEntity to avoid I/O in properties; entities read from coordinator snapshot.
- Remove PIN from device_info and do not expose a PIN sensor (privacy hardening).
- Do not call async_request_refresh() from every entity; the coordinator owns the update cycle.
- Keep IDs stable (<device_id>_<suffix>) and set correct device/state classes where applicable.

Fix in this version:
- Diagnostic sensors now rebuild their name cleanly when the backend device name changes,
  avoiding duplicated prefixes like "Livingroom Living Room Programs Enabled".
"""

from __future__ import annotations

import hashlib
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

# Diagnostic attributes to surface as sensors (PIN intentionally removed for privacy)
# (attribute, friendly name, icon, enabled_by_default)
DIAGNOSTIC_ATTRIBUTES: list[tuple[str, str, str, bool]] = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", True),
    ("modes", "Supported Modes (Bitmask)", "mdi:toggle-switch", True),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand", True),
    ("scenary", "Scenary", "mdi:bed", True),
    ("min_temp_unoccupied", "Min Temperature Unoccupied", "mdi:thermometer-low", True),
    ("max_temp_unoccupied", "Max Temperature Unoccupied", "mdi:thermometer-high", True),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True),
    ("firmware", "Firmware Version", "mdi:chip", True),
    ("brand", "Brand/Model", "mdi:factory", True),
    # ("pin", "Device PIN", "mdi:numeric", False),  # Do not expose for privacy.
    ("power", "Power State (Raw)", "mdi:power", True),
    ("units", "Units", "mdi:ruler", False),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True),
    ("cold_consign", "Cool Setpoint", "mdi:snowflake", True),
    ("heat_consign", "Heat Setpoint", "mdi:fire", True),
    ("cold_speed", "Cool Fan Speed", "mdi:fan", True),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True),
    ("update_date", "Last Update", "mdi:update", False),
    ("mode", "Current Mode (Raw)", "mdi:tag", True),
    # Slats (disabled by default)
    ("ver_state_slats", "Vertical Slat State", "mdi:swap-vertical", False),
    ("ver_position_slats", "Vertical Slat Position", "mdi:swap-vertical", False),
    ("hor_state_slats", "Horizontal Slat State", "mdi:swap-horizontal", False),
    ("hor_position_slats", "Horizontal Slat Position", "mdi:swap-horizontal", False),
    ("ver_cold_slats", "Vertical Cold Slats", "mdi:swap-vertical", False),
    ("ver_heat_slats", "Vertical Heat Slats", "mdi:swap-vertical", False),
    ("hor_cold_slats", "Horizontal Cold Slats", "mdi:swap-horizontal", False),
    ("hor_heat_slats", "Horizontal Heat Slats", "mdi:swap-horizontal", False),
    # Temperature limits (diagnostics)
    ("max_limit_cold", "Max Limit Cool", "mdi:thermometer-high", True),
    ("min_limit_cold", "Min Limit Cool", "mdi:thermometer-low", True),
    ("max_limit_heat", "Max Limit Heat", "mdi:thermometer-high", True),
    ("min_limit_heat", "Min Limit Heat", "mdi:thermometer-low", True),
    # Advanced diagnostics (mostly for debug)
    ("state", "State (Raw)", "mdi:eye", False),
    ("status", "Status", "mdi:check-circle", False),
    ("connection_date", "Connection Date", "mdi:clock", False),
    ("last_event_id", "Last Event ID", "mdi:identifier", False),
]


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up sensors from a config entry using the shared DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    sensors: list[SensorEntity] = []
    for device_id in list(coordinator.data.keys()):
        sensors.append(AirzoneTemperatureSensor(coordinator, device_id))
        for attr, name, icon, enabled_default in DIAGNOSTIC_ATTRIBUTES:
            sensors.append(
                AirzoneDiagnosticSensor(
                    coordinator,
                    device_id,
                    attribute=attr,
                    friendly_name=name,
                    icon=icon,
                    enabled_default=enabled_default,
                )
            )

    # Entities read from coordinator snapshot; update_before_add not needed.
    async_add_entities(sensors)


# -----------------------------------------------------------------------------
# Temperature Sensor (local_temp)
# -----------------------------------------------------------------------------
class AirzoneTemperatureSensor(CoordinatorEntity, SensorEntity):
    """Main temperature sensor for an Airzone device (local_temp)."""

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

        name = f"{self._device.get('name', 'Airzone Device')} Temperature"
        self._attr_name = name

        if self._device_id:
            self._attr_unique_id = f"{self._device_id}_temperature"
        else:
            # Fallback: stable hash if backend omits id (should not happen)
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()

        # Proper typing for temperature sensors
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:thermometer"

    # ---- Helpers -------------------------------------------------------------
    @property
    def _device(self) -> dict[str, Any]:
        """Return live device snapshot from coordinator."""
        return self.coordinator.data.get(self._device_id, {})

    def _read_float(self, key: str) -> float | None:
        try:
            return float(self._device.get(key))
        except (TypeError, ValueError):
            return None

    # ---- Coordinator hook ----------------------------------------------------
    def _handle_coordinator_update(self) -> None:
        """Called when the coordinator updates data."""
        # Update name in case it changed remotely
        self._attr_name = f"{self._device.get('name', 'Airzone Device')} Temperature"
        self.async_write_ha_state()

    # ---- Entity properties ---------------------------------------------------
    @property
    def available(self) -> bool:
        """Entity is available if the device exists and has an id."""
        return bool(self._device and self._device.get("id"))

    @property
    def native_value(self) -> float | None:
        """Return the current temperature reading."""
        return self._read_float("local_temp")

    @property
    def suggested_display_precision(self) -> int:
        """Display temperature as integer (e.g., 22 °C)."""
        return 0

    @property
    def device_info(self) -> dict[str, Any]:
        """Link this sensor to the device registry (without exposing the PIN)."""
        dev = self._device
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dev.get("id"))},
            "name": dev.get("name"),
            "manufacturer": "Daikin",
            # Privacy: do not include PIN in model string.
            "model": dev.get("brand") or "Unknown",
            "sw_version": dev.get("firmware") or "Unknown",
        }
        mac = dev.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info


# -----------------------------------------------------------------------------
# Diagnostic Sensors
# -----------------------------------------------------------------------------
class AirzoneDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor for an Airzone device."""

    def __init__(
        self,
        coordinator,
        device_id: str,
        *,
        attribute: str,
        friendly_name: str,
        icon: str,
        enabled_default: bool,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attribute = attribute

        # Keep a stable, friendly suffix to rebuild the title on updates.
        self._friendly_name = friendly_name

        base_name = self._device.get("name", "Airzone Device")
        self._attr_name = f"{base_name} {self._friendly_name}"
        self._attr_icon = icon
        self._attr_unique_id = (
            f"{device_id}_{attribute}"
            if device_id
            else hashlib.sha256(f"{base_name}:{attribute}".encode()).hexdigest()
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = enabled_default

        # Temperature-like attributes (°C)
        temp_attrs = {
            "cold_consign",
            "heat_consign",
            "min_temp_unoccupied",
            "max_temp_unoccupied",
            "min_limit_cold",
            "max_limit_cold",
            "min_limit_heat",
            "max_limit_heat",
        }
        if self._attribute in temp_attrs:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT

        # Numeric counters/levels where "measurement" makes sense
        if self._attribute in {"cold_speed", "heat_speed", "availables_speeds"}:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    # ---- Helpers -------------------------------------------------------------
    @property
    def _device(self) -> dict[str, Any]:
        """Return live device snapshot from coordinator."""
        return self.coordinator.data.get(self._device_id, {})

    def _read_float(self, key: str) -> float | None:
        try:
            return float(self._device.get(key))
        except (TypeError, ValueError):
            return None

    def _read_int(self, key: str) -> int | None:
        try:
            return int(self._device.get(key))
        except (TypeError, ValueError):
            return None

    # ---- Coordinator hook ----------------------------------------------------
    def _handle_coordinator_update(self) -> None:
        """Called when the coordinator updates data.

        Rebuild the full entity name using the current device name and the
        stored friendly suffix, so we never duplicate prefixes.
        """
        base_name = self._device.get("name", "Airzone Device")
        self._attr_name = f"{base_name} {self._friendly_name}"
        self.async_write_ha_state()

    # ---- Entity properties ---------------------------------------------------
    @property
    def available(self) -> bool:
        """Entity is available if the device exists and has an id."""
        return bool(self._device and self._device.get("id"))

    @property
    def native_value(self):
        """Return the value of the diagnostic attribute, formatted for UI."""
        dev = self._device
        value = dev.get(self._attribute)

        # Friendly conversions
        if self._attribute == "progs_enabled":
            return bool(value)

        if self._attribute == "machine_errors":
            if value in (None, "", [], {}):
                return "No errors"
            return str(value)

        if self._attribute in {
            "sleep_time",
            "min_temp_unoccupied",
            "max_temp_unoccupied",
            "max_limit_cold",
            "min_limit_cold",
            "max_limit_heat",
            "min_limit_heat",
            "cold_consign",
            "heat_consign",
        }:
            return self._read_float(self._attribute)

        if self._attribute in {
            "cold_speed",
            "heat_speed",
            "availables_speeds",
            "ver_state_slats",
            "ver_position_slats",
            "hor_state_slats",
            "hor_position_slats",
        }:
            return self._read_int(self._attribute)

        # Fallback: return raw value
        return value

    @property
    def device_info(self) -> dict[str, Any]:
        """Link this diagnostic sensor to the device registry (no PIN exposure)."""
        dev = self._device
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dev.get("id"))},
            "name": dev.get("name"),
            "manufacturer": "Daikin",
            # Privacy: do not include PIN in model string.
            "model": dev.get("brand") or "Unknown",
            "sw_version": dev.get("firmware") or "Unknown",
        }
        mac = dev.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info
