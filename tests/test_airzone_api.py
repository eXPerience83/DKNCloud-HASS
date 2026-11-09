"""Tests for the Airzone HTTP client contract and retry behavior."""

from __future__ import annotations

import traceback
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientOSError, ClientResponseError, ClientSession, web
from aiohttp.client_reqrep import RequestInfo
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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


async def _serve_api(aiohttp_server, monkeypatch, method: str, path: str, handler):
    """Route a real aiohttp client to an isolated loopback backend.

    This exercises real loopback HTTP without relying on DNS interception.
    """
    app = web.Application()
    app.router.add_route(method, path, handler)
    server = await aiohttp_server(app)
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.BASE_URL",
        str(server.make_url("/")).rstrip("/"),
    )


@pytest.mark.asyncio
async def test_login_posts_to_sign_in_and_sets_token(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login should POST credentials to /users/sign_in and store the token."""
    captured: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(
            {"url": f"{BASE_URL}{request.path}", "json": await request.json()}
        )
        return web.json_response({"user": {"authentication_token": "tok"}})

    await _serve_api(aiohttp_server, monkeypatch, "POST", API_LOGIN, handler)
    async with ClientSession() as session:
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
async def test_http_401_redacts_token_from_exception(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No propagated HTTP error may disclose query-string credentials."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=401)

    await _serve_api(
        aiohttp_server, monkeypatch, "GET", API_INSTALLATION_RELATIONS, handler
    )
    async with ClientSession() as session:
        api = AirzoneAPI("secret-email@example.com", session, token="secret-token")
        with pytest.raises(ClientResponseError) as exc_info:
            await api.fetch_installations()

    assert exc_info.value.status == 401
    assert "secret-email@example.com" not in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)
    assert "secret-email@example.com" not in repr(exc_info.value)
    assert "secret-token" not in repr(exc_info.value)
    assert "user_token" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "method", "path"),
    [
        (401, "GET", API_INSTALLATION_RELATIONS),
        (429, "GET", API_INSTALLATION_RELATIONS),
        (503, "PUT", "/devices/secret-device-id"),
        (401, "POST", API_EVENTS),
    ],
)
async def test_http_errors_have_no_secret_exception_chain(
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    method: str,
    path: str,
) -> None:
    """HTTP errors keep statuses but not a hidden raw credential-bearing cause."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=status, headers={"Retry-After": "7"})

    await _serve_api(aiohttp_server, monkeypatch, method, path, handler)
    async with ClientSession() as session:
        api = AirzoneAPI("hidden@example.com", session, token="hidden-token")
        with pytest.raises(ClientResponseError) as captured:
            await api._request(
                method,
                path,
                params={"user_email": api._username, "user_token": api.token},
            )

    error = captured.value
    assert error.status == status
    assert error.headers["Retry-After"] == "7"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "hidden@example.com" not in str(error)
    assert "hidden-token" not in repr(error)
    assert "secret-device-id" not in str(error)


@pytest.mark.parametrize("operation", ["event", "device"])
async def test_write_401_invokes_native_reauth_callback(
    operation: str,
) -> None:
    """Write auth failures trigger native reauth immediately without GET polling."""
    api = AirzoneAPI(
        "user@example.com",
        AsyncMock(spec=ClientSession),
        token="expired-token",
    )
    callback = Mock()
    api.on_write_auth_failure = callback
    api._request = AsyncMock(side_effect=_client_response_error(401))

    with pytest.raises(ConfigEntryAuthFailed):
        if operation == "event":
            await api.send_event({"event": {"option": "P1", "value": "1"}})
        else:
            await api.put_device_fields("fake-id", {"sleep_time": 1})

    callback.assert_called_once_with()
    api._request.assert_awaited_once()


async def test_read_401_does_not_duplicate_coordinator_reauth() -> None:
    """The coordinator, not the HTTP client, initiates reauth on read failure."""
    api = AirzoneAPI(
        "user@example.com",
        AsyncMock(spec=ClientSession),
        token="expired-token",
    )
    callback = Mock()
    api.on_write_auth_failure = callback
    api._request = AsyncMock(side_effect=_client_response_error(401))

    with pytest.raises(ClientResponseError, match="401"):
        await api.fetch_installations()

    callback.assert_not_called()


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
async def test_fetch_installations_uses_installation_relations_endpoint(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installations should be read from /installation_relations with auth params."""
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.query))
        return web.json_response(
            {
                "installation_relations": [
                    {"installation_id": "install-123", "id": "relation-ignored"}
                ]
            }
        )

    await _serve_api(
        aiohttp_server, monkeypatch, "GET", API_INSTALLATION_RELATIONS, handler
    )
    async with ClientSession() as session:
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
async def test_fetch_devices_uses_installation_id_query_param(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device snapshots should be fetched from /devices with installation_id."""
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.query))
        return web.json_response({"devices": [{"id": "dev1"}]})

    await _serve_api(aiohttp_server, monkeypatch, "GET", API_DEVICES, handler)
    async with ClientSession() as session:
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
async def test_put_device_fields_uses_device_endpoint_and_payload(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Device writes should PUT the caller-provided payload to /devices/<id>."""
    captured: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(
            {
                "url": f"{BASE_URL}{request.path}",
                "params": dict(request.query),
                "json": await request.json(),
            }
        )
        return web.json_response({"ok": True})

    await _serve_api(aiohttp_server, monkeypatch, "PUT", f"{API_DEVICES}/dev1", handler)
    async with ClientSession() as session:
        api = AirzoneAPI("user@example.com", session, token="tok")
        result = await api.put_device_fields("dev1", {"device": {"scenary": "sleep"}})

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
async def test_send_event_treats_any_2xx_as_success(
    socket_enabled: None, aiohttp_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /events control endpoint should accept non-200 2xx responses."""
    captured: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append({"params": dict(request.query), "json": await request.json()})
        return web.json_response({"accepted": True}, status=202)

    payload = {"event": {"cgi": "modmaquina", "device_id": "dev1"}}
    await _serve_api(aiohttp_server, monkeypatch, "POST", API_EVENTS, handler)
    async with ClientSession() as session:
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


@pytest.mark.parametrize("status", [403, 500])
@pytest.mark.asyncio
async def test_send_event_maps_unexpected_status_to_translated_error(
    status: int,
) -> None:
    """Other /events errors expose a localizable status without raw response data."""
    api = AirzoneAPI(
        username="user@example.com",
        password="secret",
        session=AsyncMock(spec_set=ClientSession),
    )
    api._authed_request_with_retries = AsyncMock(
        side_effect=_client_response_error(status=status)
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await api.send_event({"event": "payload"})

    error = exc_info.value
    assert error.translation_domain == "airzoneclouddaikin"
    assert error.translation_key == "event_failed"
    assert error.translation_placeholders == {"status": str(status)}
    assert error.__suppress_context__ is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_nonretryable_errors_are_propagated_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Never retry client or 401 errors, including authentication failures."""
    api, sleeps = _make_api(monkeypatch, [_client_response_error(status=status)])

    with pytest.raises(ClientResponseError) as error:
        await api._authed_request_with_retries("GET", "/devices?secret=redacted")

    assert error.value.status == status
    assert api._request.await_count == 1
    assert sleeps == []


async def test_timeout_retries_only_once_when_second_call_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated timeouts are finite and do not exhaust the 5xx retry budget."""
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    api, sleeps = _make_api(monkeypatch, [TimeoutError(), TimeoutError()])
    with pytest.raises(TimeoutError):
        await api._authed_request_with_retries("GET", "/devices")
    assert api._request.await_count == 2
    assert sleeps == pytest.approx([0.4])


async def test_429_retry_after_is_honored_without_truncating_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server Retry-After controls both sleep and shared cooldown."""
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    api, sleeps = _make_api(
        monkeypatch, [_client_response_error(429, {"Retry-After": "100"}), {"ok": 1}]
    )
    assert await api._authed_request_with_retries("GET", "/devices") == {"ok": 1}
    assert sleeps == [100.0]
    assert api._cooldown_until == pytest.approx(100.0)


async def test_request_cancellation_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation propagates without retry or sleeping."""
    import asyncio

    api, sleeps = _make_api(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await api._authed_request_with_retries("GET", "/devices")
    assert api._request.await_count == 1
    assert sleeps == []


async def test_real_client_204_reply_returns_empty_body(
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP response contract accepts empty successful bodies."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    await _serve_api(aiohttp_server, monkeypatch, "DELETE", "/devices/dev1", handler)
    async with ClientSession() as session:
        api = AirzoneAPI("user@example.com", session, token="tok")
        assert await api._request("DELETE", "/devices/dev1") == ""


@pytest.mark.parametrize(
    "relations",
    [
        False,
        {},
        "backend error",
        1,
        [False],
        [{"installation_id": "site-A"}, None],
    ],
)
async def test_installations_rejects_non_list_envelope(monkeypatch, relations):
    """Malformed HTTP 200 envelopes must not validate a renewed token."""
    api, _ = _make_api(monkeypatch, [{"installation_relations": relations}])
    api._token = "synthetic-token"
    assert await api.fetch_installations() is None


async def test_real_aiohttp_connection_timeout_redacts_url(monkeypatch):
    """Exercise aiohttp's URL-bearing connection timeout without network I/O."""
    from aiohttp import TCPConnector

    async def timeout_connect(*args, **kwargs):
        raise TimeoutError("synthetic transport failure")

    monkeypatch.setattr(TCPConnector, "connect", timeout_connect)
    async with ClientSession() as session:
        api = AirzoneAPI("hidden@example.com", session, token="hidden-token")
        with pytest.raises(TimeoutError) as captured:
            await api._request(
                "GET",
                API_INSTALLATION_RELATIONS,
                params={"user_email": api._username, "user_token": api.token},
            )
    error = captured.value
    assert "hidden-token" not in str(error)
    assert "hidden@example.com" not in repr(error)
    assert error.__context__ is None
    assert error.__cause__ is None


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", API_EVENTS),
        ("PUT", f"{API_DEVICES}/private-device-id"),
    ],
)
async def test_real_aiohttp_broken_body_write_redacts_transport_error(
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """A transport failure while sending JSON must not retain the full URL."""
    from aiohttp.payload import JsonPayload

    async def handler(request: web.Request) -> web.Response:
        await request.read()
        return web.json_response({"ok": True})

    await _serve_api(aiohttp_server, monkeypatch, method, path, handler)

    async def fail_body_write(self, writer, content_length) -> None:
        raise OSError(32, "synthetic broken pipe")

    monkeypatch.setattr(JsonPayload, "write_with_length", fail_body_write)
    async with ClientSession() as session:
        api = AirzoneAPI("private@example.com", session, token="private-token")
        callback = Mock()
        api.on_write_auth_failure = callback
        with pytest.raises(ClientOSError) as captured:
            if method == "POST":
                await api.send_event({"event": {"option": "P1", "value": "1"}})
            else:
                await api.put_device_fields(path.rsplit("/", 1)[-1], {"sleep_time": 5})

    error = captured.value
    assert error.errno == 32
    assert error.__context__ is None
    assert error.__cause__ is None
    representation = (
        str(error) + repr(error) + "".join(traceback.format_exception(error))
    )
    for secret in ("private@example.com", "private-token", "private-device-id"):
        assert secret not in representation
    callback.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [{}, {"devices": None}, {"devices": False}, {"devices": {}}, {"devices": [False]}],
)
async def test_devices_rejects_malformed_http_200(
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    """An HTTP 200 without a list of device objects is not an empty account."""

    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(payload)

    await _serve_api(aiohttp_server, monkeypatch, "GET", API_DEVICES, handler)
    async with ClientSession() as session:
        api = AirzoneAPI("user@example.com", session, token="token")
        assert await api.fetch_devices("site-A") is None


@pytest.mark.parametrize("payload", [[], {"devices": []}])
async def test_devices_accepts_valid_empty_http_list(
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    payload: list[Any] | dict[str, Any],
) -> None:
    """A valid empty device list must still clear an installation snapshot."""

    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(payload)

    await _serve_api(aiohttp_server, monkeypatch, "GET", API_DEVICES, handler)
    async with ClientSession() as session:
        api = AirzoneAPI("user@example.com", session, token="token")
        assert await api.fetch_devices("site-A") == []


async def test_shared_cooldown_preserves_long_retry_after(monkeypatch):
    """Other callers must observe the complete server-directed pause."""
    api, sleeps = _make_api(
        monkeypatch, [_client_response_error(429, {"Retry-After": "120"}), {}]
    )
    await api._authed_request_with_retries("GET", API_DEVICES)
    assert sleeps[0] == 120
    assert api._cooldown_until == 120


async def test_retry_after_http_date(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    deadline = datetime.now(UTC) + timedelta(seconds=120)
    api, sleeps = _make_api(
        monkeypatch,
        [
            _client_response_error(
                429, {"Retry-After": format_datetime(deadline, usegmt=True)}
            ),
            {},
        ],
    )
    await api._authed_request_with_retries("GET", API_DEVICES)
    assert 118 <= sleeps[0] <= 120
    assert api._cooldown_until == sleeps[0]


async def test_information_timeout_is_not_retried(monkeypatch):
    api, sleeps = _make_api(monkeypatch, [TimeoutError()])
    with pytest.raises(TimeoutError):
        await api.request_information("fake-device")
    assert api._request.await_count == 1
    assert sleeps == []


async def test_information_429_sets_shared_pause_without_retry(monkeypatch):
    api, sleeps = _make_api(
        monkeypatch, [_client_response_error(429, {"Retry-After": "120"})]
    )
    with pytest.raises(ClientResponseError):
        await api.request_information("fake-device")
    assert api._request.await_count == 1
    assert api._cooldown_until == 120
    assert sleeps == []


async def test_second_caller_rechecks_extended_shared_cooldown(monkeypatch):
    api, sleeps = _make_api(monkeypatch, [{}])
    api._cooldown_until = 20
    now = [0.0]
    monkeypatch.setattr(api, "_now", lambda: now[0])

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        if len(sleeps) == 1:
            api._cooldown_until = 50

    monkeypatch.setattr(api, "_sleep", sleep)
    await api._authed_request_with_retries("GET", API_DEVICES)
    assert sleeps == [20, 30]
    assert now[0] == 50


async def test_delayed_429_sets_pause_from_response_time(monkeypatch):
    api, _ = _make_api(monkeypatch, [])
    now = [0.0]
    monkeypatch.setattr(api, "_now", lambda: now[0])

    async def request(*args, **kwargs):
        now[0] = 35
        raise _client_response_error(429, {"Retry-After": "120"})

    monkeypatch.setattr(api, "_request", request)
    with pytest.raises(ClientResponseError):
        await api.request_information("fake")
    assert api._cooldown_until == 155


async def test_authorization_uses_form_body_without_credential_query(
    socket_enabled, aiohttp_server, monkeypatch
):
    async def handler(request):
        assert not request.query
        assert request.content_type == "application/x-www-form-urlencoded"
        assert dict(await request.post()) == {
            "user_email": "synthetic@example.invalid",
            "user_token": "synthetic-token",
            "socket_id": "1.2",
            "channel_name": "private-fake",
        }
        return web.json_response({"auth": "synthetic-signature"})

    await _serve_api(aiohttp_server, monkeypatch, "POST", "/pusher/auth", handler)
    async with ClientSession() as session:
        api = AirzoneAPI("synthetic@example.invalid", session, token="synthetic-token")
        assert (
            await api.authorize_channel("1.2", "private-fake") == "synthetic-signature"
        )


async def test_second_caller_waits_after_nonretrying_429(monkeypatch):
    api, sleeps = _make_api(
        monkeypatch, [_client_response_error(429, {"Retry-After": "120"}), {}]
    )
    with pytest.raises(ClientResponseError):
        await api.request_information("fake")
    assert api._request.await_count == 1
    await api._authed_request_with_retries("GET", API_DEVICES)
    assert sleeps == [120]
    assert api._request.await_count == 2


@pytest.mark.parametrize(
    "operation", ["information", "devices", "installations", "authorize", "cleanup"]
)
async def test_auth_fence_after_cooldown_prevents_wire_send(operation):
    import asyncio

    api = AirzoneAPI("synthetic@example.invalid", Mock(), token="synthetic")
    entered, release = asyncio.Event(), asyncio.Event()
    active = [True]
    api.request_allowed = lambda: active[0]
    api._request = AsyncMock(return_value={})
    api._cooldown_until = 10
    api._now = lambda: 0

    async def sleep(_):
        entered.set()
        await release.wait()
        api._cooldown_until = 0

    api._sleep = sleep
    operations = {
        "information": lambda: api.request_information("synthetic"),
        "devices": lambda: api.fetch_devices("synthetic"),
        "installations": api.fetch_installations,
        "authorize": lambda: api.authorize_channel("1.2", "private-synthetic"),
        "cleanup": lambda: api.async_set_scenary("synthetic", "occupied"),
    }
    task = asyncio.create_task(operations[operation]())
    await entered.wait()
    active[0] = False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    api._request.assert_not_awaited()
