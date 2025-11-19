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
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientResponseError
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
    DOMAIN,
    INTERNAL_STALE_AFTER_SEC,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
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


class AirzoneCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Typed coordinator that also carries the API handle."""

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

        return data

    except asyncio.CancelledError:
        raise
    except ClientResponseError as cre:
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
            raise UpdateFailed("Authentication required (401)") from cre
        raise UpdateFailed(f"Failed to update Airzone data: HTTP {cre.status}") from cre
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

    title = title_tpl.format(name=name)
    if kind == "offline":
        message = msg_tpl.format(
            ts_local=ts_local,
            last_iso=last_iso or "—",
            mins=mins or 0,
        )
    else:
        message = msg_tpl.format(ts_local=ts_local)
    return title, message


def _is_online(dev: dict[str, Any], now: datetime) -> bool:
    """Compute online state based on connection_date age.

    If the timestamp cannot be parsed, assume online to prevent false alarms.
    If absent, treat as offline because we lack evidence of connectivity.
    """
    s = dev.get("connection_date")
    if not s:
        return False
    dt = dt_util.parse_datetime(str(s))
    if dt is None:
        return True
    dt = dt_util.as_utc(dt)
    age = (now - dt).total_seconds()
    return age <= _OFFLINE_STALE_SECONDS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry (no migrations)."""
    hass.data.setdefault(DOMAIN, {})
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

    # ---------------- Connectivity notifications listener ----------------
    notify_state: dict[str, dict[str, Any]] = bucket.setdefault("notify_state", {})

    cancel_handles: list[Callable[[], None]] = []
    bucket["cancel_handles"] = cancel_handles

    def _on_coordinator_update() -> None:
        now = dt_util.utcnow()
        data = coordinator.data or {}
        strings = bucket.get("notify_strings") or _DEFAULT_NOTIFY_STRINGS

        for dev_id, dev in data.items():
            name = str(dev.get("name") or dev_id)
            online = _is_online(dev, now)
            st = notify_state.setdefault(
                dev_id, {"last": True, "since_offline": None, "notified": False}
            )
            last = bool(st["last"])

            # Transition: ONLINE -> OFFLINE
            if last and not online:
                st["last"] = False
                st["since_offline"] = now
                st["notified"] = False
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
                    last_iso = str(dev.get("connection_date") or "—")
                    dt_last = (
                        dt_util.parse_datetime(str(dev.get("connection_date") or ""))
                        or now
                    )
                    mins = int(
                        max(0, (now - dt_util.as_utc(dt_last)).total_seconds() // 60)
                    )
                    title, message = _fmt(
                        strings, "offline", name, ts_local, last_iso, mins
                    )
                    hass.async_create_task(
                        hass.components.persistent_notification.async_create(
                            message=message, title=title, notification_id=nid
                        )
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
                hass.async_create_task(
                    hass.components.persistent_notification.async_dismiss(nid)
                )

                ts_local = dt_util.as_local(now).strftime("%H:%M")
                title, message = _fmt(strings, "online", name, ts_local, None, None)
                nid_online = f"{nid}:online"
                hass.async_create_task(
                    hass.components.persistent_notification.async_create(
                        message=message, title=title, notification_id=nid_online
                    )
                )
                _LOGGER.info("[%s] WServer back online.", dev_id)

                cancel = async_call_later(
                    hass,
                    ONLINE_BANNER_TTL_SEC,
                    lambda _now, _nid=nid_online: hass.async_create_task(
                        hass.components.persistent_notification.async_dismiss(_nid)
                    ),
                )
                cancel_handles.append(cancel)
                continue

            # No transition
            st["last"] = online

    unsub = coordinator.async_add_listener(_on_coordinator_update)
    entry.async_on_unload(unsub)

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
        for cancel in bucket.get("cancel_handles", []):
            try:
                cancel()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Cancel handle failed during unload for config entry %s: %s",
                    entry.entry_id,
                    err,
                )

        # Clear transient state while preserving the bucket on partial unloads.
        bucket["cancel_handles"] = []
        bucket.pop("pending_refresh", None)
        bucket.pop("device_locks", None)

        if unload_ok:
            domain_bucket.pop(entry.entry_id, None)

    return unload_ok

