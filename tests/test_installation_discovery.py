"""Installation ID parity across the initial refresh and topology discovery."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers import entity_registry as er

import custom_components.airzoneclouddaikin as integration
from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.request_budget import RequestBudget


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        pytest.param(
            {
                "installation": {"id": "site", "installation_id": "nested-alias"},
                "installation_id": "outer",
                "id": "relation",
            },
            "site",
            id="nested-id",
        ),
        pytest.param(
            {"installation": {"installation_id": "site"}, "installation_id": "outer"},
            "site",
            id="nested-alias",
        ),
        pytest.param(
            {"installation_id": "site", "id": "relation"},
            "site",
            id="outer-over-relation",
        ),
        pytest.param(
            {"installation": {}, "installation_id": "site", "id": "relation"},
            "site",
            id="nested-absent",
        ),
        pytest.param(
            {
                "installation": {"id": "", "installation_id": ""},
                "installation_id": "site",
                "id": "relation",
            },
            "site",
            id="nested-empty-outer",
        ),
        pytest.param(
            {"installation": {"installation_id": ""}, "id": "site"},
            "site",
            id="nested-empty-legacy",
        ),
        pytest.param({"id": "site"}, "site", id="legacy-relation"),
        pytest.param(
            {"installation": {"id": "", "installation_id": ""}, "installation_id": ""},
            None,
            id="all-empty",
        ),
    ],
)
async def test_initial_refresh_and_rediscovery_use_same_installation_ids(
    hass, monkeypatch, dkn_config_entry_factory, sample_device, relation, expected
):
    """The same relations must not cause a spurious topology reload."""
    api = Mock()
    api.fetch_installations = AsyncMock(
        return_value=[{"installation_id": "seed"}, relation]
    )
    second_device = dict(sample_device, id="dev2", mac="AA:BB:CC:DD:EE:01")

    async def fetch_devices(installation, *, retry=True, on_start=None):
        if on_start is not None:
            on_start()
        devices = {"seed": sample_device, "site": second_device}
        return [dict(devices[installation])]

    api.fetch_devices = AsyncMock(side_effect=fetch_devices)
    monkeypatch.setattr(integration, "AirzoneAPI", Mock(return_value=api))
    monkeypatch.setattr(
        integration, "build_verified_adapter", Mock(connect=AsyncMock())
    )
    monkeypatch.setattr(integration.SyncManager, "start", Mock())
    now = 0.0

    async def advance(delay):
        nonlocal now
        now += delay

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.sync_manager.RequestBudget",
        lambda clock, sleep: RequestBudget(lambda: now, advance),
    )

    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    try:
        bucket = hass.data[DOMAIN][entry.entry_id]
        manager = bucket["sync_manager"]
        registry = er.async_get(hass)
        initial_calls = [call.args[0] for call in api.fetch_devices.await_args_list]
        initial_entity = registry.async_get_entity_id("climate", DOMAIN, "dev1_climate")

        api.fetch_devices.reset_mock()
        api.fetch_installations.reset_mock()
        reload = Mock()
        monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload)
        await manager._discover_once()

        expected_calls = ["seed"]
        expected_mapping = {"dev1": "seed"}
        expected_cache = {"seed": {"dev1"}}
        if expected is not None:
            expected_calls.append(expected)
            expected_mapping["dev2"] = expected
            expected_cache[expected] = {"dev2"}

        api.fetch_installations.assert_awaited_once_with(retry=False)
        assert [
            call.args[0] for call in api.fetch_devices.await_args_list
        ] == expected_calls
        assert initial_calls == expected_calls
        assert bucket["device_installation_map"] == expected_mapping
        assert bucket["last_devices_by_inst"] == expected_cache
        assert manager._installations == expected_mapping
        assert set(entry.runtime_data.coordinator.data) == set(expected_mapping)
        assert initial_entity is not None
        assert (
            registry.async_get_entity_id("climate", DOMAIN, "dev1_climate")
            == initial_entity
        )
        assert (
            registry.async_get_entity_id("climate", DOMAIN, "dev2_climate") is not None
        ) == (expected is not None)
        reload.assert_not_called()
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_preserves_installation_when_nested_id_becomes_empty(
    hass, dkn_config_entry_factory, sample_device
):
    """A valid outer ID still owns the cached devices after a shape change."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    api = Mock()
    api.fetch_installations = AsyncMock(return_value=[{"installation_id": "site"}])
    api.fetch_devices = AsyncMock(return_value=[dict(sample_device)])

    first = await integration._async_update_data(hass, entry, api)
    assert set(first) == {"dev1"}
    api.fetch_devices.reset_mock()
    api.fetch_installations.return_value = [
        {"installation": {"installation_id": ""}, "installation_id": "site"}
    ]
    second = await integration._async_update_data(hass, entry, api)

    assert [call.args[0] for call in api.fetch_devices.await_args_list] == ["site"]
    assert set(second) == {"dev1"}
    bucket = hass.data[DOMAIN][entry.entry_id]
    assert bucket["device_installation_map"] == {"dev1": "site"}
    assert bucket["last_devices_by_inst"] == {"site": {"dev1"}}
