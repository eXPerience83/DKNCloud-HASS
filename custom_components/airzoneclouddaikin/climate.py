"""Climate platform for DKN Cloud for HASS using DataUpdateCoordinator.

Supports dual setpoint (HEAT_COOL) and sends both P3/P4 and P7/P8 for fan and temperature.
"""
import asyncio
import logging
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up climate entities for each device in the coordinator."""
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

    In HEAT_COOL mode, both temperature setpoints and fan speeds for heat and cool are sent.
    """

    MODE_BITMASK_MAP = [
        (0, HVACMode.COOL),
        (1, HVACMode.HEAT),
        (2, HVACMode.FAN_ONLY),
        (3, HVACMode.HEAT_COOL),
        (4, HVACMode.DRY),
    ]

    def __init__(self, coordinator, api, device_data: dict, config: dict, hass):
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data.copy()
        self._config = config
        self.hass = hass

        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_unique_id = device_data.get("id")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._hvac_mode = HVACMode.OFF
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None

        # Ensure climate entity is enabled by default
        self._attr_entity_registry_enabled_default = True

    @property
    def hvac_modes(self):
        """Return supported HVAC modes based on the device bitmask (and forced if configured)."""
        modes_field = self._device_data.get("modes", "11111000")
        if len(modes_field) < 5:
            modes_field = "11111000"
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
        """Single setpoint in HEAT or COOL modes."""
        if self._hvac_mode == HVACMode.HEAT:
            return self._target_temp_high
        if self._hvac_mode == HVACMode.COOL:
            return self._target_temp_low
        return None

    @property
    def target_temperature_high(self):
        """High (heat) setpoint in HEAT_COOL mode."""
        return self._target_temp_high if self._hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def target_temperature_low(self):
        """Low (cool) setpoint in HEAT_COOL mode."""
        return self._target_temp_low if self._hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def supported_features(self):
        """Declare which features are supported based on current mode."""
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
        """Return the min allowed temp for current mode."""
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL):
            return float(self._device_data.get("min_limit_heat", 16))
        return float(self._device_data.get("min_limit_cold", 16))

    @property
    def max_temp(self):
        """Return the max allowed temp for current mode."""
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL):
            return float(self._device_data.get("max_limit_heat", 32))
        return float(self._device_data.get("max_limit_cold", 32))

    @property
    def fan_modes(self):
        """List of fan speeds as strings (disabled in OFF/DRY)."""
        if self._hvac_mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        return [str(s) for s in self.fan_speed_range]

    @property
    def fan_mode(self):
        """Current fan speed (None in OFF/DRY)."""
        return None if self._hvac_mode in (HVACMode.OFF, HVACMode.DRY) else self._fan_mode

    @property
    def device_info(self):
        """Link this entity to its parent device."""
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand', 'Unknown')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "connections": {("mac", self._device_data.get("mac"))} if self._device_data.get("mac") else None,
        }

    async def async_update(self):
        """Refresh coordinator data and update internal state."""
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_data.get("id"))
        if not device:
            return
        self._device_data = device

        if int(device.get("power", 0)) != 1:
            self._hvac_mode = HVACMode.OFF
            self._target_temp_high = None
            self._target_temp_low = None
            self._fan_mode = None
            return

        mode_val = device.get("mode")
        if mode_val == "1":
            self._hvac_mode = HVACMode.COOL
            self._target_temp_low = int(float(device.get("cold_consign", 0)))
            self._fan_mode = str(device.get("cold_speed", ""))
        elif mode_val == "2":
            self._hvac_mode = HVACMode.HEAT
            self._target_temp_high = int(float(device.get("heat_consign", 0)))
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
            self._target_temp_high = int(float(device.get("heat_consign", 0)))
            self._target_temp_low = int(float(device.get("cold_consign", 0)))
            # show heat fan speed in dual mode
            self._fan_mode = str(device.get("heat_speed", "")) or str(device.get("cold_speed", ""))
        else:
            # fallback to HEAT
            self._hvac_mode = HVACMode.HEAT
            self._target_temp_high = int(float(device.get("heat_consign", 0)))
            self._fan_mode = str(device.get("heat_speed", ""))

    async def async_set_fan_mode(self, fan_mode):
        """Call set_fan_mode in executor."""
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)

    def set_fan_mode(self, fan_mode):
        """
        Set fan speed:
        - P3 for COOL/FAN_ONLY
        - P4 for HEAT
        - both P3 & P4 for HEAT_COOL
        """
        try:
            speed = int(fan_mode)
        except ValueError:
            _LOGGER.error("Invalid fan speed: %s", fan_mode)
            return
        if speed not in self.fan_speed_range:
            _LOGGER.error("Fan speed %s not in range %s", speed, self.fan_speed_range)
            return

        if self._hvac_mode in (HVACMode.COOL, HVACMode.FAN_ONLY):
            self._send_command("P3", speed)
        elif self._hvac_mode == HVACMode.HEAT:
            self._send_command("P4", speed)
        elif self._hvac_mode == HVACMode.HEAT_COOL:
            self._send_command("P3", speed)
            self._send_command("P4", speed)
        else:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return

        self._fan_mode = str(speed)
        self._refresh_state()

    def _refresh_state(self):
        """Trigger a coordinator refresh."""
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(self.coordinator.async_request_refresh(), self.hass.loop)

    def turn_on(self):
        """Send P1=1 then refresh."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Send P1=0 then reset internal state."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._target_temp_high = None
        self._target_temp_low = None
        self._fan_mode = None
        self._refresh_state()

    def set_hvac_mode(self, hvac_mode):
        """Send P2 mapping (OFF=turn_off, COOL=1, HEAT=2, FAN_ONLY=3, HEAT_COOL=4, DRY=5)."""
        if self._hvac_mode == HVACMode.OFF and hvac_mode != HVACMode.OFF:
            self.turn_on()
        if hvac_mode == HVACMode.OFF:
            self.turn_off()
            return

        mapping = {
            HVACMode.COOL: "1",
            HVACMode.HEAT: "2",
            HVACMode.FAN_ONLY: "3",
            HVACMode.HEAT_COOL: "4",
            HVACMode.DRY: "5",
        }
        cmd = mapping.get(hvac_mode)
        if cmd:
            self._send_command("P2", cmd)
            self._hvac_mode = hvac_mode
            self._refresh_state()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_temperature(self, **kwargs):
        """
        Send P7/P8 for COOL/HEAT or both for HEAT_COOL.
        Disabled in DRY/FAN_ONLY.
        """
        if self._hvac_mode in (HVACMode.DRY, HVACMode.FAN_ONLY):
            _LOGGER.warning("Cannot adjust temperature in mode %s", self._hvac_mode)
            return

        temp = kwargs.get(ATTR_TEMPERATURE)
        temp_hi = kwargs.get("target_temp_high")
        temp_lo = kwargs.get("target_temp_low")

        # Dual mode: send both
        if self._hvac_mode == HVACMode.HEAT_COOL:
            cmds = []
            hi = temp_hi if temp_hi is not None else temp
            lo = temp_lo if temp_lo is not None else temp
            if hi is not None:
                hi = int(float(hi))
                hi = max(int(float(self._device_data.get("min_limit_heat", 16))),
                         min(hi, int(float(self._device_data.get("max_limit_heat", 32)))))
                cmds.append(("P8", f"{hi}.0"))
                self._target_temp_high = hi
            if lo is not None:
                lo = int(float(lo))
                lo = max(int(float(self._device_data.get("min_limit_cold", 16))),
                         min(lo, int(float(self._device_data.get("max_limit_cold", 32)))))
                cmds.append(("P7", f"{lo}.0"))
                self._target_temp_low = lo
            for c, v in cmds:
                self._send_command(c, v)
            self._refresh_state()
            return

        # Single mode
        if self._hvac_mode == HVACMode.HEAT and temp is not None:
            t = int(float(temp))
            t = max(int(float(self._device_data.get("min_limit_heat", 16))),
                    min(t, int(float(self._device_data.get("max_limit_heat", 32)))))
            self._send_command("P8", f"{t}.0")
            self._target_temp_high = t
            self._refresh_state()
        elif self._hvac_mode == HVACMode.COOL and temp is not None:
            t = int(float(temp))
            t = max(int(float(self._device_data.get("min_limit_cold", 16))),
                    min(t, int(float(self._device_data.get("max_limit_cold", 32)))))
            self._send_command("P7", f"{t}.0")
            self._target_temp_low = t
            self._refresh_state()

    @property
    def fan_speed_range(self):
        """Return valid fan speeds from 1 to availables_speeds."""
        try:
            max_spd = int(self._device_data.get("availables_speeds", 3))
        except ValueError:
            max_spd = 3
        return list(range(1, max_spd + 1))

    def _send_command(self, option, value):
        """Send an event payload to the Airzone API."""
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
            _LOGGER.error("No HA loop; cannot send command.")
