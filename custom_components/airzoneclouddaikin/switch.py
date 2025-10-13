"""Power switch platform for DKN Cloud for HASS (Airzone Cloud).

Key behaviors:
- CoordinatorEntity snapshot: no I/O in properties.
- Fully async commands via events endpoint; optimistic UI + short delayed refresh.
- Privacy: never include PIN in device_info.

This revision (hygiene):
- Use Home Assistant event loop clock for TTLs (hass.loop.time()).
- Wire and cancel the delayed refresh handle to avoid stacked/late callbacks.
- Add conservative idempotency for P1 ON/OFF to reduce redundant traffic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
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

    coordinator = data.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Coordinator missing for entry %s", entry.entry_id)
        return

    entities: list[AirzonePowerSwitch] = []
    for device_id in list((coordinator.data or {}).keys()):
        entities.append(AirzonePowerSwitch(coordinator, device_id))

    async_add_entities(entities)


class AirzonePowerSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, coordinator, device_id: str) -> None:
        """Initialize the power switch bound to a device id."""
        super().__init__(coordinator)
        self._device_id = device_id

        # Optimistic state (cleared once TTL expires or new data arrives)
        self._optimistic_until: float = 0.0
        self._optimistic_is_on: bool | None = None

        # Cancel handle for delayed coordinator refresh (wired in _schedule_delayed_refresh)
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

    def _now(self) -> float:
        """Return the Home Assistant event loop's monotonic time."""
        return self.coordinator.hass.loop.time()

    def _optimistic_active(self) -> bool:
        """Return True if optimistic state is still within TTL."""
        return self._now() < self._optimistic_until

    def _backend_power_is_on(self) -> bool:
        """Return backend-reported power (ignore optimistic)."""
        power = str(self._device.get("power", "0")).strip()
        return power == "1"

    def _set_optimistic(self, is_on: bool | None) -> None:
        """Set optimistic 'is_on' state with a short TTL and write state."""
        if is_on is not None:
            self._optimistic_is_on = is_on
            self._optimistic_until = self._now() + OPTIMISTIC_TTL_SEC
            self.async_write_ha_state()

    def _schedule_delayed_refresh(
        self, delay: float = POST_WRITE_REFRESH_DELAY_SEC
    ) -> None:
        """Schedule a coordinator refresh after a short delay to confirm optimistic changes.

        Keep and cancel the previous handle to avoid stacked callbacks.
        """
        # Cancel any previously scheduled refresh
        if self._cancel_delayed_refresh is not None:
            try:
                self._cancel_delayed_refresh()
            finally:
                self._cancel_delayed_refresh = None

        async def _do_refresh(_now: Any) -> None:
            try:
                await self.coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.debug("Delayed refresh failed: %s", err)

        # Store cancel handle
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
        # Keep optimistic state until TTL expires; snapshot will confirm afterwards.
        # Update the displayed name if it changes backend-side.
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
    def device_info(self) -> dict[str, Any]:
        """Return device info for the device registry (without exposing the PIN)."""
        dev = self._device
        info: dict[str, Any] = {
            # Ensure stable identifier even if 'dev' is still an empty snapshot.
            "identifiers": {(DOMAIN, dev.get("id") or self._device_id)},
            "name": dev.get("name"),
            "manufacturer": "Daikin",
            # Privacy: do not include PIN in model string.
            "model": dev.get("brand") or "Unknown",
            "sw_version": dev.get("firmware") or "Unknown",
        }
        mac = dev.get("mac")
        if mac:
            info["connections"] = {("mac", mac)}
        return info

    # -----------------------------
    # Write operations (async + optimistic + delayed refresh)
    # -----------------------------
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the device by sending P1=1 (idempotent)."""
        # Idempotency: skip when optimistic is active and already ON
        if self._optimistic_active() and self._optimistic_is_on is True:
            _LOGGER.debug("Power already optimistic ON; skipping redundant P1=1")
            return
        # Idempotency (backend): skip if backend already ON
        if not self._optimistic_active() and self._backend_power_is_on():
            _LOGGER.debug("Power already ON (backend); skipping redundant P1=1")
            return

        await self._send_event("P1", 1)
        self._set_optimistic(True)
        self._schedule_delayed_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the device by sending P1=0 (idempotent)."""
        # Idempotency: skip when optimistic is active and already OFF
        if self._optimistic_active() and self._optimistic_is_on is False:
            _LOGGER.debug("Power already optimistic OFF; skipping redundant P1=0")
            return
        # Idempotency (backend): skip if backend already OFF
        if not self._optimistic_active() and (not self._backend_power_is_on()):
            _LOGGER.debug("Power already OFF (backend); skipping redundant P1=0")
            return

        await self._send_event("P1", 0)
        self._set_optimistic(False)
        self._schedule_delayed_refresh()
