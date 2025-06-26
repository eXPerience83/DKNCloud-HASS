"""Sensor platform for DKN Cloud for HASS."""

import logging
import hashlib

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DIAGNOSTIC_ATTRIBUTES = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check"),
    ("modes", "Supported Modes (Bitmask)", "mdi:toggle-switch"),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand"),
    ("scenary", "Scenary", "mdi:bed"),
    ("min_temp_unoccupied", "Min Temp Unoccupied", "mdi:thermometer-low"),
    ("max_temp_unoccupied", "Max Temp Unoccupied", "mdi:thermometer-high"),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon"),
    ("firmware", "Firmware Version", "mdi:chip"),
    ("brand", "Brand/Model", "mdi:factory"),
    ("pin", "Device PIN", "mdi:numeric"),
    ("mode", "Current Mode (Raw)", "mdi:tag"),
    # Slat fields (optional)
    ("ver_state_slats", "Vertical Slat State", "mdi:swap-vertical"),
    ("ver_position_slats", "Vertical Slat Position", "mdi:swap-vertical"),
    ("hor_state_slats", "Horizontal Slat State", "mdi:swap-horizontal"),
    ("hor_position_slats", "Horizontal Slat Position", "mdi:swap-horizontal"),
    ("ver_cold_slats", "Vertical Cold Slats", "mdi:swap-vertical"),
    ("ver_heat_slats", "Vertical Heat Slats", "mdi:swap-vertical"),
    ("hor_cold_slats", "Horizontal Cold Slats", "mdi:swap-horizontal"),
    ("hor_heat_slats", "Horizontal Heat Slats", "mdi:swap-horizontal"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data for entry %s", entry.entry_id)
        return
    coordinator = data["coordinator"]
    entities = []

    for device_id, device in coordinator.data.items():
        # Temperature sensor
        entities.append(AirzoneTemperatureSensor(coordinator, device, device_id))
        # Diagnostic sensors
        for attr, name, icon in DIAGNOSTIC_ATTRIBUTES:
            entities.append(
                AirzoneDiagnosticSensor(coordinator, device, device_id, attr, name, icon)
            )

    async_add_entities(entities, True)


class AirzoneTemperatureSensor(SensorEntity):
    """Temperature sensor for local_temp."""

    def __init__(self, coordinator, device, device_id):
        """Initialize."""
        self.coordinator = coordinator
        self._device = device
        self._device_id = device_id

        self._attr_name = f"{device.get('name')} Temperature"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"

        self._attr_unique_id = f"{device_id}_temperature"
        self.update_state()

    @property
    def native_value(self):
        """Return current temperature."""
        return self._attr_native_value

    @property
    def accuracy_decimals(self):
        """Zero decimals."""
        return 0

    @property
    def device_info(self):
        """Link to device registry."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_update(self):
        """Refresh data."""
        await self.coordinator.async_request_refresh()
        self._device = self.coordinator.data.get(self._device_id, self._device)
        self.update_state()

    def update_state(self):
        """Update internal state."""
        try:
            self._attr_native_value = float(self._device.get("local_temp", 0))
        except (TypeError, ValueError):
            self._attr_native_value = None


class AirzoneDiagnosticSensor(SensorEntity):
    """Generic diagnostic sensor exposing raw attributes."""

    def __init__(self, coordinator, device, device_id, attribute, name, icon):
        """Initialize."""
        self.coordinator = coordinator
        self._device = device
        self._device_id = device_id
        self._attribute = attribute

        self._attr_name = f"{device.get('name')} {name}"
        self._attr_icon = icon
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = f"{device_id}_{attribute}"

    @property
    def native_value(self):
        """Return raw value or cast for known types."""
        val = self._device.get(self._attribute)
        if self._attribute == "progs_enabled":
            return bool(val)
        if self._attribute in ("sleep_time", "min_temp_unoccupied", "max_temp_unoccupied"):
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
        return val

    @property
    def device_info(self):
        """Link to main device."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_update(self):
        """Refresh device data."""
        await self.coordinator.async_request_refresh()
        self._device = self.coordinator.data.get(self._device_id, self._device)
