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
from datetime import timedelta
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.core import Context, HomeAssistant
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
    SLEEP_CLEANUP_SETTLE_SECONDS,
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
                tracking.expiry_off_reports = 0
                tracking.last_off_evidence = None
                tracking.expiry_probe_requested = False
                tracking.expiry_verified = False
                tracking.last_scenary = SCENARY_SLEEP
                tracking.force_exit_requested = False
                tracking.observed_on = False
                tracking.consecutive_off_reports = 0
                tracking.start_known = True
                raw_minutes = (
                    _entry_bucket(hass, entry_id)
                    .get("coordinator", None)
                )
                current_device = (
                    (getattr(raw_minutes, "data", None) or {}).get(device_id, {})
                    if raw_minutes is not None
                    else {}
                )
                try:
                    minutes = int(current_device.get("sleep_time"))
                except (ValueError, TypeError):
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


def power_is_uncertain(bucket: dict[str, Any], device_id: str) -> bool:
    """Return whether cached power must be ignored for control deduplication."""
    return device_id in bucket.get("uncertain_power", {})


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

    dispatched = False

    def check_dispatch() -> None:
        nonlocal dispatched
        intent.check_dispatch()
        dispatched = True

    def finish_after_cancel(command: asyncio.Task[Any]) -> None:
        if not intent.is_active():
            return
        try:
            command.result()
        except asyncio.CancelledError, Exception:
            mark_uncertain()
            return
        if intent.is_current():
            # Accepted P1 still matters after the caller goes away. Reconcile
            # through the existing overlay plus bounded readback, not replay.
            bucket.get("uncertain_power", {}).pop(device_id, None)
            optimistic_set(hass, entry_id, device_id, "power", str(value))
            request_ambiguous_control_readback(
                hass, coordinator, entry_id=entry_id, device_id=device_id,
                bucket=bucket,
            )
        else:
            # A newer intent won locally; the late P1 still needs readback.
            mark_uncertain()

    try:
        intent.check_dispatch()
        command = asyncio.create_task(
            api.send_event(
                modmaquina_payload(device_id, "P1", value),
                on_start=check_dispatch,
                on_uncertain=mark_uncertain,
            )
        )
        try:
            await asyncio.shield(command)
        except asyncio.CancelledError:
            if not dispatched:
                command.cancel()
            else:
                mark_uncertain()
                command.add_done_callback(finish_after_cancel)
            raise
    except CommandSuperseded:
        return False

    if not intent.is_active():
        return False
    bucket.get("uncertain_power", {}).pop(device_id, None)
    optimistic_set(hass, entry_id, device_id, "power", str(value))
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


