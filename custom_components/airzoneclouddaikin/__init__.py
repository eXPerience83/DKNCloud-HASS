"""DKN Cloud for HASS integration.

Hardened setup:
- Uses ConfigEntryNotReady on transient login failures so HA retries gracefully.
- Centralizes polling via DataUpdateCoordinator; entities must not perform I/O in properties.
- Wraps update exceptions into UpdateFailed for HA to surface errors properly.
- Avoids logging secrets or PII.
"""

from __future__ import annotations

import hashlib
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
OPT_ENABLE_PRESETS = "enable_presets"
OPT_SCAN_INTERVAL = "scan_interval"


def _derive_stable_device_id(device: dict[str, Any]) -> str:
    """Derive a stable unique_id when backend omits 'id'.

    Preference order:
    1) Use MAC (lowercased) if present.
    2) Use a stable SHA-256 digest of name|brand|firmware as virtual id.

    NOTE: Do NOT use Python's built-in hash(), which is salted per process and
    will produce different values across restarts (breaking unique_id).
    """
    mac = str(device.get("mac") or "").strip().lower()
    if mac:
        return mac
    base = "|".join(str(device.get(k, "") or "") for k in ("name", "brand", "firmware"))
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
    return f"virt-{digest}"


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
                # Fallback for safety: derive a STABLE id if API missed the id.
                if not device_id:
                    device_id = _derive_stable_device_id(device)
                    device["id"] = device_id
                data[device_id] = device
        return data
    except Exception as err:
        # Never expose tokens/emails in logs; message should stay generic.
        raise UpdateFailed(f"Failed to update Airzone data: {err}") from err


def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read an option with fallback to config entry data."""
    return entry.options.get(key, entry.data.get(key, default))


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called when options are updated: reload the config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


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

    # Apply scan_interval from options with fallback to data
    scan_interval = int(_opt(entry, OPT_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SEC))

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

    # Determine whether to enable presets (select/number) from options+data
    enable_presets = bool(_opt(entry, OPT_ENABLE_PRESETS, False))

    platforms = ["climate", "sensor", "switch"]
    if enable_presets:
        platforms += ["select", "number"]

    # Forward platform setups
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    _LOGGER.info(
        "DKN Cloud for HASS configured successfully (scan_interval=%ss, presets=%s).",
        scan_interval,
        enable_presets,
    )

    # Listen for options updates; on change, reload the entry
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry cleanly."""
    enable_presets = bool(_opt(entry, OPT_ENABLE_PRESETS, False))
    platforms = ["climate", "sensor", "switch"]
    if enable_presets:
        platforms += ["select", "number"]

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
