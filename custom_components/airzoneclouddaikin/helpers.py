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
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .airzone_api import CommandNotSentCooldown
from .const import (
    DOMAIN,
    MANUFACTURER,
    OPTIMISTIC_TTL_SEC,
    POST_WRITE_REFRESH_DELAY_SEC,
    SCENARY_HOME,
    SCENARY_SLEEP,
    SCENARY_VACANT,
)

OptimisticEntry = dict[str, dict[str, dict[str, Any]]]

_LOGGER = logging.getLogger(__name__)


def _entry_bucket(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    return domain_bucket.setdefault(entry_id, {})


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
                tracking.observed_on = False
                tracking.consecutive_off_reports = 0
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
) -> Any:
    """Return the overlay value if still valid, otherwise the backend value."""

    optimistic = _optimistic_bucket(hass, entry_id)
    device_overlay = optimistic.get(device_id)
    if not device_overlay:
        return backend_value

    overlay = device_overlay.get(key)
    if not overlay:
        return backend_value

    expires = overlay.get("expires")
    if not isinstance(expires, (int, float)) or hass.loop.time() >= float(expires):
        if manager := _entry_bucket(hass, entry_id).get("sync_manager"):
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

    bucket = _entry_bucket(hass, entry_id)
    if bucket.get("sync_manager") is not None:
        # Existing device cycles observe accepted writes; never create a second GET.
        return None
    cancel_handles: list[Callable[[], None]] = bucket.setdefault("cancel_handles", [])

    pending: Callable[[], None] | None = bucket.get("pending_refresh")
    if callable(pending):
        try:
            pending()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                cancel_handles.remove(pending)
            except ValueError:
                pass
            bucket["pending_refresh"] = None

    if delay <= 0:
        hass.async_create_task(coordinator.async_request_refresh())
        return None

    async def _refresh(_now: Any) -> None:
        try:
            # NOTE: If this task is cancelled (e.g. during HA shutdown),
            # asyncio.CancelledError will propagate, but the cleanup in the
            # finally block below will still run.
            hass.async_create_task(coordinator.async_request_refresh())
        finally:
            if bucket.get("pending_refresh") is cancel:
                bucket["pending_refresh"] = None
            try:
                cancel_handles.remove(cancel)
            except ValueError:
                pass

    cancel = async_call_later(hass, delay, _refresh)
    bucket["pending_refresh"] = cancel
    if cancel not in cancel_handles:
        cancel_handles.append(cancel)
    return cancel


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
) -> bool | None:
    """Distinguish a no-longer-due recheck from an attempted scene exit.

    Return True if the locked recheck is no longer due or a newer manual
    scene request superseded it. Return False if cooldown prevented any
    send, and None for other handled outcomes.
    Never replay an ambiguous scene write."""
    bucket = _entry_bucket(hass, entry_id)
    revision = bucket.get("scene_revisions", {}).get(device_id, 0)
    # Snapshot the manual intent before this cleanup can wait or send.
    # A later preset request must not be discarded by our optimistic Home.
    cleanup_intent = bucket.get("scene_preset_intents", {}).get(device_id, 0)
    raw = str(device.get("scenary") or "").lower()
    if raw == SCENARY_VACANT:
        if allow_away_handling and auto_exit_away:
            await auto_exit_away(reason)
        return
    async with acquire_device_lock(hass, entry_id, device_id):
        current = (getattr(coordinator, "data", None) or {}).get(device_id, device)
        overlay = bucket.get("optimistic", {}).get(device_id, {}).get("scenary")
        pending = bucket.setdefault("sleep_exit_pending", {})
        if (
            bucket.get("closing")
            or hass.is_stopping
            or bucket.get("scene_revisions", {}).get(device_id, 0) != revision
            or (overlay and overlay.get("value") != SCENARY_SLEEP)
            or str(current.get("scenary") or "").lower() != SCENARY_SLEEP
            or device_id in pending
        ):
            return
        if bucket.get("scene_preset_intents", {}).get(device_id, 0) != cleanup_intent:
            # A manual request arrived while cleanup waited for the lock.
            # It takes precedence and must not be marked handled.
            return True
        off_confirmed = current.get("sleep_off_confirmed") is True
        if reason == "expiry":
            if not off_confirmed and current.get("sleep_snapshot_fresh") is False:
                # A cached OFF cannot justify the legacy expiry cleanup.
                return True
            if not off_confirmed and not current.get("sleep_expired"):
                # Recheck both possible cleanup triggers under the device lock.
                return True
        elif not current.get("sleep_expired") and is_device_on():
            # A wake/helper operation may still exit an OFF Sleep session;
            # an automatic cleanup must require independently observed OFF.
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
        try:
            # No retries: a transport failure may follow acceptance by the server.
            await api.async_set_scenary(device_id, SCENARY_HOME, retry=False)
        except CommandNotSentCooldown as error:
            # No I/O occurred in this invocation; keep this session eligible.
            pending.pop(device_id, None)
            deadlines[device_id] = error.retry_at
            unsent = True
            return False
        except asyncio.CancelledError:
            raise
        except ClientResponseError as error:
            if 400 <= error.status < 500:
                pending[device_id] = "rejected"
            _LOGGER.warning("Sleep exit not confirmed (HTTP %s)", error.status)
        except Exception as error:  # noqa: BLE001
            _LOGGER.warning("Sleep exit remains unverified (%s)", type(error).__name__)
        else:
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
            if not unsent and not bucket.get("closing"):
                if manager := bucket.get("sync_manager"):
                    manager.request_recovery(device_id)
                else:
                    schedule_post_write_refresh(hass, coordinator, entry_id=entry_id)


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
