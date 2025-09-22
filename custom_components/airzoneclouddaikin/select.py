"""Select platform for DKN Cloud for HASS: scenary control.

Implements a SelectEntity for device 'scenary' via AirzoneAPI.put_device_scenary().
Options: "occupied", "vacant", "sleep".
- Optimistic UI: reflect the new option immediately; revert on error.
- No I/O in property access; reads come from DataUpdateCoordinator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_OPTIONS = ["occupied", "vacant", "sleep"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for DKN Cloud from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[SelectEntity] = []
    # coordinator.data is expected to be a dict mapping device_id -> state dict
    for device_id, device in (coordinator.data or {}).items():
        # Only create the entity if the device exposes scenary
        if "scenary" in device:
            entities.append(
                DKNScenarySelect(
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

    option: str | None = None
    valid_until_monotonic: float = 0.0


class DKNScenarySelect(CoordinatorEntity, SelectEntity):
    """Select entity to control scenary (occupied/vacant/sleep)."""

    _attr_has_entity_name = True
    _attr_name = "Scenary"
    _attr_options = _OPTIONS
    _attr_entity_category = EntityCategory.CONFIG

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

        self._attr_unique_id = f"{device_id}_scenary"

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
    def current_option(self) -> str | None:
        """Return the selected scenary option.

        Respects a short-lived optimistic state to make UI feel instant.
        """
        import time  # Local import to avoid global import if not needed

        if (
            self._optimistic.option is not None
            and time.monotonic() < self._optimistic.valid_until_monotonic
        ):
            return self._optimistic.option

        device = (self.coordinator.data or {}).get(self._device_id, {})
        opt = device.get("scenary")
        return str(opt) if opt in _OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        """Set scenary via API."""
        if option not in _OPTIONS:
            # Defensive: ignore invalid values from automations
            raise ValueError(f"Unsupported scenary option: {option}")

        # Optimistic update for ~6 seconds (two typical polling intervals)
        import time

        self._optimistic.option = option
        self._optimistic.valid_until_monotonic = time.monotonic() + 6.0
        self.async_write_ha_state()

        try:
            await self._api.put_device_scenary(self._device_id, option)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Revert optimistic state on failure
            self._optimistic.option = None
            self._optimistic.valid_until_monotonic = 0.0
            self.async_write_ha_state()
            raise
        finally:
            # Request a refresh to consolidate the state
            await self.coordinator.async_request_refresh()

    @property
    def icon(self) -> str:
        """Return a dynamic icon based on scenary."""
        opt = self.current_option
        if opt == "occupied":
            return "mdi:home-account"
        if opt == "sleep":
            return "mdi:power-sleep"
        if opt == "vacant":
            return "mdi:door-closed"
        return "mdi:home"
