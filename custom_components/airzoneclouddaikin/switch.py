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
    config = entry.data
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    session = async_get_clientsession(hass)
    api = AirzoneAPI(config.get("username"), config.get("password"), session)
    if not await api.login():
        _LOGGER.error("Login to Airzone API failed in switch setup.")
        return
    installations = await api.fetch_installations()
    switches = []
    for relation in installations:
        installation = relation.get("installation")
        if not installation:
            continue
        installation_id = installation.get("id")
        if not installation_id:
            continue
        devices = await api.fetch_devices(installation_id)
        for device in devices:
            switches.append(AirzonePowerSwitch(api, device, config, hass))
    async_add_entities(switches, True)


class AirzonePowerSwitch(SwitchEntity):
    """Representation of a power switch for an Airzone device."""

    def __init__(self, api: AirzoneAPI, device_data: dict, config: dict, hass):
        """Initialize the power switch."""
        self._api = api
        self._device_data = device_data
        self._config = config
        self._name = f"{device_data.get('name', 'Airzone Device')} Power"
        self._device_id = device_data.get("id")
        self._installation_id = device_data.get("installation_id")
        self._state = bool(int(device_data.get("power", 0)))
        self.hass = hass
        self._hass_loop = hass.loop

        # Assign unique_id safely using the device id.
        if self._device_id:
            self._attr_unique_id = f"{self._device_id}_power"
        else:
            _LOGGER.warning("No device ID found; generating unique_id from name.")
            self._attr_unique_id = hashlib.sha256(self._name.encode("utf-8")).hexdigest()

        self._attr_name = self._name
        # For compatibility with HA 2025.3, we explicitly set entity_id.
        self.entity_id = f"switch.{self._attr_unique_id}"

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
        """Turn on the device by sending P1=1."""
        await self.hass.async_add_executor_job(self.turn_on)
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn off the device by sending P1=0."""
        await self.hass.async_add_executor_job(self.turn_off)
        self._state = False
        self.async_write_ha_state()

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
        if self._hass_loop:
            asyncio.run_coroutine_threadsafe(self._api.send_event(payload), self._hass_loop)
        else:
            _LOGGER.error("No hass loop available; cannot send command.")
