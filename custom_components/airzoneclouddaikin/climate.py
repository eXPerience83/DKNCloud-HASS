"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator."""
import asyncio
import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
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
    """Representation of an Airzone Cloud Daikin climate device."""

    def __init__(self, coordinator, api, device_data: dict, config: dict, hass):
        """Initialize the climate entity.

        :param coordinator: The DataUpdateCoordinator instance.
        :param api: The AirzoneAPI instance.
        :param device_data: Dictionary with device information.
        :param config: Integration configuration.
        :param hass: Home Assistant instance.
        """
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data
        self._config = config
        self.hass = hass
        self._attr_name = device_data.get("name", "Airzone Device")
        # Use the device's id as unique_id; it is assumed to be present.
        self._attr_unique_id = device_data.get("id")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        """Return the list of supported HVAC modes."""
        # Only allow temperature adjustments in COOL, HEAT, or AUTO.
        modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY]
        if self._config.get("force_hvac_mode_auto", False):
            modes.append(HVACMode.AUTO)
        return modes

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """Return the current target temperature.
        
        Hide temperature if mode is OFF, DRY, or FAN_ONLY.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY]:
            return None
        return self._target_temperature

    @property
    def supported_features(self):
        """Return supported features based on the current mode.

        - OFF and DRY: no temperature or fan controls.
        - FAN_ONLY: only fan control.
        - Otherwise: both temperature and fan control.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return 0
        elif self._hvac_mode == HVACMode.FAN_ONLY:
            return ClimateEntityFeature.FAN_MODE
        else:
            return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def fan_modes(self):
        """Return a list of valid fan speeds as strings based on 'availables_speeds'."""
        speeds = self.fan_speed_range
        return [str(speed) for speed in speeds]

    @property
    def fan_mode(self):
        """Return the current fan speed.

        Hide fan mode in OFF and DRY.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return None
        return self._fan_mode

    @property
    def device_info(self):
        """Return device info to group this entity with others in the device registry."""
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "via_device": (DOMAIN, self._device_data.get("id")),
        }

    async def async_update(self):
        """Update the climate entity from the coordinator data and force a UI refresh."""
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
            if int(device.get("power", 0)) == 1:
                mode_val = device.get("mode")
                if mode_val == "1":
                    self._hvac_mode = HVACMode.COOL
                    self._target_temperature = int(float(device.get("cold_consign", "0")))
                    self._fan_mode = str(device.get("cold_speed", ""))
                elif mode_val == "2":
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temperature = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
                elif mode_val == "3":
                    self._hvac_mode = HVACMode.FAN_ONLY
                    self._target_temperature = None
                    self._fan_mode = str(device.get("cold_speed", ""))
                elif mode_val == "5":
                    self._hvac_mode = HVACMode.DRY
                    self._target_temperature = None
                    self._fan_mode = None
                elif mode_val == "4":
                    self._hvac_mode = HVACMode.AUTO
                    self._target_temperature = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
                else:
                    # Default fallback
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temperature = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
            else:
                self._hvac_mode = HVACMode.OFF
                self._target_temperature = None
                self._fan_mode = None
        # Force immediate UI update from this entity.
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode asynchronously and force immediate UI update."""
        if hvac_mode == HVACMode.OFF:
            self._hvac_mode = HVACMode.OFF
            self._target_temperature = None
            self._fan_mode = None
            self._send_command("P1", 0)
        else:
            # If currently OFF, send an on command first
            if self._hvac_mode == HVACMode.OFF:
                self._hvac_mode = hvac_mode
                self._send_command("P1", 1)
                # Wait briefly (1 second) to let the device turn on
                await asyncio.sleep(1)
            # Send the mode command based on mapping
            mode_mapping = {
                HVACMode.COOL: "1",
                HVACMode.HEAT: "2",
                HVACMode.FAN_ONLY: "3",
                HVACMode.DRY: "5",
                HVACMode.AUTO: "4",
            }
            if hvac_mode in mode_mapping:
                self._send_command("P2", mode_mapping[hvac_mode])
                self._hvac_mode = hvac_mode
            else:
                _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)
        # Force immediate UI update
        self.async_write_ha_state()
        self._refresh_state()

    async def async_set_temperature(self, **kwargs):
        """Set the target temperature asynchronously.

        Prevent changes in modes where temperature adjustment is not allowed.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.OFF]:
            _LOGGER.warning("Temperature adjustment not supported in mode %s", self._hvac_mode)
            return
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            temp = int(float(temp))
            if self._hvac_mode in [HVACMode.HEAT, HVACMode.AUTO]:
                min_temp = int(float(self._device_data.get("min_limit_heat", 16)))
                max_temp = int(float(self._device_data.get("max_limit_heat", 32)))
                command = "P8"
            else:
                min_temp = int(float(self._device_data.get("min_limit_cold", 16)))
                max_temp = int(float(self._device_data.get("max_limit_cold", 32)))
                command = "P7"
            if temp < min_temp:
                temp = min_temp
            elif temp > max_temp:
                temp = max_temp
            self._send_command(command, f"{temp}.0")
            self._target_temperature = temp
            self.async_write_ha_state()
            self._refresh_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set the fan mode asynchronously.

        Prevent changes in modes where fan control is not allowed.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.OFF]:
            _LOGGER.warning("Fan control not supported in mode %s", self._hvac_mode)
            return
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)
        self.async_write_ha_state()
        self._refresh_state()

    def set_fan_mode(self, fan_mode):
        """Set the fan mode by delegating to set_fan_speed."""
        self.set_fan_speed(fan_mode)

    def _refresh_state(self):
        """Force an immediate refresh of the coordinator data."""
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(
                self.coordinator.async_request_refresh(), self.hass.loop
            ).result()

    def turn_on(self):
        """Turn on the device by sending P1=1 and refresh state immediately."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Turn off the device by sending P1=0 and refresh state immediately."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None
        self._refresh_state()

    def set_fan_speed(self, speed):
        """Set the fan speed.

        Uses P3 for COOL and FAN_ONLY modes and P4 for HEAT/AUTO modes.
        Fan control is not allowed in DRY or OFF modes.
        """
        try:
            speed = int(speed)
        except ValueError:
            _LOGGER.error("Invalid fan speed: %s", speed)
            return
        if speed not in self.fan_speed_range:
            _LOGGER.error("Fan speed %s not in valid range %s", speed, self.fan_speed_range)
            return
        if self._hvac_mode in [HVACMode.COOL, HVACMode.FAN_ONLY]:
            self._send_command("P3", speed)
        elif self._hvac_mode in [HVACMode.HEAT, HVACMode.AUTO]:
            self._send_command("P4", speed)
        else:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return
        self._fan_mode = str(speed)
        self.async_write_ha_state()
        self._refresh_state()

    @property
    def fan_speed_range(self):
        """Return a list of valid fan speeds based on 'availables_speeds' from device data."""
        speeds_str = self._device_data.get("availables_speeds", "3")
        try:
            speeds = int(speeds_str)
        except ValueError:
            speeds = 3
        return list(range(1, speeds + 1))

    def _send_command(self, option, value):
        """Send a command to the device using the events endpoint and wait for response.
        
        This method sends the command and waits for its completion by calling .result().
        It then forces a coordinator refresh for immediate UI update.
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
            try:
                # Wait for the command to complete synchronously.
                asyncio.run_coroutine_threadsafe(
                    self._api.send_event(payload), self.hass.loop
                ).result()
            except Exception as err:
                _LOGGER.error("Error sending command: %s", err)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
        self._refresh_state()
