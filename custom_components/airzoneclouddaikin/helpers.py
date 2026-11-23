"""Helpers for optimistic overlays, numeric clamps, locks, and refreshes.

Optimistic overlays: keep temporary values in
``hass.data[DOMAIN][entry_id]["optimistic"]`` to bridge the gap between a user
action and the next coordinator refresh. `optimistic_set` stores a value with
an adaptive TTL derived from `_adaptive_ttl` (falls back to
``OPTIMISTIC_TTL_SEC`` but stretches to ``scan_interval + 0.5`` when known).
`optimistic_get` returns the overlay while it is still valid and cleans up
expired entries; `optimistic_invalidate` removes overlays explicitly when the
backend has caught up or a write failed. All helpers rely on `hass.loop.time()`
for expiry and keep per-entry buckets keyed by config entry ID and device ID.

Other utilities: `clamp_number` enforces ranges/steps on numeric inputs,
`acquire_device_lock` returns per-device locks to serialize writes, and
`schedule_post_write_refresh` coalesces delayed coordinator refreshes after a
write to avoid redundant work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .airzone_api import CommandNotSentCooldown, CommandSuperseded
from .commands import modmaquina_payload
from .const import (
    DOMAIN,
    MANUFACTURER,
    OPTIMISTIC_TTL_SEC,
    POST_WRITE_REFRESH_DELAY_SEC,
    SCENARY_HOME,
    SCENARY_SLEEP,
    SCENARY_VACANT,
)
from .device_state import power_is_on

OptimisticEntry = dict[str, dict[str, dict[str, Any]]]

_LOGGER = logging.getLogger(__name__)


def _entry_bucket(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    return domain_bucket.setdefault(entry_id, {})


def active_entry_bucket(
    hass: HomeAssistant, entry_id: str, coordinator: DataUpdateCoordinator[Any]
) -> dict[str, Any] | None:
    """Return the live runtime owned by this coordinator, without creating it."""
    bucket = hass.data.get(DOMAIN, {}).get(entry_id)
    if (
        bucket is not None
        and bucket.get("coordinator") is coordinator
        and not bucket.get("closing")
        and not hass.is_stopping
    ):
        return bucket
    return None


def _optimistic_bucket(hass: HomeAssistant, entry_id: str) -> OptimisticEntry:
    bucket = _entry_bucket(hass, entry_id)
    optimistic: OptimisticEntry = bucket.setdefault("optimistic", {})  # type: ignore[assignment]
    return optimistic


def _adaptive_ttl(hass: HomeAssistant, entry_id: str) -> float:
    bucket = _entry_bucket(hass, entry_id)
    scan_interval = bucket.get("scan_interval")
    if bucket.get("sync_manager") is not None:
        return 30.0
    ttl = float(OPTIMISTIC_TTL_SEC)
    try:
        if scan_interval is not None:
            interval = float(scan_interval)
            ttl = max(ttl, interval + 0.5)
    except TypeError, ValueError:
        pass
    return ttl


def optimistic_set(
    hass: HomeAssistant,
    entry_id: str,
    device_id: str,
    key: str,
    value: Any,
    *,
    ttl: float | None = None,
) -> None:
    """Store an optimistic value for a device field with expiration."""

    optimistic = _optimistic_bucket(hass, entry_id)
    if key == "scenary":
        if value == SCENARY_SLEEP:
            _entry_bucket(hass, entry_id).get("sleep_exit_retry_after", {}).pop(
                device_id, None
            )
            _entry_bucket(hass, entry_id).get("sleep_exit_pending", {}).pop(
                device_id, None
            )
            if (
                tracking := _entry_bucket(hass, entry_id)
                .get("sleep_tracking", {})
                .get(device_id)
            ):
                # A new explicit command starts a local observation, not a physical clock.
                tracking.session_revision += 1
                tracking.sleep_started_at_utc = dt_util.utcnow()
                tracking.last_scenary = SCENARY_SLEEP
                tracking.force_exit_requested = False
                tracking.off_observation_scope = None
                coordinator = _entry_bucket(hass, entry_id).get("coordinator")
                duration_at_selection = (
                    (getattr(coordinator, "data", None) or {})
                    .get(device_id, {})
                    .get("sleep_time")
                )
                try:
                    duration = int(
                        optimistic_get(
                            hass,
                            entry_id,
                            device_id,
                            "sleep_time",
                            duration_at_selection,
                        )
                    )
                except TypeError, ValueError:
                    duration = 0
                tracking.expected_minutes = duration if duration > 0 else None
                tracking.start_known = tracking.expected_minutes is not None
                tracking.overdue_on_reports = 0
                tracking.last_confirmed_on_utc = None
                tracking.overdue_probe_requested = False
                tracking.overdue_recorded = False
        revisions = _entry_bucket(hass, entry_id).setdefault("scene_revisions", {})
        revisions[device_id] = revisions.get(device_id, 0) + 1
    device_overlay = optimistic.setdefault(device_id, {})

    expires_in = ttl if ttl is not None else _adaptive_ttl(hass, entry_id)
    device_overlay[key] = {
        "value": value,
        "expires": hass.loop.time() + float(expires_in),
    }


def optimistic_get(
    hass: HomeAssistant,
    entry_id: str,
    device_id: str,
    key: str,
    backend_value: Any,
    *,
    coordinator: DataUpdateCoordinator[Any] | None = None,
) -> Any:
    """Return the overlay value if still valid, otherwise the backend value."""

    bucket = (
        active_entry_bucket(hass, entry_id, coordinator)
        if coordinator is not None
        else hass.data.get(DOMAIN, {}).get(entry_id)
    )
    if bucket is None:
        return backend_value
    device_overlay = bucket.get("optimistic", {}).get(device_id)
    if not device_overlay:
        return backend_value

    overlay = device_overlay.get(key)
    if not overlay:
        return backend_value

    expires = overlay.get("expires")
    if not isinstance(expires, (int, float)) or hass.loop.time() >= float(expires):
        if manager := bucket.get("sync_manager"):
            manager.request_recovery(device_id)
        optimistic_invalidate(hass, entry_id, device_id, key)
        return backend_value

    return overlay.get("value", backend_value)


def optimistic_invalidate(
    hass: HomeAssistant, entry_id: str, device_id: str, key: str
) -> None:
    """Remove an optimistic overlay entry if present."""

    optimistic = _optimistic_bucket(hass, entry_id)
    device_overlay = optimistic.get(device_id)
    if not device_overlay:
        return

    device_overlay.pop(key, None)
    if not device_overlay:
        optimistic.pop(device_id, None)


def schedule_post_write_refresh(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    *,
    entry_id: str,
    delay: float = POST_WRITE_REFRESH_DELAY_SEC,
) -> Callable[[], None] | None:
    """Request a coordinator refresh after a short delay (coalesced per entry)."""

    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None or bucket.get("sync_manager") is not None:
        # Existing device cycles observe accepted writes; never create a second GET.
        return None
    cancel_post_write_refresh(bucket)

    async def refresh_if_active() -> None:
        if active_entry_bucket(hass, entry_id, coordinator) is bucket:
            await coordinator.async_request_refresh()

    if delay <= 0:
        hass.async_create_task(refresh_if_active())
        return None

    async def _refresh(_now: Any) -> None:
        if bucket.get("pending_refresh") is not cancel:
            return
        bucket.pop("pending_refresh")
        await refresh_if_active()

    cancel = async_call_later(hass, delay, _refresh)
    bucket["pending_refresh"] = cancel
    return cancel


def cancel_post_write_refresh(bucket: dict[str, Any]) -> None:
    """Clear and cancel the entry's pending post-write timer."""
    pending = bucket.pop("pending_refresh", None)
    if callable(pending):
        try:
            pending()
        except Exception as error:  # noqa: BLE001
            _LOGGER.debug(
                "Could not cancel post-write refresh (%s)", type(error).__name__
            )


