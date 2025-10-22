"""Config & Options flow for DKN Cloud for HASS (P1: token-only + reauth).

Changes:
- On initial setup, store username (email) + user_token (no password).
- Reauth flow asks for password, performs login, updates the token in the entry,
  and never persists the password.
- Logging is careful not to leak exceptions/URLs with secrets.
- YAML import is *not* supported; only UI flows are provided.
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
)

_LOGGER = logging.getLogger(__name__)

# Option keys (kept stable)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_PII = "expose_pii_identifiers"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,  # stores email
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=30)
        ),
        vol.Optional(CONF_EXPOSE_PII, default=False): cv.boolean,
    }
)


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DKN Cloud for HASS."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry_id: str | None = None

    @staticmethod
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return AirzoneOptionsFlow(entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect credentials, perform login to obtain a token, and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from .airzone_api import AirzoneAPI  # local import
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to import AirzoneAPI: %s", type(exc).__name__)
                errors["base"] = "unknown"
            else:
                session = async_get_clientsession(self.hass)
                api = AirzoneAPI(
                    user_input[CONF_USERNAME], session, password=user_input[CONF_PASSWORD]
                )
                try:
                    ok = await api.login()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Login failed (network/other): %s", type(exc).__name__)
                    errors["base"] = "cannot_connect"
                else:
                    if ok and api.token:
                        return self.async_create_entry(
                            title="DKN Cloud for HASS",
                            data={
                                CONF_USERNAME: user_input[CONF_USERNAME],
                                "user_token": api.token,
                                CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 10),
                                CONF_EXPOSE_PII: user_input.get(CONF_EXPOSE_PII, False),
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

    # ---------- Reauth ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauth for an existing entry (entry_id provided in context)."""
        self._reauth_entry_id = (self.context or {}).get("entry_id")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for password, perform login, update the token, never persist password."""
        errors: dict[str, str] = {}

        entry = None
        if self._reauth_entry_id:
            entry = self.hass.config_entries.async_get_entry(self._reauth_entry_id)
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        username = entry.data.get(CONF_USERNAME)
        schema = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})

        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        session = async_get_clientsession(self.hass)
        from .airzone_api import AirzoneAPI  # local import
        api = AirzoneAPI(username, session, password=user_input[CONF_PASSWORD])

        try:
            ok = await api.login()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Reauth login failed (network/other): %s", type(exc).__name__)
            errors["base"] = "cannot_connect"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        if not ok or not api.token:
            errors["base"] = "invalid_auth"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        new_data = dict(entry.data)
        new_data["user_token"] = api.token
        new_data.pop(CONF_PASSWORD, None)
        self.hass.config_entries.async_update_entry(entry, data=new_data)

        return self.async_abort(reason="reauth_successful")


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit scan_interval, privacy flags and connectivity threshold."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Display/process options form (options preferred over data)."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data = self._entry.data
        opts = self._entry.options

        current_scan = int(opts.get("scan_interval", data.get("scan_interval", 10)))
        current_pii = bool(opts.get("expose_pii_identifiers", data.get("expose_pii_identifiers", False)))
        current_stale_after = int(opts.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT))

        schema = vol.Schema(
            {
                vol.Optional("scan_interval", default=current_scan): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=30)
                ),
                vol.Optional("expose_pii_identifiers", default=current_pii): cv.boolean,
                vol.Optional(CONF_STALE_AFTER_MINUTES, default=current_stale_after): vol.All(
                    vol.Coerce(int), vol.Range(min=6, max=30)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
