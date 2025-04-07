"""Config flow for DKN Cloud for HASS."""
from __future__ import annotations
import logging
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers import config_validation as cv
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional("scan_interval", default=10): vol.All(vol.Coerce(int), vol.Range(min=10)),
    vol.Optional("force_hvac_mode_auto", default=False): bool,
})

class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DKN Cloud for HASS."""
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> dict:
        """Handle a flow initiated by the user."""
        errors = {}
        if user_input is not None:
            # Validate credentials by attempting a login
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            from .airzone_api import AirzoneAPI
            session = async_get_clientsession(self.hass)
            api = AirzoneAPI(user_input[CONF_USERNAME], user_input[CONF_PASSWORD], session)
            if not await api.login():
                errors["base"] = "invalid_auth"
                return self.async_show_form(
                    step_id="user",
                    data_schema=DATA_SCHEMA,
                    errors=errors,
                )
            return self.async_create_entry(title="DKN Cloud for HASS", data=user_input)
        return self.async_show_form