def acquire_device_lock(
    hass: HomeAssistant, entry_id: str, device_id: str
) -> asyncio.Lock:
    """Return a shared asyncio.Lock for writes scoped to (entry, device)."""

    bucket = _entry_bucket(hass, entry_id)
    locks: dict[str, asyncio.Lock] = bucket.setdefault("device_locks", {})
    lock = locks.get(device_id)
    if lock is None:
        lock = locks[device_id] = asyncio.Lock()
    return lock


@dataclass(frozen=True, slots=True)
class PowerIntent:
    """An entry-owned reservation in the existing per-device intent counter."""

    hass: HomeAssistant
    coordinator: DataUpdateCoordinator[Any]
    entry_id: str
    device_id: str
    bucket: dict[str, Any]
    number: int

    def is_active(self) -> bool:
        return (
            active_entry_bucket(self.hass, self.entry_id, self.coordinator)
            is self.bucket
        )

    def is_current(self) -> bool:
        return (
            self.is_active()
            and self.bucket["power_intents"].get(self.device_id) == self.number
        )

    def check_dispatch(self) -> None:
        if not self.is_current():
            raise CommandSuperseded


@dataclass(frozen=True, slots=True)
class PowerDelegation:
    """A pending Switch reservation authorized by Climate's entity service."""

    intent: PowerIntent
    value: int
    context: Context
    caller: asyncio.Task | None = None


