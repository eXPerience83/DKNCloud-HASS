"""End-to-end reauthentication against a simulated token-rotating DKN backend.

These tests exercise Home Assistant's real config-entry lifecycle. The fake cloud
never uses external network access, credentials, or hardware.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import ClientResponseError, web
from aiohttp.client_reqrep import RequestInfo
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry
from yarl import URL

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin import airzone_api
from custom_components.airzoneclouddaikin.const import (
    API_DEVICES,
    API_EVENTS,
    API_INSTALLATION_RELATIONS,
    API_LOGIN,
    DOMAIN,
)


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


@pytest.mark.parametrize("initially_loaded", [False, True])
async def test_real_http_reauth_recovers_ha_and_preserves_entity_registry(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    sample_device: dict[str, Any],
    initially_loaded: bool,
) -> None:
    """Real aiohttp login/GET and HA config-entry reload survive two token rotations."""
    cloud = {"token": "token-0", "password": "password-0"}
    reads: list[tuple[str, str, str]] = []

    async def login(request: web.Request) -> web.Response:
        payload = await request.json()
        if payload.get("email") != "user@example.com":
            return web.json_response({"error": "account"}, status=401)
        if payload.get("password") != cloud["password"]:
            return web.json_response({"error": "login"}, status=401)
        return web.json_response({"user": {"authentication_token": cloud["token"]}})

    async def installations(request: web.Request) -> web.Response:
        reads.append(
            (
                "installations",
                request.query.get("user_email", ""),
                request.query.get("user_token", ""),
            )
        )
        if request.query.get("user_token") != cloud["token"]:
            return web.json_response({"error": "expired"}, status=401)
        return web.json_response(
            {"installation_relations": [{"installation_id": "site-A"}]}
        )

    async def devices(request: web.Request) -> web.Response:
        reads.append(
            (
                "devices",
                request.query.get("user_email", ""),
                request.query.get("user_token", ""),
            )
        )
        if request.query.get("user_token") != cloud["token"]:
            return web.json_response({"error": "expired"}, status=401)
        return web.json_response({"devices": [dict(sample_device)]})

    app = web.Application()
    app.router.add_post(API_LOGIN, login)
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    app.router.add_get(API_DEVICES, devices)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(
        email="  user@example.com  ",
        unique_id="user@example.com",
        options={"user_token": "token-0", "keep-option": "safe"},
    )
    entry.add_to_hass(hass)

    registry_ids: set[str] | None = None
    if initially_loaded:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Capture identity before the first token expiry, not after recovery.
        registry_ids = {
            entity.unique_id
            for entity in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        assert registry_ids

    cloud.update(token="token-1", password="password-1")
    if initially_loaded:
        await entry.runtime_data.coordinator.async_refresh()
    else:
        await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for generation in (1, 2):
        if generation == 2:
            cloud.update(token="token-2", password="password-2")
            await entry.runtime_data.coordinator.async_refresh()
            await hass.async_block_till_done()

        flows = list(
            hass.config_entries.flow.async_progress_by_handler(
                DOMAIN,
                match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            )
        )
        assert len(flows) == 1
        completed = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], user_input={CONF_PASSWORD: cloud["password"]}
        )
        assert completed["type"] is FlowResultType.ABORT
        assert completed["reason"] == "reauth_successful"
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.options["user_token"] == cloud["token"]
        assert entry.options["keep-option"] == "safe"
        assert entry.runtime_data.api.token == cloud["token"]
        assert entry.runtime_data.api.password is None
        assert entry.runtime_data.coordinator.data["dev1"]["id"] == "dev1"
        assert ("installations", "user@example.com", cloud["token"]) in reads
        assert ("devices", "user@example.com", cloud["token"]) in reads

        current_ids = {
            entity.unique_id
            for entity in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        }
        assert current_ids
        if registry_ids is None:
            registry_ids = current_ids
        else:
            assert current_ids == registry_ids

    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_real_http_write_only_401_starts_native_reauth(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    sample_device: dict[str, Any],
) -> None:
    """Command 401 starts HA reauth even while the read coordinator is healthy."""
    cloud = {"login_token": "new-token", "write_token": "new-token"}

    async def login(_request: web.Request) -> web.Response:
        return web.json_response(
            {"user": {"authentication_token": cloud["login_token"]}}
        )

    async def installations(_request: web.Request) -> web.Response:
        return web.json_response(
            {"installation_relations": [{"installation_id": "site-A"}]}
        )

    async def devices(_request: web.Request) -> web.Response:
        return web.json_response({"devices": [dict(sample_device)]})

    async def write(request: web.Request) -> web.Response:
        if request.query.get("user_token") != cloud["write_token"]:
            return web.Response(status=401)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post(API_LOGIN, login)
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    app.router.add_get(API_DEVICES, devices)
    app.router.add_post(API_EVENTS, write)
    app.router.add_put(f"{API_DEVICES}/{{device_id}}", write)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(token="old-token")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from homeassistant.exceptions import ConfigEntryAuthFailed

    with pytest.raises(ConfigEntryAuthFailed):
        await entry.runtime_data.api.put_device_fields("dev1", {"sleep_time": 5})
    with pytest.raises(ConfigEntryAuthFailed):
        await entry.runtime_data.api.send_event(
            {"event": {"cgi": "modmaquina", "option": "P1", "value": "1"}}
        )
    await hass.async_block_till_done()
    flows = list(
        hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        )
    )
    assert len(flows) == 1
    confirmed = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], user_input={CONF_PASSWORD: "password"}
    )
    assert confirmed["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.runtime_data.api.token == "new-token"
    assert (
        await entry.runtime_data.api.put_device_fields("dev1", {"sleep_time": 5}) == ""
    )
    retired_api = entry.runtime_data.api
    assert await hass.config_entries.async_unload(entry.entry_id)
    # A late 401 from a retired client must not reopen authentication.
    cloud["write_token"] = "another-token"
    with pytest.raises(ConfigEntryAuthFailed):
        await retired_api.put_device_fields("dev1", {"sleep_time": 5})
    await hass.async_block_till_done()
    assert not list(hass.config_entries.flow.async_progress_by_handler(DOMAIN))


@pytest.mark.parametrize(
    "bad_relations",
    [[False], [{"installation_id": "site-A"}, None]],
    ids=["boolean-item", "mixed-valid-and-null"],
)
async def test_real_http_reauth_rejects_malformed_relation_items(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    bad_relations: list[Any],
) -> None:
    """A successful login cannot persist a token for a malformed HTTP 200 list."""
    cloud: dict[str, Any] = {"relations": bad_relations}

    async def login(_request: web.Request) -> web.Response:
        return web.json_response({"user": {"authentication_token": "new-token"}})

    async def installations(_request: web.Request) -> web.Response:
        return web.json_response({"installation_relations": cloud["relations"]})

    app = web.Application()
    app.router.add_post(API_LOGIN, login)
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(token="old-token", options={"keep-option": "safe"})
    entry.add_to_hass(hass)
    flow = await entry.start_reauth_flow(hass)

    rejected = await hass.config_entries.flow.async_configure(
        flow["flow_id"], user_input={CONF_PASSWORD: "synthetic-password"}
    )
    assert rejected["type"] is FlowResultType.FORM
    assert rejected["errors"] == {"base": "cannot_connect"}
    assert entry.options["user_token"] == "old-token"
    assert entry.options["keep-option"] == "safe"

    # The same form can recover as soon as the backend returns a valid empty list.
    cloud["relations"] = []
    accepted = await hass.config_entries.flow.async_configure(
        flow["flow_id"], user_input={CONF_PASSWORD: "synthetic-password"}
    )
    assert accepted["type"] is FlowResultType.ABORT
    assert accepted["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.options["user_token"] == "new-token"
    assert entry.options["keep-option"] == "safe"
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_malformed_relations_during_refresh_preserves_last_good_snapshot(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    sample_device: dict[str, Any],
) -> None:
    """A malformed HTTP 200 is not an authoritative account with zero devices."""
    cloud: dict[str, Any] = {
        "relations": [{"installation_id": "site-A"}],
    }

    async def installations(_request: web.Request) -> web.Response:
        return web.json_response({"installation_relations": cloud["relations"]})

    async def devices(_request: web.Request) -> web.Response:
        return web.json_response({"devices": [dict(sample_device)]})

    app = web.Application()
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    app.router.add_get(API_DEVICES, devices)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(token="valid-token")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success
    assert "dev1" in coordinator.data
    before = dict(coordinator.data)

    cloud["relations"] = [False]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is False
    assert coordinator.data == before
    assert entry.options["user_token"] == "valid-token"

    cloud["relations"] = [{"installation_id": "site-A"}]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.last_update_success is True
    assert coordinator.data == before
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("transition", ["unload", "reload"])
async def test_delayed_write_401_cannot_reauth_retired_entry(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    transition: str,
) -> None:
    """A write started while loaded must not open reauth after unload/reload."""
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def installations(_request: web.Request) -> web.Response:
        return web.json_response({"installation_relations": []})

    async def delayed_write(_request: web.Request) -> web.Response:
        arrived.set()
        await release.wait()
        return web.Response(status=401)

    app = web.Application()
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    app.router.add_put(f"{API_DEVICES}/{{device_id}}", delayed_write)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(token="token-before-transition")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    retired_api = entry.runtime_data.api
    pending = asyncio.create_task(
        retired_api.put_device_fields("dev1", {"sleep_time": 5})
    )
    try:
        await asyncio.wait_for(arrived.wait(), timeout=10)
        if transition == "unload":
            assert await hass.config_entries.async_unload(entry.entry_id)
        else:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.runtime_data.api is not retired_api

        release.set()
        from homeassistant.exceptions import ConfigEntryAuthFailed

        with pytest.raises(ConfigEntryAuthFailed):
            await pending
        await hass.async_block_till_done()
        assert retired_api.on_write_auth_failure is None
        assert not list(hass.config_entries.flow.async_progress_by_handler(DOMAIN))
        if transition == "reload":
            assert entry.state is ConfigEntryState.LOADED
            assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_parallel_write_401_opens_only_one_reauth_flow(
    hass: HomeAssistant,
    socket_enabled: None,
    aiohttp_server,
    monkeypatch: pytest.MonkeyPatch,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
) -> None:
    """Two simultaneously failing writes must start a single native HA flow."""
    arrived = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def installations(_request: web.Request) -> web.Response:
        return web.json_response({"installation_relations": []})

    async def failed_write(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        if requests == 2:
            arrived.set()
        await release.wait()
        return web.Response(status=401)

    app = web.Application()
    app.router.add_get(API_INSTALLATION_RELATIONS, installations)
    app.router.add_put(f"{API_DEVICES}/{{device_id}}", failed_write)
    server = await aiohttp_server(app)
    monkeypatch.setattr(airzone_api, "BASE_URL", str(server.make_url("/")).rstrip("/"))

    entry = dkn_config_entry_factory(token="expired-token")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    api = entry.runtime_data.api
    pending = [
        asyncio.create_task(api.put_device_fields("dev1", {"sleep_time": 5})),
        asyncio.create_task(api.put_device_fields("dev2", {"sleep_time": 6})),
    ]
    try:
        await asyncio.wait_for(arrived.wait(), timeout=10)
        release.set()
        errors = await asyncio.gather(*pending, return_exceptions=True)
        from homeassistant.exceptions import ConfigEntryAuthFailed

        assert all(isinstance(error, ConfigEntryAuthFailed) for error in errors)
        await hass.async_block_till_done()
        flows = list(
            hass.config_entries.flow.async_progress_by_handler(
                DOMAIN,
                match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            )
        )
        assert len(flows) == 1
        assert entry.options["user_token"] == "expired-token"
    finally:
        release.set()
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    # Remove the unfinished reauth flow through HA's flow manager before unload.
    for flow in list(hass.config_entries.flow.async_progress_by_handler(DOMAIN)):
        hass.config_entries.flow.async_abort(flow["flow_id"])
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
