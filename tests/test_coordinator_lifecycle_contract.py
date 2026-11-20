"""Coordinator and lifecycle regression tests using synthetic DKN fixtures only."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientResponseError
from aiohttp.client_reqrep import RequestInfo
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from multidict import CIMultiDict
from pytest_homeassistant.common import MockConfigEntry
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

        async def fetch_devices(
            self, installation: str, **_kwargs
        ) -> list[dict[str, str]]:
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

        async def fetch_devices(self, _site: str, **_kwargs) -> list[dict[str, str]]:
            return [
                {"mac": " AA:BB:CC:DD ", "name": "legacy", "scenary": "occupied"},
                {"name": "unknown", "scenary": "occupied"},
            ]

    data = await integration._async_update_data(hass, entry, FakeAPI())
    assert set(data) == {"aa:bb:cc:dd"}
    assert data["aa:bb:cc:dd"]["id"] == "aa:bb:cc:dd"
    assert "unknown" not in str(data)


async def test_fetch_installations_401_requests_native_reauth(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """A 401 is an authentication failure, not a generic update failure."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class FakeAPI:
        async def fetch_installations(self) -> None:
            raise _auth_failure()

    with pytest.raises(ConfigEntryAuthFailed, match="401"):
        await integration._async_update_data(hass, entry, FakeAPI())


async def test_fetch_devices_401_uses_native_auth_failure_even_if_other_succeeds(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """One installation rejecting the token invalidates the full snapshot."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)

    class FakeAPI:
        async def fetch_installations(self) -> list[dict[str, str]]:
            return [{"installation_id": "bad"}, {"installation_id": "good"}]

        async def fetch_devices(
            self, installation: str, **_kwargs
        ) -> list[dict[str, str]]:
            if installation == "bad":
                raise _auth_failure()
            return [{"id": "dev-1", "scenary": "occupied"}]

    with pytest.raises(ConfigEntryAuthFailed, match="401"):
        await integration._async_update_data(hass, entry, FakeAPI())


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

        async def fetch_devices(self, _site: str, **_kwargs) -> None:
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
    original_options.pop("scan_interval", None)
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 3
    assert entry.unique_id == "user@example.com"
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options


@pytest.mark.parametrize("duplicate", [False, True])
async def test_migration_logs_never_expose_normalized_account_email(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    caplog: pytest.LogCaptureFixture,
    duplicate: bool,
) -> None:
    """Both successful and duplicate migration logs omit the account identity."""
    if duplicate:
        existing = dkn_config_entry_factory(
            entry_id="already-configured",
            email="account@example.com",
            unique_id="account@example.com",
        )
        existing.add_to_hass(hass)

    entry = dkn_config_entry_factory(
        email=" Account@Example.Com ",
        unique_id="legacy-id",
        version=1,
    )
    entry.add_to_hass(hass)
    with caplog.at_level("INFO", logger=integration.__name__):
        assert await integration.async_migrate_entry(hass, entry)
    assert entry.unique_id == ("legacy-id" if duplicate else "account@example.com")
    assert "account@example.com" not in caplog.text.lower()
    assert "account@ex" not in caplog.text.lower()


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
    assert entry.version == 3
    assert entry.unique_id == "legacy-unique"


async def test_unload_only_cancels_timers_and_removes_its_own_runtime(
    hass, dkn_config_entry_factory, monkeypatch
) -> None:
    """A successor may appear while the old manager is finishing shutdown."""
    from unittest.mock import AsyncMock, Mock

    entry = dkn_config_entry_factory()
    closing = asyncio.Event()
    finish_close = asyncio.Event()

    async def close():
        closing.set()
        await finish_close.wait()

    old = {
        "coordinator": object(),
        "sync_manager": Mock(close=close),
        "pending_refresh": Mock(),
    }
    successor = {"coordinator": object(), "pending_refresh": Mock()}
    timer = old["pending_refresh"]
    hass.data[DOMAIN] = {entry.entry_id: old}
    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    )

    task = asyncio.create_task(integration.async_unload_entry(hass, entry))
    try:
        async with asyncio.timeout(5):
            await closing.wait()
            hass.data[DOMAIN][entry.entry_id] = successor
            finish_close.set()
            assert await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    timer.assert_called_once_with()
    assert old["closing"]
    assert hass.data[DOMAIN][entry.entry_id] is successor
    successor["pending_refresh"].assert_not_called()


@pytest.mark.parametrize("phase", ["installations", "devices"])
async def test_retired_polling_refresh_cannot_continue_or_publish(
    hass, dkn_config_entry_factory, phase
) -> None:
    """A REST readback may retire while awaiting either part of the snapshot."""
    from unittest.mock import AsyncMock, Mock

    entry = dkn_config_entry_factory()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch_installations():
        if phase == "installations":
            started.set()
            await release.wait()
        return [{"installation_id": "site"}]

    async def fetch_devices(_site, **_kwargs):
        if phase == "devices":
            started.set()
            await release.wait()
        return [{"id": "device", "scenary": "occupied", "power": "1"}]

    api = Mock(
        fetch_installations=AsyncMock(side_effect=fetch_installations),
        fetch_devices=AsyncMock(side_effect=fetch_devices),
    )
    original = {"coordinator": object()}
    successor = {"coordinator": object()}
    hass.data[DOMAIN] = {entry.entry_id: original}
    task = asyncio.create_task(integration._async_update_data(hass, entry, api))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            hass.data[DOMAIN][entry.entry_id] = successor
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert hass.data[DOMAIN][entry.entry_id] is successor
    assert set(successor) == {"coordinator"}
    assert "last_data" not in original
    api.fetch_installations.assert_awaited_once_with()
    assert api.fetch_devices.await_count == (phase == "devices")


async def test_rest_polling_clears_only_the_uncertainty_seen_at_get_start(hass):
    """A retained cached snapshot is insufficient, even if it includes power."""
    from types import SimpleNamespace

    from custom_components.airzoneclouddaikin import _async_update_data

    entry = MockConfigEntry(domain=DOMAIN)
    uncertain = {}
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    bucket["uncertain_power"] = uncertain
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch(_site, *, on_start):
        nonlocal calls
        calls += 1
        on_start()
        if calls == 1:
            entered.set()
            await release.wait()
        if calls == 2:
            return None
        return [{"id": "unit", "power": "0"}]

    api = SimpleNamespace(
        fetch_installations=AsyncMock(return_value=[{"installation_id": "site"}]),
        fetch_devices=fetch,
    )
    async with asyncio.timeout(3):
        old = asyncio.create_task(_async_update_data(hass, entry, api))
        await entered.wait()
        marker = uncertain["unit"] = object()
        release.set()
        await old
        assert uncertain == {"unit": marker}
        cached = await _async_update_data(hass, entry, api)
        assert cached["unit"]["power"] == "0"
        assert uncertain == {"unit": marker}
        await _async_update_data(hass, entry, api)
        assert not uncertain
    assert calls == 3
