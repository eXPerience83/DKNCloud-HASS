"""Switch platform for DKN Cloud for HASS."""
import asyncio
import hashlib
import logging
from homeassistant.components.switch import SwitchEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the switch platform from a config entry using the DataUpdateCoordinator."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data.get("coordinator")
    api = data.get("api")
    switches = []
    # Create a switch entity for each device in the coordinator data.
    for device_id, device in coordinator.data.items():
        switches.append(AirzonePowerSwitch(coordinator, api, device))
    async_add_entities(switches, True)

class AirzonePowerSwitch(SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, coordinator, api, device_data: dict):
        """
        Initialize the power switch.
        
        :param coordinator: The DataUpdateCoordinator instance.
        :param api: The AirzoneAPI instance.
        :param device_data: Dictionary with device information.
        """
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data
        # Construct the entity name.
        name = f"{device_data.get('name', 'Airzone Device')} Power"
        self._attr_name = name
        # Set the unique_id using the device 'id'; fallback to a hash if missing.
        device_id = device_data.get("id")
        if device_id and device_id.strip():
            self._attr_unique_id = f"{device_id}_power"
        else:
            self._attr_unique_id = hashlib.sha256(name.encode("utf-8")).hexdigest()

    @property
    def is_on(self):
        """Return True if the device is on."""
        return self._device_data.get("power", "0") == "1"

    @property
    def device_info(self):
        """Return device info to link this switch with other entities in HA."""
        return {
            "identifiers": {(DOMAIN, self._device_data.get("id"))},
            "name": self._device_data.get("name"),
            "manufacturer": self._device_data.get("brand", "Daikin"),
            "model": self._device_data.get("firmware", "Unknown"),
        }

    async def async_turn_on(self, **kwargs):
        """Turn on the device by sending P1=1 and update state."""
        await self.hass.async_add_executor_job(self.turn_on)
        self._device_data["power"] = "1"
        # Let the coordinator handle state refresh

    async def async_turn_off(self, **kwargs):
        """Turn off the device by sending P1=0 and update state."""
        await self.hass.async_add_executor_job(self.turn_off)
        self._device_data["power"] = "0"
        # Let the coordinator handle state refresh

    def turn_on(self):
        """Turn on the device."""
        self._send_command("P1", 1)

    def turn_off(self):
        """Turn off the device."""
        self._send_command("P1", 0)

    def _send_command(self, option, value):
        """Send a command to the device using the events endpoint."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_data.get("id"),
                "option": option,
                "value": value,
            }
        }
        _LOGGER.info("Sending power command: %s", payload)
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(
                self._api.send_event(payload), self.hass.loop
            )
        else:
            _LOGGER.error("No hass loop available; cannot send command.")

    async def async_update(self):
        """Update the switch state from the coordinator data."""
        await self.coordinator.async_request_refresh()
        # Retrieve the updated device data using the device's unique id
        device = self.coordinator.data.get(self._device_data.get("id"))
        if device:
            self._device_data = device
        # The state will be updated by the coordinator; no need to call async_write_ha_state() here.
