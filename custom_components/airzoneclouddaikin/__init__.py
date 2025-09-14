"""DKN Cloud for HASS integration.

Hardened setup:
- Uses ConfigEntryNotReady on transient login failures so HA retries gracefully.
- Centralizes polling via DataUpdateCoordinator; entities must not perform I/O in properties.
- Wraps update exceptions into UpdateFailed for HA to surface errors properly.
- Avoids logging secrets or PII.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SEC = 10  # Keep aligned with config_flow minimum


async def _async_update_data(api: AirzoneAPI) -> dict[str, Any]:
    """Fetch and aggregate device data from the API.

    Implementation details:
    - Fetch installations, then devices for each installation.
    - Return a dict keyed by device_id to ease fast lookups.
    - Let exceptions bubble as UpdateFailed so HA can surface coordinator errors.
    """
    try:
        data: dict[str, Any] = {}
        installations = await api.fetch_installations()
        for relation in installations or []:
            installation = relation.get("installation")
            if not installation:
                continue
            installation_id = installation.get("id")
            if not installation_id:
                continue
            devices = await api.fetch_devices(installation_id)
            for device in devices or []:
                device_id = device.get("id")
                # Fallback for safety: derive a stable hash if API missed the id
                if not device_id:
                    device_id = f"{hash(str(device))}"
                    device["id"] = device_id
                data[device_id] = device
        return data
    except Exception as err:
        # Never expose tokens/emails in logs; message should stay generic.
        raise UpdateFailed(f"Failed to update Airzone data: {err}") from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    config = entry.data

    # Create the shared aiohttp session provided by HA
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    api = AirzoneAPI(config.get("username"), config.get("password"), session)

    # Login here is expected to succeed (already validated in config_flow),
    # but we still treat runtime failures as transient (NotReady) to let HA retry.
    if not await api.login():
        _LOGGER.warning("Airzone login failed during setup; deferring entry readiness.")
        raise ConfigEntryNotReady("Airzone Cloud login failed")

    scan_interval = config.get("scan_interval", DEFAULT_SCAN_INTERVAL_SEC)
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="airzone_data",
        update_method=lambda: _async_update_data(api),
        update_interval=timedelta(seconds=scan_interval),
    )
    # Attach API so platforms can access it when needed (no I/O in properties).
    coordinator.api = api  # type: ignore[attr-defined]

    # First refresh to populate data; convert to UpdateFailed for visibility
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator}

    # Forward platform setups
    await hass.config_entries.async_forward_entry_setups(entry, ["climate", "sensor", "switch"])
    _LOGGER.info("DKN Cloud for HASS configured successfully.")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "climate")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "switch")
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
