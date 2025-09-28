"""Home Assistant climate entity for DKN Cloud (Airzone Cloud).

Key behaviors (concise):
- Coordinator-based: no I/O inside properties; writes go via /events and a short refresh.
- Integer-only setpoints: precision = whole degrees, target_temperature_step = 1.0 °C.
- Supported HVAC modes: COOL / HEAT / FAN_ONLY / DRY (no AUTO/HEAT_COOL for now).
- API mapping: P1=power, P2=mode, P7/P8=setpoint (cool/heat), P3/P4=fan (cool/heat).
- Ventilate policy: prefer P2=3 if supported; else P2=8; else do not expose FAN_ONLY.
- Privacy: never log or expose secrets (email/token/MAC/PIN).
"""

from __future__ import annotations

import asyncio
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
# 1: COOL, 2: HEAT, 3: FAN_ONLY (ventilate cold-type), 5: DRY, 8: FAN_ONLY (ventilate heat-type)
MODE_TO_HVAC: dict[str, HVACMode] = {
    "1": HVACMode.COOL,
    "2": HVACMode.HEAT,
    "3": HVACMode.FAN_ONLY,
    "5": HVACMode.DRY,
    "8": HVACMode.FAN_ONLY,  # treat P2=8 as fan-only (ventilate variant)
}
# NOTE: We do not use HVAC_TO_MODE blindly for FAN_ONLY; we choose 3/8 by supported bitmask.
HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.COOL: "1",
    HVACMode.HEAT: "2",
    HVACMode.FAN_ONLY: "3",  # default; actual send uses _preferred_ventilate_code()
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
    for device_id in list((coordinator.data or {}).keys()):
        entities.append(AirzoneClimate(coordinator, device_id))

    async_add_entities(entities)


