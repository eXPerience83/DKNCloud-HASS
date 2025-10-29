"""Config & Options flow for DKN Cloud for HASS (0.4.0 – clean flow, no migrations).

What this fixes
---------------
- ERROR "Invalid handler specified": remove incorrect handler registration and nested classes.
  We expose a single ConfigFlow with `domain=DOMAIN` and a top-level OptionsFlow.
- First-setup stores token ONLY in `entry.options['user_token']`; password is never persisted.
- Options do MERGE with existing options to preserve hidden keys like the token.
- Reauth prompts only for password, refreshes the token in options and aborts with success.

Contract
--------
- `entry.data` keeps only the username (email).
- `entry.options` contains `user_token`, `scan_interval`, `expose_pii_identifiers`,
  and `stale_after_minutes`.
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

# UI guardrails
MIN_SCAN, MAX_SCAN = 10, 300
MIN_STALE, MAX_STALE = 6, 30


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): cv.string,
            vol.Required(CONF_PASSWORD): cv.string,
            vol.Optional(CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, 10)): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)
            ),
            vol.Optional(CONF_EXPOSE_PII, default=defaults.get(CONF_EXPOSE_PII, False)): cv.boolean,
            vol.Optional(
                CONF_STALE_AFTER_MINUTES,
                default=defaults.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_STALE, max=MAX_STALE)),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, 10)): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)
            ),
            vol.Optional(CONF_EXPOSE_PII, default=defaults.get(CONF_EXPOSE_PII, False)): cv.boolean,
            vol.Optional(
                CONF_STALE_AFTER_MINUTES,
                default=defaults.get(CONF_STALE_AFTER_MINUTES, STALE_AFTER_MINUTES_DEFAULT),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_STALE, max=MAX_STALE)),
        }
    )


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Primary config flow for the integration."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Expose the options flow handler (top-level class below)."""
        return AirzoneOptionsFlow(entry)

    # ------------------------- Initial setup -------------------------

    async def async_step_user
