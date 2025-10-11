"""Diagnostics for DKN Cloud for HASS (Airzone Cloud).

Provides a sanitized snapshot of the config entry, coordinator state and
device data. Uses Home Assistant's async_redact_data to remove secrets/PII.

Scope:
- Never include raw passwords, tokens, emails, MAC/PIN, GPS coordinates.
- Summarize entry.data keys instead of dumping their values.
- Include coordinator status and the full devices snapshot (redacted).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Fields to redact anywhere in the structure (nested keys included).
TO_REDACT = {
    # Auth & identity
    "email",
    "user_email",
    "password",
    "authentication_token",
    "token",
    "user_token",
    # Device identifiers / PII
    "mac",
    "pin",
    "installation_id",
    "spot_name",
    "complete_name",
    "time_zone",
    # Location data
    "latitude",
    "longitude",
    "lat",
    "lon",
    "location",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (sanitized)."""
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = domain_data.get("coordinator")

    # Build a conservative snapshot; avoid dumping secrets from entry.data.
    entry_summary = {
        "title": entry.title,
        "data_keys": sorted(list(entry.data.keys())),  # keys only, no values
        "options": entry.options,
        "version": getattr(entry, "version", None),
    }

    coord_summary = None
    if coordinator is not None:
        interval = getattr(coordinator, "update_interval", None)
        if interval is not None:
            try:
                interval = interval.total_seconds()
            except Exception:
                interval = str(interval)
        coord_summary = {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "update_interval_seconds": interval,
            "devices_count": len(getattr(coordinator, "data", {}) or {}),
            "devices": getattr(coordinator, "data", {}),
        }

    raw = {
        "entry": entry_summary,
        "coordinator": coord_summary,
    }

    # Redact secrets and PII recursively.
    return async_redact_data(raw, TO_REDACT)
