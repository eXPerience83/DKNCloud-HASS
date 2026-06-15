"""Tests for sensor setup behaviors."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.airzoneclouddaikin import sensor
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
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}

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
