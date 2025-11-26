"""Config and options flow tests using lightweight Home Assistant stubs."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest


class FlowResultType:
    """Minimal FlowResultType replacement used by the stubs."""

    FORM = "form"
    CREATE_ENTRY = "create_entry"
    ABORT = "abort"


class AbortFlow(Exception):
    """Raised when a duplicate unique ID is configured."""


class ConfigEntry:
    """Simple ConfigEntry stand-in for tests."""

    _id = 0

    def __init__(
        self,
        *,
        domain: str,
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        unique_id: str | None = None,
    ) -> None:
        ConfigEntry._id += 1
        self.entry_id = f"entry_{ConfigEntry._id}"
        self.domain = domain
        self.data = data or {}
        self.options = options or {}
        self.unique_id = unique_id

    def add_to_hass(self, hass: HassStub) -> None:
        hass.config_entries._entries.append(self)


class ConfigEntries:
    """Collection helper for ConfigEntry stubs."""

    def __init__(self) -> None:
        self._entries: list[ConfigEntry] = []

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def async_entries(self, domain: str | None = None) -> list[ConfigEntry]:
        if domain is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.domain == domain]

    def async_update_entry(
        self, entry: ConfigEntry, *, options: dict[str, Any]
    ) -> None:
        entry.options = options

    def _unique_id_exists(self, unique_id: str | None) -> bool:
        if unique_id is None:
            return False
        return any(entry.unique_id == unique_id for entry in self._entries)


class ConfigFlow:
    """Minimal ConfigFlow base to satisfy the integration flow."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Allow Home Assistant-style subclass kwargs like domain."""
        super().__init_subclass__()

    def __init__(self) -> None:
        self.hass: HassStub | None = None
        self.context: dict[str, Any] = {}
        self._unique_id: str | None = None

    async def async_set_unique_id(self, unique_id: str) -> None:
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        if self.hass and self.hass.config_entries._unique_id_exists(self._unique_id):
            raise AbortFlow()

    def async_show_form(
        self, *, step_id: str, data_schema: Any, errors: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return {
            "type": FlowResultType.FORM,
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_abort(self, *, reason: str) -> dict[str, Any]:
        return {"type": FlowResultType.ABORT, "reason": reason}

    def async_create_entry(
        self, *, title: str, data: dict[str, Any], options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "type": FlowResultType.CREATE_ENTRY,
            "title": title,
            "data": data,
            "options": options or {},
        }


class OptionsFlow:
    """Minimal OptionsFlow base."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry
        self.hass: HassStub | None = None

    def async_show_form(
        self, *, step_id: str, data_schema: Any, errors: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return {
            "type": FlowResultType.FORM,
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_create_entry(
        self, *, title: str = "", data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"type": FlowResultType.CREATE_ENTRY, "title": title, "data": data or {}}


class HassStub:
    """Lightweight Home Assistant stub with config entry tracking."""

    def __init__(self) -> None:
        self.config_entries = ConfigEntries()
        self.data: dict[str, Any] = {}


def _install_voluptuous_stub() -> None:
    """Provide a tiny voluptuous stub to avoid external dependency."""

    vol_module = types.ModuleType("voluptuous")

    class Schema:  # pragma: no cover - structure only
        def __init__(self, data: Any) -> None:
            self.data = data

    class Marker:  # pragma: no cover - structure only
        def __init__(self, key: Any, default: Any | None = None) -> None:
            self.key = key
            self.default = default

    class Required(Marker):
        pass

    class Optional(Marker):
        pass

    class Range:  # pragma: no cover - structure only
        def __init__(self, min: int | None = None, max: int | None = None) -> None:
            self.min = min
            self.max = max

    def All(*validators: Any) -> tuple[Any, ...]:  # noqa: N802 - match voluptuous API
        return validators

    def Coerce(target_type: type) -> Any:  # noqa: N802 - match voluptuous API
        return target_type

    vol_module.Schema = Schema
    vol_module.Required = Required
    vol_module.Optional = Optional
    vol_module.Range = Range
    vol_module.All = All
    vol_module.Coerce = Coerce

    sys.modules["voluptuous"] = vol_module


def _install_homeassistant_stubs() -> None:
    """Install minimal Home Assistant modules for importing config_flow."""

    ha_module = sys.modules.setdefault(
        "homeassistant", types.ModuleType("homeassistant")
    )

    config_entries_module = types.ModuleType("homeassistant.config_entries")
    config_entries_module.ConfigFlow = ConfigFlow
    config_entries_module.OptionsFlow = OptionsFlow
    config_entries_module.ConfigEntry = ConfigEntry
    config_entries_module.SOURCE_USER = "user"
    config_entries_module.SOURCE_REAUTH = "reauth"
    sys.modules["homeassistant.config_entries"] = config_entries_module

    const_module = types.ModuleType("homeassistant.const")
    const_module.CONF_USERNAME = "username"
    const_module.CONF_PASSWORD = "password"
    sys.modules["homeassistant.const"] = const_module

    core_module = types.ModuleType("homeassistant.core")
    sys.modules["homeassistant.core"] = core_module

    data_entry_flow_module = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow_module.FlowResultType = FlowResultType
    data_entry_flow_module.FlowResult = dict
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow_module

    helpers_module = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )

    aiohttp_client_module = types.ModuleType("homeassistant.helpers.aiohttp_client")

    def async_get_clientsession(_hass: HassStub) -> object:
        return object()

    aiohttp_client_module.async_get_clientsession = async_get_clientsession
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_module

    cv_module = types.ModuleType("homeassistant.helpers.config_validation")

    def boolean(value: Any) -> bool:
        return bool(value)

    def string(value: Any) -> str:
        return str(value)

    cv_module.boolean = boolean
    cv_module.string = string
    helpers_module.config_validation = cv_module
    sys.modules["homeassistant.helpers.config_validation"] = cv_module

    ha_module.config_entries = config_entries_module
    ha_module.const = const_module
    ha_module.core = core_module
    ha_module.data_entry_flow = data_entry_flow_module
    ha_module.helpers = helpers_module


_install_voluptuous_stub()
_install_homeassistant_stubs()

from homeassistant.config_entries import SOURCE_REAUTH  # type: ignore  # noqa: E402
from homeassistant.const import (  # type: ignore  # noqa: E402
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import (  # type: ignore  # noqa: E402
    FlowResultType as HAFlowResultType,
)

from custom_components.airzoneclouddaikin.config_flow import (  # noqa: E402
    CONF_EXPOSE_PII,
    CONF_SCAN_INTERVAL,
    AirzoneConfigFlow,
    AirzoneOptionsFlow,
)
from custom_components.airzoneclouddaikin.const import (  # noqa: E402
    CONF_ENABLE_HEAT_COOL,
    DOMAIN,
)


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def hass() -> HassStub:
    """Provide a fresh Hass stub for each test."""

    return HassStub()


@pytest.fixture
def api_mock() -> AsyncMock:
    """Fixture that installs a stub AirzoneAPI returning a shared mock."""

    api_mock = AsyncMock()
    api_mock.login = AsyncMock(return_value="login-token")
    api_mock.token = "login-token"
    api_mock.clear_password = lambda: None

    module_path = "custom_components.airzoneclouddaikin.airzone_api"
    api_module = types.ModuleType(module_path)
    api_module.AirzoneAPI = lambda *args, **kwargs: api_mock
    sys.modules[module_path] = api_module

    parent = sys.modules.get("custom_components.airzoneclouddaikin")
    if parent is not None:
        parent.airzone_api = api_module

    return api_mock


def test_user_step_shows_initial_form(hass: HassStub) -> None:
    flow = AirzoneConfigFlow()
    flow.hass = hass

    result = _run(flow.async_step_user(user_input=None))

    assert result["type"] is HAFlowResultType.FORM
    assert result["step_id"] == "user"
    assert "data_schema" in result


def test_user_step_success_creates_entry(hass: HassStub, api_mock: AsyncMock) -> None:
    flow = AirzoneConfigFlow()
    flow.hass = hass

    user_input = {
        CONF_USERNAME: "User@Example.Com ",
        CONF_PASSWORD: "secret",
        CONF_SCAN_INTERVAL: 15,
        CONF_EXPOSE_PII: True,
    }

    result = _run(flow.async_step_user(user_input=user_input))

    assert result["type"] is HAFlowResultType.CREATE_ENTRY
    normalized_email = user_input[CONF_USERNAME].strip()
    assert result["title"] == normalized_email
    assert result["data"] == {CONF_USERNAME: normalized_email}

    options = result["options"]
    assert options["user_token"] == "login-token"
    assert options[CONF_SCAN_INTERVAL] == 15
    assert options[CONF_EXPOSE_PII] is True
    assert CONF_ENABLE_HEAT_COOL not in options


def test_user_step_invalid_auth_from_api(hass: HassStub, api_mock: AsyncMock) -> None:
    flow = AirzoneConfigFlow()
    flow.hass = hass

    api_mock.login = AsyncMock(return_value="")
    api_mock.token = ""

    result = _run(
        flow.async_step_user(
            user_input={
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_SCAN_INTERVAL: 15,
                CONF_EXPOSE_PII: False,
            }
        )
    )

    assert result["type"] is HAFlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


def test_user_step_cannot_connect(hass: HassStub, api_mock: AsyncMock) -> None:
    flow = AirzoneConfigFlow()
    flow.hass = hass

    api_mock.login = AsyncMock(side_effect=RuntimeError("boom"))

    result = _run(
        flow.async_step_user(
            user_input={
                CONF_USERNAME: "user@example.com",
                CONF_PASSWORD: "secret",
                CONF_SCAN_INTERVAL: 15,
                CONF_EXPOSE_PII: False,
            }
        )
    )

    assert result["type"] is HAFlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


def test_reauth_flow_success_updates_token(hass: HassStub, api_mock: AsyncMock) -> None:
    entry = ConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "old-token",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
        },
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    flow = AirzoneConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_REAUTH, "entry_id": entry.entry_id}

    result = _run(flow.async_step_reauth(entry.data))

    assert result["type"] is HAFlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result2 = _run(
        flow.async_step_reauth_confirm(user_input={CONF_PASSWORD: "new-secret"})
    )

    assert result2["type"] is HAFlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.options["user_token"] == "login-token"
    assert entry.options[CONF_SCAN_INTERVAL] == 10
    assert entry.options[CONF_EXPOSE_PII] is False


def test_options_flow_updates_options_and_preserves_hidden_keys(hass: HassStub) -> None:
    entry = ConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "tok-123",
            "hidden_key": "keep-me",
            CONF_SCAN_INTERVAL: 10,
            CONF_EXPOSE_PII: False,
            CONF_ENABLE_HEAT_COOL: False,
        },
        unique_id="user@example.com",
    )
    entry.add_to_hass(hass)

    flow = AirzoneOptionsFlow(entry)
    flow.hass = hass

    result = _run(flow.async_step_init(user_input=None))

    assert result["type"] is HAFlowResultType.FORM
    assert result["step_id"] == "init"
    assert "data_schema" in result

    result2 = _run(
        flow.async_step_init(
            user_input={
                CONF_SCAN_INTERVAL: 20,
                CONF_EXPOSE_PII: True,
                CONF_ENABLE_HEAT_COOL: True,
            }
        )
    )

    assert result2["type"] is HAFlowResultType.CREATE_ENTRY
    new_options: dict[str, Any] = result2["data"]
    assert new_options[CONF_SCAN_INTERVAL] == 20
    assert new_options[CONF_EXPOSE_PII] is True
    assert new_options[CONF_ENABLE_HEAT_COOL] is True
    assert new_options["user_token"] == "tok-123"
    assert new_options["hidden_key"] == "keep-me"
