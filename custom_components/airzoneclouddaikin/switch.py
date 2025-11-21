"""Power switch entity for DKN Cloud (Airzone Cloud)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import AirzoneCoordinator  # typed coordinator
from .const import DOMAIN, MANUFACTURER
from .helpers import (
    acquire_device_lock,
    optimistic_get,
    optimistic_invalidate,
    optimistic_set,
    schedule_post_write_refresh,
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
        entities.append(AirzonePowerSwitch(coordinator, entry.entry_id, device_id))

    async_add_entities(entities)


class AirzonePowerSwitch(CoordinatorEntity[AirzoneCoordinator], SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(
        self, coordinator: AirzoneCoordinator, entry_id: str, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_id = device_id
        self._climate_entity_id: str | None = None

        dev = self._device
        name = dev.get("name") or "Airzone Device"
        self._attr_name = f"{name} Power"
        self._attr_unique_id = f"{device_id}_power"

    # -----------------------------
    # Helpers
    # -----------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._climate_entity_id = self._resolve_climate_entity_id()

    @property
    def _device(self) -> dict[str, Any]:
        """Return the current device snapshot from the coordinator."""
        return (self.coordinator.data or {}).get(self._device_id, {})  # type: ignore[no-any-return]

    def _overlay_power(self) -> Any:
        """Return the power value applying the optimistic overlay."""
        return optimistic_get(
            self.hass,
            self._entry_id,
            self._device_id,
            "power",
            self._device.get("power"),
        )

    def _backend_power_is_on(self) -> bool:
        """Return backend-reported power (ignore optimistic)."""
        power = str(self._device.get("power", "0")).strip()
        return power == "1"

    def _resolve_climate_entity_id(self) -> str | None:
        """Resolve the sibling climate entity via the entity registry."""

        hass = getattr(self, "hass", None)
        if hass is None:
            return self._climate_entity_id

        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "climate", DOMAIN, f"{self._device_id}_climate"
        )
        self._climate_entity_id = entity_id
        return entity_id

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
        try:
            await api.send_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to send_event %s=%s for %s: %s",
                option,
                value,
                self._device_id,
                err,
            )
            raise

    # -----------------------------
    # Coordinator hook
    # -----------------------------
    def _handle_coordinator_update(self) -> None:
        """Called by the coordinator when data is refreshed."""
        dev = self._device
        base_name = dev.get("name") or "Airzone Device"
        self._attr_name = f"{base_name} Power"
        self.async_write_ha_state()

    # Entity properties
    # -----------------------------
    @property
    def available(self) -> bool:
        """Entity is available if the device exists and has an id."""
        return bool(self._device and self._device.get("id"))

    @property
    def is_on(self) -> bool:
        """Return True if the device is on (optimistic overrides within TTL)."""
        value = self._overlay_power()
        sval = str(value).strip().lower()
        if sval in {"1", "true", "on"}:
            return True
        if sval in {"0", "false", "off", ""}:
            return False
        if isinstance(value, bool):
            return value
        try:
            return bool(int(value))
        except Exception:  # noqa: BLE001
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
        """Turn on the device delegating to the climate entity when possible."""

        climate_eid = self._resolve_climate_entity_id()
        if climate_eid:
            try:
                await self.hass.services.async_call(
                    "climate",
                    "turn_on",
                    {"entity_id": climate_eid},
                    blocking=True,
                    context=self.context,
                )
                return
            except TimeoutError:
                _LOGGER.debug(
                    "Climate proxy turn_on timed out for %s; falling back to P1",
                    climate_eid,
                )
            except ServiceNotFound:
                _LOGGER.warning(
                    "Climate entity %s not found; decoupling and falling back to P1",
                    climate_eid,
                )
                self._climate_entity_id = None
            except HomeAssistantError as err:
                _LOGGER.debug(
                    "Climate proxy turn_on failed transiently for %s (%s); "
                    "falling back to P1",
                    climate_eid,
                    err,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unexpected error in climate proxy turn_on for %s: %s; "
                    "falling back to P1",
                    climate_eid,
                    err,
                )
                self._climate_entity_id = None

        await self._fallback_turn_on()

    async def _fallback_turn_on(self) -> None:
        """Direct P1 fallback when the climate entity is unavailable."""

        current = str(self._overlay_power() or "").strip().lower()
        if current in {"1", "true", "on"}:
            _LOGGER.debug("Power already optimistic ON; skipping redundant P1=1")
            return

        if self._backend_power_is_on():
            optimistic_invalidate(self.hass, self._entry_id, self._device_id, "power")
            self.async_write_ha_state()
            _LOGGER.debug("Power already ON (backend); skipping redundant P1=1")
            return

        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            await self._send_event("P1", 1)
            optimistic_set(self.hass, self._entry_id, self._device_id, "power", "1")
            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the device delegating to the climate entity when possible."""

        climate_eid = self._resolve_climate_entity_id()
        if climate_eid:
            try:
                await self.hass.services.async_call(
                    "climate",
                    "turn_off",
                    {"entity_id": climate_eid},
                    blocking=True,
                    context=self.context,
                )
                return
            except TimeoutError:
                _LOGGER.debug(
                    "Climate proxy turn_off timed out for %s; falling back to P1",
                    climate_eid,
                )
            except ServiceNotFound:
                _LOGGER.warning(
                    "Climate entity %s not found; decoupling and falling back to P1",
                    climate_eid,
                )
                self._climate_entity_id = None
            except HomeAssistantError as err:
                _LOGGER.debug(
                    "Climate proxy turn_off failed transiently for %s (%s); "
                    "falling back to P1",
                    climate_eid,
                    err,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Unexpected error in climate proxy turn_off for %s: %s; "
                    "falling back to P1",
                    climate_eid,
                    err,
                )
                self._climate_entity_id = None

        await self._fallback_turn_off()

    async def _fallback_turn_off(self) -> None:
        """Direct P1 fallback when the climate entity is unavailable."""

        current = str(self._overlay_power() or "").strip().lower()
        if current in {"0", "false", "off", ""}:
            _LOGGER.debug("Power already optimistic OFF; skipping redundant P1=0")
            return

        if not self._backend_power_is_on():
            optimistic_invalidate(self.hass, self._entry_id, self._device_id, "power")
            self.async_write_ha_state()
            _LOGGER.debug("Power already OFF (backend); skipping redundant P1=0")
            return

        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            # NOTE: If _send_event raises, we bail out before applying any optimistic overlay
            await self._send_event("P1", 0)
            optimistic_set(self.hass, self._entry_id, self._device_id, "power", "0")
            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )
