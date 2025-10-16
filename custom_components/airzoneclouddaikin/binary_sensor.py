"""Binary sensor platform for DKN Cloud for HASS (Airzone Cloud).

Creates a single boolean sensor per device:
- device_on: derived from backend "power" field.

Design:
- Coordinator-backed (no I/O in properties).
- Non-diagnostic, enabled by default.
- Device class = power.

Privacy: never log or expose PII (email/token/MAC/PIN/GPS).

A9 typing-only:
- Import AirzoneCoordinator and parameterize CoordinatorEntity[AirzoneCoordinator].
- Add local type annotation for `coordinator` in async_setup_entry.

This patch (metadata consistency):
- Use const.MANUFACTURER and add MAC connection to align Device registry metadata with other platforms.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .__init__ import AirzoneCoordinator  # typing-aware coordinator (A9)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up binary sensors from a config entry using coordinator snapshot."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    # Typing-only: keep .get() + None check; annotate as Optional for IDEs.
    coordinator: AirzoneCoordinator | None = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzoneDeviceOnBinarySensor] = []
    for device_id in list((coordinator.data or {}).keys()):
        entities.append(AirzoneDeviceOnBinarySensor(coordinator, device_id))

    async_add_entities(entities)


class AirzoneDeviceOnBinarySensor(
    CoordinatorEntity[AirzoneCoordinator], BinarySensorEntity
):
    """Boolean sensor indicating whether the device reports power ON.

    Typing-only note:
    - CoordinatorEntity is parameterized so `self.coordinator.api` and
      `self.coordinator.data` are correctly typed in IDEs/linters.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_entity_registry_enabled_default = True  # enabled by default
    _attr_should_poll = False  # coordinator-driven

    def __init__(self, coordinator: AirzoneCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = "Device On"
        self._attr_unique_id = f"{device_id}_device_on"

    # ---- Helpers ---------------------------------------------------------

    @property
    def _device(self) -> dict[str, Any]:
        """Return latest device snapshot from the coordinator."""
        return (self.coordinator.data or {}).get(self._device_id, {})

    @staticmethod
    def _normalize_power(val: Any) -> bool:
        """Normalize backend power to boolean.

        Accepts "1"/1/True/"on"/"true"/"yes" as ON;
        "0"/0/False/"off"/"false"/"no"/""/None as OFF.
        """
        s = str(val).strip().lower()
        if s in ("1", "on", "true", "yes"):
            return True
        if s in ("0", "off", "false", "no", "", "none"):
            return False
        # Fallbacks
        if isinstance(val, bool):
            return val
        try:
            return bool(int(val))
        except Exception:
            return False

    # ---- BinarySensorEntity API -----------------------------------------

    @property
    def is_on(self) -> bool:
        """Return True if the device reports power ON."""
        return self._normalize_power(self._device.get("power"))

    @property
    def available(self) -> bool:
        """Available when we have a device snapshot."""
        return bool(self._device)

    # ---- Device info -----------------------------------------------------

    @property
    def device_info(self):
        dev = self._device
        info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "manufacturer": MANUFACTURER,
            "model": dev.get("brand") or "Airzone DKN",
            "sw_version": dev.get("firmware") or "",
            "name": dev.get("name") or "Airzone Device",
        }
        mac = dev.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info
