"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# List of diagnostic attributes to expose as sensors
DIAGNOSTIC_ATTRIBUTES = [
    # (attribute, friendly name, icon, enabled by default)
    ("progs_enabled", "Programs Enabled", "mdi:calendar-check", True),
    ("modes", "Supported Modes (Bitmask)", "mdi:toggle-switch", True),
    ("sleep_time", "Sleep Timer (min)", "mdi:timer-sand", True),
    ("scenary", "Scenary", "mdi:bed", True),
    ("min_temp_unoccupied", "Min Temp Unoccupied", "mdi:thermometer-low", True),
    ("max_temp_unoccupied", "Max Temp Unoccupied", "mdi:thermometer-high", True),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True),
    ("firmware", "Firmware Version", "mdi:chip", True),
    ("brand", "Brand/Model", "mdi:factory", True),
    ("pin", "Device PIN", "mdi:numeric", True),
    ("power", "Power State (Raw)", "mdi:power", True),
    ("units", "Units", "mdi:ruler", True),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True),
    ("local_temp", "Current Device Temp (Raw)", "mdi:thermometer", True),
    ("cold_consign", "Cold Setpoint (Raw)", "mdi:snowflake", True),
    ("heat_consign", "Heat Setpoint (Raw)", "mdi:fire", True),
    ("cold_speed", "Cold Fan Speed", "mdi:fan", True),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True),
    ("update_date", "Last Update", "mdi:update", False),
    ("mode", "Current Mode (Raw)", "mdi:tag", False),
    # Slats fields, disabled by default
    ("ver_state_slats", "Vertical Slat State", "mdi:swap-vertical", False),
    ("ver_position_slats", "Vertical Slat Position", "mdi:swap-vertical", False),
    ("hor_state_slats", "Horizontal Slat State", "mdi:swap-horizontal", False),
    ("hor_position_slats", "Horizontal Slat Position", "mdi:swap-horizontal", False),
    ("ver_cold_slats", "Vertical Cold Slats", "mdi:swap-vertical", False),
    ("ver_heat_slats", "Vertical Heat Slats", "mdi:swap-vertical", False),
    ("hor_cold_slats", "Horizontal Cold Slats", "mdi:swap-horizontal", False),
    ("hor_heat_slats", "Horizontal Heat Slats", "mdi:swap-horizontal", False),
    # Temperature limits (diagnostics, advanced)
    ("max_limit_cold", "Max Limit Cold", "mdi:thermometer-high", False),
    ("min_limit_cold", "Min Limit Cold", "mdi:thermometer-low", False),
    ("max_limit_heat", "Max Limit Heat", "mdi:thermometer-high", False),
    ("min_limit_heat", "Min Limit Heat", "mdi:thermometer-low", False),
    # Advanced diagnostics, mostly for debug
    ("state", "State (Raw)", "mdi:eye", False),
    ("status", "Status", "mdi:check-circle", False),
    ("connection_date", "Connection Date", "mdi:clock", False),
    ("last_event_id", "Last Event ID", "mdi:identifier", False),
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
    for device_id, device in coordinator.data.items():
        sensors.append(AirzoneTemperatureSensor(coordinator, device))
        for attr, name, icon, enabled_default in DIAGNOSTIC_ATTRIBUTES:
            sensors.append(
                AirzoneDiagnosticSensor(coordinator, device, attr, name, icon, enabled_default)
            )
    async_add_entities(sensors, True)

class AirzoneTemperatureSensor(SensorEntity):
    """Temperature sensor for Airzone device (local_temp)."""
    def __init__(self, coordinator, device_data: dict):
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
    Diagnostic sensor for Airzone device.
    These sensors are entity_category=DIAGNOSTIC and can be disabled by default if not essential.
    """
    def __init__(self, coordinator, device_data: dict, attribute: str, name: str, icon: str, enabled_default: bool):
        self.coordinator = coordinator
        self._device_data = device_data
        self._attribute = attribute
        self._attr_name = f"{device_data.get('name', 'Airzone Device')} {name}"
        self._attr_icon = icon
        self._attr_unique_id = f"{device_data.get('id')}_{attribute}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = enabled_default

    @property
    def native_value(self):
        """Return the value of the diagnostic attribute, formatted for display."""
        value = self._device_data.get(self._attribute)
        # Custom conversions for better UI
        if self._attribute == "progs_enabled":
            return bool(value)
        if self._attribute == "machine_errors":
            if value in (None, "", [], {}):
                return "No errors"
            return str(value)
        if self._attribute in ("sleep_time", "min_temp_unoccupied", "max_temp_unoccupied",
                               "max_limit_cold", "min_limit_cold", "max_limit_heat", "min_limit_heat"):
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
                return value
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
