"""Number platform for DKN Cloud for HASS: sleep_time and unoccupied limits.

Implements NumberEntity for:
- device 'sleep_time' via AirzoneAPI.put_device_sleep_time()
  * Valid range: 30..120 minutes, step 10.
  * Optimistic UI with short TTL, then coordinator refresh.
- device 'min_temp_unoccupied' and 'max_temp_unoccupied' via AirzoneAPI.put_device_fields()
  * Fixed ranges (hardcoded by product UI):
      - min_temp_unoccupied (HEAT): 12..22 °C (step 1)
      - max_temp_unoccupied (COOL): 24..34 °C (step 1)
  * Same optimistic/idempotent behavior and refresh.

Design notes:
- No I/O in properties; reads come from DataUpdateCoordinator.
- Entities are created only if the backend exposes the fields in GET /devices,
  same as sleep_time. If the field exists but is missing a value, HA shows 'unknown'.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import (
    DeviceInfo,
    EntityCategory,  # Place these controls under Configuration
)
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .airzone_api import AirzoneAPI
from .const import DOMAIN, OPTIMISTIC_TTL_SEC

# ------------------------
# Sleep time constants
# ------------------------
_SLEEP_MIN = 30
_SLEEP_MAX = 120
_SLEEP_STEP = 10

# ------------------------
# Unoccupied limits constants (hardcoded as per product UI)
# ------------------------
# English: Heat → minimum temperature while unoccupied
_UNOCC_HEAT_MIN = 12
_UNOCC_HEAT_MAX = 22
# English: Cool → maximum temperature while unoccupied
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
    coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]] = data["coordinator"]
    api: AirzoneAPI = data["api"]

    entities: list[NumberEntity] = []
    for device_id, device in (coordinator.data or {}).items():
        # Sleep time
        if "sleep_time" in device:
            entities.append(
                DKNSleepTimeNumber(
                    coordinator=coordinator,
                    api=api,
                    device_id=str(device_id),
                )
            )

        # Unoccupied Heat Min (12..22)
        if "min_temp_unoccupied" in device:
            entities.append(
                DKNUnoccupiedHeatMinNumber(
                    coordinator=coordinator,
                    api=api,
                    device_id=str(device_id),
                )
            )

        # Unoccupied Cool Max (24..34)
        if "max_temp_unoccupied" in device:
            entities.append(
                DKNUnoccupiedCoolMaxNumber(
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

    value: int | None = None
    valid_until_monotonic: float = 0.0


class _BaseDKNNumber(CoordinatorEntity, NumberEntity):
    """Shared logic for DKN numbers (idempotent + optimistic)."""

    # Subclasses must set:
    # - _field_name
    # - _native_min/_native_max/_native_step
    # - _attr_name, _attr_icon, _attr_native_unit_of_measurement

    _field_name: str
    _native_min: int
    _native_max: int
    _native_step: int

    def __init__(
        self,
        *,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]],
        api: AirzoneAPI,
        device_id: str,
        unique_suffix: str,
    ) -> None:
        """Initialize entity."""
        super().__init__(coordinator)
        self._api = api
        self._device_id = device_id
        self._optimistic = _OptimisticState()
        self._attr_unique_id = f"{device_id}_{unique_suffix}"
        self._attr_mode = NumberMode.SLIDER
        self._attr_entity_category = EntityCategory.CONFIG
        # Clamp bounds and set HA properties
        self._attr_native_min_value = self._native_min
        self._attr_native_max_value = self._native_max
        self._attr_native_step = self._native_step

    # ---------- Device registry ----------
    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry info (no PII)."""
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
        device = (self.coordinator.data or {}).get(self._device_id)
        return bool(device and device.get("available", True))

    @property
    def native_value(self) -> int | None:
        """Return current value (optimistic if still valid)."""
        if (
            self._optimistic.value is not None
            and self.coordinator.hass.loop.time()
            < self._optimistic.valid_until_monotonic
        ):
            return int(self._optimistic.value)

        val = (
            (self.coordinator.data or {}).get(self._device_id, {}).get(self._field_name)
        )
        try:
            return int(val) if val is not None else None
        except Exception:
            # Defensive: ignore invalid values
            return None

    async def async_set_native_value(self, value: float) -> None:
        """Set new value using the API with idempotency and optimism."""
        # Clamp and quantize
        ivalue = int(round(value / self._native_step) * self._native_step)
        ivalue = max(self._native_min, min(self._native_max, ivalue))

        # Idempotency: compare against optimistic or coordinator value
        effective: int | None
        if (
            self._optimistic.value is not None
            and self.coordinator.hass.loop.time()
            < self._optimistic.valid_until_monotonic
        ):
            effective = int(self._optimistic.value)
        else:
            raw = (
                (self.coordinator.data or {})
                .get(self._device_id, {})
                .get(self._field_name)
            )
            try:
                effective = int(raw) if raw is not None else None
            except Exception:
                effective = None

        if effective is not None and effective == ivalue:
            # English: Avoid redundant network call.
            return

        # Optimistic state window
        self._optimistic.value = ivalue
        self._optimistic.valid_until_monotonic = (
            self.coordinator.hass.loop.time() + OPTIMISTIC_TTL_SEC
        )
        self.async_write_ha_state()

        try:
            # Root-level payload, as confirmed by captured cURL.
            await self._api.put_device_fields(
                self._device_id, {self._field_name: ivalue}
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Revert optimistic state on failure
            self._optimistic.value = None
            self._optimistic.valid_until_monotonic = 0.0
            self.async_write_ha_state()
            raise
        finally:
            await self.coordinator.async_request_refresh()


# ------------------------
# Sleep time number
# ------------------------
class DKNSleepTimeNumber(_BaseDKNNumber):
    """Number entity to control sleep_time in minutes."""

    _field_name = "sleep_time"
    _native_min = _SLEEP_MIN
    _native_max = _SLEEP_MAX
    _native_step = _SLEEP_STEP

    _attr_has_entity_name = True
    _attr_name = "Sleep time"
    _attr_icon = "mdi:power-sleep"
    _attr_native_unit_of_measurement = "min"

    def __init__(
        self,
        *,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]],
        api: AirzoneAPI,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            device_id=device_id,
            unique_suffix="sleep_time",
        )


