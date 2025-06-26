"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
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
    # Slat diagnostics (optional)
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
    """
    Set up sensor platform: one temperature sensor + diagnostics per device.
    """
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No coordinator data for entry %s", entry.entry_id)
        return
    coordinator = data["coordinator"]
    entities = []

    for device in coordinator.data.values():
        # temperature sensor
        entities.append(AirzoneTemperatureSensor(coordinator, device))
        # diagnostic sensors
        for attr, name, icon in DIAGNOSTIC_ATTRIBUTES:
            entities.append(AirzoneDiagnosticSensor(coordinator, device, attr, name, icon))

    async_add_entities(entities, True)


class AirzoneTemperatureSensor(SensorEntity):
    """Temperature sensor for an Airzone device (local_temp)."""

    def __init__(self, coordinator, device_data: dict):
        """
        :param coordinator: DataUpdateCoordinator instance
        :param device_data: raw device dict
        """
        self.coordinator = coordinator
        self._device_data = device_data
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"

        name = f"{device_data.get('name')} Temperature"
        # unique id = "<device_id>_temperature"
        unique_id = f"{device_data['id']}_temperature"
        self.async_set_unique_id(unique_id)
        self._attr_name = name

        self.update_state()

    @property
    def native_value(self):
        """Return current temperature value."""
        return self._attr_native_value

    @property
    def accuracy_decimals(self):
        """Force integer display."""
        return 0

    @property
    def device_info(self):
        """Link to HA device registry."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware"),
            "connections": {("mac", self._device_data.get("mac"))},
        }

    async def async_update(self):
        """Refresh data from coordinator and update state."""
        await self.coordinator.async_request_refresh()
        self._device_data = self.coordinator.data.get(self._device_data["id"], self._device_data)
        self.update_state()

    def update_state(self):
        """Parse and assign local_temp."""
        try:
            self._attr_native_value = float(self._device_data.get("local_temp", 0))
        except (ValueError, TypeError):
            self._attr_native_value = None


class AirzoneDiagnosticSensor(SensorEntity):
    """Diagnostic sensor exposing arbitrary device attributes."""

    def __init__(self, coordinator, device_data: dict, attribute: str, name: str, icon: str):
        """
        :param coordinator: DataUpdateCoordinator
        :param device_data: raw device dict
        :param attribute: key in device_data
        :param name: friendly name suffix
        :param icon: mdi icon string
        """
        self.coordinator = coordinator
        self._device_data = device_data
        self._attribute = attribute

        friendly_name = f"{device_data.get('name')} {name}"
        unique_id = f"{device_data['id']}_{attribute}"
        self.async_set_unique_id(unique_id)

        self._attr_name = friendly_name
        self._attr_icon = icon
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        """Return the raw attribute, casting common types."""
        val = self._device_data.get(self._attribute)
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
        """Link back to the main device entry."""
        return {
            "identifiers": {(DOMAIN, self.unique_id.split("_")[0])},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware"),
            "connections": {("mac", self._device_data.get("mac"))},
        }

    async def async_update(self):
        """Refresh raw device_data from coordinator."""
        await self.coordinator.async_request_refresh()
        self._device_data = self.coordinator.data.get(self._device_data["id"], self._device_data)
