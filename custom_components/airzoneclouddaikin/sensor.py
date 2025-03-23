"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform from a config entry."""
    config = entry.data
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    session = async_get_clientsession(hass)
    from .airzone_api import AirzoneAPI
    api = AirzoneAPI(config.get("username"), config.get("password"), session)
    if not await api.login():
        _LOGGER.error("Login to Airzone API failed in sensor setup.")
        return
    installations = await api.fetch_installations()
    sensors = []
    for relation in installations:
        installation = relation.get("installation")
        if not installation:
            continue
        installation_id = installation.get("id")
        if not installation_id:
            continue
        devices = await api.fetch_devices(installation_id)
        for device in devices:
            sensors.append(AirzoneTemperatureSensor(api, device))
    async_add_entities(sensors)  # Let HA assign entity_id after adding entities.

class AirzoneTemperatureSensor(SensorEntity):
    """Representation of a temperature sensor for an Airzone device (local_temp)."""

    def __init__(self, api, device_data: dict):
        """Initialize the sensor entity using device data.
        
        :param api: The AirzoneAPI instance to use for updates.
        :param device_data: Dictionary with device information.
        """
        self._api = api
        self._device_data = device_data
        # Construct sensor name: "<Device Name> Temperature"
        name = f"{device_data.get('name', 'Airzone Device')} Temperature"
        self._attr_name = name

        # Use the device 'id' to form a unique id; fallback to a hash of the name if not available.
        device_id = device_data.get("id")
        if device_id and device_id.strip():
            self._attr_unique_id = f"{device_id}_temperature"
        else:
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()

        # Set the unit attributes explicitly for Home Assistant statistics.
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"
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
    def device_info(self):
        """Return device info to link this sensor to a device in Home Assistant."""
        return {
            "identifiers": {("airzoneclouddaikin", self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
        }

    async def async_update(self):
        """Update the sensor state from the API.
        
        This method calls the API (via self._api) to fetch the latest device data
        for the sensor's device (using its installation_id and id), updates the internal
        device data, and then updates the sensor state.
        """
        installation_id = self._device_data.get("installation_id")
        device_id = self._device_data.get("id")
        if not installation_id or not device_id:
            _LOGGER.error("Missing installation_id or device id in sensor update.")
            return

        # Fetch the latest device data for the given installation.
        devices = await self._api.fetch_devices(installation_id)
        for dev in devices:
            if dev.get("id") == device_id:
                self._device_data = dev
                break

        self.update_state()
        self.async_write_ha_state()

    def update_state(self):
        """Update the native value from device data."""
        try:
            self._attr_native_value = float(self._device_data.get("local_temp"))
        except (ValueError, TypeError):
            self._attr_native_value = None
