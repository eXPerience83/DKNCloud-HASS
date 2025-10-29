"""Config & Options flow for DKN Cloud for HASS (0.4.0a8).

What this revision fixes
------------------------
- FIX: 500 Internal Server Error when launching the flow in Spanish locale,
  caused by malformed translations/es.json (now fixed in the JSON file).
- HARDEN: Options flow now merges with existing options to preserve hidden keys
  like 'user_token' and won't ever erase them.
- REAUTH: Reauth asks only for password, refreshes 'user_token' in options,
  and never persists the password.

Design contracts
----------------
- Token is stored in entry.options['user_token'] (never in entry.data).
- Password is never persisted.
- No migration paths remain in the flow; fresh add + reauth only.

"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .airzone_api import AirzoneAPI
from .const import (
    CONF_STALE_AFTER_MINUTES,
    DOMAIN,
    STALE_AFTER_MINUTES_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

# UI schema constraints
MIN_SCAN = 10
MAX_SCAN = 300
MIN_STALE = 5
MAX_STALE = 180


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): cv.string,
            vol.Required(CONF_PASSWORD): cv.string,
            vol.Optional("scan_interval", default=defaults.get("scan_interval", 10)): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)
            ),
            vol.Optional(
                "expose_pii_identifiers",
                default=defaults.get("expose_pii_identifiers", False),
            ): cv.boolean,
            vol.Optional(
                CONF_STALE_AFTER_MINUTES,
                default=defaults.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_STALE, max=MAX_STALE)),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("scan_interval", default=defaults.get("scan_interval", 10)): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)
            ),
            vol.Optional(
                "expose_pii_identifiers",
                default=defaults.get("expose_pii_identifiers", False),
            ): cv.boolean,
            vol.Optional(
                CONF_STALE_AFTER_MINUTES,
                default=defaults.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_STALE, max=MAX_STALE)),
        }
    )


async def _api_login(hass, email: str, password: str) -> str:
    """Perform login against Airzone, return token or raise."""
    session = async_get_clientsession(hass)
    api = AirzoneAPI(email, session, password=password, token=None)
    try:
        token = await asyncio.wait_for(api.login(), timeout=20)
    finally:
        # Do not keep the password reference around longer than needed.
        api.password = None
    if not token:
        raise ValueError("Empty token")
    return token


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Initial step: email+password + first options."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_user_schema({}))

        email = str(user_input[CONF_USERNAME]).strip()
        password = str(user_input[CONF_PASSWORD])

        # First, try to obtain token
        try:
            token = await _api_login(self.hass, email, password)
        except asyncio.TimeoutError:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "timeout"},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Login failed: %s", err)
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "invalid_auth"},
            )

        # Prepare options (persist token here; never store password)
        options = {
            "user_token": token,
            "scan_interval": int(user_input.get("scan_interval", 10)),
            "expose_pii_identifiers": bool(user_input.get("expose_pii_identifiers", False)),
            CONF_STALE_AFTER_MINUTES: int(
                user_input.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
            ),
        }

        # Create entry with minimal data and all tunables in options
        return self.async_create_entry(title=email, data={CONF_USERNAME: email}, options=options)

    # ----------------- REAUTH -----------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle start of reauth; show confirm step."""
        self._reauth_entry = await self.async_set_unique_id(entry_data.get(CONF_USERNAME))
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Prompt only for the password, then refresh the token."""
        entry = None
        # Find the entry for this domain/username
        for e in self._async_current_entries():
            if e.data.get(CONF_USERNAME) == self.context.get("unique_id") or e.data.get(
                CONF_USERNAME
            ) == self.hass.data.get(DOMAIN, {}).get("reauth_username"):
                entry = e
                break
        if entry is None and self._async_current_entries():
            entry = self._async_current_entries()[0]

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): cv.string}),
            )

        password = str(user_input[CONF_PASSWORD])
        email = entry.data.get(CONF_USERNAME)

        try:
            token = await _api_login(self.hass, email, password)
        except asyncio.TimeoutError:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): cv.string}),
                errors={"base": "timeout"},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reauth failed: %s", err)
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): cv.string}),
                errors={"base": "invalid_auth"},
            )

        # Merge options, preserving unknown keys.
        opts = dict(entry.options)
        opts["user_token"] = token
        self.hass.config_entries.async_update_entry(entry, options=opts)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    # ----------------- OPTIONS -----------------

    @staticmethod
    @config_entries.HANDLERS.register(DOMAIN)  # type: ignore[attr-defined]
    class OptionsFlowHandler(config_entries.OptionsFlow):
        """Handle the options flow."""

        def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
            self._entry = config_entry

        async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
            defaults = dict(self._entry.options)
            if user_input is None:
                return self.async_show_form(step_id="init", data_schema=_options_schema(defaults))

            # Merge: keep any unknown keys (e.g., 'user_token')
            next_opts = dict(self._entry.options)
            next_opts.update(
                {
                    "scan_interval": int(user_input.get("scan_interval", defaults.get("scan_interval", 10))),
                    "expose_pii_identifiers": bool(
                        user_input.get("expose_pii_identifiers", defaults.get("expose_pii_identifiers", False))
                    ),
                    CONF_STALE_AFTER_MINUTES: int(
                        user_input.get(CONF_STALE_AFTER_MINUTES, defaults.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT))
                    ),
                }
            )
            self.hass.config_entries.async_update_entry(self._entry, options=next_opts)
            return self.async_create_entry(title="", data={})
