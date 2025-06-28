"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API with DataUpdateCoordinator."""

import logging
import hashlib
from homeassistant.helpers.update_coordinator import CoordinatorEntity, callback
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
    for device_id, device in coordinator.data.items():
        entities.append(AirzoneClimate(coordinator, api, device, config, hass))
    async_add_entities(entities, True)


class AirzoneClimate(CoordinatorEntity, ClimateEntity):
    """
    Representation of an Airzone Cloud Daikin climate device.

    Supports all HVAC modes: COOL, HEAT, FAN_ONLY, DRY, and OFF.
    Only single setpoint is supported (either heat or cool).
    """

    SUPPORTED_MODES = [
        HVACMode.OFF,
        HVACMode.COOL,
        HVACMode.HEAT,
        HVACMode.FAN_ONLY,
        HVACMode.DRY,
    ]

    def __init__(self, coordinator, api, device_data: dict, config: dict, hass):
        """Initialize the climate entity. Ensure unique_id is always valid."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._api = api

        # --- Robust assignment for device_id/unique_id ---
        device_id = device_data.get("id")
        if not device_id or not str(device_id).strip():
            # fallback: use a deterministic hash of device_data
            device_id = hashlib.sha256(str(device_data).encode("utf-8")).hexdigest()
            _LOGGER.warning(
                "Device with missing or empty 'id'. Generated fallback id: %s", device_id
            )
        self._device_id = str(device_id)
        self._attr_unique_id = self._device_id

        self._config = config
        self.hass = hass
        self._attr_name = device_data.get("name", "Airzone Device")
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._hvac_mode = HVACMode.OFF
        self._target_temperature = None
        self._fan_mode = None

    @property
    def _device_data(self):
        """Helper to get latest device data from the coordinator."""
        return self.coordinator.data.get(self._device_id, {})

    @property
    def hvac_modes(self):
        """Return the list of supported HVAC modes."""
        return self.SUPPORTED_MODES

    @property
    def hvac_mode(self):
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self):
        """Return the current target temperature (single setpoint)."""
        if self._hvac_mode == HVACMode.HEAT:
            return self._device_data.get("heat_consign")
        if self._hvac_mode == HVACMode.COOL:
            return self._device_data.get("cold_consign")
        return None

    @property
    def supported_features(self):
        """
        Return supported features:
        - TARGET_TEMPERATURE in HEAT or COOL mode
        - FAN_MODE in all modes except OFF/DRY
        """
        features = 0
        if self._hvac_mode in [HVACMode.HEAT, HVACMode.COOL]:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._hvac_mode not in [HVACMode.OFF, HVACMode.DRY]:
            features |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def min_temp(self):
        """Return the minimum allowed temperature for the current mode."""
        if self._hvac_mode == HVACMode.HEAT:
            return float(self._device_data.get("min_limit_heat", 16))
        return float(self._device_data.get("min_limit_cold", 16))

    @property
    def max_temp(self):
        """Return the maximum allowed temperature for the current mode."""
        if self._hvac_mode == HVACMode.HEAT:
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
        # Use cold_speed for COOL/FAN_ONLY, heat_speed for HEAT
        if self._hvac_mode == HVACMode.HEAT:
            return str(self._device_data.get("heat_speed", ""))
        else:
            return str(self._device_data.get("cold_speed", ""))

    @property
    def device_info(self):
        """Return device info to link this entity to a device in Home Assistant."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand', 'Unknown')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware", "Unknown"),
            "connections": {("mac", self._device_data.get("mac"))} if self._device_data.get("mac") else None,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """
        Called by the coordinator when new data has arrived.
        Updates the local state and notifies HA.
        """
        data = self._device_data
        if not data:
            return

        power = int(data.get("power", 0))
        if power:
            mode_val = data.get("mode")
            if mode_val == "1":
                self._hvac_mode = HVACMode.COOL
            elif mode_val == "2":
                self._hvac_mode = HVACMode.HEAT
            elif mode_val == "3":
                self._hvac_mode = HVACMode.FAN_ONLY
            elif mode_val == "5":
                self._hvac_mode = HVACMode.DRY
            else:
                self._hvac_mode = HVACMode.HEAT  # Default to HEAT if unknown
        else:
            self._hvac_mode = HVACMode.OFF

        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode):
        """Set the fan mode asynchronously."""
        await self.hass.async_add_executor_job(self.set_fan_mode, fan_mode)

    def set_fan_mode(self, fan_mode):
        """
        Set the fan mode.
        In COOL and FAN_ONLY modes, uses P3.
        In HEAT mode, uses P4.
        In DRY or OFF mode, fan speed adjustments are disabled.
        """
        try:
            speed = int(fan_mode)
        except ValueError:
            _LOGGER.error("Invalid fan speed: %s", fan_mode)
            return
        if speed not in self.fan_speed_range:
            _LOGGER.error("Fan speed %s not in valid range %s", speed, self.fan_speed_range)
            return

        if self._hvac_mode in [HVACMode.COOL, HVACMode.FAN_ONLY]:
            self._send_command("P3", speed)
        elif self._hvac_mode == HVACMode.HEAT:
            self._send_command("P4", speed)
        else:
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", self._hvac_mode)
            return
        self._refresh_state()

    def _refresh_state(self):
        """Schedule an immediate refresh of coordinator data using async_create_task."""
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    def turn_on(self):
        """Turn on the device by sending P1=1 and refresh state."""
        self._send_command("P1", 1)
        self._refresh_state()

    def turn_off(self):
        """Turn off the device by sending P1=0 and refresh state."""
        self._send_command("P1", 0)
        self._hvac_mode = HVACMode.OFF
        self._refresh_state()

    def set_hvac_mode(self, hvac_mode):
        """
        Set the HVAC mode.
        Mapping:
         - HVACMode.OFF: call turn_off() and return.
         - HVACMode.COOL -> P2=1
         - HVACMode.HEAT -> P2=2
         - HVACMode.FAN_ONLY -> P2=3
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
            HVACMode.DRY: "5",
        }
        if hvac_mode in mode_mapping:
            self._send_command("P2", mode_mapping[hvac_mode])
            self._refresh_state()
        else:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)

    def set_temperature(self, **kwargs):
        """
        Set the target temperature.
        In HEAT or COOL mode: adjusts single setpoint (P8 for HEAT, P7 for COOL).
        Temperature adjustments are disabled in DRY and FAN_ONLY modes.
        The value is constrained to the device limits and sent as an integer with '.0' appended.
        """
        if self._hvac_mode in [HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.OFF]:
            _LOGGER.warning("Temperature adjustment not supported in mode %s", self._hvac_mode)
            return

        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            temp = int(float(temp))
            if self._hvac_mode == HVACMode.HEAT:
                min_temp = int(float(self._device_data.get("min_limit_heat", 16)))
                max_temp = int(float(self._device_data.get("max_limit_heat", 32)))
                temp = max(min_temp, min(temp, max_temp))
                self._send_command("P8", f"{temp}.0")
                self._refresh_state()
            elif self._hvac_mode == HVACMode.COOL:
                min_temp = int(float(self._device_data.get("min_limit_cold", 16)))
                max_temp = int(float(self._device_data.get("max_limit_cold", 32)))
                temp = max(min_temp, min(temp, max_temp))
                self._send_command("P7", f"{temp}.0")
                self._refresh_state()

    @property
    def fan_speed_range(self):
        """
        Return a list of valid fan speeds based on 'availables_speeds' from device data.
        """
        speeds_str = self._device_data.get("availables_speeds", "3")
        try:
            speeds = int(speeds_str)
        except (ValueError, TypeError):
            speeds = 3
        return list(range(1, speeds + 1))

    def _send_command(self, option, value):
        """
        Send a command to the device using the events endpoint.
        """
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.info("Sending command: %s", payload)
        if self.hass and self.hass.loop:
            self.hass.async_create_task(self._api.send_event(payload))
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
