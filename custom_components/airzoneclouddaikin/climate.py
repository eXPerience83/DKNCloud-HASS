"""Climate platform for DKN Cloud for HASS (Airzone Cloud).

This entity follows HA async patterns and uses DataUpdateCoordinator.
Fixes a regression where `supported_features` returned a plain int, causing:
TypeError: argument of type 'int' is not iterable

Key behaviors:
- Expose COOL/HEAT/FAN_ONLY/DRY (no AUTO/HEAT_COOL until proven stable).
- Dynamic features:
    * COOL/HEAT: TARGET_TEMPERATURE + FAN_MODE
    * FAN_ONLY:  FAN_MODE
    * DRY/OFF:   none
- Fan speeds derived from `availables_speeds` (numeric strings: "1".."N").
- Optimistic UI updates with short delayed refresh after commands.
- No I/O in properties; all I/O via coordinator.api and coordinator refresh.

Privacy:
- Do not expose secrets (email/token/MAC/PIN) in logs or device_info.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Airzone mode codes observed in API:
# 1: COOL, 2: HEAT, 3: FAN_ONLY, 5: DRY
MODE_TO_HVAC: dict[str, HVACMode] = {
    "1": HVACMode.COOL,
    "2": HVACMode.HEAT,
    "3": HVACMode.FAN_ONLY,
    "5": HVACMode.DRY,
}
HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.COOL: "1",
    HVACMode.HEAT: "2",
    HVACMode.FAN_ONLY: "3",
    HVACMode.DRY: "5",
}

_OPTIMISTIC_TTL_SEC: float = 2.5
_POST_WRITE_REFRESH_DELAY_SEC: float = 1.0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the climate platform from a config entry using the coordinator snapshot."""
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

    # Entities read from coordinator snapshot; no need to update before add
    async_add_entities(entities)


