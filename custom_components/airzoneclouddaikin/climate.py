"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator."""
import asyncio
import hashlib
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
    # Create a ClimateEntity for each device in the coordinator's data.
    for device_id, device in coordinator.data.items():
        entities.append(AirzoneClimate(coordinator, api, device, config, hass))
    async_add_entities(entities, True)

class AirzoneClimate(ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    def __init__(self, coordinator, api, device_data: dict, config: dict, hass):
        """
        Initialize the climate entity.

        :param coordinator: DataUpdateCoordinator instance.
        :param api: AirzoneAPI instance.
        :param device_data: Dictionary containing device information.
        :param config: Integration configuration.
        :param hass: Home Assistant instance.
        """
        self.coordinator = coordinator
        self._api = api
        # Work with a copy of the device_data to avoid modifying the original data
        self._device_data = device_data.copy()
        self._config = config
        self.hass = hass

        # Set the entity name using the "name" field or default to "Airzone Device"
        self._attr_name = self._device_data.get("name", "Airzone Device")

        # Ensure that unique_id is set once and remains immutable.
        device_id = self._device_data.get("id")
        if not device_id or not str(device_id).strip():
            # Generate a unique id based on static fields (name, mac, pin)
            static_data = {
                "name": self._device_data.get("name"),
                "mac": self._device_data.get("mac"),
                "pin": self._device_data.get("pin")
            }
            device_id = hashlib.sha256(str(static_data).encode("utf-8")).hexdigest()
            self._device_data["id"] = device_id  # update for future use
        self._attr_unique_id = device_id

        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        """Return the list of supported HVAC modes."""
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
        """Return the target temperature.

        Returns None if the mode is OFF, DRY, or FAN_ONLY.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY]:
            return None
        return self._target_temperature

    @property
    def supported_features(self):
        """Return supported features based on the current mode.

        - OFF or DRY: no controls.
        - FAN_ONLY: only FAN_MODE control.
        - Otherwise: allow TARGET_TEMPERATURE and FAN_MODE.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return 0
        elif self._hvac_mode == HVACMode.FAN_ONLY:
            return ClimateEntityFeature.FAN_MODE
        else:
            return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def fan_modes(self):
        """Return a list of valid fan speeds (as strings) based on 'availables_speeds'."""
        speeds = self.fan_speed_range
        return [str(speed) for speed in speeds]

    @property
    def fan_mode(self):
        """Return the current fan mode.

        Returns None if the mode is OFF or DRY.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return None
        return self._fan_mode

    @property
    def device_info(self):
        """Return device info for grouping in the device registry."""
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "via_device": (DOMAIN, self._device_data.get("id")),
        }

    async def async_update(self):
        """Update the climate entity from the coordinator data and refresh the UI."""
        await self.coordinator.async_request_refresh()
        # Try to get the updated device data using the unique id stored at initialization.
        updated_device = self.coordinator.data.get(self._attr_unique_id)
        if not updated_device:
            # Fallback: iterate over coordinator data to find a device matching the name.
            for dev in self.coordinator.data.values():
                if dev.get("name") == self._attr_name:
                    updated_device = dev
                    break
        if updated_device:
            # Update only dynamic fields, leaving the unique id unchanged.
            self._device_data.update({
                "power": updated_device.get("power"),
                "mode": updated_device.get("mode"),
                "cold_consign": updated_device.get("cold_consign"),
                "heat_consign": updated_device.get("heat_consign"),
                "cold_speed": updated_device.get("cold_speed"),
                "heat_speed": updated_device.get("heat_speed"),
                "min_limit_cold": updated_device.get("min_limit_cold"),
                "max_limit_cold": updated_device.get("max_limit_cold"),
                "min_limit_heat": updated_device.get("min_limit_heat"),
                "max_limit_heat": updated_device.get("max_limit_heat"),
            })
            if int(updated_device.get("power", 0)) == 1:
                mode_val = updated_device.get("mode")
                if mode_val == "1":
                    self._hvac_mode = HVACMode.COOL
                    self._target_temperature = int(float(updated_device.get("cold_consign", "0")))
                    self._fan_mode = str(updated_device.get("cold_speed", ""))
                elif mode_val == "2":
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temperature = int(float(updated_device.get("heat_consign", "0")))
                    self._fan_mode = str(updated_device.get("heat_speed", ""))
                elif mode_val == "3":
                    self._hvac_mode = HVACMode.FAN_ONLY
                    self._target_temperature = None
                    self._fan_mode = str(updated_device.get("cold_speed", ""))
                elif mode_val == "5":
                    self._hvac_mode = HVACMode.DRY
                    self._target_temperature = None
                    self._fan_mode = None
                elif mode_val == "4":
                    self._hvac_mode = HVACMode.AUTO
                    self._target_temperature = int(float(updated_device.get("heat_consign", "0")))
                    self._fan_mode = str(updated_device.get("heat_speed", ""))
                else:
                    # Default fallback to HEAT mode
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temperature = int(float(updated_device.get("heat_consign", "0")))
                    self._fan_mode = str(updated_device.get("heat_speed", ""))
            else:
                self._hvac_mode = HVACMode.OFF
                self._target_temperature = None
                self._fan_mode = None
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode asynchronously with immediate UI update."""
        if hvac_mode == HVACMode.OFF:
            self._hvac_mode = HVACMode.OFF
            self._target_temperature = None
            self._fan_mode = None
            self._send_command("P1", 0)
        else:
            # If the device is off, turn it on first.
            if self._hvac_mode == HVACMode.OFF:
                self._hvac_mode = hvac_mode
                self._send_command("P1", 1)
                await asyncio.sleep(1)  # Wait 1 second for the device to power on
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
        self.async_write_ha_state()
        self._refresh_state()

    async def async_set_temperature(self, **kwargs):
        """Set the target temperature asynchronously.

        Does nothing if the current mode does not support temperature control.
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

        Does nothing if fan control is not supported in the current mode.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.OFF]:
            _LOGGER.warning("Fan control not supported in mode %s", self._hvac_mode)
            return
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)
        self.async_write_ha_state()
        self._refresh_state()

    def set_fan_mode(self, fan_mode):
        """Delegate fan mode setting to set_fan_speed."""
        self.set_fan_speed(fan_mode)

    def _refresh_state(self):
        """Force an immediate refresh of the coordinator data."""
        if self.hass and self.hass.loop:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.coordinator.async_request_refresh(), self.hass.loop
                ).result()
            except Exception as err:
                _LOGGER.error("Error refreshing state: %s", err)

    def turn_on(self):
        """Turn on the device by sending P1=1 and refresh state."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Turn off the device by sending P1=0, reset mode values, and refresh state."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None
        self._refresh_state()

    def set_fan_speed(self, speed):
        """Set the fan speed.

        Uses P3 for COOL/FAN_ONLY modes and P4 for HEAT/AUTO modes.
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
        """Return a list of valid fan speeds based on the 'availables_speeds' field in device data."""
        speeds_str = self._device_data.get("availables_speeds", "3")
        try:
            speeds = int(speeds_str)
        except ValueError:
            speeds = 3
        return list(range(1, speeds + 1))

    def _send_command(self, option, value):
        """Send a command to the device using the events endpoint and wait for completion."""
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
                # Wait for the API response.
                asyncio.run_coroutine_threadsafe(
                    self._api.send_event(payload), self.hass.loop
                ).result()
            except Exception as err:
                _LOGGER.error("Error sending command: %s", err)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
        self._refresh_state()
