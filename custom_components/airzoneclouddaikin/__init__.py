"""DKN Cloud for HASS integration setup (0.4.0a8, options guardrails; no migrations).

Key points in this revision:
- Token and settings are read exclusively from entry.options (no data fallback).
- Removed the `stale_after_minutes` option. Offline detection now uses a fixed
  internal threshold (10 minutes) plus a 90 s debounce for notifications.
- Select platform remains removed (preset modes are native in climate).
- Keep and cancel async_call_later handles via entry.async_on_unload(...) to avoid leaks.
- On 401 in the coordinator, open a reauth flow; on missing token in setup, raise
  ConfigEntryAuthFailed so HA triggers the reauth UI.
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
    DOMAIN,
    INTERNAL_STALE_AFTER_SEC,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SEC = 10
_OFFLINE_STALE_SECONDS = int(INTERNAL_STALE_AFTER_SEC)

_BASE_PLATFORMS: list[str] = ["climate", "sensor", "switch", "binary_sensor"]
_EXTRA_PLATFORMS: list[str] = ["number"]  # keep number for now

_DEFAULT_NOTIFY_STRINGS: dict[str, dict[str, str]] = {
    "offline": {
        "title": "DKN Cloud — {name} offline",
        "message": (
            "Connection lost at {ts_local}. "
            "Last contact: {last_iso} (about {mins} min ago)."
        ),
    },
    "online": {
        "title": "DKN Cloud — {name} back online",
        "message": "Connection restored at {ts_local}.",
    },
}


class AirzoneCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Typed coordinator that also carries the API handle."""

    api: AirzoneAPI


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
async def _async_prepare_notify_strings(hass: HomeAssistant) -> dict[str, dict[str, str]]:
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
            if category != "notification":
                continue
            if kind not in result or field not in result[kind]:
                continue
            if value:
                result[kind][field] = value

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

    bucket: dict[str, Any] = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    prev_flag = bool(bucket.get("reauth_requested", False))
    bucket["api"] = api
    bucket["coordinator"] = coordinator
    bucket["reauth_requested"] = prev_flag
    bucket["scan_interval"] = scan_interval
    bucket["notify_strings"] = await _async_prepare_notify_strings(hass)

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
                    title, message = _fmt(strings, "offline", name, ts_local, last_iso, mins)
                    hass.components.persistent_notification.async_create(
                        message=message, title=title, notification_id=nid
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
                hass.components.persistent_notification.async_dismiss(nid)

                ts_local = dt_util.as_local(now).strftime("%H:%M")
                title, message = _fmt(strings, "online", name, ts_local, None, None)
                nid_online = f"{nid}:online"
                hass.components.persistent_notification.async_create(
                    message=message, title=title, notification_id=nid_online
                )
                _LOGGER.info("[%s] WServer back online.", dev_id)

                cancel = async_call_later(
                    hass,
                    ONLINE_BANNER_TTL_SEC,
                    lambda _now, _nid=nid_online: hass.components.persistent_notification.async_dismiss(
                        _nid
                    ),
                )
                cancel_handles.append(cancel)
                entry.async_on_unload(cancel)
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
            unload_ok = unload_ok and ok
        except Exception:  # noqa: BLE001
            continue

    bucket = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {}) if unload_ok else {}
    for cancel in bucket.get("cancel_handles", []):
        try:
            cancel()
        except Exception:  # noqa: BLE001
            pass

    return unload_ok
