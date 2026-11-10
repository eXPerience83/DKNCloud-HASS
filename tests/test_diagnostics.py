"""Diagnostics export a fixed health schema, never arbitrary backend objects."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.airzoneclouddaikin.const import DOMAIN
from custom_components.airzoneclouddaikin.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"metadata": {"unknown": "private-synthetic"}},
        {"id": "private-synthetic", "name": "private-synthetic"},
        {"opaque": [{"anything": "private-synthetic"}]},
    ],
)
async def test_diagnostics_export_only_allowed_health(hass, payload):
    entry = SimpleNamespace(
        entry_id="synthetic", version=3, data=payload, options=payload
    )
    coordinator = SimpleNamespace(
        data={"synthetic-device": payload}, last_update_success=True
    )
    manager = Mock()
    manager.diagnostics.return_value = {
        "pusher_connected": True,
        "devices": {"device_1": {"last_event_age": 2}},
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "sync_manager": manager,
    }
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert "private-synthetic" not in str(result)
    assert result["coordinator"] == {"devices_count": 1, "last_update_success": True}
    assert result["synchronization"]["overlays_pending"] == 0
    assert result["transport_policy"]["static_rsa_compatibility_enabled"] is False


async def test_tls_policy_diagnostics_are_explicit(hass, monkeypatch):
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_tls.STATIC_RSA_COMPATIBILITY_ENABLED",
        True,
    )
    result = await async_get_config_entry_diagnostics(
        hass, SimpleNamespace(entry_id="synthetic", version=3)
    )
    assert result["transport_policy"] == {"static_rsa_compatibility_enabled": True}
