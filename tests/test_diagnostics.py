"""Diagnostics export a fixed health schema, never arbitrary backend objects."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.airzoneclouddaikin.const import CONF_SLEEP_CLEANUP_ENABLED, DOMAIN
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
    assert result["transport_policy"]["static_rsa_compatibility_enabled"] is True
    assert result["sleep"] == {
        "automatic_cleanup_enabled": True,
        "overdue_devices_now": 0,
        "overdue_sessions_since_restart": 0,
        "last_overdue_utc": None,
    }


async def test_sleep_diagnostics_aggregate_without_leaking_device_ids(hass):
    entry = SimpleNamespace(
        entry_id="synthetic", version=3, options={CONF_SLEEP_CLEANUP_ENABLED: False}
    )
    hass.data.setdefault(DOMAIN, {})["synthetic"] = {
        "coordinator": SimpleNamespace(
            data={
                "sensitive-device-id": {"sleep_overdue": True},
                "other-device": {"sleep_overdue": False},
            },
            last_update_success=True,
        ),
        "sleep_overdue_sessions": 3,
        "sleep_last_overdue_utc": "2026-11-12T19:31:00+00:00",
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["sleep"] == {
        "automatic_cleanup_enabled": False,
        "overdue_devices_now": 1,
        "overdue_sessions_since_restart": 3,
        "last_overdue_utc": "2026-11-12T19:31:00+00:00",
    }
    assert "sensitive-device-id" not in str(diagnostics)


async def test_tls_policy_diagnostics_are_explicit(hass, monkeypatch):
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_tls.STATIC_RSA_COMPATIBILITY_ENABLED",
        True,
    )
    result = await async_get_config_entry_diagnostics(
        hass, SimpleNamespace(entry_id="synthetic", version=3)
    )
    assert result["transport_policy"] == {"static_rsa_compatibility_enabled": True}
