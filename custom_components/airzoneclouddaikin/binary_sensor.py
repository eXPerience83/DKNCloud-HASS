"""Binary sensor platform for DKN Cloud for HASS (Airzone Cloud).

Creates boolean sensors per device:
- device_on: derived from backend "power" field.
- wserver_online: derived from last connection timestamp age (passive connectivity).

Design:
- Coordinator-backed (no I/O in properties).
- Enabled-by-default.
- Device classes: power (device_on) and connectivity (wserver_online).

Privacy: never log or expose PII (email/token/MAC/PIN/GPS).

This update:
- Use HA dt_util.parse_datetime + dt_util.as_utc to parse connection_date
  (TZ/DST safe and aligned with __init__.py logic).
- If timestamp parsing fails, do NOT force offline to avoid false positives.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .__init__ import AirzoneCoordinator  # typing-aware coordinator
from .const import (
    CONF_STALE_AFTER_MINUTES,
    DOMAIN,
    MANUFACTURER,
    STALE_AFTER_MINUTES_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up binary sensors from a config entry using coordinator snapshot."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator: AirzoneCoordinator | None = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    stale_after_min = int(
        entry.options.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
    )
    entities: list[BinarySensorEntity] = []

    for device_id in list((coordinator.data or {}).keys()):
        entities.append(AirzoneDeviceOnBinarySensor(coordinator, device_id))
        entities.append(
            AirzoneWServerOnlineBinarySensor(coordinator, device_id, stale_after_min)
        )

    async_add_entities(entities)


class AirzoneDeviceOnBinarySensor(
    CoordinatorEntity[AirzoneCoordinator], BinarySensorEntity
):
    """Boolean sensor indicating whether the device reports power ON."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_entity_registry_enabled_default = True  # enabled by default
    _attr_should_poll = False  # coordinator-driven

    def __init__(self, coordinator: AirzoneCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = "Device On"
        self._attr_unique_id = f"{device_id}_device_on"

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
        if isinstance(val, bool):
            return val
        try:
            return bool(int(val))
        except Exception:
            return False

    @property
    def is_on(self) -> bool:
        """Return True if the device reports power ON."""
        return self._normalize_power(self._device.get("power"))

    @property
    def available(self) -> bool:
        """Available when we have a device snapshot."""
        return bool(self._device)

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


class AirzoneWServerOnlineBinarySensor(
    CoordinatorEntity[AirzoneCoordinator], BinarySensorEntity
):
    """Connectivity sensor derived from the age of `connection_date` (passive)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_registry_enabled_default = True  # enabled by default
    _attr_should_poll = False  # coordinator-driven
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: AirzoneCoordinator, device_id: str, stale_after_min: int
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._stale_after_sec = max(60, int(stale_after_min) * 60)  # safety lower bound
        self._attr_name = "WServer Online"
        self._attr_unique_id = f"{device_id}_wserver_online"

    @property
    def _device(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._device_id, {})

    @property
    def is_on(self) -> bool:
        """Return True if connection_date age <= stale threshold (TZ-safe)."""
        raw = self._device.get("connection_date")
        if not raw:
            return False
        try:
            ts = dt_util.parse_datetime(str(raw))
            # If we cannot parse the timestamp, do not force offline (avoid false alarms).
            if ts is None:
                return True
            ts = dt_util.as_utc(ts)
            now = dt_util.utcnow()
            age = (now - ts).total_seconds()
            return age <= self._stale_after_sec
        except Exception:
            # Conservative: keep current state as "online" on parse error paths.
            return True

    @property
    def available(self) -> bool:
        return bool(self._device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose minimal debug attrs (no PII)."""
        raw = self._device.get("connection_date")
        age: int | None = None
        try:
            if raw:
                ts = dt_util.parse_datetime(str(raw))
                if ts is not None:
                    ts = dt_util.as_utc(ts)
                    age = int((dt_util.utcnow() - ts).total_seconds())
        except Exception:
            age = None
        return {
            "last_connection": str(raw) if raw else None,
            "stale_after_sec": int(self._stale_after_sec),
            "seconds_since_connection": age,
        }

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