class AirzoneClimate(CoordinatorEntity, ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    _attr_has_entity_name = True
    _attr_precision = PRECISION_WHOLE  # UI precision: show whole degrees only
    _attr_target_temperature_step = 1.0  # UI step: force 1°C increments
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
        return (self.coordinator.data or {}).get(self._device_id, {})  # type: ignore[no-any-return]

    def _fan_speed_max(self) -> int:
        """Max available fan speed based on 'availables_speeds'."""
        try:
            n = int(self._device.get("availables_speeds") or 0)
            return max(0, n)
        except Exception:
            return 0

    def _device_power_on(self) -> bool:
        """Return True if backend or optimistic state reports power ON.

        This method NORMALIZES values so "0"/0/"off"/False are False and "1"/1/"on"/True are True.
        """
        p = self._optimistic.get("power")
        if p is None:
            p = self._device.get("power")

        s = str(p).strip().lower()
        if s in ("1", "on", "true", "yes"):
            return True
        if s in ("0", "off", "false", "no", "", "none"):
            return False

        if isinstance(p, bool):
            return p
        try:
            return bool(int(p))
        except Exception:
            return False

    def _backend_mode_code(self) -> str | None:
        """Return backend mode code as str (e.g. '1','2','3','5','8') if present."""
        m = self._optimistic.get("mode")
        if m is not None:
            return str(m)
        raw = self._device.get("mode")
        return str(raw) if raw is not None else None

    def _modes_bitstring(self) -> str:
        """Return the device 'modes' capability bitstring or empty string."""
        raw = self._device.get("modes")
        bitstr = str(raw) if raw is not None else ""
        if bitstr and all(ch in "01" for ch in bitstr):
            return bitstr
        return ""

    def _supports_p2_value(self, code: int) -> bool:
        """True if 'modes' bitstring indicates support for given P2 code."""
        bitstr = self._modes_bitstring()
        idx = code - 1
        return bool(bitstr and idx >= 0 and len(bitstr) > idx and bitstr[idx] == "1")

    def _preferred_ventilate_code(self) -> str | None:
        """Choose P2 value for FAN_ONLY: prefer 3, else 8, else None."""
        if self._supports_p2_value(3):
            return "3"
        if self._supports_p2_value(8):
            return "8"
        # No ventilate supported
        return None

    def _hvac_from_device(self) -> HVACMode:
        """Derive HVAC mode based on power + mode code."""
        if not self._device_power_on():
            return HVACMode.OFF
        code = self._backend_mode_code()
        return MODE_TO_HVAC.get(code or "", HVACMode.OFF)

    # ---- Device info -----------------------------------------------------

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
        """Expose supported modes based on optional 'modes' bitstring.

        Bit positions (0-based):
          0 -> P2=1 (COOL)
          1 -> P2=2 (HEAT)
          2 -> P2=3 (FAN_ONLY variant)
          3 -> P2=4 (AUTO/HEAT_COOL)   [not exposed]
          4 -> P2=5 (DRY)
          5 -> P2=6 (unused here)
          6 -> P2=7 (unused here)
          7 -> P2=8 (FAN_ONLY variant)
        """
        modes = [HVACMode.OFF]
        bitstr = self._modes_bitstring()
        if bitstr:
            if len(bitstr) >= 1 and bitstr[0] == "1":
                modes.append(HVACMode.COOL)
            if len(bitstr) >= 2 and bitstr[1] == "1":
                modes.append(HVACMode.HEAT)
            # FAN_ONLY if P2=3 or P2=8 supported
            fan_ok = (len(bitstr) >= 3 and bitstr[2] == "1") or (
                len(bitstr) >= 8 and bitstr[7] == "1"
            )
            if fan_ok:
                modes.append(HVACMode.FAN_ONLY)
            if len(bitstr) >= 5 and bitstr[4] == "1":
                modes.append(HVACMode.DRY)
            return modes

        # Fallback when bitstring missing/invalid: expose common real modes.
        modes.extend([HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY])
        return modes

    # ---- Temperature control --------------------------------------------

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        return UnitOfTemperature.CELSIUS

    @staticmethod
    def _parse_float(val: Any) -> float | None:
        """Parse a backend numeric value that may come as '24.0' or '23,5'."""
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "."))
        except Exception:
            return None

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature according to current mode."""
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            val = self._optimistic.get("cold_consign", self._device.get("cold_consign"))
        elif mode == HVACMode.HEAT:
            val = self._optimistic.get("heat_consign", self._device.get("heat_consign"))
        else:
            # DRY or FAN_ONLY: no target temperature
            return None
        return self._parse_float(val)

    @property
    def min_temp(self) -> float:
        """Return min allowable temp for current mode (fallback 16)."""
        mode = self.hvac_mode
        key = "min_limit_cold" if mode == HVACMode.COOL else "min_limit_heat"
        val = self._parse_float(self._device.get(key))
        return val if val is not None else 16.0

    @property
    def max_temp(self) -> float:
        """Return max allowable temp for current mode (fallback 32)."""
        mode = self.hvac_mode
        key = "max_limit_cold" if mode == HVACMode.COOL else "max_limit_heat"
        val = self._parse_float(self._device.get(key))
        return val if val is not None else 32.0

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature using P7 (cool) or P8 (heat) depending on mode."""
        if ATTR_TEMPERATURE not in kwargs:
            return
        try:
            requested = float(kwargs[ATTR_TEMPERATURE])
        except (TypeError, ValueError):
            return

        mode = self.hvac_mode
        if mode not in (HVACMode.COOL, HVACMode.HEAT):
            _LOGGER.debug("Ignoring set_temperature in mode %s", mode)
            return

        # Clamp to device limits (use integer degrees as device expects)
        min_allowed = int(self.min_temp)
        max_allowed = int(self.max_temp)
        temp = max(min_allowed, min(max_allowed, int(round(requested))))

        if mode == HVACMode.COOL:
            await self._send_p_event("P7", f"{temp}.0")
            self._optimistic["cold_consign"] = temp
        else:
            await self._send_p_event("P8", f"{temp}.0")
            self._optimistic["heat_consign"] = temp

        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        )
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
        """Return current speed (numeric string), if applicable.

        FAN_ONLY routing:
        - If backend P2 is 3: use cold_speed
        - If backend P2 is 8: use heat_speed
        """
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            return None

        if mode == HVACMode.HEAT:
            key = "heat_speed"
        elif mode == HVACMode.COOL:
            key = "cold_speed"
        else:  # FAN_ONLY
            code = self._backend_mode_code()
            key = "heat_speed" if code == "8" else "cold_speed"

        val = self._optimistic.get(key, self._device.get(key))
        return str(val) if val else None

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan speed via P3 (cold/fan-only: code 3) or P4 (heat/fan-only: code 8)."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            _LOGGER.debug("Ignoring set_fan_mode in mode %s", mode)
            return
        if fan_mode not in (self.fan_modes or []):
            _LOGGER.debug("Invalid fan_mode %s (allowed %s)", fan_mode, self.fan_modes)
            return

        option: str
        key: str
        if mode == HVACMode.HEAT:
            option = "P4"
            key = "heat_speed"
        elif mode == HVACMode.COOL:
            option = "P3"
            key = "cold_speed"
        else:
            # FAN_ONLY: route based on backend P2 code (3 -> P3/cold, 8 -> P4/heat)
            code = self._backend_mode_code()
            if code == "8":
                option = "P4"
                key = "heat_speed"
            else:
                option = "P3"
                key = "cold_speed"

        await self._send_p_event(option, fan_mode)
        self._optimistic[key] = fan_mode

        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Power / mode ----------------------------------------------------

    async def async_turn_on(self) -> None:
        """Power ON via P1=1 (explicit user action from Climate card)."""
        await self._send_p_event("P1", 1)
        self._optimistic.update({"power": "1"})
        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    async def async_turn_off(self) -> None:
        """Power OFF via P1=0 (explicit user action from Climate card)."""
        await self._send_p_event("P1", 0)
        self._optimistic.update({"power": "0"})
        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode (power off/on + P2 as needed)."""
        if hvac_mode == HVACMode.OFF:
            await self._send_p_event("P1", 0)
            self._optimistic.update({"power": "0"})
            self._optimistic_expires = (
                self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
            )
            self.async_write_ha_state()
            self._schedule_refresh()
            return

        # Ensure power ON then set P2 (auto-on only when changing mode)
        if not self._device_power_on():
            await self._send_p_event("P1", 1)

        if hvac_mode == HVACMode.FAN_ONLY:
            # Ventilate policy: prefer P2=3; else P2=8; else default "3" if unknown
            code = self._preferred_ventilate_code() or "3"
            await self._send_p_event("P2", code)
            self._optimistic.update({"power": "1", "mode": code})
        else:
            mode_code = HVAC_TO_MODE.get(hvac_mode)
            if mode_code:
                await self._send_p_event("P2", mode_code)
                self._optimistic.update({"power": "1", "mode": mode_code})

        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + _OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Features --------------------------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return IntFlag capabilities; NEVER return a plain int."""
        feats: ClimateEntityFeature = ClimateEntityFeature(0)
        mode = self.hvac_mode
        if mode in (HVACMode.COOL, HVACMode.HEAT):
            feats |= ClimateEntityFeature.TARGET_TEMPERATURE
            feats |= ClimateEntityFeature.FAN_MODE
        elif mode == HVACMode.FAN_ONLY:
            feats |= ClimateEntityFeature.FAN_MODE
        # DRY/OFF: no fan/temperature features
        return feats

    # ---- Write helpers ---------------------------------------------------

    async def _send_p_event(self, option: str, value: Any) -> None:
        """Send a P# command using the canonical 'modmaquina' payload.

        {"event":{"cgi":"modmaquina","device_id":<id>,"option":"P#","value":...}}
        """
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API handle missing in coordinator; cannot send_event")
            return

        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        try:
            await api.send_event(payload)
        except asyncio.CancelledError:
            # Propagate cancellations cleanly.
            raise
        except Exception as err:
            # Do not swallow errors: let callers decide (UI should reflect failure).
            _LOGGER.warning("Failed to send_event %s=%s: %s", option, value, err)
            raise

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
        """Clear optimistic cache after TTL; data comes from coordinator."""
        if (
            self._optimistic_expires
            and self.coordinator.hass.loop.time() > self._optimistic_expires
        ):
            self._optimistic.clear()
            self._optimistic_expires = None
