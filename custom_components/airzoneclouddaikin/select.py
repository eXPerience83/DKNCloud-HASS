"""Select platform for DKN Cloud for HASS: scenary control.

Implements a SelectEntity for device 'scenary' via AirzoneAPI.put_device_scenary().
Options: "occupied", "vacant", "sleep".
- Optimistic UI: reflect the new option immediately; revert on error.
- No I/O in property access; reads come from DataUpdateCoordinator.

Change (hygiene):
- Use Home Assistant event loop clock (hass.loop.time()) for optimistic TTL
  to stay consistent with HA's own schedulers and ease testing.
- Unify manufacturer using const.MANUFACTURER.

This revision:
- Add conservative idempotency: early-return if requested option equals the
  current effective option (considering optimistic TTL first).
- Categorize the entity under Configuration so it appears next to number.* settings.

Typing-only change (A9):
- Import AirzoneCoordinator and parameterize CoordinatorEntity[AirzoneCoordinator].
- Update type annotations to use AirzoneCoordinator instead of DataUpdateCoordinator.

Device Registry alignment (this patch):
- device_info now returns a dict (not DeviceInfo) to match other platforms.
- Fields: identifiers, manufacturer, model (brand or fallback), sw_version (firmware), name, and connections with MAC when present.
- Removed any reference to 'fw_version' as backend returns 'firmware'.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .airzone_api import AirzoneAPI
from .const import DOMAIN, OPTIMISTIC_TTL_SEC, MANUFACTURER
from .__init__ import AirzoneCoordinator  # typing-aware coordinator (A9)

_OPTIONS = ["occupied", "vacant", "sleep"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up select entities for DKN Cloud from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: AirzoneCoordinator = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[SelectEntity] = []
    for device_id, device in (coordinator.data or {}).items():
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


class DKNScenarySelect(CoordinatorEntity[AirzoneCoordinator], SelectEntity):
    """Select entity to control scenary (occupied/vacant/sleep).

    Typing-only note:
    - CoordinatorEntity is parameterized so `self.coordinator.api` and
      `self.coordinator.data` are correctly typed in IDEs/linters.
    """

    _attr_has_entity_name = True
    _attr_name = "Scenary"
    _attr_options = _OPTIONS
    # Place under Configuration to group with number.* settings.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        *,
        coordinator: AirzoneCoordinator,
        api: AirzoneAPI,
        device_id: str,
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        self._api = api
        self._device_id = device_id
        self._optimistic = _OptimisticState()
        self._attr_unique_id = f"{device_id}_scenary"

    # ---------- Device registry ----------
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device registry info (PII-safe and unified across platforms).

        Fields:
        - identifiers: (DOMAIN, device_id)
        - manufacturer: const.MANUFACTURER
        - model: device['brand'] or "Airzone DKN"
        - sw_version: device['firmware'] or ""
        - name: device['name'] or "Airzone Device"
        - connections: {("mac", mac)} if present
        """
        device = (self.coordinator.data or {}).get(self._device_id, {})
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self._device_id)},
            "manufacturer": MANUFACTURER,
            "model": device.get("brand") or "Airzone DKN",
            "sw_version": device.get("firmware") or "",
            "name": device.get("name") or "Airzone Device",
        }
        mac = device.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info

    # ---------- State ----------
    @property
    def available(self) -> bool:
        device = (self.coordinator.data or {}).get(self._device_id)
        return bool(device and device.get("available", True))

    @property
    def current_option(self) -> str | None:
        """Return current scenary (optimistic if active)."""
        if (
            self._optimistic.option is not None
            and self.coordinator.hass.loop.time()
            < self._optimistic.valid_until_monotonic
        ):
            return self._optimistic.option

        val = (self.coordinator.data or {}).get(self._device_id, {}).get("scenary")
        return str(val) if val is not None else None

    async def async_select_option(self, option: str) -> None:
        """Set scenary via API with optimistic UI."""
        if option not in _OPTIONS:
            raise ValueError(f"Invalid scenary option: {option}")

        # Idempotency: if requested option equals the effective current one, skip.
        effective = self.current_option
        if (
            effective is not None
            and option.strip().lower() == str(effective).strip().lower()
        ):
            # English: avoid redundant network call when the option is already applied/optimistic.
            return

        # Optimistic state (event loop clock)
        self._optimistic.option = option
        self._optimistic.valid_until_monotonic = (
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()

        try:
            await self._api.put_device_scenary(self._device_id, option)
        except asyncio.CancelledError:
            # Let HA cancel cleanly
            raise
        except Exception:
            # Revert on error
            self._optimistic.option = None
            self._optimistic.valid_until_monotonic = 0.0
            self.async_write_ha_state()
            raise
        finally:
            await self.coordinator.async_request_refresh()
