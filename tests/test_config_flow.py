"""Functional tests for config and options flows without Home Assistant runtime."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------- Home Assistant stubs ---------------------------
ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

const_module = sys.modules.setdefault(
    "homeassistant.const", types.ModuleType("homeassistant.const")
)
const_module.CONF_PASSWORD = "password"
const_module.CONF_USERNAME = "username"

core_module = sys.modules.setdefault(
    "homeassistant.core", types.ModuleType("homeassistant.core")
)


class HomeAssistant:  # pragma: no cover - type stub only
    pass


core_module.HomeAssistant = HomeAssistant

config_entries_module = sys.modules.setdefault(
    "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
)

config_entries_module.SOURCE_REAUTH = "reauth"


class AbortFlow(Exception):
    """Abort marker carrying the resulting flow payload."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("Flow aborted")
        self.result = result


data_entry_flow_module = sys.modules.setdefault(
    "homeassistant.data_entry_flow", types.ModuleType("homeassistant.data_entry_flow")
)

data_entry_flow_module.AbortFlow = AbortFlow
FlowResult = dict[str, Any]
data_entry_flow_module.FlowResult = FlowResult


class ConfigEntryStub:
    def __init__(
        self,
        *,
        entry_id: str,
        domain: str,
        data: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        self.entry_id = entry_id
        self.domain = domain
        self.data = data
        self.options = options
        self.unique_id: str | None = data.get("unique_id")


class ConfigEntriesStub:
    """Minimal storage for config entries used by the flow."""

    def __init__(self) -> None:
        self._entries: list[ConfigEntryStub] = []

    def async_entries(self, domain: str | None = None) -> list[ConfigEntryStub]:
        if domain is None:
            return list(self._entries)
        return [entry for entry in self._entries if entry.domain == domain]

    def async_get_entry(self, entry_id: str | None) -> ConfigEntryStub | None:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def async_update_entry(self, entry: ConfigEntryStub, **kwargs: Any) -> None:
        if "options" in kwargs:
            entry.options = kwargs["options"]

    def add_entry(self, entry: ConfigEntryStub) -> None:
        self._entries.append(entry)


data_entry_flow_module.FlowHandler = object


class ConfigFlow:
    """Slim ConfigFlow base implementing HA helpers."""

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: D401
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)

    def __init__(self) -> None:
        self.hass: HassStub | None = None
        self.context: dict[str, Any] = {}
        self._unique_id: str | None = None

    async def async_set_unique_id(self, unique_id: str | None = None) -> None:
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        if not self.hass:
            return
        if any(
            entry.unique_id == self._unique_id
            for entry in self.hass.config_entries.async_entries()
        ):
            raise AbortFlow(self.async_abort(reason="already_configured"))

    def async_show_form(
        self, *, step_id: str, data_schema: Any, errors: dict[str, str]
    ) -> FlowResult:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }

    def async_abort(self, *, reason: str) -> FlowResult:
        return {"type": "abort", "reason": reason}

    def async_create_entry(
        self, *, title: str, data: dict[str, Any], options: dict[str, Any] | None = None
    ) -> FlowResult:
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "options": options or {},
        }


config_entries_module.ConfigFlow = ConfigFlow


class OptionsFlow:
    def __init__(self, entry: ConfigEntryStub) -> None:
        self.hass: HassStub | None = None
        self._entry = entry

    def async_show_form(
        self, *, step_id: str, data_schema: Any, errors: dict[str, str]
    ) -> FlowResult:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }

    def async_create_entry(self, *, title: str, data: dict[str, Any]) -> FlowResult:
        return {"type": "create_entry", "title": title, "data": data}


config_entries_module.OptionsFlow = OptionsFlow
config_entries_module.ConfigEntry = ConfigEntryStub


class HassStub:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.config_entries = ConfigEntriesStub()


# aiohttp client session stub
aiohttp_client_module = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)


def async_get_clientsession(_hass: HassStub) -> object:  # pragma: no cover - trivial
    return object()


aiohttp_client_module.async_get_clientsession = async_get_clientsession

helpers_module = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
helpers_cv_module = sys.modules.setdefault(
    "homeassistant.helpers.config_validation",
    types.ModuleType("homeassistant.helpers.config_validation"),
)
helpers_module.config_validation = helpers_cv_module
sys.modules.setdefault("homeassistant.helpers.config_validation", helpers_cv_module)