async def async_auto_exit_sleep_if_needed(
    hass: HomeAssistant,
    *,
    entry_id: str,
    device_id: str,
    device: Mapping[str, Any] | dict[str, Any],
    coordinator: DataUpdateCoordinator[Any],
    reason: str,
    is_device_on: Callable[[], bool],
    allow_away_handling: bool,
    auto_exit_away: Callable[[str], Awaitable[None]] | None = None,
    on_success: Callable[[], None] | None = None,
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
    revision = bucket.get("scene_revisions", {}).get(device_id, 0)
    power_revision = bucket.get("power_intents", {}).get(device_id, 0)
    # Snapshot the manual intent before this cleanup can wait or send.
    # A later preset request must not be discarded by our optimistic Home.
    cleanup_intent = bucket.get("scene_preset_intents", {}).get(device_id, 0)
    raw = str(device.get("scenary") or "").lower()
    if raw == SCENARY_VACANT:
        if allow_away_handling and auto_exit_away:
            await auto_exit_away(reason)
        return
    async with acquire_device_lock(hass, entry_id, device_id):
        if active_entry_bucket(hass, entry_id, coordinator) is not bucket or (
            is_current is not None and not is_current()
        ):
            return
        current = (getattr(coordinator, "data", None) or {}).get(device_id, device)
        overlay = bucket.get("optimistic", {}).get(device_id, {}).get("scenary")
        pending = bucket.setdefault("sleep_exit_pending", {})
        if (
            bucket.get("scene_revisions", {}).get(device_id, 0) != revision
            or (
                reason == "expiry"
                and bucket.get("power_intents", {}).get(device_id, 0)
                != power_revision
            )
            or (overlay and overlay.get("value") != SCENARY_SLEEP)
            or str(current.get("scenary") or "").lower() != SCENARY_SLEEP
            or device_id in pending
        ):
            return
        if bucket.get("scene_preset_intents", {}).get(device_id, 0) != cleanup_intent:
            # A manual request arrived while cleanup waited for the lock.
            # It takes precedence and must not be marked handled.
            return True
        tracking = bucket.get("sleep_tracking", {}).get(device_id)
        if reason == "expiry":
            # Keep the original backend countdown across all intermediate OFFs.
            # Both independent OFF observations must be AFTER expiry + settle.
            valid_deadline = (
                tracking is not None
                and tracking.last_scenary == SCENARY_SLEEP
                and tracking.cleanup_started_at_utc is not None
                and tracking.expected_minutes is not None
                and tracking.expected_minutes > 0
                and not tracking.duration_invalidated
                and dt_util.utcnow()
                >= (
                    tracking.cleanup_started_at_utc
                    + timedelta(
                        minutes=tracking.expected_minutes,
                        seconds=SLEEP_CLEANUP_SETTLE_SECONDS,
                    )
                )
            )
            if not (
                valid_deadline
                and current.get("sleep_expiry_verified") is True
                and tracking.expiry_verified
                and tracking.expiry_off_reports >= 2
                and tracking.expiry_power_revision == power_revision
                and str(current.get("power")).strip().lower()
                in {"0", "false", "off"}
                and device_id not in bucket.get("uncertain_power", {})
            ):
                return True
        else:
            # Only the explicit expiry workflow may exit Sleep automatically.
            # ON, mode, fan and setpoint edits preserve the original timer,
            # even if power happens to be OFF before the deadline.
            return True
        api = getattr(coordinator, "api", None)
        if api is None:
            return
        deadlines = bucket.setdefault("sleep_exit_retry_after", {})
        if hass.loop.time() < deadlines.get(device_id, 0):
            return False
        deadlines.pop(device_id, None)
        pending[device_id] = "unverified"
        unsent = False

        def check_dispatch() -> None:
            if (
                active_entry_bucket(hass, entry_id, coordinator) is not bucket
                or bucket.get("scene_revisions", {}).get(device_id, 0) != revision
                or bucket.get("power_intents", {}).get(device_id, 0)
                != power_revision
                or bucket.get("scene_preset_intents", {}).get(device_id, 0)
                != cleanup_intent
                or (is_current is not None and not is_current())
            ):
                raise CommandSuperseded

        try:
            # No retries: a transport failure may follow acceptance by the server.
            await api.async_set_scenary(
                device_id,
                SCENARY_HOME,
                retry=False,
                on_start=check_dispatch,
            )
        except CommandSuperseded:
            if active_entry_bucket(hass, entry_id, coordinator) is bucket:
                pending.pop(device_id, None)
            unsent = True
            return True
        except CommandNotSentCooldown as error:
            # No I/O occurred in this invocation; keep this session eligible.
            if active_entry_bucket(hass, entry_id, coordinator) is bucket:
                pending.pop(device_id, None)
                deadlines[device_id] = error.retry_at
            unsent = True
            return False
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
            # The PUT can complete after unload; its state belongs to the old entry.
            if active_entry_bucket(hass, entry_id, coordinator) is not bucket:
                return
            pending[device_id] = "accepted"
            optimistic_set(hass, entry_id, device_id, "scenary", SCENARY_HOME)
            # The cleanup wrote after its initial manual-intent snapshot.
            # A later queued preset may proceed past the revision fence,
            # while older requests and unrelated scene writes remain fenced.
            bucket.setdefault("scene_preset_last_write", {})[device_id] = (
                bucket["scene_revisions"][device_id],
                cleanup_intent,
            )
            if on_success:
                on_success()
        finally:
            if not unsent:
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
