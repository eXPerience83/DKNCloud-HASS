"""DKN Cloud for HASS integration setup.

Highlights:
- Centralized polling via DataUpdateCoordinator.
- Robust parsing of installation relations (handles 'installation.id' or 'installation_id').
- Options-aware: scan_interval is configurable post-setup.
- Presets (select/number) are now ALWAYS loaded (opt-in removed).
- Never logs or exposes PII (email, token, MAC, PIN, GPS).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL_SEC = 10  # keep aligned with config_flow minimum
_BASE_PLATFORMS: list[str] = ["climate", "sensor", "switch"]


async def _async_update_data(api: AirzoneAPI) -> dict[str, Any]:
    """Fetch and aggregate device data from the API.

    Implementation details:
    - Fetch installations, then devices for each installation.
    - Accept both shapes for installation relations:
        a) {"installation": {"id": ...}, ...}
        b) {"installation_id": ...}
    - Return a dict keyed by device_id to ease fast lookups.
    - Let exceptions bubble as UpdateFailed so HA can surface coordinator errors.
    """
    try:
        data: dict[str, Any] = {}
        relations = await api.fetch_installations()

        for rel in relations or []:
            inst_id: Any | None = None
            # Shape (a)
            inst = rel.get("installation")
            if isinstance(inst, dict):
                inst_id = inst.get("id") or inst.get("installation_id")
            # Shape (b)
            if inst_id is None:
                inst_id = rel.get("installation_id") or rel.get("id")

            if not inst_id:
                continue

            devices = await api.fetch_devices(inst_id)
            for dev in devices or []:
                dev_id = dev.get("id")
                if not dev_id:
                    # Safety: some backends omit id; use MAC if available, else keep device unindexed
                    mac = str(dev.get("mac") or "").strip().lower()
                    if mac:
                        dev_id = mac
                        dev["id"] = dev_id
                    else:
                        # Skip devices without any stable identifier
                        continue
                data[str(dev_id)] = dev

        return data

    except Exception as err:  # noqa: BLE001
        # Keep message generic; never include secrets or full URLs.
        raise UpdateFailed(
            f"Failed to update Airzone data: {type(err).__name__}"
        ) from err


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    cfg = entry.data

    # aiohttp session from HA
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    api = AirzoneAPI(cfg.get("username"), cfg.get("password"), session)

    # Login should succeed (validated in config_flow), but treat runtime failures as NotReady
    if not await api.login():
        _LOGGER.warning("Airzone login failed during setup; entry not ready yet.")
        raise ConfigEntryNotReady("Airzone Cloud login failed")

    # Respect options first, fallback to data (minimal-change policy)
    scan_interval = int(
        entry.options.get(
            "scan_interval", cfg.get("scan_interval", DEFAULT_SCAN_INTERVAL_SEC)
        )
    )

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="airzone_data",
        update_method=lambda: _async_update_data(api),
        update_interval=timedelta(seconds=max(10, scan_interval)),
    )
    # Attach API for platforms (no I/O in properties)
    coordinator.api = api  # type: ignore[attr-defined]

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # Load base platforms
    await hass.config_entries.async_forward_entry_setups(entry, _BASE_PLATFORMS)
    # Presets ALWAYS loaded (opt-in removed)
    await hass.config_entries.async_forward_entry_setups(entry, ["select", "number"])

    # Reload entry on options updates
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    _LOGGER.info(
        "DKN Cloud for HASS configured (scan_interval=%ss; presets loaded).",
        scan_interval,
    )
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    # Unload all potentially-loaded platforms
    for platform in _BASE_PLATFORMS + ["select", "number"]:
        try:
            ok = await hass.config_entries.async_forward_entry_unload(entry, platform)
            unload_ok = unload_ok and ok
        except Exception:  # noqa: BLE001
            # Keep going; platform may not have been loaded
            continue

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
