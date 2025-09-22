"""Number platform for DKN Cloud for HASS: sleep_time control.

Implements a NumberEntity for device 'sleep_time' via AirzoneAPI.put_device_sleep_time().
- Range: 30..120 minutes; step 10.
- Optimistic UI: reflect the new value immediately; revert on error.
- No I/O in property access; reads come from DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.components.number.const import NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_ENABLE_PRESETS = "enable_presets"

_OPTIMISTIC_TTL = 8.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up sleep_time number entities from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error("No integration data for entry_id=%s", entry.entry_id)
        return

    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api = data["api"]

    enable_presets = bool(
        entry.options.get(CONF_ENABLE_PRESETS, entry.data.get(CONF_ENABLE_PRESETS, False))
    )

    entities: list[NumberEntity] = []
    for device_id, device in (coordinator.data or {}).items():
        if "sleep_time" in device:
            entities.append(
                AirzoneSleepTimeNumber(
                    coordinator=coordinator,
                    api=api,
                    device_data=device,
                    enabled_by_default=enable_presets,
                )
            )

    if entities:
        async_add_entities(entities, True)


class AirzoneSleepTimeNumber(NumberEntity):
    """Number entity for sleep_time minutes (30..120, step 10)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 30
    _attr_native_max_value = 120
    _attr_native_step = 10
    _attr_device_class = NumberDeviceClass.DURATION

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api: Any,
        device_data: dict[str, Any],
        enabled_by_default: bool,
    ) -> None:
        """Initialize entity."""
        self.coordinator = coordinator
        self._api = api
        self._device_id: str = device_data.get("id")
        self._name: str = device_data.get("name", "Airzone Device")
        self._attr_name = "Sleep Time (min)"
        self._attr_unique_id = f"{self._device_id}_sleep_time"
        self._attr_entity_registry_enabled_default = enabled_by_default

        self._optimistic_value: float | None = None
        self._optimistic_until: float = 0.0

        manufacturer = device_data.get("manufacturer", "Airzone")
        model = device_data.get("model") or "DKN Cloud"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._name,
            manufacturer=manufacturer,
            model=model,
        )

    @property
    def available(self) -> bool:
        device = (self.coordinator.data or {}).get(self._device_id)
        return bool(device and "sleep_time" in device)

    @property
    def native_value(self) -> float | None:
        """Return sleep_time; prefer optimistic cache if valid."""
        now = time.monotonic()
        if self._optimistic_value is not None and now < self._optimistic_until:
            return float(self._optimistic_value)
        device = (self.coordinator.data or {}).get(self._device_id, {})
        value = device.get("sleep_time")
        try:
            return float(int(value))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set sleep_time (clamped and snapped to step 10)."""
        try:
            ivalue = int(round(value))
        except (TypeError, ValueError) as err:
            raise ValueError("sleep_time must be a number") from err

        ivalue = max(30, min(120, ivalue))
        ivalue = int(round(ivalue / 10.0) * 10)

        # Optimistic first:
        self._optimistic_value = float(ivalue)
        self._optimistic_until = time.monotonic() + _OPTIMISTIC_TTL
        self.async_write_ha_state()

        try:
            await self._api.put_device_sleep_time(self._device_id, ivalue)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Revert optimistic on failure
            self._optimistic_value = None
            self._optimistic_until = 0.0
            self.async_write_ha_state()
            raise err
        finally:
            await self.coordinator.async_request_refresh()
