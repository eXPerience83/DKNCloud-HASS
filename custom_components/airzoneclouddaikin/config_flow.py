"""Config & Options flow for DKN Cloud for HASS.

What this delivers
------------------
- Keeps the power-switch proxy transparent to the options storage; climate services
  still rely on the token saved in `entry.options`.
- Maintains the 10–30 s `scan_interval` guardrails and hidden option preservation.
- Reauth continues to refresh the token in options without persisting passwords.

Contract
--------
- `entry.data` keeps only the username (email).
- `entry.options` contains `user_token`, `scan_interval`, `expose_pii_identifiers`,
  and `enable_heat_cool_mode` (experimental opt-in).
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
    CONF_ENABLE_HEAT_COOL,
    CONF_SLEEP_TIMEOUT_ENABLED,
    DOMAIN,
)
from .helpers import device_supports_heat_cool

_LOGGER = logging.getLogger(__name__)

# Stable option keys
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_PII = "expose_pii_identifiers"

# UI guardrails
MIN_SCAN, MAX_SCAN = 10, 30


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): cv.string,
            vol.Required(CONF_PASSWORD): cv.string,
            vol.Optional(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, 10)
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)),
            vol.Optional(
                CONF_EXPOSE_PII, default=defaults.get(CONF_EXPOSE_PII, False)
            ): cv.boolean,
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, 10)
        ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN, max=MAX_SCAN)),
        vol.Optional(
            CONF_EXPOSE_PII, default=defaults.get(CONF_EXPOSE_PII, False)
        ): cv.boolean,
        vol.Optional(
            CONF_ENABLE_HEAT_COOL,
            default=defaults.get(CONF_ENABLE_HEAT_COOL, False),
        ): cv.boolean,
        vol.Optional(
            CONF_SLEEP_TIMEOUT_ENABLED,
            default=defaults.get(CONF_SLEEP_TIMEOUT_ENABLED, False),
        ): cv.boolean,
    }

    return vol.Schema(schema)


class AirzoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Primary config flow for the integration."""

    VERSION = 2

    @staticmethod
    def async_get_options_flow(
        entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Expose the options flow handler (top-level class below)."""
        return AirzoneOptionsFlow(entry)

    # ------------------------- Initial setup -------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect credentials, perform login to obtain token, and create the entry."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=_user_schema({}), errors={}
            )

        # Work on a shallow copy so we can normalize fields used as form defaults
        user_input = dict(user_input)

        email = str(user_input.get(CONF_USERNAME, "")).strip()
        user_input[CONF_USERNAME] = email
        password = str(user_input.get(CONF_PASSWORD, ""))
        # NOTE: We deliberately use the generic "invalid_auth" error here so that
        # empty credentials and bad credentials share the same translated message,
        # without introducing additional per-field error keys in translations.
        if not email or not password:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "invalid_auth"},
            )

        normalized_email = email.casefold()
        await self.async_set_unique_id(normalized_email)
        # NOTE: This will abort with the translated "already_configured"
        # reason if an entry for this account already exists.
        self._abort_if_unique_id_configured()
        scan = int(user_input.get(CONF_SCAN_INTERVAL, 10))
        pii = bool(user_input.get(CONF_EXPOSE_PII, False))

        # Local import to avoid circulars at HA import time
        try:
            from .airzone_api import AirzoneAPI
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Failed to import AirzoneAPI: %s", type(exc).__name__)
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "unknown"},
            )

        session = async_get_clientsession(self.hass)
        # Signature across the integration: (username, session, password=..., token=...)
        api = AirzoneAPI(email, session, password=password, token=None)

        try:
            # Be tolerant whether login() returns bool or token; prefer api.token finally.
            login_ret = await asyncio.wait_for(api.login(), timeout=60.0)
        except TimeoutError:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "timeout"},
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Login failed (network/other): %s", type(exc).__name__)
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "cannot_connect"},
            )
        finally:
            # Shorten lifetime of password in memory
            try:
                api.clear_password()
            except Exception:  # noqa: BLE001
                pass

        token: Any = getattr(api, "token", None)
        if isinstance(login_ret, str) and login_ret:
            token = login_ret

        if not isinstance(token, str) or not token.strip():
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(user_input),
                errors={"base": "invalid_auth"},
            )

        # Create the entry: username in data; token+settings in options
        return self.async_create_entry(
            title=email,
            data={CONF_USERNAME: email},
            options={
                "user_token": token,
                CONF_SCAN_INTERVAL: scan,
                CONF_EXPOSE_PII: pii,
            },
        )

    # ------------------------- Reauth -------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauth for an existing entry."""
        # HA usually provides entry_id in context; if not, we fallback below.
        self._reauth_entry_id = (self.context or {}).get("entry_id")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask only for password; refresh the token; never persist password."""
        # Resolve entry
        entry = None
        if getattr(self, "_reauth_entry_id", None):
            entry = self.hass.config_entries.async_get_entry(self._reauth_entry_id)
        if entry is None:
            # Fallback: if a single entry exists, use it
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if len(entries) == 1:
                entry = entries[0]
        if entry is None:
            return self.async_abort(reason="reauth_failed")

        username = entry.data.get(CONF_USERNAME)
        schema = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors={}
            )

        try:
            from .airzone_api import AirzoneAPI
        except Exception:
            return self.async_abort(reason="reauth_failed")

        session = async_get_clientsession(self.hass)
        api = AirzoneAPI(
            username, session, password=str(user_input[CONF_PASSWORD]), token=None
        )

        try:
            login_ret = await asyncio.wait_for(api.login(), timeout=60.0)
        except TimeoutError:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=schema, errors={"base": "timeout"}
            )
        except Exception:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=schema,
                errors={"base": "cannot_connect"},
            )
        finally:
            try:
                api.clear_password()
            except Exception:  # noqa: BLE001
                pass

        token: Any = getattr(api, "token", None)
        if isinstance(login_ret, str) and login_ret:
            token = login_ret

        if not isinstance(token, str) or not token.strip():
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=schema,
                errors={"base": "invalid_auth"},
            )

        # Merge options, preserving unknown keys
        new_opts = dict(entry.options)
        new_opts["user_token"] = token
        self.hass.config_entries.async_update_entry(entry, options=new_opts)
        return self.async_abort(reason="reauth_successful")


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Options flow that preserves hidden keys (like user_token)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    def _any_device_supports_heat_cool(self) -> bool | None:
        bucket = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

        if "heat_cool_supported" in bucket:
            cached = bucket.get("heat_cool_supported")
            if cached is True:
                return True
            if cached is False:
                return False
            return None

        coordinator = bucket.get("coordinator")
        data = getattr(coordinator, "data", None)
        if not data:
            return None

        try:
            return any(device_supports_heat_cool(dev) for dev in data.values())
        except Exception:  # noqa: BLE001
            return None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        opts = self._entry.options

        _supports_heat_cool = self._any_device_supports_heat_cool()
        current_heat_cool = bool(opts.get(CONF_ENABLE_HEAT_COOL, False))

        defaults = {
            CONF_SCAN_INTERVAL: int(opts.get(CONF_SCAN_INTERVAL, 10)),
            CONF_EXPOSE_PII: bool(opts.get(CONF_EXPOSE_PII, False)),
            CONF_ENABLE_HEAT_COOL: current_heat_cool,
            CONF_SLEEP_TIMEOUT_ENABLED: bool(
                opts.get(CONF_SLEEP_TIMEOUT_ENABLED, False)
            ),
        }

        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=_options_schema(defaults),
                errors={},
            )

        # Merge with existing options; never drop `user_token`
        next_opts = dict(self._entry.options)
        next_opts[CONF_SCAN_INTERVAL] = int(
            user_input.get(CONF_SCAN_INTERVAL, defaults[CONF_SCAN_INTERVAL])
        )
        next_opts[CONF_EXPOSE_PII] = bool(
            user_input.get(CONF_EXPOSE_PII, defaults[CONF_EXPOSE_PII])
        )
        next_opts[CONF_ENABLE_HEAT_COOL] = bool(
            user_input.get(CONF_ENABLE_HEAT_COOL, current_heat_cool)
        )
        next_opts[CONF_SLEEP_TIMEOUT_ENABLED] = bool(
            user_input.get(
                CONF_SLEEP_TIMEOUT_ENABLED, defaults[CONF_SLEEP_TIMEOUT_ENABLED]
            )
        )

        return self.async_create_entry(title="", data=next_opts)
