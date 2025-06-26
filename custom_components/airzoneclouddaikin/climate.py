"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator.

Implements dynamic HVAC mode mapping and exposes both HEAT and COOL setpoints
and fan speeds in HEAT_COOL mode. When setting temperatures or fan speed in HEAT_COOL,
commands are sent to both heat and cool endpoints.
"""
import asyncio
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
    coordinator = data.get("coordinator")
    api = data.get("api")
    config = entry.data
    entities = []
    # Create a climate entity for each device in the coordinator data.
    for device_id, device in coordinator.data.items():
        entities.append(AirzoneClimate(coordinator, api, device, config, hass))
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
        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_unique_id = device_data.get("id")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        """
        Return the list of supported HVAC modes according to the device's 'modes' bitmask,
        allowing 'HEAT_COOL' to be forced if configured by the user.
        """
        modes_field = self._device_data.get("modes", "11111000")
        if len(modes_field) < 5:
            modes_field = "11111000"
        available_modes = [HVACMode.OFF]
        for idx, ha_mode in self.MODE_BITMASK_MAP:
            if idx < len(modes_field) and modes_field[idx] == "1":
                # Only include HEAT_COOL if supported or forced
                if ha_mode == HVACMode.HEAT_COOL and not (
                    self._config.get("force_hvac_mode_auto", False) or modes_field[3] == "1"
                ):
                    continue
                available_modes.append(ha_mode)
        if self._config.get("force_hvac_mode_auto", False) and HVACMode.HEAT_COOL not in available_modes:
            available_modes.append(HVACMode.HEAT_COOL)
        return available_modes

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """
        Return the current target temperature (single setpoint).
        In HEAT or COOL mode, this is the main setpoint.
        """
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_high
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self):
        """
        Return the high setpoint (heat) for HEAT_COOL mode.
        """
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_high
        return None

    @property
    def target_temperature_low(self):
        """
        Return the low setpoint (cool) for HEAT_COOL mode.
        """
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_low
        return None

    @property
    def supported_features(self):
        """
        Return supported features:
        - TARGET_TEMPERATURE in single setpoint modes
        - TARGET_TEMPERATURE_HIGH and _LOW in HEAT_COOL mode
        - FAN_MODE in all modes except OFF/DRY
        """
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
        """Return the minimum allowed temperature for the current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.HEAT_COOL]:
            return float(self._device_data.get("min_limit_heat", 16))
        return float(self._device_data.get("min_limit_cold", 16))

    @property
    def max_temp(self):
        """Return the maximum allowed temperature for the current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.HEAT_COOL]:
            return float(self._device_data.get("max_limit_heat", 32))
        return float(self._device_data.get("max_limit_cold", 32))

    @property
    def fan_modes(self):
        """
        Return a list of valid fan speeds as strings based on 'availables_speeds'.
        In OFF or DRY mode, no fan speed options should be displayed.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return []
        speeds = self.fan_speed_range
        return [str(speed) for speed in speeds]

    @property
    def fan_mode(self):
        """
        Return the current fan speed.
        In OFF or DRY mode, return None so that no value is shown.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return None
        return self._fan_mode

    @property
    def device_info(self):
        """Return device info to link this entity to a device in Home Assistant."""
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
        Update the climate entity from the coordinator data.
        Updates the cached values for temperatures and fan based on the current device mode.
        """
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
            if int(device.get("power", 0)) == 1:
                mode_val = device.get("mode")
                if mode_val == "1":
                    self._hvac_mode = HVACMode.COOL
                    self._target_temp_low = int(float(device.get("cold_consign", "0")))
                    self._fan_mode = str(device.get("cold_speed", ""))
                elif mode_val == "2":
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temp_high = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
                elif mode_val == "3":
                    self._hvac_mode = HVACMode.FAN_ONLY
                    self._fan_mode = str(device.get("cold_speed", ""))
                elif mode_val == "5":
                    self._hvac_mode = HVACMode.DRY
                    self._target_temp_high = None
                    self._target_temp_low = None
                    self._fan_mode = None
                elif mode_val == "4":
                    self._hvac_mode = HVACMode.HEAT_COOL
                    self._target_temp_high = int(float(device.get("heat_consign", "0")))
                    self._target_temp_low = int(float(device.get("cold_consign", "0")))
                    # In HEAT_COOL, we display the heat fan speed
                    self._fan_mode = str(device.get("heat_speed", "")) or str(device.get("cold_speed", ""))
                else:
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temp_high = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
            else:
                self._hvac_mode = HVACMode.OFF
                self._target_temp_high = None
                self._target_temp_low = None
                self._fan_mode = None

    async def async_set_fan_mode(self, fan_mode):
        """
        Set the fan mode asynchronously.
        In HEAT_COOL mode, this sets both cool (P3) and heat (P4) speeds.
        """
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)

    def set_fan_mode(self, fan_mode):
        """
        Set the fan mode.
        In COOL and FAN_ONLY modes, uses P3.
        In HEAT mode, uses P4.
        In HEAT_COOL mode, sends both P3 (cool) and P4 (heat).
        In DRY mode, fan speed adjustments are disabled.
        """
        try:
            speed = int(fan_mode)
        except ValueError:
            _LOGGER.error("Invalid fan speed: %s", fan_mode)
            return
        if speed not in self.fan_speed_range:
            _LOGGER.error("Fan speed %s not in valid range %s", speed, self.fan_speed_range)
            return

        if self._hvac_mode == HVACMode.COOL or self._hvac_mode == HVACMode.FAN_ONLY:
            self._send_command("P3", speed)
        elif self._hvac_mode == HVACMode.HEAT:
            self._send_command("P4", speed)
        elif self._hvac_mode == HVACMode.HEAT_COOL:
            self._send_command("P3", speed)  # Set cool fan speed
            self._send_command("P4", speed)  # Set heat fan speed
        else:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return
        self._fan_mode = str(speed)
        self._refresh_state()

    def _refresh_state(self):
        """Schedule an immediate refresh of coordinator data on the event loop."""
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(self.coordinator.async_request_refresh(), self.hass.loop)

    def turn_on(self):
        """
        Turn on the device by sending P1=1 and refresh state.
        """
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """
        Turn off the device by sending P1=0 and refresh state.
        """
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None
        self._refresh_state()

    def set_hvac_mode(self, hvac_mode):
        """
        Set the HVAC mode.
        Mapping:
         - HVACMode.OFF: call turn_off() and return.
         - HVACMode.COOL -> P2=1
         - HVACMode.HEAT -> P2=2
         - HVACMode.FAN_ONLY -> P2=3
         - HVACMode.HEAT_COOL -> P2=4
         - HVACMode.DRY -> P2=5
        """
        if self._hvac_mode == HVACMode.OFF and hvac_mode != HVACMode.OFF:
            self.turn_on()
        if hvac_mode == HVACMode.OFF:
            self.turn_off()
            return
        mode_mapping = {
            HVACMode.COOL: "1",
            HVACMode.HEAT: "2",
            HVACMode.FAN_ONLY: "3",
            HVACMode.HEAT_COOL: "4",
            HVACMode.DRY: "5",
        }
        if hvac_mode in mode_mapping:
            self._send_command("P2", mode_mapping[hvac_mode])
            self._hvac_mode = hvac_mode
            self._refresh_state()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_temperature(self, **kwargs):
        """
        Set the target temperature(s).
        In HEAT or COOL mode: adjusts single setpoint (P8 for HEAT, P7 for COOL).
        In HEAT_COOL mode: adjusts both setpoints if provided (P7 for cool, P8 for heat).
        Temperature adjustments are disabled in DRY and FAN_ONLY modes.
        The value is constrained to the device limits and sent as an integer with '.0' appended.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.FAN_ONLY]:
            _LOGGER.warning("Temperature adjustment not supported in mode %s", self._hvac_mode)
            return

        temp = kwargs.get(ATTR_TEMPERATURE)
        temp_high = kwargs.get("target_temp_high")
        temp_low = kwargs.get("target_temp_low")
        # If HEAT_COOL, accept high/low or fallback to temp
        if self._hvac_mode == HVACMode.HEAT_COOL:
            # Set both setpoints if provided
            heat = temp_high if temp_high is not None else temp
            cool = temp_low if temp_low is not None else temp
            commands = []
            if heat is not None:
                heat = int(float(heat))
                min_heat = int(float(self._device_data.get("min_limit_heat", 16)))
                max_heat = int(float(self._device_data.get("max_limit_heat", 32)))
                heat = max(min_heat, min(heat, max_heat))
                commands.append(("P8", f"{heat}.0"))
                self._target_temp_high = heat
            if cool is not None:
                cool = int(float(cool))
                min_cool = int(float(self._device_data.get("min_limit_cold", 16)))
                max_cool = int(float(self._device_data.get("max_limit_cold", 32)))
                cool = max(min_cool, min(cool, max_cool))
                commands.append(("P7", f"{cool}.0"))
                self._target_temp_low = cool
            # Send all relevant commands
            for cmd, val in commands:
                self._send_command(cmd, val)
            self._refresh_state()
            return

        # Single setpoint logic
        if self._hvac_mode == HVACMode.HEAT:
            if temp is not None:
                temp = int(float(temp))
                min_temp = int(float(self._device_data.get("min_limit_heat", 16)))
                max_temp = int(float(self._device_data.get("max_limit_heat", 32)))
                temp = max(min_temp, min(temp, max_temp))
                self._send_command("P8", f"{temp}.0")
                self._target_temp_high = temp
                self._refresh_state()
        elif self._hvac_mode == HVACMode.COOL:
            if temp is not None:
                temp = int(float(temp))
                min_temp = int(float(self._device_data.get("min_limit_cold", 16)))
                max_temp = int(float(self._device_data.get("max_limit_cold", 32)))
                temp = max(min_temp, min(temp, max_temp))
                self._send_command("P7", f"{temp}.0")
                self._target_temp_low = temp
                self._refresh_state()

    @property
    def fan_speed_range(self):
        """
        Return a list of valid fan speeds based on 'availables_speeds' from device data.
        """
        speeds_str = self._device_data.get("availables_speeds", "3")
        try:
            speeds = int(speeds_str)
        except ValueError:
            speeds = 3
        return list(range(1, speeds + 1))

    def _send_command(self, option, value):
        """
        Send a command to the device using the events endpoint.
        """
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_data.get("id"),
                "option": option,
                "value": value,
            }
        }
        _LOGGER.info("Sending command: %s", payload)
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(
                self._api.send_event(payload), self.hass.loop
            )
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
