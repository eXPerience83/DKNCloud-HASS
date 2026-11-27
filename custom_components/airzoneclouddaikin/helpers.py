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
import math
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiohttp import ClientConnectorError, ClientResponseError, ConnectionTimeoutError
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .airzone_api import CommandNotSentCooldown, CommandRejected, CommandSuperseded
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
from .device_state import power_is_on, preset_to_scenary, scenary_to_preset

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

    bucket = _entry_bucket(hass, entry_id)
    optimistic = bucket.setdefault("optimistic", {})
    if key == "scenary":
        if value == SCENARY_SLEEP:
            bucket.get("sleep_exit_retry_after", {}).pop(device_id, None)
            bucket.get("sleep_exit_pending", {}).pop(device_id, None)
            if tracking := bucket.get("sleep_tracking", {}).get(device_id):
                # A new explicit command starts a local observation, not a physical clock.
                tracking.session_revision += 1
                tracking.sleep_started_at_utc = dt_util.utcnow()
                tracking.last_scenary = SCENARY_SLEEP
                tracking.force_exit_requested = False
                raw = (getattr(bucket.get("coordinator"), "data", None) or {}).get(
                    device_id, {}
                )
                power = optimistic_get(
                    hass, entry_id, device_id, "power", raw.get("power")
                )
                tracking.off_observed = str(power).strip().lower() in {
                    "0",
                    "false",
                    "off",
                } and sleep_off_power_is_certain(bucket, device_id)
                tracking.activation_until = (
                    tracking.sleep_started_at_utc + timedelta(seconds=60)
                    if tracking.off_observed
                    else None
                )
                tracking.activation_wake = None
                tracking.first_on_observation = None
                coordinator = bucket.get("coordinator")
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
                tracking.overdue_recorded = False
        elif tracking := bucket.get("sleep_tracking", {}).get(device_id):
            tracking.off_observed = False
        revisions = bucket.setdefault("scene_revisions", {})
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


def mark_field_write_uncertain(
    hass: HomeAssistant,
    entry_id: str,
    bucket: dict[str, Any],
    device_id: str,
    field: str,
    error: BaseException,
) -> None:
    """Fence stale deduplication after a possibly accepted field write."""
    if isinstance(
        error,
        (
            CommandNotSentCooldown,
            CommandRejected,
            CommandSuperseded,
            ClientConnectorError,
            ConnectionTimeoutError,
            ConfigEntryAuthFailed,
        ),
    ) or (isinstance(error, ClientResponseError) and 400 <= error.status < 500):
        return
    bucket.setdefault("uncertain_fields", {})[device_id, field] = object()
    optimistic_invalidate(hass, entry_id, device_id, field)


