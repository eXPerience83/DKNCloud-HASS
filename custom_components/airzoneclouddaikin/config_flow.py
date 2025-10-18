"""Config & Options flow for DKN Cloud for HASS.

Focus in this revision:
- Keep minimal logic but rely on AirzoneAPI.login() behavior:
  * Returns False only for 401 (invalid credentials).
  * Raises on network issues (TimeoutError, ClientConnectorError) or 5xx.
- Map errors to HA-friendly messages: 'invalid_auth' vs 'cannot_connect'.

This update:
- Add 'stale_after_minutes' option (default 10, range 6..30) to control the passive
  connectivity threshold without introducing extra network pings.
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

from .const import (
    CONF_STALE_AFTER_MINUTES,
    DOMAIN,
    STALE_AFTER_MINUTES_DEFAULT,
    STALE_AFTER_MINUTES_MAX,
    STALE_AFTER_MINUTES_MIN,
)

_LOGGER = logging.getLogger(__name__)

# Option keys (kept stable)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_PII = "expose_pii_identifiers"  # single opt-in switch for PII fields

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=10)
        ),
        vol.Optional(CONF_EXPOSE_PII, default=False): cv.boolean,
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
        """Collect credentials and validate against the API."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from .airzone_api import AirzoneAPI  # local import to avoid cycles
            except Exception as exc:
                _LOGGER.exception("Failed to import AirzoneAPI: %s", exc)
                errors["base"] = "unknown"
            else:
                session = async_get_clientsession(self.hass)
                api = AirzoneAPI(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD], session
                )

                try:
                    ok = await api.login()
                except Exception as exc:
                    # Network/5xx → cannot_connect
                    _LOGGER.warning("Login failed (network/other): %s", exc)
                    errors["base"] = "cannot_connect"
                else:
                    if ok:
                        return self.async_create_entry(
                            title="DKN Cloud for HASS",
                            data={
                                CONF_USERNAME: user_input[CONF_USERNAME],
                                CONF_PASSWORD: user_input[CONF_PASSWORD],
                                CONF_SCAN_INTERVAL: user_input.get(
                                    CONF_SCAN_INTERVAL, 10
                                ),
                                CONF_EXPOSE_PII: user_input.get(CONF_EXPOSE_PII, False),
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_import(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Support YAML import (not typical for this integration)."""
        return await self.async_step_user(user_input)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit scan_interval, privacy flags and connectivity threshold."""

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
            return self.async_create_entry(title="", data=user_input)

        data = self._entry.data
        opts = self._entry.options

        current_scan = int(opts.get("scan_interval", data.get("scan_interval", 10)))
        current_pii = bool(
            opts.get(
                "expose_pii_identifiers", data.get("expose_pii_identifiers", False)
            )
        )
        current_stale_after = int(
            opts.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
        )

        schema = vol.Schema(
            {
                vol.Optional("scan_interval", default=current_scan): vol.All(
                    vol.Coerce(int), vol.Range(min=10)
                ),
                vol.Optional("expose_pii_identifiers", default=current_pii): cv.boolean,
                vol.Optional(
                    CONF_STALE_AFTER_MINUTES, default=current_stale_after
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=STALE_AFTER_MINUTES_MIN, max=STALE_AFTER_MINUTES_MAX),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