delegated_power_intent: ContextVar[PowerDelegation | None] = ContextVar(
    "dkn_delegated_power_intent", default=None
)


def reserve_power_intent(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    entry_id: str,
    device_id: str,
    *,
    delegated_value: int | None = None,
    service_context: Context | None = None,
) -> PowerIntent | None:
    """Record the request before waiting for a device lock or scene preparation."""
    if (
        (delegated := delegated_power_intent.get()) is not None
        and delegated.caller is None
        and delegated.value == delegated_value
        and delegated.context is service_context
        and delegated.intent.entry_id == entry_id
        and delegated.intent.device_id == device_id
    ):
        # The entity method consumes this authorization before it can yield.
        delegated_power_intent.set(None)
        intent = delegated.intent
        return (
            intent if intent.coordinator is coordinator and intent.is_active() else None
        )

    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None:
        return None
    intents: dict[str, int] = bucket.setdefault("power_intents", {})
    number = intents.get(device_id, 0) + 1
    intents[device_id] = number
    return PowerIntent(hass, coordinator, entry_id, device_id, bucket, number)


def power_is_uncertain(intent: PowerIntent) -> bool:
    """Ignore stale power for deduplication after an ambiguous or partial write."""
    bucket, device = intent.bucket, intent.device_id
    marker = bucket.get("uncertain_power", {}).get(device)
    if marker is None:
        return False
    overlay = bucket.get("optimistic", {}).get(device, {}).get("power")
    # Only the specific acknowledged P1 overlay can still justify a duplicate.
    return marker is not overlay or intent.hass.loop.time() >= overlay["expires"]