def confirm_field_read(
    uncertain: dict[tuple[str, str], object],
    at_dispatch: dict[tuple[str, str], object],
    device_id: str,
    device: dict[str, Any],
    fields: set[str],
) -> None:
    """Resolve only a valid applied field from a GET begun after uncertainty."""
    for field in fields:
        key = (device_id, field)
        marker = at_dispatch.get(key)
        if marker is None or uncertain.get(key) is not marker:
            continue
        value = device.get(field)
        if field == "mode":
            valid = str(value) in {"1", "2", "3", "4", "5", "6", "7", "8"}
        else:
            try:
                valid = math.isfinite(float(str(value)))
            except TypeError, ValueError:
                valid = False
        if valid:
            uncertain.pop(key, None)


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
    preset_intent: int
    wakes: bool

    def is_active(self) -> bool:
        return (
            active_entry_bucket(self.hass, self.entry_id, self.coordinator)
            is self.bucket
        )

    def is_current(self) -> bool:
        return (
            self.is_active()
            and self.bucket["power_intents"].get(self.device_id) == self.number
            and (
                not self.wakes
                or self.bucket.get("scene_preset_intents", {}).get(self.device_id, 0)
                == self.preset_intent
            )
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
    value: int,
    service_context: Context | None = None,
) -> PowerIntent | None:
    """Record the request before waiting for a device lock or scene preparation."""
    if (
        (delegated := delegated_power_intent.get()) is not None
        and delegated.caller is None
        and delegated.value == value
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
    tracking = bucket.get("sleep_tracking", {}).get(device_id)
    intents[device_id] = number
    if (
        value == 1
        and tracking is not None
        and tracking.activation_until is not None
        and dt_util.utcnow() < tracking.activation_until
        and (caller := asyncio.current_task()) is not None
    ):
        tracking.activation_wake = number, caller
    # OFF remains independent of preset selection, but its Home PUT does not.
    preset_intent = bucket.get("scene_preset_intents", {}).get(device_id, 0)
    return PowerIntent(
        hass,
        coordinator,
        entry_id,
        device_id,
        bucket,
        number,
        preset_intent,
        value == 1,
    )


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
        if value == 1 and sleep_wake_needs_home(intent):
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
    if value == 1 and sleep_wake_needs_home(intent):
        if not await async_prepare_sleep_wake(intent):
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
            tracking.off_observed = False
        if cancellation is not None:
            request_ambiguous_control_readback(
                hass, coordinator, entry_id=entry_id, device_id=device_id, bucket=bucket
            )
    if cancellation is not None:
        raise cancellation
    return active


def effective_preset(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    entry_id: str,
    device_id: str,
) -> str | None:
    """Resolve the scene from an acknowledged overlay or the current snapshot."""
    device = (coordinator.data or {}).get(device_id, {})
    scene = optimistic_get(
        hass,
        entry_id,
        device_id,
        "scenary",
        device.get("scenary"),
        coordinator=coordinator,
    )
    return scenary_to_preset(str(scene) if scene is not None else None)


async def async_exit_away(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    entry_id: str,
    device_id: str,
    bucket: dict[str, Any],
    preset_intent: int,
    is_current: Callable[[], bool],
) -> bool:
    """Clear effective Away under the device lock while the scene choice is current."""

    def can_clear_away() -> bool:
        return (
            is_current()
            and bucket.get("scene_preset_intents", {}).get(device_id, 0)
            == preset_intent
            and effective_preset(hass, coordinator, entry_id, device_id) == "away"
        )

    try:
        return await async_write_preset(
            hass,
            coordinator,
            entry_id,
            device_id,
            "home",
            bucket,
            preset_intent,
            can_clear_away,
            automatic_away=True,
        )
    except Exception as error:  # noqa: BLE001
        _LOGGER.debug("Automatic Away exit failed (%s)", type(error).__name__)
        return False


async def async_set_manual_power(
    intent: PowerIntent,
    value: int,
    *,
    power_matches: Callable[[], bool],
    on_write: Callable[[], None],
) -> bool | None:
    """Apply manual power and Away under lock. None means the request was stopped."""
    if not intent.is_current():
        return None

    power_written = False
    scene_written = False
    try:
        # A necessary OFF takes priority over any attempt to clear Away.
        if not value and (not power_matches() or power_is_uncertain(intent)):
            power_written = await async_send_power(intent, 0)
            if not power_written:
                return None
            on_write()
        scene_written = await async_exit_away(
            intent.hass,
            intent.coordinator,
            intent.entry_id,
            intent.device_id,
            intent.bucket,
            intent.preset_intent,
            intent.is_current,
        )
        if (
            value
            and intent.is_current()
            and (not power_matches() or power_is_uncertain(intent))
        ):
            power_written = await async_send_power(intent, 1)
        return power_written or scene_written
    finally:
        if intent.is_active() and (power_written or scene_written):
            if scene_written or value:
                on_write()
            if power_written and (value or not scene_written):
                schedule_post_write_refresh(
                    intent.hass, intent.coordinator, entry_id=intent.entry_id
                )


async def async_write_preset(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    entry_id: str,
    device_id: str,
    preset_mode: str,
    bucket: dict[str, Any],
    intent: int,
    is_current: Callable[[], bool],
    *,
    automatic_away: bool = False,
) -> bool:
    """Write a preset under the device lock and publish its acknowledged overlay."""

    def entry_active() -> bool:
        return active_entry_bucket(hass, entry_id, coordinator) is bucket

    api = getattr(coordinator, "api", None)
    scenary = preset_to_scenary(preset_mode)
    if api is None or scenary is None or not entry_active() or not is_current():
        return False

    def sleep_observed() -> bool:
        device = (coordinator.data or {}).get(device_id, {})
        raw_sleep = str(device.get("scenary") or "").lower() == "sleep"
        tracking = bucket.get("sleep_tracking", {}).get(device_id)
        return raw_sleep or (tracking is not None and tracking.last_scenary == "sleep")

    current_preset = effective_preset(hass, coordinator, entry_id, device_id)
    revisions = bucket.setdefault("scene_revisions", {})
    in_sleep = sleep_observed()
    pending = bucket.setdefault("sleep_exit_pending", {})
    entry_pending = pending.get(device_id) == "sleep"
    uncertain_scenes = bucket.setdefault("uncertain_scenes", {})
    if preset_mode == "sleep":
        if device_id in pending:
            raise HomeAssistantError(
                "Confirm the current scene before selecting Sleep."
            )
        if in_sleep or current_preset == "sleep":
            return False
    elif current_preset == preset_mode:
        status = pending.get(device_id)
        if status == "accepted" or (
            status is None
            and not in_sleep
            and (automatic_away or device_id not in uncertain_scenes)
        ):
            return False
    exits_sleep = preset_mode in {"home", "away"} and (in_sleep or device_id in pending)

    def request_readback() -> None:
        request_ambiguous_control_readback(
            hass,
            coordinator,
            entry_id=entry_id,
            device_id=device_id,
            bucket=bucket,
        )

    def fence_ambiguous_scene() -> None:
        if not entry_active():
            return
        # An accepted server-side write may have lost its HTTP reply.
        # Automatic Sleep expiry must not issue another scene command
        # before a subsequent backend read confirms convergence.
        # An ambiguous automatic Home from Away must not turn an unconfirmed
        # Sleep entry into a strict exit fence. Renew the readback identity.
        status = (
            "sleep"
            if automatic_away and entry_pending and not (in_sleep or sleep_observed())
            else "unverified"
        )
        pending[device_id] = SleepExitStatus(status)
        request_readback()

    def check_dispatch() -> None:
        if not entry_active() or not is_current():
            raise CommandSuperseded

    try:
        await api.put_device_fields(
            device_id,
            {"device": {"scenary": scenary}},
            **({"retry": False} if exits_sleep else {}),
            on_start=check_dispatch,
        )
    except CommandSuperseded:
        return False
    except (Exception, asyncio.CancelledError) as error:
        if (
            exits_sleep
            and isinstance(error, ClientResponseError)
            and error.status == 422
        ):
            raise
        if entry_active() and not isinstance(error, CommandNotSentCooldown):
            if exits_sleep:
                fence_ambiguous_scene()
            elif preset_mode == "sleep":
                pending[device_id] = SleepExitStatus("sleep")
                request_readback()
            else:
                # A definitive rejection or connection failure cannot undo
                # a previous uncertainty, but does not create a new one.
                if not isinstance(
                    error,
                    (
                        ClientConnectorError,
                        ConnectionTimeoutError,
                        ConfigEntryAuthFailed,
                    ),
                ) and not (
                    isinstance(error, ClientResponseError) and 400 <= error.status < 500
                ):
                    uncertain_scenes[device_id] = object()
                    optimistic_invalidate(hass, entry_id, device_id, "scenary")
                request_readback()
        raise

    # A late successful PUT must not recreate a removed entry bucket.
    if not entry_active():
        return False

    uncertain_scenes.pop(device_id, None)
    optimistic_set(hass, entry_id, device_id, "scenary", scenary)
    bucket.setdefault("scene_preset_last_write", {})[device_id] = (
        revisions.get(device_id, 0),
        intent,
    )
    if exits_sleep:
        # A scene ACK still needs applied remote readback.
        pending[device_id] = SleepExitStatus("accepted")
        request_readback()
    else:
        schedule_post_write_refresh(hass, coordinator, entry_id=entry_id)
    return True


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
        and not tracking.force_exit_requested
        and tracking.off_observed
        and (
            tracking.activation_until is None
            or dt_util.utcnow() >= tracking.activation_until
        )
        and not (
            (wake := tracking.activation_wake) is not None
            and wake[0] == bucket.get("power_intents", {}).get(device)
            and not wake[1].done()
        )
        and bucket.get("optimistic", {}).get(device, {}).get("scenary", {}).get("value")
        in {None, SCENARY_SLEEP}
        and str(state.get("scenary")).strip().lower() == SCENARY_SLEEP
        and str(state.get("power")).strip().lower() in {"0", "false", "off"}
        and sleep_off_power_is_certain(bucket, device)
        and device not in bucket.get("sleep_exit_pending", {})
    )