helpers_event_module = sys.modules.setdefault(
    "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
)
helpers_event_module.async_call_later = lambda *_args, **_kwargs: None

helpers_update_module = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class DataUpdateCoordinator:  # pragma: no cover - type stub only
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass


helpers_update_module.DataUpdateCoordinator = DataUpdateCoordinator

helpers_cv_module.string = str
helpers_cv_module.boolean = bool
helpers_cv_module.multi_select = lambda items: items
helpers_cv_module.template_complex = lambda value: value

helpers_module.event = helpers_event_module
helpers_module.update_coordinator = helpers_update_module
sys.modules.setdefault("homeassistant.helpers.event", helpers_event_module)
sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator", helpers_update_module
)

ha_module.helpers = helpers_module
ha_module.config_entries = config_entries_module
ha_module.data_entry_flow = data_entry_flow_module
ha_module.const = const_module
ha_module.helpers.aiohttp_client = aiohttp_client_module  # type: ignore[attr-defined]
ha_module.helpers.event = helpers_event_module  # type: ignore[attr-defined]
ha_module.helpers.update_coordinator = helpers_update_module  # type: ignore[attr-defined]
ha_module.core = core_module


# --------------------------- Voluptuous stub ---------------------------
vol_module = types.ModuleType("voluptuous")


class _Marker:
    def __init__(self, key: Any, default: Any | None = None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:  # pragma: no cover - mapping support only
        return hash((self.key, self.default))

    def __eq__(self, other: object) -> bool:  # pragma: no cover - mapping support only
        return isinstance(other, _Marker) and (self.key, self.default) == (
            other.key,
            other.default,
        )


class Schema:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Schema) and other.value == self.value


vol_module.Schema = Schema
vol_module.Required = lambda key, default=None: _Marker(key, default)
vol_module.Optional = lambda key, default=None: _Marker(key, default)
vol_module.All = lambda *validators: ("all", validators)
vol_module.Coerce = lambda type_fn: ("coerce", type_fn)
vol_module.Range = lambda **kwargs: ("range", kwargs)

sys.modules["voluptuous"] = vol_module


# ------------------------------- API stub -------------------------------
airzone_api_module = types.ModuleType(
    "custom_components.airzoneclouddaikin.airzone_api"
)


class AirzoneAPIMock:
    def __init__(
        self,
        username: str,
        _session: object,
        *,
        password: str | None,
        token: str | None,
    ) -> None:
        self.username = username
        self.password = password
        self.token = token

    async def login(self) -> str | bool:
        behavior = _AIRZONE_BEHAVIOR["login"]
        if isinstance(behavior, Exception):
            raise behavior
        if callable(behavior):
            return await _maybe_await(behavior(self))
        return behavior

    def clear_password(self) -> None:
        self.password = None


def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return value
    return value


_AIRZONE_BEHAVIOR: dict[str, Any] = {"login": "token-from-api"}
airzone_api_module.AirzoneAPI = AirzoneAPIMock

custom_components_pkg = sys.modules.setdefault(
    "custom_components", types.ModuleType("custom_components")
)
custom_components_pkg.__path__ = [str(ROOT / "custom_components")]

integration_pkg = sys.modules.setdefault(
    "custom_components.airzoneclouddaikin",
    types.ModuleType("custom_components.airzoneclouddaikin"),
)
integration_pkg.__path__ = [str(ROOT / "custom_components" / "airzoneclouddaikin")]
sys.modules["custom_components.airzoneclouddaikin.airzone_api"] = airzone_api_module


# --------------------------- Module under test ---------------------------
@pytest.fixture(name="config_flow")
def fixture_config_flow() -> types.ModuleType:
    """Import and expose the config_flow module with stubs in place."""

    module = importlib.import_module("custom_components.airzoneclouddaikin.config_flow")
    return module


@pytest.fixture(autouse=True)
def reset_api_behavior() -> None:
    _AIRZONE_BEHAVIOR["login"] = "token-from-api"


# --------------------------------- Tests ---------------------------------


def test_user_step_initial_form(config_flow: types.ModuleType) -> None:
    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    result = asyncio.run(flow.async_step_user(None))

    assert result == {
        "type": "form",
        "step_id": "user",
        "data_schema": config_flow._user_schema({}),
        "errors": {},
    }


