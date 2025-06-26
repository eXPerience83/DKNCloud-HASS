"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator.

Implements dynamic HVAC mode mapping and exposes both HEAT and COOL setpoints
and fan speeds in HEAT_COOL mode. When setting temperatures or fan speed in HEAT_COOL,
commands are sent to both heat and cool endpoints.
"""
import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the climate platform from a config entry using the DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data["coordinator"]
    api = data["api"]
    config = entry.data
    entities = [
        AirzoneClimate(coordinator, api, device, config, hass)
        for device in coordinator.data.values()
    ]
    async_add_entities(entities, True)


class AirzoneClimate(ClimateEntity):
    """
    Representation of an Airzone Cloud Daikin climate device.

    Supports all HVAC modes, both single setpoint and dual setpoint (HEAT_COOL).
    In HEAT_COOL mode, both heat and cool setpoints and fan speeds can be set.
    """

    MODE_BITMASK_MAP = [
        (0, HVACMode.COOL),
        (1, HVACMode.HEAT),
        (2, HVACMode.FAN_ONLY),
        (3, HVACMode.HEAT_COOL),
        (4, HVACMode.DRY),
    ]

    def __init__(self, coordinator, api, device_data: dict, config: dict, hass):
        """
        Initialize the climate entity.
        :param coordinator: The DataUpdateCoordinator instance.
        :param api: The AirzoneAPI instance.
        :param device_data: Dictionary with device information.
        :param config: Integration configuration.
        :param hass: Home Assistant instance.
        """
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data.copy()
        self._config = config
        self.hass = hass

        # Entity attributes
        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None

        # Register unique_id properly
        unique_id = device_data.get("id")
        self.async_set_unique_id(unique_id)

    @property
    def hvac_modes(self):
        """Return supported HVAC modes based on the device's modes bitmask."""
        modes_field = self._device_data.get("modes", "11111000")
        available = [HVACMode.OFF]
        for idx, ha_mode in self.MODE_BITMASK_MAP:
            if idx < len(modes_field) and modes_field[idx] == "1":
                if ha_mode == HVACMode.HEAT_COOL and not (
                    self._config.get("force_hvac_mode_auto", False) or modes_field[3] == "1"
                ):
                    continue
                available.append(ha_mode)
        if self._config.get("force_hvac_mode_auto", False) and HVACMode.HEAT_COOL not in available:
            available.append(HVACMode.HEAT_COOL)
        return available

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """Return the main setpoint in single setpoint modes."""
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_high
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self):
        """Return the high (heat) setpoint in HEAT_COOL mode."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_high
        return None

    @property
    def target_temperature_low(self):
        """Return the low (cool) setpoint in HEAT_COOL mode."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_low
        return None

    @property
    def supported_features(self):
        """Return supported features bitmask."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TARGET_TEMPERATURE_HIGH
                | ClimateEntityFeature.TARGET_TEMPERATURE_LOW
                | ClimateEntityFeature.FAN_MODE
            )
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def min_temp(self):
        """Return minimum temperature for current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.HEAT_COOL]:
            return float(self._device_data.get("min_limit_heat", 16))
        return float(self._device_data.get("min_limit_cold", 16))

    @property
    def max_temp(self):
        """Return maximum temperature for current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.HEAT_COOL]:
            return float(self._device_data.get("max_limit_heat", 32))
        return float(self._device_data.get("max_limit_cold", 32))

    @property
    def fan_modes(self):
        """Return available fan speeds as strings."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return []
        return [str(s) for s in range(1, int(self._device_data.get("availables_speeds", 3)) + 1)]

    @property
    def fan_mode(self):
        """Return current fan speed, or None if not applicable."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return None
        return self._fan_mode

    @property
    def device_info(self):
        """Return device registry info."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand', 'Unknown')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "connections": {("mac", self._device_data.get("mac"))},
        }

    async def async_update(self):
        """
        Refresh from coordinator and update local state variables.
        """
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self.unique_id)
        if not device:
            return

        self._device_data = device
        # parse power & mode
        if int(device.get("power", 0)) == 1:
            mode = device.get("mode")
            if mode == "1":
                self._hvac_mode = HVACMode.COOL
                self._target_temp_low = int(float(device.get("cold_consign", 0)))
                self._fan_mode = device.get("cold_speed")
            elif mode == "2":
                self._hvac_mode = HVACMode.HEAT
                self._target_temp_high = int(float(device.get("heat_consign", 0)))
                self._fan_mode = device.get("heat_speed")
            elif mode == "3":
                self._hvac_mode = HVACMode.FAN_ONLY
                self._fan_mode = device.get("cold_speed")
            elif mode == "5":
                self._hvac_mode = HVACMode.DRY
                self._fan_mode = None
            elif mode == "4":
                self._hvac_mode = HVACMode.HEAT_COOL
                self._target_temp_high = int(float(device.get("heat_consign", 0)))
                self._target_temp_low = int(float(device.get("cold_consign", 0)))
                self._fan_mode = device.get("heat_speed") or device.get("cold_speed")
            else:
                self._hvac_mode = HVACMode.OFF
                self._fan_mode = None
        else:
            self._hvac_mode = HVACMode.OFF
            self._fan_mode = None

    async def async_set_fan_mode(self, fan_mode):
        """
        Set the fan mode asynchronously.
        In HEAT_COOL mode, this sets both cool (P3) and heat (P4) speeds.
        """
        await self.hass.async_create_task(self._async_set_fan_mode_sync(fan_mode))

    async def _async_set_fan_mode_sync(self, fan_mode):
        """Internal coroutine to send fan speed commands."""
        speed = int(fan_mode)
        if speed not in range(1, int(self._device_data.get("availables_speeds", 3)) + 1):
            _LOGGER.error("Fan speed %s not valid", speed)
            return

        if self._hvac_mode in [HVACMode.COOL, HVACMode.FAN_ONLY]:
            self._send_command("P3", speed)
        elif self._hvac_mode == HVACMode.HEAT:
            self._send_command("P4", speed)
        elif self._hvac_mode == HVACMode.HEAT_COOL:
            self._send_command("P3", speed)
            self._send_command("P4", speed)

        self._fan_mode = fan_mode
        self._refresh_state()

    def _refresh_state(self):
        """Schedule an immediate refresh of coordinator data."""
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    def turn_on(self):
        """Turn on the device."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Turn off the device."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._refresh_state()

    def set_hvac_mode(self, hvac_mode):
        """Set HVAC mode with proper P2 mapping."""
        if hvac_mode == HVACMode.OFF:
            return self.turn_off()

        mapping = {
            HVACMode.COOL: "1",
            HVACMode.HEAT: "2",
            HVACMode.FAN_ONLY: "3",
            HVACMode.HEAT_COOL: "4",
            HVACMode.DRY: "5",
        }
        # ensure device is ON
        if self._hvac_mode == HVACMode.OFF:
            self.turn_on()
        self._send_command("P2", mapping[hvac_mode])
        self._hvac_mode = hvac_mode
        self._refresh_state()

    def set_temperature(self, **kwargs):
        """
        Set temperatures: single or dual setpoint.
        Uses P7 for cool, P8 for heat.
        """
        temp = kwargs.get(ATTR_TEMPERATURE)
        temp_high = kwargs.get("target_temp_high")
        temp_low = kwargs.get("target_temp_low")

        if self._hvac_mode in [HVACMode.DRY, HVACMode.FAN_ONLY]:
            _LOGGER.warning("Temperature not supported in mode %s", self._hvac_mode)
            return

        commands = []
        if self._hvac_mode == HVACMode.HEAT_COOL:
            if temp_high is not None:
                commands.append(("P8", f"{int(temp_high)}.0"))
            if temp_low is not None:
                commands.append(("P7", f"{int(temp_low)}.0"))
        else:
            if self._hvac_mode == HVACMode.HEAT and temp is not None:
                commands.append(("P8", f"{int(temp)}.0"))
            if self._hvac_mode == HVACMode.COOL and temp is not None:
                commands.append(("P7", f"{int(temp)}.0"))

        for opt, val in commands:
            self._send_command(opt, val)
        self._refresh_state()

    def _send_command(self, option, value):
        """Send an event to the Airzone API."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self.unique_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Sending command: %s", payload)
        # schedule via HA event loop
        self.hass.async_create_task(self._api.send_event(payload))
