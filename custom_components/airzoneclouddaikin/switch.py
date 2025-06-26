"""Switch platform for DKN Cloud for HASS."""
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
    coordinator = data["coordinator"]
    api = data["api"]
    entities = [
        AirzonePowerSwitch(coordinator, api, device, hass)
        for device in coordinator.data.values()
    ]
    async_add_entities(entities, True)


class AirzonePowerSwitch(SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, coordinator, api, device_data: dict, hass):
        """
        :param coordinator: DataUpdateCoordinator
        :param api: AirzoneAPI instance
        :param device_data: raw device dict
        :param hass: Home Assistant instance
        """
        self.coordinator = coordinator
        self._api = api
        self._device_data = device_data
        self.hass = hass

        name = f"{device_data.get('name')} Power"
        unique_id = f"{device_data['id']}_power"
        self.async_set_unique_id(unique_id)
        self._attr_name = name

    @property
    def is_on(self):
        """Return True if the device is on."""
        return self._device_data.get("power") == "1"

    @property
    def device_info(self):
        """Link to the HA device registry."""
        return {
            "identifiers": {(DOMAIN, self.unique_id.split("_")[0])},
            "name": self._device_data.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device_data.get('brand')} (PIN: {self._device_data.get('pin')})",
            "sw_version": self._device_data.get("firmware"),
            "connections": {("mac", self._device_data.get("mac"))},
        }

    async def async_turn_on(self, **kwargs):
        """Turn on the device asynchronously."""
        self._send_command("P1", 1)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn off the device asynchronously."""
        self._send_command("P1", 0)
        await self.coordinator.async_request_refresh()

    def _send_command(self, option, value):
        """Send a power command to the Airzone API."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self.unique_id.split("_")[0],
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Sending power command: %s", payload)
        # schedule via HA event loop
        self.hass.async_create_task(self._api.send_event(payload))

    async def async_update(self):
        """Refresh switch state from coordinator."""
        await self.coordinator.async_request_refresh()
        self._device_data = self.coordinator.data.get(self.unique_id.split("_")[0], self._device_data)
