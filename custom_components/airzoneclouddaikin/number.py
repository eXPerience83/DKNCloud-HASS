"""Number platform for DKN Cloud for HASS: sleep_time control.

Implements a NumberEntity for device 'sleep_time' via AirzoneAPI.put_device_sleep_time().
- Valid range: 30..120 minutes, step 10.
- Optimistic UI with short TTL, then coordinator refresh.
- No I/O in properties; reads come from DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_MIN = 30
_MAX = 120
_STEP = 10
_OPTIMISTIC_TTL_SEC = 6.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up number entities for DKN Cloud from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[NumberEntity] = []
    for device_id, device in (coordinator.data or {}).items():
        if "sleep_time" in device:
            entities.append(
                DKNSleepTimeNumber(
                    coordinator=coordinator,
                    api=api,
                    device_id=str(device_id),
                )
            )

    if entities:
        async_add_entities(entities)


@dataclass(slots=True, kw_only=True)
class _OptimisticState:
    """Helper to keep short-lived optimistic state."""

    value: int | None = None
    valid_until_monotonic: float = 0.0


class DKNSleepTimeNumber(CoordinatorEntity, NumberEntity):
    """Number entity to control sleep_time in minutes."""

    _attr_has_entity_name = True
    _attr_name = "Sleep time"
    _attr_icon = "mdi:power-sleep"
    _attr_native_unit_of_measurement = "min"
    _attr_native_min_value = _MIN
    _attr_native_max_value = _MAX
    _attr_native_step = _STEP
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        *,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]],
        api: AirzoneAPI,
        device_id: str,
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        self._api = api
        self._device_id = device_id
        self._optimistic = _OptimisticState()
        self._attr_unique_id = f"{device_id}_sleep_time"

    # ---------- Device registry ----------
    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info (no PII)."""
        device = (self.coordinator.data or {}).get(self._device_id, {})
        manufacturer = device.get("manufacturer") or "Daikin"
        model = device.get("model") or "DKN"
        sw_version = device.get("fw_version") or device.get("firmware")
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=manufacturer,
            model=model,
            sw_version=str(sw_version) if sw_version is not None else None,
            name=device.get("name") or f"Device {self._device_id}",
        )

    # ---------- State ----------
    @property
    def available(self) -> bool:
        device = (self.coordinator.data or {}).get(self._device_id)
        return bool(device and device.get("available", True))

    @property
    def native_value(self) -> int | None:
        """Return current sleep_time (optimistic if active)."""
        import time

        if (
            self._optimistic.value is not None
            and time.monotonic() < self._optimistic.valid_until_monotonic
        ):
            return int(self._optimistic.value)

        val = (self.coordinator.data or {}).get(self._device_id, {}).get("sleep_time")
        try:
            return int(val) if val is not None else None
        except Exception:
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new sleep_time (rounded to nearest step) using the API."""
        # Clamp and quantize to step of 10 minutes
        ivalue = int(round(value / _STEP) * _STEP)
        ivalue = max(_MIN, min(_MAX, ivalue))

        # Optimistic update for a few seconds
        import time

        self._optimistic.value = ivalue
        self._optimistic.valid_until_monotonic = time.monotonic() + _OPTIMISTIC_TTL_SEC
        self.async_write_ha_state()

        try:
            await self._api.put_device_sleep_time(self._device_id, ivalue)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Revert optimistic state on failure
            self._optimistic.value = None
            self._optimistic.valid_until_monotonic = 0.0
            self.async_write_ha_state()
            raise
        finally:
            # Request a refresh to consolidate the state
            await self.coordinator.async_request_refresh()
