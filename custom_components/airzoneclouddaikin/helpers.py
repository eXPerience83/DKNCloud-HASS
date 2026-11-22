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

from aiohttp import (
    ClientConnectorError,
    ClientResponseError,
    ConnectionTimeoutError,
    ServerFingerprintMismatch,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .airzone_api import (
    CommandNotSentCooldown,
    CommandSuperseded,
    device_write_rejected,
)
from .commands import modmaquina_payload
from .const import (
    CONF_SLEEP_CLEANUP_ENABLED,
    DOMAIN,
    MANUFACTURER,
    OPTIMISTIC_TTL_SEC,
    POST_WRITE_REFRESH_DELAY_SEC,
    REQUEST_TIMEOUT,
    SCENARY_HOME,
    SCENARY_SLEEP,
    SCENARY_VACANT,
)
from .sleep import (
    SLEEP_REARM_UNVERIFIED,
    SleepTracking,
    sleep_expiry_is_current,
    sleep_intent_revisions,
)

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
                tracking.cleanup_started_at_utc = tracking.sleep_started_at_utc
                tracking.confidence = "local_ack"
                tracking.duration_invalidated = False
                tracking.reset_expiry_session()
                tracking.last_scenary = SCENARY_SLEEP
                tracking.force_exit_requested = False
                tracking.observed_on = False
                tracking.consecutive_off_reports = 0
                tracking.start_known = True
                raw_minutes = _entry_bucket(hass, entry_id).get("coordinator", None)
                current_device = (
                    (getattr(raw_minutes, "data", None) or {}).get(device_id, {})
                    if raw_minutes is not None
                    else {}
                )
                try:
                    minutes = int(
                        optimistic_get(
                            hass,
                            entry_id,
                            device_id,
                            "sleep_time",
                            current_device.get("sleep_time"),
                        )
                    )
                except ValueError, TypeError:
                    minutes = 0
                tracking.expected_minutes = minutes if minutes > 0 else None
                if save := _entry_bucket(hass, entry_id).get("save_sleep_sessions"):
                    save()
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
class SleepExpiryFence:
    """Current evidence captured before reserving a single new intent."""

    tracking: SleepTracking
    session: int
    revisions: tuple[int, int, int]

    @classmethod
    def capture(
        cls, hass, coordinator, bucket, device_id, *, for_wake: bool = False
    ) -> SleepExpiryFence | None:
        """Capture verified expiry or a wake that needs renewed evidence."""
        tracking = bucket.get("sleep_tracking", {}).get(device_id)
        if tracking is None or not tracking.start_known:
            return None
        revisions = sleep_intent_revisions(bucket, device_id)
        evidence = tracking.expiry_revisions
        if for_wake and evidence is not None and evidence[1:] == revisions[1:]:
            # A prior power reservation may have invalidated the same OFF pair.
            # Carry the need for confirmation, never permission to use its votes.
            fence = cls(tracking, tracking.session_revision, evidence)
        else:
            fence = cls(tracking, tracking.session_revision, revisions)
        return (
            fence
            if fence.is_current(hass, coordinator, bucket, device_id, revisions)
            else None
        )

    def is_current(self, hass, coordinator, bucket, device_id, revisions) -> bool:
        """Permit only the caller's documented single intent transition."""
        return (
            bucket.get("sleep_tracking", {}).get(device_id) is self.tracking
            and self.tracking.session_revision == self.session
            and sleep_intent_revisions(bucket, device_id) == revisions
            and sleep_expiry_is_current(
                bucket,
                device_id,
                (coordinator.data or {}).get(device_id, {}),
                now=dt_util.utcnow(),
                monotonic=hass.loop.time(),
                evidence_revisions=self.revisions,
            )
        )


def _sleep_cleanup_enabled(coordinator: DataUpdateCoordinator[Any]) -> bool:
    entry = getattr(coordinator, "config_entry", None)
    return entry is None or entry.options.get(CONF_SLEEP_CLEANUP_ENABLED, True)


@dataclass(frozen=True, slots=True)
class PowerIntent:
    """An entry-owned reservation in the existing per-device intent counter."""

    hass: HomeAssistant
    coordinator: DataUpdateCoordinator[Any]
    entry_id: str
    device_id: str
    bucket: dict[str, Any]
    number: int
    explicit_on: bool = False
    sleep_expiry: SleepExpiryFence | None = None

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
    requested_value: int | None = None,
    service_context: Context | None = None,
) -> PowerIntent | None:
    """Record the request before waiting for a device lock or scene preparation."""
    if (
        (delegated := delegated_power_intent.get()) is not None
        and delegated.caller is None
        and delegated.value == requested_value
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
    explicit_on = requested_value == 1
    sleep_expiry = (
        SleepExpiryFence.capture(hass, coordinator, bucket, device_id, for_wake=True)
        if explicit_on and _sleep_cleanup_enabled(coordinator)
        else None
    )
    intents: dict[str, int] = bucket.setdefault("power_intents", {})
    number = intents.get(device_id, 0) + 1
    intents[device_id] = number
    return PowerIntent(
        hass,
        coordinator,
        entry_id,
        device_id,
        bucket,
        number,
        explicit_on,
        sleep_expiry,
    )


def power_is_uncertain(bucket: dict[str, Any], device_id: str) -> bool:
    """Return whether cached power must be ignored for control deduplication."""
    return device_id in bucket.get("uncertain_power", {})


def _record_accepted_sleep_home(hass, entry_id, device_id, bucket, preset) -> None:
    """Publish the Home acknowledgement once and let newer presets proceed."""
    optimistic_set(hass, entry_id, device_id, "scenary", SCENARY_HOME)
    _, scene, _ = sleep_intent_revisions(bucket, device_id)
    bucket.setdefault("scene_preset_last_write", {})[device_id] = scene, preset


async def async_restore_sleep_home(
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator[Any],
    entry_id: str,
    device_id: str,
    check_dispatch: Callable[[], None],
) -> bool:
    """Under the device lock, restore Home once and require a new applied read."""
    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None:
        return False
    pending = bucket.setdefault("sleep_exit_pending", {})
    if pending.get(device_id) == "rejected":
        raise HomeAssistantError(
            "Sleep restoration was rejected. Confirm Home before turning on the device."
        )
    if pending.get(device_id) == SLEEP_REARM_UNVERIFIED:
        request_ambiguous_control_readback(
            hass, coordinator, entry_id=entry_id, device_id=device_id, bucket=bucket
        )
        return False
    if device_id not in pending:
        _, _, preset = sleep_intent_revisions(bucket, device_id)
        in_flight = False

        def mark_dispatched(value: bool) -> None:
            nonlocal in_flight
            in_flight = value

        try:
            check_dispatch()
            await coordinator.api.put_device_fields(
                device_id,
                {"device": {"scenary": SCENARY_HOME}},
                retry=False,
                on_start=check_dispatch,
                on_in_flight=mark_dispatched,
            )
        except CommandSuperseded:
            return False
        except (Exception, asyncio.CancelledError) as error:
            if active_entry_bucket(
                hass, entry_id, coordinator
            ) is bucket and not device_write_rejected(error, in_flight=in_flight):
                pending[device_id] = hass.loop.create_future()
                request_ambiguous_control_readback(
                    hass,
                    coordinator,
                    entry_id=entry_id,
                    device_id=device_id,
                    bucket=bucket,
                )
            raise
        if active_entry_bucket(hass, entry_id, coordinator) is not bucket:
            return False
        if tracking := bucket.get("sleep_tracking", {}).get(device_id):
            tracking.force_exit_requested = True
        _record_accepted_sleep_home(hass, entry_id, device_id, bucket, preset)

    # Install the demand before either transport can publish. Nothing from
    # the PUT response or cache can resolve this future.
    confirmation = pending[device_id] = hass.loop.create_future()
    try:
        if manager := bucket.get("sync_manager"):
            recovery = manager.request_recovery(device_id, require_fresh=True)
            if recovery is not None:
                await asyncio.wait(
                    {confirmation, recovery},
                    timeout=REQUEST_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        else:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await coordinator.async_request_refresh()
    except TimeoutError:
        return False
    return (
        active_entry_bucket(hass, entry_id, coordinator) is bucket
        and confirmation.done()
        and confirmation.result()
    )


async def async_send_power(intent: PowerIntent, value: int) -> bool:
    """Send P1 under the device lock and record only this attempt's uncertainty."""
    hass, coordinator = intent.hass, intent.coordinator
    entry_id, device_id, bucket = intent.entry_id, intent.device_id, intent.bucket
    api = getattr(coordinator, "api", None)
    if api is None:
        _LOGGER.error("API not attached to coordinator; cannot send power command")
        return False

    if not intent.is_current():
        return False

    home_revisions = None
    pending = bucket.get("sleep_exit_pending", {})
    if (
        value == 1
        and intent.explicit_on
        and (intent.sleep_expiry is not None or device_id in pending)
    ):
        expiry = intent.sleep_expiry
        revisions = sleep_intent_revisions(bucket, device_id)
        scene_before = revisions[1]
        device = (coordinator.data or {}).get(device_id, {})
        home_confirmed = (
            str(device.get("scenary")).strip().lower() == SCENARY_HOME
            and device_id not in pending
            and expiry is not None
            and revisions[1] == expiry.revisions[1] + 1
        )
        if expiry is not None and revisions[2] != expiry.revisions[2]:
            return False
        if not home_confirmed:
            already_pending = device_id in pending
            if (
                not already_pending
                and expiry is not None
                and expiry.revisions[0] + 1 != intent.number
            ):
                request_ambiguous_control_readback(
                    hass,
                    coordinator,
                    entry_id=entry_id,
                    device_id=device_id,
                    bucket=bucket,
                )
                return False

            def check_home_write() -> None:
                if (
                    not intent.is_current()
                    or not _sleep_cleanup_enabled(coordinator)
                    or expiry is None
                    or expiry.tracking.force_exit_requested
                    or not expiry.is_current(
                        hass,
                        coordinator,
                        bucket,
                        device_id,
                        (intent.number, *expiry.revisions[1:]),
                    )
                ):
                    raise CommandSuperseded

            if not await async_restore_sleep_home(
                hass,
                coordinator,
                entry_id,
                device_id,
                check_home_write,
            ):
                return False
            scene_before += not already_pending
        home_revisions = (intent.number, scene_before, revisions[2])

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
        if home_revisions is not None:
            device = (coordinator.data or {}).get(device_id, {})
            if (
                sleep_intent_revisions(bucket, device_id) != home_revisions
                or str(device.get("scenary")).strip().lower() != SCENARY_HOME
                or str(device.get("power")).strip().lower() not in {"0", "false", "off"}
                or power_is_uncertain(bucket, device_id)
                or device_id in bucket.get("sleep_exit_pending", {})
            ):
                raise CommandSuperseded

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
    on_success: Callable[[], None] | None = None,
    is_current: Callable[[], bool] | None = None,
) -> bool:
    """Return whether automatic cleanup consumed this session's opportunity."""
    bucket = active_entry_bucket(hass, entry_id, coordinator)
    if bucket is None:
        return False
    if reason != "expiry":
        if (
            str(device.get("scenary") or "").lower() == SCENARY_VACANT
            and allow_away_handling
            and auto_exit_away
        ):
            await auto_exit_away(reason)
        return False

    revisions = sleep_intent_revisions(bucket, device_id)
    sessions = bucket.get("sleep_tracking", {})
    tracking = sessions.get(device_id)
    if tracking is None:
        return False
    session_revision = tracking.session_revision

    def check_dispatch() -> None:
        current = (getattr(coordinator, "data", None) or {}).get(device_id, {})
        if (
            active_entry_bucket(hass, entry_id, coordinator) is not bucket
            or sessions.get(device_id) is not tracking
            or tracking.session_revision != session_revision
            or tracking.force_exit_requested
            or sleep_intent_revisions(bucket, device_id) != revisions
            or (is_current is not None and not is_current())
            or not sleep_expiry_is_current(
                bucket,
                device_id,
                current,
                now=dt_util.utcnow(),
                monotonic=hass.loop.time(),
            )
        ):
            raise CommandSuperseded

    async with acquire_device_lock(hass, entry_id, device_id):
        try:
            check_dispatch()
        except CommandSuperseded:
            return False
        api = getattr(coordinator, "api", None)
        pending = bucket.setdefault("sleep_exit_pending", {})
        deadlines = bucket.setdefault("sleep_exit_retry_after", {})
        if (
            api is None
            or device_id in pending
            or hass.loop.time() < deadlines.get(device_id, 0)
        ):
            return False
        deadlines.pop(device_id, None)
        pending[device_id] = "unverified"
        attempted = True
        try:
            # No retries: a transport failure may follow acceptance by the server.
            await api.async_set_scenary(
                device_id,
                SCENARY_HOME,
                retry=False,
                on_start=check_dispatch,
            )
        except (
            CommandSuperseded,
            ClientConnectorError,
            ConnectionTimeoutError,
            ServerFingerprintMismatch,
        ):
            attempted = False
        except CommandNotSentCooldown as error:
            attempted = False
            if active_entry_bucket(hass, entry_id, coordinator) is bucket:
                deadlines[device_id] = error.retry_at
        except asyncio.CancelledError:
            raise
        except ClientResponseError as error:
            if (
                400 <= error.status < 500
                and active_entry_bucket(hass, entry_id, coordinator) is bucket
            ):
                pending[device_id] = "rejected"
            _LOGGER.warning("Sleep exit not confirmed (HTTP %s)", error.status)
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("Sleep exit remains unverified (%s)", type(error).__name__)
        else:
            if active_entry_bucket(hass, entry_id, coordinator) is bucket:
                pending[device_id] = "accepted"
                _record_accepted_sleep_home(
                    hass, entry_id, device_id, bucket, revisions[2]
                )
                if on_success:
                    on_success()
        finally:
            if attempted:
                request_ambiguous_control_readback(
                    hass,
                    coordinator,
                    entry_id=entry_id,
                    device_id=device_id,
                    bucket=bucket,
                )
            elif active_entry_bucket(hass, entry_id, coordinator) is bucket:
                pending.pop(device_id, None)
        return attempted


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
