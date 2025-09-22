"""Select platform for DKN Cloud for HASS: scenary control.

Implements a SelectEntity for device 'scenary' via AirzoneAPI.put_device_scenary().
- Options: "occupied", "vacant", "sleep"
- Optimistic UI: reflect the new choice immediately; revert on error.
- No I/O in property access; reads come from DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCENARY_OPTIONS: list[str] = ["occupied", "vacant", "sleep"]
CONF_ENABLE_PRESETS = "enable_presets"

# Optimistic cache TTL (seconds) to keep UI updated until coordinator refreshes
_OPTIMISTIC_TTL = 8.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up scenary select entities from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error("No integration data for entry_id=%s", entry.entry_id)
        return

    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api = data["api"]

    enable_presets = bool(
        entry.options.get(CONF_ENABLE_PRESETS, entry.data.get(CONF_ENABLE_PRESETS, False))
    )

    entities: list[SelectEntity] = []
    for device_id, device in (coordinator.data or {}).items():
        if "scenary" in device:
            entities.append(
                AirzoneScenarySelect(
                    coordinator=coordinator,
                    api=api,
                    device_data=device,
                    enabled_by_default=enable_presets,
                )
            )

    if entities:
        async_add_entities(entities, True)


class AirzoneScenarySelect(SelectEntity):
    """Scenary selector (occupied/vacant/sleep)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:home-account"

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
        self._attr_name = "Scenary"
        self._attr_unique_id = f"{self._device_id}_scenary"
        self._attr_options = SCENARY_OPTIONS
        self._attr_entity_registry_enabled_default = enabled_by_default

        # Optimistic cache
        self._optimistic_option: str | None = None
        self._optimistic_until: float = 0.0

        # DeviceInfo w/o PII
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
        """Entity available if device exists in coordinator and has scenary."""
        device = (self.coordinator.data or {}).get(self._device_id)
        return bool(device and "scenary" in device)

    @property
    def current_option(self) -> str | None:
        """Current scenary; prefer optimistic cache if still valid."""
        now = time.monotonic()
        if self._optimistic_option and now < self._optimistic_until:
            return self._optimistic_option
        device = (self.coordinator.data or {}).get(self._device_id, {})
        scenary = device.get("scenary")
        return scenary if scenary in SCENARY_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        """Handle user selection: optimistic update + API call."""
        if option not in SCENARY_OPTIONS:
            raise ValueError(f"Unsupported scenary option: {option}")

        # Optimistic: reflect immediately
        self._optimistic_option = option
        self._optimistic_until = time.monotonic() + _OPTIMISTIC_TTL
        self.async_write_ha_state()

        try:
            await self._api.put_device_scenary(self._device_id, option)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Revert optimistic on failure
            self._optimistic_option = None
            self._optimistic_until = 0.0
            self.async_write_ha_state()
            raise err
        finally:
            # Request a refresh to consolidate the state
            await self.coordinator.async_request_refresh()
