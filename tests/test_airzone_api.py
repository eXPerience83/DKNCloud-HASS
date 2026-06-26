"""Tests for the Airzone HTTP client contract and retry behavior."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl

import pytest
from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_reqrep import RequestInfo
from aiointercept import CallbackResult, aiointercept
from homeassistant.exceptions import HomeAssistantError
from multidict import CIMultiDict
from yarl import URL

from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
from custom_components.airzoneclouddaikin.const import (
    API_DEVICES,
    API_EVENTS,
    API_INSTALLATION_RELATIONS,
    API_LOGIN,
    BASE_URL,
)


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


def _request_params(url: URL) -> dict[str, str]:
    """Return query parameters from an intercepted request URL."""
    return dict(parse_qsl(url.query_string))


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
async def test_login_posts_to_sign_in_and_sets_token() -> None:
    """Login should POST credentials to /users/sign_in and store the token."""
    captured: list[dict[str, Any]] = []

    def _callback(url: URL, **kwargs: Any) -> CallbackResult:
        captured.append({"url": str(url), "json": kwargs.get("json")})
        return CallbackResult(
            status=200,
            payload={"user": {"authentication_token": "tok"}},
        )

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as mocked:
            mocked.post(f"{BASE_URL}{API_LOGIN}", callback=_callback)
            api = AirzoneAPI(
                username="user@example.com",
                password="secret",
                session=session,
            )

            assert await api.login() is True

    assert api.token == "tok"
    assert api.password == "secret"
    assert captured == [
        {
            "url": f"{BASE_URL}{API_LOGIN}",
            "json": {"email": "user@example.com", "password": "secret"},
        }
    ]


@pytest.mark.asyncio
async def test_login_handles_unauthorized() -> None:
    """Unauthorized login should return False and leave the token empty."""
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
async def test_fetch_installations_uses_installation_relations_endpoint() -> None:
    """Installations should be read from /installation_relations with auth params."""
    captured: list[dict[str, str]] = []

    def _callback(url: URL, **kwargs: Any) -> CallbackResult:
        captured.append(_request_params(url))
        return CallbackResult(
            status=200,
            payload={
                "installation_relations": [
                    {"installation_id": "install-123", "id": "relation-ignored"}
                ]
            },
        )

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as mocked:
            mocked.get(
                re.compile(rf"^{re.escape(BASE_URL + API_INSTALLATION_RELATIONS)}.*$"),
                callback=_callback,
            )
            api = AirzoneAPI("user@example.com", session, token="tok")

            result = await api.fetch_installations()

    assert result == [{"installation_id": "install-123", "id": "relation-ignored"}]
    assert captured == [
        {
            "user_email": "user@example.com",
            "user_token": "tok",
            "format": "json",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_devices_uses_installation_id_query_param() -> None:
    """Device snapshots should be fetched from /devices with installation_id."""
    captured: list[dict[str, str]] = []

    def _callback(url: URL, **kwargs: Any) -> CallbackResult:
        captured.append(_request_params(url))
        return CallbackResult(status=200, payload={"devices": [{"id": "dev1"}]})

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as mocked:
            mocked.get(
                re.compile(rf"^{re.escape(BASE_URL + API_DEVICES)}.*$"),
                callback=_callback,
            )
            api = AirzoneAPI("user@example.com", session, token="tok")

            result = await api.fetch_devices("install-123")

    assert result == [{"id": "dev1"}]
    assert captured == [
        {
            "user_email": "user@example.com",
            "user_token": "tok",
            "format": "json",
            "installation_id": "install-123",
        }
    ]


@pytest.mark.asyncio
async def test_put_device_fields_uses_device_endpoint_and_payload() -> None:
    """Device writes should PUT the caller-provided payload to /devices/<id>."""
    captured: list[dict[str, Any]] = []

    def _callback(url: URL, **kwargs: Any) -> CallbackResult:
        captured.append(
            {
                "url": str(url).partition("?")[0],
                "params": _request_params(url),
                "json": kwargs.get("json"),
            }
        )
        return CallbackResult(status=200, payload={"ok": True})

    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as mocked:
            mocked.put(
                re.compile(rf"^{re.escape(BASE_URL + API_DEVICES + '/dev1')}.*$"),
                callback=_callback,
            )
            api = AirzoneAPI("user@example.com", session, token="tok")

            result = await api.put_device_fields(
                "dev1", {"device": {"scenary": "sleep"}}
            )

    assert result == {"ok": True}
    assert captured == [
        {
            "url": f"{BASE_URL}{API_DEVICES}/dev1",
            "params": {
                "user_email": "user@example.com",
                "user_token": "tok",
                "format": "json",
            },
            "json": {"device": {"scenary": "sleep"}},
        }
    ]


@pytest.mark.asyncio
async def test_send_event_treats_any_2xx_as_success() -> None:
    """The /events control endpoint should accept non-200 2xx responses."""
    captured: list[dict[str, Any]] = []

    def _callback(url: URL, **kwargs: Any) -> CallbackResult:
        captured.append(
            {
                "params": _request_params(url),
                "json": kwargs.get("json"),
            }
        )
        return CallbackResult(status=202, payload={"accepted": True})

    payload = {"event": {"cgi": "modmaquina", "device_id": "dev1"}}
    async with ClientSession() as session:
        async with aiointercept(mock_external_urls=True) as mocked:
            mocked.post(
                re.compile(rf"^{re.escape(BASE_URL + API_EVENTS)}.*$"),
                callback=_callback,
            )
            api = AirzoneAPI("user@example.com", session, token="tok")

            result = await api.send_event(payload)

    assert result == {"accepted": True}
    assert captured == [
        {
            "params": {"user_email": "user@example.com", "user_token": "tok"},
            "json": payload,
        }
    ]


@pytest.mark.asyncio
async def test_authed_request_retries_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 responses should honor Retry-After and then retry."""
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
    """5xx responses should retry with exponential backoff then raise."""
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
    """Timeouts should get one short retry."""
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
    """Debug logs should not include credentials or full secret-bearing URLs."""
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
    """Scenary writes should be nested under device."""
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
    """HTTP 423 from /events should map to the translated HA error."""
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
