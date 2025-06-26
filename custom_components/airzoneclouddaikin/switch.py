"""Switch platform for DKN Cloud for HASS."""

import logging
from homeassistant.components.switch import SwitchEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up switch platform."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        _LOGGER.error("No data for entry %s", entry.entry_id)
        return
    coordinator = data["coordinator"]
    api = data["api"]
    entities = []
    for device_id, device in coordinator.data.items():
        entities.append(AirzonePowerSwitch(coordinator, api, device, device_id, hass))
    async_add_entities(entities, True)


class AirzonePowerSwitch(SwitchEntity):
    """Power switch for Airzone device."""

    def __init__(self, coordinator, api, device, device_id, hass):
        """Initialize."""
        self.coordinator = coordinator
        self._api = api
        self._device = device
        self._device_id = device_id
        self.hass = hass

        self._attr_name = f"{device.get('name')} Power"
        self._attr_unique_id = f"{device_id}_power"

    @property
    def is_on(self):
        """Return True if on."""
        return self._device.get("power") == "1"

    @property
    def device_info(self):
        """Link to device registry."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("name"),
            "manufacturer": "Daikin",
            "model": f"{self._device.get('brand')} (PIN: {self._device.get('pin')})",
            "sw_version": self._device.get("firmware"),
            "connections": {("mac", self._device.get("mac"))},
        }

    async def async_turn_on(self, **kwargs):
        """Turn on."""
        self._send("P1", 1)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn off."""
        self._send("P1", 0)
        await self.coordinator.async_request_refresh()

    def _send(self, option, value):
        """Helper to call API."""
        payload = {
            "event": {
                "cgi": "modmaquina",
                "device_id": self._device_id,
                "option": option,
                "value": value,
            }
        }
        _LOGGER.debug("Power command %s=%s", option, value)
        self.hass.async_create_task(self._api.send_event(payload))

    async def async_update(self):
        """Refresh state."""
        await self.coordinator.async_request_refresh()
        self._device = self.coordinator.data.get(self._device_id, self._device)
