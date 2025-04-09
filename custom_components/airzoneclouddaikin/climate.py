"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator."""
import asyncio
import logging
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    ATTR_MIN_TEMP,
    ATTR_MAX_TEMP,
    ATTR_FAN_MODES,
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
    """Representation of an Airzone Cloud Daikin climate device."""

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
        # Usamos una copia para evitar perder datos originales
        self._device_data = device_data.copy()
        self._config = config
        self.hass = hass
        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        # Usamos el id del dispositivo para el unique_id (si no existe, generamos uno)
        device_id = device_data.get("id")
        if not device_id:
            device_id = str(hash(str(device_data)))
            self._device_data["id"] = device_id
        self._attr_unique_id = str(device_id)

        # Estado interno
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None

    @property
    def supported_features(self):
        """Return the supported features as bitmask.

        Si el dispositivo está apagado o en modo DRY, no se soportan cambios.
        Si está en FAN_ONLY, sólo se permite el control del ventilador.
        De lo contrario, se permiten tanto temperatura como ventilador.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return 0
        elif self._hvac_mode == HVACMode.FAN_ONLY:
            return ClimateEntityFeature.FAN_MODE
        return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def capability_attributes(self):
        """Return extra attributes for the climate entity.

        Se usan operaciones bitwise para evitar que HA intente iterar sobre la bitmask.
        """
        attributes = {}
        supported = self.supported_features

        if supported & ClimateEntityFeature.TARGET_TEMPERATURE:
            if self.min_temp is not None:
                attributes[ATTR_MIN_TEMP] = self.min_temp
            if self.max_temp is not None:
                attributes[ATTR_MAX_TEMP] = self.max_temp

        if supported & ClimateEntityFeature.FAN_MODE:
            attributes[ATTR_FAN_MODES] = self.fan_modes

        return attributes

    @property
    def state_attributes(self):
        """Return the state attributes of the climate entity."""
        attributes = {}
        supported = self.supported_features
        if supported & ClimateEntityFeature.TARGET_TEMPERATURE:
            attributes[ATTR_TEMPERATURE] = self.target_temperature
        if supported & ClimateEntityFeature.FAN_MODE:
            attributes["fan_mode"] = self.fan_mode
        return attributes

    @property
    def target_temperature(self):
        """Return the current target temperature.

        Si el dispositivo está apagado o en modos que no permiten ajuste, devuelve None.
        """
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY]:
            return None
        return self._target_temperature

    @property
    def min_temp(self):
        """Return the minimum allowed temperature based on the current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.AUTO]:
            return int(float(self._device_data.get("min_limit_heat", 16)))
        elif self._hvac_mode == HVACMode.COOL:
            return int(float(self._device_data.get("min_limit_cold", 16)))
        return None

    @property
    def max_temp(self):
        """Return the maximum allowed temperature based on the current mode."""
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.AUTO]:
            return int(float(self._device_data.get("max_limit_heat", 32)))
        elif self._hvac_mode == HVACMode.COOL:
            return int(float(self._device_data.get("max_limit_cold", 32)))
        return None

    @property
    def fan_modes(self):
        """Return a list of valid fan speeds as strings."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return []
        return [str(speed) for speed in self.fan_speed_range]

    @property
    def fan_mode(self):
        """Return the current fan mode as a string."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            return None
        return self._fan_mode

    @property
    def device_info(self):
        """Return device info for device registry."""
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": self._device_data.get("brand", "Unknown"),
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "via_device": (DOMAIN, self._device_data.get("id")),
        }

    async def async_update(self):
        """Update the entity from the coordinator data."""
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
                    self._hvac_mode = HVACMode.HEAT
                    self._target_temperature = int(float(device.get("heat_consign", "0")))
                    self._fan_mode = str(device.get("heat_speed", ""))
            else:
                self._hvac_mode = HVACMode.OFF
                self._target_temperature = None
                self._fan_mode = None

    async def async_set_temperature(self, **kwargs):
        """Set the target temperature asynchronously."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY]:
            _LOGGER.warning("Temperature adjustment not allowed in mode %s", self._hvac_mode)
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
            self._refresh_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set the fan mode asynchronously."""
        if self._hvac_mode in [HVACMode.OFF, HVACMode.DRY]:
            _LOGGER.warning("Fan control not allowed in mode %s", self._hvac_mode)
            return
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)

    def set_fan_mode(self, fan_mode):
        """Set the fan mode by calling set_fan_speed."""
        self.set_fan_speed(fan_mode)

    def turn_on(self):
        """Turn on the device by sending P1=1."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Turn off the device by sending P1=0."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None
        self._refresh_state()

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
            self._refresh_state()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_fan_speed(self, speed):
        """Set the fan speed.

        Uses P3 for COOL and FAN_ONLY modes, P4 for HEAT/AUTO modes.
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

    def _refresh_state(self):
        """Schedule an immediate refresh of coordinator data on the event loop."""
        if self.hass and self.hass.loop:
            from asyncio import ensure_future
            ensure_future(self.coordinator.async_request_refresh(), loop=self.hass.loop)

    def _send_command(self, option, value):
        """Send a command to the device using the events endpoint."""
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
            asyncio.run_coroutine_threadsafe(self._api.send_event(payload), self.hass.loop)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
