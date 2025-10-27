"""DKN Cloud for HASS integration setup (P1: token-only storage + reauth).

Key points:
- No password persisted in the config entry; only username (email) + user_token.
- On setup:
  * If 'user_token' exists, we use it (no login on startup).
  * If a legacy 'password' exists (and no token), attempt one-time login to migrate
    token and purge password.
    - If login() returns False (invalid auth / unexpected schema) → open reauth and raise NotReady.
    - If login() raises (network/429/5xx/timeout) → do NOT open reauth; raise NotReady to retry later.
- Coordinator: on HTTP 401 from reads, open a reauth flow once and surface UpdateFailed.

Fixes in P2:
- Explicitly re-raise asyncio.CancelledError in both the migration login block and the
  coordinator update path, so HA cancellations (reload/stop) are not turned into
  UpdateFailed/NotReady by mistake.
- Preserve any existing 'reauth_requested' flag set during the *first* refresh to avoid
  spawning multiple reauth flows when the initial update fails with 401.
"""

from __future__ import annotations

import asyncio  # Added for explicit CancelledError handling
import logging
from datetime import timedelta
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .airzone_api import AirzoneAPI
from .const import DOMAIN

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
