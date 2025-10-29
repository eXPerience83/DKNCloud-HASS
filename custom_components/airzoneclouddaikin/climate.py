"""Home Assistant climate entity for DKN Cloud (Airzone Cloud).

P4-B changes in this revision (0.4.0):
- BREAKING: Use native `preset_modes` (home/away/sleep) instead of exposing a
  separate select.scenary entity. Preset ↔ scenary mapping is handled internally.
- REFACTOR: Return a `DeviceInfo` object from `device_info` for forward compatibility.
- CONSISTENCY: Keep explicit TURN_ON/TURN_OFF advertised in supported_features.

Key behaviors (concise):
- Coordinator-based: no I/O in properties; writes go via /events and short refresh.
- Integer-only setpoints: precision = whole degrees, target_temperature_step = 1.0 °C.
- Supported HVAC modes: COOL / HEAT / FAN_ONLY / DRY (no AUTO/HEAT_COOL for now).
- API mapping: P1=power, P2=mode, P7/P8=setpoint (cool/heat), P3/P4=fan (cool/heat).
- Ventilate policy: prefer P2=3 if supported; else P2=8; else do not expose FAN_ONLY.
- Privacy: never log or expose secrets (email/token/MAC/PIN).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .__init__ import AirzoneCoordinator
from .const import (
    DOMAIN,
    MANUFACTURER,
    OPTIMISTIC_TTL_SEC,
    POST_WRITE_REFRESH_DELAY_SEC,
)

_LOGGER = logging.getLogger(__name__)

# Airzone mode codes observed in API:
# 1: COOL, 2: HEAT, 3: FAN_ONLY (ventilate cold-type), 5: DRY, 8: FAN_ONLY (ventilate heat-type)
MODE_TO_HVAC: dict[str, HVACMode] = {
    "1": HVACMode.COOL,
    "2": HVACMode.HEAT,
    "3": HVACMode.FAN_ONLY,
    "5": HVACMode.DRY,
    "8": HVACMode.FAN_ONLY,
}
# NOTE: For FAN_ONLY we pick P2=3 or P2=8 dynamically via _preferred_ventilate_code().
HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.COOL: "1",
    HVACMode.HEAT: "2",
    HVACMode.FAN_ONLY: "3",  # default; real send uses _preferred_ventilate_code()
    HVACMode.DRY: "5",
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the climate platform from a config entry using the coordinator snapshot."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator: AirzoneCoordinator | None = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzoneClimate] = [
        AirzoneClimate(coordinator, device_id)
        for device_id in list((coordinator.data or {}).keys())
    ]
    async_add_entities(entities)


class AirzoneClimate(CoordinatorEntity[AirzoneCoordinator], ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    _attr_has_entity_name = True
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1.0
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: AirzoneCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._optimistic_expires: float | None = None
        self._optimistic: dict[str, Any] = {}
        self._cancel_scheduled_refresh: Callable[[], None] | None = None

        device = self._device
        name = device.get("name") or "Airzone Device"
        self._attr_name = name
        self._attr_unique_id = f"{self._device_id}_climate"

    # ---- Helpers ---------------------------------------------------------

    @property
    def _device(self) -> dict[str, Any]:
        """Latest device snapshot (no I/O)."""
        return (self.coordinator.data or {}).get(self._device_id, {})  # type: ignore[no-any-return]

    def _fan_speed_max(self) -> int:
        try:
            n = int(self._device.get("availables_speeds") or 0)
            return max(0, n)
        except Exception:
            return 0

    def _use_normalized_fan_labels(self) -> bool:
        """Return True when we should expose low/medium/high instead of numeric."""
        return self._fan_speed_max() == 3

    @staticmethod
    def _num_to_label(num: str) -> str:
        """Map '1'/'2'/'3' to 'low'/'medium'/'high' (fallback to input if unknown)."""
        mapping = {"1": "low", "2": "medium", "3": "high"}
        return mapping.get(str(num), str(num))

    @staticmethod
    def _label_to_num(label: str) -> str:
        """Map 'low'/'medium'/'high' to '1'/'2'/'3' (fallback to input if unknown)."""
        norm = (label or "").strip().lower()
        mapping = {"low": "1", "medium": "2", "high": "3"}
        return mapping.get(norm, label)

    def _device_power_on(self) -> bool:
        """Normalize backend/optimistic power to bool."""
        p = self._optimistic.get("power", self._device.get("power"))
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
        m = self._optimistic.get("mode")
        if m is not None:
            return str(m)
        raw = self._device.get("mode")
        return str(raw) if raw is not None else None

    def _modes_bitstring(self) -> str:
        raw = self._device.get("modes")
        bitstr = str(raw) if raw is not None else ""
        if bitstr and all(ch in "01" for ch in bitstr):
            return bitstr
        return ""

    def _supports_p2_value(self, code: int) -> bool:
        bitstr = self._modes_bitstring()
        idx = code - 1
        return bool(bitstr and idx >= 0 and len(bitstr) > idx and bitstr[idx] == "1")

    def _preferred_ventilate_code(self) -> str | None:
        if self._supports_p2_value(3):
            return "3"
        if self._supports_p2_value(8):
            return "8"
        return None

    def _hvac_from_device(self) -> HVACMode:
        if not self._device_power_on():
            return HVACMode.OFF
        code = self._backend_mode_code()
        return MODE_TO_HVAC.get(code or "", HVACMode.OFF)

    # ---- Optimistic helpers ---------------------------------------------

    def _optimistic_active(self) -> bool:
        return bool(
            self._optimistic_expires
            and self.coordinator.hass.loop.time() < self._optimistic_expires
        )

    def _expire_optimistic_if_needed(self) -> None:
        if self._optimistic and not self._optimistic_active():
            self._optimistic.clear()
            self._optimistic_expires = None

    # ---- Preset/scenary mapping -----------------------------------------

    @staticmethod
    def _scenary_to_preset(scenary: str | None) -> str | None:
        s = (scenary or "").strip().lower()
        if s == "occupied":
            return "home"
        if s == "vacant":
            return "away"
        if s == "sleep":
            return "sleep"
        return None

    @staticmethod
    def _preset_to_scenary(preset: str) -> str | None:
        p = (preset or "").strip().lower()
        if p == "home":
            return "occupied"
        if p == "away":
            return "vacant"
        if p == "sleep":
            return "sleep"
        return None

    # ---- Device info -----------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        """Return rich device metadata for the device registry."""
        dev = self._device
        info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            model=dev.get("brand") or "Airzone DKN",
            sw_version=str(dev.get("firmware") or ""),
            name=dev.get("name") or "Airzone Device",
        )
        mac = dev.get("mac")
        if mac:
            # HA will normalize the connection type to "mac"
            info["connections"] = {("mac", str(mac))}
        return info

    # ---- Core state ------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_from_device()

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = [HVACMode.OFF]
        bitstr = self._modes_bitstring()
        if bitstr:
            if len(bitstr) >= 1 and bitstr[0] == "1":
                modes.append(HVACMode.COOL)
            if len(bitstr) >= 2 and bitstr[1] == "1":
                modes.append(HVACMode.HEAT)
            fan_ok = (len(bitstr) >= 3 and bitstr[2] == "1") or (
                len(bitstr) >= 8 and bitstr[7] == "1"
            )
            if fan_ok:
                modes.append(HVACMode.FAN_ONLY)
            if len(bitstr) >= 5 and bitstr[4] == "1":
                modes.append(HVACMode.DRY)
            return modes
        modes.extend([HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY])
        return modes

    # ---- Presets (HA) ----------------------------------------------------

    @property
    def preset_modes(self) -> list[str] | None:
        return ["home", "away", "sleep"]

    @property
    def preset_mode(self) -> str | None:
        self._expire_optimistic_if_needed()
        scen = self._optimistic.get("scenary", self._device.get("scenary"))
        return self._scenary_to_preset(str(scen) if scen is not None else None)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in (self.preset_modes or []):
            _LOGGER.debug(
                "Invalid preset_mode %s (allowed %s)", preset_mode, self.preset_modes
            )
            return

        current = self.preset_mode
        if current == preset_mode:
            return

        scenary = self._preset_to_scenary(preset_mode)
        if not scenary:
            _LOGGER.debug("Unsupported preset -> scenary mapping: %s", preset_mode)
            return

        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API handle missing in coordinator; cannot set scenary")
            return

        try:
            if hasattr(api, "put_device_scenary"):
                await api.put_device_scenary(self._device_id, scenary)
            elif hasattr(api, "set_device_scenary"):
                await api.set_device_scenary(self._device_id, scenary)
            elif hasattr(api, "update_device"):
                await api.update_device(self._device_id, {"scenary": scenary})
            elif hasattr(api, "put_device"):
                await api.put_device(self._device_id, {"scenary": scenary})
            else:
                raise NotImplementedError("No scenary setter available on API client")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning("Failed to set preset/scenary=%s: %s", scenary, err)
            raise

        self._optimistic["scenary"] = scenary
        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Auto-exit AWAY on active commands -------------------------------

    async def _auto_exit_away_if_needed(self, reason: str) -> None:
        try:
            self._expire_optimistic_if_needed()
            if self.preset_mode == "away":
                await self.async_set_preset_mode("home")
        except Exception as err:
            _LOGGER.debug("Auto-exit away skipped (%s): %s", reason, err)

    # ---- Temperature control --------------------------------------------

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float | None:
        val = self._device.get("local_temp")
        return self._parse_float(val)

    @staticmethod
    def _parse_float(val: Any) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "."))
        except Exception:
            return None

    @property
    def target_temperature(self) -> float | None:
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            val = self._optimistic.get("cold_consign", self._device.get("cold_consign"))
        elif mode == HVACMode.HEAT:
            val = self._optimistic.get("heat_consign", self._device.get("heat_consign"))
        else:
            # DRY / FAN_ONLY / OFF: do not expose target temperature
            return None
        return self._parse_float(val)

    @property
    def min_temp(self) -> float:
        """Return min allowable temp (per mode; neutral combo in OFF/DRY/FAN_ONLY)."""
        dev = self._device
        mode = self.hvac_mode
        cold = self._parse_float(dev.get("min_limit_cold"))
        heat = self._parse_float(dev.get("min_limit_heat"))
        if mode == HVACMode.COOL and cold is not None:
            return cold
        if mode == HVACMode.HEAT and heat is not None:
            return heat
        vals = [v for v in (cold, heat) if v is not None]
        return min(vals) if vals else 16.0

    @property
    def max_temp(self) -> float:
        """Return max allowable temp (per mode; neutral combo in OFF/DRY/FAN_ONLY)."""
        dev = self._device
        mode = self.hvac_mode
        cold = self._parse_float(dev.get("max_limit_cold"))
        heat = self._parse_float(dev.get("max_limit_heat"))
        if mode == HVACMode.COOL and cold is not None:
            return cold
        if mode == HVACMode.HEAT and heat is not None:
            return heat
        vals = [v for v in (cold, heat) if v is not None]
        return max(vals) if vals else 32.0

    async def async_set_temperature(self, **kwargs: Any) -> None:
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

        await self._auto_exit_away_if_needed("set_temperature")

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
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Fan control -----------------------------------------------------

    @property
    def fan_modes(self) -> list[str] | None:
        """Expose common labels when exactly 3 speeds exist; otherwise numeric."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            return []
        n = self._fan_speed_max()
        if n == 3:
            return ["low", "medium", "high"]
        return [str(i) for i in range(1, n + 1)]

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode; map 1/2/3 to low/medium/high when normalized."""
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
        if not val:
            return None

        sval = str(val)
        if self._use_normalized_fan_labels():
            return self._num_to_label(sval)
        return sval

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Accept normalized labels (low/medium/high) or numeric strings."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY):
            _LOGGER.debug("Ignoring set_fan_mode in mode %s", mode)
            return
        allowed = self.fan_modes or []
        if fan_mode not in allowed:
            _LOGGER.debug("Invalid fan_mode %s (allowed %s)", fan_mode, allowed)
            return

        await self._auto_exit_away_if_needed("set_fan_mode")

        # Map label to numeric when normalized
        value_to_send = (
            self._label_to_num(fan_mode)
            if self._use_normalized_fan_labels()
            else fan_mode
        )

        if mode == HVACMode.HEAT:
            option = "P4"
            key = "heat_speed"
        elif mode == HVACMode.COOL:
            option = "P3"
            key = "cold_speed"
        else:
            code = self._backend_mode_code()
            if code == "8":
                option = "P4"
                key = "heat_speed"
            else:
                option = "P3"
                key = "cold_speed"

        await self._send_p_event(option, value_to_send)
        self._optimistic[key] = value_to_send

        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()
        self._schedule_refresh()

    # ---- Power / mode ----------------------------------------------------

    async def async_turn_on(self) -> None:
        now = self.coordinator.hass.loop.time()
        optimistic_active = bool(
            self._optimistic_expires and now < self._optimistic_expires
        )
        if optimistic_active and str(self._optimistic.get("power", "")).strip() in (
            "1",
            "true",
            "on",
        ):
            return
        if not optimistic_active:
            backend_on = str(self._device.get("power", "0")).strip() == "1"
            if backend_on:
                return

        await self._auto_exit_away_if_needed("turn_on")

        await self._send_p_event("P1", 1)
        self._optimistic.update({"power": "1"})
        self._optimistic_expires = now + OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()
        self._schedule_refresh()

    async def async_turn_off(self) -> None:
        now = self.coordinator.hass.loop.time()
        optimistic_active = bool(
            self._optimistic_expires and now < self._optimistic_expires
        )
        if optimistic_active and str(self._optimistic.get("power", "")).strip() in (
            "0",
            "false",
            "off",
            "",
        ):
            return
        if not optimistic_active:
            backend_off = str(self._device.get("power", "0")).strip() != "1"
            if backend_off:
                return

        await self._send_p_event("P1", 0)
        self._optimistic.update({"power": "0"})
        self._optimistic_expires = now + OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()
        self._schedule_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send_p_event("P1", 0)
            self._optimistic.update({"power": "0"})
            self._optimistic_expires = (
                self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
            )
            self.async_write_ha_state()
            self._schedule_refresh()
            return

        await self._auto_exit_away_if_needed("set_hvac_mode")

        if self._device_power_on() and self._hvac_from_device() == hvac_mode:
            _LOGGER.debug(
                "HVAC mode %s already active; skipping redundant P2", hvac_mode
            )
            return

        if not self._device_power_on():
            await self._send_p_event("P1", 1)

        if hvac_mode == HVACMode.FAN_ONLY:
            code = self._preferred_ventilate_code() or "3"
            await self._send_p_event("P2", code)
            self._optimistic.update({"power": "1", "mode": code})
        else:
            mode_code = HVAC_TO_MODE.get(hvac_mode)
            if mode_code:
                await self._send_p_event("P2", mode_code)
                self._optimistic.update({"power": "1", "mode": mode_code})

        self._optimistic_expires = (
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
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
        # DRY/OFF: no temperature/fan controls

        # Presets always available (mapped to scenary)
        feats |= ClimateEntityFeature.PRESET_MODE

        # Explicit on/off methods implemented: advertise consistently
        feats |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
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
            raise
        except Exception as err:
            _LOGGER.warning("Failed to send_event %s=%s: %s", option, value, err)
            raise

    def _schedule_refresh(self) -> None:
        if self._cancel_scheduled_refresh is not None:
            try:
                self._cancel_scheduled_refresh()
            finally:
                self._cancel_scheduled_refresh = None

        async def _refresh_cb(_now):
            try:
                await self.coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.debug("Refresh after write failed: %s", err)

        self._cancel_scheduled_refresh = async_call_later(
            self.hass, POST_WRITE_REFRESH_DELAY_SEC, _refresh_cb
        )

    # ---- Coordinator update hook ----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic:
            now = self.coordinator.hass.loop.time()
            if not self._optimistic_expires or now >= self._optimistic_expires:
                self._optimistic.clear()
                self._optimistic_expires = None
            else:
                try:
                    if "scenary" in self._optimistic:
                        scen_o = str(self._optimistic.get("scenary"))
                        scen_b = str(self._device.get("scenary"))
                        if scen_b and scen_b != scen_o:
                            self._optimistic.clear()
                            self._optimistic_expires = None
                except Exception:
                    self._optimistic.clear()
                    self._optimistic_expires = None

        super()._handle_coordinator_update()

    # ---- Availability / update ------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._device)

    async def async_update(self) -> None:
        self._expire_optimistic_if_needed()

    async def async_will_remove_from_hass(self) -> None:
        if self._cancel_scheduled_refresh is not None:
            try:
                self._cancel_scheduled_refresh()
            finally:
                self._cancel_scheduled_refresh = None
        await super().async_will_remove_from_hass()
