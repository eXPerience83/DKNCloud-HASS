# coding: utf-8
"""Config flow and options flow for DKN Cloud for HASS.

Key points:
- Validates credentials on initial setup (login).
- Exposes Options Flow to tweak:
  - scan_interval (>= 10 s)
  - enable_presets (to enable select/number platforms)
- Never logs or exposes PII (email, token, MAC, PIN, GPS).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .airzone_api import AirzoneAPI
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_INTERVAL = 10  # seconds, minimum by policy


def _schema_user(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the schema for the initial user step."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required("username", default=d.get("username", "")): str,
            vol.Required("password", default=d.get("password", "")): str,
            vol.Optional(
                "scan_interval", default=int(d.get("scan_interval", DEFAULT_SCAN_INTERVAL))
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
            vol.Optional("enable_presets", default=bool(d.get("enable_presets", False))): bool,
        }
    )


def _schema_options(entry: config_entries.ConfigEntry) -> vol.Schema:
    """Build the schema for the options UI."""
    scan_default = entry.options.get(
        "scan_interval",
        entry.data.get("scan_interval", DEFAULT_SCAN_INTERVAL),
    )
    presets_default = bool(entry.options.get("enable_presets", False))
    return vol.Schema(
        {
            vol.Optional(
                "scan_interval", default=int(scan_default)
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
            vol.Optional("enable_presets", default=presets_default): bool,
        }
    )


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DKN Cloud for HASS."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}

    async def _validate_login(self, hass: HomeAssistant, data: dict[str, Any]) -> bool:
        """Try to login to validate credentials."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        api = AirzoneAPI(data["username"], data["password"], session)
        ok = await api.login()
        return bool(ok)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Basic sanitization
            username = (user_input.get("username") or "").strip()
            password = user_input.get("password") or ""
            scan_interval = int(user_input.get("scan_interval") or DEFAULT_SCAN_INTERVAL)
            enable_presets = bool(user_input.get("enable_presets", False))

            if not username or not password:
                errors["base"] = "auth"
            else:
                try:
                    if not await self._validate_login(self.hass, user_input):
                        errors["base"] = "auth"
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Login validation error (masked): %s", type(exc).__name__)
                    errors["base"] = "cannot_connect"

            if not errors:
                # Unique entry by username (one account per entry)
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()

                data = {
                    "username": username,
                    "password": password,
                    "scan_interval": scan_interval,
                }
                # Store presets flag initially in options so it can be changed later
                options = {"enable_presets": enable_presets, "scan_interval": scan_interval}
                return self.async_create_entry(title=username, data=data, options=options)

        return self.async_show_form(step_id="user", data_schema=_schema_user(user_input), errors=errors)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Handle options for an existing entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                scan_interval = int(user_input.get("scan_interval"))
                enable_presets = bool(user_input.get("enable_presets"))
                # Accept and store
                return self.async_create_entry(
                    title="Options",
                    data={
                        "scan_interval": max(10, scan_interval),
                        "enable_presets": enable_presets,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Options validation error: %s", type(exc).__name__)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="init",
            data_schema=_schema_options(self.config_entry),
            errors=errors,
        )


@callback
def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> AirzoneOptionsFlow:
    """Provide the options flow handler (module-level hook required by HA)."""
    return AirzoneOptionsFlow(config_entry)