async def async_send_power(intent: PowerIntent, value: int) -> bool:
    """Send P1 under the device lock and record only this attempt's uncertainty."""
    hass, coordinator = intent.hass, intent.coordinator
    entry_id, device_id, bucket = intent.entry_id, intent.device_id, intent.bucket
    api = getattr(coordinator, "api", None)
    if api is None:
        _LOGGER.error("API not attached to coordinator; cannot send power command")
        return False

    def mark_uncertain() -> None:
        # A queued intent may supersede this one while the request is on the
        # wire. Its decision must still account for this dispatched write.
        if intent.is_active():
            bucket.setdefault("uncertain_power", {})[device_id] = object()
            optimistic_invalidate(hass, entry_id, device_id, "power")
            request_ambiguous_control_readback(
                hass, coordinator, entry_id=entry_id, device_id=device_id, bucket=bucket
            )

    owner = asyncio.current_task()
    in_flight = False
    cancellation: asyncio.CancelledError | None = None

    def check_dispatch() -> None:
        # The writer can resume before the owner catches CancelledError.
        if owner is not None and owner.cancelling():
            raise CommandSuperseded
        intent.check_dispatch()
        if value == 1:
            tracking = bucket.get("sleep_tracking", {}).get(device_id)
            pending = bucket.get("sleep_exit_pending", {}).get(device_id)
            raw = (coordinator.data or {}).get(device_id, {}).get("scenary")
            if pending in {"accepted", "unverified", "rejected"} or (
                str(raw).strip().lower() == SCENARY_SLEEP
                and tracking
                and tracking.end
                and tracking.off_observation_scope is not None
                and device_id not in bucket.get("uncertain_power", {})
            ):
                raise HomeAssistantError("Confirm Home before turning on the device.")

    def set_in_flight(value: bool) -> None:
        nonlocal in_flight
        in_flight = value
        if not value and cancellation is not None:
            # A definitive 429 ended the drained attempt. Keep the recorded
            # cooldown, but do not retry after the caller cancelled.
            raise CommandSuperseded

    if not intent.is_current():
        return False
    command = asyncio.create_task(
        api.send_event(
            modmaquina_payload(device_id, "P1", value),
            on_start=check_dispatch,
            on_in_flight=set_in_flight,
            on_uncertain=mark_uncertain,
        )
    )
    while True:
        try:
            await asyncio.shield(command)
            break
        except asyncio.CancelledError as error:
            if command.cancelled():
                raise (cancellation or error) from None
            cancellation = error
            if not in_flight and not command.done():
                command.cancel()
            # Rejoin under the caller's lock, even on a second cancellation.
        except Exception as error:
            if cancellation is not None:
                raise cancellation from None
            if isinstance(error, CommandSuperseded):
                return False
            raise

    active = intent.is_active()
    if active:
        bucket.get("uncertain_power", {}).pop(device_id, None)
        optimistic_set(hass, entry_id, device_id, "power", str(value))
        if value == 1 and (tracking := bucket.get("sleep_tracking", {}).get(device_id)):
            tracking.evidence_revision += 1
            tracking.off_observation_scope = None
        if cancellation is not None:
            request_ambiguous_control_readback(
                hass, coordinator, entry_id=entry_id, device_id=device_id, bucket=bucket
            )
    if cancellation is not None:
        raise cancellation
    return active


def request_ambiguous_control_readback(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    *,
    entry_id: str,
    device_id: str,
    bucket: dict[str, Any] | None,
) -> None:
    """Read back an uncertain command, never replay the possibly accepted write."""
    # An in-flight write may fail after successful HA unload. Never create
    # a fresh entry bucket or schedule a task for a removed config entry.
    if bucket is None or active_entry_bucket(hass, entry_id, coordinator) is not bucket:
        return
    if manager := bucket.get("sync_manager"):
        manager.request_recovery(device_id, require_fresh=True)
    else:
        schedule_post_write_refresh(hass, coordinator, entry_id=entry_id)


def clamp_number(
    value: float | int,
    *,
    minimum: float | int,
    maximum: float | int,
    step: float | int,
) -> float | int:
    """Clamp a numeric value and quantize it to the provided step."""

    try:
        num = float(value)
    except TypeError, ValueError:  # noqa: BLE001
        raise ValueError("Invalid numeric value") from None

    try:
        min_v = float(minimum)
        max_v = float(maximum)
    except TypeError, ValueError:  # noqa: BLE001
        raise ValueError("Invalid clamp bounds") from None

    if min_v > max_v:
        min_v, max_v = max_v, min_v

    num = max(min_v, min(max_v, num))

    try:
        step_v = float(step)
    except TypeError, ValueError:  # noqa: BLE001
        step_v = 0.0

    if step_v > 0:
        base = min_v
        steps = round((num - base) / step_v)
        num = base + steps * step_v
        num = max(min_v, min(max_v, num))

    if abs(num - round(num)) < 1e-6:
        return int(round(num))
    return num


def clamp_temperature(
    value: float | int,
    *,
    min_temp: float | int,
    max_temp: float | int,
    step: float | int,
) -> float | int:
    """Clamp a temperature value with semantics aligned to climate entities."""

    return clamp_number(value, minimum=min_temp, maximum=max_temp, step=step)


