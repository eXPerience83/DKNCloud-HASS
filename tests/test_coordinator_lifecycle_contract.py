"""Coordinator and lifecycle regression tests using synthetic DKN fixtures only."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientResponseError
from aiohttp.client_reqrep import RequestInfo
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.const import DOMAIN


def _auth_failure() -> ClientResponseError:
    request = RequestInfo(
        URL("https://example.invalid/api/devices"),
        "GET",
        CIMultiDict(),
        URL("https://example.invalid/api/devices"),
    )
    return ClientResponseError(request, history=(), status=401)


async def test_two_installations_fetch_concurrently_without_external_io(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Both installations must be in flight before either is allowed to finish."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    launched: list[str] = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    class FakeAPI:
        async def fetch_installations(self) -> list[dict[str, str]]:
            return [{"installation_id": "first"}, {"installation_id": "second"}]

        async def fetch_devices(self, installation: str) -> list[dict[str, str]]:
            launched.append(installation)
            if len(launched) == 2:
                both_started.set()
            await release.wait()
            return [
                {
                    "id": f"unit-{installation}",
                    "scenary": "occupied",
                    "name": installation,
                }
            ]

    task = asyncio.create_task(integration._async_update_data(hass, entry, FakeAPI()))
    try:
        await asyncio.wait_for(both_started.wait(), timeout=3)
        assert set(launched) == {"first", "second"}
    finally:
        release.set()
    data = await asyncio.wait_for(task, timeout=3)
    assert set(data) == {"unit-first", "unit-second"}
    assert hass.data[DOMAIN][entry.entry_id]["has_successful_snapshot"] is True


async def test_devices_without_server_id_use_normalized_mac_fallback(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Legacy records are keyed with a consistent sanitized MAC fallback."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class FakeAPI:
        async def fetch_installations(self) -> list[dict[str, str]]:
            return [{"installation_id": "site-1"}]

        async def fetch_devices(self, _site: str) -> list[dict[str, str]]:
            return [
                {"mac": " AA:BB:CC:DD ", "name": "legacy", "scenary": "occupied"},
                {"name": "unknown", "scenary": "occupied"},
            ]

    data = await integration._async_update_data(hass, entry, FakeAPI())
    assert set(data) == {"aa:bb:cc:dd"}
    assert data["aa:bb:cc:dd"]["id"] == "aa:bb:cc:dd"
    assert "unknown" not in str(data)


async def test_fetch_installations_401_requests_reauth_once(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth failure triggers HA reauth and is never silently treated as offline."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    reauth = Mock()
    monkeypatch.setattr(integration, "_request_reauth_once", reauth)

    class FakeAPI:
        async def fetch_installations(self) -> None:
            raise _auth_failure()

    with pytest.raises(UpdateFailed, match="401"):
        await integration._async_update_data(hass, entry, FakeAPI())
    reauth.assert_called_once_with(hass, entry)


async def test_fetch_devices_cancellation_is_not_swallowed(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Cancellation of one installation aborts a refresh, not a partial state."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class FakeAPI:
        async def fetch_installations(self) -> list[dict[str, str]]:
            return [{"installation_id": "site-1"}]

        async def fetch_devices(self, _site: str) -> None:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await integration._async_update_data(hass, entry, FakeAPI())


async def test_legacy_entry_migration_normalizes_unique_id_only(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Migration preserves user-facing data/options while normalizing identity."""
    entry = dkn_config_entry_factory(
        email=" User@Example.Com ",
        unique_id="legacy-id",
        version=1,
        options={"hidden_setting": "keep"},
    )
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    original_options = dict(entry.options)
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.unique_id == "user@example.com"
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options


async def test_legacy_entry_without_username_does_not_invent_identity(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Migration can upgrade schema without guessing a missing account email."""
    entry = dkn_config_entry_factory(
        email="",
        unique_id="legacy-unique",
        version=1,
        data={CONF_USERNAME: ""},
    )
    entry.add_to_hass(hass)
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 2
    assert entry.unique_id == "legacy-unique"
