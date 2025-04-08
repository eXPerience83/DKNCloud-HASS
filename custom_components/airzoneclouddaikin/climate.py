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
    # Creación de entidad climate para cada dispositivo en el coordinador.
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
        :param device_data: Diccionario con la información del dispositivo.
        :param config: Configuración de la integración.
        :param hass: Instancia de Home Assistant.
        """
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data
        self._config = config
        self.hass = hass
        self._attr_name = device_data.get("name", "Airzone Device")
        # Usamos el id del dispositivo para unique_id. Se asume que viene en los datos.
        self._attr_unique_id = device_data.get("id")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        """Return the set of supported HVAC modes."""
        modes = {HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY}
        if self._config.get("force_hvac_mode_auto", False):
            modes.add(HVACMode.AUTO)
        return modes

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """
        Return the target temperature.
        Se devuelve None en modos donde no se admite ajuste (FAN_ONLY, DRY, OFF).
        """
        if self._hvac_mode in {HVACMode.FAN_ONLY, HVACMode.DRY, HVACMode.OFF}:
            return None
        return self._target_temperature

    @property
    def supported_features(self):
        """
        Return supported features:
          - En OFF y DRY, no se soportan ni temperatura ni ventilador.
          - En FAN_ONLY, sólo se soporta el ajuste del ventilador.
          - En los demás modos se soportan ambos.
        """
        if self._hvac_mode in {HVACMode.DRY, HVACMode.OFF}:
            return 0
        elif self._hvac_mode == HVACMode.FAN_ONLY:
            return ClimateEntityFeature.FAN_MODE
        else:
            return ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE

    @property
    def fan_modes(self):
        """Return a list of valid fan speeds as strings."""
        speeds = self.fan_speed_range
        return [str(speed) for speed in speeds]

    @property
    def fan_mode(self):
        """
        Return the current fan speed.
        En OFF y DRY, se devuelve None.
        """
        if self._hvac_mode in {HVACMode.DRY, HVACMode.OFF}:
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
        """Update the climate entity from the coordinator data."""
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
                    self._target_temperature = None
                elif mode_val == "5":
                    self._hvac_mode = HVACMode.DRY
                    self._fan_mode = None
                    self._target_temperature = None
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
        # No forzamos async_write_ha_state() aquí ya que la actualización se gestiona vía el coordinator.

    async def async_set_fan_mode(self, fan_mode):
        """Async method to set fan mode (delegating to set_fan_speed)."""
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)
        # Aquí estamos en el event loop; se actualiza el estado.
        self.async_write_ha_state()

    def set_fan_mode(self, fan_mode):
        """Synchronous method: delegate to set_fan_speed."""
        self.set_fan_speed(fan_mode)

    def _run_state_update(self):
        """Schedule an immediate state update safely from a thread."""
        if self.hass and self.hass.loop:
            self.hass.async_run_coroutine_threadsafe(self.async_write_ha_state(), self.hass.loop)

    def turn_on(self):
        """Turn on the device (send P1=1) and update local state."""
        self._send_command("P1", 1)
        # Asumimos que al encender se entra en modo COOL por defecto.
        self._hvac_mode = HVACMode.COOL
        self._run_state_update()

    def turn_off(self):
        """Turn off the device (send P1=0) and update local state."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None
        self._run_state_update()

    def set_hvac_mode(self, hvac_mode):
        """
        Set the HVAC mode.
        Mapeo:
         - HVACMode.OFF: se llama a turn_off().
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
            # En FAN_ONLY y DRY, se deshabilita la temperatura.
            if hvac_mode in {HVACMode.FAN_ONLY, HVACMode.DRY}:
                self._target_temperature = None
            self._run_state_update()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_temperature(self, **kwargs):
        """
        Set the target temperature.
        Sólo se permite en modos que soportan temperatura (no en DRY, FAN_ONLY o OFF).
        Para HEAT/AUTO se utiliza P8 y para COOL se utiliza P7.
        """
        if self._hvac_mode in {HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.OFF}:
            _LOGGER.warning("Temperature adjustment not supported in mode %s", self._hvac_mode)
            return
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            temp = int(float(temp))
            if self._hvac_mode in {HVACMode.HEAT, HVACMode.AUTO}:
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
            self._run_state_update()

    def set_fan_speed(self, speed):
        """
        Set the fan speed.
        En COOL y FAN_ONLY se utiliza P3; en HEAT y AUTO se utiliza P4.
        No se permite en DRY ni en OFF.
        """
        if self._hvac_mode in {HVACMode.DRY, HVACMode.OFF}:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return
        try:
            speed = int(speed)
        except ValueError:
            _LOGGER.error("Invalid fan speed: %s", speed)
            return
        if speed not in self.fan_speed_range:
            _LOGGER.error("Fan speed %s not in valid range %s", speed, self.fan_speed_range)
            return
        if self._hvac_mode in {HVACMode.COOL, HVACMode.FAN_ONLY}:
            self._send_command("P3", speed)
        elif self._hvac_mode in {HVACMode.HEAT, HVACMode.AUTO}:
            self._send_command("P4", speed)
        else:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return
        self._fan_mode = str(speed)
        self._run_state_update()

    @property
    def fan_speed_range(self):
        """Return a list of valid fan speeds based on 'availables_speeds'."""
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
