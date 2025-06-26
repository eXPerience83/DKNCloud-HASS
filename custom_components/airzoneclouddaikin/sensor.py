"""Sensor platform for DKN Cloud for HASS."""
import logging
import hashlib

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Attributes to expose as diagnostic sensors
DIAGNOSTICS = [
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", True),
    ("modes", "Supported Modes", "mdi:toggle-switch", True),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand", True),
    ("scenary", "Scenary", "mdi:bed", True),
    ("min_temp_unoccupied", "Min Temp Unoccupied", "mdi:thermometer-low", True),
    ("max_temp_unoccupied", "Max Temp Unoccupied", "mdi:thermometer-high", True),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True),
    ("firmware", "Firmware Version", "mdi:chip", True),
    ("brand", "Brand/Model", "mdi:factory", True),
    ("pin", "Device PIN", "mdi:numeric", True),
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
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up temperature + diagnostics sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities = []
    for dev in coordinator.data.values():
        # Temperature probe
        entities.append(AirzoneTemperatureSensor(coordinator, dev))
        # Diagnostic sensors
        for attr, name, icon, enabled in DIAGNOSTICS:
            entities.append(AirzoneDiagnosticSensor(
                coordinator, dev, attr, name, icon, enabled
            ))

    async_add_entities(entities, True)


class AirzoneTemperatureSensor(SensorEntity):
    """Temperature sensor for the device."""
    def __init__(self, coordinator, device):
        self.coordinator = coordinator
        self._device = device
        self._attr_name = f"{device['name']} Temperature"
        self._attr_unique_id = f"{device['id']}_temperature"
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"
        self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self):
        return float(self._device.get("local_temp", 0))

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_update(self):
        await self.coordinator.async_request_refresh()
        self._device = self.coordinator.data[self._device["id"]]


class AirzoneDiagnosticSensor(SensorEntity):
    """Generic diagnostic sensor exposing arbitrary device attribute."""
    def __init__(self, coordinator, device, attribute, name, icon, enabled):
        self.coordinator = coordinator
        self._device = device
        self._attribute = attribute
        self._attr_name = f"{device['name']} {name}"
        self._attr_unique_id = f"{device['id']}_{attribute}"
        self._attr_icon = icon
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # default enable/disable
        self._attr_entity_registry_enabled_default = bool(enabled)

    @property
    def native_value(self):
        val = self._device.get(self._attribute)
        # Type conversions
        if self._attribute == "progs_enabled":
            return bool(val)
        if self._attribute in ("sleep_time", "min_temp_unoccupied", "max_temp_unoccupied"):
            try:
                return int(val)
            except:
                return None
        # Slats or other ints
        try:
            return int(val)
        except:
            return val

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_update(self):
        await self.coordinator.async_request_refresh()
        self._device = self.coordinator.data[self._device["id"]]