def test_user_step_success_creates_entry(config_flow: types.ModuleType) -> None:
    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    user_input = {
        config_flow.CONF_USERNAME: "user@example.com",
        config_flow.CONF_PASSWORD: "secret",
        config_flow.CONF_SCAN_INTERVAL: 15,
        config_flow.CONF_EXPOSE_PII: True,
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result["type"] == "create_entry"
    assert result["title"] == "user@example.com"
    assert result["data"] == {config_flow.CONF_USERNAME: "user@example.com"}
    assert result["options"]["user_token"] == "token-from-api"
    assert result["options"][config_flow.CONF_SCAN_INTERVAL] == 15
    assert result["options"][config_flow.CONF_EXPOSE_PII] is True


def test_user_step_timeout_returns_error(config_flow: types.ModuleType) -> None:
    _AIRZONE_BEHAVIOR["login"] = TimeoutError()

    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    user_input = {
        config_flow.CONF_USERNAME: "user@example.com",
        config_flow.CONF_PASSWORD: "secret",
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "timeout"}


def test_user_step_cannot_connect_error(config_flow: types.ModuleType) -> None:
    _AIRZONE_BEHAVIOR["login"] = Exception("net down")

    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    user_input = {
        config_flow.CONF_USERNAME: "user@example.com",
        config_flow.CONF_PASSWORD: "secret",
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


def test_user_step_invalid_auth_when_token_missing(
    config_flow: types.ModuleType,
) -> None:
    _AIRZONE_BEHAVIOR["login"] = ""

    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    user_input = {
        config_flow.CONF_USERNAME: "user@example.com",
        config_flow.CONF_PASSWORD: "secret",
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


def test_reauth_success_updates_token(config_flow: types.ModuleType) -> None:
    flow = config_flow.AirzoneConfigFlow()
    hass = HassStub()
    flow.hass = hass

    entry = ConfigEntryStub(
        entry_id="entry-1",
        domain=config_flow.DOMAIN,
        data={config_flow.CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "old",
            config_flow.CONF_SCAN_INTERVAL: 10,
            config_flow.CONF_EXPOSE_PII: False,
        },
    )
    hass.config_entries.add_entry(entry)
    flow.context["entry_id"] = "entry-1"

    initial = asyncio.run(flow.async_step_reauth({}))
    assert initial["type"] == "form"
    assert initial["step_id"] == "reauth_confirm"

    result = asyncio.run(
        flow.async_step_reauth_confirm({config_flow.CONF_PASSWORD: "newpass"})
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.options["user_token"] == "token-from-api"
    assert entry.options[config_flow.CONF_SCAN_INTERVAL] == 10
    assert entry.options[config_flow.CONF_EXPOSE_PII] is False


def test_reauth_without_entry_aborts(config_flow: types.ModuleType) -> None:
    flow = config_flow.AirzoneConfigFlow()
    flow.hass = HassStub()

    result = asyncio.run(flow.async_step_reauth({}))

    assert result == {"type": "abort", "reason": "reauth_failed"}


def test_options_flow_preserves_token_and_updates_fields(
    config_flow: types.ModuleType,
) -> None:
    entry = ConfigEntryStub(
        entry_id="entry-1",
        domain=config_flow.DOMAIN,
        data={config_flow.CONF_USERNAME: "user@example.com"},
        options={
            "user_token": "persist-me",
            config_flow.CONF_SCAN_INTERVAL: 12,
            config_flow.CONF_EXPOSE_PII: False,
            config_flow.CONF_ENABLE_HEAT_COOL: False,
        },
    )

    flow = config_flow.AirzoneOptionsFlow(entry)
    flow.hass = HassStub()

    initial = asyncio.run(flow.async_step_init(None))
    assert initial["type"] == "form"
    assert initial["step_id"] == "init"

    user_input = {
        config_flow.CONF_SCAN_INTERVAL: 20,
        config_flow.CONF_EXPOSE_PII: True,
        config_flow.CONF_ENABLE_HEAT_COOL: True,
    }

    result = asyncio.run(flow.async_step_init(user_input))

    assert result["type"] == "create_entry"
    assert result["data"]["user_token"] == "persist-me"
    assert result["data"][config_flow.CONF_SCAN_INTERVAL] == 20
    assert result["data"][config_flow.CONF_EXPOSE_PII] is True
    assert result["data"][config_flow.CONF_ENABLE_HEAT_COOL] is True
