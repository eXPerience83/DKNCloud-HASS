"""Config & Options flow for DKN Cloud for HASS (0.4.0a5).

What this revision fixes
------------------------
1) Options flow overwrote entry.options and dropped hidden keys (notably
   'user_token'), causing "Token required; reauth triggered" on reload.
   -> We now MERGE the new options with the existing ones and preserve
      any unknown/hidden keys. As a safety net, if a legacy token still
      lives in entry.data, we copy it into options.

2) Reauth UI path is intact: `async_step_reauth` + `async_step_reauth_confirm`
   ask only for the password, mint a fresh token via API, and update
   entry.options['user_token'] (never persist password).

Design contracts
----------------
- Token is stored in entry.options['user_token'] (encrypted at rest by HA).
- Password is never persisted (only used transiently during login/reauth).
- ConfigFlow.VERSION == 2 to match __init__.py migration logic.

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

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,  # email
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=10): vol.All(
            vol.Coerce(int), vol.Range(min=10, max=30)
        ),
        vol.Optional(CONF_EXPOSE_PII, default=False): cv.boolean,
    }
)


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for DKN Cloud for HASS."""

    # Keep version aligned with async_migrate_entry in __init__.py
    VERSION = 2

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
                # Keep a local reference to password and wipe it right after use.
                _pwd = user_input.get(CONF_PASSWORD)
                api = AirzoneAPI(user_input[CONF_USERNAME], session, password=_pwd)
                try:
                    ok = await api.login()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Login failed (network/other): %s", type(exc).__name__
                    )
                    errors["base"] = "cannot_connect"
                else:
                    # Wipe password from memory ASAP (hardening)
                    try:
                        if _pwd is not None:
                            _pwd = None
                        if CONF_PASSWORD in user_input:
                            user_input[CONF_PASSWORD] = None
                            del user_input[CONF_PASSWORD]
                    except Exception:  # noqa: BLE001
                        pass

                    if ok and api.token:
                        # Create entry with username. Token and basic options are placed
                        # in data for 0.4.x and migrated to options during setup.
                        return self.async_create_entry(
                            title="DKN Cloud for HASS",
                            data={
                                CONF_USERNAME: user_input[CONF_USERNAME],
                                # temporary compatibility fields (migrated to options)
                                "user_token": api.token,
                                CONF_SCAN_INTERVAL: user_input.get(
                                    CONF_SCAN_INTERVAL, 10
                                ),
                                CONF_EXPOSE_PII: user_input.get(CONF_EXPOSE_PII, False),
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    # ---------- Reauth ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauth for an existing entry (entry_id provided in context)."""
        self._reauth_entry_id = (self.context or {}).get("entry_id")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for password, perform login, update the token in entry.options, never persist password."""
        errors: dict[str, str] = {}

        entry = None
        if self._reauth_entry_id:
            entry = self.hass.config_entries.async_get_entry(self._reauth_entry_id)
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        username = entry.data.get(CONF_USERNAME)
        schema = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors=errors
            )

        session = async_get_clientsession(self.hass)
        from .airzone_api import AirzoneAPI  # local import

        # Keep a local reference and wipe it after use.
        _pwd = user_input.get(CONF_PASSWORD)
        api = AirzoneAPI(username, session, password=_pwd)

        try:
            # UI-level 60s guard; built-in TimeoutError also covers asyncio timeouts.
            ok = await asyncio.wait_for(api.login(), timeout=60.0)
        except TimeoutError:
            _LOGGER.warning("Reauth login timed out after 60s.")
            errors["base"] = "timeout"
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors=errors
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Reauth login failed (network/other): %s", type(exc).__name__
            )
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors=errors
            )

        # Wipe password from memory ASAP (hardening)
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
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors=errors
            )

        # Merge new token into options; never write password.
        new_opts = dict(entry.options)
        new_opts["user_token"] = api.token
        self.hass.config_entries.async_update_entry(entry, options=new_opts)

        return self.async_abort(reason="reauth_successful")


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow to edit scan_interval, privacy flags and connectivity threshold.

    Critical behavior: **always preserve** hidden options (notably user_token).
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display/process options form (options preferred over data)."""
        data = self._entry.data
        opts = self._entry.options

        current_scan = int(
            opts.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, 10))
        )
        current_pii = bool(opts.get(CONF_EXPOSE_PII, data.get(CONF_EXPOSE_PII, False)))
        current_stale_after = int(
            opts.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT)
        )

        if user_input is not None:
            # Start from existing options to avoid dropping hidden keys.
            new_options: dict[str, Any] = dict(opts)

            # Overwrite with form values
            new_options[CONF_SCAN_INTERVAL] = int(
                user_input.get(CONF_SCAN_INTERVAL, current_scan)
            )
            new_options[CONF_EXPOSE_PII] = bool(
                user_input.get(CONF_EXPOSE_PII, current_pii)
            )
            new_options[CONF_STALE_AFTER_MINUTES] = int(
                user_input.get(CONF_STALE_AFTER_MINUTES, current_stale_after)
            )

            # Safety net: if token still lives in data (during 0.4.x migration), copy it.
            if "user_token" not in new_options and "user_token" in data:
                new_options["user_token"] = data["user_token"]

            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=current_scan): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=30)
                ),
                vol.Optional(CONF_EXPOSE_PII, default=current_pii): cv.boolean,
                vol.Optional(
                    CONF_STALE_AFTER_MINUTES, default=current_stale_after
                ): vol.All(vol.Coerce(int), vol.Range(min=6, max=30)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