class AirzoneClimate(CoordinatorEntity, ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    _attr_has_entity_name = True
    _attr_precision = PRECISION_WHOLE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, device_id: str) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._optimistic_expires: float | None = None
        self._optimistic: dict[str, Any] = {}  # temporary values until refresh

        device = self._device
        name = device.get("name") or "Airzone Device"
        self._attr_name = name
        self._attr_unique_id = f"{self._device_id}_climate"

    # ---- Helpers ---------------------------------------------------------

    @property
    def _device(self) -> dict[str, Any]:
        """Return latest device snapshot from the coordinator."""
        return self.coordinator.data.get(self._device_id, {})  # type: ignore[no-any-return]

    def _fan_speed_max(self) -> int:
        """Max available fan speed based on 'availables_speeds'."""
        try:
            n = int(self._device.get("availables_speeds") or 0)
            return max(0, n)
        except Exception:
            return 0

    def _device_power_on(self) -> bool:
        """Return True if backend reports power on (best-effort)."""
        p = self._optimistic.get("power")
        if p is not None:
            return bool(p)
        return str(self._device.get("power") or "").lower() in ("1", "on", "true")

    def _backend_mode_code(self) -> str | None:
        """Return backend mode code as str ('1','2','3','5') if present."""
        m = self._optimistic.get("mode")
        if m is not None:
            return str(m)
        raw = self._device.get("mode")
        return str(raw) if raw is not None else None

    def _hvac_from_device(self) -> HVACMode:
        """Derive HVAC mode based on power + mode code."""
        if not self._device_power_on():
            return HVACMode.OFF
        code = self._backend_mode_code()
        return MODE_TO_HVAC.get(code or "", HVACMode.OFF)

    # ---- Entity metadata -------------------------------------------------

    @property
    def device_info(self):
        """Attach model/firmware; avoid exposing sensitive IDs."""
        dev = self._device
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "manufacturer": "Daikin / Airzone",
            "model": dev.get("brand") or "Airzone DKN",
            "sw_version": dev.get("firmware") or "",
            "name": dev.get("name") or "Airzone Device",
        }

    # ---- Core state ------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode:
        """Current HVAC mode derived from snapshot (OFF if power is off)."""
        return self._hvac_from_device()

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Expose supported modes based on optional 'modes' bitmask, else defaults."""
        modes = [HVACMode.OFF]
        bitmask = self._device.get("modes")
        # NOTE: When 'modes' is provided, prefer it; otherwise, expose all real modes.
        try:
            bm = int(bitmask) if bitmask is not None else None
        except Exception:
            bm = None

        def add(mode: HVACMode, bit: int | None):
            if bm is None or (bit is not None and bm & bit):
                modes.append(mode)

        # Assuming bit assignment 1<<0 COOL, 1<<1 HEAT, 1<<2 FAN_ONLY, 1<<3 DRY if provided
        add(HVACMode.COOL, 1 << 0)
        add(HVACMode.HEAT, 1 << 1)
        add(HVACMode.FAN_ONLY, 1 << 2)
        add(HVACMode.DRY, 1 << 3)
        return modes

    # ---- Temperature control --------------------------------------------

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature according to current mode."""
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            val = self._optimistic.get("cold_consign", self._device.get("cold_consign"))
        elif mode == HVACMode.HEAT:
            val = self._optimistic.get("heat_consign", self._device.get("heat_consign"))
        else:
            return None
        try:
            return int(val)  # show whole degrees in UI
        except Exception:
            return None

    @property
    def min_temp(self) -> float:
        """Return min allowable temp for current mode (fallback 16)."""
        mode = self.hvac_mode
        key = "min_limit_cold" if mode == HVACMode.COOL else "min_limit_heat"
        try:
            return int(self._device.get(key) or 16)
        except Exception:
            return 16

    @property
    def max_temp(self) -> float:
        """Return max allowable temp for current mode (fallback 30)."""
        mode = self.hvac_mode
        key = "max_limit_cold" if mode == HVACMode.COOL else "max_limit_heat"
        try:
            return int(self._device.get(key) or 30)
        except Exception:
            return 30

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature using P7 (cool) or P8 (heat) depending on mode."""
        if ATTR_TEMPERATURE not in kwargs:
            return
        temp = int(kwargs[ATTR_TEMPERATURE])
        mode = self.hvac_mode
        if mode not in (HVACMode.COOL, HVACMode.HEAT):
            _LOGGER.debug("Ignoring set_temperature in mode %s", mode)
            return

        # Clamp to device limits
        temp = max(int(self.min_temp), min(int(self.max_temp), temp))
        payload: dict[str, Any]
        if mode == HVACMode.COOL:
            payload = {"event": {"P7": f"{temp}.0"}}
            self._optimistic["cold_consign"] = temp
        else:
            payload = {"event": {"P8": f"{temp}.0"}}
            self._optimistic["heat_consign"] = temp

        await self._send_event(payload)
        # Optimistic ttl
        self._optimistic_expires = self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Fan control -----------------------------------------------------

    @property
    def fan_modes(self) -> list[str] | None:
        """Return list of fan speeds as numeric strings."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        n = self._fan_speed_max()
        return [str(i) for i in range(1, n + 1)] if n > 0 else []

    @property
    def fan_mode(self) -> str | None:
        """Return current speed (numeric string), if applicable."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            return None
        key = "heat_speed" if mode == HVACMode.HEAT else "cold_speed"
        val = self._optimistic.get(key, self._device.get(key))
        return str(val) if val else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan speed via P3 (cool/fan) or P4 (heat)."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            _LOGGER.debug("Ignoring set_fan_mode in mode %s", mode)
            return
        # Validate value
        if fan_mode not in (self.fan_modes or []):
            _LOGGER.debug("Invalid fan_mode %s (allowed %s)", fan_mode, self.fan_modes)
            return

        payload: dict[str, Any]
        if mode == HVACMode.HEAT:
            payload = {"event": {"P4": fan_mode}}
            self._optimistic["heat_speed"] = fan_mode
        else:
            # COOL or FAN_ONLY
            payload = {"event": {"P3": fan_mode}}
            self._optimistic["cold_speed"] = fan_mode

        await self._send_event(payload)
        self._optimistic_expires = self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Power / mode ----------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode (power off/on + P2 as needed)."""
        if hvac_mode == HVACMode.OFF:
            await self._send_event({"event": {"P1": "0"}})
            self._optimistic.update({"power": "0"})
        else:
            # Ensure power on then set P2
            if not self._device_power_on():
                await self._send_event({"event": {"P1": "1"}})
                self._optimistic.update({"power": "1"})
            mode_code = HVAC_TO_MODE.get(hvac_mode)
            if mode_code:
                await self._send_event({"event": {"P2": mode_code}})
                self._optimistic.update({"mode": mode_code})

        self._optimistic_expires = self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Features (critical fix) ----------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return IntFlag capabilities; NEVER return a plain int.

        This avoids TypeError: 'int' is not iterable in HA when it checks:
        `if ClimateEntityFeature.X in supported_features: ...`
        """
        feats: ClimateEntityFeature = ClimateEntityFeature(0)  # IntFlag accumulator
        mode = self.hvac_mode
        if mode in (HVACMode.COOL, HVACMode.HEAT):
            feats |= ClimateEntityFeature.TARGET_TEMPERATURE
            feats |= ClimateEntityFeature.FAN_MODE
        elif mode == HVACMode.FAN_ONLY:
            feats |= ClimateEntityFeature.FAN_MODE
        # DRY/OFF: no fan/temperature features
        return feats

    # ---- Write helpers ---------------------------------------------------

    async def _send_event(self, payload: dict[str, Any]) -> None:
        """POST /events with standard headers for event commands (P1..P8)."""
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API handle missing in coordinator; cannot send_event")
            return
        try:
            await api.send_event(payload)
        except Exception as err:
            _LOGGER.warning("Failed to send_event %s: %s", payload, err)

    def _schedule_refresh(self) -> None:
        """Schedule a short delayed refresh after write operations."""
        async def _refresh_cb(_now):
            try:
                await self.coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.debug("Refresh after write failed: %s", err)

        async_call_later(self.hass, _POST_WRITE_REFRESH_DELAY_SEC, _refresh_cb)

    # ---- Availability / update ------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._device)

    async def async_update(self) -> None:
        # Entity pulls from coordinator; no direct I/O.
        # Clean optimistic state once TTL expires.
        if self._optimistic_expires and self.coordinator.hass.loop.time() > self._optimistic_expires:
            self._optimistic.clear()
            self._optimistic_expires = None
