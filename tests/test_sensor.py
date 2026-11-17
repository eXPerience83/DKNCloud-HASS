"""Tests for sensor setup behaviors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.airzoneclouddaikin import AirzoneRuntimeData, sensor
from custom_components.airzoneclouddaikin.config_flow import CONF_EXPOSE_PII
from custom_components.airzoneclouddaikin.const import DOMAIN


async def test_async_setup_entry_removes_orphan_pii_entities_only(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Opt-out cleanup removes orphan PII sensors even with no device snapshot."""
    entry = dkn_config_entry_factory(options={CONF_EXPOSE_PII: False})
    entry.add_to_hass(hass)
    coordinator = dummy_coordinator_factory({})
    entry.runtime_data = AirzoneRuntimeData(api=Mock(), coordinator=coordinator)

    registry = er.async_get(hass)
    pii_mac = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "olddevice_mac",
        suggested_object_id="olddevice_mac",
        config_entry=entry,
    )
    pii_installation = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "olddevice_installation_id",
        suggested_object_id="olddevice_installation_id",
        config_entry=entry,
    )
    non_pii = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "olddevice_local_temp",
        suggested_object_id="olddevice_local_temp",
        config_entry=entry,
    )
    external = registry.async_get_or_create(
        "sensor",
        "other_platform",
        "olddevice_mac",
        suggested_object_id="external_mac",
        config_entry=entry,
    )
    binary_sensor = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "olddevice_mac",
        suggested_object_id="binary_olddevice_mac",
        config_entry=entry,
    )

    added_entities: list[Any] = []

    await sensor.async_setup_entry(hass, entry, added_entities.extend)

    assert registry.async_get(pii_mac.entity_id) is None
    assert registry.async_get(pii_installation.entity_id) is None
    assert registry.async_get(non_pii.entity_id) is not None
    assert registry.async_get(external.entity_id) is not None
    assert registry.async_get(binary_sensor.entity_id) is not None
    assert added_entities == []


async def test_sync_mode_sensors_are_per_device_readonly_and_user_disableable(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """Mode comes from the manager, without PII, new requests or IDs changing."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    coordinator = dummy_coordinator_factory(
        {"device_a": {"id": "device_a"}, "device_b": {"id": "device_b"}}
    )
    entry.runtime_data = AirzoneRuntimeData(api=Mock(), coordinator=coordinator)
    state = {"device_a": "push", "device_b": "rest_fallback"}
    sync_manager = Mock()
    sync_manager.mode.side_effect = state.get
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "sync_manager": sync_manager,
    }
    added_entities: list[Any] = []
    await sensor.async_setup_entry(hass, entry, added_entities.extend)
    modes = {
        entity._device_id: entity
        for entity in added_entities
        if isinstance(entity, sensor.AirzoneSyncModeSensor)
    }
    assert set(modes) == {"device_a", "device_b"}
    assert modes["device_a"].unique_id == "device_a_sync_mode"
    assert modes["device_b"].unique_id == "device_b_sync_mode"
    for entity in modes.values():
        assert entity.entity_category == sensor.EntityCategory.DIAGNOSTIC
        assert entity.should_poll is False
        assert entity.entity_registry_enabled_default
        assert entity.device_class == sensor.SensorDeviceClass.ENUM
        assert entity.translation_key == "sync_mode"
        # None is NOT equivalent to absence in HA's entity-name resolver.
        assert not hasattr(entity, "_attr_name")
        assert entity.extra_state_attributes is None
    assert modes["device_a"].native_value == "push"
    assert modes["device_b"].native_value == "rest_fallback"
    state.update(device_a="resyncing", device_b="auth_required")
    assert modes["device_a"].native_value == "resyncing"
    assert modes["device_b"].native_value == "auth_required"

    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        modes["device_a"].unique_id,
        suggested_object_id="device_a_sync_mode",
        config_entry=entry,
    )
    disabled = registry.async_update_entity(
        registered.entity_id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    assert disabled.disabled_by == er.RegistryEntryDisabler.USER
    assert modes["device_a"].unique_id == "device_a_sync_mode"
    assert sync_manager.mode.call_count == 4


async def test_sync_mode_sensor_not_created_without_sync_manager(
    hass: HomeAssistant,
    dkn_config_entry_factory: Callable[..., MockConfigEntry],
    dummy_coordinator_factory: Callable[[dict[str, dict[str, Any]], Any | None], Any],
) -> None:
    """REST-only setups without SyncManager keep existing sensors unchanged."""
    entry = dkn_config_entry_factory()
    entry.add_to_hass(hass)
    coordinator = dummy_coordinator_factory({"device_a": {"id": "device_a"}})
    entry.runtime_data = AirzoneRuntimeData(api=Mock(), coordinator=coordinator)
    added_entities: list[Any] = []
    await sensor.async_setup_entry(hass, entry, added_entities.extend)
    assert not any(isinstance(e, sensor.AirzoneSyncModeSensor) for e in added_entities)
