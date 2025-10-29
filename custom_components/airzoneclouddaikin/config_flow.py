"""Config & Options flow for DKN Cloud for HASS (0.4.0a7, no-migrations).

Key decisions in this revision
------------------------------
- No migrations at all: the token is stored in entry.options when creating
  the entry; password is never persisted.
- Options flow MERGEs with existing options to preserve hidden keys (e.g. token).
- Reauth flow asks only for the password, mints a fresh token, and updates
  entry.options['user_token'].

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

from .const import (
    CONF_STALE_AFTER_MINUTES,
    DOMAIN,
    STALE_AFTER_MINUTES_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

# Stable option keys
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_PII = "expose_pii_identifiers"

# User-step schema (initial setup)
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=30)
        ),
        vol.Optional(CONF_EXPOSE_PII, default=False): cv.boolean,
        vol.Optional(CONF_STALE_AFTER_MINUTES, default=STALE_AFTER_MINUTES_DEFAULT): vol.All(
            vol.Coerce(int), vol.Range(min=6, max=30)
        ),
    }
)


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DKN Cloud for HASS."""

    # Keep a version, but we do NOT implement async_migrate_entry in __init__.py.
    # All new entries are created already with the correct storage contract.
    VERSION = 2

    def __init__(self) -> None:
        self._reauth_entry_id: str | None = None

    # ------------------------- Create entry -------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from .airzone_api import AirzoneAPI  # local import avoids HA import cycles
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to import AirzoneAPI: %s", type(exc).__name__)
                errors["base"] = "unknown"
            else:
                session = async_get_clientsession(self.hass)
                _pwd = user_input.get(CONF_PASSWORD)
                api = AirzoneAPI(user_input[CONF_USERNAME], session, password=_pwd)
                try:
                    ok = await api.login()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Login failed (network/other): %s", type(exc).__name__)
                    errors["base"] = "cannot_connect"
                else:
                    # wipe password from memory ASAP (hardening)
                    try:
                        if _pwd is not None:
                            _pwd = None
                        if CONF_PASSWORD in user_input:
                            user_input[CONF_PASSWORD] = None
                            del user_input[CONF_PASSWORD]
                    except Exception:  # noqa: BLE001
                        pass

                    if ok and api.token:
                        # Create entry with username in data, token+settings in options.
                        return self.async_create_entry(
                            title="DKN Cloud for HASS",
                            data={CONF_USERNAME: user_input[CONF_USERNAME]},
                            options={
                                "user_token": api.token,
                                CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, 10)),
                                CONF_EXPOSE_PII: bool(user_input.get(CONF_EXPOSE_PII, False)),
                                CONF_STALE_AFTER_MINUTES: int(
                                    user_input.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
                                ),
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

    # ------------------------- Reauth flow -------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauth for an existing entry (entry_id is provided in context)."""
        self._reauth_entry_id = (self.context or {}).get("entry_id")

        # Fallback: if no entry_id in context and only one entry exists, use it.
        if not self._reauth_entry_id:
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if len(entries) == 1:
                self._reauth_entry_id = entries[0].entry_id

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask only for password; update token in options; never persist password."""
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

        _pwd = user_input.get(CONF_PASSWORD)
        api = AirzoneAPI(username, session, password=_pwd)

        try:
            ok = await asyncio.wait_for(api.login(), timeout=60.0)
        except TimeoutError:
            _LOGGER.warning("Reauth login timed out after 60s.")
            errors["base"] = "timeout"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Reauth login failed (network/other): %s", type(exc).__name__)
            errors["base"] = "cannot_connect"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        # wipe password from memory
        try:
            if _pwd is not None:
                _pwd = None
            if user_input and CONF_PASSWORD in user_input:
                user_input[CONF_PASSWORD] = None
                del user_input[CONF_PASSWORD]
        except Exception:  # noqa: BLE001
            pass

        if not ok or not api.token:
            errors["base"] = "invalid_auth"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        # Merge token into options (preserve all existing keys)
        new_opts = dict(entry.options)
        new_opts["user_token"] = api.token
        self.hass.config_entries.async_update_entry(entry, options=new_opts)

        return self.async_abort(reason="reauth_successful")

    # ------------------------- Options flow -------------------------

    @staticmethod
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return AirzoneOptionsFlow(entry)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow that always preserves hidden keys like the user token."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        opts = self._entry.options

        current_scan = int(opts.get(CONF_SCAN_INTERVAL, 10))
        current_pii = bool(opts.get(CONF_EXPOSE_PII, False))
        current_stale_after = int(opts.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT))

        if user_input is not None:
            # Start from existing options to avoid dropping hidden keys.
            new_options: dict[str, Any] = dict(opts)
            new_options[CONF_SCAN_INTERVAL] = int(user_input.get(CONF_SCAN_INTERVAL, current_scan))
            new_options[CONF_EXPOSE_PII] = bool(user_input.get(CONF_EXPOSE_PII, current_pii))
            new_options[CONF_STALE_AFTER_MINUTES] = int(
                user_input.get(CONF_STALE_AFTER_MINUTES, current_stale_after)
            )
            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=current_scan): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=30)
                ),
                vol.Optional(CONF_EXPOSE_PII, default=current_pii): cv.boolean,
                vol.Optional(CONF_STALE_AFTER_MINUTES, default=current_stale_after): vol.All(
                    vol.Coerce(int), vol.Range(min=6, max=30)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
