"""Sensor platform for DKN Cloud for HASS."""
import hashlib
import logging
from homeassistant.helpers.update_coordinator import CoordinatorEntity, callback
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
    ("min_temp_unoccupied", "Min Temperature Unoccupied", "mdi:thermometer-low", True),
    ("max_temp_unoccupied", "Max Temperature Unoccupied", "mdi:thermometer-high", True),
    ("machine_errors", "Machine Errors", "mdi:alert-octagon", True),
    ("firmware", "Firmware Version", "mdi:chip", True),
    ("brand", "Brand/Model", "mdi:factory", True),
    ("pin", "Device PIN", "mdi:numeric", True),
    ("power", "Power State (Raw)", "mdi:power", True),
    ("units", "Units", "mdi:ruler", False),
    ("availables_speeds", "Available Fan Speeds", "mdi:fan", True),
    ("cold_consign", "Cool Setpoint", "mdi:snowflake", True),
    ("heat_consign", "Heat Setpoint", "mdi:fire", True),
    ("cold_speed", "Cool Fan Speed", "mdi:fan", True),
    ("heat_speed", "Heat Fan Speed", "mdi:fan", True),
    ("update_date", "Last Update", "mdi:update", False),
    ("mode", "Current Mode (Raw)", "mdi:tag", True),
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
    ("max_limit_cold", "Max Limit Cool", "mdi:thermometer-high", True),
    ("min_limit_cold", "Min Limit Cool", "mdi:thermometer-low", True),
    ("max_limit_heat", "Max Limit Heat", "mdi:thermometer-high", True),
    ("min_limit_heat", "Min Limit Heat", "mdi:thermometer-low", True),
    # Advanced diagnostics, mostly for debug
    ("state", "State (Raw)", "mdi:eye", False),
    ("status", "Status", "mdi:check-circle", False),
    ("connection_date", "Connection Date", "mdi:clock", False),
    ("last_event_id", "Last Event ID", "mdi:identifier", False),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """
    Set up the sensor platform from a config entry using the DataUpdateCoordinator.
    For each device, creates a main temperature sensor (local_temp) and a set of diagnostic sensors.
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
            if attr == "local_temp":
                continue
            sensors.append(
                AirzoneDiagnosticSensor(coordinator, device, attr, name, icon, enabled_default)
            )
    async_add_entities(sensors, True)


class AirzoneTemperatureSensor(CoordinatorEntity, SensorEntity):
    """Main temperature sensor for Airzone device (local_temp)."""

    def __init__(self, coordinator, device_data: dict):
        super().__init__(coordinator)
        self._device_id = device_data.get("id")
        name = f"{device_data.get('name', 'Airzone Device')} Temperature"
        self._attr_name = name
        if self._device_id and self._device_id.strip():
            self._attr_unique_id = f"{self._device_id}_temperature"
        else:
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_icon = "mdi:thermometer"
        self._attr_device_info = None

    @property
    def _device_data(self):
        """Get latest device data from the coordinator."""
        return self.coordinator.data.get(self._device_id, {})

    @property
    def native_value(self):
        """Return the current temperature reading."""
        try:
            return float(self._device_data.get("local_temp"))
        except (ValueError, TypeError):
            return None

    @property
    def accuracy_decimals(self):
        """Return 0 to display the state as an integer (e.g., 22°C)."""
        return 0

    @property
    def device_info(self):
        """Return device info for linking this sensor to a device in Home Assistant."""
        data = self._device_data
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{data.get('brand', 'Unknown')} (PIN: {data.get('pin')})",
            "sw_version": data.get("firmware", "Unknown"),
            "connections": {("mac", data.get("mac"))} if data.get("mac") else None,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update state when the coordinator has new data."""
        self.async_write_ha_state()


class AirzoneDiagnosticSensor(CoordinatorEntity, SensorEntity):
    """
    Diagnostic sensor for Airzone device.
    These sensors are entity_category=DIAGNOSTIC and can be disabled by default if not essential.
    """

    def __init__(self, coordinator, device_data: dict, attribute: str, name: str, icon: str, enabled_default: bool):
        super().__init__(coordinator)
        self._device_id = device_data.get("id")
        self._attribute = attribute
        self._attr_name = f"{device_data.get('name', 'Airzone Device')} {name}"
        self._attr_icon = icon
        self._attr_unique_id = f"{self._device_id}_{attribute}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_entity_registry_enabled_default = enabled_default
        self._attr_device_info = None

        temp_sensors = (
            "cold_consign", "heat_consign",
            "min_temp_unoccupied", "max_temp_unoccupied",
            "min_limit_cold", "max_limit_cold",
            "min_limit_heat", "max_limit_heat"
        )
        if self._attribute in temp_sensors:
            self._attr_device_class = "temperature"
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = "measurement"
        if self._attribute in ("cold_speed", "heat_speed", "availables_speeds"):
            self._attr_state_class = "measurement"

    @property
    def _device_data(self):
        """Get latest device data from the coordinator."""
        return self.coordinator.data.get(self._device_id, {})

    @property
    def native_value(self):
        """Return the value of the diagnostic attribute, formatted for display."""
        value = self._device_data.get(self._attribute)
        if self._attribute == "progs_enabled":
            return bool(value)
        if self._attribute == "machine_errors":
            if value in (None, "", [], {}):
                return "No errors"
            return str(value)
        if self._attribute in (
            "sleep_time", "min_temp_unoccupied", "max_temp_unoccupied",
            "max_limit_cold", "min_limit_cold", "max_limit_heat", "min_limit_heat",
            "cold_consign", "heat_consign"
        ):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        if self._attribute in (
            "cold_speed", "heat_speed", "availables_speeds",
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
        """Return device info for linking this sensor to a device in Home Assistant."""
        data = self._device_data
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{data.get('brand', 'Unknown')} (PIN: {data.get('pin')})",
            "sw_version": data.get("firmware", "Unknown"),
            "connections": {("mac", data.get("mac"))} if data.get("mac") else None,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update state when the coordinator has new data."""
        self.async_write_ha_state()
