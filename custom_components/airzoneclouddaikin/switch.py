"""Power switch entity for DKN Cloud (Airzone Cloud)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import AirzoneConfigEntry, AirzoneCoordinator
from .commands import modmaquina_payload
from .const import DOMAIN
from .device_state import power_is_on
from .helpers import (
    acquire_device_lock,
    async_auto_exit_sleep_if_needed,
    build_device_info,
    optimistic_get,
    optimistic_invalidate,
    optimistic_set,
    schedule_post_write_refresh,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: AirzoneConfigEntry, async_add_entities
) -> None:
    """Set up the switch platform from a config entry using the DataUpdateCoordinator."""
    coordinator = entry.runtime_data.coordinator

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
        return power_is_on(self._device.get("power"))

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

    async def _ensure_occupied_before_active_action(self, reason: str) -> None:
        await async_auto_exit_sleep_if_needed(
            self.hass,
            entry_id=self._entry_id,
            device_id=self._device_id,
            device=self._device,
            coordinator=self.coordinator,
            reason=reason,
            is_device_on=self._backend_power_is_on,
            allow_away_handling=False,
        )

    async def _send_event(self, option: str, value: Any) -> None:
        """Send a command to the device using the events endpoint."""
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API not attached to coordinator; cannot send command.")
            return
        payload = modmaquina_payload(self._device_id, option, value)
        _LOGGER.debug("Sending device control event %s", option)
        try:
            await api.send_event(payload)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Device control event %s failed (%s)", option, type(err).__name__
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
        """Return device registry info (PII-safe and unified across platforms)."""
        return build_device_info(self._device, self._device_id)

    # -----------------------------
    # Write operations
    # -----------------------------
    async def _async_delegate_climate_power(self, service: str) -> bool:
        """Return True if the sibling climate entity handled the power command."""
        climate_eid = self._resolve_climate_entity_id()
        if not climate_eid:
            return False

        # Disabled or restored-unavailable Climate entities can retain a registry
        # entry while an HA service call succeeds without reaching an entity.
        climate_state = self.hass.states.get(climate_eid)
        if climate_state is None or climate_state.state == STATE_UNAVAILABLE:
            _LOGGER.debug(
                "Climate entity %s has no usable state; falling back to P1",
                climate_eid,
            )
            return False

        if not self.hass.services.has_service("climate", service):
            # The service is not registered, so no delegated command was sent.
            # This is distinct from an exception raised *inside* a handler.
            self._climate_entity_id = None
            return False

        try:
            await self.hass.services.async_call(
                "climate",
                service,
                {"entity_id": climate_eid},
                blocking=True,
                context=self._context,
            )
            return True
        except (TimeoutError, ServiceNotFound, HomeAssistantError) as err:
            # A dispatched Climate handler may have delivered P1 before
            # raising. Never send an additional P1 with unknown outcome.
            _LOGGER.warning(
                "Climate proxy %s failed after dispatch (%s); P1 fallback suppressed",
                service,
                type(err).__name__,
            )
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Unexpected climate proxy %s error after dispatch (%s); "
                "P1 fallback suppressed",
                service,
                type(err).__name__,
            )
            raise

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the device delegating to the climate entity when possible."""
        if await self._async_delegate_climate_power("turn_on"):
            return
        await self._fallback_turn_on()

    async def _fallback_turn_on(self) -> None:
        """Direct P1 fallback when the climate entity is unavailable."""

        current = str(self._overlay_power() or "").strip().lower()
        if current in {"1", "true", "on"}:
            _LOGGER.debug("Power already optimistic ON; skipping redundant P1=1")
            return

        await self._ensure_occupied_before_active_action("switch.turn_on")

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
        if await self._async_delegate_climate_power("turn_off"):
            return
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
