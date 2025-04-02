"""DKN Cloud for HASS integration."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

DOMAIN = "airzoneclouddaikin"

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    # Forward setups for climate, sensor, and switch platforms.
    await hass.config_entries.async_forward_entry_setups(entry, ["climate", "sensor", "switch"])
    _LOGGER.info("DKN Cloud for HASS integration configured successfully.")
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "climate")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    unload_ok = unload_ok and await hass.config_entries.async_forward_entry_unload(entry, "switch")
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
