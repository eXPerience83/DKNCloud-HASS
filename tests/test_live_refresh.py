"""Tests for active live-state refresh after cloud device snapshots."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest
from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict
from yarl import URL

from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
from custom_components.airzoneclouddaikin.const import API_EVENTS, HEADERS_EVENTS


def _client_response_error(status: int) -> ClientResponseError:
    """Create a ClientResponseError with minimal request context."""
    request_info = RequestInfo(
        URL("https://example.com"),
        "POST",
        CIMultiDict(),
        URL("https://example.com"),
    )
    return ClientResponseError(
        request_info,
        history=(),
        status=status,
        message="",
    )


def _make_api() -> AirzoneAPI:
    """Create an authenticated API with a mocked aiohttp session."""
    return AirzoneAPI(
        username="user@example.com",
        session=AsyncMock(spec=ClientSession),
        token="tok",
    )


@pytest.mark.asyncio
async def test_request_device_info_uses_infomaquina_payload() -> None:
    """Live refresh should use the exact information-request event contract."""
    api = _make_api()
    api._authed_request_with_retries = AsyncMock(return_value={"accepted": True})

    result = await api.request_device_info("dev1")

    assert result == {"accepted": True}
    api._authed_request_with_retries.assert_awaited_once_with(
        "POST",
        API_EVENTS,
        params={"user_email": "user@example.com", "user_token": "tok"},
        json={
            "event": {
                "cgi": "infomaquina",
                "option": "",
                "value": "",
                "device_id": "dev1",
            }
        },
        extra_headers=HEADERS_EVENTS,
    )


@pytest.mark.asyncio
async def test_fetch_devices_requests_live_refresh_for_snapshot_devices() -> None:
    """A successful snapshot should request fresh information for each device."""
    api = _make_api()
    devices = [{"id": "dev1"}, {"id": "dev2"}]
    api._authed_request_with_retries = AsyncMock(return_value={"devices": devices})
    api.request_device_info = AsyncMock(return_value=None)

    result = await api.fetch_devices("install-1")

    assert result == devices
    assert api.request_device_info.await_args_list == [call("dev1"), call("dev2")]


@pytest.mark.asyncio
async def test_fetch_devices_keeps_valid_snapshot_when_live_refresh_fails() -> None:
    """Ordinary live-refresh failures must not discard a valid device snapshot."""
    api = _make_api()
    devices = [{"id": "dev1"}]
    api._authed_request_with_retries = AsyncMock(return_value=devices)
    api.request_device_info = AsyncMock(side_effect=_client_response_error(423))

    result = await api.fetch_devices("install-1")

    assert result == devices
    api.request_device_info.assert_awaited_once_with("dev1")


@pytest.mark.asyncio
async def test_fetch_devices_propagates_live_refresh_401() -> None:
    """Authentication failures from live refresh should reach coordinator reauth."""
    api = _make_api()
    devices = [{"id": "dev1"}]
    api._authed_request_with_retries = AsyncMock(return_value=devices)
    api.request_device_info = AsyncMock(side_effect=_client_response_error(401))

    with pytest.raises(ClientResponseError) as exc_info:
        await api.fetch_devices("install-1")

    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_failed_snapshot_does_not_request_live_refresh() -> None:
    """Failed installation fetches must not refresh devices from stale fallback data."""
    api = _make_api()
    api._authed_request_with_retries = AsyncMock(
        side_effect=_client_response_error(503)
    )
    api.request_device_info = AsyncMock()

    with pytest.raises(ClientResponseError) as exc_info:
        await api.fetch_devices("install-1")

    assert exc_info.value.status == 503
    api.request_device_info.assert_not_awaited()
