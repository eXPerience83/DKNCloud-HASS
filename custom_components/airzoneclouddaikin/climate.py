"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API."""
import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up climate entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    api = data["api"]
    config = entry.data

    entities = [
        AirzoneClimate(coordinator, api, device, config, hass)
        for device in coordinator.data.values()
    ]
    async_add_entities(entities, True)


class AirzoneClimate(ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    def __init__(self, coordinator, api, device_data, config, hass):
        """Initialize the climate entity."""
        self.coordinator = coordinator
        self._api = api
        self._device = device_data.copy()
        self._config = config
        self.hass = hass

        # ─── Core attributes ────────────────────────────────────────────────
        self._attr_name = self._device["name"]
        # Use device_id as unique_id
        self._attr_unique_id = self._device["id"]
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        # ─── Enable entity by default ───────────────────────────────────────
        self._attr_entity_registry_enabled_default = True

        # ─── Runtime state ──────────────────────────────────────────────────
        self._hvac_mode = HVACMode.OFF
        self._target_temp_low = None
        self._target_temp_high = None
        self._fan_mode = None

    @property
    def hvac_modes(self):
        modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT,
                 HVACMode.FAN_ONLY, HVACMode.DRY]
        if self._config.get("force_hvac_mode_auto"):
            modes.append(HVACMode.AUTO)
        return modes

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def target_temperature(self):
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_high
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self):
        if self._hvac_mode == HVACMode.AUTO:
            return self._target_temp_high
        return None

    @property
    def target_temperature_low(self):
        if self._hvac_mode == HVACMode.AUTO:
            return self._target_temp_low
        return None

    @property
    def supported_features(self):
        feat = ClimateEntityFeature.FAN_MODE
        if self._hvac_mode == HVACMode.AUTO:
            feat |= (ClimateEntityFeature.TARGET_TEMPERATURE |
                     ClimateEntityFeature.TARGET_TEMPERATURE_HIGH |
                     ClimateEntityFeature.TARGET_TEMPERATURE_LOW)
        else:
            feat |= ClimateEntityFeature.TARGET_TEMPERATURE
        return feat

    @property
    def fan_modes(self):
        if self._hvac_mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        max_speed = int(self._device.get("availables_speeds", 3))
        return [str(i) for i in range(1, max_speed + 1)]

    @property
    def fan_mode(self):
        return self._fan_mode

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_update(self):
        """Refresh data from API and update state."""
        await self.coordinator.async_request_refresh()
        dev = self.coordinator.data[self._device["id"]]
        self._device = dev

        power = int(dev.get("power", 0))
        if power == 0:
            self._hvac_mode = HVACMode.OFF
            return

        mode = dev.get("mode")
        if mode == "1":
            self._hvac_mode = HVACMode.COOL
            self._target_temp_low = int(float(dev.get("cold_consign", 0)))
            self._fan_mode = dev.get("cold_speed")
        elif mode == "2":
            self._hvac_mode = HVACMode.HEAT
            self._target_temp_high = int(float(dev.get("heat_consign", 0)))
            self._fan_mode = dev.get("heat_speed")
        elif mode == "3":
            self._hvac_mode = HVACMode.FAN_ONLY
            self._fan_mode = dev.get("cold_speed")
        elif mode == "5":
            self._hvac_mode = HVACMode.DRY
        else:
            # Treat “4” or AUTO fallback
            self._hvac_mode = HVACMode.AUTO
            self._target_temp_low = int(float(dev.get("cold_consign", 0)))
            self._target_temp_high = int(float(dev.get("heat_consign", 0)))
            self._fan_mode = dev.get("heat_speed") or dev.get("cold_speed")

    def _send(self, option, value):
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device["id"],
                "option": option,
                "value": value
            }
        }
        _LOGGER.debug("Sending event %s", payload)
        asyncio.run_coroutine_threadsafe(
            self._api.send_event(payload), self.hass.loop
        )

    def turn_on(self):
        self._send("P1", 1)

    def turn_off(self):
        self._send("P1", 0)

    def set_hvac_mode(self, hvac_mode):
        if hvac_mode == HVACMode.OFF:
            return self.turn_off()
        mapping = {
            HVACMode.COOL: "1", HVACMode.HEAT: "2",
            HVACMode.FAN_ONLY: "3", HVACMode.AUTO: "4",
            HVACMode.DRY: "5"
        }
        self._send("P2", mapping[hvac_mode])

    def set_temperature(self, **kwargs):
        temp = kwargs.get(ATTR_TEMPERATURE)
        if self._hvac_mode == HVACMode.COOL and temp is not None:
            self._send("P7", f"{int(temp)}.0")
        if self._hvac_mode == HVACMode.HEAT and temp is not None:
            self._send("P8", f"{int(temp)}.0")
        if self._hvac_mode == HVACMode.AUTO:
            high = kwargs.get("target_temp_high", temp)
            low = kwargs.get("target_temp_low", temp)
            if low is not None:
                self._send("P7", f"{int(low)}.0")
            if high is not None:
                self._send("P8", f"{int(high)}.0")

    def set_fan_mode(self, fan_mode):
        speed = int(fan_mode)
        if self._hvac_mode in (HVACMode.COOL, HVACMode.FAN_ONLY):
            self._send("P3", speed)
        elif self._hvac_mode in (HVACMode.HEAT, HVACMode.AUTO):
            # In AUTO, set both
            self._send("P4", speed)
            if self._hvac_mode == HVACMode.AUTO:
                self._send("P3", speed)
        self._fan_mode = fan_mode
