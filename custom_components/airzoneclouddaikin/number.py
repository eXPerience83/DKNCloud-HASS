"""Number platform for DKN Cloud for HASS: sleep_time control.

Implements a NumberEntity for device 'sleep_time' via AirzoneAPI.put_device_sleep_time().
- Range: 30..120 minutes; step 10.
- Optimistic UI: reflect the new value immediately; revert on error.
- No I/O in property access; reads come from DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_MIN = 30
_MAX = 120
_STEP = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for DKN Cloud from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[NumberEntity] = []
    # coordinator.data is expected to be a dict mapping device_id -> state dict
    for device_id, device in (coordinator.data or {}).items():
        # Only create the entity if the device exposes sleep_time
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

    # ---------- Home Assistant required metadata ----------

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information.

        We only use safe identifiers and non-PII attributes.
        """
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
        """Entity availability based on coordinator data."""
        device = (self.coordinator.data or {}).get(self._device_id)
        if not device:
            return False
        # Optional 'available' flag in data; default to True
        return bool(device.get("available", True))

    @property
    def native_value(self) -> int | None:
        """Return current sleep_time in minutes.

        Respects a short-lived optimistic state to make UI feel instant.
        """
        import time  # Local import to avoid global import if not needed

        if (
            self._optimistic.value is not None
            and time.monotonic() < self._optimistic.valid_until_monotonic
        ):
            return self._optimistic.value

        device = (self.coordinator.data or {}).get(self._device_id, {})
        raw = device.get("sleep_time")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new sleep_time (rounded to nearest step) using the API."""
        # Clamp and quantize to step of 10 minutes
        ivalue = int(round(value / _STEP) * _STEP)
        ivalue = max(_MIN, min(_MAX, ivalue))

        # Optimistic update for ~6 seconds (two typical polling intervals)
        import time

        self._optimistic.value = ivalue
        self._optimistic.valid_until_monotonic = time.monotonic() + 6.0
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
