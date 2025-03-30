"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform from a config entry using the DataUpdateCoordinator.

    This function retrieves the coordinator from hass.data and creates a sensor entity
    for each device using the coordinator's data.
    """
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data.get("coordinator")
    sensors = []
    # Iterate over each device in the coordinator's data (keyed by device id)
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

        # Use the device 'id' to form a unique id; if missing, fallback to a hash of the name.
        device_id = device_data.get("id")
        if device_id and device_id.strip():
            self._attr_unique_id = f"{device_id}_temperature"
        else:
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()

        # Set the unit attributes explicitly for HA statistics and display.
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

        # Set device class and state class for statistics
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"
        # Initialize sensor state
        self.update_state()

    @property
    def unique_id(self):
        """Return the unique id for this sensor."""
        return self._attr_unique_id

    @property
    def native_value(self):
        """Return the current temperature reading."""
        return self._attr_native_value

    @property
    def accuracy_decimals(self):
        """Return the number of decimals to use for the sensor state (display as integer)."""
        return 0

    @property
    def device_info(self):
        """Return device info to link this sensor to a device in Home Assistant."""
        return {
            "identifiers": { (DOMAIN, self._device_data.get("id")) },
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
        }

    async def async_update(self):
        """Update the sensor state from the coordinator data.

        This method requests a refresh of the coordinator data and then updates
        the sensor state using the latest device data.
        """
        await self.coordinator.async_request_refresh()
        # Retrieve the updated device data using the device's unique id
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
        self.update_state()
        self.async_schedule_update_ha_state()

    def update_state(self):
        """Update the native value from device data."""
        try:
            self._attr_native_value = float(self._device_data.get("local_temp"))
        except (ValueError, TypeError):
            self._attr_native_value = None
