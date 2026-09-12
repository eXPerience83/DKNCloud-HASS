"""Tests for active live-state refresh after cloud device snapshots."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_reqrep import RequestInfo
from homeassistant.core import HomeAssistant
from multidict import CIMultiDict
from yarl import URL

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.airzone_api import AirzoneAPI
from custom_components.airzoneclouddaikin.const import (
    API_EVENTS,
    DOMAIN,
    HEADERS_EVENTS,
)
from custom_components.airzoneclouddaikin.live_refresh import (
    cancel_live_refresh,
    queue_live_refresh,
)


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


async def _await_live_refresh(hass: HomeAssistant, entry_id: str) -> None:
    """Await the current background live-refresh task when one exists."""
    task = hass.data[DOMAIN][entry_id].get("live_refresh_task")
    if task is not None:
        await task


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
        live_refresh=True,
    )


@pytest.mark.asyncio
async def test_fetch_devices_is_pure_snapshot_read() -> None:
    """Fetching a snapshot must not wait for or trigger live refresh work."""
    api = _make_api()
    devices = [{"id": "dev1"}, {"id": "dev2"}]
    api._authed_request_with_retries = AsyncMock(return_value={"devices": devices})
    api.request_device_info = AsyncMock()

    result = await api.fetch_devices("install-1")

    assert result == devices
    api.request_device_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_coordinator_returns_snapshot_while_live_refresh_is_pending(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
) -> None:
    """A pending infomaquina request must not delay a valid coordinator snapshot."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    api = _make_api()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "install-1"}])
    api.fetch_devices = AsyncMock(
        return_value=[{"id": "dev1", "name": "Unit 1", "scenary": "occupied"}]
    )

    started = asyncio.Event()
    release = asyncio.Event()

    async def _pending_refresh(_device_id: str) -> None:
        started.set()
        await release.wait()

    api.request_device_info = AsyncMock(side_effect=_pending_refresh)

    data = await integration._async_update_data(hass, entry, api)

    assert set(data) == {"dev1"}
    await asyncio.wait_for(started.wait(), timeout=1)
    worker = hass.data[DOMAIN][entry.entry_id]["live_refresh_task"]
    assert worker is not None
    assert not worker.done()
    await asyncio.wait_for(hass.async_block_till_done(), timeout=0.2)
    assert not worker.done()

    release.set()
    await worker
    api.request_device_info.assert_awaited_once_with("dev1")


@pytest.mark.asyncio
async def test_live_refresh_deduplicates_and_coalesces_inflight_device(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
) -> None:
    """Repeated polls must not queue a second refresh for an in-flight device."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    api = _make_api()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "install-1"}])
    api.fetch_devices = AsyncMock(
        return_value=[
            {"id": "dev1", "scenary": "occupied"},
            {"id": "dev1", "scenary": "occupied"},
        ]
    )

    started = asyncio.Event()
    release = asyncio.Event()

    async def _pending_refresh(_device_id: str) -> None:
        started.set()
        await release.wait()

    api.request_device_info = AsyncMock(side_effect=_pending_refresh)

    await integration._async_update_data(hass, entry, api)
    await asyncio.wait_for(started.wait(), timeout=1)
    await integration._async_update_data(hass, entry, api)

    release.set()
    await _await_live_refresh(hass, entry.entry_id)

    api.request_device_info.assert_awaited_once_with("dev1")


@pytest.mark.asyncio
async def test_live_refresh_bounds_concurrency(
    hass: HomeAssistant, dkn_config_entry_factory: Any
) -> None:
    """The background worker must not burst all queued devices at once."""
    api = _make_api()
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    bucket: dict[str, Any] = {}
    release = asyncio.Event()
    two_started = asyncio.Event()
    active = 0
    max_active = 0

    async def _pending_refresh(_device_id: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            two_started.set()
        try:
            await release.wait()
        finally:
            active -= 1

    api.request_device_info = AsyncMock(side_effect=_pending_refresh)

    queue_live_refresh(
        hass,
        entry,
        bucket,
        api,
        {"dev1", "dev2", "dev3"},
        Mock(),
    )

    await asyncio.wait_for(two_started.wait(), timeout=1)
    assert max_active == 2
    assert api.request_device_info.await_count == 2

    release.set()
    task = bucket["live_refresh_task"]
    assert task is not None
    await task
    assert api.request_device_info.await_count == 3
    assert max_active == 2


@pytest.mark.asyncio
async def test_partial_installation_fallback_refreshes_only_fresh_devices(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
) -> None:
    """Devices restored from last_data must never trigger infomaquina."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    stale_device = {"id": "stale", "name": "Stale", "scenary": "occupied"}
    bucket["has_successful_snapshot"] = True
    bucket["last_data"] = {"stale": stale_device}
    bucket["last_devices_by_inst"] = {"install-2": {"stale"}}
    bucket["device_installation_map"] = {"stale": "install-2"}

    api = _make_api()
    api.fetch_installations = AsyncMock(
        return_value=[
            {"installation_id": "install-1"},
            {"installation_id": "install-2"},
        ]
    )

    async def _fetch_devices(installation_id: str) -> list[dict[str, Any]]:
        if installation_id == "install-1":
            return [{"id": "fresh", "name": "Fresh", "scenary": "occupied"}]
        raise _client_response_error(503)

    api.fetch_devices = AsyncMock(side_effect=_fetch_devices)
    api.request_device_info = AsyncMock(return_value=None)

    data = await integration._async_update_data(hass, entry, api)
    await _await_live_refresh(hass, entry.entry_id)

    assert set(data) == {"fresh", "stale"}
    api.request_device_info.assert_awaited_once_with("fresh")


