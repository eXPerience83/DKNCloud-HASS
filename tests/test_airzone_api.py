"""HTTP client retry behavior tests without Home Assistant dependencies."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("aiohttp")

from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict
from yarl import URL

from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI


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


class AirzoneAPITestStub(AirzoneAPI):
    """Override timing and HTTP entrypoints for deterministic testing."""

    def __init__(self, responses: list[object]):
        super().__init__(
            username="user@example.com", session=AsyncMock(spec=ClientSession)
        )
        self._time = 0.0
        self._sleeps: list[float] = []
        self._responses = list(responses)

    @property
    def sleeps(self) -> list[float]:
        return self._sleeps

    def _now(self) -> float:  # type: ignore[override]
        return self._time

    async def _sleep(self, seconds: float) -> None:  # type: ignore[override]
        self._sleeps.append(seconds)
        self._time += seconds

    async def _request(self, *_args: object, **_kwargs: object) -> object:  # type: ignore[override]
        if not self._responses:
            raise AssertionError("Unexpected extra request")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


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
    api = AirzoneAPITestStub([retry_error, {"ok": True}])

    result = await api._authed_request_with_retries("GET", "/foo")

    assert result == {"ok": True}
    assert api.sleeps == [2.0]
    assert api._cooldown_until == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_authed_request_5xx_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    errors = [_client_response_error(status=500) for _ in range(4)]
    api = AirzoneAPITestStub(errors)

    with pytest.raises(ClientResponseError):
        await api._authed_request_with_retries("GET", "/bar")

    assert api.sleeps == pytest.approx([0.6, 1.2, 2.4])
    assert api._cooldown_until == 0.0


@pytest.mark.asyncio
async def test_timeout_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.airzone_api.random.uniform",
        lambda *_: 0.0,
    )
    api = AirzoneAPITestStub([TimeoutError(), {"ok": True}])

    result = await api._authed_request_with_retries("GET", "/baz")

    assert result == {"ok": True}
    assert api.sleeps == pytest.approx([0.4])


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
