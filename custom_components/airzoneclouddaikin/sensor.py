"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform from a config entry using the DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data.get("coordinator")
    sensors = []
    # Create a sensor entity for each device in the coordinator data
    for device_id, device in coordinator.data.items():
        sensors.append(AirzoneTemperatureSensor(coordinator, device))
    async_add_entities(sensors, True)

class AirzoneTemperatureSensor(SensorEntity):
    """Representation of a temperature sensor for an Airzone device (local_temp)."""

    def __init__(self, coordinator, device_data: dict):
        """Initialize the sensor entity using device data.

        :param coordinator: The DataUpdateCoordinator instance.
        :param device_data: Dictionary with device information.
        """
        self.coordinator = coordinator
        self._device_data = device_data
        # Construct sensor name: "<Device Name> Temperature"
        name = f"{device_data.get('name', 'Airzone Device')} Temperature"
        self._attr_name = name
        # Set unique_id using the device id plus a suffix
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
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
        }

    async def async_update(self):
        """Update the sensor state from the coordinator data.
        
        This method requests a refresh of the coordinator data and updates the sensor state.
        """
        await self.coordinator.async_request_refresh()
        # Retrieve the updated device data using its id
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
        self.update_state()
        self.async_write_ha_state()

    def update_state(self):
        """Update the native value from device data."""
        try:
            self._attr_native_value = float(self._device_data.get("local_temp"))
        except (ValueError, TypeError):
            self._attr_native_value = None