@pytest.mark.asyncio
async def test_live_refresh_ordinary_failure_keeps_snapshot(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
) -> None:
    """An ordinary background failure must not invalidate the published snapshot."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    api = _make_api()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "install-1"}])
    api.fetch_devices = AsyncMock(
        return_value=[{"id": "dev1", "name": "Unit 1", "scenary": "occupied"}]
    )
    api.request_device_info = AsyncMock(side_effect=_client_response_error(423))

    data = await integration._async_update_data(hass, entry, api)
    await _await_live_refresh(hass, entry.entry_id)

    assert set(data) == {"dev1"}
    assert hass.data[DOMAIN][entry.entry_id]["last_data"]["dev1"] is data["dev1"]


@pytest.mark.asyncio
async def test_live_refresh_401_requests_reauth_without_discarding_snapshot(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background 401 should request reauth while keeping the valid snapshot."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    def _mark_reauth(_hass: HomeAssistant, _entry: Any) -> None:
        hass.data[DOMAIN][entry.entry_id]["reauth_requested"] = True

    reauth = Mock(side_effect=_mark_reauth)
    monkeypatch.setattr(integration, "_request_reauth_once", reauth)

    api = _make_api()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "install-1"}])
    api.fetch_devices = AsyncMock(
        return_value=[{"id": "dev1", "name": "Unit 1", "scenary": "occupied"}]
    )
    api.request_device_info = AsyncMock(side_effect=_client_response_error(401))

    data = await integration._async_update_data(hass, entry, api)
    await _await_live_refresh(hass, entry.entry_id)

    assert set(data) == {"dev1"}
    reauth.assert_called_once_with(hass, entry)
    assert hass.data[DOMAIN][entry.entry_id]["last_data"]["dev1"] is data["dev1"]

    second = await integration._async_update_data(hass, entry, api)
    assert set(second) == {"dev1"}
    assert api.request_device_info.await_count == 1
    reauth.assert_called_once_with(hass, entry)

    new_entry = dkn_config_entry_factory(
        entry_id="dkn-entry-reloaded", email="new-user@example.com"
    )
    new_entry.add_to_hass(hass)
    new_api = _make_api()
    new_api.fetch_installations = AsyncMock(
        return_value=[{"installation_id": "install-1"}]
    )
    new_api.fetch_devices = AsyncMock(
        return_value=[{"id": "dev1", "name": "Unit 1", "scenary": "occupied"}]
    )
    new_api.request_device_info = AsyncMock(return_value=None)
    await integration._async_update_data(hass, new_entry, new_api)
    await _await_live_refresh(hass, new_entry.entry_id)
    new_api.request_device_info.assert_awaited_once_with("dev1")


@pytest.mark.asyncio
async def test_device_without_backend_id_is_not_live_refreshed(
    hass: HomeAssistant,
    dkn_config_entry_factory: Any,
) -> None:
    """MAC fallback identifiers are entity IDs, not infomaquina device IDs."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    api = _make_api()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "install-1"}])
    api.fetch_devices = AsyncMock(
        return_value=[{"mac": "AA:BB:CC:DD:EE:FF", "scenary": "occupied"}]
    )
    api.request_device_info = AsyncMock(return_value=None)

    data = await integration._async_update_data(hass, entry, api)

    assert set(data) == {"aa:bb:cc:dd:ee:ff"}
    api.request_device_info.assert_not_awaited()
    assert hass.data[DOMAIN][entry.entry_id].get("live_refresh_task") is None


@pytest.mark.asyncio
async def test_cancel_live_refresh_cancels_worker(
    hass: HomeAssistant, dkn_config_entry_factory: Any
) -> None:
    """Unloading an entry must be able to cancel pending background refresh work."""
    api = _make_api()
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    bucket: dict[str, Any] = {}
    started = asyncio.Event()
    release = asyncio.Event()

    async def _pending_refresh(_device_id: str) -> None:
        started.set()
        await release.wait()

    api.request_device_info = AsyncMock(side_effect=_pending_refresh)
    queue_live_refresh(hass, entry, bucket, api, {"dev1"}, Mock())

    await asyncio.wait_for(started.wait(), timeout=1)
    task = bucket["live_refresh_task"]
    assert task is not None

    cancel_live_refresh(bucket)
    with suppress(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert bucket["live_refresh_pending_ids"] == set()
    assert bucket["live_refresh_inflight_ids"] == set()