class SleepExitStatus(str):
    """Identify the specific scene write a subsequent GET may confirm."""


def sleep_wake_needs_home(intent: PowerIntent) -> bool:
    bucket, device = intent.bucket, intent.device_id
    if bucket.get("sleep_exit_pending", {}).get(device) in {
        "accepted",
        "unverified",
        "rejected",
        "sleep",
    }:
        return True
    state = (intent.coordinator.data or {}).get(device, {})
    tracking = bucket.get("sleep_tracking", {}).get(device)
    if str(state.get("scenary")).strip().lower() != SCENARY_SLEEP and (
        tracking is None or tracking.last_scenary != SCENARY_SLEEP
    ):
        return False
    wake = tracking.activation_wake if tracking is not None else None
    return wake is None or wake[0] != intent.number


async def async_prepare_sleep_wake(intent: PowerIntent) -> bool:
    """Confirm Home under the caller's device lock before waking from Sleep."""
    bucket, device, coordinator = intent.bucket, intent.device_id, intent.coordinator
    pending = bucket.setdefault("sleep_exit_pending", {})

    if pending.get(device) not in {"accepted", "unverified", "sleep"}:
        try:
            await async_write_sleep_home(
                intent.hass,
                intent.entry_id,
                device,
                coordinator,
                bucket,
                intent.check_dispatch,
            )
        except CommandSuperseded:
            return False
    if not intent.is_current():
        return False
    if manager := bucket.get("sync_manager"):
        if readback := manager.pending_recovery(device):
            await asyncio.shield(readback)
    else:
        await coordinator.async_refresh()
    if not intent.is_current():
        return False
    if (
        sleep_wake_needs_home(intent)
        or (coordinator.data or {}).get(device, {}).get("scenary") != SCENARY_HOME
    ):
        raise HomeAssistantError("Confirm Home before turning on the device.")
    return True


