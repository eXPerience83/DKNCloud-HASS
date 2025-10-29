"""Power switch platform for DKN Cloud for HASS (Airzone Cloud).

Metadata consistency (0.4.0):
- Device Registry: return a DeviceInfo object (not a plain dict), aligned with climate.py.
- Pass MAC via constructor 'connections' using CONNECTION_NETWORK_MAC (no post-mutation).
- Identifiers unified as (DOMAIN, self._device_id); keep optimistic ON/OFF pattern.

Behavior:
- CoordinatorEntity snapshot: no I/O in properties.
- Async commands via events; optimistic UI + delayed coordinator refresh.
- Privacy: never include PIN in device_info.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import AirzoneCoordinator  # typed coordinator
from .const import (
    DOMAIN,
    MANUFACTURER,
    OPTIMISTIC_TTL_SEC,
    POST_WRITE_REFRESH_DELAY_SEC,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    """Set up the switch platform from a config entry using the DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return

    coordinator: AirzoneCoordinator | None = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzonePowerSwitch] = []
    for device_id in list((coordinator.data or {}).keys()):
        entities.append(AirzonePowerSwitch(coordinator, device_id))

    async_add_entities(entities)


class AirzonePowerSwitch(CoordinatorEntity[AirzoneCoordinator], SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, coordinator: AirzoneCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

        # Optimistic state (cleared once TTL expires or new data arrives)
        self._optimistic_until: float = 0.0
        self._optimistic_is_on: bool | None = None

        # Cancel handle for delayed coordinator refresh
        self._cancel_delayed_refresh: Callable[[], None] | None = None

        dev = self._device
        name = dev.get("name") or "Airzone Device"
        self._attr_name = f"{name} Power"
        self._attr_unique_id = f"{device_id}_power"

    # -----------------------------
    # Helpers
    # -----------------------------
    @property
    def _device(self) -> dict[str, Any]:
        """Return the current device snapshot from the coordinator."""
        return (self.coordinator.data or {}).get(self._device_id, {})  # type: ignore[no-any-return]

    def _optimistic_active(self) -> bool:
        """Return True if optimistic state is still within TTL."""
        return self.coordinator.hass.loop.time() < self._optimistic_until

    def _backend_power_is_on(self) -> bool:
        """Return backend-reported power (ignore optimistic)."""
        power = str(self._device.get("power", "0")).strip()
        return power == "1"

    def _set_optimistic(self, is_on: bool | None) -> None:
        """Set optimistic 'is_on' state with a short TTL and write state."""
        if is_on is not None:
            self._optimistic_is_on = is_on
            self._optimistic_until = (
                self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
            )
            self.async_write_ha_state()

    def _schedule_delayed_refresh(
        self, delay: float = POST_WRITE_REFRESH_DELAY_SEC
    ) -> None:
        """Schedule a coordinator refresh after a short delay to confirm optimistic changes."""
        if self._cancel_delayed_refresh is not None:
            try:
                self._cancel_delayed_refresh()
            finally:
                self._cancel_delayed_refresh = None

        async def _do_refresh(_now: Any) -> None:
            try:
                await self.coordinator.async_request_refresh()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Delayed refresh failed: %s", err)

        self._cancel_delayed_refresh = async_call_later(self.hass, delay, _do_refresh)

    async def _send_event(self, option: str, value: Any) -> None:
        """Send a command to the device using the events endpoint."""
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API not attached to coordinator; cannot send command.")
            return
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Sending event %s=%s for %s", option, value, self._device_id)
        await api.send_event(payload)

    # -----------------------------
    # Coordinator hook
    # -----------------------------
    def _handle_coordinator_update(self) -> None:
        """Called by the coordinator when data is refreshed."""
        dev = self._device
        base_name = dev.get("name") or "Airzone Device"
        self._attr_name = f"{base_name} Power"
        self.async_write_ha_state()

    # -----------------------------
    # Entity lifecycle
    # -----------------------------
    async def async_will_remove_from_hass(self) -> None:
        """Cancel any scheduled delayed refresh when the entity is removed."""
        if self._cancel_delayed_refresh is not None:
            try:
                self._cancel_delayed_refresh()
            finally:
                self._cancel_delayed_refresh = None
        await super().async_will_remove_from_hass()

    # -----------------------------
    # Entity properties
    # -----------------------------
    @property
    def available(self) -> bool:
        """Entity is available if the device exists and has an id."""
        return bool(self._device and self._device.get("id"))

    @property
    def is_on(self) -> bool:
        """Return True if the device is on (optimistic overrides within TTL)."""
        if self._optimistic_active() and self._optimistic_is_on is not None:
            return self._optimistic_is_on
        return self._backend_power_is_on()

    @property
    def icon(self) -> str:
        """Return an icon matching the current state."""
        return "mdi:power" if self.is_on else "mdi:power-off"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info (PII-safe and unified across platforms).

        NOTE: Pass MAC via 'connections' at construction time using
        CONNECTION_NETWORK_MAC; avoid mutating the object after creation.
        """
        dev = self._device
        mac = (str(dev.get("mac") or "").strip()) or None
        connections = {(CONNECTION_NETWORK_MAC, mac)} if mac else None

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            model=dev.get("brand") or "Airzone DKN",
            sw_version=str(dev.get("firmware") or ""),
            name=dev.get("name") or "Airzone Device",
            connections=connections,
        )

    # -----------------------------
    # Write operations
    # -----------------------------
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the device by sending P1=1 (idempotent)."""
        if self._optimistic_active() and self._optimistic_is_on is True:
            _LOGGER.debug("Power already optimistic ON; skipping redundant P1=1")
            return
        if not self._optimistic_active() and self._backend_power_is_on():
            _LOGGER.debug("Power already ON (backend); skipping redundant P1=1")
            return

        await self._send_event("P1", 1)
        self._set_optimistic(True)
        self._schedule_delayed_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the device by sending P1=0 (idempotent)."""
        if self._optimistic_active() and self._optimistic_is_on is False:
            _LOGGER.debug("Power already optimistic OFF; skipping redundant P1=0")
            return
        if not self._optimistic_active() and (not self._backend_power_is_on()):
            _LOGGER.debug("Power already OFF (backend); skipping redundant P1=0")
            return

        await self._send_event("P1", 0)
        self._set_optimistic(False)
        self._schedule_delayed_refresh()
