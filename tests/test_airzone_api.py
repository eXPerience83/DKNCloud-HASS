"""HTTP client retry behavior tests without Home Assistant dependencies."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

try:
    from aiohttp import ClientResponseError, ClientSession
    from aiohttp.client_reqrep import RequestInfo
    from multidict import CIMultiDict
    from yarl import URL
except ModuleNotFoundError:  # pragma: no cover - handled by CI deps
    # NOTE: This module-level skip is expected in lightweight environments
    # (e.g., Codex/Qodo) where aiohttp is not installed. CI installs aiohttp
    # via requirements_test.txt so these tests run in automation.
    pytest.skip("aiohttp is required for API retry tests", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Minimal Home Assistant shims so the package import works without HA
# ---------------------------------------------------------------------------
ha_module = types.ModuleType("homeassistant")
sys.modules["homeassistant"] = ha_module

components_module = types.ModuleType("homeassistant.components")
persistent_notification_module = types.ModuleType(
    "homeassistant.components.persistent_notification"
)
components_module.persistent_notification = persistent_notification_module
sys.modules["homeassistant.components"] = components_module
sys.modules["homeassistant.components.persistent_notification"] = (
    persistent_notification_module
)
ha_module.components = components_module

exceptions_module = types.ModuleType("homeassistant.exceptions")


class HomeAssistantError(Exception):
    """Minimal Home Assistant error placeholder."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args)
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_key = kwargs.get("translation_key")


exceptions_module.HomeAssistantError = HomeAssistantError
ha_module.exceptions = exceptions_module
sys.modules["homeassistant.exceptions"] = exceptions_module

config_entries_module = types.ModuleType("homeassistant.config_entries")
config_entries_module.SOURCE_REAUTH = "reauth"


class ConfigEntry:  # pragma: no cover - used only for import wiring
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.entry_id = "dummy"
        self.data = {}
        self.options = {}
        self.unique_id = None
        self.version = 1


config_entries_module.ConfigEntry = ConfigEntry
sys.modules["homeassistant.config_entries"] = config_entries_module
ha_module.config_entries = config_entries_module

const_module = types.ModuleType("homeassistant.const")
const_module.CONF_USERNAME = "username"
sys.modules["homeassistant.const"] = const_module
ha_module.const = const_module

core_module = types.ModuleType("homeassistant.core")


class HomeAssistant:  # pragma: no cover - signature irrelevant for tests
    pass


core_module.HomeAssistant = HomeAssistant
sys.modules["homeassistant.core"] = core_module
ha_module.core = core_module


class ConfigEntryAuthFailed(HomeAssistantError):
    """Minimal auth failure placeholder."""


exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed

helpers_module = types.ModuleType("homeassistant.helpers")
aiohttp_client_module = types.ModuleType("homeassistant.helpers.aiohttp_client")
sys.modules["homeassistant.helpers"] = helpers_module


def async_get_clientsession(*_: object, **__: object) -> None:
    return None


aiohttp_client_module.async_get_clientsession = async_get_clientsession
helpers_module.aiohttp_client = aiohttp_client_module
sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_module

event_module = types.ModuleType("homeassistant.helpers.event")


async def async_call_later(*_: object, **__: object) -> None:
    return None


event_module.async_call_later = async_call_later
helpers_module.event = event_module
sys.modules["homeassistant.helpers.event"] = event_module

translation_module = types.ModuleType("homeassistant.helpers.translation")


async def async_get_translations(*_: object, **__: object) -> dict[str, str]:
    return {}


translation_module.async_get_translations = async_get_translations
helpers_module.translation = translation_module
sys.modules["homeassistant.helpers.translation"] = translation_module

update_coordinator_module = types.ModuleType("homeassistant.helpers.update_coordinator")


class UpdateFailed(Exception):
    """Placeholder for coordinator update failures."""


class DataUpdateCoordinator:  # pragma: no cover - not exercised in tests
    def __init__(
        self,
        hass: object,
        *args: object,
        update_method: object | None = None,
        **kwargs: object,
    ) -> None:
        self.hass = hass
        self.update_method = update_method
        self.data = None
        self._listeners: list[object] = []

    async def async_config_entry_first_refresh(self) -> None:
        if self.update_method:
            self.data = await self.update_method()

    def async_add_listener(self, listener: object) -> object:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    async def async_request_refresh(self) -> None:
        return None

    # Allow generics like DataUpdateCoordinator[dict[str, Any]] in annotations.
    def __class_getitem__(cls, item: object) -> type:
        return cls


update_coordinator_module.UpdateFailed = UpdateFailed
update_coordinator_module.DataUpdateCoordinator = DataUpdateCoordinator
helpers_module.update_coordinator = update_coordinator_module
sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator_module

