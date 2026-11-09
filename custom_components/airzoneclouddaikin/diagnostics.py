"""Allowlisted integration health diagnostics without backend objects."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (sanitized)."""
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = domain_data.get("coordinator")

    result = {"entry": {"version": getattr(entry, "version", None)}}
    if coordinator is not None:
        result["coordinator"] = {
            "devices_count": len(coordinator.data or {}),
            "last_update_success": coordinator.last_update_success,
        }
    if manager := domain_data.get("sync_manager"):
        result["synchronization"] = manager.diagnostics()
        result["synchronization"]["overlays_pending"] = sum(
            len(fields) for fields in domain_data.get("optimistic", {}).values()
        )
    return result
