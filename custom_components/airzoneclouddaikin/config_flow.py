"""Config & Options flow for DKN Cloud for HASS.

This file keeps changes minimal and focused:
- Adds a proper OptionsFlow so Home Assistant shows the "Options" button.
- Keeps existing login validation logic.
- Stores scan_interval and enable_presets; OptionsFlow can override them later.

Notes:
- Comments are in English as our codebase convention states.
- We prefer not to log secrets or PII; any logging must be carefully redacted.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Option keys (kept as plain strings to avoid public API rename churn)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ENABLE_PRESETS = "enable_presets"

# --- User step schema (minimal change from previous versions) -----------------
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

    @staticmethod
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler for an existing entry."""
        return AirzoneOptionsFlow(entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step where we collect credentials and base options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate credentials by attempting a login against the API.
            try:
                from .airzone_api import (
                    AirzoneAPI,  # local import to avoid import cycles
                )
            except Exception as exc:  # defensive
                _LOGGER.exception("Failed to import AirzoneAPI: %s", exc)
                errors["base"] = "unknown"
            else:
                session = async_get_clientsession(self.hass)
                api = AirzoneAPI(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD], session
                )

                ok = False
                try:
                    ok = await api.login()
                except Exception as exc:  # network or unexpected
                    _LOGGER.warning("Login attempt failed (network/other): %s", exc)
                    errors["base"] = "cannot_connect"

                if ok:
                    # Minimal change policy: keep all inputs in data; options may override later.
                    return self.async_create_entry(
                        title="DKN Cloud for HASS",
                        data={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 10),
                            CONF_ENABLE_PRESETS: user_input.get(
                                CONF_ENABLE_PRESETS, False
                            ),
                        },
                    )

                if "base" not in errors:
                    # If we reached here, credentials were not accepted
                    errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Support YAML import (not typically used for this integration)."""
        return await self.async_step_user(user_input)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit scan_interval and feature flags like enable_presets."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display/process options form.

        We prefer options over data as the source of truth:
        - If an option exists, use it.
        - Otherwise, fall back to the stored data value.
        """
        if user_input is not None:
            # Persist only in options; do not mutate the entry's data.
            return self.async_create_entry(title="", data=user_input)

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