util_module = types.ModuleType("homeassistant.util")
dt_module = types.ModuleType("homeassistant.util.dt")
util_module.dt = dt_module
helpers_module.util = util_module
sys.modules["homeassistant.util"] = util_module
sys.modules["homeassistant.util.dt"] = dt_module

ha_module.helpers = helpers_module


from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI  # noqa: E402


def _client_response_error(
    status: int, headers: dict[str, str] | None = None
) -> ClientResponseError:
    """Create a ClientResponseError with minimal request context."""

    request_info = RequestInfo(
        URL("https://example.com"),
        "GET",
        CIMultiDict(),
        URL("https://example.com"),
    )
    return ClientResponseError(
        request_info,
        history=(),
        status=status,
        message="",
        headers=headers,
    )


def _make_api(
    monkeypatch: pytest.MonkeyPatch, responses: list[object]
) -> tuple[AirzoneAPI, list[float]]:
    """Create an API with deterministic time and response sequencing."""

    api = AirzoneAPI(username="user@example.com", session=AsyncMock(spec=ClientSession))
    sleeps: list[float] = []
    clock = {"now": 0.0}

    monkeypatch.setattr(api, "_now", lambda: clock["now"])

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(api, "_sleep", fake_sleep)
    monkeypatch.setattr(api, "_request", AsyncMock(side_effect=responses))
    return api, sleeps


@pytest.mark.asyncio
async def test_login_success_sets_token_and_preserves_password() -> None:
    api = AirzoneAPI(
        username="user@example.com",
        password="secret",
        session=AsyncMock(spec_set=ClientSession),
    )
    with patch.object(
        api,
        "_request",
        AsyncMock(return_value={"user": {"authentication_token": "tok"}}),
    ):
        assert await api.login() is True

    assert api.token == "tok"
    assert api.password == "secret"


@pytest.mark.asyncio
async def test_login_handles_unauthorized() -> None:
    api = AirzoneAPI(
        username="user@example.com",
        password="secret",
        session=AsyncMock(spec_set=ClientSession),
    )
    with patch.object(
        api,
        "_request",
        AsyncMock(side_effect=_client_response_error(status=401)),
    ):
        assert await api.login() is False

    assert api.token is None


@pytest.mark.asyncio
async def test_authed_request_retries_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    retry_error = _client_response_error(status=429, headers={"Retry-After": "2"})
    api, sleeps = _make_api(monkeypatch, [retry_error, {"ok": True}])

    result = await api._authed_request_with_retries("GET", "/foo")

    assert result == {"ok": True}
    assert sleeps == [2.0]
    assert api._cooldown_until == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_authed_request_5xx_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    errors = [_client_response_error(status=500) for _ in range(4)]
    api, sleeps = _make_api(monkeypatch, errors)

    with pytest.raises(ClientResponseError):
        await api._authed_request_with_retries("GET", "/bar")

    assert sleeps == pytest.approx([0.6, 1.2, 2.4])
    assert api._cooldown_until == 0.0


@pytest.mark.asyncio
async def test_timeout_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    api, sleeps = _make_api(monkeypatch, [TimeoutError(), {"ok": True}])

    result = await api._authed_request_with_retries("GET", "/baz")

    assert result == {"ok": True}
    assert sleeps == pytest.approx([0.4])


@pytest.mark.asyncio
async def test_error_logs_do_not_leak_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = AirzoneAPI(
        username="user@example.com",
        password="topsecret",
        session=AsyncMock(spec=ClientSession),
    )
    with patch.object(
        api,
        "_request",
        AsyncMock(side_effect=_client_response_error(status=503)),
    ):
        caplog.set_level("DEBUG")
        with pytest.raises(ClientResponseError):
            await api._authed_request_with_retries("GET", "/api/secure?pw=topsecret")

    assert "topsecret" not in caplog.text
    assert "user@example.com" not in caplog.text


@pytest.mark.asyncio
async def test_async_set_scenary_uses_wrapped_payload() -> None:
    api = AirzoneAPI(
        username="user@example.com",
        password="secret",
        session=AsyncMock(spec_set=ClientSession),
    )
    api.put_device_fields = AsyncMock()

    await api.async_set_scenary("123", "sleep")

    api.put_device_fields.assert_awaited_once_with(
        "123", {"device": {"scenary": "sleep"}}
    )


@pytest.mark.asyncio
async def test_send_event_maps_423_machine_not_ready() -> None:
    api = AirzoneAPI(
        username="user@example.com",
        password="secret",
        session=AsyncMock(spec_set=ClientSession),
    )
    api._authed_request_with_retries = AsyncMock(
        side_effect=_client_response_error(status=423)
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await api.send_event({"event": "payload"})

    assert exc_info.value.translation_domain == "airzoneclouddaikin"
    assert exc_info.value.translation_key == "machine_not_ready"
