"""Switch platform for DKN Cloud for HASS."""
import asyncio
import hashlib
import logging
from homeassistant.components.switch import SwitchEntity
from .const import DOMAIN
from .airzone_api import AirzoneAPI

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the switch platform from a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data found in hass.data for entry %s", entry.entry_id)
        return
    coordinator = data.get("coordinator")
    config = entry.data
    switches = []
    # Create a switch entity for each device in coordinator data.
    for device_id, device in coordinator.data.items():
        switches.append(AirzonePowerSwitch(coordinator, device, config, hass))
    async_add_entities(switches, True)

class AirzonePowerSwitch(SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, coordinator, device_data: dict, config: dict, hass):
        """Initialize the power switch."""
        self.coordinator = coordinator
        self._device_data = device_data
        self._config = config
        self._name = f"{device_data.get('name', 'Airzone Device')} Power"
        self._device_id = device_data.get("id")
        self._installation_id = device_data.get("installation_id")
        self._state = bool(int(device_data.get("power", 0)))
        self.hass = hass

        # Assign unique_id using the device id.
        if self._device_id:
            self._attr_unique_id = f"{self._device_id}_power"
        else:
            _LOGGER.warning("No device ID found; generating unique_id from name.")
            self._attr_unique_id = hashlib.sha256(self._name.encode("utf-8")).hexdigest()

        self._attr_name = self._name

    @property
    def unique_id(self):
        """Return a unique ID for this switch."""
        return self._attr_unique_id

    @property
    def name(self):
        """Return the name of the switch."""
        return self._attr_name

    @property
    def is_on(self):
        """Return True if the device is on."""
        return self._state

    @property
    def device_info(self):
        """Return device info to link this switch with other entities in HA."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_data.get("name"),
            "manufacturer": self._device_data.get("brand", "Daikin"),
            "model": self._device_data.get("firmware", "Unknown"),
        }

    async def async_turn_on(self, **kwargs):
        """Turn on the device by sending P1=1 and schedule an update."""
        await self.hass.async_add_executor_job(self.turn_on)
        self._state = True
        self.async_schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn off the device by sending P1=0 and schedule an update."""
        await self.hass.async_add_executor_job(self.turn_off)
        self._state = False
        self.async_schedule_update_ha_state()

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
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.info("Sending power command: %s", payload)
        if self.hass and self.hass.loop:
            asyncio.run_coroutine_threadsafe(self._api.send_event(payload), self.hass.loop)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")

    async def async_update(self):
        """Update the switch state by reading from the coordinator data."""
        await self.coordinator.async_request_refresh()
        device = self.coordinator.data.get(self._device_id)
        if device:
            self._device_data = device
            self._state = bool(int(device.get("power", 0)))
            _LOGGER.info("Power state updated: %s", self._state)
        self.async_schedule_update_ha_state()
