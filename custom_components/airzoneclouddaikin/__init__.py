"""DKN Cloud for HASS integration."""
import logging
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .airzone_api import AirzoneAPI

_LOGGER = logging.getLogger(__name__)

async def _async_update_data(api: AirzoneAPI) -> dict:
    """Fetch and aggregate device data from the API.

    This function fetches installations and then, for each installation,
    fetches all devices. The data is aggregated into a dictionary keyed by device id.
    """
    data = {}
    installations = await api.fetch_installations()
    for relation in installations:
        installation = relation.get("installation")
        if not installation:
            continue
        installation_id = installation.get("id")
        if not installation_id:
            continue
        devices = await api.fetch_devices(installation_id)
        for device in devices:
            device_id = device.get("id")
            if not device_id:
                # Fallback: use a hash of the device data if no id is provided
                device_id = f"{hash(str(device))}"
                device["id"] = device_id
            data[device_id] = device
    return data

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DKN Cloud for HASS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    config = entry.data

    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    session = async_get_clientsession(hass)
    api = AirzoneAPI(config.get("username"), config.get("password"), session)
    if not await api.login():
        _LOGGER.error("Login to Airzone API failed.")
        return False

    # Use scan_interval from config or default to 10 seconds
    scan_interval = config.get("scan_interval", 10)
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="airzone_data",
        update_method=lambda: _async_update_data(api),
        update_interval=timedelta(seconds=scan_interval),
    )
    # Attach the API instance to the coordinator so that entities can use it.
    coordinator.api = api

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        _LOGGER.error("Failed to fetch initial data: %s", err)
        raise UpdateFailed(err) from err

    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator}
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
