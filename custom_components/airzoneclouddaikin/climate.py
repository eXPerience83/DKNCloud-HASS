"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API."""
import asyncio
import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from .const import DOMAIN
from .airzone_api import AirzoneAPI

_LOGGER = logging.getLogger(__name__)

# We use HVACMode.AUTO directly
async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the climate platform from a config entry."""
    config = entry.data
    username = config.get("username")
    password = config.get("password")
    if not username or not password:
        _LOGGER.error("Missing username or password")
        return
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    session = async_get_clientsession(hass)
    api = AirzoneAPI(username, password, session)
    if not await api.login():
        _LOGGER.error("Login to Airzone API failed.")
        return
    installations = await api.fetch_installations()
    entities = []
    for relation in installations:
        installation = relation.get("installation")
        if not installation:
            continue
        installation_id = installation.get("id")
        if not installation_id:
            continue
        devices = await api.fetch_devices(installation_id)
        for device in devices:
            entities.append(AirzoneClimate(api, device, config, hass))
    async_add_entities(entities, True)

class AirzoneClimate(ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""
    def __init__(self, api: AirzoneAPI, device_data: dict, config: dict, hass):
        """Initialize the climate entity.

        :param api: The AirzoneAPI instance.
        :param device_data: Dictionary with device information.
        :param config: Integration configuration.
        :param hass: Home Assistant instance.
        """
        self._api = api
        self._device_data = device_data
        self._config = config
        self._name = device_data.get("name", "Airzone Device")
        self._device_id = device_data.get("id")
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None
        self.hass = hass
        self._hass_loop = None

    async def async_added_to_hass(self):
        """Store the Home Assistant event loop for thread-safe commands."""
        self._hass_loop = self.hass.loop

    @property
    def unique_id(self):
        """Return a unique ID for this climate entity."""
        return self._device_id

    @property
    def name(self):
        """Return the name of the climate entity."""
        return self._name

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

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
        """Return the current target temperature."""
        return self._target_temperature

    @property
    def supported_features(self):
        """Return supported features: target temperature and fan mode."""
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def fan_modes(self):
        """Return a list of valid fan speeds as strings based on 'availables_speeds'."""
        speeds = self.fan_speed_range
        return [str(speed) for speed in speeds]

    @property
    def fan_mode(self):
        """Return the current fan speed."""
        return self._fan_mode

    @property
    def device_info(self):
        """Return device info for the device registry including extra attributes."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "via_device": (DOMAIN, self._device_id),
            "area": None,
            "configuration_url": None,
            # Extra attributes: MAC, PIN, and scenary
            "extra": {
                "mac": self._device_data.get("mac"),
                "pin": self._device_data.get("pin"),
                "scenary": self._device_data.get("scenary")
            }
        }

    async def async_update(self):
        """Poll updated device data from the API and update state."""
        installations = await self._api.fetch_installations()
        for relation in installations:
            installation = relation.get("installation")
            if installation and installation.get("id") == self._device_data.get("installation_id"):
                devices = await self._api.fetch_devices(installation.get("id"))
                for dev in devices:
                    if dev.get("id") == self._device_id:
                        self._device_data = dev
                        if int(dev.get("power", 0)) == 1:
                            mode_val = dev.get("mode")
                            if mode_val == "1":
                                self._hvac_mode = HVACMode.COOL
                                self._target_temperature = int(float(dev.get("cold_consign", "0")))
                                self._fan_mode = str(dev.get("cold_speed", ""))
                            elif mode_val == "2":
                                self._hvac_mode = HVACMode.HEAT
                                self._target_temperature = int(float(dev.get("heat_consign", "0")))
                                self._fan_mode = str(dev.get("heat_speed", ""))
                            elif mode_val == "3":
                                self._hvac_mode = HVACMode.FAN_ONLY
                                self._fan_mode = str(dev.get("cold_speed", ""))
                            elif mode_val == "5":
                                self._hvac_mode = HVACMode.DRY
                                self._fan_mode = ""
                            elif mode_val == "4":
                                self._hvac_mode = HVACMode.AUTO
                                self._target_temperature = int(float(dev.get("heat_consign", "0")))
                                self._fan_mode = str(dev.get("heat_speed", ""))
                            else:
                                self._hvac_mode = HVACMode.HEAT
                                self._target_temperature = int(float(dev.get("heat_consign", "0")))
                                self._fan_mode = str(dev.get("heat_speed", ""))
                        else:
                            self._hvac_mode = HVACMode.OFF
                        break
        self.schedule_update_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set the fan mode asynchronously."""
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)

    def set_fan_mode(self, fan_mode):
        """Set the fan mode by calling set_fan_speed."""
        self.set_fan_speed(fan_mode)

    def turn_on(self):
        """Turn on the device by sending P1=1."""
        self._send_command("P1", 1)
        self.schedule_update_ha_state()

    def turn_off(self):
        """Turn off the device by sending P1=0."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self.schedule_update_ha_state()

    def set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode.
        
        Mapping:
         - HVACMode.OFF: call turn_off() and return.
         - HVACMode.COOL -> P2=1
         - HVACMode.HEAT -> P2=2
         - HVACMode.FAN_ONLY -> P2=3
         - HVACMode.DRY -> P2=5
         - HVACMode.AUTO -> P2=4
        """
        # If the device is off and a non-OFF mode is requested, turn it on first.
        if self._hvac_mode == HVACMode.OFF and hvac_mode != HVACMode.OFF:
            self.turn_on()
        if hvac_mode == HVACMode.OFF:
            self.turn_off()
            return
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
            self.schedule_update_ha_state()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_temperature(self, **kwargs):
        """Set the target temperature.
        
        For HEAT or AUTO modes, use P8; for COOL mode, use P7.
        Temperature adjustments are disabled in DRY and FAN_ONLY modes.
        The value is constrained to the device limits and sent as an integer with '.0' appended.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.FAN_ONLY]:
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
            self.schedule_update_ha_state()

    def set_fan_speed(self, speed):
        """Set the fan speed.
        
        Uses P3 for COOL and FAN_ONLY modes and P4 for HEAT/AUTO modes.
        In DRY mode, fan speed adjustments are disabled.
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
        self.schedule_update_ha_state()

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
        """Send a command to the device using the events endpoint."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.info("Sending command: %s", payload)
        if self._hass_loop:
            asyncio.run_coroutine_threadsafe(self._api.send_event(payload), self._hass_loop)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