def parse_modes_bitmask(value: Any) -> str:
    """Normalize the modes bitmask to a binary string or return an empty string."""

    try:
        bitmask = str(value or "")
    except Exception:  # noqa: BLE001
        return ""

    bitmask = bitmask.strip()
    if bitmask and all(ch in "01" for ch in bitmask):
        return bitmask
    return ""


def bitmask_supports_p2(bitmask: str, code: int) -> bool:
    """Return True if the sanitized bitmask exposes the given P2 code."""

    if not bitmask:
        return False

    try:
        idx = int(code) - 1
    except TypeError, ValueError:  # noqa: BLE001
        return False

    return idx >= 0 and len(bitmask) > idx and bitmask[idx] == "1"


def device_supports_p2(device: Mapping[str, Any] | dict[str, Any], code: int) -> bool:
    """Check whether a device reports support for a given P2 value in its bitmask."""

    try:
        raw = (device or {}).get("modes")
    except Exception:  # noqa: BLE001
        raw = ""

    bitmask = parse_modes_bitmask(raw)
    return bitmask_supports_p2(bitmask, code)


def device_supports_heat_cool(device: Mapping[str, Any] | dict[str, Any]) -> bool:
    """Return True when the device bitmask exposes the HEAT_COOL (P2=4) mode."""

    return device_supports_p2(device, 4)


def sleep_scope(bucket: dict[str, Any], device: str) -> tuple:
    tracking = bucket.get("sleep_tracking", {}).get(device)
    return (
        getattr(tracking, "session_revision", None),
        getattr(tracking, "sleep_started_at_utc", None),
        getattr(tracking, "evidence_revision", None),
        getattr(tracking, "expected_minutes", None),
        *(
            bucket.get(key, {}).get(device, 0)
            for key in ("power_intents", "scene_preset_intents", "scene_revisions")
        ),
    )


def sleep_off_power_is_certain(bucket: dict[str, Any], device: str) -> bool:
    overlay = bucket.get("optimistic", {}).get(device, {}).get("power", {})
    return device not in bucket.get("uncertain_power", {}) and not power_is_on(
        overlay.get("value")
    )


def sleep_cleanup_candidate(
    bucket: dict[str, Any], device: str, state: Mapping
) -> bool:
    tracking = bucket.get("sleep_tracking", {}).get(device)
    return bool(
        tracking
        and tracking.end
        and not tracking.force_exit_requested
        and tracking.off_observation_scope == sleep_scope(bucket, device)
        and str(state.get("scenary")).strip().lower() == SCENARY_SLEEP
        and str(state.get("power")).strip().lower() in {"0", "false", "off"}
        and sleep_off_power_is_certain(bucket, device)
        and device not in bucket.get("sleep_exit_pending", {})
    )


class SleepExitStatus(str):
    """Identify the specific scene write a subsequent GET may confirm."""


