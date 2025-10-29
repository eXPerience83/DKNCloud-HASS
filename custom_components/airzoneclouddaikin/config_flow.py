"""Config & Options flow for DKN Cloud for HASS (0.4.0a6).

Fixes
-----
- Prevent token loss when saving Options by **merging** with existing `entry.options`
  and preserving hidden keys (notably `user_token`). Also copy legacy token from
  `entry.data` if present (0.4.x migrations).
- Make **reauth robust** even if the reauth context lacks `entry_id` by resolving
  the target entry via username or falling back to the first domain entry.
- Avoid hard imports of optional constants that can cause import-time 500s.

Security & UX
-------------
- Token lives in `entry.options["user_token"]` (encrypted at rest by HA).
- Password is never persisted; it is zeroed immediately after use.
- ConfigFlow.VERSION == 2, aligned with entry migration.

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

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# ---- Keys used in options/data (stringly-typed to avoid hard import coupling) ----
CONF_SCAN_INTERVAL = "scan_interval"
CONF_EXPOSE_PII = "expose_pii_identifiers"
CONF_STALE_AFTER_MINUTES = "stale_after_minutes"

# Fallback default if const import is unavailable or missing the symbol
try:  # soft dependency
    from .const import STALE_AFTER_MINUTES_DEFAULT as _STALE_DEFAULT  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    _STALE_DEFAULT = 12  # sane fallback

# ---------------------------- Schemas --------------------------------------
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

    VERSION = 2  # align with __init__.py migration

    def __init__(self) -> None:
        self._reauth_entry_id: str | None = None
        self._reauth_username: str | None = None

    # --------------------------------- Setup ---------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect credentials, login to mint token, and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                from .airzone_api import AirzoneAPI  # local import to minimize coupling
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to import AirzoneAPI: %s", type(exc).__name__)
                errors["base"] = "unknown"
            else:
                session = async_get_clientsession(self.hass)
                pwd = (user_input.get(CONF_PASSWORD) or "").strip()
                api = AirzoneAPI((user_input.get(CONF_USERNAME) or "").strip(), session, password=pwd)
                try:
                    ok = await api.login()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Login failed (%s)", type(exc).__name__)
                    errors["base"] = "cannot_connect"
                else:
                    # Wipe password from memory ASAP
                    try:
                        if pwd:
                            pwd = ""
                        if CONF_PASSWORD in user_input:
                            user_input[CONF_PASSWORD] = None
                            del user_input[CONF_PASSWORD]
                    except Exception:  # noqa: BLE001
                        pass

                    if ok and getattr(api, "token", None):
                        return self.async_create_entry(
                            title="DKN Cloud for HASS",
                            data={
                                CONF_USERNAME: (user_input.get(CONF_USERNAME) or "").strip(),
                                # Place token and basic tunables in data, they will be migrated to options.
                                "user_token": api.token,
                                CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, 10)),
                                CONF_EXPOSE_PII: bool(user_input.get(CONF_EXPOSE_PII, False)),
                                CONF_STALE_AFTER_MINUTES: int(_STALE_DEFAULT),
                            },
                        )
                    errors["base"] = "invalid_auth"

        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors=errors)

    # -------------------------------- Reauth ---------------------------------

    async def async_step_reauth(self, data: dict[str, Any] | None) -> FlowResult:
        """Start reauth (HA passes `entry_id` in context and usually `username` in `data`)."""
        # Cache context username if provided
        self._reauth_username = (data or {}).get(CONF_USERNAME)
        # Resolve the target entry as robustly as possible
        entry = self._resolve_entry_for_reauth(self._reauth_username)
        if entry:
            self._reauth_entry_id = entry.entry_id
        else:
            _LOGGER.warning("Reauth started without resolvable entry; will still render password form.")
            self._reauth_entry_id = None
        return await self.async_step_reauth_confirm()

    def _resolve_entry_for_reauth(self, username_hint: str | None):
        """Try to resolve which entry to reauth when context lacks entry_id (robustness)."""
        # 1) Use entry_id from context if present
        ctx_entry_id = (self.context or {}).get("entry_id")
        if ctx_entry_id:
            ent = self.hass.config_entries.async_get_entry(ctx_entry_id)
            if ent:
                return ent
        # 2) Try by username hint
        if username_hint:
            for ent in self.hass.config_entries.async_entries(DOMAIN):
                if ent.data.get(CONF_USERNAME) == username_hint:
                    return ent
        # 3) Fallback to first domain entry (single-instance common case)
        entries = self.hass.config_entries.async_entries(DOMAIN)
        return entries[0] if entries else None

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask only for the password; on success, update the token in options."""
        errors: dict[str, str] = {}

        # Resolve entry now (handles rare cases where it wasn't available earlier)
        entry = None
        if self._reauth_entry_id:
            entry = self.hass.config_entries.async_get_entry(self._reauth_entry_id)
        if entry is None:
            entry = self._resolve_entry_for_reauth(self._reauth_username)
        if entry is None:
            # We cannot proceed without a target entry; ask user to remove/add integration.
            return self.async_abort(reason="reauth_failed")

        username = entry.data.get(CONF_USERNAME) or self._reauth_username or ""

        schema = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})

        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        try:
            from .airzone_api import AirzoneAPI  # local import
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Failed to import AirzoneAPI during reauth: %s", type(exc).__name__)
            errors["base"] = "unknown"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        session = async_get_clientsession(self.hass)
        pwd = (user_input.get(CONF_PASSWORD) or "").strip()
        api = AirzoneAPI(username, session, password=pwd)

        try:
            ok = await asyncio.wait_for(api.login(), timeout=60.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("Reauth login timed out after 60s.")
            errors["base"] = "timeout"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Reauth login failed: %s", type(exc).__name__)
            errors["base"] = "cannot_connect"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)
        finally:
            # Wipe password ASAP
            try:
                if pwd:
                    pwd = ""
                if user_input and CONF_PASSWORD in user_input:
                    user_input[CONF_PASSWORD] = None
                    del user_input[CONF_PASSWORD]
            except Exception:  # noqa: BLE001
                pass

        if not ok or not getattr(api, "token", None):
            errors["base"] = "invalid_auth"
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

        # Merge new token into options; never persist password
        new_opts = dict(entry.options)
        new_opts["user_token"] = api.token
        # Also keep other options untouched
        self.hass.config_entries.async_update_entry(entry, options=new_opts)

        # Let HA show the success toast and reload the entry
        return self.async_abort(reason="reauth_successful")

    # ------------------------------ Options ----------------------------------

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return AirzoneOptionsFlow(entry)


