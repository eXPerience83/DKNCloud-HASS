"""End-to-end reauthentication against a simulated token-rotating DKN backend.

These tests exercise Home Assistant's real config-entry lifecycle. The fake cloud
never uses external network access, credentials, or hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import ClientResponseError
from aiohttp.client_reqrep import RequestInfo
from homeassistant.config_entries import ConfigEntryState, SOURCE_REAUTH
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import airzone_api
from custom_components.airzoneclouddaikin.const import DOMAIN


def _unauthorized() -> ClientResponseError:
    """Make a non-sensitive fake 401 for the isolated backend."""
    url = URL("https://example.invalid/installation_relations")
    return ClientResponseError(
        RequestInfo(url, "GET", CIMultiDict(), url), history=(), status=401
    )


@pytest.mark.parametrize("initially_loaded", [False, True])
async def test_reauth_restores_complete_ha_runtime_twice(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    sample_device: dict[str, Any],
    initially_loaded: bool,
) -> None:
    """Recover initial-setup or live 401, then recover a second token expiry."""
    cloud = {"token": "old-token", "password": "old-password"}
    calls: list[tuple[str, str, str]] = []
    clients: list[FakeAPI] = []

    class FakeAPI:
        def __init__(
            self,
            username: str,
            session: Any,
            *,
            password: str | None = None,
            token: str | None = None,
        ) -> None:
            self.username = username
            self.password = password
            self.token = token
            clients.append(self)

        async def login(self) -> bool:
            if self.password != cloud["password"]:
                return False
            self.token = cloud["token"]
            return True

        def clear_password(self) -> None:
            self.password = None

        async def fetch_installations(self) -> list[dict[str, str]]:
            calls.append(("installations", self.username, self.token or ""))
            if self.token != cloud["token"]:
                raise _unauthorized()
            return [{"installation_id": "installation-A"}]

        async def fetch_devices(self, installation_id: str) -> list[dict[str, Any]]:
            calls.append(("devices", self.username, self.token or ""))
            if self.token != cloud["token"]:
                raise _unauthorized()
            assert installation_id == "installation-A"
            return [dict(sample_device)]

    monkeypatch.setattr(integration, "AirzoneAPI", FakeAPI)
    monkeypatch.setattr(airzone_api, "AirzoneAPI", FakeAPI)

    # Legacy entries may carry whitespace despite modern user setup stripping it.
    entry = dkn_config_entry_factory(
        email="  user@example.com  ",
        unique_id="user@example.com",
        options={"user_token": "old-token", "preserve-me": "value"},
    )
    entry.add_to_hass(hass)

    if not initially_loaded:
        cloud.update(token="token-1", password="password-1")
    else:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        cloud.update(token="token-1", password="password-1")
        coordinator = entry.runtime_data.coordinator
        await coordinator.async_refresh()

    await hass.async_block_till_done()

    for number in (1, 2):
        if number == 2:
            cloud.update(token="token-2", password="password-2")
            coordinator = entry.runtime_data.coordinator
            await coordinator.async_refresh()
            await hass.async_block_till_done()

        if number == 1 and not initially_loaded:
            # On first setup, an expired token never allows platforms to load.
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        flows = list(
            hass.config_entries.flow.async_progress_by_handler(
                DOMAIN,
                match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            )
        )
        assert len(flows) == 1
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"],
            user_input={CONF_PASSWORD: cloud["password"]},
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.options["user_token"] == cloud["token"]
        assert entry.options["preserve-me"] == "value"
        assert entry.data[CONF_USERNAME] == "  user@example.com  "
        assert entry.runtime_data.api.token == cloud["token"]
        assert entry.runtime_data.api.password is None
        assert entry.runtime_data.api.username == "user@example.com"
        assert entry.runtime_data.coordinator.data["dev1"]["id"] == "dev1"
        assert ("installations", "user@example.com", cloud["token"]) in calls
        assert ("devices", "user@example.com", cloud["token"]) in calls
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    assert len(clients) >= 3
    assert await hass.config_entries.async_unload(entry.entry_id)
