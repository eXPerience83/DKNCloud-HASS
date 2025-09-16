"""Climate platform for DKN Cloud for HASS (Airzone Cloud).

This entity follows HA async patterns and uses the DataUpdateCoordinator.
It restores fan control in the UI (Cool/Heat/Fan-only), hides it in Dry mode,
and uses integer steps for target temperature.

Why this revision?
- Fixed a regression: write commands now send proper /events payloads (P1/P2/P3/P4/P7/P8),
  instead of incorrectly calling `api.send_event()` with positional args.
- Entities are now sourced directly from `coordinator.data` (a dict keyed by device_id),
  not from a non-existent `data["devices"]` list.

Notes:
- We optimistically update the coordinator snapshot after write calls
  so the UI reflects the change until the next refresh.
- Temperatures are integers (1 ºC steps) in the UI; we still send "25.0" to the API.
- We only expose COOL, HEAT, FAN_ONLY and DRY. No AUTO/HEAT_COOL until verified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_WHOLE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

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


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the climate platform from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No integration data for entry %s", entry.entry_id)
        return

    coordinator: DataUpdateCoordinator = data["coordinator"]
    api = data["api"]

    # Create one entity per device present in the coordinator snapshot
    entities: list[ClimateEntity] = [
        AirzoneClimate(coordinator, api, device_id) for device_id in coordinator.data.keys()
    ]
    async_add_entities(entities)


@dataclass
class _Ctx:
    device_id: str
    mac: str | None
    name: str


class AirzoneClimate(CoordinatorEntity, ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1.0  # expose whole-degree steps

    def __init__(
        self, coordinator: DataUpdateCoordinator, api, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        dev = self._device  # snapshot
        self._ctx = _Ctx(
            device_id=str(device_id),
            mac=(dev.get("mac") or None),
            name=str(dev.get("name") or "Airzone Device"),
        )
        self._attr_name = self._ctx.name
        self._attr_unique_id = self._ctx.device_id

    # ---- Helpers -----------------------------------------------------------------

    @property
    def _device(self) -> dict[str, Any]:
        """Return the current device snapshot from the coordinator."""
        return self.coordinator.data.get(self._ctx.device_id, {})

    def _update_local(self, **changes: Any) -> None:
        """Apply optimistic changes into the coordinator snapshot."""
        dev = dict(self._device)
        dev.update(changes)
        new_data = dict(self.coordinator.data)
        new_data[self._ctx.device_id] = dev
        self.coordinator.async_set_updated_data(new_data)

    async def _send_event(self, option: str, value: Any) -> None:
        """Send a command to the device using the events endpoint."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._ctx.device_id,
                "option": option,
                "value": value,
            }
        }
        await self._api.send_event(payload)

    @property
    def device_info(self) -> DeviceInfo:
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, self._ctx.device_id)},
            "name": self._ctx.name,
            "manufacturer": "Daikin / Airzone",
        }
        if self._ctx.mac:
            info["connections"] = {("mac", self._ctx.mac)}
        return info

    # ---- Current state -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._device)

    def _raw(self, key: str) -> Any:
        return self._device.get(key)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        # Prefer device-supported mask if present; else expose all four
        bitmask = str(self._raw("modes") or "")
        # Known order in many firmwares aligns with: [COOL, HEAT, FAN, AUTO, DRY, ?, ?, ?]
        # We ignore AUTO entirely.
        modes: list[HVACMode] = []
        try:
            if len(bitmask) >= 1 and bitmask[0] == "1":
                modes.append(HVACMode.COOL)
            if len(bitmask) >= 2 and bitmask[1] == "1":
                modes.append(HVACMode.HEAT)
            if len(bitmask) >= 3 and bitmask[2] == "1":
                modes.append(HVACMode.FAN_ONLY)
            # some firmwares place DRY at index 4
            if len(bitmask) >= 5 and bitmask[4] == "1":
                modes.append(HVACMode.DRY)
        except Exception:  # noqa: BLE001
            modes = []
        if not modes:
            modes = [HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.DRY]
        return [HVACMode.OFF, *modes]

    @property
    def hvac_mode(self) -> HVACMode:
        power = str(self._raw("power") or "0")
        if power == "0":
            return HVACMode.OFF
        mode_code = str(self._raw("mode") or "")
        return MODE_TO_HVAC.get(mode_code, HVACMode.OFF)

    @property
    def fan_modes(self) -> list[str]:
        # Hide fan control in DRY and when OFF
        current = self.hvac_mode
        if current in (HVACMode.OFF, HVACMode.DRY):
            return []
        try:
            n = int(float(self._raw("availables_speeds") or 0))
        except (TypeError, ValueError):
            n = 0
        n = max(0, n)
        return [str(i) for i in range(1, n + 1)]

    @property
    def fan_mode(self) -> str | None:
        if not self.fan_modes:
            return None
        current = self.hvac_mode
        key = (
            "cold_speed"
            if current in (HVACMode.COOL, HVACMode.FAN_ONLY)
            else "heat_speed"
        )
        val = self._raw(key)
        return str(val) if val is not None else None

    @property
    def target_temperature(self) -> int | None:
        current = self.hvac_mode
        if current == HVACMode.COOL:
            raw = self._raw("cold_consign")
        elif current == HVACMode.HEAT:
            raw = self._raw("heat_consign")
        else:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    @property
    def min_temp(self) -> float:
        current = self.hvac_mode
        key = "min_limit_cold" if current == HVACMode.COOL else "min_limit_heat"
        raw = self._raw(key) if current in (HVACMode.COOL, HVACMode.HEAT) else 16
        try:
            return float(int(float(raw)))
        except (TypeError, ValueError):
            return 16.0

    @property
    def max_temp(self) -> float:
        current = self.hvac_mode
        key = "max_limit_cold" if current == HVACMode.COOL else "max_limit_heat"
        raw = self._raw(key) if current in (HVACMode.COOL, HVACMode.HEAT) else 32
        try:
            return float(int(float(raw)))
        except (TypeError, ValueError):
            return 32.0

    @property
    def supported_features(self) -> int:
        features = 0
        mode = self.hvac_mode
        if mode in (HVACMode.COOL, HVACMode.HEAT):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if mode in (HVACMode.COOL, HVACMode.HEAT, HVACMode.FAN_ONLY):
            features |= ClimateEntityFeature.FAN_MODE
        return features

    # ---- Commands ----------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send_event("P1", 0)
            self._update_local(power="0")
            return

        mode_code = HVAC_TO_MODE.get(hvac_mode)
        if not mode_code:
            _LOGGER.debug("Ignored unsupported hvac_mode: %s", hvac_mode)
            return

        # Select new mode, then ensure power ON
        await self._send_event("P2", mode_code)
        await self._send_event("P1", 1)
        self._update_local(mode=mode_code, power="1")

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        current = self.hvac_mode
        if current in (HVACMode.OFF, HVACMode.DRY):
            _LOGGER.debug("Fan control disabled in mode %s", current)
            return
        # Use P3 in COOL/FAN_ONLY; P4 in HEAT
        option = "P3" if current in (HVACMode.COOL, HVACMode.FAN_ONLY) else "P4"
        await self._send_event(option, str(fan_mode))
        key = "cold_speed" if option == "P3" else "heat_speed"
        self._update_local(**{key: str(fan_mode)})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        try:
            # UI enforces integers; send "25.0" to API for compatibility
            value_str = f"{int(round(float(temp))):.1f}"
        except (TypeError, ValueError):
            return

        current = self.hvac_mode
        if current == HVACMode.COOL:
            option, key = "P7", "cold_consign"
        elif current == HVACMode.HEAT:
            option, key = "P8", "heat_consign"
        else:
            _LOGGER.debug("Temperature set ignored for mode %s", current)
            return

        await self._send_event(option, value_str)
        self._update_local(**{key: value_str})