class AirzoneOptionsFlow(config_entries.OptionsFlow):
    """Edit configurable tunables while preserving hidden keys like `user_token`."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        data = self._entry.data
        opts = self._entry.options

        cur_scan = int(opts.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, 10)))
        cur_pii = bool(opts.get(CONF_EXPOSE_PII, data.get(CONF_EXPOSE_PII, False)))
        cur_stale = int(opts.get(CONF_STALE_AFTER_MINUTES, data.get(CONF_STALE_AFTER_MINUTES, _STALE_DEFAULT)))

        if user_input is not None:
            # Start from existing options to avoid dropping hidden keys
            new_options: dict[str, Any] = dict(opts)
            new_options[CONF_SCAN_INTERVAL] = int(user_input.get(CONF_SCAN_INTERVAL, cur_scan))
            new_options[CONF_EXPOSE_PII] = bool(user_input.get(CONF_EXPOSE_PII, cur_pii))
            new_options[CONF_STALE_AFTER_MINUTES] = int(user_input.get(CONF_STALE_AFTER_MINUTES, cur_stale))

            # Safety net: if token still lives in data (during 0.4.x migration), copy it once
            if "user_token" not in new_options and "user_token" in data:
                new_options["user_token"] = data["user_token"]

            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=cur_scan): vol.All(vol.Coerce(int), vol.Range(min=10, max=30)),
                vol.Optional(CONF_EXPOSE_PII, default=cur_pii): cv.boolean,
                vol.Optional(CONF_STALE_AFTER_MINUTES, default=cur_stale): vol.All(vol.Coerce(int), vol.Range(min=6, max=30)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
