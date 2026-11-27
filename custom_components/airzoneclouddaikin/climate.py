"""Climate entity for DKN Cloud (Airzone Cloud)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .__init__ import AirzoneConfigEntry, AirzoneCoordinator
from .airzone_api import CommandNotSentCooldown, CommandSuperseded
from .commands import modmaquina_payload
from .const import (
    CONF_ENABLE_HEAT_COOL,
    DOMAIN,
)
from .device_state import power_is_on
from .helpers import (
    PowerDelegation,
    acquire_device_lock,
    active_entry_bucket,
    async_exit_away,
    async_send_power,
    async_set_manual_power,
    async_write_preset,
    bitmask_supports_p2,
    build_device_info,
    clamp_temperature,
    delegated_power_intent,
    effective_preset,
    mark_field_write_uncertain,
    optimistic_get,
    optimistic_invalidate,
    optimistic_set,
    parse_float,
    parse_modes_bitmask,
    power_is_uncertain,
    request_ambiguous_control_readback,
    reserve_power_intent,
    schedule_post_write_refresh,
)

_LOGGER = logging.getLogger(__name__)

# Airzone mode codes observed in API:
# 1: COOL, 2: HEAT, 3: FAN_ONLY (ventilate cold-type), 4: HEAT_COOL, 5: DRY,
# 6/7: heat-cool/auto-like telemetry aliases (display only; never written),
# 8: FAN_ONLY (ventilate heat-type)
MODE_TO_HVAC: dict[str, HVACMode | None] = {
    "1": HVACMode.COOL,
    "2": HVACMode.HEAT,
    "3": HVACMode.FAN_ONLY,
    "4": HVACMode.HEAT_COOL,
    "5": HVACMode.DRY,
    "6": HVACMode.HEAT_COOL,
    "7": HVACMode.HEAT_COOL,
    "8": HVACMode.FAN_ONLY,
}
# FAN_ONLY sends P2=3 only; reported P2=8 is a telemetry alias.
HVAC_TO_MODE: dict[HVACMode, str] = {
    HVACMode.COOL: "1",
    HVACMode.HEAT: "2",
    HVACMode.FAN_ONLY: "3",
    HVACMode.HEAT_COOL: "4",
    HVACMode.DRY: "5",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: AirzoneConfigEntry, async_add_entities
) -> None:
    """Set up the climate platform from a config entry using the coordinator snapshot."""
    coordinator = entry.runtime_data.coordinator

    entities: list[AirzoneClimate] = [
        AirzoneClimate(coordinator, entry.entry_id, device_id)
        for device_id in list((coordinator.data or {}).keys())
    ]
    async_add_entities(entities)


class AirzoneClimate(CoordinatorEntity[AirzoneCoordinator], ClimateEntity):
    """Representation of an Airzone Cloud Daikin climate device."""

    _attr_has_entity_name = True
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1.0
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self, coordinator: AirzoneCoordinator, entry_id: str, device_id: str
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._device_id = device_id

        device = self._device
        name = device.get("name") or "Airzone Device"
        self._attr_name = name
        self._attr_unique_id = f"{self._device_id}_climate"

    # ---- Helpers ---------------------------------------------------------

    def _active_entry_bucket(self) -> dict[str, Any] | None:
        return active_entry_bucket(self.hass, self._entry_id, self.coordinator)

    @property
    def _device(self) -> dict[str, Any]:
        """Latest device snapshot (no I/O)."""
        return (self.coordinator.data or {}).get(self._device_id, {})  # type: ignore[no-any-return]

    def _overlay_value(self, key: str, backend_value: Any) -> Any:
        """Return the optimistic value for the given key if still valid."""
        return optimistic_get(
            self.hass,
            self._entry_id,
            self._device_id,
            key,
            backend_value,
            coordinator=self.coordinator,
        )

    def _fan_speed_max(self) -> int:
        try:
            n = int(self._device.get("availables_speeds") or 0)
            return max(0, n)
        except Exception:
            return 0

    def _use_normalized_fan_labels(self) -> bool:
        """Return True when we should expose low/medium/high instead of numeric."""
        return self._fan_speed_max() == 3

    @staticmethod
    def _num_to_label(num: str) -> str:
        """Map '1'/'2'/'3' to 'low'/'medium'/'high' (fallback to input if unknown)."""
        mapping = {"1": "low", "2": "medium", "3": "high"}
        return mapping.get(str(num), str(num))

    @staticmethod
    def _label_to_num(label: str) -> str:
        """Map 'low'/'medium'/'high' to '1'/'2'/'3' (fallback to input if unknown)."""
        norm = (label or "").strip().lower()
        mapping = {"low": "1", "medium": "2", "high": "3"}
        return mapping.get(norm, label)

    def _device_power_on(self) -> bool:
        """Normalize backend/optimistic power to bool."""
        p = self._overlay_value("power", self._device.get("power"))
        return power_is_on(p)

    def _backend_mode_code(self) -> str | None:
        raw = self._overlay_value("mode", self._device.get("mode"))
        return str(raw) if raw is not None else None

    def _modes_bitstring(self) -> str:
        return parse_modes_bitmask(self._device.get("modes"))

    def _supports_p2_value(self, code: int) -> bool:
        return bitmask_supports_p2(self._modes_bitstring(), code)

    def _preferred_ventilate_code(self) -> str | None:
        """Only advertised P2=3 is a selectable ventilation command."""
        return "3" if self._supports_p2_value(3) else None

    def _heat_cool_opt_in(self) -> bool:
        hass = getattr(self, "hass", None)
        if hass is None:
            return False

        bucket = hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        if bucket:
            return bool(bucket.get("heat_cool_opt_in"))

        entry = hass.config_entries.async_get_entry(self._entry_id)
        if entry:
            return bool(entry.options.get(CONF_ENABLE_HEAT_COOL, False))
        return False

    def _heat_cool_enabled(self) -> bool:
        return self._heat_cool_opt_in() and self._supports_p2_value(4)

    def _display_only_heat_cool_alias(self) -> bool:
        """P2=6/7 and unapproved P2=4 are read-only telemetry states."""
        code = self._backend_mode_code()
        return code in {"6", "7"} or (code == "4" and not self._heat_cool_enabled())

    def _heat_side_auto_alias(self) -> bool:
        """Reported P2=7 uses heat-side fields, unlike P2=4/6."""
        return self._backend_mode_code() == "7"

    def _hvac_from_device(self) -> HVACMode | None:
        if not self._device_power_on():
            return HVACMode.OFF
        code = self._backend_mode_code()
        if not code:
            return HVACMode.OFF
        if code not in MODE_TO_HVAC:
            return HVACMode.OFF
        return MODE_TO_HVAC[code]

    # ---- Device info -----------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        """Return rich device metadata for the device registry."""
        return build_device_info(self._device, self._device_id)

    # ---- Core state ------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode | None:
        return self._hvac_from_device()

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes = [HVACMode.OFF]
        bitstr = self._modes_bitstring()
        heat_cool_opt_in = self._heat_cool_opt_in()
        heat_cool_supported = self._supports_p2_value(4)
        heat_cool_enabled = heat_cool_supported and heat_cool_opt_in

        if bitstr:
            if self._supports_p2_value(1):
                modes.append(HVACMode.COOL)
            if self._supports_p2_value(2):
                modes.append(HVACMode.HEAT)
            if self._supports_p2_value(3):
                modes.append(HVACMode.FAN_ONLY)
            if heat_cool_enabled:
                modes.append(HVACMode.HEAT_COOL)
            if self._supports_p2_value(5):
                modes.append(HVACMode.DRY)
            return modes

        # Missing or malformed capabilities grant no permission to select a
        # mode. Reported HVAC telemetry is still displayed separately.
        return modes

    # ---- Presets (HA) ----------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        """Expose read-only Sleep monitoring without asserting physical truth."""
        dev = self._device
        return {
            "sleep_start_known": bool(dev.get("sleep_start_known", False)),
            "sleep_overdue": bool(dev.get("sleep_overdue", False)),
        }

    @property
    def preset_modes(self) -> list[str] | None:
        return ["home", "away", "sleep"]

    @property
    def preset_mode(self) -> str | None:
        return effective_preset(
            self.hass, self.coordinator, self._entry_id, self._device_id
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Write an explicitly selected scene."""
        if preset_mode not in (self.preset_modes or []):
            _LOGGER.debug(
                "Invalid preset_mode %s (allowed %s)", preset_mode, self.preset_modes
            )
            return

        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API handle missing in coordinator; cannot set scenary")
            return

        bucket = self._active_entry_bucket()
        if bucket is None:
            return

        def entry_active() -> bool:
            return self._active_entry_bucket() is bucket

        revisions = bucket.setdefault("scene_revisions", {})
        revision = revisions.get(self._device_id, 0)
        intents = bucket.setdefault("scene_preset_intents", {})
        intent = intents.get(self._device_id, 0)
        intent += 1
        intents[self._device_id] = intent
        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            if not entry_active() or intents.get(self._device_id, 0) != intent:
                # Discard superseded preset requests.
                return
            current_revision = revisions.get(self._device_id, 0)
            previous_write = bucket.get("scene_preset_last_write", {}).get(
                self._device_id
            )
            if current_revision != revision and not (
                previous_write is not None
                and previous_write[0] == current_revision
                and previous_write[1] < intent
            ):
                # A write outside this preset command path changed the scene
                # after the request was queued; do not overwrite it.
                return
            if await async_write_preset(
                self.hass,
                self.coordinator,
                self._entry_id,
                self._device_id,
                preset_mode,
                bucket,
                intent,
                lambda: intents.get(self._device_id, 0) == intent,
            ):
                self.async_write_ha_state()

    # ---- Temperature control --------------------------------------------

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float | None:
        val = self._device.get("local_temp")
        return parse_float(val)

    @property
    def target_temperature(self) -> float | None:
        mode = self.hvac_mode
        if mode == HVACMode.HEAT or (
            mode == HVACMode.HEAT_COOL and self._heat_side_auto_alias()
        ):
            val = self._overlay_value("heat_consign", self._device.get("heat_consign"))
        elif mode in (HVACMode.COOL, HVACMode.HEAT_COOL):
            val = self._overlay_value("cold_consign", self._device.get("cold_consign"))
        else:
            # DRY / FAN_ONLY / OFF: do not expose target temperature
            return None
        return parse_float(val)

    @property
    def min_temp(self) -> float:
        """Return min allowable temp (per mode; neutral combo in OFF/DRY/FAN_ONLY)."""
        dev = self._device
        mode = self.hvac_mode
        cold = parse_float(dev.get("min_limit_cold"))
        heat = parse_float(dev.get("min_limit_heat"))
        if (
            mode == HVACMode.HEAT
            or (mode == HVACMode.HEAT_COOL and self._heat_side_auto_alias())
        ) and heat is not None:
            return heat
        if mode in (HVACMode.COOL, HVACMode.HEAT_COOL) and cold is not None:
            return cold
        vals = [v for v in (cold, heat) if v is not None]
        return min(vals) if vals else 16.0

    @property
    def max_temp(self) -> float:
        """Return max allowable temp (per mode; neutral combo in OFF/DRY/FAN_ONLY)."""
        dev = self._device
        mode = self.hvac_mode
        cold = parse_float(dev.get("max_limit_cold"))
        heat = parse_float(dev.get("max_limit_heat"))
        if (
            mode == HVACMode.HEAT
            or (mode == HVACMode.HEAT_COOL and self._heat_side_auto_alias())
        ) and heat is not None:
            return heat
        if mode in (HVACMode.COOL, HVACMode.HEAT_COOL) and cold is not None:
            return cold
        vals = [v for v in (cold, heat) if v is not None]
        return max(vals) if vals else 32.0

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_TEMPERATURE not in kwargs:
            return
        try:
            requested = float(kwargs[ATTR_TEMPERATURE])
        except TypeError, ValueError:
            return

        bucket = self._active_entry_bucket()
        if bucket is None:
            return

        mode = self.hvac_mode
        if self._display_only_heat_cool_alias():
            _LOGGER.debug("Ignoring set_temperature for read-only DKN mode alias")
            return
        if mode not in (HVACMode.COOL, HVACMode.HEAT, HVACMode.HEAT_COOL):
            _LOGGER.debug("Ignoring set_temperature in mode %s", mode)
            return

        preset_intent = bucket.get("scene_preset_intents", {}).get(self._device_id, 0)

        def can_adjust() -> bool:
            return (
                self._active_entry_bucket() is bucket
                and self.hvac_mode == mode
                and not self._display_only_heat_cool_alias()
            )

        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            if not can_adjust():
                return
            if await async_exit_away(
                self.hass,
                self.coordinator,
                self._entry_id,
                self._device_id,
                bucket,
                preset_intent,
                can_adjust,
            ):
                self.async_write_ha_state()
            if not can_adjust():
                return
            temp = clamp_temperature(
                requested,
                min_temp=self.min_temp,
                max_temp=self.max_temp,
                step=1,
            )
            temp_int = int(round(float(temp)))
            if mode in (HVACMode.COOL, HVACMode.HEAT_COOL):
                await self._send_p_event("P7", f"{temp_int}.0")
                if self._active_entry_bucket() is not bucket:
                    return
                optimistic_set(
                    self.hass,
                    self._entry_id,
                    self._device_id,
                    "cold_consign",
                    temp_int,
                )
            else:
                await self._send_p_event("P8", f"{temp_int}.0")
                if self._active_entry_bucket() is not bucket:
                    return
                optimistic_set(
                    self.hass,
                    self._entry_id,
                    self._device_id,
                    "heat_consign",
                    temp_int,
                )

            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )

    # ---- Fan control -----------------------------------------------------

    @property
    def fan_modes(self) -> list[str] | None:
        """Expose common labels when exactly 3 speeds exist; otherwise numeric."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY) or mode is None:
            return None
        n = self._fan_speed_max()
        if n <= 0:
            return None
        if n == 3:
            return ["low", "medium", "high"]
        return [str(i) for i in range(1, n + 1)]

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode; map 1/2/3 to low/medium/high when normalized."""
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY) or mode is None:
            return None

        if mode == HVACMode.HEAT or self._backend_mode_code() in {"7", "8"}:
            key = "heat_speed"
        else:
            key = "cold_speed"

        val = self._overlay_value(key, self._device.get(key))
        if not val:
            return None

        sval = str(val)
        if self._use_normalized_fan_labels():
            return self._num_to_label(sval)
        return sval

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Accept normalized labels (low/medium/high) or numeric strings."""
        bucket = self._active_entry_bucket()
        if bucket is None:
            return

        if self._display_only_heat_cool_alias():
            _LOGGER.debug("Ignoring set_fan_mode for read-only DKN mode alias")
            return
        mode = self.hvac_mode
        if mode in (HVACMode.OFF, HVACMode.DRY) or mode is None:
            _LOGGER.debug("Ignoring set_fan_mode in mode %s", mode)
            return
        allowed = self.fan_modes or []
        if fan_mode not in allowed:
            _LOGGER.debug("Invalid fan_mode %s (allowed %s)", fan_mode, allowed)
            return

        preset_intent = bucket.get("scene_preset_intents", {}).get(self._device_id, 0)

        # Map label to numeric when normalized
        value_to_send = (
            self._label_to_num(fan_mode)
            if self._use_normalized_fan_labels()
            else fan_mode
        )

        if mode == HVACMode.HEAT:
            option = "P4"
            key = "heat_speed"
        elif mode in (HVACMode.COOL, HVACMode.HEAT_COOL):
            option = "P3"
            key = "cold_speed"
        else:
            code = self._backend_mode_code()
            if code == "8":
                option = "P4"
                key = "heat_speed"
            else:
                option = "P3"
                key = "cold_speed"

        def can_adjust() -> bool:
            return (
                self._active_entry_bucket() is bucket
                and self.hvac_mode == mode
                and not self._display_only_heat_cool_alias()
                and fan_mode in (self.fan_modes or [])
                # P2=3 and reported P2=8 share an HA mode but use different
                # fan channels. Recheck routing before either write.
                and (
                    mode != HVACMode.FAN_ONLY
                    or option == ("P4" if self._backend_mode_code() == "8" else "P3")
                )
            )

        lock = acquire_device_lock(self.hass, self._entry_id, self._device_id)
        async with lock:
            if not can_adjust():
                return
            if await async_exit_away(
                self.hass,
                self.coordinator,
                self._entry_id,
                self._device_id,
                bucket,
                preset_intent,
                can_adjust,
            ):
                self.async_write_ha_state()
            if not can_adjust():
                return
            await self._send_p_event(option, value_to_send)
            if self._active_entry_bucket() is not bucket:
                return
            optimistic_set(
                self.hass, self._entry_id, self._device_id, key, value_to_send
            )

            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )

    # ---- Power / mode ----------------------------------------------------

    async def async_request_call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        # HA runs this hook in the blocking service caller before scheduling
        # the entity method. call_service listeners can inherit the pending
        # reservation, but run this hook in a different caller task.
        delegation = delegated_power_intent.get()
        if delegation is None or delegation.caller is not asyncio.current_task():
            return await super().async_request_call(coro)
        token = delegated_power_intent.set(
            PowerDelegation(delegation.intent, delegation.value, delegation.context)
        )
        try:
            return await super().async_request_call(coro)
        finally:
            delegated_power_intent.reset(token)

    async def async_turn_on(self) -> None:
        await self._async_set_power(1)

    async def async_turn_off(self) -> None:
        await self._async_set_power(0)

    async def _async_set_power(self, value: int, *, hvac_off: bool = False) -> None:
        intent = reserve_power_intent(
            self.hass,
            self.coordinator,
            self._entry_id,
            self._device_id,
            value=value,
            service_context=None if hvac_off else self._context,
        )
        if intent is None:
            return

        def publish_power_state() -> None:
            if hvac_off:
                optimistic_invalidate(
                    self.hass, self._entry_id, self._device_id, "mode"
                )
            self.async_write_ha_state()

        def power_matches() -> bool:
            if hvac_off:
                return not self._device_power_on()
            power = self._overlay_value("power", self._device.get("power"))
            matching = {"1", "true", "on"} if value else {"0", "false", "off", ""}
            return str(power or "").strip().lower() in matching

        async with acquire_device_lock(self.hass, self._entry_id, self._device_id):
            if not intent.is_current():
                return
            changed = await async_set_manual_power(
                intent, value, power_matches=power_matches, on_write=publish_power_state
            )
            if hvac_off and changed is False and intent.is_current():
                publish_power_state()
                schedule_post_write_refresh(
                    self.hass, self.coordinator, entry_id=self._entry_id
                )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._async_set_power(0, hvac_off=True)
            return

        bucket = self._active_entry_bucket()
        if bucket is None:
            return

        def entry_active() -> bool:
            return self._active_entry_bucket() is bucket

        # A reported P2=6/7 is only a HEAT_COOL state alias. Never treat
        # telemetry as proof that P2=4 is selectable. Reject a forced HA
        # service call before automatic power-on or away/sleep transitions.
        if hvac_mode == HVACMode.HEAT_COOL and not self._heat_cool_enabled():
            _LOGGER.debug(
                "Ignoring set_hvac_mode=heat_cool: opt-in disabled or device unsupported"
            )
            return

        # Reject unavailable ventilation before power-on or preset transitions.
        fan_code: str | None = None
        if hvac_mode == HVACMode.FAN_ONLY:
            fan_code = self._preferred_ventilate_code()
            if fan_code is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="fan_only_unsupported",
                ) from None
        elif hvac_mode not in HVAC_TO_MODE:
            _LOGGER.debug("Ignoring unsupported HVAC mode: %s", hvac_mode)
            return
        else:
            # HA services can be called with modes not offered by the selector.
            # Reject unadvertised P2 codes before power or preset changes.
            mode_code = int(HVAC_TO_MODE[hvac_mode])
            if not self._supports_p2_value(mode_code):
                _LOGGER.debug(
                    "Ignoring set_hvac_mode=%s: P2=%s not advertised",
                    hvac_mode,
                    mode_code,
                )
                return

        # A mode selection may implicitly power on the unit. Fence this P1
        # against newer direct power commands before scene preparation awaits.
        intent = reserve_power_intent(
            self.hass, self.coordinator, self._entry_id, self._device_id, value=1
        )
        if intent is None:
            return
        async with acquire_device_lock(self.hass, self._entry_id, self._device_id):
            if await async_exit_away(
                self.hass,
                self.coordinator,
                self._entry_id,
                self._device_id,
                bucket,
                intent.preset_intent,
                intent.is_current,
            ):
                self.async_write_ha_state()
            if not intent.is_current():
                return
            # A deliberate FAN_ONLY selection normalizes the reported P2=8
            # alias to the selectable P2=3.
            normalize_fan_alias = (
                hvac_mode == HVACMode.FAN_ONLY and self._backend_mode_code() == "8"
            )
            needs_power = power_is_uncertain(intent) or not self._device_power_on()
            if (
                not needs_power
                and (self._device_id, "mode") not in bucket.get("uncertain_fields", {})
                and self._hvac_from_device() == hvac_mode
                and not normalize_fan_alias
            ):
                _LOGGER.debug(
                    "HVAC mode %s already active; skipping redundant P2", hvac_mode
                )
                return

            powered_on = False
            if needs_power:
                powered_on = await async_send_power(intent, 1)
                if not powered_on:
                    return

            # All paths above validated the mode before preparing power.
            selected_mode = (
                fan_code if hvac_mode == HVACMode.FAN_ONLY else HVAC_TO_MODE[hvac_mode]
            )
            try:
                intent.check_dispatch()
                await self._send_p_event(
                    "P2", selected_mode, on_start=intent.check_dispatch
                )
            except (Exception, asyncio.CancelledError) as error:
                if entry_active():
                    mark_field_write_uncertain(
                        self.hass, self._entry_id, bucket, self._device_id, "mode", error
                    )
                    if powered_on:
                        if not power_is_on(self._device.get("power")):
                            # Keep the accepted overlay usable for its TTL.
                            # Its removal must not make older raw power reliable.
                            overlay = bucket.get("optimistic", {}).get(
                                self._device_id, {}
                            )
                            bucket.setdefault("uncertain_power", {})[
                                self._device_id
                            ] = overlay.get("power") or object()
                        self.async_write_ha_state()
                    if powered_on or not isinstance(
                        error, (CommandSuperseded, CommandNotSentCooldown)
                    ):
                        request_ambiguous_control_readback(
                            self.hass,
                            self.coordinator,
                            entry_id=self._entry_id,
                            device_id=self._device_id,
                            bucket=bucket,
                        )
                if isinstance(error, CommandSuperseded):
                    return
                raise

            if not entry_active():
                return
            bucket.get("uncertain_fields", {}).pop((self._device_id, "mode"), None)
            optimistic_set(
                self.hass, self._entry_id, self._device_id, "mode", selected_mode
            )
            optimistic_set(self.hass, self._entry_id, self._device_id, "power", "1")
            self.async_write_ha_state()
            schedule_post_write_refresh(
                self.hass, self.coordinator, entry_id=self._entry_id
            )

    # ---- Features --------------------------------------------------------

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return IntFlag capabilities; NEVER return a plain int."""
        feats: ClimateEntityFeature = ClimateEntityFeature(0)
        mode = self.hvac_mode
        if (
            mode in (HVACMode.COOL, HVACMode.HEAT, HVACMode.HEAT_COOL)
            and not self._display_only_heat_cool_alias()
        ):
            feats |= ClimateEntityFeature.TARGET_TEMPERATURE
            feats |= ClimateEntityFeature.FAN_MODE
        elif mode == HVACMode.FAN_ONLY:
            feats |= ClimateEntityFeature.FAN_MODE
        # Intentionally dynamic per the backend/UI contract documented in info.md:
        # - DRY: setpoints are not meaningful and fan control is typically not exposed.
        # - FAN_ONLY (including backend alias P2=8): fan control only; no target
        #   temperature because P7/P8 setpoints do not apply.
        # - OFF: no temperature/fan controls.
        # - Backend aliases 6/7 and unapproved P2=4: report state but
        #   keep setpoint/fan read-only.

        # Presets always available (mapped to scenary)
        feats |= ClimateEntityFeature.PRESET_MODE

        # Explicit on/off methods implemented: advertise consistently
        feats |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        return feats

    # ---- Write helpers ---------------------------------------------------

    async def _send_p_event(
        self, option: str, value: Any, *, on_start: Callable[[], None] | None = None
    ) -> None:
        """Send a P# command using the canonical 'modmaquina' payload.

        {"event":{"cgi":"modmaquina","device_id":<id>,"option":"P#","value":...}}
        """
        bucket = self._active_entry_bucket()
        if bucket is None:
            return
        api = getattr(self.coordinator, "api", None)
        if api is None:
            _LOGGER.error("API handle missing in coordinator; cannot send_event")
            return

        payload = modmaquina_payload(self._device_id, option, value)
        try:
            await api.send_event(
                payload, **({"on_start": on_start} if on_start is not None else {})
            )
        except CommandSuperseded:
            raise
        except asyncio.CancelledError:
            if option in {"P3", "P4", "P7", "P8"}:
                request_ambiguous_control_readback(
                    self.hass,
                    self.coordinator,
                    entry_id=self._entry_id,
                    device_id=self._device_id,
                    bucket=bucket,
                )
            raise
        except Exception as err:
            if option in {"P3", "P4", "P7", "P8"} and not isinstance(
                err, CommandNotSentCooldown
            ):
                request_ambiguous_control_readback(
                    self.hass,
                    self.coordinator,
                    entry_id=self._entry_id,
                    device_id=self._device_id,
                    bucket=bucket,
                )
            _LOGGER.warning(
                "Failed to send_event %s=%s: %s",
                option,
                value,
                err,
                exc_info=True,
            )
            raise

    # ---- Coordinator update hook ----------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._active_entry_bucket() is None:
            return
        device = self._device
        name = device.get("name") or self._attr_name
        if name:
            self._attr_name = name
        super()._handle_coordinator_update()

    # ---- Availability / update ------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._device)
