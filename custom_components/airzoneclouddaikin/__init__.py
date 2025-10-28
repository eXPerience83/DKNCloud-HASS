"""DKN Cloud for HASS integration setup with connectivity notifications (PR A).

Key points implemented in this file:
- Token-only storage + reauth flow on 401 (as before).
- DataUpdateCoordinator fetch remains the same.
- NEW: Post-refresh listener detects ONLINE↔OFFLINE transitions per device_id
  using connection_date + stale_after_minutes, with a 90s debounce to avoid
  flapping, and manages persistent notifications:
  * One "offline" notification per device (stable notification_id).
  * Auto-dismiss of the "offline" banner when the device comes back online.
  * Optional "back online" banner that auto-closes after 20s.
- No I/O in entity properties; everything happens around the coordinator.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .airzone_api import AirzoneAPI
from .const import (
    CONF_STALE_AFTER_MINUTES,
    DOMAIN,
    OFFLINE_DEBOUNCE_SEC,
    ONLINE_BANNER_TTL_SEC,
    PN_KEY_PREFIX,
    PN_MESSAGES,
    PN_TITLES,
    STALE_AFTER_MINUTES_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SEC = 10
_BASE_PLATFORMS: list[str] = ["climate", "sensor", "switch", "binary_sensor"]


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
        # English: propagate cancellations without converting them into UpdateFailed
        # so HA reload/stop remains clean.
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
                        context={"source": "reauth", "entry_id": entry.entry_id},
                        data={CONF_USERNAME: entry.data.get(CONF_USERNAME)},
                    )
                )
            raise UpdateFailed("Authentication required (401)") from cre
        raise UpdateFailed(f"Failed to update Airzone data: HTTP {cre.status}") from cre
    except Exception as err:  # noqa: BLE001
        raise UpdateFailed(
            f"Failed to update Airzone data: {type(err).__name__}"
        ) from err


def _choose_lang(hass: HomeAssistant) -> str:
    """Choose language code. Prefer Spanish if HA is set to Spanish."""
    lang = (getattr(hass.config, "language", None) or "").lower()
    return "es" if lang.startswith("es") else "en"


def _fmt(
    hass: HomeAssistant,
    kind: str,  # "offline" | "online"
    name: str,
    ts_local: str,
    last_iso: str | None,
    mins: int | None,
) -> tuple[str, str]:
    """Render title & message with minimal localization.

    English: We avoid full runtime i18n complexity for now; we pick ES if HA
    language starts with 'es', otherwise EN. No PII in these strings.
    """
    lang = _choose_lang(hass)
    titles = PN_TITLES.get(lang) or PN_TITLES["en"]
    msgs = PN_MESSAGES.get(lang) or PN_MESSAGES["en"]

    title = titles[kind].format(name=name)
    if kind == "offline":
        message = msgs[kind].format(
            ts_local=ts_local, last_iso=last_iso or "—", mins=mins or 0
        )
    else:
        message = msgs[kind].format(ts_local=ts_local)
    return title, message


def _is_online(dev: dict[str, Any], now: datetime, stale_minutes: int) -> bool:
    """Compute online state based on connection_date age.

    English: If the timestamp cannot be parsed, assume online to prevent false
    alarms. If absent, treat as offline because we lack evidence of connectivity.
    """
    s = dev.get("connection_date")
    if not s:
        return False
    dt = dt_util.parse_datetime(str(s))
    if dt is None:
        return True
    # Normalize to UTC for age calculation
    dt = dt_util.as_utc(dt)
    age = (now - dt).total_seconds()
    return age <= stale_minutes * 60


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    cfg = entry.data

    session = async_get_clientsession(hass)
    # Canonical key is CONF_USERNAME, which stores the email.
    username = cfg.get(CONF_USERNAME)
    token = cfg.get("user_token")
    password = cfg.get("password")  # legacy only

    # --- Migration: password -> token (one-time) ---
    if password and not token:
        _LOGGER.info("Migrating entry to token-only storage (one-time login).")
        legacy_api = AirzoneAPI(username, session, password=password, token=None)
        try:
            ok = await legacy_api.login()
        except asyncio.CancelledError:
            # English: do not convert cancel into NotReady; just bubble up.
            raise
        except Exception as exc:  # network/429/5xx/timeout/etc.
            # Do NOT open reauth on transient errors; let HA retry cleanly.
            _LOGGER.warning(
                "Migration deferred due to %s; will retry later.",
                type(exc).__name__,
            )
            raise ConfigEntryNotReady("Temporary error during migration") from exc

        if not ok:
            # Invalid auth or unexpected response (no token): require reauth.
            _LOGGER.warning("Migration invalid auth; requiring reauth.")
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "reauth", "entry_id": entry.entry_id},
                    data={CONF_USERNAME: username},
                )
            )
            raise ConfigEntryNotReady("Reauth required to migrate token")

        # Persist new token and purge password
        token = legacy_api.token
        new_data = dict(cfg)
        new_data.pop("password", None)
        new_data["user_token"] = token
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.info("Migration completed: password purged; token stored.")

    # Runtime API: token-only
    api = AirzoneAPI(username, session, password=None, token=token)

    scan_interval = int(
        entry.options.get(
            "scan_interval", cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL_SEC)
        )
    )

    coordinator: AirzoneCoordinator = AirzoneCoordinator(
        hass,
        _LOGGER,
        name="airzone_data",
        update_method=lambda: _async_update_data(hass, entry, api),
        update_interval=timedelta(seconds=max(10, scan_interval)),
    )
    coordinator.api = api

    # First refresh: may set reauth_requested=True inside _async_update_data
    await coordinator.async_config_entry_first_refresh()

    # ---- Preserve any previously-set 'reauth_requested' flag (FIX) ----
    bucket = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    prev_flag = bool(bucket.get("reauth_requested", False))
    bucket["api"] = api
    bucket["coordinator"] = coordinator
    # Keep the flag if it was set during first refresh; otherwise default to False.
    bucket["reauth_requested"] = prev_flag

    # ---------------- NEW: connectivity notifications listener -------------
    # English: We keep a small per-device state (previous online/offline and
    # the instant when the offline transition was first seen).
    notify_state: dict[str, dict[str, Any]] = bucket.setdefault("notify_state", {})
    stale_minutes = int(
        entry.options.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
    )

    def _on_coordinator_update() -> None:
        now = dt_util.utcnow()
        data = coordinator.data or {}

        for dev_id, dev in data.items():
            name = str(dev.get("name") or dev_id)
            online = _is_online(dev, now, stale_minutes)
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
                continue  # process remaining devices in this cycle

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
                    # Build and create the persistent notification (single ID)
                    nid = f"{PN_KEY_PREFIX}{entry.entry_id}:{dev_id}"
                    ts_local = dt_util.as_local(now).strftime("%H:%M")
                    last_iso = str(dev.get("connection_date") or "—")
                    # Compute minutes (approximate)
                    dt_last = (
                        dt_util.parse_datetime(str(dev.get("connection_date") or ""))
                        or now
                    )
                    mins = int(
                        max(
                            0,
                            (now - dt_util.as_utc(dt_last)).total_seconds() // 60,
                        )
                    )
                    title, message = _fmt(
                        hass, "offline", name, ts_local, last_iso, mins
                    )
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
                # Always dismiss the offline banner if it existed
                hass.components.persistent_notification.async_dismiss(nid)

                # Show "back online" banner (auto-dismiss after ONLINE_BANNER_TTL_SEC)
                ts_local = dt_util.as_local(now).strftime("%H:%M")
                title, message = _fmt(hass, "online", name, ts_local, None, None)
                nid_online = f"{nid}:online"
                hass.components.persistent_notification.async_create(
                    message=message, title=title, notification_id=nid_online
                )
                _LOGGER.info("[%s] WServer back online.", dev_id)

                # Bind the notification id as a default argument to avoid late binding.
                async_call_later(
                    hass,
                    ONLINE_BANNER_TTL_SEC,
                    lambda _now, _nid=nid_online: hass.components.persistent_notification.async_dismiss(  # noqa: E501
                        _nid
                    ),
                )
                continue

            # No transition: update last state to current for completeness
            st["last"] = online

    # Attach the listener and keep its unsubscribe to avoid leaks on reload/unload.
    # English: Always register the unsubscribe callback with the entry.
    unsub = coordinator.async_add_listener(_on_coordinator_update)
    entry.async_on_unload(unsub)

    # Load platforms
    await hass.config_entries.async_forward_entry_setups(entry, _BASE_PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, ["select", "number"])

    # Options updates
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    _LOGGER.info(
        "DKN Cloud for HASS configured (scan_interval=%ss; token-only auth).",
        scan_interval,
    )
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    for platform in _BASE_PLATFORMS + ["select", "number"]:
        try:
            ok = await hass.config_entries.async_forward_entry_unload(entry, platform)
            unload_ok = unload_ok and ok
        except Exception:  # noqa: BLE001
            continue

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
