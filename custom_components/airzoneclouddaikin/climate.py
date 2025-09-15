"""Climate platform for DKN Cloud for HASS using the Airzone Cloud API.

Key changes vs previous version:
- Use CoordinatorEntity to avoid I/O in properties and centralize polling.
- Fully async commands (await API), no run_coroutine_threadsafe.
- Optimistic UI with short TTL + delayed refresh after write operations.
- Expose HVAC modes based on device 'modes' bitmask (OFF + supported real modes).
- Remove PIN from device_info for privacy.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Small TTL (seconds) to keep optimistic state before backend confirmation
_OPTIMISTIC_TTL_SEC = 2.5
# Small delay (seconds) before asking the coordinator to refresh after a command
_POST_WRITE_REFRESH_DELAY_SEC = 1.0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the climate platform from a config entry using the DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzoneClimate] = []
    for device_id in list(coordinator.data.keys()):
        entities.append(AirzoneClimate(coordinator, device_id))

    # No update_before_add: entity reads from coordinator snapshot
    async_add_entities(entities)


class AirzoneClimate(CoordinatorEntity, ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    def __init__(self, coordinator, device_id: str) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id

        # Optimistic state (cleared once TTL expires or new data arrives)
        self._optimistic_until: float = 0.0
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_target_temperature: float | None = None
        self._optimistic_fan_mode: str | None = None

        # Static attributes
        device = self._device
        self._attr_unique_id = self._device_id
        self._attr_name = device.get("name") or "Airzone Device"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

    # -----------------------------
    # Helpers
    # -----------------------------
    @property
    def _device(self) -> dict[str, Any]:
        """Return the current device snapshot from the coordinator."""
        return self.coordinator.data.get(self._device_id, {})  # type: ignore[no-any-return]

    def _now(self) -> float:
        return time.monotonic()

    def _optimistic_active(self) -> bool:
        return self._now() < self._optimistic_until

    def _set_optimistic(
        self,
        hvac: HVACMode | None = None,
        target: float | None = None,
        fan: str | None = None,
    ) -> None:
        """Set optimistic fields and TTL."""
        if hvac is not None:
            self._optimistic_hvac_mode = hvac
        if target is not None:
            self._optimistic_target_temperature = target
        if fan is not None:
            self._optimistic_fan_mode = fan
        self._optimistic_until = self._now() + _OPTIMISTIC_TTL_SEC
        # Reflect changes immediately in the UI
        self.async_write_ha_state()

    def _schedule_delayed_refresh(
        self, delay: float = _POST_WRITE_REFRESH_DELAY_SEC
    ) -> None:
        """Schedule a coordinator refresh after a short delay to confirm optimistic changes."""

        async def _do_refresh(_now: Any) -> None:
            await self.coordinator.async_request_refresh()

        async_call_later(self.hass, delay, _do_refresh)

    async def _send_event(self, option: str, value: Any) -> None:
        """Send a command to the device using the events endpoint."""
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API not attached to coordinator; cannot send command.")
            return
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Sending event %s=%s for %s", option, value, self._device_id)
        await api.send_event(payload)

    # -----------------------------
    # Coordinator hooks
    # -----------------------------
    def _handle_coordinator_update(self) -> None:
        """Called by the coordinator when data is refreshed."""
        # Keep optimistic state until TTL expires; otherwise rely on new snapshot.
        # Also update human-friendly name if it changes server-side.
        self._attr_name = self._device.get("name") or self._attr_name
        self.async_write_ha_state()

    # -----------------------------
    # Entity availability/device info
    # -----------------------------
    @property
    def available(self) -> bool:
        """Entity is available if the device exists and has an id."""
        return bool(self._device and self._device.get("id"))

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for the device registry (without exposing the PIN)."""
        dev = self._device
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dev.get("id"))},
            "name": dev.get("name"),
            "manufacturer": "Daikin",
            # Privacy: do not include PIN in model string.
            "model": dev.get("brand") or "Unknown",
            "sw_version": dev.get("firmware") or "Unknown",
        }
        mac = dev.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info

    # -----------------------------
    # HVAC modes & features
    # -----------------------------
    def _supported_modes_from_bitmask(self) -> list[HVACMode]:
        """Compute supported HVAC modes from the 'modes' bitmask, excluding HEAT_COOL."""
        bm = (self._device.get("modes") or "").strip()
        # Expected order: P2=1..8 => COOL, HEAT, FAN_ONLY, HEAT_COOL, DRY, ?, ?, ?
        # We expose only modes proven stable in DKN: COOL/HEAT/FAN_ONLY/DRY.
        supported: list[HVACMode] = []
        if len(bm) >= 5:
            if bm[0] == "1":
                supported.append(HVACMode.COOL)
            if bm[1] == "1":
                supported.append(HVACMode.HEAT)
            if bm[2] == "1":
                supported.append(HVACMode.FAN_ONLY)
            # bm[3] corresponds to HEAT_COOL (auto) -> intentionally not exposed
            if bm[4] == "1":
                supported.append(HVACMode.DRY)
        else:
            # Fallback if bitmask missing/invalid
            supported = [HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY]
        return supported

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of supported HVAC modes (OFF + supported)."""
        return [HVACMode.OFF] + self._supported_modes_from_bitmask()

    # -----------------------------
    # Current state
    # -----------------------------
    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode (OFF if power=0)."""
        if self._optimistic_active() and self._optimistic_hvac_mode is not None:
            return self._optimistic_hvac_mode

        dev = self._device
        power = str(dev.get("power", "0"))
        if power == "0":
            return HVACMode.OFF

        mode_val = str(dev.get("mode", ""))
        if mode_val == "1":
            return HVACMode.COOL
        if mode_val == "2":
            return HVACMode.HEAT
        if mode_val == "3":
            return HVACMode.FAN_ONLY
        if mode_val == "5":
            return HVACMode.DRY
        # Default fallback if API reports an unknown code
        return HVACMode.HEAT

    @property
    def target_temperature(self) -> float | None:
        """Return the current target temperature (single setpoint)."""
        if (
            self._optimistic_active()
            and self._optimistic_target_temperature is not None
        ):
            return self._optimistic_target_temperature

        mode = self.hvac_mode
        dev = self._device
        try:
            if mode == HVACMode.HEAT:
                return float(dev.get("heat_consign"))
            if mode == HVACMode.COOL:
                return float(dev.get("cold_consign"))
        except (TypeError, ValueError):
            return None
        return None

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan speed."""
        if self._optimistic_active() and self._optimistic_fan_mode is not None:
            return self._optimistic_fan_mode

        mode = self.hvac_mode
        dev = self._device
        if mode in (HVACMode.OFF, HVACMode.DRY):
            return None
        if mode in (HVACMode.COOL, HVACMode.FAN_ONLY):
            return str(dev.get("cold_speed", "")) or None
        if mode == HVACMode.HEAT:
            return str(dev.get("heat_speed", "")) or None
        return None

    @property
    def fan_modes(self) -> list[str]:
        """Return a list of valid fan speeds as strings based on 'availables_speeds'."""
        if self.hvac_mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        speeds_str = str(self._device.get("availables_speeds", "3"))
        try:
            speeds = max(1, int(speeds_str))
        except (TypeError, ValueError):
            speeds = 3
        return [str(i) for i in range(1, speeds + 1)]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return supported features according to the current mode."""
        feats = ClimateEntityFeature(0)
        if self.hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
            feats |= ClimateEntityFeature.TARGET_TEMPERATURE
            feats |= ClimateEntityFeature.FAN_MODE
        elif self.hvac_mode == HVACMode.FAN_ONLY:
            feats |= ClimateEntityFeature.FAN_MODE
        return feats

    @property
    def min_temp(self) -> float:
        """Return the minimum allowed temperature for the current mode."""
        dev = self._device
        if self.hvac_mode == HVACMode.HEAT:
            return float(dev.get("min_limit_heat", 16))
        return float(dev.get("min_limit_cold", 16))

    @property
    def max_temp(self) -> float:
        """Return the maximum allowed temperature for the current mode."""
        dev = self._device
        if self.hvac_mode == HVACMode.HEAT:
            return float(dev.get("max_limit_heat", 32))
        return float(dev.get("max_limit_cold", 32))

    # -----------------------------
    # Write operations (async + optimistic + delayed refresh)
    # -----------------------------
    async def async_turn_on(self) -> None:
        """Turn on the device by sending P1=1."""
        await self._send_event("P1", 1)
        # Keep the current non-OFF mode if known; otherwise default to HEAT (safe fallback).
        next_mode = self.hvac_mode if self.hvac_mode != HVACMode.OFF else HVACMode.HEAT
        self._set_optimistic(hvac=next_mode)
        self._schedule_delayed_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the device by sending P1=0."""
        await self._send_event("P1", 0)
        self._set_optimistic(hvac=HVACMode.OFF, target=None, fan=None)
        self._schedule_delayed_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode via P2 mapping (OFF handled by P1)."""
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return

        # Ensure power on first
        if self.hvac_mode == HVACMode.OFF:
            await self._send_event("P1", 1)

        mode_mapping: dict[HVACMode, str] = {
            HVACMode.COOL: "1",
            HVACMode.HEAT: "2",
            HVACMode.FAN_ONLY: "3",
            HVACMode.DRY: "5",
        }
        if hvac_mode not in mode_mapping:
            _LOGGER.error("Unsupported HVAC mode: %s", hvac_mode)
            return

        await self._send_event("P2", mode_mapping[hvac_mode])

        # On DRY, temperature control is disabled; on FAN_ONLY too.
        target_opt = (
            None
            if hvac_mode in (HVACMode.DRY, HVACMode.FAN_ONLY)
            else self.target_temperature
        )
        fan_opt = None if hvac_mode == HVACMode.DRY else self.fan_mode

        self._set_optimistic(hvac=hvac_mode, target=target_opt, fan=fan_opt)
        self._schedule_delayed_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature (P7 for COOL, P8 for HEAT)."""
        mode = self.hvac_mode
        if mode in (HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.OFF):
            _LOGGER.warning("Temperature adjustment not supported in mode %s", mode)
            return

        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        try:
            # API expects "XX.0" (integer with .0)
            temp_int = int(float(temp))
        except (TypeError, ValueError):
            _LOGGER.error("Invalid target temperature: %s", temp)
            return

        # Clamp to device limits
        if mode == HVACMode.HEAT:
            vmin, vmax, cmd = int(self.min_temp), int(self.max_temp), "P8"
        else:  # COOL
            vmin, vmax, cmd = int(self.min_temp), int(self.max_temp), "P7"

        temp_int = max(vmin, min(vmax, temp_int))
        await self._send_event(cmd, f"{temp_int}.0")

        self._set_optimistic(target=float(temp_int))
        self._schedule_delayed_refresh()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan speed via P3 (cool/fan) or P4 (heat)."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            _LOGGER.warning("Fan speed adjustment not supported in mode %s", mode)
            return

        try:
            speed = int(fan_mode)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid fan speed: %s", fan_mode)
            return

        valid = self.fan_modes
        if fan_mode not in valid:
            _LOGGER.error("Fan speed %s not in valid range %s", fan_mode, valid)
            return

        cmd = "P3" if mode in (HVACMode.COOL, HVACMode.FAN_ONLY) else "P4"
        await self._send_event(cmd, speed)

        self._set_optimistic(fan=str(speed))
        self._schedule_delayed_refresh()
