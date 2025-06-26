"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# List of diagnostic attributes enabled by default (shown to users)
ENABLED_DIAGNOSTICS = [
    "progs_enabled",
    "modes",
    "sleep_time",
    "scenary",
    "min_temp_unoccupied",
    "max_temp_unoccupied",
    "machine_errors",
    "firmware",
    "brand",
    "pin",
    "mode"
]
# List of diagnostic attributes disabled by default (advanced/rarely needed)
DISABLED_DIAGNOSTICS = [
    "update_date",
    "ver_state_slats",
    "ver_position_slats",
    "hor_state_slats",
    "hor_position_slats",
    "ver_cold_slats",
    "ver_heat_slats",
    "hor_cold_slats",
    "hor_heat_slats"
]

# Full list of all diagnostic attributes to be exposed as sensors (with icons and friendly names)
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
    ("update_date", "Last Update", "mdi:update"),
    ("mode", "Current Mode (Raw)", "mdi:tag"),
    # Slats fields:
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
    # Create a temperature sensor and all diagnostic sensors for each device in the coordinator data.
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
    Only selected sensors are enabled by default; others must be enabled manually by the user.
    """
    def __init__(self, coordinator, device_data: dict, attribute: str, name: str, icon: str):
        self.coordinator = coordinator
        self._device_data = device_data
        self._attribute = attribute
        self._attr_name = f"{device_data.get('name', 'Airzone Device')} {name}"
        self._attr_icon = icon
        self._attr_unique_id = f"{device_data.get('id')}_{attribute}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # Enable or disable sensor by default depending on your selection
        if attribute in DISABLED_DIAGNOSTICS:
            self._attr_entity_registry_enabled_default = False
        else:
            self._attr_entity_registry_enabled_default = True

    @property
    def native_value(self):
        """Return the value of the diagnostic attribute, formatted for display."""
        value = self._device_data.get(self._attribute)
        # Custom conversions for a more readable UI
        if self._attribute == "progs_enabled":
            return bool(value)
        if self._attribute in ("sleep_time", "min_temp_unoccupied", "max_temp_unoccupied"):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None
        if self._attribute in (
            "ver_state_slats", "ver_position_slats",
            "hor_state_slats", "hor_position_slats",
        ):
            try:
                return int(value)
            except (TypeError, ValueError):
                return value  # return as-is if not convertible
        # For other fields, return value as-is
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
