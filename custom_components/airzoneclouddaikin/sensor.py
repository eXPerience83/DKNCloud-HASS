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
]

async def async_setup_entry(hass, entry, async_add_entities):
    """
    Set up the sensor platform from a config entry using the DataUpdateCoordinator.
    This function retrieves the coordinator from hass.data and creates both a temperature sensor
    and diagnostic sensors for each device.
    """
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data.get("coordinator")
    sensors = []
    # Create a temperature sensor and diagnostic sensors for each device in the coordinator data.
    for device_id, device in coordinator.data.items():
        sensors.append(AirzoneTemperatureSensor(coordinator, device))
        for attr, name, icon in DIAGNOSTIC_ATTRIBUTES:
            sensors.append(
                AirzoneDiagnosticSensor(coordinator, device, attr, name, icon)
            )
    async_add_entities(sensors, True)

class AirzoneTemperatureSensor(SensorEntity):
    """Representation of a temperature sensor for an Airzone device (local_temp)."""
    def __init__(self, coordinator, device_data: dict):
        """
        Initialize the sensor entity using device data.
        :param coordinator: The DataUpdateCoordinator instance.
        :param device_data: Dictionary with device information.
        """
        self.coordinator = coordinator
        self._device_data = device_data
        # Construct sensor name: "<Device Name> Temperature"
        name = f"{device_data.get('name', 'Airzone Device')} Temperature"
        self._attr_name = name
        device_id = device_data.get("id")
        if device_id and device_id.strip():
            self._attr_unique_id = f"{device_id}_temperature"
        else:
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"
        self.update_state()

    @property
    def native_value(self):
        """Return the current temperature reading."""
        return self._attr_native_value

    @property
    def accuracy_decimals(self):
        """Return 0 to display the state as an integer (e.g., 22°C)."""
        return 0

    @property
    def device_info(self):
        """Return device info to link this sensor to a device in Home Assistant.""" 
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand', 'Unknown')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "connections": {("mac", self._device_data.get("mac"))} if self._device_data.get("mac") else None,
        }

    async def async_update(self):
        """
        Update the sensor state from the coordinator data.
        This method requests a refresh of the coordinator data and then updates
        the sensor state using the latest device data.
        """
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
        self.update_state()

    def update_state(self):
        """Update the native value from device data."""
        try:
            self._attr_native_value = float(self._device_data.get("local_temp"))
        except (ValueError, TypeError):
            self._attr_native_value = None

class AirzoneDiagnosticSensor(SensorEntity):
    """
    Representation of a diagnostic sensor for an Airzone device.
    Displays additional device attributes as separate sensor entities with DIAGNOSTIC category.
    """
    def __init__(self, coordinator, device_data: dict, attribute: str, name: str, icon: str):
        self.coordinator = coordinator
        self._device_data = device_data
        self._attribute = attribute
        self._attr_name = f"{device_data.get('name', 'Airzone Device')} {name}"
        self._attr_icon = icon
        self._attr_unique_id = f"{device_data.get('id')}_{attribute}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        """Return the value of the diagnostic attribute."""
        value = self._device_data.get(self._attribute)
        # Some fields are strings that should be int/bool for better representation
        if self._attribute == "progs_enabled":
            return bool(value)
        if self._attribute in ("sleep_time", "min_temp_unoccupied", "max_temp_unoccupied"):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
        return value

    @property
    def device_info(self):
        """Return device info to link this sensor to a device in Home Assistant.""" 
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand', 'Unknown')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "connections": {("mac", self._device_data.get("mac"))} if self._device_data.get("mac") else None,
        }

    async def async_update(self):
        """Update the diagnostic sensor state from the coordinator data."""
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
