"""Config flow for DKN Cloud for HASS."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_PRESETS = "enable_presets"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=10)
        ),
        vol.Optional(CONF_ENABLE_PRESETS, default=False): cv.boolean,
    }
)


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DKN Cloud for HASS."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle a flow initiated by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # Validate credentials by attempting a login
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            from .airzone_api import AirzoneAPI

            session = async_get_clientsession(self.hass)
            api = AirzoneAPI(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD], session
            )
            ok = await api.login()
            if not ok:
                errors["base"] = "invalid_auth"
            else:
                # Create entry with provided data; options can override later
                return self.async_create_entry(
                    title="DKN Cloud for HASS",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 10),
                        CONF_ENABLE_PRESETS: user_input.get(CONF_ENABLE_PRESETS, False),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_import(self, user_input: dict | None = None) -> FlowResult:
        """Support YAML import if ever needed (not standard for this integration)."""
        return await self.async_step_user(user_input)

    # ---- Proper Options Flow registration (so the "Options" button appears) ----
    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "AirzoneOptionsFlow":
        return AirzoneOptionsFlow(config_entry)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit scan_interval and feature flags like enable_presets."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Handle options."""
        if user_input is not None:
            # Persist options only in options (do not mutate data)
            return self.async_create_entry(title="", data=user_input)

        # Current values: prefer options over data
        data = self._entry.data
        opts = self._entry.options
        current_scan = int(
            opts.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, 10))
        )
        current_presets = bool(
            opts.get(CONF_ENABLE_PRESETS, data.get(CONF_ENABLE_PRESETS, False))
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=current_scan): vol.All(
                    vol.Coerce(int), vol.Range(min=10)
                ),
                vol.Optional(CONF_ENABLE_PRESETS, default=current_presets): cv.boolean,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