# ------------------------
# Unoccupied Heat (min)
# ------------------------
class DKNUnoccupiedHeatMinNumber(_BaseDKNNumber):
    """Number entity to control unoccupied heat min temperature (°C)."""

    _field_name = "min_temp_unoccupied"
    _native_min = _UNOCC_HEAT_MIN
    _native_max = _UNOCC_HEAT_MAX
    _native_step = _UNOCC_STEP

    _attr_has_entity_name = True
    _attr_name = "Unoccupied Heat Temp"
    _attr_icon = "mdi:home-thermometer-outline"
    _attr_native_unit_of_measurement = "°C"

    def __init__(
        self,
        *,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]],
        api: AirzoneAPI,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            device_id=device_id,
            unique_suffix="min_temp_unoccupied",
        )


# ------------------------
# Unoccupied Cool (max)
# ------------------------
class DKNUnoccupiedCoolMaxNumber(_BaseDKNNumber):
    """Number entity to control unoccupied cool max temperature (°C)."""

    _field_name = "max_temp_unoccupied"
    _native_min = _UNOCC_COOL_MIN
    _native_max = _UNOCC_COOL_MAX
    _native_step = _UNOCC_STEP

    _attr_has_entity_name = True
    _attr_name = "Unoccupied Cool Temp"
    _attr_icon = "mdi:snowflake-thermometer"
    _attr_native_unit_of_measurement = "°C"

    def __init__(
        self,
        *,
        coordinator: DataUpdateCoordinator[dict[str, dict[str, Any]]],
        api: AirzoneAPI,
        device_id: str,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            api=api,
            device_id=device_id,
            unique_suffix="max_temp_unoccupied",
        )
