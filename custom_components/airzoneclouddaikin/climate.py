"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API."""

import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the climate platform from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data for entry %s", entry.entry_id)
        return

    coordinator = data["coordinator"]
    api = data["api"]
    config = entry.data

    entities = []
    for device_id, device in coordinator.data.items():
        entities.append(
            AirzoneClimate(coordinator, api, device, config, hass, device_id)
        )
    async_add_entities(entities, True)


class AirzoneClimate(ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    MODE_MAP = {
        "1": HVACMode.COOL,
        "2": HVACMode.HEAT,
        "3": HVACMode.FAN_ONLY,
        "4": HVACMode.HEAT_COOL,
        "5": HVACMode.DRY,
    }

    def __init__(self, coordinator, api, device_data, config, hass, device_id):
        """Initialize."""
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data.copy()
        self._config = config
        self.hass = hass

        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_unique_id = device_id
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._hvac_mode = HVACMode.OFF
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        """Return supported modes from bitmask, plus forced if configured."""
        mask = self._device_data.get("modes", "")
        available = [HVACMode.OFF]
        for idx, mode_key in enumerate(["1","2","3","4","5"]):
            if idx < len(mask) and mask[idx] == "1":
                ha = self.MODE_MAP[mode_key]
                if ha == HVACMode.HEAT_COOL and not (
                    self._config.get("force_hvac_mode_auto") or mask[3] == "1"
                ):
                    continue
                available.append(ha)
        if self._config.get("force_hvac_mode_auto") and HVACMode.HEAT_COOL not in available:
            available.append(HVACMode.HEAT_COOL)
        return available

    @property
    def hvac_mode(self):
        """Return current mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """Return single setpoint."""
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_high
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self):
        """High setpoint for HEAT_COOL."""
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return self._target_temp_high
        return None

    @property
    def target_temperature_low(self):
        """Low setpoint for HEAT_COOL."""
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
        """Return minimum allowed temperature."""
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL):
            return float(self._device_data.get("min_limit_heat", 16))
        return float(self._device_data.get("min_limit_cold", 16))

    @property
    def max_temp(self):
        """Return maximum allowed temperature."""
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL):
            return float(self._device_data.get("max_limit_heat", 32))
        return float(self._device_data.get("max_limit_cold", 32))

    @property
    def fan_modes(self):
        """Return valid fan speeds."""
        if self._hvac_mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        speeds = int(self._device_data.get("availables_speeds", 3))
        return [str(i) for i in range(1, speeds + 1)]

    @property
    def fan_mode(self):
        """Return current fan mode."""
        return self._fan_mode

    @property
    def device_info(self):
        """Return device registry info."""
        return {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware"),
            "connections": {("mac", self._device_data.get("mac"))},
        }

    async def async_update(self):
        """Refresh data and update internal state."""
        await self.coordinator.async_request_refresh()
        dev = self.coordinator.data.get(self._attr_unique_id)
        if not dev:
            return
        self._device_data = dev

        if dev.get("power") == "1":
            mode = dev.get("mode")
            ha = self.MODE_MAP.get(mode, HVACMode.OFF)
            self._hvac_mode = ha
            if ha == HVACMode.COOL:
                self._target_temp_low = int(float(dev.get("cold_consign", 0)))
                self._fan_mode = dev.get("cold_speed")
            elif ha == HVACMode.HEAT:
                self._target_temp_high = int(float(dev.get("heat_consign", 0)))
                self._fan_mode = dev.get("heat_speed")
            elif ha == HVACMode.FAN_ONLY:
                self._fan_mode = dev.get("cold_speed")
            elif ha == HVACMode.DRY:
                self._fan_mode = None
            elif ha == HVACMode.HEAT_COOL:
                self._target_temp_low = int(float(dev.get("cold_consign", 0)))
                self._target_temp_high = int(float(dev.get("heat_consign", 0)))
                self._fan_mode = dev.get("heat_speed") or dev.get("cold_speed")
        else:
            self._hvac_mode = HVACMode.OFF
            self._fan_mode = None

    async def async_set_fan_mode(self, fan_mode):
        """Set fan mode."""
        await self.hass.async_create_task(self._send_fan(fan_mode))

    async def _send_fan(self, fan_mode):
        speed = int(fan_mode)
        if self._hvac_mode in (HVACMode.COOL, HVACMode.FAN_ONLY):
            self._send("P3", speed)
        elif self._hvac_mode == HVACMode.HEAT:
            self._send("P4", speed)
        elif self._hvac_mode == HVACMode.HEAT_COOL:
            self._send("P3", speed)
            self._send("P4", speed)
        self._fan_mode = fan_mode
        await self.coordinator.async_request_refresh()

    def turn_on(self):
        """Turn on device."""
        self._send("P1", 1)

    def turn_off(self):
        """Turn off device."""
        self._send("P1", 0)

    def set_hvac_mode(self, mode):
        """Set HVAC mode."""
        if mode == HVACMode.OFF:
            return self.turn_off()
        code = {v: k for k, v in self.MODE_MAP.items()}.get(mode)
        if code:
            if self._hvac_mode == HVACMode.OFF:
                self.turn_on()
            self._send("P2", code)

    def set_temperature(self, **kwargs):
        """Set temperature(s)."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        high = kwargs.get("target_temp_high")
        low = kwargs.get("target_temp_low")
        cmds = []
        if self._hvac_mode == HVACMode.HEAT_COOL:
            if low is not None:
                cmds.append(("P7", f"{int(low)}.0"))
            if high is not None:
                cmds.append(("P8", f"{int(high)}.0"))
        else:
            if self._hvac_mode == HVACMode.COOL and temp is not None:
                cmds.append(("P7", f"{int(temp)}.0"))
            if self._hvac_mode == HVACMode.HEAT and temp is not None:
                cmds.append(("P8", f"{int(temp)}.0"))
        for opt, val in cmds:
            self._send(opt, val)

    def _send(self, option, value):
        """Helper to fire API event."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._attr_unique_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Sending %s=%s", option, value)
        self.hass.async_create_task(self._api.send_event(payload))
