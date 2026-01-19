"""Number entities for DKN Cloud (Airzone Cloud)."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import AirzoneCoordinator
from .airzone_api import AirzoneAPI
from .const import DOMAIN, MANUFACTURER
from .helpers import (
    acquire_device_lock,
    clamp_number,
    optimistic_get,
    optimistic_set,
    schedule_post_write_refresh,
)

# ------------------------
# Sleep time constants
# ------------------------
_SLEEP_MIN = 30
_SLEEP_MAX = 120
_SLEEP_STEP = 10

# ------------------------
# Unoccupied limits constants (hardcoded as per product UI)
# ------------------------
_UNOCC_HEAT_MIN = 12
_UNOCC_HEAT_MAX = 22
_UNOCC_COOL_MIN = 24
_UNOCC_COOL_MAX = 34
_UNOCC_STEP = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up number entities for DKN Cloud from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: AirzoneCoordinator = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[NumberEntity] = []
    for device_id, device in (coordinator.data or {}).items():
        # Sleep time
        if "sleep_time" in device:
            entities.append(
                DKNSleepTimeNumber(
                    coordinator=coordinator,
                    api=api,
                    entry_id=entry.entry_id,
                    device_id=str(device_id),
                )
            )

        # Unoccupied Heat Min (12..22)
        if "min_temp_unoccupied" in device:
            entities.append(
                DKNUnoccupiedHeatMinNumber(
                    coordinator=coordinator,
                    api=api,
                    entry_id=entry.entry_id,
                    device_id=str(device_id),
                )
            )

        # Unoccupied Cool Max (24..34)
        if "max_temp_unoccupied" in device:
            entities.append(
                DKNUnoccupiedCoolMaxNumber(
                    coordinator=coordinator,
                    api=api,
                    entry_id=entry.entry_id,
                    device_id=str(device_id),
                )
            )

    if entities:
        async_add_entities(entities)


class _BaseDKNNumber(CoordinatorEntity[AirzoneCoordinator], NumberEntity):
    """Shared logic for DKN numbers (idempotent + optimistic)."""

    _field_name: str
    _native_min: int
    _native_max: int
    _native_step: int
    _payload_wrapped: bool = True

    def __init__(
        self,
        *,
        coordinator: AirzoneCoordinator,
        api: AirzoneAPI,
        entry_id: str,
        device_id: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._entry_id = entry_id
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{unique_suffix}"
        self._attr_mode = NumberMode.SLIDER
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_native_min_value = self._native_min
        self._attr_native_max_value = self._native_max
        self._attr_native_step = self._native_step

    @property
    def _device(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._device_id, {})

    # ---------- Device registry ----------
    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info (PII-safe and unified across platforms).

        NOTE: Pass MAC via 'connections' at construction time using
        CONNECTION_NETWORK_MAC; avoid mutating the object after creation.
        """
        device = self._device
        mac = (str(device.get("mac") or "").strip()) or None
        connections = {(CONNECTION_NETWORK_MAC, mac)} if mac else None

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer=MANUFACTURER,
            model=device.get("brand") or "Airzone DKN",
            sw_version=str(device.get("firmware") or ""),
            name=device.get("name") or "Airzone Device",
            connections=connections,
        )

    # ---------- State ----------
    @property
    def available(self) -> bool:
        device = self._device
        return bool(device and device.get("available", True))

    @property
    def native_value(self) -> int | None:
        """Return the current value using the optimistic overlay when available."""
        backend = self._device.get(self._field_name)
        val = optimistic_get(
            self.hass, self._entry_id, self._device_id, self._field_name, backend
        )
        try:
            return int(val) if val is not None else None
        except Exception:  # noqa: BLE001
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new value using the API and central optimistic overlay."""

        clamped = clamp_number(
            value,
            minimum=self._native_min,
            maximum=self._native_max,
            step=self._native_step,
        )
        ivalue = int(round(float(clamped)))

        current = self.native_value
        if current is not None and current == ivalue:
            return

        payload = (
            {"device": {self._field_name: ivalue}}
            if self._payload_wrapped
            else {self._field_name: ivalue}
        )

        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            try:
                await self._api.put_device_fields(self._device_id, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise

            optimistic_set(
                self.hass, self._entry_id, self._device_id, self._field_name, ivalue
            )
            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )


class DKNSleepTimeNumber(_BaseDKNNumber):
    """Number entity to control sleep_time in minutes."""

    _field_name = "sleep_time"
    _native_min = _SLEEP_MIN
    _native_max = _SLEEP_MAX
    _native_step = _SLEEP_STEP
    _payload_wrapped = False

    _attr_has_entity_name = True
    _attr_name = "Sleep time"
    _attr_icon = "mdi:power-sleep"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES  # UI: 'min'

    def __init__(
        self,
        *,
        coordinator: AirzoneCoordinator,
        api: AirzoneAPI,
        entry_id: str,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            entry_id=entry_id,
            device_id=device_id,
            unique_suffix="sleep_time",
        )


class DKNUnoccupiedHeatMinNumber(_BaseDKNNumber):
    """Number entity to control unoccupied heat min temperature (°C)."""

    _field_name = "min_temp_unoccupied"
    _native_min = _UNOCC_HEAT_MIN
    _native_max = _UNOCC_HEAT_MAX
    _native_step = _UNOCC_STEP
    _payload_wrapped = False

    _attr_has_entity_name = True
    _attr_name = "Unoccupied Heat Temp"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        *,
        coordinator: AirzoneCoordinator,
        api: AirzoneAPI,
        entry_id: str,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            entry_id=entry_id,
            device_id=device_id,
            unique_suffix="min_temp_unoccupied",
        )


class DKNUnoccupiedCoolMaxNumber(_BaseDKNNumber):
    """Number entity to control unoccupied cool max temperature (°C)."""

    _field_name = "max_temp_unoccupied"
    _native_min = _UNOCC_COOL_MIN
    _native_max = _UNOCC_COOL_MAX
    _native_step = _UNOCC_STEP
    _payload_wrapped = False

    _attr_has_entity_name = True
    _attr_name = "Unoccupied Cool Temp"
    _attr_icon = "mdi:snowflake-thermometer"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        *,
        coordinator: AirzoneCoordinator,
        api: AirzoneAPI,
        entry_id: str,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            entry_id=entry_id,
            device_id=device_id,
            unique_suffix="max_temp_unoccupied",
        )