async def async_auto_exit_sleep_if_needed(
    hass: HomeAssistant,
    *,
    entry_id: str,
    device_id: str,
    device: Mapping[str, Any] | dict[str, Any],
    coordinator: DataUpdateCoordinator[Any],
    reason: str,
) -> None:
    """Clean up eligible OFF Sleep under the device lock."""
    # Never recreate an unloaded entry during a queued Sleep/wake recheck.
    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None:
        return
    raw = str(device.get("scenary") or "").lower()
    if raw == SCENARY_VACANT or reason != "expiry":
        return
    scope = sleep_scope(bucket, device_id)
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
            return
        try:
            await async_write_sleep_home(
                hass,
                entry_id,
                device_id,
                coordinator,
                bucket,
                check_dispatch,
                automatic=True,
            )
        except CommandSuperseded:
            return
        except CommandNotSentCooldown as error:
            deadlines[device_id] = error.retry_at
            return
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("Sleep exit not confirmed (%s)", type(error).__name__)


async def async_write_sleep_home(
    hass, entry_id, device_id, coordinator, bucket, check_dispatch, *, automatic=False
) -> None:
    check_dispatch()
    tracking = bucket.get("sleep_tracking", {}).get(device_id)
    session_revision = tracking.session_revision if automatic else None
    sent = False
    status = "unverified"

    def dispatched(value: bool) -> None:
        nonlocal sent
        sent = value

    try:
        await coordinator.api.async_set_scenary(
            device_id,
            SCENARY_HOME,
            retry=False,
            on_start=check_dispatch,
            on_in_flight=dispatched,
        )
        sent = True
        status = "accepted"
    except ClientResponseError as error:
        if error.status == 422 and not automatic:
            sent = False
        if 400 <= error.status < 500:
            status = "rejected"
        raise
    finally:
        if sent and active_entry_bucket(hass, entry_id, coordinator) is bucket:
            if not automatic or (
                bucket.get("sleep_tracking", {}).get(device_id) is tracking
                and tracking.session_revision == session_revision
            ):
                if automatic:
                    tracking.force_exit_requested = True
                bucket.setdefault("sleep_exit_pending", {})[device_id] = (
                    SleepExitStatus(status)
                )
            if automatic or status != "accepted" or bucket.get("sync_manager"):
                request_ambiguous_control_readback(
                    hass,
                    coordinator,
                    entry_id=entry_id,
                    device_id=device_id,
                    bucket=bucket,
                )


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