async def async_auto_exit_sleep_if_needed(
    hass: HomeAssistant,
    *,
    entry_id: str,
    device_id: str,
    device: Mapping[str, Any] | dict[str, Any],
    coordinator: DataUpdateCoordinator[Any],
    reason: str,
    allow_away_handling: bool,
    auto_exit_away: Callable[[str], Awaitable[None]] | None = None,
    is_current: Callable[[], bool] | None = None,
) -> bool | None:
    """Distinguish a no-longer-due recheck from an attempted scene exit.

    Return True if the locked recheck is no longer due or a newer manual
    scene request superseded it. Return False if cooldown prevented any
    send, and None for other handled outcomes.
    Never replay an ambiguous scene write."""
    # Never recreate an unloaded entry during a queued Sleep/wake recheck.
    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None:
        return
    raw = str(device.get("scenary") or "").lower()
    if raw == SCENARY_VACANT:
        if allow_away_handling and auto_exit_away:
            await auto_exit_away(reason)
        return
    if reason != "expiry":
        return
    scope = sleep_scope(bucket, device_id)
    pending = bucket.setdefault("sleep_exit_pending", {})
    deadlines = bucket.setdefault("sleep_exit_retry_after", {})

    def check_dispatch() -> None:
        current = (coordinator.data or {}).get(device_id, {})
        entry = getattr(coordinator, "config_entry", None)
        if (
            active_entry_bucket(hass, entry_id, coordinator) is not bucket
            or (
                entry is not None
                and not entry.options.get("sleep_cleanup_enabled", True)
            )
            or sleep_scope(bucket, device_id) != scope
            or not sleep_cleanup_candidate(bucket, device_id, current)
        ):
            raise CommandSuperseded

    async with acquire_device_lock(hass, entry_id, device_id):
        api = getattr(coordinator, "api", None)
        if api is None or hass.loop.time() < deadlines.get(device_id, 0):
            return False
        tracking = bucket.get("sleep_tracking", {}).get(device_id)
        sent = False
        status = "unverified"

        def dispatched(value: bool) -> None:
            nonlocal sent
            sent = value

        try:
            check_dispatch()
            await api.async_set_scenary(
                device_id,
                SCENARY_HOME,
                retry=False,
                on_start=check_dispatch,
                on_in_flight=dispatched,
            )
            sent = True
            status = "accepted"
        except CommandSuperseded:
            return True
        except CommandNotSentCooldown as error:
            deadlines[device_id] = error.retry_at
            return False
        except asyncio.CancelledError:
            raise
        except ClientResponseError as error:
            if 400 <= error.status < 500:
                status = "rejected"
            _LOGGER.warning("Sleep exit not confirmed (HTTP %s)", error.status)
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("Sleep exit remains unverified (%s)", type(error).__name__)
        finally:
            if sent and active_entry_bucket(hass, entry_id, coordinator) is bucket:
                tracking.force_exit_requested = True
                pending[device_id] = SleepExitStatus(status)
                request_ambiguous_control_readback(
                    hass,
                    coordinator,
                    entry_id=entry_id,
                    device_id=device_id,
                    bucket=bucket,
                )
        return None if sent else True


def parse_float(value: Any, *, precision: int | None = None) -> float | None:
    """Parse a numeric value handling comma decimals, with optional rounding.

    Returns None for None, non-parseable strings, or values that raise
    TypeError/ValueError.  Strings with decimal commas (``"23,5"``) are
    normalized to ``"23.5"`` before parsing.  When *precision* is *None*
    (default) the parsed float is returned without rounding; otherwise
    ``round(result, precision)`` is applied.
    """

    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except TypeError, ValueError:
        return None
    if precision is not None:
        return round(parsed, precision)
    return parsed


def build_device_info(device: dict[str, Any], device_id: str) -> DeviceInfo:
    """Return DeviceInfo with identifiers, manufacturer, model, sw_version, name, and MAC connection."""
    stable_device_id = str(device.get("id") or device_id).strip() or device_id
    mac = (str(device.get("mac") or "").strip()) or None
    connections = {(CONNECTION_NETWORK_MAC, mac)} if mac else None

    return DeviceInfo(
        identifiers={(DOMAIN, stable_device_id)},
        manufacturer=MANUFACTURER,
        model=device.get("brand") or "Airzone DKN",
        sw_version=str(device.get("firmware") or ""),
        name=device.get("name") or "Airzone Device",
        connections=connections,
    )


def confirm_optimistic(hass, entry_id, data, touched):
    """Retire only overlays for explicitly received matching fields."""
    changed = False
    optimistic = _optimistic_bucket(hass, entry_id)
    for device, keys in touched.items():
        state = data.get(device, {})
        for key in keys:
            overlay = optimistic.get(device, {}).get(key)
            if overlay is not None and str(overlay.get("value")) == str(state.get(key)):
                optimistic_invalidate(hass, entry_id, device, key)
                changed = True
    return changed


def expire_optimistic(hass, entry_id):
    """Expire overlays even when no enabled entity reads their properties."""
    now = hass.loop.time()
    optimistic = _optimistic_bucket(hass, entry_id)
    for device, overlays in list(optimistic.items()):
        for field, overlay in list(overlays.items()):
            if overlay.get("expires", 0) <= now:
                manager = _entry_bucket(hass, entry_id).get("sync_manager")
                if manager is not None:
                    manager.request_recovery(device)
                optimistic_invalidate(hass, entry_id, device, field)
