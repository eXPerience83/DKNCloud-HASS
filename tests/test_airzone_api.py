"""HTTP client retry behavior tests without Home Assistant dependencies."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("aiohttp")

from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict
from yarl import URL

ha_module = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
exceptions_module = types.ModuleType("homeassistant.exceptions")


class HomeAssistantError(Exception):
    """Minimal Home Assistant error placeholder."""


exceptions_module.HomeAssistantError = HomeAssistantError
ha_module.exceptions = exceptions_module
sys.modules.setdefault("homeassistant.exceptions", exceptions_module)

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
        session=AsyncMock(spec=ClientSession),
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
        session=AsyncMock(spec=ClientSession),
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
