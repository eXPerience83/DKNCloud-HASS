"""DKN Cloud for HASS integration setup.

Key points in this revision:
- Power switch delegates to the climate entity, inheriting away auto-exit and
  optimistic overlays while preserving a direct P1 fallback when needed.
- Post-write refreshes are coalesced per entry to avoid redundant refresh bursts
  after consecutive commands.
- All write paths share a per-device asyncio.Lock so concurrent commands from the
  UI and automations maintain deterministic ordering.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .airzone_api import AirzoneAPI
from .const import (
    CONF_ENABLE_HEAT_COOL,
    CONF_SLEEP_TIMEOUT_ENABLED,
    DOMAIN,
    INTERNAL_STALE_AFTER_SEC,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
    SCENARY_HOME,
    SCENARY_SLEEP,
    SCENARY_VACANT,
    SLEEP_TIMEOUT_GRACE_MINUTES,
)
from .helpers import device_supports_heat_cool

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SEC = 10
_OFFLINE_STALE_SECONDS = int(INTERNAL_STALE_AFTER_SEC)

_BASE_PLATFORMS: list[str] = ["climate", "sensor", "switch", "binary_sensor"]
_EXTRA_PLATFORMS: list[str] = ["number"]

# NOTE: These templates act only as a last-resort fallback; the localized
# translations under translations/*.json provide the polished copy at runtime.
_DEFAULT_NOTIFY_STRINGS: dict[str, dict[str, str]] = {
    "offline": {
        "title": "DKN Cloud offline notification",
        "message": "{name} lost the connection at {ts_local}.",
    },
    "online": {
        "title": "DKN Cloud connection restored",
        "message": "{name} reconnected at {ts_local}.",
    },
}


@dataclass(slots=True)
class SleepTracking:
    """Track sleep scenary sessions per device."""

    last_scenary: str | None = None
    sleep_started_at_utc: datetime | None = None
    force_exit_requested: bool = False


def _update_sleep_tracking_for_device(tracking: SleepTracking, scenary: str) -> None:
    """Update sleep tracking state for the provided scenary."""

    now = dt_util.utcnow()

    if scenary == SCENARY_SLEEP:
        if (
            tracking.sleep_started_at_utc is None
            or tracking.last_scenary != SCENARY_SLEEP
        ):
            tracking.sleep_started_at_utc = now
            tracking.force_exit_requested = False
    else:
        tracking.sleep_started_at_utc = None

    tracking.last_scenary = scenary


def _parse_sleep_time_minutes(device: dict[str, Any]) -> int | None:
    """Parse sleep_time minutes from a device snapshot."""

    raw = device.get("sleep_time")
    if raw is None:
        return None

    try:
        minutes = int(raw)
    except (TypeError, ValueError):  # noqa: BLE001
        return None

    return minutes if minutes >= 0 else None


def _backend_power_is_off(device: dict[str, Any]) -> bool:
    """Return True if backend power (P1) indicates the unit is OFF."""

    raw = device.get("power")
    if raw is None:
        return False
    if isinstance(raw, bool):
        return not raw
    if isinstance(raw, str):
        sval = raw.strip().lower()
        if sval in {"off", "false", "0"}:
            return True
        if sval in {"on", "true", "1"}:
            return False
    try:
        return int(str(raw).strip()) == 0
    except (TypeError, ValueError):  # noqa: BLE001
        return False


class AirzoneCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator that aggregates installations and exposes device data.

    ``data`` is a mapping of device IDs (Airzone or MAC) to device dicts as
    returned by the API, refreshed on the coordinator interval. The coordinator
    owns the ``AirzoneAPI`` instance used across platforms, triggers reauth if a
    401 is encountered, and drives notification refresh scheduling. Per-entry
    buckets under ``hass.data[DOMAIN][entry_id]`` hold auxiliary state such as
    reauth flags and notification templates.
    """

    api: AirzoneAPI


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate legacy config entries to the latest schema."""

    target_version = 2

    needs_unique_id = (
        config_entry.version < target_version or not config_entry.unique_id
    )

    if needs_unique_id:
        username = str(config_entry.data.get(CONF_USERNAME, "")).strip()
        normalized = username.casefold()
        # NOTE: We normalize the username only to compute the unique_id and detect
        # duplicates. We intentionally avoid rewriting entry.data[CONF_USERNAME]
        # during migration to preserve the original user-facing casing and to not
        # change any backend login semantics for existing entries.

        if not normalized:
            _LOGGER.warning(
                "Config entry %s lacks a username; skipping unique_id migration.",
                config_entry.entry_id,
            )
        elif config_entry.unique_id != normalized:
            duplicates = [
                entry
                for entry in hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id != config_entry.entry_id
                and entry.unique_id == normalized
            ]

            if duplicates:
                _LOGGER.warning(
                    "Config entry %s skipped unique_id migration because %s is already in use.",
                    config_entry.entry_id,
                    normalized,
                )
            else:
                hass.config_entries.async_update_entry(
                    config_entry, unique_id=normalized
                )
                _LOGGER.info(
                    "Migrated config entry %s to unique_id %s.",
                    config_entry.entry_id,
                    normalized,
                )

    if config_entry.version != target_version:
        hass.config_entries.async_update_entry(config_entry, version=target_version)

    return True


async def _async_update_data(
    hass: HomeAssistant, entry: ConfigEntry, api: AirzoneAPI
) -> dict[str, dict[str, Any]]:
    """Fetch and aggregate device data.

    - On 401, open a reauth flow (once) and raise UpdateFailed (entities unavailable).
    """
    try:
        domain_bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        sleep_tracking: dict[str, SleepTracking] = domain_bucket.setdefault(
            "sleep_tracking", {}
        )
        sleep_timeout_enabled = bool(
            entry.options.get(CONF_SLEEP_TIMEOUT_ENABLED, False)
        )

        data: dict[str, dict[str, Any]] = {}
        relations = await api.fetch_installations()

        for rel in relations or []:
            inst_id: Any | None = None
            inst = rel.get("installation")
            if isinstance(inst, dict):
                inst_id = inst.get("id") or inst.get("installation_id")
            if inst_id is None:
                inst_id = rel.get("installation_id") or rel.get("id")

            if not inst_id:
                continue

            devices = await api.fetch_devices(inst_id)
            for dev in devices or []:
                dev_id = dev.get("id")
                if not dev_id:
                    mac = str(dev.get("mac") or "").strip().lower()
                    if mac:
                        dev_id = mac
                        dev["id"] = dev_id
                    else:
                        continue
                data[str(dev_id)] = dev

        now = dt_util.utcnow()
        tracked_ids = set(sleep_tracking)
        active_ids = set(data)
        for stale_id in tracked_ids - active_ids:
            sleep_tracking.pop(stale_id, None)
        for dev_id, dev in data.items():
            raw_scenary = str(dev.get("scenary") or "").strip().lower()

            tracking = sleep_tracking.setdefault(dev_id, SleepTracking())
            if raw_scenary in {SCENARY_HOME, SCENARY_SLEEP, SCENARY_VACANT}:
                _update_sleep_tracking_for_device(tracking, raw_scenary)

            sleep_expired = False
            backend_power_off = False
            sleep_time_minutes = _parse_sleep_time_minutes(dev)

            if (
                sleep_timeout_enabled
                and tracking.sleep_started_at_utc is not None
                and sleep_time_minutes is not None
                and sleep_time_minutes > 0
            ):
                timeout_at = tracking.sleep_started_at_utc + timedelta(
                    minutes=sleep_time_minutes + SLEEP_TIMEOUT_GRACE_MINUTES
                )
                backend_power_off = _backend_power_is_off(dev)
                sleep_expired = now >= timeout_at and backend_power_off

            dev["sleep_expired"] = sleep_expired

            effective_scenary = raw_scenary
            if sleep_timeout_enabled and raw_scenary == SCENARY_SLEEP and sleep_expired:
                effective_scenary = SCENARY_HOME
            dev["effective_scenary"] = effective_scenary

        return data

    except asyncio.CancelledError:
        raise
    except ClientResponseError as cre:
        status = cre.status
        if cre.status == 401:
            bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
            if not bucket.get("reauth_requested"):
                bucket["reauth_requested"] = True
                _LOGGER.warning("Authentication expired; opening reauth flow.")
                hass.async_create_task(
                    hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                        data=dict(entry.data),
                    )
                )
            raise UpdateFailed("Authentication required (401)") from None
        raise UpdateFailed(f"Failed to update Airzone data: HTTP {status}") from None
    except Exception as err:  # noqa: BLE001
        raise UpdateFailed(
            f"Failed to update Airzone data: {type(err).__name__}"
        ) from err


async def _async_prepare_notify_strings(
    hass: HomeAssistant,
) -> dict[str, dict[str, str]]:
    """Build notification templates from translations with English fallbacks."""

    result = {kind: dict(strings) for kind, strings in _DEFAULT_NOTIFY_STRINGS.items()}

    lang_raw = (getattr(hass.config, "language", None) or "").strip()
    preferred: list[str] = []
    if lang_raw:
        preferred.append(lang_raw.lower())
        base = lang_raw.split("-")[0].lower()
        if base and base not in preferred:
            preferred.append(base)
    if "en" not in preferred:
        preferred.append("en")

    prefix = f"component.{DOMAIN}."
    for lang in preferred:
        try:
            translations = await async_get_translations(
                hass,
                lang,
                category="component",
                integration=DOMAIN,
            )
        except Exception:  # noqa: BLE001
            continue

        for key, value in translations.items():
            if not key.startswith(prefix):
                continue
            path = key[len(prefix) :]
            parts = path.split(".")
            if len(parts) != 3:
                continue
            category, kind, field = parts
            if category != "issues":
                continue
            target_field = "message" if field == "description" else field
            if kind not in result or target_field not in result[kind]:
                continue
            if value:
                result[kind][target_field] = value

    return result


class _SafeMissing:
    """Placeholder for missing values that formats safely."""

    def __repr__(self) -> str:
        return "—"

    def __str__(self) -> str:
        return "—"

    def __format__(self, _spec: str) -> str:
        return "—"


class _SafeFormatDict(dict[str, Any]):
    """Format mapping that substitutes missing keys with a neutral fallback."""

    def __missing__(self, key: str) -> _SafeMissing:
        return _SafeMissing()


def _fmt(
    strings: dict[str, dict[str, str]],
    kind: str,  # "offline" | "online"
    name: str,
    ts_local: str,
    last_iso: str | None,
    mins: int | None,
) -> tuple[str, str]:
    """Render title & message with localization; no PII in these strings."""

    templates = strings.get(kind) or _DEFAULT_NOTIFY_STRINGS[kind]
    title_tpl = templates.get("title") or _DEFAULT_NOTIFY_STRINGS[kind]["title"]
    msg_tpl = templates.get("message") or _DEFAULT_NOTIFY_STRINGS[kind]["message"]

    values = _SafeFormatDict(
        name=name,
        ts_local=ts_local,
        last_iso=last_iso if last_iso is not None else _SafeMissing(),
        mins=mins if mins is not None else _SafeMissing(),
    )

    try:
        title = title_tpl.format_map(values)
        message = msg_tpl.format_map(values)
    except Exception:  # noqa: BLE001
        fallback = _DEFAULT_NOTIFY_STRINGS[kind]
        title = fallback["title"].format_map(values)
        message = fallback["message"].format_map(values)
    return title, message


def _is_online(dev: dict[str, Any], now: datetime) -> bool:
    """Compute online state based on connection_date age.

    If the timestamp cannot be parsed, assume online to prevent false alarms.
    If absent, treat as offline because we lack evidence of connectivity.
    """
    s = dev.get("connection_date")
    if not s:
        return False
    if isinstance(s, datetime):
        dt = dt_util.as_utc(s)
    else:
        dt = dt_util.parse_datetime(str(s))
    if dt is None:
        return True
    dt = dt_util.as_utc(dt)
    age = (now - dt).total_seconds()
    return age <= _OFFLINE_STALE_SECONDS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    bucket: dict[str, Any] = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    bucket.setdefault("sleep_tracking", {})
    cfg = entry.data
    opts = entry.options

    session = async_get_clientsession(hass)
    username = cfg.get(CONF_USERNAME)

    # Read token exclusively from options (no fallback to data).
    token = opts.get("user_token")

    if not token:
        _LOGGER.warning(
            "No token available in options; raising ConfigEntryAuthFailed to trigger reauth."
        )
        raise ConfigEntryAuthFailed("Token required; reauth triggered")

    # Runtime API: token-only
    api = AirzoneAPI(username, session, password=None, token=token)

    scan_interval = int(opts.get("scan_interval", DEFAULT_SCAN_INTERVAL_SEC))
    # Runtime clamp for safety even if UI guardrails are bypassed.
    scan_interval = max(10, min(30, scan_interval))

    coordinator: AirzoneCoordinator = AirzoneCoordinator(
        hass,
        _LOGGER,
        name="airzone_data",
        update_method=lambda: _async_update_data(hass, entry, api),
        update_interval=timedelta(seconds=scan_interval),
    )
    coordinator.api = api

    await coordinator.async_config_entry_first_refresh()

    data_snapshot = coordinator.data or {}
    supports_heat_cool: bool | None
    if not data_snapshot:
        supports_heat_cool = None
    else:
        try:
            supports_heat_cool = any(
                device_supports_heat_cool(dev) for dev in data_snapshot.values()
            )
        except Exception:  # noqa: BLE001
            supports_heat_cool = None

    bucket: dict[str, Any] = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    prev_flag = bool(bucket.get("reauth_requested", False))
    bucket["api"] = api
    bucket["coordinator"] = coordinator
    bucket["reauth_requested"] = prev_flag
    bucket["scan_interval"] = scan_interval
    bucket["notify_strings"] = await _async_prepare_notify_strings(hass)
    bucket["heat_cool_supported"] = supports_heat_cool
    heat_cool_opt_in = bool(
        opts.get(CONF_ENABLE_HEAT_COOL, False) and supports_heat_cool is not False
    )
    bucket["heat_cool_opt_in"] = heat_cool_opt_in

    if opts.get(CONF_ENABLE_HEAT_COOL, False) and supports_heat_cool is False:
        _LOGGER.warning(
            "HEAT_COOL opt-in ignored: no devices expose bitmask index 3 (P2=4)."
        )

    async def _async_handle_sleep_expiry() -> None:
        sleep_expiry_lock = bucket.setdefault("sleep_expiry_lock", asyncio.Lock())
        async with sleep_expiry_lock:
            data = coordinator.data or {}
            sleep_tracking: dict[str, SleepTracking] = bucket.get("sleep_tracking", {})

            if not data:
                return

            refresh_needed = False
            for dev_id, dev in data.items():
                tracking = sleep_tracking.get(dev_id)
                if tracking is None or tracking.force_exit_requested:
                    continue

                raw_scenary = str(dev.get("scenary") or "").strip().lower()
                if raw_scenary != SCENARY_SLEEP or not dev.get("sleep_expired"):
                    continue

                if api is None:
                    _LOGGER.debug(
                        "API handle missing; skipping sleep expiry cleanup for %s",
                        dev_id,
                    )
                    continue

                try:
                    await api.async_set_scenary(dev_id, SCENARY_HOME)
                except asyncio.CancelledError:
                    raise
                except ClientResponseError as cre:
                    status = cre.status
                    _LOGGER.warning(
                        "Failed to clean up expired sleep scenary on %s (HTTP %s).",
                        dev_id,
                        status,
                    )
                    continue
                except TimeoutError:
                    _LOGGER.warning(
                        "Failed to clean up expired sleep scenary on %s (timeout)",
                        dev_id,
                    )
                    continue
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to clean up expired sleep scenary on %s (unexpected error): %s",
                        dev_id,
                        err,
                        exc_info=True,
                    )
                    continue

                tracking.force_exit_requested = True
                refresh_needed = True

            if refresh_needed:
                coordinator.async_request_refresh()

    def _on_sleep_candidate() -> None:
        existing: asyncio.Task[None] | None = bucket.get("sleep_expiry_task")
        if existing is not None and not existing.done():
            return
        data = coordinator.data or {}
        if not data:
            return
        sleep_tracking: dict[str, SleepTracking] = bucket.get("sleep_tracking", {})
        has_candidate = False
        for dev_id, dev in data.items():
            tracking = sleep_tracking.get(dev_id)
            if tracking is None or tracking.force_exit_requested:
                continue
            raw_scenary = str(dev.get("scenary") or "").strip().lower()
            if raw_scenary == SCENARY_SLEEP and dev.get("sleep_expired"):
                has_candidate = True
                break
        if not has_candidate:
            return
        bucket["sleep_expiry_task"] = hass.async_create_task(
            _async_handle_sleep_expiry()
        )

    def _cancel_sleep_expiry_task() -> None:
        task: asyncio.Task[None] | None = bucket.get("sleep_expiry_task")
        if task is not None and not task.done():
            task.cancel()

    unsub_sleep = coordinator.async_add_listener(_on_sleep_candidate)

    # ---------------- Connectivity notifications listener ----------------
    notify_state: dict[str, dict[str, Any]] = bucket.setdefault("notify_state", {})

    def _on_coordinator_update() -> None:
        now = dt_util.utcnow()
        data = coordinator.data or {}
        strings = bucket.get("notify_strings") or _DEFAULT_NOTIFY_STRINGS

        for dev_id, dev in data.items():
            try:
                name = str(dev.get("name") or dev_id)
                online = _is_online(dev, now)
                st = notify_state.setdefault(
                    dev_id,
                    {
                        "last": True,
                        "since_offline": None,
                        "notified": False,
                        "online_cancel": None,
                    },
                )
                last = bool(st["last"])

                # Transition: ONLINE -> OFFLINE
                if last and not online:
                    st["last"] = False
                    st["since_offline"] = now
                    st["notified"] = False
                    nid = f"{PN_KEY_PREFIX}{entry.entry_id}:{dev_id}"
                    nid_online = f"{nid}:online"
                    persistent_notification.async_dismiss(hass, nid_online)
                    cancel = st.get("online_cancel")
                    try:
                        if callable(cancel):
                            cancel()
                    finally:
                        st["online_cancel"] = None
                    _LOGGER.debug("[%s] offline transition started at %s", dev_id, now)
                    continue

                # While OFFLINE: check debounce and notify once
                if not last and not online:
                    since = st.get("since_offline")
                    if since is None:
                        st["since_offline"] = now
                        continue
                    if (
                        not st.get("notified")
                        and (now - since).total_seconds() >= OFFLINE_DEBOUNCE_SEC
                    ):
                        nid = f"{PN_KEY_PREFIX}{entry.entry_id}:{dev_id}"
                        ts_local = dt_util.as_local(now).strftime("%H:%M")
                        connection_date_raw = dev.get("connection_date")
                        if isinstance(connection_date_raw, datetime):
                            dt_last = dt_util.as_utc(connection_date_raw)
                            last_iso = dt_last.isoformat()
                        else:
                            connection_date_str = (
                                connection_date_raw
                                if isinstance(connection_date_raw, str)
                                else None
                            )
                            dt_last = dt_util.parse_datetime(connection_date_str or "")
                            last_iso = (
                                connection_date_str if dt_last is not None else None
                            )
                        mins = (
                            int(
                                max(
                                    0,
                                    (now - dt_util.as_utc(dt_last)).total_seconds()
                                    // 60,
                                )
                            )
                            if dt_last is not None
                            else None
                        )
                        title, message = _fmt(
                            strings, "offline", name, ts_local, last_iso, mins
                        )
                        persistent_notification.async_create(
                            hass,
                            message=message,
                            title=title,
                            notification_id=nid,
                        )
                        st["notified"] = True
                        _LOGGER.warning("[%s] WServer offline (notified).", dev_id)
                    continue

                # Transition: OFFLINE -> ONLINE
                if not last and online:
                    st["last"] = True
                    st["since_offline"] = None
                    st["notified"] = False

                    nid = f"{PN_KEY_PREFIX}{entry.entry_id}:{dev_id}"
                    persistent_notification.async_dismiss(hass, nid)

                    ts_local = dt_util.as_local(now).strftime("%H:%M")
                    title, message = _fmt(strings, "online", name, ts_local, None, None)
                    nid_online = f"{nid}:online"
                    persistent_notification.async_create(
                        hass,
                        message=message,
                        title=title,
                        notification_id=nid_online,
                    )
                    _LOGGER.info("[%s] WServer back online.", dev_id)

                    cancel = st.get("online_cancel")
                    try:
                        if callable(cancel):
                            cancel()
                    finally:
                        st["online_cancel"] = None

                    cancel = async_call_later(
                        hass,
                        ONLINE_BANNER_TTL_SEC,
                        lambda _now, _nid=nid_online: persistent_notification.async_dismiss(
                            hass, _nid
                        ),
                    )
                    if callable(cancel):
                        st["online_cancel"] = cancel
                    continue

                # No transition
                st["last"] = online
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Notification update failed for %s (%s).",
                    dev_id,
                    type(err).__name__,
                )

    unsub = coordinator.async_add_listener(_on_coordinator_update)
    entry.async_on_unload(unsub_sleep)
    entry.async_on_unload(unsub)
    entry.async_on_unload(_cancel_sleep_expiry_task)

    await hass.config_entries.async_forward_entry_setups(entry, _BASE_PLATFORMS)
    if _EXTRA_PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, _EXTRA_PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    _LOGGER.info(
        "DKN Cloud for HASS configured (scan_interval=%ss; token from options).",
        scan_interval,
    )
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    for platform in _BASE_PLATFORMS + _EXTRA_PLATFORMS:
        try:
            ok = await hass.config_entries.async_forward_entry_unload(entry, platform)
        except Exception:  # noqa: BLE001
            unload_ok = False
            _LOGGER.exception(
                "Unexpected error unloading platform %s for config entry %s.",
                platform,
                entry.entry_id,
            )
            continue

        if not ok:
            unload_ok = False
            _LOGGER.warning(
                "Platform %s did not unload cleanly for config entry %s.",
                platform,
                entry.entry_id,
            )

    domain_bucket = hass.data.get(DOMAIN)
    if domain_bucket is not None and entry.entry_id in domain_bucket:
        bucket = domain_bucket[entry.entry_id]
        cancel_sleep_expiry = bucket.get("sleep_expiry_task")
        if cancel_sleep_expiry is not None and not cancel_sleep_expiry.done():
            cancel_sleep_expiry.cancel()
        notify_state = bucket.get("notify_state", {})
        for dev_id, st in notify_state.items():
            cancel = st.get("online_cancel")
            if callable(cancel):
                try:
                    cancel()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Cancel handle failed during unload for config entry %s: %s",
                        entry.entry_id,
                        err,
                    )
            st["online_cancel"] = None
            if unload_ok:
                offline_nid = f"{PN_KEY_PREFIX}{entry.entry_id}:{dev_id}"
                persistent_notification.async_dismiss(hass, offline_nid)
                persistent_notification.async_dismiss(hass, f"{offline_nid}:online")

        # Clear transient state while preserving the bucket on partial unloads.
        bucket.pop("pending_refresh", None)
        bucket.pop("device_locks", None)

        if unload_ok:
            notify_state.clear()
            domain_bucket.pop(entry.entry_id, None)

    return unload_ok
